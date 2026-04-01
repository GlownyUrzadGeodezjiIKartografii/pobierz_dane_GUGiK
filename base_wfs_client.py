# -*- coding: utf-8 -*-
"""
Bazowy klient WFS dla usług GUGiK/GeoPortal.

Zastępuje duplikaty: egib_client_dzialki.py, egib_client_budynki.py, rcn_client.py.
Podklasy konfigurują tylko url / layer_name / id_field / geom_field.
"""
import urllib.parse
import time
import requests
from requests.adapters import HTTPAdapter
from xml.etree import ElementTree as ET
from qgis.core import QgsMessageLog, Qgis, QgsGeometry, QgsWkbTypes

try:
    from urllib3.util.retry import Retry
    from urllib3.exceptions import RemoteDisconnected
except ImportError:
    from requests.packages.urllib3.util.retry import Retry
    RemoteDisconnected = Exception

LOG_TAG = "PD_GUGiK"
MAX_RETRIES = 3


def _make_session() -> requests.Session:
    """Tworzy sesję requests ze strategią retry i domyślnymi nagłówkami."""
    session = requests.Session()
    try:
        retry = Retry(
            total=MAX_RETRIES,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
    except TypeError:
        # Starsze wersje urllib3
        retry = Retry(
            total=MAX_RETRIES,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            method_whitelist=["GET", "POST"],
        )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "QGIS-PobieranieGUGiK/1.0",
        "Content-Type": "application/xml",
        "Connection": "close",
    })
    return session


