# -*- coding: utf-8 -*-

from qgis.PyQt import QtWidgets, uic
from qgis.PyQt.QtCore import pyqtSignal, Qt, QVariant, QSettings
from qgis.PyQt.QtWidgets import QMessageBox, QCompleter, QListWidgetItem, QFileDialog
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsFeature, QgsGeometry,
    QgsField, QgsFields, QgsWkbTypes, QgsCoordinateTransform,
    QgsCoordinateReferenceSystem, QgsRectangle, QgsApplication,
    QgsMessageLog, Qgis, QgsRasterLayer, QgsPointCloudLayer
)
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand, QgsMapLayerComboBox

import os
import json
import requests
import zipfile
import re
import tempfile

from .base_wfs_client import EGIBClientBudynki, RCNClient, WFSClient, PRGClientAdresy, PRGClientAdmin
from .geoparquet_download_task import GeoparquetDownloadTask
from .download_task import CheckHitsTask, DownloadTask
from .prg_client import PRGClient

from .wfs_index_dialog import WfsIndexResultsDialog
from .wfs_index_client import WFSIndexClient
from .skorowidz_services import SKOROWIDZ_SERVICES, SKOROWIDZ_BY_LABEL

from .eziudp_download_dialog import EziudpWidget


FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'pobierz_dane_GUGiK_dockwidget_base.ui'))

class RectangleMapTool(QgsMapToolEmitPoint):
    def __init__(self, canvas, callback):
        super().__init__(canvas)
        self.canvas = canvas
        self.callback = callback
        self.rubberBand = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
        # self.rubberBand.setColor(Qt.red)
        self.rubberBand.setFillColor(QColor(255, 255, 0, 80))   # półprzezroczyste wypełnienie
        self.rubberBand.setColor(QColor(255, 200, 0, 200))      # obwódka
        self.rubberBand.setWidth(2)
        self.startPoint = None
        self.isEmittingPoint = False
       

    def canvasPressEvent(self, e):
        self.startPoint = self.toMapCoordinates(e.pos())
        self.rubberBand.reset(QgsWkbTypes.PolygonGeometry)
        self.isEmittingPoint = True

    def canvasMoveEvent(self, e):
        if not self.isEmittingPoint:
            return

        currentPoint = self.toMapCoordinates(e.pos())
        self.rubberBand.reset(QgsWkbTypes.PolygonGeometry)

        # Create normalized rectangle (min x,y to max x,y)
        xmin = min(self.startPoint.x(), currentPoint.x())
        ymin = min(self.startPoint.y(), currentPoint.y())
        xmax = max(self.startPoint.x(), currentPoint.x())
        ymax = max(self.startPoint.y(), currentPoint.y())

        rect = QgsRectangle(xmin, ymin, xmax, ymax)
        self.rubberBand.addGeometry(QgsGeometry.fromRect(rect), None)

    def canvasReleaseEvent(self, e):
        self.isEmittingPoint = False
        endPoint = self.toMapCoordinates(e.pos())

        # Create normalized rectangle (min x,y to max x,y)
        xmin = min(self.startPoint.x(), endPoint.x())
        ymin = min(self.startPoint.y(), endPoint.y())
        xmax = max(self.startPoint.x(), endPoint.x())
        ymax = max(self.startPoint.y(), endPoint.y())

        rect = QgsRectangle(xmin, ymin, xmax, ymax)
        self.callback(rect)
        self.rubberBand.reset(QgsWkbTypes.PolygonGeometry)


