# -*- coding: utf-8 -*-
"""
Zadanie QgsTask pobierające dane z powiatowych usług WFS przez eziudp.

Zmiany:
  - Przyjmuje listę kluczy warstw (layer_keys) zamiast jednego zbioru
  - OGR fallback paginuje tak samo jak główna ścieżka
  - clip_geom_wkt do lokalnego przycinania wyników
  - STREAMING: każda strona paginacji jest natychmiast harmonizowana
    i wczytywana do warstwy QGIS – brak akumulacji GML/features w pamięci
"""
import re
import os
import tempfile
from dataclasses import dataclass, field
from typing import Optional, Iterator
from xml.etree import ElementTree as ET

from qgis.core import (
    QgsTask, QgsMessageLog, Qgis,
    QgsCoordinateReferenceSystem, QgsGeometry, QgsVectorLayer,
)
from qgis.PyQt.QtCore import pyqtSignal

from .base_wfs_client import BaseWFSClient
from .download_task import DownloadTask
from .eziudp_client import EziudpClient, ZBIOR_CONFIG
from .bdsog_harmonizer import get_harmonizer

LOG_TAG = "PD_GUGiK"

_POLISH_CRS = {"2176", "2177", "2178", "2179", "2180"}


# ---------------------------------------------------------------------------
# Definicja dostępnych warstw do wyboru przez użytkownika
# ---------------------------------------------------------------------------

AVAILABLE_LAYERS = [
    {
        "key":           "bdsog_poziom",
        "label":         "Osnowa pozioma (BDSOG)",
        "zbior":         "bdsog",
        "layer_pattern": "poziom",
    },
    {
        "key":           "bdsog_wysok",
        "label":         "Osnowa wysokościowa (BDSOG)",
        "zbior":         "bdsog",
        "layer_pattern": "wysok",
    },
    {
        "key":           "egib_dzi",
        "label":         "Działki ewidencyjne (EGIB)",
        "zbior":         "egib",
        "layer_pattern": "dzialki",
    },
    {
        "key":           "egib_bud",
        "label":         "Budynki ewidencyjne (EGIB)",
        "zbior":         "egib",
        "layer_pattern": "budynki",
    },
    {
        "key":           "rcn_tran",
        "label":         "Lokalizacja transakcji (RCN)",
        "zbior":         "rcn",
        "layer_pattern": "transakcje",
    },
    {
        "key":           "Budynki i obiekty towarzyszące",
        "label":         "Lokalizacja transakcji (BDOT500)",
        "zbior":         "BDOT500",
        "layer_pattern": "budynki",
    },
]

# Szybki dostęp po kluczu
AVAILABLE_LAYERS_BY_KEY = {e["key"]: e for e in AVAILABLE_LAYERS}


# ---------------------------------------------------------------------------
# Raporty
# ---------------------------------------------------------------------------

@dataclass
class PowiatDownloadReport:
    teryt: str
    success: bool = False
    feature_count: int = 0
    wfs_url: str = ""
    layer_names: list = field(default_factory=list)
    detected_crs: str = "EPSG:2180"
    error: str = ""


@dataclass
class EziudpDownloadReport:
    zbior: str = ""
    total_teryts: int = 0
    successful: int = 0
    failed: int = 0
    total_features: int = 0
    powiaty: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def format_summary(self) -> str:
        lines = [
            f"Zbiór: {self.zbior}",
            f"Powiaty: {self.successful}/{self.total_teryts} OK, "
            f"{self.failed} błędów",
            f"Pobrane obiekty: {self.total_features}",
        ]
        if self.errors:
            lines.append("Błędy:")
            for e in self.errors[:10]:
                lines.append(f"  • {e}")
            if len(self.errors) > 10:
                lines.append(f"  … i {len(self.errors) - 10} więcej")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dynamiczny klient WFS – pomija pusty parametr filter
# ---------------------------------------------------------------------------

