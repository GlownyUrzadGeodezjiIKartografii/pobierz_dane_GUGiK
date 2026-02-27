# -*- coding: utf-8 -*-
"""
PRG WFS Client - Państwowy Rejestr Granic
Pobiera geometrię jednostek administracyjnych dla filtrowania działek EGiB
"""
import requests
from qgis.core import QgsMessageLog, Qgis, QgsGeometry, QgsCoordinateReferenceSystem, QgsOgcUtils, QgsPointXY
from PyQt5.QtXml import QDomDocument, QDomNode


class PRGClient:
    def __init__(self, url="http://mapy.geoportal.gov.pl/wss/service/PZGIK/PRG/WFS/AdministrativeBoundaries"):
        self.url = url
        self.ns = {
            'wfs': 'http://www.opengis.net/wfs/2.0',
            'gml': 'http://www.opengis.net/gml/3.2',
            'ms': 'http://mapserver.gis.umn.edu/mapserver'
        }
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Content-Type': 'application/xml'
        })

    def get_boundary_geometry(self, teryt):
        """
        Pobiera geometrię jednostki administracyjnej na podstawie kodu TERYT.

        :param teryt: Kod TERYT (np. "14", "1412", "141201_1", "141201_1.0001")
        :returns: QgsGeometry w EPSG:2180 lub None
        """
        if not teryt:
            return None

        length = len(teryt)
        
        # Determine layer based on TERYT length/format
        feature_type = None
        filter_property = "ms:JPT_KOD_JE" # Corrected with ms: prefix
        
        # Województwo: 2 digits (e.g. "14")
        if length == 2 and teryt.isdigit():
            feature_type = 'ms:A01_Granice_wojewodztw'
            
        # Powiat: 4 digits (e.g. "1412")
        elif length == 4 and teryt.isdigit():
            feature_type = 'ms:A02_Granice_powiatow'
            
        # Gmina: 8 chars usually "WWPPGG_R" (e.g. "141201_1")
        elif length == 8 and '_' in teryt:
             feature_type = 'ms:A05_Granice_jednostek_ewidencyjnych'
             
        # Obręb: "WWPPGG_R.OOOO" (e.g. "141201_1.0001")
        elif '.' in teryt:
            feature_type = 'ms:A06_Granice_obrebow_ewidencyjnych'
            # feature_type = 'ms:A03_Granice_gmin'
            
        else:
            # Fallback heuristics
            if length == 6 and teryt.isdigit():
                 feature_type = 'ms:A05_Granice_jednostek_ewidencyjnych'
            else:
                 feature_type = 'ms:A06_Granice_obrebow_ewidencyjnych'

        QgsMessageLog.logMessage(f"[PRG] Pobieranie geometrii dla {teryt} z warstwy {feature_type}", "PobieranieEGIB", Qgis.Info)


        geom = self._fetch_geometry(feature_type, filter_property, teryt)
        if geom is None or geom.isEmpty():
            QgsMessageLog.logMessage(f"[PRG] Próba pobierania z innej warstwy", "PobieranieEGIB", Qgis.Info)
            if feature_type.startswith("ms:A05"):
                geom = self._fetch_geometry('ms:A03_Granice_gmin', filter_property, teryt.replace("_", ""))
        return geom

    def _fetch_geometry(self, feature_type, property_name, value):
        """
        Wykonuje zapytanie GetFeature do PRG i zwraca geometrię.

        :param feature_type: Typ warstwy WFS (np. ms:A01_Granice_wojewodztw)
        :param property_name: Nazwa atrybutu do filtrowania
        :param value: Wartość atrybutu
        :returns: QgsGeometry lub None
        """
        # Add ms namespace to filter as requested
        filter_xml = f"""<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0" xmlns:ms="http://mapserver.gis.umn.edu/mapserver"><fes:PropertyIsEqualTo><fes:ValueReference>{property_name}</fes:ValueReference><fes:Literal>{value}</fes:Literal></fes:PropertyIsEqualTo></fes:Filter>"""

        params = {
            'service': 'WFS',
            'version': '2.0.0',
            'request': 'GetFeature',
            'typenames': feature_type,
            'outputFormat': 'application/gml+xml; version=3.2',
            'language': 'pol',
            'filter': filter_xml
        }

        # Construct full URL for logging (browser-ready)
        from requests import Request
        req = Request('GET', self.url, params=params)
        prepared_url = req.prepare().url
        QgsMessageLog.logMessage(f"[PRG] Zapytanie URL: {prepared_url}", "PobieranieEGIB", Qgis.Info)

        try:
            response = self.session.get(self.url, params=params, timeout=60)
            response.raise_for_status()

            QgsMessageLog.logMessage(f"[PRG] Odpowiedź: Status {response.status_code}, Size: {len(response.content)} bytes", "PobieranieEGIB", Qgis.Info)

            return self._parse_geometry(response.text)

        except Exception as e:

            QgsMessageLog.logMessage(f"[PRG] Błąd pobierania geometrii: {e}", "PobieranieEGIB", Qgis.Warning)
            return None

    def _manual_parse_geometry(self, gml_element):
        """
        Zaktualizowany fallback ręcznego parsowania GML, obsługujący 
        enklawy (otwory / interior) oraz obiekty wieloczęściowe (MultiSurface).
        """
        try:
            def get_points(node):
                # Rekurencyjnie szuka tagu posList wewnątrz węzła
                pos_list = None
                def find_poslist(n):
                    nonlocal pos_list
                    if pos_list is not None: return
                    if n.nodeType() == QDomNode.ElementNode and n.toElement().localName() == "posList":
                        pos_list = n.toElement()
                        return
                    c = n.firstChild()
                    while not c.isNull():
                        find_poslist(c)
                        c = c.nextSibling()
                
                find_poslist(node)
                if not pos_list: return []
                
                coords_text = pos_list.text().strip()
                if not coords_text: return []
                
                dim = 2
                if pos_list.hasAttribute("srsDimension"):
                    try: dim = int(pos_list.attribute("srsDimension"))
                    except ValueError: pass
                
                coords = coords_text.split()
                points = []
                for i in range(0, len(coords), dim):
                    if i+1 < len(coords):
                        try:
                            val1 = float(coords[i])
                            val2 = float(coords[i+1])
                            # GML z PRG często zwraca X/Y jako (North, East). 
                            # QGIS w EPSG:2180 oczekuje X,Y (East, North), stąd zamiana miejscami: val2, val1
                            points.append(QgsPointXY(val2, val1))
                        except ValueError:
                            pass
                return points

            # Krok 1: Znajdź wszystkie węzły pojedynczych poligonów (Polygon, Surface, PolygonPatch itp.)
            polygons = []
            def find_polygons(n):
                if n.nodeType() == QDomNode.ElementNode:
                    # GML może używać Polygon lub Surface dla bloków wewnątrz MultiSurface
                    if n.toElement().localName() in ["Polygon", "Surface", "PolygonPatch"]:
                        polygons.append(n)
                c = n.firstChild()
                while not c.isNull():
                    find_polygons(c)
                    c = c.nextSibling()

            find_polygons(gml_element)
            
            # Zabezpieczenie na wypadek zwykłego LinearRing bez tagów Polygon
            if not polygons and gml_element.localName() == "LinearRing":
                pts = get_points(gml_element)
                if pts: return QgsGeometry.fromPolygonXY([pts])
                return None

            multi_polygon_rings = []

            # Krok 2: Dla każdego poligonu wydobądź obręb zewnętrzny oraz wszystkie enklawy
            for poly_node in polygons:
                poly_rings = []
                exterior_pts = []
                interior_rings = []
                
                child = poly_node.firstChild()
                while not child.isNull():
                    if child.nodeType() == QDomNode.ElementNode:
                        elem = child.toElement()
                        if elem.localName() == "exterior":
                            exterior_pts = get_points(elem)
                        elif elem.localName() == "interior":
                            pts = get_points(elem)
                            if pts: interior_rings.append(pts)
                    child = child.nextSibling()
                
                # Zgodnie ze standardem QGIS, pierwszy zestaw punktów to ZAWSZE exterior
                # kolejne w liście poly_rings reprezentują dziury (interior)
                if exterior_pts:
                    poly_rings.append(exterior_pts)
                    poly_rings.extend(interior_rings)
                    multi_polygon_rings.append(poly_rings)

            # Krok 3: Połącz w jedną, wieloczęściową geometrię obsługującą dziury
            if multi_polygon_rings:
                return QgsGeometry.fromMultiPolygonXY(multi_polygon_rings)

        except Exception as e:
            QgsMessageLog.logMessage(f"[PRG] Manual parsing exception: {e}", "PobieranieEGIB", Qgis.Warning)
        
        return None

    def _parse_geometry(self, gml_content):
        """
        Parsuje geometrię z odpowiedzi WFS.

        :param gml_content: Zawartość GML
        :returns: QgsGeometry lub None
        """
        try:
            doc = QDomDocument()
            if not doc.setContent(gml_content, True):
                QgsMessageLog.logMessage("[PRG] Błąd parsowania XML (setContent)", "PobieranieEGIB", Qgis.Warning)
                return None

            root = doc.documentElement()

            members = root.elementsByTagNameNS('http://www.opengis.net/wfs/2.0', 'member')
            if members.count() == 0:
                members = root.elementsByTagName('wfs:member')

            if members.count() == 0:
                QgsMessageLog.logMessage("[PRG] Nie znaleziono wfs:member", "PobieranieEGIB", Qgis.Warning)
                return None

            member_node = members.item(0)

            feature_elem = None
            child = member_node.firstChild()
            while not child.isNull():
                if child.nodeType() == QDomNode.ElementNode:
                    feature_elem = child.toElement()
                    break
                child = child.nextSibling()

            if feature_elem is None:
                QgsMessageLog.logMessage("[PRG] Nie znaleziono elementu cechy w member", "PobieranieEGIB", Qgis.Warning)
                return None

            geom_elem = None
            geom_prop = feature_elem.firstChild()
            while not geom_prop.isNull():
                if geom_prop.nodeType() == QDomNode.ElementNode:
                    elem = geom_prop.toElement()
                    
                    name = elem.localName()
                    if not name:
                         name = elem.tagName().split(':')[-1]
                    
                    if name == 'msGeometry':
                        geom_child = elem.firstChild()
                        while not geom_child.isNull():
                            if geom_child.nodeType() == QDomNode.ElementNode:
                                geom_elem = geom_child.toElement()
                                break
                            geom_child = geom_child.nextSibling()
                        break
                geom_prop = geom_prop.nextSibling()

            if geom_elem is None:
                QgsMessageLog.logMessage("[PRG] Nie znaleziono geometrii", "PobieranieEGIB", Qgis.Warning)
                return None

            geom = QgsOgcUtils.geometryFromGML(geom_elem)

            if not geom or geom.isEmpty():
                QgsMessageLog.logMessage("[PRG] QgsOgcUtils zwrócił pustą geometrię, próba ręcznego parsowania", "PobieranieEGIB", Qgis.Warning)
                geom = self._manual_parse_geometry(geom_elem)
                if geom and not geom.isEmpty():
                    QgsMessageLog.logMessage(f"[PRG] Ręczne parsowanie udane {geom_elem}", "PobieranieEGIB", Qgis.Info)
                else:
                    QgsMessageLog.logMessage("[PRG] Ręczne parsowanie również nie powiodło się", "PobieranieEGIB", Qgis.Warning)
                    return None

            return geom

        except Exception as e:
            QgsMessageLog.logMessage(f"[PRG] Błąd parsowania geometrii: {e}", "PobieranieEGIB", Qgis.Warning)
            return None
    