class BaseWFSClient:
    """
    Bazowy klient WFS dla usług GUGiK.

    Podklasy ustawiają atrybuty klasy:
        url        – adres endpointu WFS
        layer_name – pełna nazwa typenames, np. "ms:dzialki"
        id_field   – nazwa pola identyfikatora obiektów, np. "id_dzialki"
        geom_field – nazwa pola geometrii (domyślnie "geom")
    """
    url: str = ""
    layer_name: str = ""
    id_field: str = ""
    geom_field: str = "geom"

    def __init__(self):
        self.session = _make_session()

    # ------------------------------------------------------------------ HTTP

    def _get(self, params: dict, timeout: int) -> requests.Response:
        """Wykonuje żądanie GET z retry i logowaniem każdej próby."""
        url = f"{self.url}?{urllib.parse.urlencode(params)}"
        QgsMessageLog.logMessage(
                    f"[WFS] url: "
                    f"{url}",
                    LOG_TAG, Qgis.MessageLevel.Info,
                )
        for attempt in range(MAX_RETRIES):
            try:
                t0 = time.time()
                resp = self.session.get(url, timeout=timeout)
                elapsed = time.time() - t0
                resp.raise_for_status()
                QgsMessageLog.logMessage(
                    f"[WFS] {self.layer_name} {resp.status_code} "
                    f"{elapsed:.1f}s {len(resp.content)}B (próba {attempt + 1}/{MAX_RETRIES})",
                    LOG_TAG, Qgis.MessageLevel.Info,
                )
                return resp
            except (requests.ConnectionError, requests.Timeout, RemoteDisconnected) as e:
                wait = (attempt + 1) * 2
                if attempt < MAX_RETRIES - 1:
                    QgsMessageLog.logMessage(
                        f"[WFS] {type(e).__name__} – ponawianie za {wait}s "
                        f"(próba {attempt + 1}/{MAX_RETRIES})",
                        LOG_TAG, Qgis.MessageLevel.Warning,
                    )
                    time.sleep(wait)
                else:
                    QgsMessageLog.logMessage(
                        f"[WFS] Błąd po {MAX_RETRIES} próbach: {e}",
                        LOG_TAG, Qgis.MessageLevel.Critical,
                    )
                    raise
            except Exception as e:
                QgsMessageLog.logMessage(
                    f"[WFS] Nieoczekiwany błąd: {e}", LOG_TAG, Qgis.MessageLevel.Critical
                )
                raise
        # Linia nieosiągalna, ale zadowala statyczne analizatory
        raise RuntimeError("Nieoczekiwany koniec pętli retry")

    # ------------------------------------------------------------------ API

    def get_hits(self, filter_xml: str) -> int:
        """
        Zwraca przybliżoną liczbę obiektów pasujących do filtra.
        Używa małego zapytania GetFeature i odczytuje numberMatched z nagłówka.
        Zwraca -1 gdy serwer odpowiada 'unknown'.
        """
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typenames": self.layer_name,
            "startIndex": "0",
            "count": "100",
            "filter": filter_xml.strip(),
        }
        resp = self._get(params, timeout=60)
        root = ET.fromstring(resp.content)
        matched = root.get("numberMatched", "unknown")
        if matched == "unknown":
            QgsMessageLog.logMessage(
                "[WFS] numberMatched=unknown", LOG_TAG, Qgis.MessageLevel.Warning
            )
            return -1
        result = int(matched)
        QgsMessageLog.logMessage(
            f"[WFS] get_hits={result}", LOG_TAG, Qgis.MessageLevel.Info
        )
        return result

    def download(self, filter_xml: str, start_index: int = 0,
                 count: int = 1000, attributes=None) -> str:
        """
        Pobiera jedną stronę wyników WFS.
        Zwraca surowy tekst odpowiedzi GML.
        """
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typenames": self.layer_name,
            "filter": filter_xml.strip(),
            "startIndex": start_index,
            "count": count,
        }
        if attributes:
            props = list(attributes)
            # Upewniamy się, że pole geometrii zawsze jest pobierane
            if self.geom_field not in props:
                props.append(self.geom_field)
            params["propertyName"] = ",".join(props)

        QgsMessageLog.logMessage(
            f"[WFS] download {self.layer_name} start={start_index} count={count}",
            LOG_TAG, Qgis.MessageLevel.Info,
        )
        return self._get(params, timeout=180).text

    # ------------------------------------------------------------------ Filtry

    def build_bbox_filter(self, xmin: float, ymin: float,
                          xmax: float, ymax: float) -> str:
        """Buduje filtr BBOX w EPSG:2180."""
        xmin, ymin = round(xmin, 2), round(ymin, 2)
        xmax, ymax = round(xmax, 2), round(ymax, 2)
        return (
            f'<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0"'
            f' xmlns:gml="http://www.opengis.net/gml/3.2">'
            f'<fes:BBOX>'
            f'<fes:ValueReference>{self.geom_field}</fes:ValueReference>'
            f'<gml:Envelope srsName="EPSG:2180">'
            f'<gml:lowerCorner>{ymin} {xmin}</gml:lowerCorner>'
            f'<gml:upperCorner>{ymax} {xmax}</gml:upperCorner>'
            f'</gml:Envelope>'
            f'</fes:BBOX>'
            f'</fes:Filter>'
        )

    def build_spatial_filter(self, wkt_polygon: str, use_bbox: bool = True) -> str:
        """
        Buduje filtr przestrzenny (BBOX lub dokładny Intersects).

        Uwaga dot. kolejności współrzędnych: EPSG:2180 ma oś N (Y) przed E (X),
        dlatego w posList piszemy '{pt.y()} {pt.x()}', a w Envelope
        lowerCorner='{ymin} {xmin}'.
        """
        geom = QgsGeometry.fromWkt(wkt_polygon)
        if geom is None or geom.isEmpty():
            raise ValueError(f"Nieprawidłowa geometria WKT: {wkt_polygon[:80]}")

        bbox = geom.boundingBox()

        # Zawsze używaj BBOX dla geometrii innych niż poligony
        if use_bbox or geom.wkbType() not in (
            QgsWkbTypes.Polygon, QgsWkbTypes.MultiPolygon
        ):
            return self.build_bbox_filter(
                bbox.xMinimum(), bbox.yMinimum(),
                bbox.xMaximum(), bbox.yMaximum(),
            )

        # Precyzyjny Intersects – zewnętrzny pierścień pierwszego poligonu
        poly = geom.asPolygon()
        if not poly:
            polys = geom.asMultiPolygon()
            poly = polys[0] if polys else None
        if not poly:
            raise ValueError("Nie można wyciągnąć zewnętrznego pierścienia poligonu.")

        exterior = poly[0]
        coords = [f"{pt.y()} {pt.x()}" for pt in exterior]
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        pos_list = " ".join(coords)

        return (
            f'<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0"'
            f' xmlns:gml="http://www.opengis.net/gml/3.2">'
            f'<fes:Intersects>'
            f'<fes:ValueReference>{self.geom_field}</fes:ValueReference>'
            f'<gml:Polygon gml:id="polygon_filter" srsName="EPSG:2180">'
            f'<gml:exterior><gml:LinearRing>'
            f'<gml:posList srsDimension="2">{pos_list}</gml:posList>'
            f'</gml:LinearRing></gml:exterior>'
            f'</gml:Polygon>'
            f'</fes:Intersects>'
            f'</fes:Filter>'
        )

    def build_id_filter(self, ids: list) -> str:
        """Buduje filtr równości/like dla listy identyfikatorów."""
        if not ids:
            return ""
        conditions = []
        for ident in ids:
            ident = ident.strip()
            if not ident:
                continue
            if "*" in ident or "?" in ident:
                conditions.append(
                    f'<fes:PropertyIsLike wildCard="*" singleChar="?" escape="\\">'
                    f'<fes:ValueReference>{self.id_field}</fes:ValueReference>'
                    f'<fes:Literal>{ident}</fes:Literal>'
                    f'</fes:PropertyIsLike>'
                )
            else:
                conditions.append(
                    f'<fes:PropertyIsEqualTo>'
                    f'<fes:ValueReference>{self.id_field}</fes:ValueReference>'
                    f'<fes:Literal>{ident}</fes:Literal>'
                    f'</fes:PropertyIsEqualTo>'
                )
        if not conditions:
            return ""
        inner = (
            conditions[0]
            if len(conditions) == 1
            else "<fes:Or>" + "".join(conditions) + "</fes:Or>"
        )
        return (
            f'<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0">'
            f'{inner}'
            f'</fes:Filter>'
        )

    def combine_filters(self, filter_list: list) -> str:
        """
        Łączy listę filtrów operatorem AND.
        Parsuje XML zamiast operować na stringach, co jest odporne
        na różne warianty deklaracji namespace.
        """
        inner_parts = []
        for f in filter_list:
            if not f:
                continue
            try:
                root = ET.fromstring(f)
                # Serializujemy wszystkie bezpośrednie dzieci tagu <fes:Filter>
                inner_parts.append(
                    "".join(ET.tostring(child, encoding="unicode") for child in root)
                )
            except ET.ParseError as e:
                QgsMessageLog.logMessage(
                    f"[WFS] combine_filters – błąd parsowania XML: {e}",
                    LOG_TAG, Qgis.MessageLevel.Warning,
                )
                inner_parts.append(f)  # Fallback: przekaż as-is

        if not inner_parts:
            return ""
        inner = (
            inner_parts[0]
            if len(inner_parts) == 1
            else "<fes:And>" + "".join(inner_parts) + "</fes:And>"
        )
        return (
            f'<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0"'
            f' xmlns:gml="http://www.opengis.net/gml/3.2">'
            f'{inner}'
            f'</fes:Filter>'
        )

    def build_attribute_filter(self, attribute: str, value: str,
                               like: bool = False) -> str:
        """Buduje filtr atrybutowy (równość lub like z '*' na końcu)."""
        # Usuwa prefiks namespace, jeśli jest obecny
        clean = attribute.split(":")[-1]
        if like:
            return (
                f'<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0">'
                f'<fes:PropertyIsLike wildCard="*" singleChar="?" escape="\\">'
                f'<fes:ValueReference>{clean}</fes:ValueReference>'
                f'<fes:Literal>{value}*</fes:Literal>'
                f'</fes:PropertyIsLike>'
                f'</fes:Filter>'
            )
        return (
            f'<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0">'
            f'<fes:PropertyIsEqualTo>'
            f'<fes:ValueReference>{clean}</fes:ValueReference>'
            f'<fes:Literal>{value}</fes:Literal>'
            f'</fes:PropertyIsEqualTo>'
            f'</fes:Filter>'
        )