class _DynamicWFSClient(BaseWFSClient):
    def __init__(self, url: str, layer_name: str,
                 id_field: str = "", geom_field: str = "msGeometry"):
        super().__init__()
        self.url = url
        self.layer_name = layer_name
        self.id_field = id_field
        self.geom_field = geom_field

    def download(self, filter_xml: str, start_index: int = 0,
                 count: int = 1000, attributes=None) -> str:
        """Pomija parametr 'filter' gdy jest pusty – Mapserver zwraca wtedy pusty wynik."""
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typenames": self.layer_name,
            "startIndex": start_index,
            "count": count,
        }
        if filter_xml and filter_xml.strip():
            params["filter"] = filter_xml.strip()

        if attributes:
            props = list(attributes)
            if self.geom_field not in props:
                props.append(self.geom_field)
            params["propertyName"] = ",".join(props)

        QgsMessageLog.logMessage(
            f"[WFS] download {self.layer_name} start={start_index} count={count}",
            LOG_TAG, Qgis.MessageLevel.Info,
        )
        return self._get(params, timeout=180).text


# ---------------------------------------------------------------------------
# Funkcje pomocnicze
# ---------------------------------------------------------------------------

def _detect_crs_from_gml(gml: str) -> str:
    match = re.search(r'srsName=["\'][^"\']*?21(\d{2})["\']', gml)
    if match:
        code = "21" + match.group(1)
        if code in _POLISH_CRS:
            return f"EPSG:{code}"
    return "EPSG:2180"


def _number_matched_from_gml(gml: str) -> Optional[int]:
    try:
        first_tag = re.match(r'(<[^>]+>)', gml.strip(), re.DOTALL)
        if not first_tag:
            return None
        m = re.search(r'numberMatched=["\'](\w+)["\']', first_tag.group(1))
        if m:
            val = m.group(1)
            if val == "unknown":
                return None
            return int(val)
    except Exception:
        pass
    return None


def _is_error_response(gml: str, teryt: str, layer_name: str) -> bool:
    stripped = gml.strip()
    lower = stripped.lower()
    if "exceptionreport" in lower or "serviceexception" in lower:
        QgsMessageLog.logMessage(
            f"[EziudpTask] TERYT {teryt} '{layer_name}': ExceptionReport:\n{stripped[:300]}",
            LOG_TAG, Qgis.MessageLevel.Warning,
        )
        return True
    if lower.startswith("<!doctype") or lower.startswith("<html"):
        QgsMessageLog.logMessage(
            f"[EziudpTask] TERYT {teryt} '{layer_name}': odpowiedź HTML/PHP:\n{stripped[:300]}",
            LOG_TAG, Qgis.MessageLevel.Warning,
        )
        return True
    if len(stripped) < 50:
        QgsMessageLog.logMessage(
            f"[EziudpTask] TERYT {teryt} '{layer_name}': odpowiedź zbyt krótka: {stripped!r}",
            LOG_TAG, Qgis.MessageLevel.Warning,
        )
        return True
    return False


def _try_wfs11_fallback(client, filter_xml, count, start_index, teryt, layer_name):
    """WFS 1.1.0 fallback z paginacją przez maxFeatures + startIndex."""
    import urllib.parse as _up
    params = {
        "service": "WFS",
        "version": "1.1.0",
        "request": "GetFeature",
        "typename": client.layer_name,
        "maxFeatures": str(count),
        "startIndex": str(start_index),
    }
    if filter_xml and filter_xml.strip():
        params["filter"] = filter_xml.strip()

    url = f"{client.url.split('?')[0]}?{_up.urlencode(params)}"
    QgsMessageLog.logMessage(
        f"[EziudpTask] WFS 1.1.0 fallback: {url}",
        LOG_TAG, Qgis.MessageLevel.Info,
    )
    try:
        resp = client.session.get(url, timeout=180)
        resp.raise_for_status()
        gml = resp.text
        if _is_error_response(gml, teryt, layer_name):
            return None
        return gml
    except Exception as exc:
        QgsMessageLog.logMessage(
            f"[EziudpTask] WFS 1.1.0 fallback błąd: {exc}",
            LOG_TAG, Qgis.MessageLevel.Warning,
        )
        return None


