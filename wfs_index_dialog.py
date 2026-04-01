from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QTableWidget, QTableWidgetItem, QPushButton,
    QHBoxLayout, QAbstractItemView, QHeaderView, QRadioButton, QGroupBox,
    QMenu, QAction, QWidgetAction, QListWidget, QListWidgetItem, QLineEdit,
    QWidget, QLabel,
)
from qgis.PyQt.QtCore import Qt, QPoint
from qgis.PyQt.QtGui import QColor, QIcon, QFont
from qgis.core import QgsVectorLayer, QgsProject, QgsFeature


# Indeks kolumny checkboxa
_COL_CHK = 0


class _FilterState:
    """Przechowuje aktywne filtry i stan sortowania dla całej tabeli."""

    def __init__(self):
        # col_index -> set of allowed values (None = brak filtra)
        self.active: dict[int, set[str] | None] = {}
        self.sort_col: int = -1
        self.sort_asc: bool = True

    def is_filtered(self, col: int) -> bool:
        return self.active.get(col) is not None

    def passes(self, col: int, value: str) -> bool:
        allowed = self.active.get(col)
        return allowed is None or value in allowed

    def set_filter(self, col: int, allowed: set[str] | None):
        if allowed is None:
            self.active.pop(col, None)
        else:
            self.active[col] = allowed

    def clear(self):
        self.active.clear()