# ------------------------------------------------------------------ Konkretne klasy

class WFSClient(BaseWFSClient):
    """Klient dla warstwy działek EGIB (ms:dzialki)."""
    url = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/EGIB/WFS/UslugaZbiorcza"
    layer_name = "ms:dzialki"
    id_field = "id_dzialki"


class EGIBClientBudynki(BaseWFSClient):
    """Klient dla warstwy budynków EGIB (ms:budynki)."""
    url = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/EGIB/WFS/UslugaZbiorcza"
    layer_name = "ms:budynki"
    id_field = "id_budynku"


class RCNClient(BaseWFSClient):
    """
    Klient dla usługi RCN.
    Nazwa warstwy jest dynamiczna – przekazywana przez parametr obj_layer.
    """
    url = "https://mapy.geoportal.gov.pl/wss/service/rcn"
    id_field = "dzi_id_dzialki"

    def __init__(self, obj_layer: str = "dzialki"):
        super().__init__()
        self.layer_name = f"ms:{obj_layer}"

#############################################################################
# Nie gotowe 
#############################################################################

class PRGClientAdresy(BaseWFSClient):
    """Klient dla usługi PRG - Adresy i Ulice."""
    url = "https://mapy.geoportal.gov.pl/wss/ext/KrajowaIntegracjaNumeracjiAdresowej"

    id_field = "simc"
    def __init__(self, obj_layer: str = "place"):
        super().__init__()
        self.layer_name = f"ms:prg-{obj_layer}"

class PRGClientAdmin(BaseWFSClient):
    """Klient dla usługi PRG - Granice administracyjne."""
    url = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/PRG/WFS/AdministrativeBoundaries"

    id_field = "IIP_IDENTY"
    def __init__(self, obj_layer: str = "A06_Granice_obrebow_ewidencyjnych"):
        super().__init__()
        self.layer_name = f"ms:{obj_layer}"

class PRNGClient(BaseWFSClient):
    """Klient dla usługi PRNG."""
    url = "https://mapy.geoportal.gov.pl/wss/service/PZGiK/PRNG/WFS/GeographicalNames"

    id_field = "IDIIP"
    geom_field = "msGeometry"
    def __init__(self, obj_layer: str = "O1_UrzedoweNazwyObiektowFizjograficznych"):
        super().__init__()
        self.layer_name = f"ms:{obj_layer}"