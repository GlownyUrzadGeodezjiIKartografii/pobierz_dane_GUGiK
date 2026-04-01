# -*- coding: utf-8 -*-
from qgis.core import QgsTask, QgsMessageLog, Qgis, QgsGeometry, QgsOgcUtils, QgsPointXY
from qgis.core import QgsFeature, QgsField, QgsFields, QgsWkbTypes
from qgis.PyQt.QtCore import QVariant, pyqtSignal
from qgis.PyQt.QtXml import QDomDocument, QDomNode

from .base_wfs_client import BaseWFSClient, WFSClient, EGIBClientBudynki, RCNClient, PRGClientAdresy, PRNGClient, PRGClientAdmin

LOG_TAG = "PD_GUGiK"


class CheckHitsTask(QgsTask):
    """
    Zadanie sprawdzające liczbę obiektów pasujących do filtra.
    """
    hitsReady = pyqtSignal(int)

    def __init__(self, filter_xml: str):
        super().__init__("Sprawdzanie liczby obiektów...", QgsTask.Flag.CanCancel)
        self.filter_xml = filter_xml
        self.client = WFSClient()
        self.hits = 0
        self.exception = None

    def run(self) -> bool:
        try:
            self.hits = self.client.get_hits(self.filter_xml)
            return True
        except Exception as e:
            self.exception = e
            return False

    def finished(self, result: bool):
        self.hitsReady.emit(self.hits if result else -1)