def _parse_gml_via_ogr(gml: str, teryt: str, layer_name: str) -> list:
    """
    Parsuje GML przez OGR (plik tymczasowy).
    Używany gdy główny parser DOM zwraca 0 wyników na niepustej odpowiedzi.
    Zwraca listę {'geom': wkt, 'attrs': {...}}.
    """
    features = []
    tmp_path = None
    vl = None
    try:
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=".gml", mode="w", encoding="utf-8"
        ) as f:
            f.write(gml)
            tmp_path = f.name

        vl = QgsVectorLayer(tmp_path, "tmp_ogr", "ogr")
        if not vl.isValid():
            QgsMessageLog.logMessage(
                f"[EziudpTask] OGR fallback: QgsVectorLayer nieprawidłowa",
                LOG_TAG, Qgis.MessageLevel.Warning,
            )
            return []

        for feat in vl.getFeatures():
            geom = feat.geometry()
            wkt = geom.asWkt() if (geom and not geom.isEmpty()) else None
            if not wkt:
                continue
            attrs = {field.name(): feat.attribute(field.name()) for field in vl.fields()}
            features.append({"geom": wkt, "attrs": attrs})

    except Exception as exc:
        QgsMessageLog.logMessage(
            f"[EziudpTask] OGR fallback błąd: {exc}",
            LOG_TAG, Qgis.MessageLevel.Warning,
        )
    finally:
        if vl is not None:
            del vl
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    return features


# ---------------------------------------------------------------------------
# Główne zadanie QgsTask
# ---------------------------------------------------------------------------