class PD_GUGiKDockWidget(QtWidgets.QDockWidget, FORM_CLASS):

    closingPlugin = pyqtSignal()

    def __init__(self, parent=None):
        """Constructor."""
        super(PD_GUGiKDockWidget, self).__init__(parent)
        self.setupUi(self)

        # Data Cache
        self.wojewodztwa = {}
        self.powiaty = {}
        self.gminy = {}
        self.obreby = {}

        # Init
        self.prg_client = PRGClient()
        self.load_data()
        self.connect_signals()
        self.local_filter_geom = None
        self.running_tasks = []

        # Add checkbox for precise spatial filtering to admin tab
        if hasattr(self, 'tab_admin'):
            self.chk_precise_spatial = QtWidgets.QCheckBox("Dokładne filtrowanie przestrzenne")
            self.chk_precise_spatial.setToolTip("Jeśli zaznaczone, używa dokładnej geometrii zamiast prostokąta otaczającego (wolniejsze, ale bardziej precyzyjne).")
            
            # Add button for PRG geometry download
            self.btn_download_prg_geom = QtWidgets.QPushButton("Pobierz granicę z PRG")
            self.btn_download_prg_geom.setToolTip("Pobierz tylko geometrię obszaru administracyjnego z PRG (nie pobiera działek).")
            self.btn_download_prg_geom.clicked.connect(self.run_prg_geometry_download)

            # Insert before download button
            layout = self.tab_admin.layout()
            # Assuming 'Pobierz' button is at the end or near the end.
            # chk_precise_spatial was inserted at count-2.
            # Let's insert this button there too (pushing download button further down?)
            # Or insert it before precise check?
            # Layout order usually: Inputs, Spacer, Buttons.
            # Let's add it before chk_precise_spatial or after.
            
            # Current structure seems to be VBox.
            # We want: [Precise Check], [Download PRG Boundary], [Download Parcels]
            
            layout.insertWidget(layout.count() - 2, self.chk_precise_spatial)
            layout.insertWidget(layout.count() - 1, self.btn_download_prg_geom)

            # self._patch_init_eziudp_button()


        # Add new tab for precise search (obreb + dzialka nr)
        if hasattr(self, 'tabWidget'):
            from qgis.PyQt.QtWidgets import QWidget, QVBoxLayout, QLabel, QLineEdit, QPushButton, QFormLayout

            self.btn_search_obreb_nr.clicked.connect(self.run_precise_search)

        # Add layer selection and download button to map tab
        if hasattr(self, 'tab_map'):
            from qgis.PyQt.QtWidgets import QFormLayout, QLabel
            map_layout = self.tab_map.layout()

            # Create form layout for layer selection
            form_layout = QFormLayout()

            self.lbl_layer = QLabel("Warstwa poligonowa:")
            self.cmb_layer = QgsMapLayerComboBox()

            form_layout.addRow(self.lbl_layer, self.cmb_layer)

            # Create download button
            self.btn_download_layer = QtWidgets.QPushButton("Pobierz przez warstwę poligonową")
            self.btn_download_layer.setToolTip("Pobierz działki dla wszystkich geometrii w wybranej warstwie.")

            # Insert widgets before spacer
            form_widget = QtWidgets.QWidget()
            form_widget.setLayout(form_layout)
            map_layout.insertWidget(1, form_widget)
            map_layout.insertWidget(2, self.btn_download_layer)

            # Connect signal
            self.btn_download_layer.clicked.connect(self.run_layer_download)

        self.map_tool = None

        # Wypełnij cmbObjType ze statycznych wpisów EGiB/RCN + dynamicznie z rejestru usług skorowidzowych
        self._populate_obj_type_combo()
        self._init_eziudp_tab()
        self._init_category_filter()

        # Dynamiczny combobox roku – widoczny tylko dla usług skorowidzowych z year_range
        # (cmb_orto_rok / cmb_nmt_rok z .ui są zastępowane jedną parą sterowaną z kodu)
        self._init_year_combos()

        from qgis.utils import iface
        self.iface = iface
        self.canvas = self.iface.mapCanvas()

        # Internal state
        self.download_stopped = False

        # Initialize Settings and Attributes
        self.init_settings_ui()
        self.setup_completer()

    def _patch_init_eziudp_button(self):
        """
        Fragment do wklejenia w __init__(), np. na końcu bloku tab_admin.
    
        Tworzy przycisk "Pobierz dane powiatowe (eziudp)" i wstawia go
        do layoutu zakładki administracyjnej.
        Jeśli wolisz zdefiniować go w Qt Designerze – po prostu nazwij go
        'btn_eziudp' i pomiń ten blok.
        """
        if hasattr(self, "tab_admin"):
            self.btn_eziudp = QtWidgets.QPushButton(
                "Pobierz dane powiatowe (EZiUDP) …"
            )
            self.btn_eziudp.setToolTip(
                "Otwiera okno pobierania danych z powiatowych usług WFS\n"
                "przez serwis integracja.gugik.gov.pl (BDSOG, EGIB, RCN)."
            )
            layout = self.tab_admin.layout()
            # Wstaw przed ostatnim widgetem (zwykle spacer lub btn_download_admin)
            layout.insertWidget(layout.count() - 1, self.btn_eziudp)
            # Podpięcie sygnału – connect_signals() uruchomił się wcześniej,
            # więc podpinamy tutaj bezpośrednio

    def _populate_obj_type_combo(self):
        """
        Wypełnia cmbObjType: najpierw stałe typy EGiB/RCN,
        potem wszystkie usługi skorowidzowe z rejestru.
        Dzięki temu dodanie nowej usługi do SKOROWIDZ_SERVICES
        automatycznie pojawia się w UI.
        """
        # Zachowaj istniejące wpisy EGiB/RCN jeśli combo już ma elementy
        # (setupUi mógł wypełnić je z .ui), w przeciwnym razie dodaj ręcznie
        if self.cmbObjType.count() == 0:
            for label in ["dzialki (EGIB)", "budynki (EGIB)",
                          "dzialki (RCN)", "budynki (RCN)", "lokale (RCN)", "adresy (PRG)"]:
                self.cmbObjType.addItem(label)

        # Usuń stare wpisy skorowidzowe (gdyby były w .ui) – zachowaj tylko EGiB/RCN
        egib_rcn_count = self.cmbObjType.count()

        # Dodaj wszystkie usługi z rejestru
        for svc in SKOROWIDZ_SERVICES:
            self.cmbObjType.addItem(svc.label)

        self.cmbObjType.addItem("Usługi powiatowe (EZiUDP)")

    def _init_year_combos(self):
        """
        Tworzy dynamiczną parę comboboxów roku (od/do) sterowaną przez rejestr.
        Zastępuje hardkodowane cmb_orto_rok / cmb_nmt_rok jeśli istnieją w .ui,
        lub tworzy nowe widgety programowo gdy ich nie ma.
        """


        self.cmb_skorowidz_rok.setVisible(False)
        self.cmb_skorowidz_rok_do.setVisible(False)
        self.lbl_skorowidz_date.setVisible(False)

        # Podpięcie sygnału rok_do – robimy to tu, po tym jak widget na pewno istnieje,
        # żeby uniknąć TypeError przy disconnect w connect_signals.
        # self.cmb_skorowidz_rok_do.currentIndexChanged.connect(self.update_ui_from_type)

    def get_gmina_name(self, teryt):
        """Pobierz nazwę gminy z kodu TERYT."""
        if teryt[:8] in self.gminy:
            return self.gminy[teryt[:8]]['nazwa']
        elif teryt[:6] in self.gminy:
            return self.gminy[teryt[:6]]['nazwa']
        return None

    def get_powiat_name(self, teryt):
        """Pobierz nazwę powiatu z kodu TERYT."""
        if teryt[:4] in self.powiaty:
            return self.powiaty[teryt[:4]]['nazwa']
        return None

    def init_settings_ui(self):
        """Initialize settings tab and load attributes list."""
        self.attributes_all = [
            'id_dzialki', 'numer_dzialki', 'numer_obrebu', 'numer_jednostki', 'nazwa_obrebu', 'nazwa_gminy',
            'pole_powierzchni', 'grupa_rejestrowa', 'data', 'klasouzytki_egib'
        ]
        for attr in self.attributes_all:
            item = QListWidgetItem(attr)
            item.setCheckState(Qt.CheckState.Checked)
            self.list_attributes.addItem(item)
            
    def setup_completer(self):
        """Setup QCompleter for TERYT search by name."""
        self.name_to_teryt = {}
        # Collect all names from loaded data
        for t, data in self.wojewodztwa.items():
            # self.name_to_teryt[data['nazwa']] = t
            self.name_to_teryt[f"{data['nazwa']} (województwo {t})"] = t
        for t, data in self.powiaty.items():
            # self.name_to_teryt[f"{data['nazwa']} (powiat)"] = t
            self.name_to_teryt[f"{data['nazwa']} (powiat {t})"] = t
        for t, data in self.gminy.items():
            # self.name_to_teryt[f"{data['nazwa']} (gmina)"] = t
            self.name_to_teryt[f"{data['nazwa']} (gmina {t})"] = t
        if hasattr(self, 'obreby_teryt_to_info'):
            for t, data in self.obreby_teryt_to_info.items():
                self.name_to_teryt[f"{data['nazwa']} (obręb {t})"] = t

        self.completer = QCompleter(self.name_to_teryt.keys(), self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.txt_teryt_manual.setCompleter(self.completer)
        
        self.completer.activated.connect(self.on_completer_activated)

    def on_completer_activated(self, text):
        
        # Try to find TERYT in the selected text (e.g. from "Brzuchania (obręb 120805_5.0002)")
        # match = re.search(r'\(obręb (.*?)\)', text)
        match = re.search(r'^.+\.([\d:4]+)', text)
        if match:
            teryt = match.group()
        elif text in self.name_to_teryt:
            teryt = self.name_to_teryt[text]
        else:
            teryt = text

        
            
        self.txt_teryt_manual.setText(teryt)
        self.sync_combos_from_teryt(teryt)

        self.lbl_teryt_info.setText(teryt)

    def sync_combos_from_teryt(self, teryt):
        """Update ComboBoxes based on given TERYT code."""
        if len(teryt) >= 2:
            idx = self.cmb_woj.findData(teryt[:2])
            if idx >= 0: self.cmb_woj.setCurrentIndex(idx)
        if len(teryt) >= 4:
            idx = self.cmb_pow.findData(teryt[:4])
            if idx >= 0: self.cmb_pow.setCurrentIndex(idx)
        if len(teryt) >= 6:
            # Try to find exactly or prefix
            t6 = teryt[:7] if len(teryt) >= 7 else teryt
            idx = self.cmb_gmina.findData(t6)
            if idx < 0 and len(teryt) >= 8:
                idx = self.cmb_gmina.findData(teryt[:8])
            if idx >= 0: self.cmb_gmina.setCurrentIndex(idx)
        if "." in teryt:
            idx = self.cmb_obreb.findData(teryt)
            if idx >= 0: self.cmb_obreb.setCurrentIndex(idx)

    def validate_teryt(self):
        text = self.txt_teryt_manual.text().strip()
        text = text.split(" ")[-1].replace(")","") if " " in text else text
        if not text:
            self.lbl_teryt_info.setText("")
            return

        is_valid = False
        if text in self.wojewodztwa or text in self.powiaty or text in self.gminy:
            is_valid = True
        elif hasattr(self, 'obreby_teryt_to_info') and text in self.obreby_teryt_to_info:
            is_valid = True
        elif '*' in text or '?' in text:
            is_valid = True
        elif len(text) in [2, 4, 7, 8]:
            is_valid = True
        elif "." in text:
            is_valid = True

        if not is_valid:
            self.lbl_teryt_info.setText("Teryt może być niewłaściwy!")
        else:
            self.lbl_teryt_info.setText("")
            # Avoid recursion if just selected from completer
            if not self.completer.popup().isVisible():
                 # Don't sync on every keystroke to avoid perf issues, maybe only on certain length
                 if len(text) in [2, 4, 7, 8] or "." in text:
                     self.sync_combos_from_teryt(text)

    def load_data(self):
        """Load JSON/GeoJSON files into memory."""
        plugin_dir = os.path.dirname(__file__)
        data_dir = os.path.join(plugin_dir, 'data')

        from qgis.core import QgsMessageLog, Qgis

        def get_val(props, keys):
            # Case-insensitive lookup
            props_lower = {k.lower(): v for k, v in props.items()}
            for k in keys:
                if k.lower() in props_lower:
                    return props_lower[k.lower()]
            return None

        try:
            # Ładowanie województw
            path = os.path.join(data_dir, 'wojewodztwa.geojson')
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    count = 0
                    for feat in data.get('features', []):
                        props = feat.get('properties', {})
                        name = get_val(props, ['nazwa', 'name', 'voivodeship'])
                        teryt = get_val(props, ['teryt', 'id', 'kod'])
                        geom_json = feat.get('geometry')

                        if name and teryt:
                            teryt_str = str(teryt)
                            geom = None
                            if geom_json:
                                geom = QgsGeometry.fromJson(json.dumps(geom_json))

                            self.wojewodztwa[teryt_str] = {
                                'nazwa': name,
                                'geom': geom
                            }
                            count += 1

                    self.cmb_woj.addItem("- Wybierz -", None)
                    self.cmb_woj.addItem("Cała Polska (0)", "")
                    for teryt_str, w in sorted(self.wojewodztwa.items(), key=lambda x: x[1]['nazwa']):
                        self.cmb_woj.addItem(f"{w['nazwa']} ({teryt_str})", teryt_str)
                    QgsMessageLog.logMessage(f"Załadowano {count} województw.", "PD_GUGiK", Qgis.MessageLevel.Info)

            # Ładowanie powiatów
            path = os.path.join(data_dir, 'powiaty.geojson')
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    count = 0
                    for feat in data.get('features', []):
                        props = feat.get('properties', {})
                        name = get_val(props, ['nazwa', 'name', 'powiat'])
                        teryt = get_val(props, ['teryt', 'id', 'kod'])
                        geom_json = feat.get('geometry')

                        if name and teryt:
                            teryt_str = str(teryt)
                            geom = None
                            if geom_json:
                                geom = QgsGeometry.fromJson(json.dumps(geom_json))

                            # Parent is first 2 chars of TERYT (Województwo)
                            self.powiaty[teryt_str] = {
                                'nazwa': name,
                                'parent': teryt_str[:2],
                                'geom': geom
                            }
                            count += 1
                    QgsMessageLog.logMessage(f"Załadowano {count} powiatów.", "PD_GUGiK", Qgis.MessageLevel.Info)

            # Ładowanie gmin
            path = os.path.join(data_dir, 'gminy.geojson')
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    count = 0
                    for feat in data.get('features', []):
                        props = feat.get('properties', {})
                        name = get_val(props, ['nazwa', 'name', 'gmina'])
                        teryt = get_val(props, ['teryt', 'id', 'kod'])
                        geom_json = feat.get('geometry')

                        if name and teryt:
                            teryt_str = str(teryt)
                            geom = None
                            if geom_json:
                                geom = QgsGeometry.fromJson(json.dumps(geom_json))

                            # Parent is first 4 chars of TERYT (Powiat)
                            self.gminy[teryt_str] = {
                                'nazwa': name,
                                'parent': teryt_str[:4],
                                'geom': geom
                            }
                            count += 1
                    QgsMessageLog.logMessage(f"Załadowano {count} gmin.", "PD_GUGiK", Qgis.MessageLevel.Info)
            
            # Ładowanie obrębów - zapisz jako słownik według kodu TERYT (pełnego)
            obreb_path = os.path.join(data_dir, 'obreby.geojson')
            if os.path.exists(obreb_path):
                with open(obreb_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.obreby_by_gmina = {}
                    self.obreby_by_name = {}
                    self.obreby_teryt_to_info = {}
                    count = 0
                    for feat in data.get('features', []):
                        props = feat.get('properties', {})
                        teryt = get_val(props, ['TERYT', 'teryt', 'id', 'kod'])
                        nazwa = get_val(props, ['Nazwa', 'nazwa', 'name'])

                        if teryt and nazwa:
                            teryt_str = str(teryt)
                            # Format TERYT obrębu: WWPPGG_R.OOOO (np. 141201_1.0001)
                            # Kod gminy to część przed kropką (WWPPGG_R)
                            if '_' in teryt_str:
                                gmina_code = teryt_str.split('_')[0]
                               
                            else:
                                # Fallback: take first 8 chars if matches pattern
                                gmina_code = teryt_str[:6]

                            if gmina_code not in self.obreby_by_gmina:
                                self.obreby_by_gmina[gmina_code] = []
                            self.obreby_by_gmina[gmina_code].append((nazwa, teryt_str))

                            # Mapowanie nazwy obrębu do listy TERYT
                            if nazwa not in self.obreby_by_name:
                                self.obreby_by_name[nazwa] = []
                            self.obreby_by_name[nazwa].append(teryt_str)

                            # Mapowanie TERYT obrębu do info
                            self.obreby_teryt_to_info[teryt_str] = {
                                'nazwa': nazwa,
                                'gmina_code': gmina_code,
                                'powiat_code': teryt_str[:4]
                            }
                            count += 1
                    QgsMessageLog.logMessage(f"Załadowano {count} obrębów.", "PD_GUGiK", Qgis.MessageLevel.Info)
            else:
                self.obreby_by_gmina = {}
                self.obreby_by_name = {}
                self.obreby_teryt_to_info = {}

        except Exception as e:
            msg = f"Błąd ładowania danych: {e}"
            QgsMessageLog.logMessage(msg, "PD_GUGiK", Qgis.MessageLevel.Critical)
            self.show_error(msg)

    def connect_signals(self):
        self.cmb_woj.currentIndexChanged.connect(self.update_teryt_from_woj)
        self.cmb_pow.currentIndexChanged.connect(self.update_teryt_from_pow)
        self.cmb_gmina.currentIndexChanged.connect(self.update_teryt_from_gmina)
        self.cmb_obreb.currentIndexChanged.connect(self.update_teryt_from_obreb)
        self.cmbObjType.currentIndexChanged.connect(self.update_ui_from_type)
        # Sygnał cmb_skorowidz_rok_do podpinany w _init_year_combos po utworzeniu widgetu
        self.cmbCategory.currentIndexChanged.connect(self._apply_category_filter)

        self.btn_download_admin.clicked.connect(self.run_admin_download)
        self.btn_download_ids.clicked.connect(self.run_id_download)

        self.btn_select_rect.clicked.connect(self.toggle_map_tool)
        self.btn_download_extent.clicked.connect(self.run_extent_download)

        self.btn_cancel.clicked.connect(self.cancel_download)
        self.txt_teryt_manual.textChanged.connect(self.validate_teryt)


    def _current_skorowidz_service(self):
        """
        Zwraca SkorowidzService dla aktualnie wybranego typu obiektu,
        lub None jeśli to nie jest usługa skorowidzowa.
        """
        return SKOROWIDZ_BY_LABEL.get(self.cmbObjType.currentText())

    def _update_year_combo(self, svc):
        """
        Wypełnia cmb_skorowidz_rok / cmb_skorowidz_rok_do zakresem lat
        zdefiniowanym w SkorowidzService. Ukrywa combo gdy year_range=None.
        """
        has_years = svc is not None and svc.year_range is not None
        self.cmb_skorowidz_rok.setVisible(has_years)
        self.cmb_skorowidz_rok_do.setVisible(has_years)
        self.lbl_skorowidz_date.setVisible(has_years)

        if not has_years:
            return

        max_rok, min_rok = svc.year_range
        lata = ["Wszystkie"] + [str(r) for r in range(max_rok, min_rok - 1, -1)]
        lata_do = ["- Brak -"] + [str(r) for r in range(max_rok, min_rok - 1, -1)]

        self.cmb_skorowidz_rok.blockSignals(True)
        self.cmb_skorowidz_rok_do.blockSignals(True)
        self.cmb_skorowidz_rok.clear()
        self.cmb_skorowidz_rok_do.clear()
        self.cmb_skorowidz_rok.addItems(lata)
        self.cmb_skorowidz_rok_do.addItems(lata_do)
        self.cmb_skorowidz_rok.blockSignals(False)
        self.cmb_skorowidz_rok_do.blockSignals(False)

    def _get_years_from_combos(self, cmb_from, cmb_to) -> list:
        """
        Zwraca listę lat (jako stringi) wybranych w dwóch comboboxach rok_od/rok_do.
        """
        rok_od = cmb_from.currentText()
        rok_do = cmb_to.currentText()

        if rok_od == "Wszystkie":
            return [cmb_from.itemText(i) for i in range(1, cmb_from.count())]

        rok_od_int = int(rok_od)
        if rok_do and rok_do != "- Brak -":
            rok_do_int = int(rok_do)
            min_rok = min(rok_od_int, rok_do_int)
            max_rok = max(rok_od_int, rok_do_int)
            return [str(r) for r in range(max_rok, min_rok - 1, -1)]
        return [rok_od]

    def _run_skorowidz(self, geom, svc=None):
        """
        Uruchamia wyszukiwanie w skorowidzu dla podanej geometrii.
        Pobiera konfigurację usługi z rejestru SKOROWIDZ_SERVICES.

        :param geom: QgsGeometry obszaru wyszukiwania
        :param svc:  SkorowidzService – jeśli None, pobierany z aktualnego cmbObjType
        """
        if svc is None:
            svc = self._current_skorowidz_service()
        if svc is None:
            return

        if svc.year_range is None:
            # Usługa bez podziału rocznikowego – pobieramy całą warstwę
            self.search_and_show_wfs_index(geom, svc.url, svc.layer_name)
        else:
            lata = self._get_years_from_combos(
                self.cmb_skorowidz_rok, self.cmb_skorowidz_rok_do
            )
            if not lata:
                self.show_error("Brak poprawnych lat do wyszukania.")
                return
            if len(lata) == 1:
                self.search_and_show_wfs_index(
                    geom, svc.url, f"{svc.layer_name}{lata[0]}{svc.layer_suffix}"
                )
            else:
                self.search_and_show_multiple_wfs_indices(
                    geom, svc.url, lata, svc.layer_name, svc.layer_suffix
                )

    def update_teryt_from_woj(self):
        woj_id = self.cmb_woj.currentData()
        self.cmb_pow.clear()
        self.cmb_pow.setEnabled(True)
        self.cmb_pow.addItem("- Wybierz -", None)


        if woj_id:
            self.txt_teryt_manual.setText(str(woj_id))

            self.cmb_pow.addItem("Brak", None)
            for teryt, p in sorted(self.powiaty.items(), key=lambda x: x[1]['nazwa']):
                if p['parent'] == woj_id:
                    self.cmb_pow.addItem(f"{p['nazwa']} ({teryt})", teryt)
        elif woj_id == "":
            self.txt_teryt_manual.setText("0")
        else:
            self.txt_teryt_manual.clear()

    def update_teryt_from_pow(self):
        pow_id = self.cmb_pow.currentData()
        self.cmb_gmina.clear()
        if not pow_id:
            self.cmb_gmina.setEnabled(False)

            if self.cmb_pow.currentIndex() == 0:
                woj_id = self.cmb_woj.currentData()
                if woj_id:
                    self.txt_teryt_manual.setText(str(woj_id))
                else:
                    self.txt_teryt_manual.clear()
            elif self.cmb_pow.currentIndex() == 1:
                woj_id = self.cmb_woj.currentData()
                if woj_id:
                    self.txt_teryt_manual.setText(str(woj_id))
                else:
                    self.txt_teryt_manual.clear()
            return

        self.cmb_gmina.setEnabled(True)
        self.cmb_gmina.addItem("- Wybierz -", None)
        self.txt_teryt_manual.setText(str(pow_id))

        self.cmb_gmina.addItem("Brak", None)
        for teryt, g in sorted(self.gminy.items(), key=lambda x: x[1]['nazwa']):
            if g['parent'] == pow_id:
                self.cmb_gmina.addItem(f"{g['nazwa']} ({teryt})", teryt)

    def update_teryt_from_gmina(self):
        gmina_id = self.cmb_gmina.currentData()
        self.cmb_obreb.clear()
        if not gmina_id:
            self.cmb_obreb.setEnabled(False)

            if self.cmb_gmina.currentIndex() == 0:
                pow_id = self.cmb_pow.currentData()
                if pow_id:
                    self.txt_teryt_manual.setText(str(pow_id))
                else:
                    woj_id = self.cmb_woj.currentData()
                    if woj_id:
                        self.txt_teryt_manual.setText(str(woj_id))
                    else:
                        self.txt_teryt_manual.clear()
            elif self.cmb_gmina.currentIndex() == 1:
                pow_id = self.cmb_pow.currentData()
                if pow_id:
                    self.txt_teryt_manual.setText(str(pow_id))
                else:
                    woj_id = self.cmb_woj.currentData()
                    if woj_id:
                        self.txt_teryt_manual.setText(str(woj_id))
                    else:
                        self.txt_teryt_manual.clear()
            return

        self.cmb_obreb.setEnabled(True)
        self.cmb_obreb.addItem("- Wybierz -", None)
        self.txt_teryt_manual.setText(str(gmina_id))

        self.cmb_obreb.addItem("Brak", None)
        if str(gmina_id[:6]) in self.obreby_by_gmina:
            for nazwa, teryt in sorted(self.obreby_by_gmina[str(gmina_id[:6])], key=lambda x: x[0]):
                self.cmb_obreb.addItem(f"{nazwa} ({teryt})", teryt)

    def update_teryt_from_obreb(self):
        obreb_id = self.cmb_obreb.currentData()

        if obreb_id:
            self.txt_teryt_manual.setText(str(obreb_id))
        else:
            if self.cmb_obreb.currentIndex() == 0:
                gmina_id = self.cmb_gmina.currentData()
                if gmina_id:
                    self.txt_teryt_manual.setText(str(gmina_id))
                else:
                    pow_id = self.cmb_pow.currentData()
                    if pow_id:
                        self.txt_teryt_manual.setText(str(pow_id))
                    else:
                        woj_id = self.cmb_woj.currentData()
                        if woj_id:
                            self.txt_teryt_manual.setText(str(woj_id))
                        else:
                            self.txt_teryt_manual.clear()
            elif self.cmb_obreb.currentIndex() == 1:
                gmina_id = self.cmb_gmina.currentData()
                if gmina_id:
                    self.txt_teryt_manual.setText(str(gmina_id))
                else:
                    pow_id = self.cmb_pow.currentData()
                    if pow_id:
                        self.txt_teryt_manual.setText(str(pow_id))
                    else:
                        woj_id = self.cmb_woj.currentData()
                        if woj_id:
                            self.txt_teryt_manual.setText(str(woj_id))
                        else:
                            self.txt_teryt_manual.clear()
    def toggle_orto_rok_do(self):
        """Nieużywane – pozostawione dla kompatybilności wstecznej."""
        pass

    def update_ui_from_type(self):
        wybrany_tekst = self.cmbObjType.currentText()

        # Zakładki Identyfikator i Obręb+nr tylko dla działek EGIB
        for tab, title in [(self.tab_id, "Identyfikator / Plik"),
                           (self.tab_precise, "Obręb + nr działki")]:
            idx = self.tabWidget.indexOf(tab)
            if wybrany_tekst == "dzialki (EGIB)":
                if idx == -1:
                    self.tabWidget.insertTab(1, tab, title)
            else:
                if idx != -1:
                    self.tabWidget.removeTab(idx)

        # Combobox roku – aktualizuj na podstawie rejestru
        svc = self._current_skorowidz_service()
        self._update_year_combo(svc)
        self._update_tabs_for_type()

    def toggle_map_tool(self):
        if self.btn_select_rect.isChecked():
            self.map_tool = RectangleMapTool(self.canvas, self.on_rect_selected)
            self.canvas.setMapTool(self.map_tool)
        else:
            self.canvas.unsetMapTool(self.map_tool)
            self.map_tool = None

    def on_rect_selected(self, rect):
        self.btn_select_rect.setChecked(False)
        self.canvas.unsetMapTool(self.map_tool)
        self.run_spatial_download(rect, "Prostokąt", log_extent=False)

    def get_geometry_from_data(self, level, teryt_id):
        """
        Get geometry from loaded reference data.

        :param level: 'wojewodztwo', 'powiat', or 'gmina'
        :param teryt_id: TERYT identifier
        :returns: QgsGeometry or None
        """
        t_id = teryt_id
        t_id_match= re.search(r"'([\d_\.]+)'", t_id)
        teryt_id = t_id_match.group()
        
        if level == 'wojewodztwo' and teryt_id in self.wojewodztwa:
            if isinstance(self.wojewodztwa[teryt_id], dict):
                return self.wojewodztwa[teryt_id].get('geom')
        elif level == 'powiat' and teryt_id in self.powiaty:
            if isinstance(self.powiaty[teryt_id], dict):
                return self.powiaty[teryt_id].get('geom')
        elif level == 'gmina' and teryt_id in self.gminy:
            if isinstance(self.gminy[teryt_id], dict):
                return self.gminy[teryt_id].get('geom')

        QgsMessageLog.logMessage(
            f"[UI] Brak geometrii dla {level}={teryt_id}",
            "PD_GUGiK", Qgis.MessageLevel.Warning
        )
        return None

    def run_prg_geometry_download(self):
        """Pobierz i wyświetl geometrię z PRG (bez pobierania działek)."""
        manual_text = self.txt_teryt_manual.text().strip()
        if not manual_text:
            self.show_error("Wpisz kod TERYT do wyszukania.")
            return

        manual_text = manual_text.split(" ")[-1].replace(")", "") if "(" in manual_text else manual_text

        QgsMessageLog.logMessage(
            f"[UI] Pobieranie geometrii PRG dla TERYT: {manual_text}",
            "PD_GUGiK", Qgis.MessageLevel.Info
        )

        try:
            geom = self.prg_client.get_boundary_geometry(manual_text)
        except Exception as e:
            self.show_error(f"Błąd podczas pobierania geometrii z PRG: {e}")
            return

        if geom is None or geom.isEmpty():
            msg = f"Nie udało się pobrać geometrii dla TERYT: {manual_text}. Sprawdź poprawność kodu."
            QgsMessageLog.logMessage(f"[UI] {msg}", "PD_GUGiK", Qgis.MessageLevel.Warning)
            self.show_error(msg)
            return

        # Create memory layer for the boundary
        vl = QgsVectorLayer("Polygon?crs=epsg:2180", f"Granica PRG - {manual_text}", "memory")
        pr = vl.dataProvider()
        
        # Add TERYT attribute
        pr.addAttributes([QgsField("TERYT", QVariant.String)])
        vl.updateFields()

        feat = QgsFeature()
        feat.setGeometry(geom)
        feat.setAttributes([manual_text])
        
        pr.addFeatures([feat])
        vl.updateExtents()
        

        if self.chk_load_style.isChecked():
            style_path = os.path.join(os.path.dirname(__file__), 'data', f'prg.qml')
            if os.path.exists(style_path):
                vl.loadNamedStyle(style_path)

        QgsProject.instance().addMapLayer(vl)
        
        # Zoom to extent
        if self.canvas:
            self.canvas.setExtent(vl.extent())
            self.canvas.refresh()

        QgsMessageLog.logMessage(f"[UI] Dodano warstwę z granicą PRG dla {manual_text}", "PD_GUGiK", Qgis.MessageLevel.Info)


    def run_admin_download(self):

        obj_type = self.cmbObjType.currentText()

        manual_text = self.txt_teryt_manual.text().strip()
        if not manual_text:
            self.show_error("Wpisz kod TERYT do wyszukania.")
            return
        manual_text = manual_text.split(" ")[-1].replace(")", "") if "(" in manual_text else manual_text

        QgsMessageLog.logMessage(
            f"[UI] Pobieranie {obj_type} dla TERYT: {manual_text}",
            "PD_GUGiK", Qgis.MessageLevel.Info
        )

        # ── Usługi skorowidzowe ──────────────────────────────────────────
        # Dla skorowidzów: pobieramy geometrię PRG i od razu przekazujemy
        # do _run_skorowidz. Nie budujemy klientów EGIB/RCN, nie pobieramy GeoParquet.
        if self._current_skorowidz_service() is not None:
            try:
                geom = self.prg_client.get_boundary_geometry(manual_text)
            except Exception as e:
                self.show_error(f"Błąd podczas pobierania geometrii z PRG: {e}")
                return
            if geom is None or geom.isEmpty():
                self.show_error(f"Nie udało się pobrać geometrii dla TERYT: {manual_text}.")
                return
            self._run_skorowidz(geom)
            return

        # ── Dane wektorowe EGIB / RCN ────────────────────────────────────
        if obj_type == "dzialki (EGIB)":
            client = WFSClient()
            attr_filter_value = "id_dzialki"
        elif obj_type == "budynki (EGIB)":
            client = EGIBClientBudynki()
            attr_filter_value = "id_budynku"
        elif "(RCN)" in obj_type:
            obj_layer = obj_type.split(" ")[0]
            client = RCNClient(obj_layer=obj_layer)
            if obj_layer == "dzialki":
                attr_filter_value = "dzi_id_dzialki"
            elif obj_layer == "budynki":
                attr_filter_value = "bud_id_budynku"
            else:
                attr_filter_value = "lok_id_lokalu"
        elif "adresy (PRG)" in obj_type or "ulice (PRG)" in obj_type or "place (PRG)" in obj_type :
            obj_layer = obj_type.split(" ")[0]
            client = PRGClientAdresy(obj_layer=obj_layer)
            attr_filter_value = "teryt"
        elif "(PRG_" in obj_type:
            obj_layer = obj_type.split(" ")[0]
            client = PRGClientAdmin(obj_layer=obj_layer)
            # attr_filter_value = "teryt"
            attr_filter_value = "IIP_IDENTY"
        elif "(PRNG)" in obj_type:
            obj_layer = obj_type.split(" ")[0]
            client = PRGClientAdresy(obj_layer=obj_layer)
            attr_filter_value = "IDIIP"
        else:
            return
            client = WFSClient()
            attr_filter_value = "id_dzialki"

        if "(EGIB)" in obj_type or "(RCN)" in obj_type:
            # Pobierz geometrię jednostki administracyjnej z PRG
            if len(manual_text) < 4:
                try:
                    self.run_geoparquet_download(manual_text, obj_type)
                except Exception as e:
                    self.show_error(f"Błąd podczas pobierania {obj_type}: {e}")
                return

            # Powiaty (4 cyfry) → zapytaj o metodę pobierania
            if len(manual_text) == 4:
                msg_box = QMessageBox(self.iface.mainWindow())
                msg_box.setWindowTitle("Wybór metody pobierania")
                msg_box.setText(f"Wybrano powiat (TERYT: {manual_text}).\nKtórą metodą chcesz pobrać dane?")
                parquet_btn = msg_box.addButton("GeoParquet (zapis pliku na dysku)", QMessageBox.ButtonRole.ActionRole)
                wfs_btn = msg_box.addButton("WFS (warstwa tymczasowa)", QMessageBox.ButtonRole.ActionRole)
                cancel_btn = msg_box.addButton("Anuluj", QMessageBox.ButtonRole.RejectRole)
                msg_box.exec()
                if msg_box.clickedButton() == parquet_btn:
                    self.run_geoparquet_download(manual_text, obj_type)
                    return
                elif msg_box.clickedButton() == cancel_btn:
                    return

        try:
            geom = self.prg_client.get_boundary_geometry(manual_text)
        except Exception as e:
            self.show_error(f"Błąd podczas pobierania geometrii z PRG: {e}")
            return

        if geom is None or geom.isEmpty():
            msg = f"Nie udało się pobrać geometrii dla TERYT: {manual_text}. Sprawdź poprawność kodu."
            QgsMessageLog.logMessage(f"[UI] {msg}", "PD_GUGiK", Qgis.MessageLevel.Warning)
            self.show_error(msg)
            return

        use_precise = hasattr(self, 'chk_precise_spatial') and self.chk_precise_spatial.isChecked()
        filter_xml = client.build_spatial_filter(geom.asWkt(), use_bbox=not use_precise)
        if not filter_xml:
            self.show_error("Błąd budowania filtra przestrzennego.")
            return

        filter_type = "dokładnym (polygon)" if use_precise else "przybliżonym (BBOX)"
        QgsMessageLog.logMessage(
            f"[UI] Pobieranie z filtrem {filter_type} dla TERYT: {manual_text}",
            "PD_GUGiK", Qgis.MessageLevel.Info
        )

        local_filter_geom = False
        if "(PRG" in obj_type or "(PRNG)" in obj_type:
            attr_filter = client.build_attribute_filter(attr_filter_value, manual_text[:6], like=True)
            filter_xml = client.build_spatial_filter(geom.asWkt(), use_bbox=True)
            local_filter_geom = True
            self.local_filter_geom = geom
            combined_filter = filter_xml
        else:
            attr_filter = client.build_attribute_filter(attr_filter_value, manual_text, like=True)
            combined_filter = client.combine_filters([filter_xml, attr_filter])

        attributes = [self.list_attributes.item(i).text() for i in range(self.list_attributes.count())
                      if self.list_attributes.item(i).checkState() == Qt.CheckState.Checked]
        if len(attributes) == self.list_attributes.count():
            attributes = None

        
        self.start_download_direct(combined_filter, attributes=attributes, local_filter_geom=local_filter_geom)


    def run_geoparquet_download(self, teryt, obj_type):

        ext = ""

        baza = obj_type.split(" ")[-1]

        if len(teryt) < 4:
            file_type = "parquet"
            obj_type_clean = "_" + obj_type.split(" ")[0]
        else:
            file_type = "gpkg"
            ext = ".zip"
            obj_type_clean = ""


        if baza == "(EGIB)":
            url = f"https://opendata.geoportal.gov.pl/InneDane/latest_exports/eziudp_wfs/{file_type.upper()}/{teryt}{obj_type_clean}.{file_type}{ext}"
        elif baza == "(RCN)":
            url = f"https://opendata.geoportal.gov.pl/InneDane/latest_exports/rcn_transakcje_ceny/{file_type.upper()}/{teryt}_transakcje_ceny{obj_type_clean}.{file_type}{ext}"

        # 1. Wybór lokalizacji zapisu (musi być w wątku głównym)
        default_name = f"{teryt}{obj_type_clean}.{file_type}{ext}"
        path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(),
            f"Wybierz miejsce zapisu pliku {file_type}",
            default_name,
            f"{file_type} Files (*.{file_type}{ext})"
        )

        if not path:
            return

        # 2. Tworzenie zadania (Task)
        desc = f"Pobieranie {file_type}: {obj_type_clean} ({teryt})"
        task = GeoparquetDownloadTask(desc, url, path, obj_type_clean)

        # WAŻNE: Dodaj zadanie do listy, aby Python go nie usunął!
        self.running_tasks.append(task)

        # 3. Połączenie paska postępu z zadaniem
        # Zadanie będzie automatycznie raportować postęp przez sygnał progressChanged
        task.progressChanged.connect(lambda p: self.progressBar.setValue(int(p)))
        
        # Opcjonalnie: pokazanie/ukrycie paska
        self.progressBar.setVisible(True)
        task.begun.connect(lambda: self.progressBar.setVisible(True))
        task.taskCompleted.connect(lambda: self.progressBar.setVisible(False))
        task.taskTerminated.connect(lambda: self.progressBar.setVisible(False))
        task.downloadFinished.connect(self.on_download_finished_gp)
        task.taskCompleted.connect(lambda: self.running_tasks.remove(task))
        task.taskTerminated.connect(lambda: self.running_tasks.remove(task))

        # 4. Dodanie zadania do menedżera QGIS
        QgsApplication.taskManager().addTask(task)
        
        self.iface.messageBar().pushMessage("Zadanie uruchomione", "Pobieranie odbywa się w tle...", level=Qgis.MessageLevel.Info)

    def run_id_download(self):
        ids_text = self.txt_ids.toPlainText()
        if not ids_text.strip():
            self.show_error("Wpisz identyfikatory.")
            return

        all_ids = [line.strip() for line in ids_text.splitlines() if line.strip()]
        client = WFSClient()
        
        attributes = [self.list_attributes.item(i).text() for i in range(self.list_attributes.count()) 
                      if self.list_attributes.item(i).checkState() == Qt.CheckState.Checked]
        if len(attributes) == self.list_attributes.count(): attributes = None

        # Batching by 10
        batch_size = 10
        all_features = []
        
        from .download_task import DownloadTask
        
        QgsMessageLog.logMessage(f"[UI] Pobieranie {len(all_ids)} identyfikatorów w paczkach po {batch_size}", "PD_GUGiK", Qgis.MessageLevel.Info)
        
        # Create a single progress bar for the whole batch process
        self.toggle_ui(False)
        self.progressBar.setVisible(True)
        self.progressBar.setRange(0, len(all_ids))
        self.progressBar.setValue(0)
        self.btn_cancel.setVisible(True)
        self.download_stopped = False

        # Create or clear layer first
        vl = QgsProject.instance().mapLayersByName("Dzialki")
        if not vl:
            # We will create it on first batch success
            pass

        for i in range(0, len(all_ids), batch_size):
            if self.download_stopped: break
            
            batch = all_ids[i:i + batch_size]
            filter_xml = client.build_id_filter(batch)
            
            try:
                gml = client.download(filter_xml, attributes=attributes)
                temp_task = DownloadTask(filter_xml)
                features = temp_task._parse_gml(gml)
                
                if features:
                    all_features.extend(features)
                    self.create_layer(features)
                
                self.progressBar.setValue(i + len(batch))
                QgsApplication.processEvents()
                
            except Exception as e:
                QgsMessageLog.logMessage(f"[UI] Błąd w paczce {i//batch_size + 1}: {e}", "PD_GUGiK", Qgis.MessageLevel.Warning)

        self.reset_ui()
        if all_features:
            self.show_info(f"Pobrano łącznie {len(all_features)} działek.")
        elif not self.download_stopped:
            self.show_error("Nie pobrano żadnych działek.")

    def run_extent_download(self):
        extent = self.canvas.extent()
        self.run_spatial_download(extent, "Widok mapy", log_extent=False)

    def run_spatial_download(self, rect, source_name, log_extent=True):
        # rect is QgsRectangle in Canvas CRS
        # Check CRS
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        target_crs = QgsCoordinateReferenceSystem("EPSG:2180")

        if canvas_crs != target_crs:
            xform = QgsCoordinateTransform(canvas_crs, target_crs, QgsProject.instance())
            rect = xform.transformBoundingBox(rect)

        # Log extent only if requested
        if log_extent:
            QgsMessageLog.logMessage(
                f"[UI] Zakres {source_name}: Xmin={rect.xMinimum():.2f}, Ymin={rect.yMinimum():.2f}, Xmax={rect.xMaximum():.2f}, Ymax={rect.yMaximum():.2f}",
                "PD_GUGiK", Qgis.MessageLevel.Info
            )

        geom = QgsGeometry.fromRect(rect)

        # Przechwycenie dla usług skorowidzowych (automatycznie obsługuje wszystkie z rejestru)
        if self._current_skorowidz_service() is not None:
            self._run_skorowidz(geom)
            return

        client = WFSClient()
        filter_xml = client.build_spatial_filter(geom.asWkt())

        # Bezpośrednio rozpoczynamy pobieranie - hits jest niewiarygodny dla przestrzeni
        self.start_download_direct(filter_xml)

    def run_layer_download(self):
        """Download parcels using polygon layer as filter"""
        from qgis.PyQt.QtCore import pyqtSlot

        # Get selected layer from combo box
        if hasattr(self, 'cmb_layer'):
            layer = self.cmb_layer.currentLayer()
        else:
            # Fallback: get first polygon layer
            layers = QgsProject.instance().mapLayers().values()
            layer = None
            for l in layers:
                if l.type() == QgsVectorLayer.VectorLayer and l.geometryType() == QgsWkbTypes.PolygonGeometry:
                    layer = l
                    break

        if not layer:
            self.show_error("Nie znaleziono warstwy poligonowej.")
            return

        # Manual filter check
        if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
            self.show_error("Wybrana warstwa nie jest poligonowa.")
            return

        # Ask for confirmation if many features
        feature_count = layer.featureCount()
        if feature_count > 100:
            reply = QMessageBox.question(
                self, "Potwierdzenie",
                f"Warstwa zawiera {feature_count} obiektów. Może to zająć dużo czasu. Czy kontynuować?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                return

        QgsMessageLog.logMessage(
            f"[UI] Rozpoczynam pobieranie przez warstwę: {layer.name()} ({feature_count} obiektów)",
            "PD_GUGiK", Qgis.MessageLevel.Info
        )

        # Iterate through features and download parcels
        client = WFSClient()
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        target_crs = QgsCoordinateReferenceSystem("EPSG:2180")

        # Create transform if needed
        xform = None
        if canvas_crs != target_crs:
            xform = QgsCoordinateTransform(canvas_crs, target_crs, QgsProject.instance())

        current = 0
        skipped = 0
        local_filter_geom = None

        attributes = [self.list_attributes.item(i).text() for i in range(self.list_attributes.count())
                if self.list_attributes.item(i).checkState() == Qt.CheckState.Checked]
        if len(attributes) == self.list_attributes.count(): attributes = None

        # Przechwycenie dla usług skorowidzowych – rozpuszczamy wszystkie geometrie warstwy w jedną
        if self._current_skorowidz_service() is not None:
            dissolved_geom = None
            for feat in layer.getFeatures():
                g = feat.geometry()
                dissolved_geom = g if dissolved_geom is None else dissolved_geom.combine(g)
            if dissolved_geom:
                self._run_skorowidz(dissolved_geom)
            return

        for feat in layer.getFeatures():
            current += 1
            geom = feat.geometry()

            QgsMessageLog.logMessage(
                    f"[UI] Pobieranie {current}/{feature_count}",
                    "PD_GUGiK", Qgis.MessageLevel.Warning
                )

            # Skip invalid geometries
            if geom is None or geom.isEmpty():
                QgsMessageLog.logMessage(
                    f"[UI] Pominięto geometrię {current}/{feature_count}: pusta/nieprawidłowa",
                    "PD_GUGiK", Qgis.MessageLevel.Warning
                )
                skipped += 1
                continue

            # Transform to EPSG:2180 if needed
            if xform is not None:
                geom.transform(xform)

            n_coords = geom.constGet().nCoordinates()
            # Smart filtering: check vertex count
            if n_coords > 100:

                convex_hull = geom.convexHull()
                hull_coords = convex_hull.constGet().nCoordinates()

                if hull_coords <= 100:
                    # Otoczka wypukła mieści się w limitach - jest ciaśniejsza niż BBOX
                    QgsMessageLog.logMessage(
                        f"[UI] Geometria {current} posiada dużo wierzchołków ({n_coords}). "
                        f"Używam otoczki wypukłej ({hull_coords} wierzchołków) w WFS + dokładny filtr lokalny.", 
                        "PD_GUGiK", Qgis.MessageLevel.Info
                    )
                    # Używamy WKT otoczki wypukłej dla WFS
                    filter_xml = client.build_spatial_filter(convex_hull.asWkt(), use_bbox=False)
                    local_filter_geom = geom
                else:
                    # Nawet otoczka wypukła jest zbyt skomplikowana (rzadkie, ale możliwe)
                    QgsMessageLog.logMessage(
                        f"[UI] Geometria {current} i jej otoczka są zbyt skomplikowane "
                        f"({n_coords} i {hull_coords} wierzchołków). Używam prostokąta BBOX + dokładny filtr lokalny.", 
                        "PD_GUGiK", Qgis.MessageLevel.Info
                    )
                    filter_xml = client.build_spatial_filter(geom.asWkt(), use_bbox=True)
                    local_filter_geom = geom

            else:
                filter_xml = client.build_spatial_filter(geom.asWkt(), use_bbox=False)
                local_filter_geom = None



            # Download...

            self.local_filter_geom = local_filter_geom
            
            local_filter = True if self.local_filter_geom else False

            

            if feature_count < 2:
                self.start_download(filter_xml, total=100000, attributes=attributes, local_filter_geom=True)
                return

            start_index = 0
            count = 1000 # Page size
            total_expected = 100000
            features_data = [] # List of dicts: {'geom': wkt, 'attrs': {...}}
            exception = None
            stopped = False

            temp_task = DownloadTask(filter_xml, total_expected=total_expected, attributes=attributes)
            
            while not stopped:
                try:
                    
                    gml_content = client.download(filter_xml, start_index, count, attributes=attributes)
                    
                    new_features = temp_task._parse_gml(gml_content)
                    features_data.extend(new_features)
                    
                    if len(new_features) < count:
                        break
                    
                    start_index += count
                    
                except Exception as e:
                    # self.exception = e
                    # return False
                    ...
                    
            try:

                if local_filter_geom:
                    filtered = []
                    for f in features_data:
                        f_geom = QgsGeometry.fromWkt(f['geom'])
                        if f_geom.intersects(local_filter_geom):
                            filtered.append(f)
                    features_data = filtered
            
            except Exception as e:
                    QgsMessageLog.logMessage(f"[UI] Błąd geometrii: {e}", "PD_GUGiK", Qgis.MessageLevel.Warning)
            
            self.create_layer(features_data)

            QgsMessageLog.logMessage(
                f"[UI] Pobieranie {current}/{feature_count} z filtrem localnym: {local_filter}",
                "PD_GUGiK", Qgis.MessageLevel.Info
            )
        self.show_info(f"Pobrano obiekty.")



            # continue

    def start_check_hits(self, filter_xml):
        self.toggle_ui(False)
        self.progressBar.setVisible(True)
        self.progressBar.setRange(0, 0) # Indeterminate

        self.check_task = CheckHitsTask(filter_xml)
        self.check_task.hitsReady.connect(lambda hits: self.on_hits_checked(hits, filter_xml))
        QgsApplication.taskManager().addTask(self.check_task)

    def start_download_direct(self, filter_xml, attributes=None, local_filter_geom=False):
        """Bezpośrednio rozpoczynamy pobieranie bez sprawdzania hits."""
        self.download_stopped = False
        reply = QMessageBox.question(
            self, "Potwierdzenie",
            "Rozpoczynamy pobieranie danych.\n\nPobieranie może zająć dużo czasu w zależności od ilości danych.\n\nMożesz przerwać w dowolnym momencie.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.No:
            return

        self.start_download(filter_xml, 100000, attributes=attributes, local_filter_geom=local_filter_geom)

    def on_hits_checked(self, hits, filter_xml):
        if hits == -1:
            err_msg = str(self.check_task.exception) if self.check_task.exception else "Nieznany błąd"
            self.show_error(f"Błąd sprawdzania: {err_msg}")
            self.reset_ui()
            return

        if hits == 0:
            self.show_info("Brak wyników w zadanym obszarze.")
            self.reset_ui()
            return

        attributes = [self.list_attributes.item(i).text() for i in range(self.list_attributes.count()) 
                      if self.list_attributes.item(i).checkState() == Qt.CheckState.Checked]
        if len(attributes) == self.list_attributes.count(): attributes = None

        limit = 5000
        if hits > limit:
            reply = QMessageBox.question(
                self, "Potwierdzenie",
                f"Zapytanie może zwróć około {hits} obiektów.\n\nCzy kontynuować?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                self.reset_ui()
                return

        self.start_download(filter_xml, hits, attributes=attributes)

    def start_download(self, filter_xml, total, attributes=None, local_filter_geom=False):
        self.progressBar.setRange(0, 100)
        self.btn_cancel.setVisible(True)

        self.download_task = DownloadTask(filter_xml, total_expected=total, attributes=attributes, data_type=self.cmbObjType.currentText())

        
        if local_filter_geom:
            self.download_task.downloadFinished.connect(self.on_download_finished_not_load)

        else:
            
            self.download_task.downloadFinished.connect(self.on_download_finished)
        self.download_task.progressValue.connect(lambda val: self.progressBar.setValue(int(val)))
        QgsApplication.taskManager().addTask(self.download_task)

    def on_download_finished_gp(self, path):
        # self.show_info("!!!")
        # features_data is a list of dicts, or empty list on failure

        if not path: # Jeśli ścieżka jest pusta (błąd/anulowanie), przerwij
            self.reset_ui()
            return

        if path.lower().endswith(".zip"):
            QgsMessageLog.logMessage(f"{path}", "PD_GUGiK", Qgis.MessageLevel.Critical)
            try:
                QgsMessageLog.logMessage("Rozpakowywanie archiwum...", "PD_GUGiK", Qgis.MessageLevel.Info)
                
                folder = os.path.dirname(path)
                with zipfile.ZipFile(path, 'r') as zip_ref:
                    # Pobieramy listę plików i szukamy pierwszego .parquet
                    gpkg_files = [f for f in zip_ref.namelist() if f.lower().endswith('.gpkg')]
                    
                    if not gpkg_files:
                        QgsMessageLog.logMessage("BŁĄD: Wewnątrz ZIP nie znaleziono pliku GPKG!", "PD_GUGiK", Qgis.MessageLevel.Critical)
                        return

                    # Rozpakuj wszystko do folderu, gdzie jest ZIP
                    zip_ref.extractall(folder)
                    
                    # Ścieżka do wypakowanego pliku
                    final_gpkg_path = os.path.join(folder, gpkg_files[0])
                    QgsMessageLog.logMessage(f"Rozpakowano: {gpkg_files[0]}", "PD_GUGiK", Qgis.MessageLevel.Success)
                    
            except Exception as e:
                QgsMessageLog.logMessage(f"Błąd podczas rozpakowywania: {str(e)}", "PD_GUGiK", Qgis.MessageLevel.Critical)
                return
            
            os.remove(path)
            path = final_gpkg_path

        if path.lower().endswith(".gpkg"):
            # Tworzymy tymczasową warstwę, żeby „przeskanować” zawartość pliku
            temp_layer = QgsVectorLayer(path, "temp", "ogr")

            if not temp_layer.isValid():
                QgsMessageLog.logMessage(f"Nieprawidłowy plik GPKG: {path}", "PD_GUGiK", Qgis.MessageLevel.Critical)
                return
            
            sublayers = temp_layer.dataProvider().subLayers()

            if sublayers:
                QgsMessageLog.logMessage(f"Znaleziono warstwy: {len(sublayers)}", "PD_GUGiK", Qgis.MessageLevel.Info)
            
            if len(sublayers) > 1:
                QgsMessageLog.logMessage(f"Wykryto {len(sublayers)} warstw w GPKG. Wczytywanie wszystkich...", "PD_GUGiK", Qgis.MessageLevel.Info)
                
                for sub in sublayers:
                    QgsMessageLog.logMessage(f"Analizuję warstwę: {sub}", "PD_GUGiK", Qgis.MessageLevel.Info)
                    # 'sub' ma format "index:nazwa_warstwy:liczba_obiektów:typ_geometrii"
                    parts = sub.split('!!::!!')
                    layer_name = ""

                    if len(parts) >= 2 and parts[1].strip():
                        layer_name = parts[1] # Standard: "0:nazwa:..."
                    else:
                        # Fallback jeśli separator by się zmienił
                        layer_name = sub.split(':')[0].replace('!!', '')

                    if not layer_name:
                        continue

                    # Konstruujemy URI wskazujące na konkretną warstwę
                    uri = f"{path}|layername={layer_name}"
                    vlayer = QgsVectorLayer(uri, layer_name, "ogr")

                    trg_layer = layer_name
                    if "_" in trg_layer:
                        trg_layer = trg_layer.split("_")[-1].split(".")[0]

                    # if "transakcje" in layer_name: trg_layer += "_rcn"


                    if self.chk_load_style.isChecked():
                            style_path = os.path.join(os.path.dirname(__file__), 'data', f'{trg_layer.lower()}.qml')
                            if os.path.exists(style_path):
                                vlayer.loadNamedStyle(style_path)
                    
                    if vlayer.isValid():
                        QgsProject.instance().addMapLayer(vlayer)
                        QgsMessageLog.logMessage(f"Dodano warstwę: {layer_name}", "PD_GUGiK", Qgis.MessageLevel.Success)
                    else:
                        QgsMessageLog.logMessage(f"BŁĄD: Nie można wczytać warstwy {layer_name} z URI: {uri}", "PD_GUGiK", Qgis.MessageLevel.Critical)
            else:
                # Jeśli jest tylko jedna warstwa (lub subLayers zawiodło), wczytaj standardowo
                self.add_single_layer(path)
        else:
            # Dla plików .parquet (które zazwyczaj mają jedną warstwę)
            self.add_single_layer(path)

    def add_single_layer(self, path):
        """Pomocnicza funkcja do wczytywania pojedynczego pliku."""
        layer_name = os.path.basename(path)
        vlayer = QgsVectorLayer(path, layer_name, "ogr")

        trg_layer = layer_name

        if "_" in trg_layer:
            trg_layer = trg_layer.split("_")[-1].split(".")[0]

        if self.chk_load_style.isChecked():
                style_path = os.path.join(os.path.dirname(__file__), 'data', f'{trg_layer.lower()}.qml')
                if os.path.exists(style_path):
                     vlayer.loadNamedStyle(style_path)

        if vlayer.isValid():
            QgsProject.instance().addMapLayer(vlayer)

    def on_download_finished_not_load(self, features_data):
        # self.show_info("!!!")
        # features_data is a list of dicts, or empty list on failure
        # if not features_data: # and self.download_task.exception:
            # self.show_error(f"Błąd pobierania")#: {self.download_task.exception}")
            # self.reset_ui()
            # return

        if not features_data:
            self.show_info("Pobieranie anulowane lub brak danych.")
            self.reset_ui()
            return
        
        local_filter_geom = self.local_filter_geom

        try:

            if local_filter_geom:
                filtered = []
                for f in features_data:
                    f_geom = QgsGeometry.fromWkt(f['geom'])
                    if f_geom.intersects(local_filter_geom):
                        filtered.append(f)
                features_data = filtered
        
        except Exception as e:
                QgsMessageLog.logMessage(f"[UI] Błąd geometrii: {e}", "PD_GUGiK", Qgis.MessageLevel.Warning)
        
        self.create_layer(features_data)
        self.reset_ui()
        self.show_info(f"Pobrano {len(features_data)} obiektów.")

    def on_download_finished(self, features_data):
        # features_data is a list of dicts, or empty list on failure
        if not features_data and self.download_task.exception:
             self.show_error(f"Błąd pobierania: {self.download_task.exception}")
             self.reset_ui()
             return

        if not features_data:
             self.show_info("Pobieranie anulowane lub brak danych.")
             self.reset_ui()
             return

        self.create_layer(features_data)
        self.reset_ui()
        self.show_info(f"Pobrano {len(features_data)} obiektów.")

    def create_layer(self, features_data):
        if not features_data:
            return
        existing_ids = set()

        trg_layer = "Dzialki"
        unique_field = 'ID_DZIALKI'
        geom_type = "Polygon"


        if self.cmbObjType.currentText() == "budynki (EGIB)":
            unique_field = 'ID_BUDYNKU'
            trg_layer = "Budynki"
        elif self.cmbObjType.currentText() == "dzialki (RCN)":
            unique_field = 'tran_lokalny_id_iip'
            trg_layer = "Dzialki_RCN"
        elif self.cmbObjType.currentText() == "budynki (RCN)":
            unique_field = 'tran_lokalny_id_iip'
            trg_layer = "Budynki_RCN"
        elif self.cmbObjType.currentText() == "lokale (RCN)":
            unique_field = 'tran_lokalny_id_iip'
            trg_layer = "Lokale_RCN"
            geom_type = "Point"
        elif self.cmbObjType.currentText() == "adresy (PRG)":
            unique_field = 'simc'
            trg_layer = "adresy_PRG"
            geom_type = "Point"
        elif self.cmbObjType.currentText() == "ulice (PRG)":
            unique_field = 'simc'
            trg_layer = "ulice_PRG"
            geom_type = "MultiLineString"
        elif self.cmbObjType.currentText() == "place (PRG)":
            unique_field = 'simc'
            trg_layer = "place_PRG"
        elif "(PRG_" in self.cmbObjType.currentText():
            if "_A)" in self.cmbObjType.currentText():
                unique_field = 'JPT_KOD_JE'
            elif "_P)" in self.cmbObjType.currentText():
                unique_field = 'JPT_JOR_ID'
            elif "_R)" in self.cmbObjType.currentText():
                if self.cmbObjType.currentText()[2] == "1":
                    unique_field = 'REJ'
                else:
                    unique_field = 'OBWOD'
            elif "_S)" in self.cmbObjType.currentText():
                unique_field = 'JPT_ID'
            elif "_U)" in self.cmbObjType.currentText():
                # unique_field = 'JPT_ID'
                unique_field = 'boundedBy'
            elif "_W)" in self.cmbObjType.currentText():
                # unique_field = 'JPT_ID'
                unique_field = 'boundedBy'
                if self.cmbObjType.currentText()[1:2] not in ["02", "03", "04", "05", "10", "11", "12"]:
                    geom_type = "MultiLineString"
            else:
                unique_field = 'boundedBy'
            tmp_l = self.cmbObjType.currentText().split(" ")[0]
            tmp_k = self.cmbObjType.currentText()[0]
            trg_layer = f"{tmp_l}-{tmp_k}_prg"
        elif "(PRNG)" in self.cmbObjType.currentText():
            unique_field = 'IDIIP'
            tmp_l = self.cmbObjType.currentText().split(" ")[0]
            trg_layer = f"{tmp_l}_PRNG"
            geom_type = "Point"

        QgsMessageLog.logMessage(f"unique_field: {unique_field}", "PD_GUGiK", Qgis.MessageLevel.Warning)
        

        # self.show_info(f"Current Objtype: {self.cmbObjType.currentText()}")

        layers = QgsProject.instance().mapLayersByName(trg_layer)

        # Check if layer "Dzialki" already exists
        
        if layers:
            vl = layers[0]
            pr = vl.dataProvider()

            if unique_field not in ["simc"]:
            
                idx = vl.fields().indexOf(unique_field)
                if idx != -1:
                    # Pobieramy tylko wartości z jednej kolumny dla szybkości
                    existing_ids = set(f.attribute(unique_field) for f in vl.getFeatures())
        else:
            # Create memory layer
            vl = QgsVectorLayer(f"{geom_type}?crs=epsg:2180", trg_layer, "memory")
            pr = vl.dataProvider()
            
            # Define fields based on first feature
            sample_attrs = features_data[0]['attrs']
            fields = [QgsField(k, QVariant.String) for k in sample_attrs.keys()]
            pr.addAttributes(fields)
            vl.updateFields()
            
            if self.chk_load_style.isChecked():
                if "-" in trg_layer:
                    trg_layer = trg_layer.split("-")[-1]
                style_path = os.path.join(os.path.dirname(__file__), 'data', f'{trg_layer.lower()}.qml')
                if os.path.exists(style_path):
                     vl.loadNamedStyle(style_path)

            QgsProject.instance().addMapLayer(vl)
        
        qgs_features = []
        for fd in features_data:

            feat_id = fd['attrs'].get(unique_field)
            if unique_field not in ["simc"]:
                if feat_id in existing_ids:
                    continue

            feat = QgsFeature()
            feat.setFields(vl.fields())
            feat.setGeometry(QgsGeometry.fromWkt(fd['geom']))
            feat.setAttributes([fd['attrs'].get(f.name()) for f in vl.fields()])
            qgs_features.append(feat)

            existing_ids.add(feat_id)
            
        if qgs_features:
            pr.addFeatures(qgs_features)
            vl.updateExtents()
            vl.triggerRepaint()

    def cancel_download(self):
        self.download_stopped = True
        if hasattr(self, 'download_task'):
            self.download_task.cancel()

    def reset_ui(self):
        self.toggle_ui(True)
        self.progressBar.setVisible(False)
        self.btn_cancel.setVisible(False)

    def toggle_ui(self, enabled):
        self.tabWidget.setEnabled(enabled)

    def show_error(self, msg):
        QMessageBox.critical(self, "Błąd", str(msg))

    def show_info(self, msg):
        QMessageBox.information(self, "Info", str(msg))

    def run_precise_search(self):
        """Uruchom wyszukiwanie po nazwie obrębu i numerze działki."""
        obreb_name = self.txt_obreb_name.text().strip()
        dzialka_nr = self.txt_dzialka_nr.text().strip()
        self.search_by_obreb_and_nr(obreb_name, dzialka_nr)

    def search_by_obreb_and_nr(self, obreb_name, dzialka_nr):
        """Wyszukaj działki po nazwie obrębu i numerze działki."""
        if not obreb_name or not dzialka_nr:
            self.show_error("Wpisz nazwę obrębu i numer działki.")
            return

        obreb_teryt = None

        obreb_name_corrected = obreb_name[0].upper() + obreb_name[1:].lower() 

        # Wyszukaj obręby po nazwie
        if obreb_name in self.obreby_by_name:
            obreb_teryt_list = self.obreby_by_name[obreb_name]

            if len(obreb_teryt_list) == 1:
                obreb_teryt = obreb_teryt_list[0]
            else:
                # Wiele obrębów o tej nazwie - zapytaj użytkownika
                selected_obreb = self.select_obreb_from_duplicates(obreb_teryt_list)
                if not selected_obreb:
                    return
                obreb_teryt = selected_obreb

        elif obreb_name_corrected in self.obreby_by_name:
            obreb_teryt_list = self.obreby_by_name[obreb_name_corrected]

            if len(obreb_teryt_list) == 1:
                obreb_teryt = obreb_teryt_list[0]
            else:
                # Wiele obrębów o tej nazwie - zapytaj użytkownika
                selected_obreb = self.select_obreb_from_duplicates(obreb_teryt_list)
                if not selected_obreb:
                    return
                obreb_teryt = selected_obreb
        else:
            self.show_error(f"Nie znaleziono obrębu o nazwie '{obreb_name}'.")
            return

        # Buduj zapytanie: TERYT.obreb + numer dzialki
        dzialka_id = f"{obreb_teryt}.{dzialka_nr}"

        QgsMessageLog.logMessage(
            f"[UI] Wyszukiwanie: obręb '{obreb_name}', działka {dzialka_nr} -> ID: {dzialka_id}",
            "PD_GUGiK", Qgis.MessageLevel.Info
        )

        client = WFSClient()
        filter_xml = client.build_id_filter([dzialka_id])

        if filter_xml:
            self.start_check_hits(filter_xml)

    def select_obreb_from_duplicates(self, obreb_teryt_list):
        """Zapytaj użytkownika o wybór obrębu z listy duplikatów."""
        from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout, QListWidget, QPushButton, QLabel

        dialog = QDialog(self)
        dialog.setWindowTitle("Wybierz obręb")
        dialog.setMinimumWidth(400)

        layout = QVBoxLayout()

        label = QLabel("Znaleziono wiele obrębów o tej nazwie. Wybierz odpowiedni:")
        layout.addWidget(label)

        list_widget = QListWidget()
        for teryt in obreb_teryt_list:
            info = self.obreby_teryt_to_info.get(teryt, {})
            nazwa = info.get('nazwa', teryt)
            gmina = self.get_gmina_name(teryt)
            powiat = self.get_powiat_name(teryt)
            text = f"{nazwa} ({teryt}) - Gmina: {gmina if gmina else '?'}, Powiat: {powiat if powiat else '?'}"
            list_widget.addItem(text)
        layout.addWidget(list_widget)

        btn_ok = QPushButton("Wybierz")
        btn_cancel = QPushButton("Anuluj")

        button_layout = QVBoxLayout()
        button_layout.addWidget(btn_ok)
        button_layout.addWidget(btn_cancel)
        layout.addLayout(button_layout)

        dialog.setLayout(layout)

        selected_teryt = None

        def on_ok():
            nonlocal selected_teryt
            current_row = list_widget.currentRow()
            if current_row >= 0:
                selected_teryt = obreb_teryt_list[current_row]
            dialog.accept()

        def on_cancel():
            dialog.reject()

        btn_ok.clicked.connect(on_ok)
        btn_cancel.clicked.connect(on_cancel)

        if dialog.exec() == QDialog.DialogCode.Accepted and selected_teryt:
            return selected_teryt
        return None

    def closeEvent(self, event):
        self.closingPlugin.emit()
        event.accept()

    #
    #
    # NEW LINES
    def parse_wfs_index_to_features(self, xml_content):
        """
        Uniwersalny parser GML dla usług skorowidzowych WFS (Orto, NMT).
        Wykorzystuje mechanizmy QGIS do bezpiecznego odczytu geometrii i atrybutów.
        """
        features_data = []
        # Zapisz zawartość GML do pliku tymczasowego
        with tempfile.NamedTemporaryFile(delete=False, suffix='.gml') as f:
            f.write(xml_content.encode('utf-8'))
            temp_path = f.name
        
        vl = None
        try:
            # Wczytaj jako ukrytą warstwę wektorową
            vl = QgsVectorLayer(temp_path, "temp_wfs", "ogr")
            if vl.isValid():
                for feat in vl.getFeatures():
                    # Zapisujemy geometrię i wszystkie dostępne atrybuty
                    features_data.append({
                        'geom': QgsGeometry(feat.geometry()),
                        'attrs': {field.name(): feat[field.name()] for field in vl.fields()}
                    })
        except Exception as e:
            QgsMessageLog.logMessage(f"[UI] Błąd parsowania WFS Skorowidzów: {e}", "PD_GUGiK", Qgis.MessageLevel.Warning)
        finally:
            # 1. ZWOLNIENIE BLOKADY PLIKU PRZEZ QGIS/OGR
            if vl is not None:
                del vl
                vl = None
            # 2. USUNIĘCIE PLIKU TYMCZASOWEGO
            # Usuń plik tymczasowy
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception as e:
                    # Jeśli Windows nadal trzyma plik, po prostu to logujemy, nie blokując działania wtyczki
                    QgsMessageLog.logMessage(f"[UI] Nie można usunąć pliku temp {temp_path}: {e}", "PD_GUGiK", Qgis.MessageLevel.Info)
                    
        return features_data

    def search_and_show_wfs_index(self, geom, service_url, layer_name):
        """
        Funkcja, którą wywołujesz np. z run_spatial_download.
        Możesz w parametrach podawać różne url i warstwy (Orto, NMT).
        """
        QgsMessageLog.logMessage(f"[UI] Szukam w skorowidzu: {layer_name}", "PD_GUGiK", Qgis.MessageLevel.Info)
        
        # Przykładowe budowanie filtra (zaimplementowane w Twoim kodzie)
        client = WFSIndexClient(url=service_url)
        filter_xml = client.build_spatial_filter(geom.asWkt())
        
        try:
            # POBIERANIE XML-a GML Z WFS (Tu użyj swojej wbudowanej funkcji)
            gml_content = client.download(filter_xml, layer_name=layer_name) 
            
            # Parsowanie uniwersalne (z geometriami)
            features_data = self.parse_wfs_index_to_features(gml_content)
            
            if not features_data:
                self.show_info("Brak wyników w zaznaczonym obszarze dla wybranej warstwy.")
                return
                
            # Pokaż okienko
            dialog = WfsIndexResultsDialog(features_data, self)
            if dialog.exec():
                urls_to_load = dialog.selected_urls
                mode = dialog.load_mode
                
                if urls_to_load:
                    self.process_selected_rasters(urls_to_load, mode)
                    
        except Exception as e:
            self.show_error(f"Błąd podczas szukania w skorowidzu: {e}")

    # Rozszerzenia traktowane jako chmury punktów
    _POINT_CLOUD_EXTENSIONS = frozenset({".laz", ".las", ".copc.laz", ".copc"})

    def _load_file_as_layer(self, path_or_url: str, layer_name: str) -> bool:
        """
        Ładuje plik jako warstwę QGIS dobierając typ na podstawie rozszerzenia.

        Obsługuje:
          - chmury punktów (.laz, .las, .copc.laz, .copc) → QgsPointCloudLayer
            * strumień /vsicurl/ wymaga formatu COPC; zwykłe LAZ nie obsługuje
              strumieniowania – zostanie zalogowane ostrzeżenie
          - wszystko inne → QgsRasterLayer

        Zwraca True jeśli warstwa została dodana do projektu.
        """
        lower = path_or_url.lower()
        # Sprawdź po najdłuższym możliwym sufiksie (copc.laz przed .laz)
        ext = next(
            (e for e in sorted(self._POINT_CLOUD_EXTENSIONS, key=len, reverse=True)
             if lower.endswith(e)),
            None,
        )

        if ext is not None:
            # Chmura punktów
            is_stream = path_or_url.startswith("/vsicurl/")
            if is_stream and ".copc" not in lower:
                QgsMessageLog.logMessage(
                    f"[LAZ] Plik {layer_name} nie jest w formacie COPC – "                    f"strumieniowanie /vsicurl/ może nie działać. "                    f"Zalecane pobieranie na dysk.",
                    "PD_GUGiK", Qgis.MessageLevel.Warning,
                )
            layer = QgsPointCloudLayer(path_or_url, layer_name, "pdal")
        else:
            # Raster (ortofotomapa, NMT, NMPT …)
            layer = QgsRasterLayer(path_or_url, layer_name)

        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            return True

        QgsMessageLog.logMessage(
            f"Nie udało się wczytać: {path_or_url}",
            "PD_GUGiK", Qgis.MessageLevel.Warning,
        )
        return False

    def process_selected_rasters(self, urls, mode):
        """Wczytuje zaznaczone pliki na mapę (strumieniowo lub przez pobranie)."""
        if mode == "vsicurl":
            added = 0
            for name, url in urls:
                file_name = url.split('/')[-1]
                vsi_url = f"/vsicurl/{url}"
                if self._load_file_as_layer(vsi_url, file_name):
                    added += 1
            self.show_info(f"Dodano {added} z {len(urls)} warstw strumieniowych.")

        elif mode == "download":
            folder_path = QFileDialog.getExistingDirectory(self, "Wybierz folder do zapisu pobranych plików")
            if not folder_path:
                return

            self.toggle_ui(False)
            self.progressBar.setVisible(True)
            total_files = len(urls)

            for i, (name, url) in enumerate(urls):
                file_name = url.split('/')[-1]
                save_path = os.path.join(folder_path, file_name)

                self.iface.mainWindow().statusBar().showMessage(
                    f"Pobieranie pliku {i+1} z {total_files}: {file_name}"
                )

                try:
                    with requests.get(url, stream=True) as r:
                        r.raise_for_status()
                        total_length = r.headers.get('content-length')

                        if total_length is None:
                            self.progressBar.setRange(0, 0)
                            with open(save_path, 'wb') as f:
                                for chunk in r.iter_content(chunk_size=8192):
                                    f.write(chunk)
                                    QgsApplication.processEvents()
                        else:
                            total_length = int(total_length)
                            self.progressBar.setRange(0, 100)
                            downloaded = 0
                            with open(save_path, 'wb') as f:
                                for chunk in r.iter_content(chunk_size=8192):
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    self.progressBar.setValue(
                                        int((downloaded / total_length) * 100)
                                    )
                                    QgsApplication.processEvents()

                    self._load_file_as_layer(save_path, file_name)

                except Exception as e:
                    QgsMessageLog.logMessage(
                        f"Błąd pobierania {file_name}: {e}",
                        "PD_GUGiK", Qgis.MessageLevel.Critical,
                    )

            self.toggle_ui(True)
            self.progressBar.setVisible(False)
            self.iface.mainWindow().statusBar().clearMessage()
            self.show_info(f"Zakończono pobieranie {total_files} plików na dysk.")

    def search_and_show_multiple_wfs_indices(self, geom, service_url, years,
                                              layer_prefix: str = "gugik:SkorowidzOrtofomapy",
                                              layer_suffix: str = ""):
        """
        Przeszukuje wszystkie warstwy skorowidza dla podanej listy lat.

        :param geom:          QgsGeometry obszaru wyszukiwania
        :param service_url:   URL endpointu WFS
        :param years:         Lista lat jako stringi, np. ['2024', '2023']
        :param layer_prefix:  Prefix nazwy warstwy WFS, np. 'gugik:SkorowidzOrtofomapy'
        :param layer_suffix:  Opcjonalny sufiks po roku, np. '' lub '_KRON86'
        """
        QgsMessageLog.logMessage(
            f"[UI] Szukam w skorowidzach ({layer_prefix}) dla {len(years)} lat...",
            "PD_GUGiK", Qgis.MessageLevel.Info,
        )

        client = WFSIndexClient(url=service_url)
        filter_xml = client.build_spatial_filter(geom.asWkt())
        all_features = []

        self.progressBar.setVisible(True)
        self.progressBar.setRange(0, len(years))
        self.iface.mainWindow().statusBar().showMessage(
            "Przeszukiwanie warstw WFS dla wszystkich roczników..."
        )

        for i, rok in enumerate(years):
            wfs_layer_name = f"{layer_prefix}{rok}{layer_suffix}"
            try:
                gml_content = client.download(filter_xml, layer_name=wfs_layer_name)
                features = self.parse_wfs_index_to_features(gml_content)
                if features:
                    all_features.extend(features)
            except Exception as e:
                # Niektóre starsze roczniki mogą nie istnieć – logujemy i pomijamy
                QgsMessageLog.logMessage(
                    f"[UI] Błąd dla warstwy {wfs_layer_name}: {e}",
                    "PD_GUGiK", Qgis.MessageLevel.Warning,
                )

            self.progressBar.setValue(i + 1)
            QgsApplication.processEvents()

        self.progressBar.setVisible(False)
        self.iface.mainWindow().statusBar().clearMessage()

        if not all_features:
            self.show_info("Brak wyników w zaznaczonym obszarze dla wszystkich sprawdzonych lat.")
            return

        dialog = WfsIndexResultsDialog(all_features, self)
        if dialog.exec():
            urls_to_load = dialog.selected_urls
            mode = dialog.load_mode
            if urls_to_load:
                self.process_selected_rasters(urls_to_load, mode)

    def _init_eziudp_tab(self):
        """
        Tworzy zakładkę 'Usługi powiatowe' z EziudpWidget i dodaje ją
        do tabWidget. Zakładka jest początkowo ukryta – pojawia się tylko
        gdy cmbObjType = "Usługi powiatowe (eziudp)".
    
        Wywołaj tę metodę po self.setup_completer() w __init__,
        żeby name_to_teryt był już dostępny.
        """
        from .eziudp_download_dialog import EziudpWidget
        self._eziudp_widget = EziudpWidget(parent=self)
    
        # *** ZMIANA: inject_scope_data zamiast setup_completer ***
        # Przekazujemy pełne słowniki, nie tylko name_to_teryt
        self._eziudp_widget.inject_scope_data(
            self.wojewodztwa,
            self.powiaty,
            self.gminy,
            self.name_to_teryt if hasattr(self, "name_to_teryt") else {},
        )

        self._std_tabs = []
        for i in range(self.tabWidget.count()):
            self._std_tabs.append(
                (self.tabWidget.widget(i), self.tabWidget.tabText(i))
            )
        self._eziudp_tab_visible = False
    
    
    def _update_tabs_for_type(self):
        """
        Chowa standardowe zakładki i pokazuje zakładkę eziudp
        gdy wybrane jest "Usługi powiatowe (eziudp)", i odwrotnie.
    
        Wywołaj tę metodę NA KOŃCU istniejącej metody update_ui_from_type().
        """
        is_eziudp = (self.cmbObjType.currentText() == "Usługi powiatowe (EZiUDP)")
    
        if is_eziudp and not self._eziudp_tab_visible:
            # Usuń standardowe zakładki
            while self.tabWidget.count() > 0:
                self.tabWidget.removeTab(0)
            # Dodaj zakładkę eziudp
            self.tabWidget.addTab(self._eziudp_widget, "Usługi powiatowe")
            self._eziudp_tab_visible = True
    
        elif not is_eziudp and self._eziudp_tab_visible:
            # Usuń zakładkę eziudp
            while self.tabWidget.count() > 0:
                self.tabWidget.removeTab(0)
            # Przywróć standardowe zakładki
            for widget, title in self._std_tabs:
                self.tabWidget.addTab(widget, title)
            self._eziudp_tab_visible = False

    def _init_category_filter(self):
        """
        Tworzy cmbCategory i wstawia go nad cmbObjType programowo
        (nie wymaga edycji .ui).
    
        Kategorie wykrywane automatycznie z tekstu w nawiasach w cmbObjType.
        Specjalne kategorie bez nawiasów trafiają do "Skorowidze".
        """
        from qgis.PyQt.QtWidgets import QComboBox, QLabel, QHBoxLayout, QWidget
    
        # Utwórz widget jeśli nie zdefiniowany w .ui
        if not hasattr(self, "cmbCategory"):
            self.cmbCategory = QComboBox()
            self.cmbCategory.setToolTip(
                "Filtruj listę typów danych według kategorii.\n"
                "Zmiana kategorii ogranicza dostępne opcje w polu poniżej."
            )
    
            # Wstaw cmbCategory nad cmbObjType w tym samym layoucie
            # Szukamy cmbObjType i wstawiamy przed nim
            parent_widget = self.cmbObjType.parentWidget()
            layout = parent_widget.layout() if parent_widget else None
            if layout:
                idx = None
                for i in range(layout.count()):
                    item = layout.itemAt(i)
                    if item and item.widget() is self.cmbObjType:
                        idx = i
                        break
                if idx is not None:
                    row = QHBoxLayout()
                    row.addWidget(QLabel("Kategoria:"))
                    row.addWidget(self.cmbCategory, 1)
                    container = QWidget()
                    container.setLayout(row)
                    layout.insertWidget(idx, container)
    
        # Zbierz unikalne kategorie z cmbObjType
        self._rebuild_category_combo()
    
    
    def _rebuild_category_combo(self):
        """
        Wypełnia cmbCategory unikalnymi kategoriami z cmbObjType.
    
        Kategoria = tekst w nawiasie, np. "(EGIB)" → "EGIB".
        Pozycje bez nawiasów → "Skorowidze".
        Pierwsza pozycja zawsze "Wszystkie".
        """
        import re as _re
    
        # cats = ["Wszystkie"]
        cats = []
        seen = set()
    
        for i in range(self.cmbObjType.count()):
            text = self.cmbObjType.itemText(i)
            m = _re.search(r'\(([^)]+)\)', text)
            if m:
                cat = m.group(1)
            else:
                cat = "Skorowidze"
            if cat not in seen:
                cats.append(cat)
                seen.add(cat)
    
        self.cmbCategory.blockSignals(True)
        prev = self.cmbCategory.currentText()
        self.cmbCategory.clear()
        self.cmbCategory.addItems(cats)
        # Przywróć poprzednią kategorię jeśli jeszcze istnieje
        idx = self.cmbCategory.findText(prev)
        if idx >= 0:
            self.cmbCategory.setCurrentIndex(idx)
        self.cmbCategory.blockSignals(False)
    
        # Zastosuj filtr od razu
        self._apply_category_filter()
    
    
    def _apply_category_filter(self):
        """
        Chowa/pokazuje pozycje w cmbObjType pasujące do wybranej kategorii.
    
        Implementacja: Qt nie obsługuje natywnego ukrywania pozycji w QComboBox,
        więc przebudowujemy cmbObjType zostawiając tylko pasujące pozycje
        i zachowując referencję do pełnej listy w self._all_obj_type_items.
        """
        import re as _re
    
        selected_cat = self.cmbCategory.currentText()
    
        # Zachowaj pełną listę przy pierwszym wywołaniu
        if not hasattr(self, "_all_obj_type_items"):
            self._all_obj_type_items = [
                self.cmbObjType.itemText(i)
                for i in range(self.cmbObjType.count())
            ]
    
        current_text = self.cmbObjType.currentText()
    
        self.cmbObjType.blockSignals(True)
        self.cmbObjType.clear()
    
        for text in self._all_obj_type_items:
            if selected_cat == "Wszystkie":
                self.cmbObjType.addItem(text)
            else:
                m = _re.search(r'\(([^)]+)\)', text)
                cat = m.group(1) if m else "Skorowidze"
                if cat == selected_cat:
                    self.cmbObjType.addItem(text)
    
        # Przywróć poprzednią wartość jeśli jest w filtrze
        idx = self.cmbObjType.findText(current_text)
        if idx >= 0:
            self.cmbObjType.setCurrentIndex(idx)
        else:
            self.cmbObjType.setCurrentIndex(0)
    
        self.cmbObjType.blockSignals(False)
    
        # Wyzwól update UI dla nowego wyboru
        self.update_ui_from_type()