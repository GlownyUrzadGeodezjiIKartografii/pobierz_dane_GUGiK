import requests
from xml.etree import ElementTree as ET
import time
from requests.adapters import HTTPAdapter
from qgis.core import QgsMessageLog, Qgis, QgsGeometry, QgsCoordinateReferenceSystem, QgsWkbTypes

try:
    from urllib3.util.retry import Retry
    from urllib3.exceptions import RemoteDisconnected
except ImportError:
    from requests.packages.urllib3.util.retry import Retry
    RemoteDisconnected = Exception

class EGIBClientBudynki:
    def __init__(self, url="https://mapy.geoportal.gov.pl/wss/service/PZGIK/EGIB/WFS/UslugaZbiorcza", obj_layer="dzialki"):
        self.url = url
        self.ns = {
            'wfs': 'http://www.opengis.net/wfs/2.0',
            'fes': 'http://www.opengis.net/fes/2.0',
            'gml': 'http://www.opengis.net/gml/3.2',
            'ms': 'http://mapserver.gis.umn.edu/mapserver'
        }
        self.session = requests.Session()
        
        try:
            retry_strategy = Retry(
                total=3,
                backoff_factor=1,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["POST", "GET"]
            )
        except TypeError:
            retry_strategy = Retry(
                total=3,
                backoff_factor=1,
                status_forcelist=[429, 500, 502, 503, 504],
                method_whitelist=["POST", "GET"]
            )

        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=10)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Content-Type': 'application/xml',
            'Connection': 'close'
        })

    def get_hits(self, filter_xml):
        """
        Check how many features match the filter.
        NOTE: resultType=hits is unreliable in practice.
        Instead, perform a small GetFeature request and read numberMatched from response.
        Returns integer count (estimate from first page).
        """
        import urllib.parse

        params = {
            'service': 'WFS',
            'version': '2.0.0',
            'request': 'GetFeature',
            'typenames': 'ms:budynki',
            'startIndex': '0',
            'count': '100',
            'filter': filter_xml.strip()
        }

        url = f"{self.url}?{urllib.parse.urlencode(params)}"
        max_retries = 3

        QgsMessageLog.logMessage(
            f"[WFS] get_hits request (attempt 1/{max_retries})\nURL: {url[:500]}...",
            "PobieranieEGIB", Qgis.Info
        )

        for attempt in range(max_retries):
            try:
                start_time = time.time()
                response = self.session.get(url, timeout=60)
                elapsed_time = time.time() - start_time
                response.raise_for_status()

                QgsMessageLog.logMessage(
                    f"[WFS] get_hits response: Status {response.status_code}, "
                    f"Time: {elapsed_time:.2f}s, Size: {len(response.content)} bytes",
                    "PobieranieEGIB", Qgis.Info
                )

                root = ET.fromstring(response.content)
                matched = root.get('numberMatched')

                if matched == 'unknown':
                    # Fallback: check if metadata exists
                    QgsMessageLog.logMessage("[WFS] get_hits result: unknown", "PobieranieEGIB", Qgis.Warning)
                    return -1

                result = int(matched)
                QgsMessageLog.logMessage(f"[WFS] get_hits result (estimate from first page): {result} features", "PobieranieEGIB", Qgis.Info)
                return result
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    RemoteDisconnected) as e:
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2
                    QgsMessageLog.logMessage(
                        f"[WFS] get_hits error (attempt {attempt + 1}/{max_retries}): {type(e).__name__}. Retrying in {wait_time}s...",
                        "PobieranieEGIB", Qgis.Warning
                    )
                    time.sleep(wait_time)
                    continue
                else:
                    QgsMessageLog.logMessage(
                        f"[WFS] get_hits error after {max_retries} attempts: {e}",
                        "PobieranieEGIB", Qgis.Critical
                    )
                    raise
            except Exception as e:
                QgsMessageLog.logMessage(f"[WFS] get_hits error: {e}", "PobieranieEGIB", Qgis.Critical)
                raise

    def download(self, filter_xml, start_index=0, count=1000, attributes=None):
        """
        Download a page of results.
        Returns raw response text (GML).
        """
        import urllib.parse
        
        params = {
            'service': 'WFS',
            'version': '2.0.0',
            'request': 'GetFeature',
            'typenames': 'ms:budynki',
            'filter': filter_xml.strip(),
            'startIndex': start_index,
            'count': count
        }
        
        if attributes:
            params['propertyName'] = ",".join(attributes)
            if 'geom' not in attributes:
                params['propertyName'] += ",geom"
        
        url = f"{self.url}?{urllib.parse.urlencode(params)}"
        max_retries = 3
        
        QgsMessageLog.logMessage(
            f"[WFS] download request: start={start_index}, count={count}\nURL: {url[:500]}...", 
            "PobieranieEGIB", Qgis.Info
        )
        
        for attempt in range(max_retries):
            try:
                start_time = time.time()
                response = self.session.get(url, timeout=180)
                elapsed_time = time.time() - start_time
                response.raise_for_status()
                
                QgsMessageLog.logMessage(
                    f"[WFS] download response (attempt {attempt + 1}/{max_retries}): "
                    f"Status {response.status_code}, Time: {elapsed_time:.2f}s, Size: {len(response.content)} bytes", 
                    "PobieranieEGIB", Qgis.Info
                )
                
                return response.text
            except (requests.exceptions.ConnectionError, 
                    requests.exceptions.Timeout,
                    RemoteDisconnected) as e:
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2
                    QgsMessageLog.logMessage(
                        f"[WFS] download error (attempt {attempt + 1}/{max_retries}): {type(e).__name__}. "
                        f"start={start_index}, Retrying in {wait_time}s...", 
                        "PobieranieEGIB", Qgis.Warning
                    )
                    time.sleep(wait_time)
                    continue
                else:
                    QgsMessageLog.logMessage(
                        f"[WFS] download error after {max_retries} attempts: {e}", 
                        "PobieranieEGIB", Qgis.Critical
                    )
                    raise
            except Exception as e:
                QgsMessageLog.logMessage(f"[WFS] download error: {e}", "PobieranieEGIB", Qgis.Critical)
                raise

    def _build_get_feature_payload(self, filter_xml, result_type=None, start_index=0, count=1000, attributes=None):
        """
        Constructs the full GetFeature XML request.
        """
        attr_result_type = f'resultType="{result_type}"' if result_type else ''
        attr_paging = f'startIndex="{start_index}" count="{count}"' if not result_type else ''
        
        property_names = ""
        if attributes:
            for attr in attributes:
                property_names += f"        <wfs:PropertyName>{attr}</wfs:PropertyName>\n"
            if 'geom' not in attributes:
                property_names += "        <wfs:PropertyName>geom</wfs:PropertyName>\n"
        
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<wfs:GetFeature service="WFS" version="2.0.0" 
    xmlns:wfs="http://www.opengis.net/wfs/2.0"
    xmlns:fes="http://www.opengis.net/fes/2.0"
    xmlns:gml="http://www.opengis.net/gml/3.2"
    xmlns:ms="http://mapserver.gis.umn.edu/mapserver"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="http://www.opengis.net/wfs/2.0 http://schemas.opengis.net/wfs/2.0/wfs.xsd"
    {attr_result_type} {attr_paging}>
    <wfs:Query typeNames="ms:budynki" srsName="EPSG:2180">
{property_names}        {filter_xml}
    </wfs:Query>
