from qgis.core import QgsTask, QgsMessageLog, Qgis, QgsGeometry, QgsOgcUtils, QgsPointXY
from qgis.core import QgsFeature, QgsField, QgsFields, QgsWkbTypes
from qgis.PyQt.QtCore import QVariant, pyqtSignal
from qgis.PyQt.QtXml import QDomDocument, QDomNode
import xml.etree.ElementTree as ET
from .wfs_client import WFSClient

class CheckHitsTask(QgsTask):
    """
    Task to check the number of features matching the filter.
    """
    hitsReady = pyqtSignal(int)

    def __init__(self, filter_xml):
        super().__init__("Sprawdzanie liczby obiektów...", QgsTask.CanCancel)
        self.filter_xml = filter_xml
        self.client = WFSClient()
        self.hits = 0
        self.exception = None

    def run(self):
        try:
            self.hits = self.client.get_hits(self.filter_xml)
            return True
        except Exception as e:
            self.exception = e
            return False

    def finished(self, result):
        if result:
            self.hitsReady.emit(self.hits)
        else:
            self.hitsReady.emit(-1)

class DownloadTask(QgsTask):
    """
    Task to download features with pagination and parse them.
    """
    downloadFinished = pyqtSignal(list)
    progressValue = pyqtSignal(float)

    def __init__(self, filter_xml, total_expected=0, attributes=None):
        super().__init__("Pobieranie danych EGiB...", QgsTask.CanCancel)
        self.filter_xml = filter_xml
        self.client = WFSClient()
        self.total_expected = total_expected
        self.attributes = attributes
        self.features_data = [] # List of dicts: {'geom': wkt, 'attrs': {...}}
        self.exception = None
        self.stopped = False

    def run(self):
        start_index = 0
        count = 1000 # Page size
        
        while not self.stopped:
            try:
                if self.isCanceled():
                    return False
                
                if self.total_expected > 0:
                    prog = (start_index / self.total_expected) * 100
                    self.progressValue.emit(int(prog))
                    self.setProgress(int(prog))
                
                gml_content = self.client.download(self.filter_xml, start_index, count, attributes=self.attributes)
                
                new_features = self._parse_gml(gml_content)
                self.features_data.extend(new_features)
                
                if len(new_features) < count:
                    break
                
                start_index += count
                
            except Exception as e:
                self.exception = e
                return False
                
        self.setProgress(100)
        self.progressValue.emit(int(100))
        return True

    def finished(self, result):
        if result:
            self.downloadFinished.emit(self.features_data)
        else:
            self.downloadFinished.emit([])

    def _manual_parse_geometry(self, gml_element):
        try:
            all_rings = []

            # Funkcja pomocnicza do wyciągania punktów z dowolnego węzła (exterior/interior)
            def extract_points_from_ring_node(parent_node):
                ring = None
                # Szukamy LinearRing wewnątrz exterior/interior
                child = parent_node.firstChild()
                while not child.isNull():
                    if child.toElement().localName() == "LinearRing":
                        ring = child.toElement()
                        break
                    child = child.nextSibling()
                
                if not ring: return None
                
                # Szukamy posList wewnątrz LinearRing
                pos_list = None
                child = ring.firstChild()
                while not child.isNull():
                    if child.toElement().localName() == "posList":
                        pos_list = child.toElement()
                        break
                    child = child.nextSibling()
                    
                if not pos_list: return None
                
                coords_text = pos_list.text().strip()
                dim = int(pos_list.attribute("srsDimension", "2"))
                coords = coords_text.split()
                
                points = []
                for i in range(0, len(coords), dim):
                    if i + 1 < len(coords):
                        # Pamiętaj o kolejności (EGiB często ma Y X, czyli Lat Lon)
                        points.append(QgsPointXY(float(coords[i+1]), float(coords[i])))
                return points

            # Iterujemy po wszystkich dzieciach głównego elementu (np. Polygon)
            child = gml_element.firstChild()
            while not child.isNull():
                elem = child.toElement()
                local_name = elem.localName()
                
                if local_name in ["exterior", "interior"]:
                    pts = extract_points_from_ring_node(elem)
                    if pts:
                        all_rings.append(pts)
                
                child = child.nextSibling()

            if all_rings:
                return QgsGeometry.fromPolygonXY(all_rings)
                
        except Exception as e:
            QgsMessageLog.logMessage(f"Manual parsing error: {e}", "PobieranieEGIB", Qgis.Warning)
        
        return None

    def _parse_gml(self, gml_content):
        features = []
        try:
            doc = QDomDocument()
            if not doc.setContent(gml_content, True):
                QgsMessageLog.logMessage("Błąd parsowania XML w DownloadTask", "PobieranieEGIB", Qgis.Warning)
                return []
            
            root = doc.documentElement()
            members = root.elementsByTagNameNS('http://www.opengis.net/wfs/2.0', 'member')
            if members.count() == 0:
                 members = root.elementsByTagName('wfs:member')
            
            for i in range(members.count()):
                member_node = members.item(i)
                feature_elem = None
                child = member_node.firstChild()
                while not child.isNull():
                    if child.nodeType() == QDomNode.ElementNode:
                        feature_elem = child.toElement()
                        break
                    child = child.nextSibling()
                
                if feature_elem is None: continue

                attrs = {}
                geom_wkt = None
                
                prop = feature_elem.firstChild()
                while not prop.isNull():
                    if prop.nodeType() == QDomNode.ElementNode:
                        elem = prop.toElement()
                        name = elem.localName()
                        if not name:
                            name = elem.tagName().split(':')[-1]
                        
                        if name == 'geom':
                            geom_child = elem.firstChild()
                            while not geom_child.isNull():
                                if geom_child.nodeType() == QDomNode.ElementNode:
                                    g_elem = geom_child.toElement()
                                    try:
                                        ggeom = QgsOgcUtils.geometryFromGML(g_elem)
                                        if ggeom and not ggeom.isEmpty():
                                            geom_wkt = ggeom.asWkt()
                                        else:
                                            ggeom_manual = self._manual_parse_geometry(g_elem)
                                            if ggeom_manual and not ggeom_manual.isEmpty():
                                                geom_wkt = ggeom_manual.asWkt()
                                    except:
                                        pass
                                    break
                                geom_child = geom_child.nextSibling()
                        else:
                            attrs[name] = elem.text()
                            
                    prop = prop.nextSibling()

                if geom_wkt:
                    features.append({'geom': geom_wkt, 'attrs': attrs})
        except Exception as e:
            QgsMessageLog.logMessage(f"GML Parse Error: {str(e)}", "PobieranieEGIB", Qgis.Warning)
            
        return features

    def cancel(self):
        self.stopped = True
        super().cancel()
