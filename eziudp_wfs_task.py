# -*- coding: utf-8 -*-
"""
Zadanie QgsTask dodające warstwy z powiatowych usług WFS przez eziudp
w trybie snapshot (WFS provider → kopia do warstwy memory).

Architektura:
  - Dla każdego powiatu × wybranej warstwy budowany jest URI WFS
    z parametrami BBOX lub filtrem geometrii (subsetString OGC BBOX).
  - QgsVectorLayer jest tworzona z providerem "WFS" – QGIS sam pobiera
    dane przez paginację WFS 2.0 (fallback: 1.1).
  - Po walidacji featuresy są kopiowane do warstwy memory – snapshot.
  - Warstwa memory dostaje styl QML (tak samo jak BaseHarmonizer).
  - Brak harmonizacji schematu – pola zachowane tak jak ze źródła WFS.

Filtrowanie zakresu:
  - TERYT powiatu → brak filtra (cały powiat)
  - TERYT gminy / extend / warstwa → BBOX w URI WFS
    ("restrictToRequestBBOX=1" + "bbox=…") oraz subsetString
    z OGC BBOX do lokalnego przycinania po pobraniu jeśli serwer
    nie zastosował filtra
"""
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from qgis.core import (
    QgsTask, QgsMessageLog, Qgis,
    QgsVectorLayer, QgsProject,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsGeometry, QgsFeature, QgsRectangle,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import pyqtSignal

from .eziudp_client import EziudpClient, ZBIOR_CONFIG
from .bdsog_harmonizer import get_harmonizer

LOG_TAG = "PD_GUGiK"

# Wersje WFS próbowane po kolei
_WFS_VERSIONS = ["2.0.0", "1.1.0"]

# Domyślny CRS warstw powiatowych
_TARGET_CRS = "EPSG:2180"

# Pola meta dodawane do każdej warstwy memory
_META_FIELDS = ["teryt_powiatu", "zrodlo_wfs"]


# ---------------------------------------------------------------------------
# Re-eksport AVAILABLE_LAYERS – ten moduł może zastąpić eziudp_download_task
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
        "key":           "egib_pkt",
        "label":         "Punkty graniczne (EGIB)",
        "zbior":         "egib",
        "layer_pattern": "punkty_graniczne",
    },
    {
        "key":           "rcn_tran",
        "label":         "Lokalizacja transakcji (RCN)",
        "zbior":         "rcn",
        "layer_pattern": "transakcje",
    },
    {
        "key":           "bdot500",
        "label":         "Budynki i obiekty towarzysząc (BDOT500)",
        "zbior":         "bdot500",
        "layer_pattern": "budynki",
    },
]

AVAILABLE_LAYERS_BY_KEY = {e["key"]: e for e in AVAILABLE_LAYERS}


# ---------------------------------------------------------------------------
# Raporty
# ---------------------------------------------------------------------------

@dataclass
class WfsLayerReport:
    teryt: str
    key: str
    success: bool = False
    feature_count: int = 0
    wfs_url: str = ""
    wfs_layer: str = ""
    wfs_version: str = ""
    error: str = ""