</wfs:GetFeature>"""
        
        return xml.strip()

    def build_spatial_filter(self, wkt_polygon, use_bbox=True):
        """
        Builds a spatial filter for WFS.
        """
        try:
            geom = QgsGeometry.fromWkt(wkt_polygon)
            if geom is None or geom.isEmpty():
                raise ValueError(f"Invalid WKT geometry: {wkt_polygon}")

            if use_bbox:
                bbox = geom.boundingBox()
                xmin, ymin, xmax, ymax = bbox.xMinimum(), bbox.yMinimum(), bbox.xMaximum(), bbox.yMaximum()
                xmin, ymin, xmax, ymax = round(xmin, 2), round(ymin, 2), round(xmax, 2), round(ymax, 2)
                filter_xml = f'<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0"><fes:BBOX><fes:ValueReference>geom</fes:ValueReference><gml:Envelope srsName="EPSG:2180"><gml:lowerCorner>{ymin} {xmin}</gml:lowerCorner><gml:upperCorner>{ymax} {xmax}</gml:upperCorner></gml:Envelope></fes:BBOX></fes:Filter>'
            else:
                if geom.wkbType() in [QgsWkbTypes.Polygon, QgsWkbTypes.MultiPolygon]:
                    # MultiPolygon support
                    if geom.isMultipart():
                        # For WFS 2.0.0 Intersects, we might need to handle each part or use MultiPolygon GML.
                        # Simple approach: use the first polygon for Intersects if too complex, or rely on BBOX.
                        # Actually GUGiK supports MultiPolygon in GML.
                        pass
                    
                    # Simpler fallback: for precise spatial, we take the exterior ring of the first part.
                    poly = geom.asPolygon()
                    if not poly:
                        # Maybe MultiPolygon
                        polys = geom.asMultiPolygon()
                        if polys:
                            poly = polys[0]
                    
                    if poly:
                        exterior = poly[0]
                        # coords = [f"{pt.x()} {pt.y()}" for pt in exterior]
                        coords = [f"{pt.y()} {pt.x()}" for pt in exterior]
                        if coords[0] != coords[-1]:
                            coords.append(coords[0])
                        pos_list = " ".join(coords)
                        filter_xml = f'''<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0" xmlns:gml="http://www.opengis.net/gml/3.2">
<fes:Intersects>
<fes:ValueReference>geom</fes:ValueReference>
<gml:Polygon gml:id="polygon_filter" srsName="EPSG:2180">
<gml:exterior>
<gml:LinearRing>
<gml:posList srsDimension="2">{pos_list}</gml:posList>
</gml:LinearRing>
</gml:exterior>
</gml:Polygon>
</fes:Intersects>
</fes:Filter>'''
                    else:
                        raise ValueError("Could not extract polygon exterior ring")
                else:
                    bbox = geom.boundingBox()
                    xmin, ymin, xmax, ymax = bbox.xMinimum(), bbox.yMinimum(), bbox.xMaximum(), bbox.yMaximum()
                    xmin, ymin, xmax, ymax = round(xmin, 2), round(ymin, 2), round(xmax, 2), round(ymax, 2)
                    filter_xml = f'<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0"><fes:BBOX><fes:ValueReference>geom</fes:ValueReference><gml:Envelope srsName="EPSG:2180"><gml:lowerCorner>{ymin} {xmin}</gml:lowerCorner><gml:upperCorner>{ymax} {xmax}</gml:upperCorner></gml:Envelope></fes:BBOX></fes:Filter>'

            return filter_xml
        except Exception as e:
            QgsMessageLog.logMessage(f"[WFS] Error creating spatial filter: {e}", "PobieranieEGIB", Qgis.Warning)
            raise ValueError(f"Invalid WKT for spatial filter: {e}")
        
    def build_spatial_filter2(self, wkt_polygon, use_bbox=True):
            """
            Builds a spatial filter for WFS.
            """
            try:
                geom = QgsGeometry.fromWkt(wkt_polygon)
                if geom is None or geom.isEmpty():
                    raise ValueError(f"Invalid WKT geometry: {wkt_polygon}")

                if use_bbox:
                    bbox = geom.boundingBox()
                    xmin, ymin, xmax, ymax = bbox.xMinimum(), bbox.yMinimum(), bbox.xMaximum(), bbox.yMaximum()
                    xmin, ymin, xmax, ymax = round(xmin, 2), round(ymin, 2), round(xmax, 2), round(ymax, 2)
                    filter_xml = f'<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0"><fes:BBOX><fes:ValueReference>geom</fes:ValueReference><gml:Envelope srsName="EPSG:2180"><gml:lowerCorner>{ymin} {xmin}</gml:lowerCorner><gml:upperCorner>{ymax} {xmax}</gml:upperCorner></gml:Envelope></fes:BBOX></fes:Filter>'
                else:
                    if geom.wkbType() in [QgsWkbTypes.Polygon, QgsWkbTypes.MultiPolygon]:
                        # MultiPolygon support
                        if geom.isMultipart():
                            # For WFS 2.0.0 Intersects, we might need to handle each part or use MultiPolygon GML.
                            # Simple approach: use the first polygon for Intersects if too complex, or rely on BBOX.
                            # Actually GUGiK supports MultiPolygon in GML.
                            pass
                        
                        # Simpler fallback: for precise spatial, we take the exterior ring of the first part.
                        poly = geom.asPolygon()
                        if not poly:
                            # Maybe MultiPolygon
                            polys = geom.asMultiPolygon()
                            if polys:
                                poly = polys[0]
                        
                        if poly:
                            exterior = poly[0]
                            # coords = [f"{pt.x()} {pt.y()}" for pt in exterior]
                            coords = [f"{pt.y()} {pt.x()}" for pt in exterior]
                            if coords[0] != coords[-1]:
                                coords.append(coords[0])
                            pos_list = " ".join(coords)
                            filter_xml = f'''<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0" xmlns:gml="http://www.opengis.net/gml/3.2">
    <fes:Intersects>
    <fes:ValueReference>geom</fes:ValueReference>
    <gml:Polygon gml:id="polygon_filter" srsName="EPSG:2180">
    <gml:exterior>
    <gml:LinearRing>
    <gml:posList srsDimension="2">{pos_list}</gml:posList>
    </gml:LinearRing>
    </gml:exterior>
    </gml:Polygon>
    </fes:Intersects>
    </fes:Filter>'''
                        else:
                            raise ValueError("Could not extract polygon exterior ring")
                    else:
                        bbox = geom.boundingBox()
                        xmin, ymin, xmax, ymax = bbox.xMinimum(), bbox.yMinimum(), bbox.xMaximum(), bbox.yMaximum()
                        xmin, ymin, xmax, ymax = round(xmin, 2), round(ymin, 2), round(xmax, 2), round(ymax, 2)
                        filter_xml = f'<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0"><fes:BBOX><fes:ValueReference>geom</fes:ValueReference><gml:Envelope srsName="EPSG:2180"><gml:lowerCorner>{ymin} {xmin}</gml:lowerCorner><gml:upperCorner>{ymax} {xmax}</gml:upperCorner></gml:Envelope></fes:BBOX></fes:Filter>'

                return filter_xml
            except Exception as e:
                QgsMessageLog.logMessage(f"[WFS] Error creating spatial filter: {e}", "PobieranieEGIB", Qgis.Warning)
                raise ValueError(f"Invalid WKT for spatial filter: {e}")

    def build_id_filter(self, ids):
        """
        Builds a filter for ID list.
        """
        if not ids:
            return ""

        conditions = []
        for identifier in ids:
            identifier = identifier.strip()
            if not identifier: continue
            if '*' in identifier or '?' in identifier:
                 conditions.append(f'<fes:PropertyIsLike wildCard="*" singleChar="?" escape="\\"><fes:ValueReference>id_budynku</fes:ValueReference><fes:Literal>{identifier}</fes:Literal></fes:PropertyIsLike>')
            else:
                conditions.append(f'<fes:PropertyIsEqualTo><fes:ValueReference>id_budynku</fes:ValueReference><fes:Literal>{identifier}</fes:Literal></fes:PropertyIsEqualTo>')

        if len(conditions) == 1:
            inner = conditions[0]
        else:
            inner = "<fes:Or>" + "".join(conditions) + "</fes:Or>"

        return f'<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0">{inner}</fes:Filter>'

    def combine_filters(self, filter_list):
        """
        Combines multiple filter strings into one <fes:Filter> with <fes:And>.
        Each input filter filter must be a full <fes:Filter>...</fes:Filter> block 
        (the tags will be stripped) or just the inner part.
        """
        inner_conditions = []
        for f in filter_list:
            if not f: continue
            # Strip <fes:Filter> tags if present
            content = f.replace('<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0">', '')
            content = content.replace('<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0" xmlns:gml="http://www.opengis.net/gml/3.2">', '')
            content = content.replace('</fes:Filter>', '')
            inner_conditions.append(content.strip())
            
        if not inner_conditions:
            return ""
        if len(inner_conditions) == 1:
            inner = inner_conditions[0]
        else:
            inner = "<fes:And>" + "".join(inner_conditions) + "</fes:And>"
            
        return f'<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0" xmlns:gml="http://www.opengis.net/gml/3.2">{inner}</fes:Filter>'

    def build_attribute_filter(self, attribute, value, like=False):
        clean_attribute = attribute.split(':')[-1] if ':' in attribute else attribute
        if like:
            val = f"{value}*"
            filter_xml = f'<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0"><fes:PropertyIsLike wildCard="*" singleChar="?" escape="\\"><fes:ValueReference>{clean_attribute}</fes:ValueReference><fes:Literal>{val}</fes:Literal></fes:PropertyIsLike></fes:Filter>'
        else:
            filter_xml = f'<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0"><fes:PropertyIsEqualTo><fes:ValueReference>{clean_attribute}</fes:ValueReference><fes:Literal>{value}</fes:Literal></fes:PropertyIsEqualTo></fes:Filter>'
        return filter_xml