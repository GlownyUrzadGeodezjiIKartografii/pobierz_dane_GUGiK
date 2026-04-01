# -*- coding: utf-8 -*-
"""
Widget eziudp – zakładka w tabWidget dockwidgetu.

Zmiany:
  - Zamiast cmb_zbior (jeden wybór) – lista checkboxów do wyboru warstw
  - Warstwy zgrupowane: BDSOG (2), EGIB (2), GESUT, BDOT500
"""
import os

from .teryt_scope_widget import TerytScopeWidget

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton,
    QTextEdit, QProgressBar, QGroupBox, QRadioButton,
    QButtonGroup, QMessageBox, QSizePolicy, QCheckBox, QScrollArea,
)
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsProject, QgsGeometry, QgsApplication,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsMessageLog, Qgis,
)
from qgis.gui import QgsMapLayerComboBox

try:
    from qgis.core import QgsMapLayerProxyModel
except ImportError:
    from qgis.gui import QgsMapLayerProxyModel  # type: ignore[no-redef]

from .eziudp_wfs_task import (
       EziudpWfsTask as EziudpDownloadTask, EziudpWfsReport as EziudpDownloadReport,
       AVAILABLE_LAYERS,
   )

LOG_TAG = "PD_GUGiK"


class EziudpWidget(QWidget):
    """Widget zakładki 'Usługi powiatowe (eziudp)'."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._task: EziudpDownloadTask = None
        self._layer_checks: dict = {}   # key → QCheckBox
        self._setup_ui()

    # ------------------------------------------------------------------
    # Budowa UI
    # ------------------------------------------------------------------

    def _setup_ui(self):
        main = QVBoxLayout(self)
        main.setSpacing(6)
        main.setContentsMargins(6, 6, 6, 6)

        # --- Wybór warstw ---
        grp_layers = QGroupBox("Warstwy do pobrania")
        lay_layers = QGridLayout()
        lay_layers.setSpacing(4)

        # Grupuj wpisy wizualnie co 2 kolumny
        for i, entry in enumerate(AVAILABLE_LAYERS):
            chk = QCheckBox(entry["label"])
            chk.setChecked(False)
            lay_layers.addWidget(chk, i // 2, i % 2)
            self._layer_checks[entry["key"]] = chk

        # Przyciski zaznacz/odznacz wszystko
        row_sel = QHBoxLayout()
        btn_all = QPushButton("Wszystkie")
        btn_none = QPushButton("Żadne")
        btn_all.setMaximumWidth(80)
        btn_none.setMaximumWidth(80)
        btn_all.clicked.connect(lambda: self._check_all(True))
        btn_none.clicked.connect(lambda: self._check_all(False))
        row_sel.addWidget(btn_all)
        row_sel.addWidget(btn_none)
        row_sel.addStretch()
        lay_layers.addLayout(row_sel, (len(AVAILABLE_LAYERS) + 1) // 2, 0, 1, 2)
        grp_layers.setLayout(lay_layers)
        main.addWidget(grp_layers)

        # --- Zakres ---
        grp_zakres = QGroupBox("Zakres")
        lay_zakres = QVBoxLayout()

        self.radio_teryt = QRadioButton("Granica administracyjna (TERYT)")
        self.radio_canvas = QRadioButton("Zasięg widoku mapy")
        self.radio_layer = QRadioButton("Warstwa poligonowa")
        self.radio_teryt.setChecked(True)

        btn_grp = QButtonGroup(self)
        for rb in (self.radio_teryt, self.radio_canvas, self.radio_layer):
            btn_grp.addButton(rb)
            lay_zakres.addWidget(rb)

        # *** NOWE: TerytScopeWidget zamiast txt_teryt + prowizoryczny completer ***
        self.teryt_scope = TerytScopeWidget()
        lay_zakres.addWidget(self.teryt_scope)

        # Warstwa poligonowa – bez zmian
        row_layer = QHBoxLayout()
        row_layer.addWidget(QLabel("Warstwa:"))
        self.cmb_layer = QgsMapLayerComboBox()
        try:
            self.cmb_layer.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)
        except AttributeError:
            self.cmb_layer.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        row_layer.addWidget(self.cmb_layer, 1)
        lay_zakres.addLayout(row_layer)

        grp_zakres.setLayout(lay_zakres)
        main.addWidget(grp_zakres)

        # --- Przyciski ---
        row_btn = QHBoxLayout()
        self.btn_start = QPushButton("Pobierz dane")
        self.btn_cancel = QPushButton("Przerwij")
        self.btn_cancel.setEnabled(False)
        row_btn.addWidget(self.btn_start)
        row_btn.addWidget(self.btn_cancel)
        row_btn.addStretch()
        main.addLayout(row_btn)

        # --- Pasek postępu ---
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        main.addWidget(self.progress_bar)

        # --- Log ---
        main.addWidget(QLabel("Dziennik:"))
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.log_edit.setStyleSheet("font-family: monospace; font-size: 10px;")
        main.addWidget(self.log_edit, 1)

        # --- Sygnały ---
        self.btn_start.clicked.connect(self._on_start)
        self.btn_cancel.clicked.connect(self._on_cancel)
        self.radio_teryt.toggled.connect(self._update_input_state)
        self.radio_layer.toggled.connect(self._update_input_state)
        self._update_input_state()

    def _check_all(self, state: bool):
        for chk in self._layer_checks.values():
            chk.setChecked(state)

    def _update_input_state(self):
        # *** ZMIANA: teryt_scope zamiast txt_teryt ***
        self.teryt_scope.setEnabled(self.radio_teryt.isChecked())
        self.cmb_layer.setEnabled(self.radio_layer.isChecked())

    def inject_scope_data(
        self,
        wojewodztwa: dict,
        powiaty: dict,
        gminy: dict,
        name_to_teryt: dict,
    ):
        """
        Przekazuje dane referencyjne do wbudowanego TerytScopeWidget.
        Wywoływana przez dockwidget po load_data().
        """
        self.teryt_scope.inject_scope_data(
            wojewodztwa, powiaty, gminy, name_to_teryt
        )

    # ------------------------------------------------------------------
    # Obsługa zdarzeń
    # ------------------------------------------------------------------

    def _on_start(self):
        layer_keys = [k for k, chk in self._layer_checks.items() if chk.isChecked()]
        if not layer_keys:
            QMessageBox.warning(self, "Brak wyboru", "Zaznacz co najmniej jedną warstwę.")
            return

        teryt_list, bbox_wkt, clip_geom_wkt, err = self._resolve_scope()
        if err:
            QMessageBox.warning(self, "Błąd zakresu", err)
            return
        if not teryt_list:
            QMessageBox.warning(
                self, "Brak zakresu",
                "Nie znaleziono powiatów dla podanego zakresu.",
            )
            return

        labels = [AVAILABLE_LAYERS[i]["label"]
                  for i, e in enumerate(AVAILABLE_LAYERS) if e["key"] in layer_keys]
        self._log(
            f"Warstwy: {', '.join(labels)}\n"
            f"Powiaty: {', '.join(teryt_list)}"
        )

        dock = self._dock()
        plugin_dir = ""
        load_style = False
        if dock:
            try:
                import inspect
                plugin_dir = os.path.dirname(inspect.getfile(type(dock)))
            except Exception:
                pass
            chk = getattr(dock, "chk_load_style", None)
            load_style = chk.isChecked() if chk is not None else False

        self._task = EziudpDownloadTask(
            teryt_list=teryt_list,
            layer_keys=layer_keys,
            bbox_wkt=bbox_wkt,
            clip_geom_wkt=clip_geom_wkt,
            plugin_dir=plugin_dir,
            load_style=load_style,
        )
        self._task.downloadFinished.connect(self._on_finished)
        self._task.progressMessage.connect(self._log)
        self._task.progressChanged.connect(lambda p: self.progress_bar.setValue(int(p)))

        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        QgsApplication.taskManager().addTask(self._task)

    def _on_cancel(self):
        if self._task:
            self._task.cancel()
        self._log("Anulowanie…")
        self.btn_cancel.setEnabled(False)

    def _on_finished(self, features: list, report: EziudpDownloadReport):
        self.progress_bar.setVisible(False)
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self._log("\n" + report.format_summary())

        if report.total_features == 0:
            QMessageBox.warning(self, "Brak danych",
                                "Nie pobrano żadnych obiektów.\nSprawdź dziennik.")
        else:
            QMessageBox.information(
                self, "Zakończono",
                f"Pobrano {report.total_features} obiektów "
                f"z {report.successful}/{report.total_teryts} powiatów.",
            )

    # ------------------------------------------------------------------
    # Wyznaczanie zakresu
    # ------------------------------------------------------------------

    def _resolve_scope(self):
        """Zwraca (teryt_list, bbox_wkt, clip_geom_wkt, error_str)."""
        if self.radio_teryt.isChecked():
            raw = self.teryt_scope.get_resolved_teryt()  # ← zamiast txt_teryt.text()
            if not raw:
                return [], None, None, "Wybierz obszar administracyjny."

            if len(raw) == 4:
                return [raw], None, None, None

            if len(raw) == 2:
                dock = self._dock()
                tlist = [t for t in getattr(dock, "powiaty", {}) if t.startswith(raw)]
                if not tlist:
                    return [], None, None, f"Nie znaleziono powiatów dla województwa '{raw}'."
                return tlist, None, None, None

            if len(raw) >= 6:
                powiat = raw[:4]
                clip_geom ,bbox_wkt = self._geom_for_teryt(raw)
                if clip_geom is None:
                    return [powiat], None, None, None
                return [powiat], bbox_wkt, clip_geom.asWkt(), None

            return [], None, None, f"Nie można rozwinąć kodu TERYT: '{raw}'."

        if self.radio_canvas.isChecked():
            iface = self._iface()
            if not iface:
                return [], None, None, "Brak dostępu do interfejsu QGIS."
            ext = iface.mapCanvas().extent()
            src_crs = iface.mapCanvas().mapSettings().destinationCrs()
            bbox_wkt = self._to_wkt_2180(ext, src_crs)
            teryt_list = self._teryts_from_bbox_wkt(bbox_wkt)
            if not teryt_list:
                return [], None, None, "Nie znaleziono powiatów w zasięgu mapy."
            return teryt_list, bbox_wkt, bbox_wkt, None

        if self.radio_layer.isChecked():
            layer = self.cmb_layer.currentLayer()
            if not layer:
                return [], None, None, "Nie wybrano warstwy poligonowej."
            dissolved = None
            for feat in layer.getFeatures():
                g = feat.geometry()
                dissolved = g if dissolved is None else dissolved.combine(g)
            if not dissolved or dissolved.isEmpty():
                return [], None, None, "Warstwa nie zawiera geometrii."
            tgt = QgsCoordinateReferenceSystem("EPSG:2180")
            if layer.crs() != tgt:
                xf = QgsCoordinateTransform(layer.crs(), tgt, QgsProject.instance())
                dissolved.transform(xf)
            bbox_wkt = dissolved.boundingBox().asWktPolygon()
            teryt_list = self._teryts_from_bbox_wkt(bbox_wkt)
            if not teryt_list:
                return [], None, None, "Nie znaleziono powiatów przecinających warstwę."
            return teryt_list, bbox_wkt, dissolved.asWkt(), None

        return [], None, None, "Wybierz metodę wyznaczania zakresu."

    def _geom_for_teryt(self, teryt: str):
        dock = self._dock()
        prg = getattr(dock, "prg_client", None)
        if prg is None:
            return None, None
        try:
            geom = prg.get_boundary_geometry(teryt)

            convex_hull = geom.convexHull()
            if convex_hull and not convex_hull.isEmpty():
                hull_coords = convex_hull.constGet().nCoordinates()
                if hull_coords <= 200:
                    QgsMessageLog.logMessage(
                    f"[EziudpWidget] Trying {teryt}: convex hull",
                    LOG_TAG, Qgis.MessageLevel.Warning,
                    )
                    return geom, convex_hull.asWkt()
                
            if geom and not geom.isEmpty():
                QgsMessageLog.logMessage(
                    f"[EziudpWidget] Trying {teryt}: bbox",
                    LOG_TAG, Qgis.MessageLevel.Warning,
                    )
                return geom, geom.boundingBox().asWktPolygon()
        except Exception as exc:
            QgsMessageLog.logMessage(
                f"[EziudpWidget] PRG błąd dla TERYT {teryt}: {exc}",
                LOG_TAG, Qgis.MessageLevel.Warning,
            )
        return None, None
    
    def _get_powiaty_index(self):
        """Zwraca PowiatyIndex (singleton) lub None gdy brak plugin_dir."""
        dock = self._dock()
        plugin_dir = ""
        """if dock:
            try:
                import inspect
                plugin_dir = os.path.dirname(inspect.getfile(type(dock)))
            except Exception:
                pass
        if not plugin_dir:
            return None
        """
        plugin_dir = os.path.dirname(__file__)
        
        from .powiaty_index import PowiatyIndex
        return PowiatyIndex.instance(plugin_dir)

    def _teryts_from_bbox_wkt(self, bbox_wkt: str) -> list:
        """
        Zwraca listę 4-cyfrowych TERYT-ów powiatów przecinających bbox_wkt.
 
        Korzysta z lokalnego pliku index/powiaty.geojson (przez PowiatyIndex)
        zamiast usługi WFS/PRG. Szybkie – indeks przestrzenny QgsSpatialIndex.
        bbox_wkt musi być w EPSG:2180.
        """
        bbox_geom = QgsGeometry.fromWkt(bbox_wkt)
        if bbox_geom is None or bbox_geom.isEmpty():
            return []
 
        idx = self._get_powiaty_index()
        if idx is None or not idx.is_loaded():
            QgsMessageLog.logMessage(
                "[EziudpWidget] PowiatyIndex niedostępny – brak plugin_dir.",
                LOG_TAG, Qgis.MessageLevel.Warning,
            )
            return []
 
        return idx.teryts_intersecting(bbox_geom)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _dock(self):
        w = self.parent()
        while w is not None:
            if hasattr(w, "iface") and hasattr(w, "powiaty"):
                return w
            w = w.parent()
        return None

    def _iface(self):
        return getattr(self._dock(), "iface", None)

    def _to_wkt_2180(self, extent, src_crs) -> str:
        tgt = QgsCoordinateReferenceSystem("EPSG:2180")
        if src_crs != tgt:
            xf = QgsCoordinateTransform(src_crs, tgt, QgsProject.instance())
            extent = xf.transformBoundingBox(extent)
        return extent.asWktPolygon()

    def _log(self, msg: str):
        self.log_edit.append(str(msg))
        sb = self.log_edit.verticalScrollBar()
        sb.setValue(sb.maximum())