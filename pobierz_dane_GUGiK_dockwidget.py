# -*- coding: utf-8 -*-
import os
import json
from qgis.PyQt import QtWidgets, uic
from qgis.PyQt.QtCore import pyqtSignal, Qt, QVariant, QSettings
from qgis.PyQt.QtWidgets import QMessageBox, QCompleter, QListWidgetItem
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsFeature, QgsGeometry,
    QgsField, QgsFields, QgsWkbTypes, QgsCoordinateTransform,
    QgsCoordinateReferenceSystem, QgsRectangle, QgsApplication,
    QgsMessageLog, Qgis
)
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand, QgsMapLayerComboBox

from .wfs_client import WFSClient
from .download_task import CheckHitsTask, DownloadTask
from .prg_client import PRGClient

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


class PobieranieEGIBDockWidget(QtWidgets.QDockWidget, FORM_CLASS):

    closingPlugin = pyqtSignal()

    def __init__(self, parent=None):
        """Constructor."""
        super(PobieranieEGIBDockWidget, self).__init__(parent)
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
            
            # layout.insertWidget(layout.count() - 2, self.chk_precise_spatial)
            layout.insertWidget(layout.count() - 2, self.btn_download_prg_geom)

        # Add new tab for precise search (obreb + dzialka nr)
        if hasattr(self, 'tabWidget'):
            from qgis.PyQt.QtWidgets import QWidget, QVBoxLayout, QLabel, QLineEdit, QPushButton, QFormLayout

            self.tab_precise = QWidget()
            self.tab_precise.setObjectName("tab_precise")

            precise_layout = QVBoxLayout()

            # Form layout for inputs
            form_layout = QFormLayout()

            self.lbl_obreb_name = QLabel("Nazwa obrębu:")
            self.txt_obreb_name = QLineEdit()
            self.txt_obreb_name.setPlaceholderText("np. Łasków")

            self.lbl_dzialka_nr = QLabel("Numer działki:")
            self.txt_dzialka_nr = QLineEdit()
            self.txt_dzialka_nr.setPlaceholderText("np. 123/1")

            form_layout.addRow(self.lbl_obreb_name, self.txt_obreb_name)
            form_layout.addRow(self.lbl_dzialka_nr, self.txt_dzialka_nr)

            precise_layout.addLayout(form_layout)

            # Search buttons
            self.btn_search_obreb_nr = QPushButton("POBIERZ")
            self.btn_search_obreb_nr.clicked.connect(self.run_precise_search)

            precise_layout.addWidget(self.btn_search_obreb_nr)
            precise_layout.addStretch()

            self.tab_precise.setLayout(precise_layout)

            settings_index = self.tabWidget.indexOf(self.tab_settings)
            if settings_index != -1:
                # Wstawiamy przed zakładkę ustawień
                self.tabWidget.insertTab(settings_index, self.tab_precise, "Obręb + nr działki")
            else:
                # Jeśli z jakiegoś powodu nie znaleziono tab_settings, dodaj na koniec
                self.tabWidget.addTab(self.tab_precise, "Obręb + nr działki")

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

        from qgis.utils import iface
        self.iface = iface
        self.canvas = self.iface.mapCanvas()
        
        # Internal state
        self.download_stopped = False
        
        # Initialize Settings and Attributes
        self.init_settings_ui()
        self.setup_completer()

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
            item.setCheckState(Qt.Checked)
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
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchContains)
        self.txt_teryt_manual.setCompleter(self.completer)
        
        self.completer.activated.connect(self.on_completer_activated)

    def on_completer_activated(self, text):
        import re
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
                    self.cmb_woj.addItem("Cała Polska", "")
                    for teryt_str, w in sorted(self.wojewodztwa.items(), key=lambda x: x[1]['nazwa']):
                        self.cmb_woj.addItem(f"{w['nazwa']} ({teryt_str})", teryt_str)
                    QgsMessageLog.logMessage(f"Załadowano {count} województw.", "PobieranieEGIB", Qgis.Info)

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
                    QgsMessageLog.logMessage(f"Załadowano {count} powiatów.", "PobieranieEGIB", Qgis.Info)

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
                    QgsMessageLog.logMessage(f"Załadowano {count} gmin.", "PobieranieEGIB", Qgis.Info)
            
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
                    QgsMessageLog.logMessage(f"Załadowano {count} obrębów.", "PobieranieEGIB", Qgis.Info)
            else:
                self.obreby_by_gmina = {}
                self.obreby_by_name = {}
                self.obreby_teryt_to_info = {}

        except Exception as e:
            msg = f"Błąd ładowania danych: {e}"
            QgsMessageLog.logMessage(msg, "PobieranieEGIB", Qgis.Critical)
            self.show_error(msg)

    def connect_signals(self):
        self.cmb_woj.currentIndexChanged.connect(self.update_teryt_from_woj)
        self.cmb_pow.currentIndexChanged.connect(self.update_teryt_from_pow)
        self.cmb_gmina.currentIndexChanged.connect(self.update_teryt_from_gmina)
        self.cmb_obreb.currentIndexChanged.connect(self.update_teryt_from_obreb)

        self.btn_download_admin.clicked.connect(self.run_admin_download)
        self.btn_download_ids.clicked.connect(self.run_id_download)
        
        self.btn_select_rect.clicked.connect(self.toggle_map_tool)
        self.btn_download_extent.clicked.connect(self.run_extent_download)
        
        self.btn_cancel.clicked.connect(self.cancel_download)
        self.txt_teryt_manual.textChanged.connect(self.validate_teryt)

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
            self.txt_teryt_manual.setText("")
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
            "PobieranieEGIB", Qgis.Warning
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
            "PobieranieEGIB", Qgis.Info
        )

        try:
            geom = self.prg_client.get_boundary_geometry(manual_text)
        except Exception as e:
            self.show_error(f"Błąd podczas pobierania geometrii z PRG: {e}")
            return

        if geom is None or geom.isEmpty():
            msg = f"Nie udało się pobrać geometrii dla TERYT: {manual_text}. Sprawdź poprawność kodu."
            QgsMessageLog.logMessage(f"[UI] {msg}", "PobieranieEGIB", Qgis.Warning)
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
        QgsProject.instance().addMapLayer(vl)
        
        # Zoom to extent
        if self.canvas:
            self.canvas.setExtent(vl.extent())
            self.canvas.refresh()

        QgsMessageLog.logMessage(f"[UI] Dodano warstwę z granicą PRG dla {manual_text}", "PobieranieEGIB", Qgis.Info)

    def run_admin_download(self):
        client = WFSClient()
        filter_xml = None

        manual_text = self.txt_teryt_manual.text().strip()
        if not manual_text:
            self.show_error("Wpisz kod TERYT do wyszukania.")
            return

        manual_text = manual_text.split(" ")[-1].replace(")", "") if "(" in manual_text else manual_text

        QgsMessageLog.logMessage(
            f"[UI] Pobieranie działek dla TERYT: {manual_text}",
            "PobieranieEGIB", Qgis.Info
        )

        # Pobierz geometrię jednostki administracyjnej z PRG
        try:
            geom = self.prg_client.get_boundary_geometry(manual_text)
        except Exception as e:
            self.show_error(f"Błąd podczas pobierania geometrii z PRG: {e}")
            return

        if geom is None or geom.isEmpty():
            msg = f"Nie udało się pobrać geometrii dla TERYT: {manual_text}. Sprawdź poprawność kodu."
            QgsMessageLog.logMessage(f"[UI] {msg}", "PobieranieEGIB", Qgis.Warning)
            self.show_error(msg)
            return

        # Zbuduj filtr przestrzenny z geometrii PRG
        # Sprawdź czy użytkownik chce dokładne filtrowanie
        use_precise = hasattr(self, 'chk_precise_spatial') and self.chk_precise_spatial.isChecked()

        filter_xml = client.build_spatial_filter(geom.asWkt(), use_bbox=not use_precise)

        if not filter_xml:
            self.show_error("Błąd budowania filtra przestrzennego.")
            return

        # Użyj przestrzennego filtra - bardziej skuteczne niż PropertyIsLike
        filter_type = "dokładnym (polygon)" if use_precise else "przybliżonym (BBOX)"
        QgsMessageLog.logMessage(
            f"[UI] Rozpoczynam pobieranie z {filter_type} filtrem przestrzennym dla obszaru administracyjnego.",
            "PobieranieEGIB", Qgis.Info
        )
        
        # Zbuduj filtr atrybutowy (początek id_dzialki musi się zgadzać z TERYT)
        attr_filter = client.build_attribute_filter('id_dzialki', manual_text, like=True)
        
        # Połącz filtry: Przestrzenny AND Atrybutowy
        combined_filter = client.combine_filters([filter_xml, attr_filter])

        # Get selected attributes
        attributes = [self.list_attributes.item(i).text() for i in range(self.list_attributes.count()) 
                      if self.list_attributes.item(i).checkState() == Qt.Checked]
        if len(attributes) == self.list_attributes.count(): attributes = None

        self.start_download_direct(combined_filter, attributes=attributes)

    def run_id_download(self):
        ids_text = self.txt_ids.toPlainText()
        if not ids_text.strip():
            self.show_error("Wpisz identyfikatory.")
            return

        all_ids = [line.strip() for line in ids_text.splitlines() if line.strip()]
        client = WFSClient()
        
        attributes = [self.list_attributes.item(i).text() for i in range(self.list_attributes.count()) 
                      if self.list_attributes.item(i).checkState() == Qt.Checked]
        if len(attributes) == self.list_attributes.count(): attributes = None

        # Batching by 10
        batch_size = 10
        all_features = []
        
        from .download_task import DownloadTask
        
        QgsMessageLog.logMessage(f"[UI] Pobieranie {len(all_ids)} identyfikatorów w paczkach po {batch_size}", "PobieranieEGIB", Qgis.Info)
        
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
                QgsMessageLog.logMessage(f"[UI] Błąd w paczce {i//batch_size + 1}: {e}", "PobieranieEGIB", Qgis.Warning)

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
                "PobieranieEGIB", Qgis.Info
            )

        geom = QgsGeometry.fromRect(rect)
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
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.No:
                return

        QgsMessageLog.logMessage(
            f"[UI] Rozpoczynam pobieranie przez warstwę: {layer.name()} ({feature_count} obiektów)",
            "PobieranieEGIB", Qgis.Info
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
                if self.list_attributes.item(i).checkState() == Qt.Checked]
        if len(attributes) == self.list_attributes.count(): attributes = None

        for feat in layer.getFeatures():
            current += 1
            geom = feat.geometry()

            QgsMessageLog.logMessage(
                    f"[UI] Pobieranie {current}/{feature_count}",
                    "PobieranieEGIB", Qgis.Warning
                )

            # Skip invalid geometries
            if geom is None or geom.isEmpty():
                QgsMessageLog.logMessage(
                    f"[UI] Pominięto geometrię {current}/{feature_count}: pusta/nieprawidłowa",
                    "PobieranieEGIB", Qgis.Warning
                )
                skipped += 1
                continue

            # Transform to EPSG:2180 if needed
            if xform is not None:
                geom.transform(xform)

            # Smart filtering: check vertex count
            if geom.constGet().nCoordinates() > 100:
                QgsMessageLog.logMessage(f"[UI] Geometria {current} posiada dużo wierzchołków ({geom.constGet().nCoordinates()}), używam BBOX + filtr lokalny", "PobieranieEGIB", Qgis.Info)
                filter_xml = client.build_spatial_filter(geom.asWkt(), use_bbox=True)
                #QgsMessageLog.logMessage(
                    #f"[UI] Geometria fitrująca: {filter_xml}",
                    #"PobieranieEGIB", Qgis.Warning
                #)
                local_filter_geom = geom # feat.geometry().transform(xform)
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
                    QgsMessageLog.logMessage(f"[UI] Błąd geometrii: {e}", "PobieranieEGIB", Qgis.Warning)
            
            self.create_layer(features_data)

            QgsMessageLog.logMessage(
                f"[UI] Pobieranie {current}/{feature_count} z filtrem localnym: {local_filter}",
                "PobieranieEGIB", Qgis.Info
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

    def start_download_direct(self, filter_xml, attributes=None):
        """Bezpośrednio rozpoczynamy pobieranie bez sprawdzania hits."""
        self.download_stopped = False
        reply = QMessageBox.question(
            self, "Potwierdzenie",
            "Rozpoczynamy pobieranie danych.\n\nPobieranie może zająć dużo czasu w zależności od ilości danych.\n\nMożesz przerwać w dowolnym momencie.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.No:
            return

        self.start_download(filter_xml, 100000, attributes=attributes)

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
                      if self.list_attributes.item(i).checkState() == Qt.Checked]
        if len(attributes) == self.list_attributes.count(): attributes = None

        limit = 5000
        if hits > limit:
            reply = QMessageBox.question(
                self, "Potwierdzenie",
                f"Zapytanie może zwróć około {hits} obiektów.\n\nCzy kontynuować?",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.No:
                self.reset_ui()
                return

        self.start_download(filter_xml, hits, attributes=attributes)

    def start_download(self, filter_xml, total, attributes=None, local_filter_geom=False):
        self.progressBar.setRange(0, 100)
        self.btn_cancel.setVisible(True)

        self.download_task = DownloadTask(filter_xml, total_expected=total, attributes=attributes)

        
        if local_filter_geom:
            self.download_task.downloadFinished.connect(self.on_download_finished_not_load)

        else:
            
            self.download_task.downloadFinished.connect(self.on_download_finished)
        self.download_task.progressValue.connect(lambda val: self.progressBar.setValue(int(val)))
        QgsApplication.taskManager().addTask(self.download_task)

    def on_download_finished_not_load(self, features_data):
        # self.show_info("!!!")
        # features_data is a list of dicts, or empty list on failure
        if not features_data: # and self.download_task.exception:
             self.show_error(f"Błąd pobierania")#: {self.download_task.exception}")
             self.reset_ui()
             return

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
                QgsMessageLog.logMessage(f"[UI] Błąd geometrii: {e}", "PobieranieEGIB", Qgis.Warning)
        
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
        
        unique_field = 'ID_DZIALKI'
        existing_ids = set()

        # Check if layer "Dzialki" already exists
        layers = QgsProject.instance().mapLayersByName("Dzialki")
        if layers:
            vl = layers[0]
            pr = vl.dataProvider()
            
            idx = vl.fields().indexOf(unique_field)
            if idx != -1:
                # Pobieramy tylko wartości z jednej kolumny dla szybkości
                existing_ids = set(f.attribute(unique_field) for f in vl.getFeatures())
        else:
            # Create memory layer
            vl = QgsVectorLayer("Polygon?crs=epsg:2180", "Dzialki", "memory")
            pr = vl.dataProvider()
            
            # Define fields based on first feature
            sample_attrs = features_data[0]['attrs']
            fields = [QgsField(k, QVariant.String) for k in sample_attrs.keys()]
            pr.addAttributes(fields)
            vl.updateFields()
            
            if self.chk_load_style.isChecked():
                style_path = os.path.join(os.path.dirname(__file__), 'data', 'dzialki.qml')
                if os.path.exists(style_path):
                     vl.loadNamedStyle(style_path)

            QgsProject.instance().addMapLayer(vl)
        
        qgs_features = []
        for fd in features_data:
            feat_id = fd['attrs'].get(unique_field)
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
        else:
            self.show_error(f"Nie znaleziono obrębu o nazwie '{obreb_name}'.")
            return

        # Buduj zapytanie: TERYT.obreb + numer dzialki
        dzialka_id = f"{obreb_teryt}.{dzialka_nr}"

        QgsMessageLog.logMessage(
            f"[UI] Wyszukiwanie: obręb '{obreb_name}', działka {dzialka_nr} -> ID: {dzialka_id}",
            "PobieranieEGIB", Qgis.Info
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

        if dialog.exec_() == QDialog.Accepted and selected_teryt:
            return selected_teryt
        return None

    def closeEvent(self, event):
        self.closingPlugin.emit()
        event.accept()