class EziudpDownloadTask(QgsTask):
    """
    Pobiera dane z powiatowych WFS przez eziudp.

    Tryb STREAMING: każda strona paginacji jest harmonizowana i wczytywana
    do warstwy QGIS natychmiast po pobraniu. GML strony nie jest akumulowany
    w pamięci – zwolnienie następuje przed pobraniem kolejnej strony.

    :param layer_keys:     lista kluczy z AVAILABLE_LAYERS_BY_KEY
                           np. ["bdsog_poziom", "bdsog_wysok", "egib_dzi"]
    :param teryt_list:     lista 4-cyfrowych TERYT-ów powiatów
    :param bbox_wkt:       WKT do filtra WFS
    :param clip_geom_wkt:  WKT do lokalnego przycinania po pobraniu

    UWAGA: downloadFinished emituje pustą listę features – odbiorca powinien
    korzystać wyłącznie z raportu (EziudpDownloadReport) i warstw QGIS.
    """

    downloadFinished = pyqtSignal(list, object)
    progressMessage = pyqtSignal(str)
    progressChanged = pyqtSignal(float)

    def __init__(
        self,
        teryt_list: list,
        layer_keys: list,
        bbox_wkt: Optional[str] = None,
        clip_geom_wkt: Optional[str] = None,
        plugin_dir: str = "",
        load_style: bool = False,
    ):
        labels = [AVAILABLE_LAYERS_BY_KEY[k]["label"] for k in layer_keys
                  if k in AVAILABLE_LAYERS_BY_KEY]
        task_label = f"eziudp: {', '.join(labels)} ({len(teryt_list)} powiatów)"

        super().__init__(task_label, QgsTask.Flag.CanCancel)
        self.teryt_list = [str(t).strip() for t in teryt_list if str(t).strip()]
        self.layer_keys = layer_keys
        self.bbox_wkt = bbox_wkt
        self.clip_geom_wkt = clip_geom_wkt
        self.plugin_dir = plugin_dir
        self.load_style = load_style

        self._eziudp = EziudpClient()
        # Nie akumulujemy features – śledzimy tylko liczniki
        self._report = EziudpDownloadReport(
            zbior=", ".join(layer_keys),
            total_teryts=len(self.teryt_list),
        )
        self.exception: Optional[Exception] = None

    # ------------------------------------------------------------------
    # QgsTask
    # ------------------------------------------------------------------

    def run(self) -> bool:
        total = len(self.teryt_list)
        for i, teryt in enumerate(self.teryt_list):
            if self.isCanceled():
                return False

            self.setProgress(int((i / total) * 100))
            self.progressChanged.emit(int((i / total) * 100))
            msg = f"[{i + 1}/{total}] TERYT {teryt}"
            self.progressMessage.emit(msg)
            QgsMessageLog.logMessage(f"[EziudpTask] {msg}", LOG_TAG, Qgis.MessageLevel.Info)

            report = self._process_one_teryt(teryt)
            self._report.powiaty.append(report)
            if report.success:
                self._report.successful += 1
                self._report.total_features += report.feature_count
            else:
                self._report.failed += 1
                self._report.errors.append(f"TERYT {teryt}: {report.error}")

        self.setProgress(100)
        self.progressChanged.emit(100)
        return True

    def finished(self, result: bool):
        status = "Zakończono" if result else ("Anulowano" if self.isCanceled() else "Błąd")
        QgsMessageLog.logMessage(
            f"[EziudpTask] {status}: {self._report.format_summary()}",
            LOG_TAG,
            Qgis.MessageLevel.Success if result else Qgis.MessageLevel.Warning,
        )
        # Emitujemy pustą listę – dane są już w warstwach QGIS (tryb streaming)
        self.downloadFinished.emit([], self._report)

    def cancel(self):
        super().cancel()

    # ------------------------------------------------------------------
    # Logika dla jednego powiatu
    # ------------------------------------------------------------------

    def _process_one_teryt(self, teryt: str) -> PowiatDownloadReport:
        report = PowiatDownloadReport(teryt=teryt)

        # Zgrupuj layer_keys po zbiorze – jedno GetCapabilities na zbior
        zbior_to_keys: dict = {}
        for key in self.layer_keys:
            cfg = AVAILABLE_LAYERS_BY_KEY.get(key)
            if cfg:
                zbior_to_keys.setdefault(cfg["zbior"], []).append(key)

        for zbior, keys in zbior_to_keys.items():
            if self.isCanceled():
                return report

            # Pobierz URL WFS z eziudp
            eziudp_result = self._eziudp.get_wfs_urls(teryt, zbior)
            if not eziudp_result.success or not eziudp_result.records:
                err = eziudp_result.error_message or "Brak usługi WFS w EZiUDP"
                QgsMessageLog.logMessage(
                    f"[EziudpTask] TERYT {teryt} zbior={zbior}: {err}",
                    LOG_TAG, Qgis.MessageLevel.Warning,
                )
                self._report.errors.append(f"TERYT {teryt} [{zbior}]: {err}")
                continue

            wfs_url = eziudp_result.records[0].url_wfs
            report.wfs_url = wfs_url

            # Pobierz listę warstw z GetCapabilities
            all_wfs_layers = self._eziudp.get_layer_names(wfs_url, zbior)

            zbior_cfg = ZBIOR_CONFIG.get(zbior, {})
            geom_field = zbior_cfg.get("geom_field", "msGeometry")
            id_field = zbior_cfg.get("id_field", "")

            for key in keys:
                if self.isCanceled():
                    return report

                layer_cfg = AVAILABLE_LAYERS_BY_KEY[key]
                pattern = layer_cfg.get("layer_pattern")

                wfs_layer = self._pick_wfs_layer(all_wfs_layers, pattern)
                if not wfs_layer:
                    QgsMessageLog.logMessage(
                        f"[EziudpTask] TERYT {teryt}: brak warstwy dla key={key} "
                        f"(wzorzec={pattern!r}) w {all_wfs_layers}",
                        LOG_TAG, Qgis.MessageLevel.Warning,
                    )
                    continue

                self.progressMessage.emit(f"  TERYT {teryt}: '{wfs_layer}'")
                report.layer_names.append(wfs_layer)

                client = _DynamicWFSClient(
                    url=wfs_url,
                    layer_name=wfs_layer,
                    id_field=id_field,
                    geom_field=geom_field,
                )

                filter_xml = ""
                if self.bbox_wkt:
                    try:
                        filter_xml = client.build_spatial_filter(self.bbox_wkt, use_bbox=True)
                    except Exception as exc:
                        QgsMessageLog.logMessage(
                            f"[EziudpTask] filtr błąd: {exc}", LOG_TAG, Qgis.MessageLevel.Warning
                        )

                harmonizer = get_harmonizer(zbior, wfs_layer)
                layer_display_name = (
                    f"{harmonizer.LAYER_NAME}_{teryt}" if harmonizer else None
                )

                # -------------------------------------------------------
                # STREAMING: iteruj po stronach, wczytuj każdą natychmiast
                # -------------------------------------------------------
                page_count, total_added = self._stream_and_flush(
                    client=client,
                    filter_xml=filter_xml,
                    teryt=teryt,
                    wfs_layer=wfs_layer,
                    zbior=zbior,
                    wfs_url=wfs_url,
                    harmonizer=harmonizer,
                    layer_display_name=layer_display_name,
                    detected_crs_ref=report,
                )

                # Fallback: BBOX filtr zwrócił 0 stron z danymi – spróbuj bez filtra
                if (
                    page_count == 0
                    and filter_xml
                    and self.clip_geom_wkt
                    and not self.isCanceled()
                ):
                    QgsMessageLog.logMessage(
                        f"[EziudpTask] TERYT {teryt} '{wfs_layer}': "
                        f"filtr BBOX zwrócił 0 wyników – ponawiam bez filtra "
                        f"i przetnę lokalnie.",
                        LOG_TAG, Qgis.MessageLevel.Info,
                    )
                    _, total_added = self._stream_and_flush(
                        client=client,
                        filter_xml="",
                        teryt=teryt,
                        wfs_layer=wfs_layer,
                        zbior=zbior,
                        wfs_url=wfs_url,
                        harmonizer=harmonizer,
                        layer_display_name=layer_display_name,
                        detected_crs_ref=report,
                    )

                report.feature_count += total_added

        report.success = True
        return report

    def _stream_and_flush(
        self,
        client: _DynamicWFSClient,
        filter_xml: str,
        teryt: str,
        wfs_layer: str,
        zbior: str,
        wfs_url: str,
        harmonizer,
        layer_display_name: Optional[str],
        detected_crs_ref: PowiatDownloadReport,
    ) -> tuple:
        """
        Iteruje generator stron WFS i dla każdej strony:
          1. Przytnij lokalnie (jeśli clip_geom_wkt)
          2. Harmonizuj
          3. Wczytaj do warstwy QGIS
          4. Zwolnij pamięć (page_features usunięte po flush)

        Zwraca (liczba_stron_z_danymi, łączna_liczba_dodanych_obiektów).
        """
        clip_geom = None
        if self.clip_geom_wkt:
            clip_geom = QgsGeometry.fromWkt(self.clip_geom_wkt)
            if clip_geom and clip_geom.isEmpty():
                clip_geom = None

        pages_with_data = 0
        total_added = 0

        for page_features, detected_crs in self._stream_pages(
            client, filter_xml, teryt, wfs_layer
        ):
            if self.isCanceled():
                break

            # Aktualizuj CRS z pierwszej strony
            if pages_with_data == 0:
                detected_crs_ref.detected_crs = detected_crs

            if not page_features:
                continue

            pages_with_data += 1

            # Lokalne przycinanie do dokładnej geometrii
            if clip_geom is not None:
                before = len(page_features)
                page_features = [
                    f for f in page_features
                    if QgsGeometry.fromWkt(f.get("geom", "")).intersects(clip_geom)
                ]
                if before != len(page_features):
                    QgsMessageLog.logMessage(
                        f"[EziudpTask] TERYT {teryt} '{wfs_layer}' strona: "
                        f"przycinanie {before} → {len(page_features)}",
                        LOG_TAG, Qgis.MessageLevel.Info,
                    )

            if not page_features:
                continue

            # Harmonizuj i wczytaj do QGIS – natychmiast zwalniamy page_features
            if harmonizer:
                harmonized = harmonizer.harmonize(
                    page_features,
                    teryt=teryt,
                    wfs_url=wfs_url,
                    source_crs=detected_crs,
                )
                # page_features nie jest już potrzebne
                del page_features

                added = harmonizer.add_to_layer(
                    harmonized,
                    layer_name=layer_display_name,
                    load_style=self.load_style,
                    plugin_dir=self.plugin_dir,
                )
                del harmonized
                total_added += added

                self.progressMessage.emit(
                    f"  TERYT {teryt} '{wfs_layer}': "
                    f"+{added} obiektów (łącznie: {total_added})"
                )
            else:
                # Brak harmonizatora – wczytaj surowe dane bezpośrednio (rzadki przypadek)
                total_added += len(page_features)
                del page_features

        return pages_with_data, total_added

    @staticmethod
    def _pick_wfs_layer(available: list, pattern: Optional[str]) -> Optional[str]:
        """Zwraca nazwę warstwy WFS pasującą do wzorca (lub pierwszą jeśli pattern=None)."""
        if not available:
            return None
        if not pattern:
            return available[0]
        norm = pattern.lower().replace("_", "").replace("-", "")
        for name in available:
            name_norm = name.lower().replace(":", "").replace("_", "").replace("-", "")
            if norm in name_norm:
                return name
        return None

    # ------------------------------------------------------------------
    # Generator stron WFS (streaming)
    # ------------------------------------------------------------------

    def _stream_pages(
        self,
        client: _DynamicWFSClient,
        filter_xml: str,
        teryt: str,
        layer_name: str,
    ) -> Iterator[tuple]:
        """
        Generator – dla każdej strony WFS yield'uje (page_features, detected_crs).

        Każda iteracja pobiera dokładnie jedną stronę (PAGE_SIZE rekordów),
        parsuje ją i oddaje sterowanie. GML poprzedniej strony jest poza
        zasięgiem i może zostać zwolniony przez GC zanim zacznie się pobieranie
        kolejnej strony.

        Obsługuje:
          - WFS 2.0 (domyślnie)
          - WFS 1.1 fallback (jeśli WFS 2.0 zwraca ExceptionReport na start=0)
          - OGR fallback parser (jeśli DOM zwraca 0 na niepustym GML)

        Generuje puste sekwencje (StopIteration) przy:
          - błędzie sieciowym
          - numberMatched=0 na pierwszej stronie
          - anulowaniu zadania
        """
        PAGE_SIZE = 1000000 # 500
        start_index = 0
        detected_crs = "EPSG:2180"
        crs_detected = False
        use_ogr = False
        use_wfs11 = False

        _parser = DownloadTask.__new__(DownloadTask)

        while True:
            if self.isCanceled():
                return

            # --- Pobierz stronę ---
            try:
                if use_wfs11:
                    gml = _try_wfs11_fallback(
                        client, filter_xml, PAGE_SIZE, start_index, teryt, layer_name
                    )
                    if gml is None:
                        return
                else:
                    gml = client.download(
                        filter_xml, start_index=start_index, count=PAGE_SIZE
                    )
            except Exception as exc:
                QgsMessageLog.logMessage(
                    f"[EziudpTask] WFS błąd (start={start_index}): {exc}",
                    LOG_TAG, Qgis.MessageLevel.Warning,
                )
                return

            # --- Walidacja odpowiedzi ---
            if _is_error_response(gml, teryt, layer_name):
                if start_index == 0 and not use_wfs11:
                    QgsMessageLog.logMessage(
                        f"[EziudpTask] TERYT {teryt} '{layer_name}': "
                        f"WFS 2.0 błąd – przełączam na WFS 1.1",
                        LOG_TAG, Qgis.MessageLevel.Info,
                    )
                    use_wfs11 = True
                    continue
                return

            # --- numberMatched=0 na pierwszej stronie → warstwa pusta ---
            if start_index == 0:
                num = _number_matched_from_gml(gml)
                if num == 0:
                    QgsMessageLog.logMessage(
                        f"[EziudpTask] TERYT {teryt} '{layer_name}': numberMatched=0",
                        LOG_TAG, Qgis.MessageLevel.Info,
                    )
                    return

            # --- Wykryj CRS z pierwszej strony ---
            if not crs_detected:
                detected_crs = _detect_crs_from_gml(gml)
                crs_detected = True

            # --- Parsuj stronę ---
            if use_ogr:
                page_features = _parse_gml_via_ogr(gml, teryt, layer_name)
            else:
                try:
                    page_features = _parser._parse_gml(gml)
                except Exception as exc:
                    QgsMessageLog.logMessage(
                        f"[EziudpTask] parsowanie GML błąd: {exc}",
                        LOG_TAG, Qgis.MessageLevel.Warning,
                    )
                    return

                # DOM zwrócił 0 na niepustym GML → przełącz na OGR
                if not page_features and start_index == 0:
                    preview = gml.strip()[:600].replace("\n", " ")
                    QgsMessageLog.logMessage(
                        f"[EziudpTask] TERYT {teryt} '{layer_name}': "
                        f"parser DOM zwrócił 0 wyników ({len(gml)} znaków). "
                        f"Przełączam na OGR fallback.\nPodgląd: {preview}",
                        LOG_TAG, Qgis.MessageLevel.Warning,
                    )
                    use_ogr = True
                    page_features = _parse_gml_via_ogr(gml, teryt, layer_name)

            # GML tej strony nie jest już potrzebny – jawne usunięcie referencji
            del gml

            QgsMessageLog.logMessage(
                f"[EziudpTask] TERYT {teryt} '{layer_name}' "
                f"start={start_index}: {len(page_features)} rek.",
                LOG_TAG, Qgis.MessageLevel.Info,
            )

            # --- Yield strony do wywołującego ---
            yield page_features, detected_crs

            # --- Koniec paginacji gdy strona niepełna ---
            if len(page_features) < PAGE_SIZE:
                return

            start_index += PAGE_SIZE