class DownloadTask(QgsTask):
    """
    Zadanie pobierające obiekty WFS ze stronicowaniem i parsowaniem GML.

    Zamiast przekazywać magiczny string 'data_type' do wyboru klienta,
    można przekazać gotową instancję BaseWFSClient. Dla wstecznej
    kompatybilności obsługiwany jest też stary parametr data_type.
    """
    downloadFinished = pyqtSignal(list)
    progressValue = pyqtSignal(float)

    def __init__(self, filter_xml: str, total_expected: int = 0,
                 attributes=None, data_type: str = "dzialki (EGIB)",
                 client: BaseWFSClient = None):
        super().__init__("Pobieranie danych EGiB...", QgsTask.Flag.CanCancel)
        self.filter_xml = filter_xml
        self.total_expected = total_expected
        self.attributes = attributes
        self.features_data = []
        self.exception = None
        self.stopped = False

        # Preferujemy jawnie przekazany klient; fallback na data_type
        if client is not None:
            self.client = client
        elif data_type == "budynki (EGIB)":
            self.client = EGIBClientBudynki()
        elif "(RCN)" in data_type:
            self.client = RCNClient(obj_layer=data_type.split(" ")[0])
        elif "adresy (PRG)" in data_type or "ulice (PRG)" in data_type or "place (PRG)" in data_type:
            self.client = PRGClientAdresy(obj_layer=data_type.split(" ")[0])
        elif "(PRG_" in data_type:
            self.client = PRGClientAdmin(obj_layer=data_type.split(" ")[0])
        elif "(PRNG)" in data_type:
            self.client = PRNGClient(obj_layer=data_type.split(" ")[0])
        else:
            self.client = WFSClient()

    def run(self) -> bool:
        start_index = 0
        count = 1000  # Rozmiar strony

        while not self.stopped:
            try:
                if self.isCanceled():
                    return False

                if self.total_expected > 0:
                    prog = int((start_index / self.total_expected) * 100)
                    self.progressValue.emit(prog)
                    self.setProgress(prog)

                gml_content = self.client.download(
                    self.filter_xml, start_index, count,
                    attributes=self.attributes,
                )
                new_features = self._parse_gml(gml_content)
                self.features_data.extend(new_features)

                if len(new_features) < count:
                    break

                start_index += count

            except Exception as e:
                self.exception = e
                return False

        self.setProgress(100)
        self.progressValue.emit(100)
        return True

    def finished(self, result: bool):
        self.downloadFinished.emit(self.features_data if result else [])

    # ------------------------------------------------------------------ Parsowanie

    def _manual_parse_geometry(self, gml_element):
        """
        Ręczny parser GML dla geometrii, których QgsOgcUtils nie obsługuje.
        Obsługuje Point oraz Polygon z exterior/interior.

        Uwaga: EGiB zwraca współrzędne w kolejności Y X (North-first),
        dlatego zamieniamy kolejność przy tworzeniu QgsPointXY(X, Y).
        """
        try:
            local_name = gml_element.localName()

            # --- Punkt ---
            if local_name == "Point":
                child = gml_element.firstChild()
                while not child.isNull():
                    if child.toElement().localName() in ("pos", "coordinates"):
                        coords = child.toElement().text().strip().replace(",", " ").split()
                        if len(coords) >= 2:
                            # coords[0]=Y (North), coords[1]=X (East)
                            return QgsGeometry.fromPointXY(
                                QgsPointXY(float(coords[1]), float(coords[0]))
                            )
                        break
                    child = child.nextSibling()
                return None

            # --- MULTIPOINT ---
            if local_name == "MultiPoint":
                points = []

                child = gml_element.firstChild()
                while not child.isNull():
                    elem = child.toElement()

                    if elem.localName() == "pointMember":
                        point_node = elem.firstChild()

                        while not point_node.isNull():
                            if point_node.toElement().localName() == "Point":
                                geom = self._manual_parse_geometry(point_node.toElement())
                                if geom:
                                    points.append(geom.asPoint())
                                break
                            point_node = point_node.nextSibling()

                    child = child.nextSibling()

                if points:
                    return QgsGeometry.fromMultiPointXY(points)

            # --- LINESTRING / MULTILINE / MULTICURVE ---
            if local_name in ("LineString", "Curve"):
                pos_list = None
                child = gml_element.firstChild()
                while not child.isNull():
                    if child.toElement().localName() == "posList":
                        pos_list = child.toElement()
                        break
                    child = child.nextSibling()

                if pos_list:
                    dim = int(pos_list.attribute("srsDimension", "2"))
                    coords = pos_list.text().strip().split()

                    points = []
                    for i in range(0, len(coords), dim):
                        if i + 1 < len(coords):
                            points.append(
                                QgsPointXY(float(coords[i + 1]), float(coords[i]))
                            )

                    if points:
                        return QgsGeometry.fromPolylineXY(points)


            # --- MULTICURVE ---
            if local_name == "MultiCurve":
                lines = []

                child = gml_element.firstChild()
                while not child.isNull():
                    elem = child.toElement()

                    if elem.localName() == "curveMember":
                        curve = elem.firstChild()

                        while not curve.isNull():
                            if curve.toElement().localName() in ("LineString", "Curve"):
                                geom = self._manual_parse_geometry(curve.toElement())
                                if geom:
                                    lines.append(geom.asPolyline())
                                break
                            curve = curve.nextSibling()

                    child = child.nextSibling()

                if lines:
                    return QgsGeometry.fromMultiPolylineXY(lines)



            # --- Poligon ---
            def extract_ring_points(parent_node):
                """Wyciąga listę punktów z węzła exterior/interior."""
                # Szukamy LinearRing
                ring = None
                c = parent_node.firstChild()
                while not c.isNull():
                    if c.toElement().localName() == "LinearRing":
                        ring = c.toElement()
                        break
                    c = c.nextSibling()
                if ring is None:
                    return None

                # Szukamy posList wewnątrz LinearRing
                pos_list = None
                c = ring.firstChild()
                while not c.isNull():
                    if c.toElement().localName() == "posList":
                        pos_list = c.toElement()
                        break
                    c = c.nextSibling()
                if pos_list is None:
                    return None

                dim = int(pos_list.attribute("srsDimension", "2"))
                coords = pos_list.text().strip().split()
                points = []
                for i in range(0, len(coords), dim):
                    if i + 1 < len(coords):
                        # coords[i]=Y (North), coords[i+1]=X (East)
                        points.append(
                            QgsPointXY(float(coords[i + 1]), float(coords[i]))
                        )
                return points
            
            if local_name == "Polygon":
                all_rings = []
                child = gml_element.firstChild()
                while not child.isNull():
                    elem = child.toElement()
                    if elem.localName() in ("exterior", "interior"):
                        pts = extract_ring_points(elem)
                        if pts:
                            all_rings.append(pts)
                    child = child.nextSibling()
                if all_rings:
                    return QgsGeometry.fromPolygonXY(all_rings)
                return None

            if local_name in ("MultiSurface", "MultiPolygon"):
                polygons = []

                child = gml_element.firstChild()
                while not child.isNull():
                    elem = child.toElement()

                    # GML 3.x: surfaceMember, GML 2.x: polygonMember
                    if elem.localName() in ("surfaceMember", "polygonMember"):
                        poly_node = elem.firstChild()

                        while not poly_node.isNull():
                            if poly_node.toElement().localName() == "Polygon":
                                geom = self._manual_parse_geometry(poly_node.toElement())
                                if geom:
                                    polygons.append(geom.asPolygon())
                                break
                            poly_node = poly_node.nextSibling()

                    child = child.nextSibling()

                if polygons:
                    return QgsGeometry.fromMultiPolygonXY(polygons)
                return None

        except Exception as e:
            QgsMessageLog.logMessage(
                f"[DownloadTask] Ręczne parsowanie geometrii: {e}",
                LOG_TAG, Qgis.MessageLevel.Warning,
            )
        return None

    def _parse_gml(self, gml_content: str) -> list:
        """
        Parsuje odpowiedź GML i zwraca listę słowników
        {'geom': wkt_str, 'attrs': {name: value, ...}}.
        """
        features = []
        try:
            doc = QDomDocument()
            if not doc.setContent(gml_content, True):
                QgsMessageLog.logMessage(
                    "[DownloadTask] Błąd parsowania XML",
                    LOG_TAG, Qgis.MessageLevel.Warning,
                )
                return []

            root = doc.documentElement()
            members = root.elementsByTagNameNS(
                "http://www.opengis.net/wfs/2.0", "member"
            )
            if members.count() == 0:
                members = root.elementsByTagName("wfs:member")

            for i in range(members.count()):
                member_node = members.item(i)

                # Znajdź element cechy (pierwszy element-dziecko member)
                feature_elem = None
                child = member_node.firstChild()
                while not child.isNull():
                    if child.nodeType() == QDomNode.NodeType.ElementNode:
                        feature_elem = child.toElement()
                        break
                    child = child.nextSibling()
                if feature_elem is None:
                    continue

                attrs = {}
                geom_wkt = None

                prop = feature_elem.firstChild()
                while not prop.isNull():
                    if prop.nodeType() == QDomNode.NodeType.ElementNode:
                        elem = prop.toElement()
                        name = elem.localName() or elem.tagName().split(":")[-1]

                        if name in ("geom", "msGeometry", "geometry"):
                            geom_child = elem.firstChild()
                            while not geom_child.isNull():
                                if geom_child.nodeType() == QDomNode.NodeType.ElementNode:
                                    g_elem = geom_child.toElement()
                                    try:
                                        ggeom = QgsOgcUtils.geometryFromGML(g_elem)
                                        if ggeom and not ggeom.isEmpty():
                                            geom_wkt = ggeom.asWkt()
                                        else:
                                            # Fallback: ręczny parser
                                            ggeom_manual = self._manual_parse_geometry(g_elem)
                                            if ggeom_manual and not ggeom_manual.isEmpty():
                                                geom_wkt = ggeom_manual.asWkt()
                                    except Exception as e:
                                        QgsMessageLog.logMessage(
                                            f"[DownloadTask] geometryFromGML: {e}",
                                            LOG_TAG, Qgis.MessageLevel.Warning,
                                        )
                                    break
                                geom_child = geom_child.nextSibling()
                        else:
                            attrs[name] = elem.text()

                    prop = prop.nextSibling()

                if geom_wkt:
                    features.append({"geom": geom_wkt, "attrs": attrs})

        except Exception as e:
            QgsMessageLog.logMessage(
                f"[DownloadTask] GML Parse Error: {e}",
                LOG_TAG, Qgis.MessageLevel.Warning,
            )

        return features

    def cancel(self):
        self.stopped = True
        super().cancel()