class WfsIndexResultsDialog(QDialog):
    def __init__(self, features_data, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Dostępne dane do pobrania ze skorowidza")
        self.resize(960, 560)

        self.features_data = features_data
        self.selected_urls: list[tuple[str, str]] = []
        self.load_mode = "download"

        self._filter_state = _FilterState()
        # Cache: col_index -> lista wszystkich unikalnych wartości w tej kolumnie
        self._unique_values: dict[int, list[str]] = {}
        # Mapa: row w tabeli -> indeks w features_data
        self._row_to_feat: list[int] = []

        # Warstwa tymczasowa
        self.temp_layer = QgsVectorLayer(
            "Polygon?crs=epsg:2180",
            "Zasięg wybranych danych (tymczasowy)",
            "memory",
        )
        self._setup_temp_layer_style()
        QgsProject.instance().addMapLayer(self.temp_layer)

        self._setup_ui()
        self._populate_table()
        self._build_unique_values_cache()
        self._apply_filters_and_sort()   # pierwszy render mapy

    # ------------------------------------------------------------------
    # Warstwa tymczasowa
    # ------------------------------------------------------------------

    def _setup_temp_layer_style(self):
        symbol = self.temp_layer.renderer().symbol()
        symbol.setColor(QColor(0, 150, 255, 60))
        symbol.symbolLayer(0).setStrokeColor(QColor(0, 50, 255, 200))
        symbol.symbolLayer(0).setStrokeWidth(0.8)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # Tryb wczytywania
        mode_group = QGroupBox("Opcje wczytywania")
        mode_layout = QHBoxLayout()
        self.radio_vsicurl = QRadioButton("Wczytaj jako strumień w locie (/vsicurl/) – oszczędza dysk")
        self.radio_download = QRadioButton("Pobierz fizycznie pliki na dysk")
        self.radio_download.setChecked(True)
        mode_layout.addWidget(self.radio_download)
        mode_group.setLayout(mode_layout)
        layout.addWidget(mode_group)

        # Pasek zaznaczenia + info o filtrach
        sel_layout = QHBoxLayout()
        self.btn_sel_all   = QPushButton("Zaznacz wszystko")
        self.btn_desel_all = QPushButton("Odznacz wszystko")
        self.btn_inv_sel   = QPushButton("Odwróć zaznaczenie")
        self.btn_clear_filters = QPushButton("Wyczyść filtry")
        self.btn_clear_filters.setEnabled(False)
        self.lbl_tip = QLabel("Kliknij na kolumne aby po sortować, kliknij prawym, aby filtrować")

        self.lbl_status = QLabel("")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        sel_layout.addWidget(self.btn_sel_all)
        sel_layout.addWidget(self.btn_desel_all)
        sel_layout.addWidget(self.btn_inv_sel)
        sel_layout.addWidget(self.btn_clear_filters)
        sel_layout.addWidget(self.lbl_tip)
        sel_layout.addStretch()
        sel_layout.addWidget(self.lbl_status)
        layout.addLayout(sel_layout)

        # Tabela
        self.table = QTableWidget()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSortingEnabled(False)   # sortujemy ręcznie
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionsClickable(True)
        hh.sectionClicked.connect(self._on_header_clicked)
        layout.addWidget(self.table)

        # Przyciski dolne
        btn_layout = QHBoxLayout()
        self.btn_download = QPushButton("Dodaj wybrane do mapy")
        self.btn_cancel   = QPushButton("Anuluj")
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_download)
        layout.addLayout(btn_layout)

        # Sygnały
        self.btn_download.clicked.connect(self.accept_selection)
        self.btn_cancel.clicked.connect(self.reject)
        self.table.itemChanged.connect(self._on_item_changed)

        self.btn_sel_all.clicked.connect(self._select_all)
        self.btn_desel_all.clicked.connect(self._deselect_all)
        self.btn_inv_sel.clicked.connect(self._invert_selection)
        self.btn_clear_filters.clicked.connect(self._clear_all_filters)

    # ------------------------------------------------------------------
    # Wypełnianie tabeli (jednorazowe – tylko surowe dane)
    # ------------------------------------------------------------------

    def _populate_table(self):
        if not self.features_data:
            return

        sample_attrs = self.features_data[0]["attrs"]
        self._visible_cols = [
            k for k in sample_attrs.keys() if k not in ("gml_id", "msGeometry")
        ]

        # +1 kolumna checkboxa, +1 kolumna „Filtr" (ukryta technicznie = obsługiwana nagłówkiem)
        col_count = len(self._visible_cols) + 1   # 0 = checkbox, 1..N = atrybuty
        self.table.setColumnCount(col_count)

        # Nagłówki – do etykiety dołączamy ikonę filtra (tekst)
        self._update_header_labels()

        self.table.setRowCount(len(self.features_data))
        self._row_to_feat = list(range(len(self.features_data)))

        self.table.blockSignals(True)
        for row, feat in enumerate(self.features_data):
            chk_item = QTableWidgetItem()
            chk_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk_item.setCheckState(Qt.CheckState.Checked)
            chk_item.setData(Qt.ItemDataRole.UserRole, row)
            self.table.setItem(row, _COL_CHK, chk_item)

            for col, attr_key in enumerate(self._visible_cols, start=1):
                val = str(feat["attrs"].get(attr_key, ""))
                item = QTableWidgetItem(val)
                item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                self.table.setItem(row, col, item)

        self.table.blockSignals(False)

    def _update_header_labels(self):
        """Odświeża etykiety nagłówków – dodaje ▲/▼ przy sortowanej kolumnie i 🔽 przy filtrowanej."""
        headers = ["Dodaj"]
        for col_idx, col_name in enumerate(self._visible_cols, start=1):
            label = col_name
            # Wskaźnik sortowania
            if self._filter_state.sort_col == col_idx:
                label += "  ▲" if self._filter_state.sort_asc else "  ▼"
            # Wskaźnik aktywnego filtra
            if self._filter_state.is_filtered(col_idx):
                label += "  ●"
            headers.append(label)
        self.table.setHorizontalHeaderLabels(headers)

    # ------------------------------------------------------------------
    # Cache unikalnych wartości
    # ------------------------------------------------------------------

    def _build_unique_values_cache(self):
        self._unique_values.clear()
        for col_idx in range(1, self.table.columnCount()):
            vals: set[str] = set()
            for row in range(self.table.rowCount()):
                item = self.table.item(row, col_idx)
                if item:
                    vals.add(item.text())
            self._unique_values[col_idx] = sorted(vals, key=lambda x: x.lower())

    # ------------------------------------------------------------------
    # Sortowanie i widoczność wierszy
    # ------------------------------------------------------------------

    def _on_header_clicked(self, logical_col: int):
        if logical_col == _COL_CHK:
            return

        fs = self._filter_state
        if fs.sort_col == logical_col:
            if fs.sort_asc:
                fs.sort_asc = False
            else:
                # Trzecie kliknięcie – usuwa sortowanie
                fs.sort_col = -1
                fs.sort_asc = True
        else:
            fs.sort_col = logical_col
            fs.sort_asc = True

        self._apply_filters_and_sort()

    def _apply_filters_and_sort(self):
        """
        Centralna metoda porządkująca tabelę:
        1. Zbiera wszystkie wiersze z danymi.
        2. Filtruje – ukrywa/odznacza wiersze niespełniające filtrów.
        3. Sortuje widoczne wiersze.
        4. Aktualizuje nagłówki i mapę.
        """
        fs = self._filter_state

        # Dane każdego wiersza: (feat_idx, {col: text})
        all_rows: list[tuple[int, dict[int, str]]] = []
        for row in range(self.table.rowCount()):
            chk = self.table.item(row, _COL_CHK)
            if chk is None:
                continue
            feat_idx = chk.data(Qt.ItemDataRole.UserRole)
            row_data: dict[int, str] = {}
            for col in range(1, self.table.columnCount()):
                item = self.table.item(row, col)
                row_data[col] = item.text() if item else ""
            all_rows.append((feat_idx, row_data))

        # Podział na pasujące i niepasujące filtrom
        visible_rows:  list[tuple[int, dict[int, str]]] = []
        hidden_rows:   list[tuple[int, dict[int, str]]] = []
        for feat_idx, row_data in all_rows:
            passes = all(
                fs.passes(col, row_data.get(col, ""))
                for col in fs.active
            )
            if passes:
                visible_rows.append((feat_idx, row_data))
            else:
                hidden_rows.append((feat_idx, row_data))

        # Sortowanie widocznych wierszy
        if fs.sort_col > 0:
            visible_rows.sort(
                key=lambda r: r[1].get(fs.sort_col, "").lower(),
                reverse=not fs.sort_asc,
            )

        # Przepisanie wierszy do tabeli (blokujemy sygnały)
        self.table.blockSignals(True)

        # Połącz: widoczne na górze, ukryte na dole
        ordered = visible_rows + hidden_rows
        for new_row, (feat_idx, _) in enumerate(ordered):
            # Aktualizuj UserRole checkboxa
            chk = self.table.item(new_row, _COL_CHK)
            if chk:
                chk.setData(Qt.ItemDataRole.UserRole, feat_idx)

        # Przebuduj zawartość wierszy zgodnie z nową kolejnością
        self._rewrite_table_rows(ordered)

        # Ukryj wiersze niewidoczne
        visible_count = len(visible_rows)
        for row in range(self.table.rowCount()):
            is_hidden = row >= visible_count
            self.table.setRowHidden(row, is_hidden)
            if is_hidden:
                # Odznacz ukryte wiersze
                chk = self.table.item(row, _COL_CHK)
                if chk:
                    chk.setCheckState(Qt.CheckState.Unchecked)

        self.table.blockSignals(False)

        self._update_header_labels()
        self._update_status_label(visible_count, len(all_rows))
        self.btn_clear_filters.setEnabled(bool(fs.active))
        self.update_temp_map_layer()

    def _rewrite_table_rows(self, ordered: list[tuple[int, dict[int, str]]]):
        """Nadpisuje zawartość tabeli zgodnie z podaną kolejnością."""
        for new_row, (feat_idx, row_data) in enumerate(ordered):
            # Checkbox
            chk = self.table.item(new_row, _COL_CHK)
            if chk is None:
                chk = QTableWidgetItem()
                chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                chk.setCheckState(Qt.CheckState.Checked)
                self.table.setItem(new_row, _COL_CHK, chk)
            chk.setData(Qt.ItemDataRole.UserRole, feat_idx)

            # Komórki danych
            for col in range(1, self.table.columnCount()):
                item = self.table.item(new_row, col)
                text = self.features_data[feat_idx]["attrs"].get(
                    self._visible_cols[col - 1], ""
                )
                text = str(text)
                if item is None:
                    item = QTableWidgetItem(text)
                    item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                    self.table.setItem(new_row, col, item)
                else:
                    item.setText(text)

    # ------------------------------------------------------------------
    # Dropdown filtrowania
    # ------------------------------------------------------------------

    def _on_header_clicked(self, logical_col: int):  # noqa: F811 – override
        """
        Klik lewym: sortowanie.
        Klik z Ctrl / klik prawym: filtrowanie (dropdown).
        Tutaj obsługujemy LEWYM – ale ikona filtra w nagłówku jest
        kliknięta przez contextMenu zdarzenie nagłówka.

        Żeby mieć jedno zdarzenie dla obu akcji rozdzielamy:
        - klik bez modyfikatora → sortowanie
        - klik z Ctrl → filtr
        """
        modifiers = __import__("qgis.PyQt.QtWidgets", fromlist=["QApplication"]).QApplication.keyboardModifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier or logical_col == _COL_CHK:
            if logical_col != _COL_CHK:
                self._show_filter_menu(logical_col)
            return

        # Sortowanie
        fs = self._filter_state
        if fs.sort_col == logical_col:
            if fs.sort_asc:
                fs.sort_asc = False
            else:
                fs.sort_col = -1
                fs.sort_asc = True
        else:
            fs.sort_col = logical_col
            fs.sort_asc = True

        self._apply_filters_and_sort()

    def _show_filter_menu(self, col: int):
        """Pokazuje dropdown z listą unikalnych wartości dla danej kolumny."""
        unique = self._unique_values.get(col, [])
        if not unique:
            return

        current_filter: set[str] | None = self._filter_state.active.get(col)

        # Tworzymy menu
        menu = QMenu(self)

        # Pole wyszukiwania wewnątrz menu
        search_widget = QWidget()
        search_layout = QHBoxLayout(search_widget)
        search_layout.setContentsMargins(6, 4, 6, 2)
        search_edit = QLineEdit()
        search_edit.setPlaceholderText("Szukaj…")
        search_layout.addWidget(search_edit)

        wa_search = QWidgetAction(menu)
        wa_search.setDefaultWidget(search_widget)
        menu.addAction(wa_search)

        # Lista wartości
        list_widget = QListWidget()
        list_widget.setFixedWidth(260)
        list_widget.setMaximumHeight(220)

        def _populate_list(filter_text: str = ""):
            list_widget.clear()
            for val in unique:
                if filter_text.lower() not in val.lower():
                    continue
                item = QListWidgetItem(val)
                item.setFlags(
                    Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                )
                if current_filter is None or val in current_filter:
                    item.setCheckState(Qt.CheckState.Checked)
                else:
                    item.setCheckState(Qt.CheckState.Unchecked)
                list_widget.addItem(item)

        _populate_list()
        search_edit.textChanged.connect(_populate_list)

        wa_list = QWidgetAction(menu)
        wa_list.setDefaultWidget(list_widget)
        menu.addAction(wa_list)

        menu.addSeparator()

        # Przyciski Zaznacz wszystko / Odznacz wszystko wewnątrz menu
        def _check_all():
            for i in range(list_widget.count()):
                list_widget.item(i).setCheckState(Qt.CheckState.Checked)

        def _uncheck_all():
            for i in range(list_widget.count()):
                list_widget.item(i).setCheckState(Qt.CheckState.Unchecked)

        act_all  = QAction("Zaznacz wszystkie", menu)
        act_none = QAction("Odznacz wszystkie", menu)
        act_apply  = QAction("✔ Zastosuj filtr", menu)
        act_clear  = QAction("✖ Wyczyść filtr kolumny", menu)

        act_all.triggered.connect(_check_all)
        act_none.triggered.connect(_uncheck_all)
        menu.addAction(act_all)
        menu.addAction(act_none)
        menu.addSeparator()
        menu.addAction(act_apply)
        menu.addAction(act_clear)

        # Pozycja menu – pod nagłówkiem kolumny
        header = self.table.horizontalHeader()
        x = header.sectionViewportPosition(col)
        pos = header.mapToGlobal(QPoint(x, header.height()))

        chosen = menu.exec(pos)

        if chosen == act_apply:
            checked_vals: set[str] = set()
            for i in range(list_widget.count()):
                it = list_widget.item(i)
                if it.checkState() == Qt.CheckState.Checked:
                    checked_vals.add(it.text())
            # Jeśli zaznaczono wszystkie – traktuj jako brak filtra
            if checked_vals == set(unique):
                self._filter_state.set_filter(col, None)
            else:
                self._filter_state.set_filter(col, checked_vals)
            self._apply_filters_and_sort()

        elif chosen == act_clear:
            self._filter_state.set_filter(col, None)
            self._apply_filters_and_sort()

    # ------------------------------------------------------------------
    # Kontekstowe menu nagłówka (prawy klik → filtr)
    # ------------------------------------------------------------------

    def _setup_header_context_menu(self):
        hh = self.table.horizontalHeader()
        hh.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._on_header_context_menu)

    def _on_header_context_menu(self, pos: QPoint):
        col = self.table.horizontalHeader().logicalIndexAt(pos)
        if col > _COL_CHK:
            self._show_filter_menu(col)

    # ------------------------------------------------------------------
    # Masowa selekcja
    # ------------------------------------------------------------------

    def _select_all(self):
        self._change_all_visible_states(Qt.CheckState.Checked)

    def _deselect_all(self):
        self._change_all_visible_states(Qt.CheckState.Unchecked)

    def _invert_selection(self):
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            if self.table.isRowHidden(row):
                continue
            item = self.table.item(row, _COL_CHK)
            if item:
                new_state = (
                    Qt.CheckState.Unchecked
                    if item.checkState() == Qt.CheckState.Checked
                    else Qt.CheckState.Checked
                )
                item.setCheckState(new_state)
        self.table.blockSignals(False)
        self.update_temp_map_layer()

    def _change_all_visible_states(self, state: Qt.CheckState):
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            if self.table.isRowHidden(row):
                continue
            item = self.table.item(row, _COL_CHK)
            if item:
                item.setCheckState(state)
        self.table.blockSignals(False)
        self.update_temp_map_layer()

    def _clear_all_filters(self):
        self._filter_state.clear()
        self._apply_filters_and_sort()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _update_status_label(self, visible: int, total: int):
        if visible == total:
            self.lbl_status.setText(f"{total} rekordów")
        else:
            hidden = total - visible
            self.lbl_status.setText(
                f"{visible} z {total} rekordów  •  ukrytych: {hidden}"
            )

    # ------------------------------------------------------------------
    # Mapa tymczasowa
    # ------------------------------------------------------------------

    def _on_item_changed(self, item: QTableWidgetItem):
        if item.column() == _COL_CHK:
            self.update_temp_map_layer()

    def update_temp_map_layer(self):
        pr = self.temp_layer.dataProvider()
        if self.temp_layer.featureCount() > 0:
            pr.deleteFeatures([f.id() for f in self.temp_layer.getFeatures()])

        feats = []
        for row in range(self.table.rowCount()):
            if self.table.isRowHidden(row):
                continue
            chk_item = self.table.item(row, _COL_CHK)
            if chk_item and chk_item.checkState() == Qt.CheckState.Checked:
                idx = chk_item.data(Qt.ItemDataRole.UserRole)
                geom = self.features_data[idx]["geom"]
                f = QgsFeature()
                f.setGeometry(geom)
                feats.append(f)

        if feats:
            pr.addFeatures(feats)
        self.temp_layer.triggerRepaint()

        from qgis.utils import iface
        canvas = iface.mapCanvas()
        if feats:
            extent = self.temp_layer.extent()
            extent.scale(1.1)
            canvas.setExtent(extent)
            canvas.refresh()

    # ------------------------------------------------------------------
    # Akceptacja
    # ------------------------------------------------------------------

    def accept_selection(self):
        self.selected_urls = []
        for row in range(self.table.rowCount()):
            if self.table.isRowHidden(row):
                continue
            chk_item = self.table.item(row, _COL_CHK)
            if chk_item and chk_item.checkState() == Qt.CheckState.Checked:
                idx = chk_item.data(Qt.ItemDataRole.UserRole)
                attrs = self.features_data[idx]["attrs"]
                url = attrs.get("url_do_pobrania", "") or attrs.get("URL_GML", "")
                if url:
                    name = attrs.get("godlo", url.split("/")[-1])
                    self.selected_urls.append((name, url))

        self.load_mode = "download" if self.radio_download.isChecked() else "vsicurl"
        self.accept()

    # ------------------------------------------------------------------
    # Zamknięcie
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        QgsProject.instance().removeMapLayer(self.temp_layer.id())
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Inicjalizacja menu kontekstowego (wywołana po setup_ui)
    # ------------------------------------------------------------------

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        mode_group = QGroupBox("Opcje wczytywania")
        mode_layout = QHBoxLayout()
        self.radio_vsicurl  = QRadioButton("Wczytaj jako strumień w locie (/vsicurl/) – oszczędza dysk")
        self.radio_download = QRadioButton("Pobierz fizycznie pliki na dysk")
        self.radio_download.setChecked(True)
        mode_layout.addWidget(self.radio_download)
        mode_group.setLayout(mode_layout)
        layout.addWidget(mode_group)

        sel_layout = QHBoxLayout()
        self.btn_sel_all       = QPushButton("Zaznacz wszystko")
        self.btn_desel_all     = QPushButton("Odznacz wszystko")
        self.btn_inv_sel       = QPushButton("Odwróć zaznaczenie")
        self.btn_clear_filters = QPushButton("Wyczyść filtry")
        self.btn_clear_filters.setEnabled(False)
        self.lbl_status = QLabel("")
        self.lbl_status.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        sel_layout.addWidget(self.btn_sel_all)
        sel_layout.addWidget(self.btn_desel_all)
        sel_layout.addWidget(self.btn_inv_sel)
        sel_layout.addWidget(self.btn_clear_filters)
        sel_layout.addStretch()
        sel_layout.addWidget(self.lbl_status)
        layout.addLayout(sel_layout)

        self.table = QTableWidget()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSortingEnabled(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionsClickable(True)
        hh.sectionClicked.connect(self._on_header_clicked)
        # Prawy klik na nagłówku → filtr
        hh.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._on_header_context_menu)
        layout.addWidget(self.table)

        btn_layout = QHBoxLayout()
        self.btn_download = QPushButton("Dodaj wybrane do mapy")
        self.btn_cancel   = QPushButton("Anuluj")
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_download)
        layout.addLayout(btn_layout)

        self.btn_download.clicked.connect(self.accept_selection)
        self.btn_cancel.clicked.connect(self.reject)
        self.table.itemChanged.connect(self._on_item_changed)
        self.btn_sel_all.clicked.connect(self._select_all)
        self.btn_desel_all.clicked.connect(self._deselect_all)
        self.btn_inv_sel.clicked.connect(self._invert_selection)
        self.btn_clear_filters.clicked.connect(self._clear_all_filters)