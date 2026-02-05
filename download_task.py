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
            def find_elem(node, tag_name):
                child = node.firstChild()
                while not child.isNull():
                    if child.nodeType() == QDomNode.ElementNode:
                        elem = child.toElement()
                        if elem.localName() == tag_name:
                            return elem
                    child = child.nextSibling()
                return None

            exterior = find_elem(gml_element, "exterior")
            if not exterior:
                if gml_element.localName() == "LinearRing":
                    ring = gml_element
                else:
                    return None
            else:
                ring = find_elem(exterior, "LinearRing")
            
            if not ring:
                return None
                
            pos_list = find_elem(ring, "posList")
            if not pos_list:
                return None
            
            coords_text = pos_list.text().strip()
            if not coords_text:
                return None
                
            dim = 2
            if pos_list.hasAttribute("srsDimension"):
                 try:
                     dim = int(pos_list.attribute("srsDimension"))
                 except:
                     pass
            
            coords = coords_text.split()
            points = []
            
            for i in range(0, len(coords), dim):
                if i+1 < len(coords):
                    try:
                        val1 = float(coords[i])
                        val2 = float(coords[i+1])
                        points.append(QgsPointXY(val2, val1))
                    except:
                        pass
            
            if points:
                return QgsGeometry.fromPolygonXY([points])
                
        except Exception as e:
            QgsMessageLog.logMessage(f"[DownloadTask] Manual parsing exception: {e}", "PobieranieEGIB", Qgis.Warning)
        
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