@dataclass
class EziudpWfsReport:
    total_teryts: int = 0
    successful: int = 0
    failed: int = 0
    total_features: int = 0
    layers: list = field(default_factory=list)   # lista WfsLayerReport
    errors: list = field(default_factory=list)

    def format_summary(self) -> str:
        lines = [
            f"Powiaty: {self.successful}/{self.total_teryts} OK, {self.failed} błędów",
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
# Pomocnicze funkcje URI WFS
# ---------------------------------------------------------------------------

def _wfs_uri(
    base_url: str,
    layer_name: str,
    version: str,
    bbox_rect: Optional[QgsRectangle] = None,
    crs: str = _TARGET_CRS,
) -> str:
    """
    Buduje URI dla QgsVectorLayer z providerem WFS.

    Parametry QGIS WFS provider:
      url        – adres bazowy (bez ?service=WFS itp.)
      typename   – nazwa warstwy
      version    – "2.0.0" lub "1.1.0"
      srsname    – układ współrzędnych
      bbox       – "xmin,ymin,xmax,ymax,EPSG:XXXX" (opcjonalny)
      restrictToRequestBBOX – "1" gdy BBOX podany → provider sam filtruje

    Dokumentacja parametrów:
    https://docs.qgis.org/latest/en/docs/user_manual/managing_data_source/opening_data.html#wfs-and-wcs-client
    """
    # Wyczyść URL z parametrów OGC – provider sam je buduje
    base = base_url.split("?")[0]

    parts = [
        f"url={base}",
        f"typename={layer_name}",
        f"version={version}",
        f"srsname={crs}",
        "pagingEnabled=true",
        "preferCoordinatesForWfsT11=false",
    ]

    if bbox_rect is not None:
        xmin = bbox_rect.xMinimum()
        ymin = bbox_rect.yMinimum()
        xmax = bbox_rect.xMaximum()
        ymax = bbox_rect.yMaximum()
        parts.append(f"bbox={xmin},{ymin},{xmax},{ymax},{crs}")
        parts.append("restrictToRequestBBOX=1")

    return " ".join(parts)


def _try_wfs_layer(
    base_url: str,
    layer_name: str,
    bbox_rect: Optional[QgsRectangle],
    crs: str = _TARGET_CRS,
) -> tuple:
    """
    Próbuje zbudować QgsVectorLayer kolejno dla WFS 2.0.0 i 1.1.0.

    Zwraca (QgsVectorLayer, version_str) dla pierwszej działającej wersji
    lub (None, "") gdy obie zawiodą.
    """
    for version in _WFS_VERSIONS:
        uri = _wfs_uri(base_url, layer_name, version, bbox_rect, crs)
        vl = QgsVectorLayer(uri, f"_tmp_{layer_name}", "WFS")
        if vl.isValid():
            QgsMessageLog.logMessage(
                f"[EziudpWfsTask] WFS {version} OK: {layer_name} @ {base_url}",
                LOG_TAG, Qgis.MessageLevel.Info,
            )
            return vl, version
        else:
            QgsMessageLog.logMessage(
                f"[EziudpWfsTask] WFS {version} nie działa dla {layer_name}: "
                f"{vl.error().message() if hasattr(vl, 'error') else 'nieznany błąd'}",
                LOG_TAG, Qgis.MessageLevel.Info,
            )
    return None, ""


# ---------------------------------------------------------------------------
# Snapshot: kopiuj featuresy z WFS provider do warstwy memory
# ---------------------------------------------------------------------------

def _snapshot_to_memory(
    wfs_layer: QgsVectorLayer,
    mem_layer: QgsVectorLayer,
    clip_geom: Optional[QgsGeometry],
    teryt: str,
    wfs_url: str,
    task_check_cancelled,
) -> int:
    """
    Iteruje przez featuresy z wfs_layer i zapisuje je do mem_layer.

    Jeśli clip_geom podany – stosuje lokalne przycinanie (przydatne gdy
    serwer nie zastosował filtra BBOX).

    Zwraca liczbę dodanych obiektów.
    """
    pr = mem_layer.dataProvider()
    wfs_fields = wfs_layer.fields()
    mem_fields = mem_layer.fields()

    # Indeksy pól meta w mem_layer
    idx_teryt = mem_fields.indexOf("teryt_powiatu")
    idx_src = mem_fields.indexOf("zrodlo_wfs")

    # Transformacja CRS jeśli wfs_layer ma inny CRS niż mem_layer
    src_crs = wfs_layer.crs()
    tgt_crs = mem_layer.crs()
    transform = None
    if src_crs.isValid() and tgt_crs.isValid() and src_crs != tgt_crs:
        transform = QgsCoordinateTransform(src_crs, tgt_crs, QgsProject.instance())

    added = 0
    batch: list = []
    BATCH_SIZE = 500

    for src_feat in wfs_layer.getFeatures():
        if task_check_cancelled():
            break

        geom = src_feat.geometry()
        if geom is None or geom.isEmpty():
            continue

        # Transformacja geometrii
        if transform:
            geom.transform(transform)

        # Lokalne przycinanie
        if clip_geom is not None and not geom.intersects(clip_geom):
            continue

        dst_feat = QgsFeature(mem_fields)
        dst_feat.setGeometry(geom)

        # Kopiuj atrybuty po nazwie pola
        for wfs_field in wfs_fields:
            mem_idx = mem_fields.indexOf(wfs_field.name())
            if mem_idx >= 0:
                dst_feat.setAttribute(mem_idx, src_feat.attribute(wfs_field.name()))

        # Pola meta
        if idx_teryt >= 0:
            dst_feat.setAttribute(idx_teryt, teryt)
        if idx_src >= 0:
            dst_feat.setAttribute(idx_src, wfs_url)

        batch.append(dst_feat)

        if len(batch) >= BATCH_SIZE:
            pr.addFeatures(batch)
            added += len(batch)
            batch.clear()

    if batch:
        pr.addFeatures(batch)
        added += len(batch)

    if added > 0:
        mem_layer.updateExtents()
        mem_layer.triggerRepaint()

    return added


def _create_memory_layer(
    wfs_layer: QgsVectorLayer,
    layer_name: str,
    plugin_dir: str,
    load_style: bool,
) -> QgsVectorLayer:
    """
    Tworzy lub zwraca istniejącą warstwę memory dla podanej nazwy.

    Schemat pól = pola z WFS provider + pola meta (teryt_powiatu, zrodlo_wfs).
    Jeśli warstwa o tej nazwie już istnieje w projekcie – zwraca ją
    (featuresy będą dołączone do istniejącej).
    """
    existing = QgsProject.instance().mapLayersByName(layer_name)
    if existing:
        return existing[0]

    # Typ geometrii z WFS provider
    geom_type = QgsWkbTypes.displayString(wfs_layer.wkbType())
    crs = _TARGET_CRS

    mem_uri = f"{geom_type}?crs={crs}"
    mem_layer = QgsVectorLayer(mem_uri, layer_name, "memory")
    pr = mem_layer.dataProvider()

    # Pola z WFS + meta
    fields_to_add = list(wfs_layer.fields())
    for meta in _META_FIELDS:
        if wfs_layer.fields().indexOf(meta) < 0:
            from qgis.PyQt.QtCore import QVariant
            from qgis.core import QgsField
            fields_to_add.append(QgsField(meta, QVariant.String))

    pr.addAttributes(fields_to_add)
    mem_layer.updateFields()

    # Styl QML – szukamy po base_name (np. "dzialki", "osnowa_pozioma")
    if load_style and plugin_dir:
        style_base = layer_name.lower()
        # Usuń sufiks TERYT jeśli obecny (np. "Dzialki_0401" → "dzialki")
        style_base = re.sub(r"_\d{4,7}$", "", style_base)
        style_path = os.path.join(plugin_dir, "data", f"{style_base}.qml")
        if os.path.exists(style_path):
            mem_layer.loadNamedStyle(style_path)
            QgsMessageLog.logMessage(
                f"[EziudpWfsTask] Styl załadowany: {style_path}",
                LOG_TAG, Qgis.MessageLevel.Info,
            )

    QgsProject.instance().addMapLayer(mem_layer)
    return mem_layer


# ---------------------------------------------------------------------------
# Główne zadanie QgsTask
# ---------------------------------------------------------------------------

class EziudpWfsTask(QgsTask):
    """
    Dodaje warstwy WFS jako snapshot (memory) dla wybranych powiatów.

    Osobna warstwa memory na każdy powiat × typ danych,
    np. "Dzialki_0401", "Dzialki_0402".

    Filtrowanie zakresu:
      - bbox_wkt  → BBOX przekazany do URI WFS + lokalne przycinanie
      - clip_geom_wkt → precyzyjne lokalne przycinanie po pobraniu

    Parametry zgodne z EziudpDownloadTask – możliwa wymiana w dialog.
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
        task_label = f"eziudp WFS: {', '.join(labels)} ({len(teryt_list)} powiatów)"

        super().__init__(task_label, QgsTask.Flag.CanCancel)
        self.teryt_list = [str(t).strip() for t in teryt_list if str(t).strip()]
        self.layer_keys = layer_keys
        self.bbox_wkt = bbox_wkt
        self.clip_geom_wkt = clip_geom_wkt
        self.plugin_dir = plugin_dir
        self.load_style = load_style

        self._eziudp = EziudpClient()
        self._report = EziudpWfsReport(total_teryts=len(self.teryt_list))
        self.exception: Optional[Exception] = None

        # Przelicz BBOX raz – używany wielokrotnie
        self._bbox_rect: Optional[QgsRectangle] = None
        if bbox_wkt:
            g = QgsGeometry.fromWkt(bbox_wkt)
            if g and not g.isEmpty():
                self._bbox_rect = g.boundingBox()

        self._clip_geom: Optional[QgsGeometry] = None
        if clip_geom_wkt:
            g = QgsGeometry.fromWkt(clip_geom_wkt)
            if g and not g.isEmpty():
                self._clip_geom = g

    # ------------------------------------------------------------------
    # QgsTask
    # ------------------------------------------------------------------

    def run(self) -> bool:
        total = len(self.teryt_list)

        for i, teryt in enumerate(self.teryt_list):
            if self.isCanceled():
                return False

            progress = int((i / total) * 100)
            self.setProgress(progress)
            self.progressChanged.emit(float(progress))

            msg = f"[{i + 1}/{total}] TERYT {teryt}"
            self.progressMessage.emit(msg)
            QgsMessageLog.logMessage(f"[EziudpWfsTask] {msg}", LOG_TAG, Qgis.MessageLevel.Info)

            powiat_ok = self._process_one_teryt(teryt)
            if powiat_ok:
                self._report.successful += 1
            else:
                self._report.failed += 1

        self.setProgress(100)
        self.progressChanged.emit(100.0)
        return True

    def finished(self, result: bool):
        status = "Zakończono" if result else (
            "Anulowano" if self.isCanceled() else "Błąd"
        )
        QgsMessageLog.logMessage(
            f"[EziudpWfsTask] {status}: {self._report.format_summary()}",
            LOG_TAG,
            Qgis.MessageLevel.Success if result else Qgis.MessageLevel.Warning,
        )
        # Emituje pustą listę – dane są już w warstwach QGIS
        self.downloadFinished.emit([], self._report)

    def cancel(self):
        super().cancel()

    # ------------------------------------------------------------------
    # Logika dla jednego powiatu
    # ------------------------------------------------------------------

    def _process_one_teryt(self, teryt: str) -> bool:
        """Przetwarza wszystkie wybrane warstwy dla jednego powiatu."""
        any_ok = False

        # Grupuj klucze po zbiorze – jedno GetCapabilities na zbior
        zbior_to_keys: dict = {}
        for key in self.layer_keys:
            cfg = AVAILABLE_LAYERS_BY_KEY.get(key)
            if cfg:
                zbior_to_keys.setdefault(cfg["zbior"], []).append(key)

        for zbior, keys in zbior_to_keys.items():
            if self.isCanceled():
                return any_ok

            # Pobierz URL WFS z eziudp
            eziudp_result = self._eziudp.get_wfs_urls(teryt, zbior)
            if not eziudp_result.success or not eziudp_result.records:
                err = eziudp_result.error_message or "Brak usługi WFS w EZiUDP"
                QgsMessageLog.logMessage(
                    f"[EziudpWfsTask] TERYT {teryt} zbior={zbior}: {err}",
                    LOG_TAG, Qgis.MessageLevel.Warning,
                )
                self._report.errors.append(f"TERYT {teryt} [{zbior}]: {err}")
                continue

            wfs_url = eziudp_result.records[0].url_wfs
            base_url = wfs_url.split("?")[0]

            # Pobierz listę warstw z GetCapabilities
            all_wfs_layers = self._eziudp.get_layer_names(wfs_url, zbior)

            for key in keys:
                if self.isCanceled():
                    return any_ok

                layer_cfg = AVAILABLE_LAYERS_BY_KEY[key]
                pattern = layer_cfg.get("layer_pattern")
                wfs_layer_name = self._pick_wfs_layer(all_wfs_layers, pattern)

                if not wfs_layer_name:
                    msg = (
                        f"TERYT {teryt}: brak warstwy dla key={key} "
                        f"(wzorzec={pattern!r}) w {all_wfs_layers}"
                    )
                    QgsMessageLog.logMessage(
                        f"[EziudpWfsTask] {msg}", LOG_TAG, Qgis.MessageLevel.Warning,
                    )
                    self._report.errors.append(msg)
                    continue

                report = self._fetch_one_layer(
                    teryt=teryt,
                    key=key,
                    base_url=base_url,
                    wfs_url=wfs_url,
                    wfs_layer_name=wfs_layer_name,
                )
                self._report.layers.append(report)
                self._report.total_features += report.feature_count

                if report.success:
                    any_ok = True
                    self.progressMessage.emit(
                        f"  TERYT {teryt} '{wfs_layer_name}': "
                        f"{report.feature_count} obiektów → '{key}_{teryt}'"
                    )
                else:
                    self._report.errors.append(
                        f"TERYT {teryt} [{key}]: {report.error}"
                    )

        return any_ok

    def _fetch_one_layer(
        self,
        teryt: str,
        key: str,
        base_url: str,
        wfs_url: str,
        wfs_layer_name: str,
    ) -> WfsLayerReport:
        """
        Pobiera jedną warstwę WFS dla jednego powiatu jako snapshot.

        Zwraca WfsLayerReport z wynikami.
        """
        report = WfsLayerReport(teryt=teryt, key=key, wfs_url=wfs_url,
                                wfs_layer=wfs_layer_name)

        # Harmonizer używany tylko do LAYER_NAME i nazwy stylu
        harmonizer = get_harmonizer(
            AVAILABLE_LAYERS_BY_KEY[key]["zbior"], wfs_layer_name
        )
        base_layer_name = harmonizer.LAYER_NAME if harmonizer else key
        mem_layer_name = f"{base_layer_name}_{teryt}"

        # Sprawdź czy warstwa już istnieje (np. ponowne uruchomienie)
        existing = QgsProject.instance().mapLayersByName(mem_layer_name)
        if existing:
            QgsMessageLog.logMessage(
                f"[EziudpWfsTask] Warstwa '{mem_layer_name}' już istnieje – pomijam.",
                LOG_TAG, Qgis.MessageLevel.Info,
            )
            report.success = True
            report.wfs_version = "cached"
            return report

        # Próbuj WFS 2.0, fallback na 1.1
        wfs_vl, version = _try_wfs_layer(
            base_url, wfs_layer_name, self._bbox_rect
        )

        if wfs_vl is None:
            # Ostatnia próba: bez BBOX (niektóre serwery odrzucają BBOX w URI)
            if self._bbox_rect is not None:
                QgsMessageLog.logMessage(
                    f"[EziudpWfsTask] TERYT {teryt} '{wfs_layer_name}': "
                    f"próba bez BBOX w URI (przytnę lokalnie).",
                    LOG_TAG, Qgis.MessageLevel.Info,
                )
                wfs_vl, version = _try_wfs_layer(base_url, wfs_layer_name, None)

            if wfs_vl is None:
                report.error = (
                    f"QgsVectorLayer (WFS) nieprawidłowa dla obu wersji: "
                    f"{wfs_layer_name} @ {base_url}"
                )
                QgsMessageLog.logMessage(
                    f"[EziudpWfsTask] {report.error}", LOG_TAG, Qgis.MessageLevel.Warning,
                )
                return report

        report.wfs_version = version

        # Utwórz lub pobierz warstwę memory (schemat pól z WFS provider)
        mem_layer = _create_memory_layer(
            wfs_layer=wfs_vl,
            layer_name=mem_layer_name,
            plugin_dir=self.plugin_dir,
            load_style=self.load_style,
        )

        if not mem_layer.isValid():
            report.error = f"Nie udało się utworzyć warstwy memory: {mem_layer_name}"
            return report

        # Snapshot: kopiuj featuresy z WFS do memory
        # clip_geom stosowany gdy: wybrano gminę / extend / warstwę
        # i gdy BBOX w URI mógł nie być zrozumiany przez serwer
        count = _snapshot_to_memory(
            wfs_layer=wfs_vl,
            mem_layer=mem_layer,
            clip_geom=self._clip_geom,
            teryt=teryt,
            wfs_url=wfs_url,
            task_check_cancelled=self.isCanceled,
        )

        # Jawne usunięcie WFS layer – zwalniamy połączenie i pamięć
        del wfs_vl

        # Jeśli snapshot jest pusty a był BBOX → ponów bez BBOX i przytnij lokalnie
        if count == 0 and self._bbox_rect is not None and self._clip_geom is not None:
            QgsMessageLog.logMessage(
                f"[EziudpWfsTask] TERYT {teryt} '{wfs_layer_name}': "
                f"BBOX w URI dał 0 wyników – ponawiam bez BBOX, przytnę lokalnie.",
                LOG_TAG, Qgis.MessageLevel.Info,
            )
            wfs_vl2, version2 = _try_wfs_layer(base_url, wfs_layer_name, None)
            if wfs_vl2 is not None:
                count = _snapshot_to_memory(
                    wfs_layer=wfs_vl2,
                    mem_layer=mem_layer,
                    clip_geom=self._clip_geom,
                    teryt=teryt,
                    wfs_url=wfs_url,
                    task_check_cancelled=self.isCanceled,
                )
                del wfs_vl2
                report.wfs_version = version2

        QgsMessageLog.logMessage(
            f"[EziudpWfsTask] TERYT {teryt} '{wfs_layer_name}' "
            f"WFS {report.wfs_version}: {count} obiektów → '{mem_layer_name}'",
            LOG_TAG, Qgis.MessageLevel.Info,
        )

        report.feature_count = count
        report.success = True
        return report

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_wfs_layer(available: list, pattern: Optional[str]) -> Optional[str]:
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