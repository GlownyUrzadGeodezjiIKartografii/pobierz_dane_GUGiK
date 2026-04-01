# -*- coding: utf-8 -*-
"""
TerytScopeWidget – samodzielny widget wyboru zakresu administracyjnego.

Dostarcza:
  - kaskadowe comboboxes: województwo → powiat → gmina
  - QLineEdit z QCompleter (MatchContains, jak w tab_admin)
  - walidację TERYT z etykietą informacyjną
  - sygnał teryt_changed(str) emitowany przy każdej zmianie

Użycie w EziudpWidget:
    self.scope = TerytScopeWidget()
    self.scope.inject_scope_data(dock.wojewodztwa, dock.powiaty, dock.gminy,
                                 dock.name_to_teryt)
    layout.addWidget(self.scope)
    teryt = self.scope.get_resolved_teryt()   # np. "1462", "14", "146201_1"
"""
import re

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QComboBox, QCompleter,
)
from qgis.PyQt.QtCore import Qt, pyqtSignal


class TerytScopeWidget(QWidget):
    """
    Panel wyboru zakresu administracyjnego TERYT z podpowiedziami.

    Emituje teryt_changed(str) gdy zmienia się aktywny kod TERYT.
    """

    teryt_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._blocking = False          # guard przed rekurencją sygnałów
        self._name_to_teryt: dict = {}
        self._wojewodztwa: dict = {}
        self._powiaty: dict = {}
        self._gminy: dict = {}
        self._setup_ui()

    # ------------------------------------------------------------------
    # Budowa UI
    # ------------------------------------------------------------------

    def _setup_ui(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(4)
        lay.setContentsMargins(0, 0, 0, 0)

        # Wiersz 1: combobox województwa
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Województwo:"))
        self.cmb_woj = QComboBox()
        self.cmb_woj.addItem("- Wybierz -", None)
        row1.addWidget(self.cmb_woj, 1)
        lay.addLayout(row1)

        # Wiersz 2: combobox powiatu
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Powiat:"))
        self.cmb_pow = QComboBox()
        self.cmb_pow.setEnabled(False)
        self.cmb_pow.addItem("- Wybierz -", None)
        row2.addWidget(self.cmb_pow, 1)
        lay.addLayout(row2)

        # Wiersz 3: combobox gminy
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("Gmina:"))
        self.cmb_gmina = QComboBox()
        self.cmb_gmina.setEnabled(False)
        self.cmb_gmina.addItem("- Wybierz -", None)
        row3.addWidget(self.cmb_gmina, 1)
        lay.addLayout(row3)

        # Wiersz 4: pole tekstowe z completerem
        row4 = QHBoxLayout()
        row4.addWidget(QLabel("TERYT / nazwa:"))
        self.txt_teryt = QLineEdit()
        self.txt_teryt.setPlaceholderText("np. Warszawa, 14, 1462, 146201_1")
        row4.addWidget(self.txt_teryt, 1)
        lay.addLayout(row4)

        # Wiersz 5: etykieta informacyjna (błąd lub aktywny kod)
        self.lbl_info = QLabel("")
        self.lbl_info.setStyleSheet("color: var(--color-text-danger, red); font-size: 11px;")
        lay.addWidget(self.lbl_info)

        # Sygnały
        self.cmb_woj.currentIndexChanged.connect(self._on_woj_changed)
        self.cmb_pow.currentIndexChanged.connect(self._on_pow_changed)
        self.cmb_gmina.currentIndexChanged.connect(self._on_gmina_changed)
        self.txt_teryt.textChanged.connect(self._on_text_changed)

    # ------------------------------------------------------------------
    # Inicjalizacja danych (wywoływana przez dockwidget po load_data)
    # ------------------------------------------------------------------

    def inject_scope_data(
        self,
        wojewodztwa: dict,
        powiaty: dict,
        gminy: dict,
        name_to_teryt: dict,
    ):
        """
        Przekazuje słowniki z danych referencyjnych dockwidgetu.
        Wypełnia cmb_woj i podpina QCompleter.
        Bezpieczne do wywołania wielokrotnie (np. po odświeżeniu danych).
        """
        self._wojewodztwa = wojewodztwa
        self._powiaty = powiaty
        self._gminy = gminy
        self._name_to_teryt = name_to_teryt

        # Wypełnij cmb_woj
        self._blocking = True
        self.cmb_woj.clear()
        self.cmb_woj.addItem("- Wybierz -", None)
        for teryt, info in sorted(
            wojewodztwa.items(), key=lambda x: x[1].get("nazwa", "")
        ):
            self.cmb_woj.addItem(f"{info['nazwa']} ({teryt})", teryt)
        self._blocking = False

        self.cmb_pow.clear()
        self.cmb_pow.setEnabled(False)
        self.cmb_pow.addItem("- Wybierz -", None)
        self.cmb_gmina.clear()
        self.cmb_gmina.setEnabled(False)
        self.cmb_gmina.addItem("- Wybierz -", None)

        # QCompleter
        completer = QCompleter(list(name_to_teryt.keys()), self.txt_teryt)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.txt_teryt.setCompleter(completer)
        completer.activated.connect(self._on_completer_activated)

    # ------------------------------------------------------------------
    # API publiczne
    # ------------------------------------------------------------------

    def get_resolved_teryt(self) -> str:
        """
        Zwraca aktualnie wybrany kod TERYT jako string.

        Priorytet: txt_teryt (po wyczyszczeniu) > cmb_gmina > cmb_pow > cmb_woj.
        Zwraca "" gdy nic nie wybrano.
        """
        raw = self.txt_teryt.text().strip()
        # usuń sufiks "(powiat 1462)" itp. zostawiony przez completer
        raw = raw.split(" ")[-1].replace(")", "").strip() if " " in raw else raw
        if raw:
            return raw
        if gmina := self.cmb_gmina.currentData():
            return gmina
        if pow_ := self.cmb_pow.currentData():
            return pow_
        if woj := self.cmb_woj.currentData():
            return woj
        return ""

    def set_teryt(self, teryt: str):
        """Programowe ustawienie TERYT + synchronizacja kaskady."""
        self.txt_teryt.setText(teryt)
        self._sync_combos_from_teryt(teryt)

    def clear(self):
        """Resetuje widget do stanu początkowego."""
        self._blocking = True
        self.txt_teryt.clear()
        self.cmb_woj.setCurrentIndex(0)
        self.cmb_pow.clear()
        self.cmb_pow.setEnabled(False)
        self.cmb_pow.addItem("- Wybierz -", None)
        self.cmb_gmina.clear()
        self.cmb_gmina.setEnabled(False)
        self.cmb_gmina.addItem("- Wybierz -", None)
        self.lbl_info.setText("")
        self._blocking = False

    # ------------------------------------------------------------------
    # Obsługa sygnałów – kaskada comboboxes
    # (logika wzorowana na dockwidgecie, bez blokowania UI threada)
    # ------------------------------------------------------------------

    def _on_woj_changed(self):
        if self._blocking:
            return
        woj_id = self.cmb_woj.currentData()

        self._blocking = True
        self.cmb_pow.clear()
        self.cmb_pow.addItem("- Wybierz -", None)
        self.cmb_gmina.clear()
        self.cmb_gmina.addItem("- Wybierz -", None)
        self.cmb_gmina.setEnabled(False)
        self._blocking = False

        if woj_id:
            self.cmb_pow.setEnabled(True)
            for teryt, info in sorted(
                self._powiaty.items(), key=lambda x: x[1].get("nazwa", "")
            ):
                if info.get("parent") == woj_id:
                    self.cmb_pow.addItem(f"{info['nazwa']} ({teryt})", teryt)
            self._set_text_silent(woj_id)
        else:
            self.cmb_pow.setEnabled(False)
            self._set_text_silent("")

    def _on_pow_changed(self):
        if self._blocking:
            return
        pow_id = self.cmb_pow.currentData()

        self._blocking = True
        self.cmb_gmina.clear()
        self.cmb_gmina.addItem("- Wybierz -", None)
        self._blocking = False

        if pow_id:
            self.cmb_gmina.setEnabled(True)
            for teryt, info in sorted(
                self._gminy.items(), key=lambda x: x[1].get("nazwa", "")
            ):
                if info.get("parent") == pow_id:
                    self.cmb_gmina.addItem(f"{info['nazwa']} ({teryt})", teryt)
            self._set_text_silent(pow_id)
        else:
            self.cmb_gmina.setEnabled(False)
            woj_id = self.cmb_woj.currentData()
            self._set_text_silent(woj_id or "")

    def _on_gmina_changed(self):
        if self._blocking:
            return
        gmina_id = self.cmb_gmina.currentData()
        if gmina_id:
            self._set_text_silent(gmina_id)
        else:
            pow_id = self.cmb_pow.currentData()
            self._set_text_silent(pow_id or "")

    def _on_completer_activated(self, text: str):
        """Wybór z listy podpowiedzi → wpisz TERYT i synchronizuj kaskadę."""
        teryt = self._name_to_teryt.get(text, "")
        if not teryt:
            # fallback: wyłuskaj z nawiasu np. "(powiat 1462)"
            m = re.search(r'\(([\w.]+)\)', text)
            teryt = m.group(1) if m else text
        self._set_text_silent(teryt)
        self._sync_combos_from_teryt(teryt)
        self.teryt_changed.emit(teryt)

    def _on_text_changed(self, text: str):
        if self._blocking:
            return
        raw = text.strip()
        raw = raw.split(" ")[-1].replace(")", "").strip() if " " in raw else raw
        self._validate(raw)
        # Synchronizuj kaskadę tylko dla kompletnych kodów
        if len(raw) in (2, 4, 7, 8) or "." in raw:
            self._sync_combos_from_teryt(raw)
        self.teryt_changed.emit(raw)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_text_silent(self, value: str):
        """Ustawia txt_teryt bez wyzwalania _on_text_changed."""
        self._blocking = True
        self.txt_teryt.setText(value)
        self._blocking = False

    def _validate(self, raw: str):
        if not raw:
            self.lbl_info.setText("")
            return
        valid = (
            raw in self._wojewodztwa
            or raw in self._powiaty
            or raw in self._gminy
            or len(raw) in (2, 4, 6, 7, 8)
            or "." in raw
            or "*" in raw
        )
        self.lbl_info.setText("" if valid else "Kod TERYT może być nieprawidłowy")

    def _sync_combos_from_teryt(self, teryt: str):
        """Synchronizuje kaskadę comboboxes z podanym kodem TERYT."""
        self._blocking = True
        try:
            if len(teryt) >= 2:
                idx = self.cmb_woj.findData(teryt[:2])
                if idx >= 0:
                    self.cmb_woj.setCurrentIndex(idx)
                    self._reload_pow(teryt[:2])
            if len(teryt) >= 4:
                idx = self.cmb_pow.findData(teryt[:4])
                if idx >= 0:
                    self.cmb_pow.setCurrentIndex(idx)
                    self._reload_gmina(teryt[:4])
            if len(teryt) >= 6:
                t = teryt[:7] if len(teryt) >= 7 else teryt
                idx = self.cmb_gmina.findData(t)
                if idx >= 0:
                    self.cmb_gmina.setCurrentIndex(idx)
        finally:
            self._blocking = False

    def _reload_pow(self, woj_id: str):
        self.cmb_pow.clear()
        self.cmb_pow.addItem("- Wybierz -", None)
        self.cmb_pow.setEnabled(True)
        for teryt, info in sorted(
            self._powiaty.items(), key=lambda x: x[1].get("nazwa", "")
        ):
            if info.get("parent") == woj_id:
                self.cmb_pow.addItem(f"{info['nazwa']} ({teryt})", teryt)

    def _reload_gmina(self, pow_id: str):
        self.cmb_gmina.clear()
        self.cmb_gmina.addItem("- Wybierz -", None)
        self.cmb_gmina.setEnabled(True)
        for teryt, info in sorted(
            self._gminy.items(), key=lambda x: x[1].get("nazwa", "")
        ):
            if info.get("parent") == pow_id:
                self.cmb_gmina.addItem(f"{info['nazwa']} ({teryt})", teryt)