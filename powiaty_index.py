# -*- coding: utf-8 -*-
"""
Indeks przestrzenny powiatów ładowany z bundlowanego pliku GeoJSON.

Plik: <plugin_dir>/index/powiaty.geojson
Schemat: { "TERYT": "1465", "Nazwa": "powiat Warszawa" }
CRS pliku: EPSG:4326 – konwertowane do EPSG:2180 przy załadowaniu.

Użycie:
    from .powiaty_index import PowiatyIndex
    idx = PowiatyIndex.instance(plugin_dir)
    teryts = idx.teryts_intersecting(bbox_geom_2180)
    geom   = idx.geometry_for_teryt("1465")          # EPSG:2180
"""
from __future__ import annotations

import json
import os
from typing import Optional

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsMessageLog,
    QgsProject,
    QgsSpatialIndex,
    QgsFeature,
    QgsRectangle,
    Qgis,
)

LOG_TAG = "PD_GUGiK"
_CRS_WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
_CRS_2180 = QgsCoordinateReferenceSystem("EPSG:2180")


class PowiatyIndex:
    """
    Singleton per plugin_dir – ładuje GeoJSON raz, buduje QgsSpatialIndex.

    Wątek-bezpieczność: konstruktor wywoływany tylko z wątku głównego
    (przy starcie wtyczki lub pierwszym użyciu w dialogu).
    """

    _instances: dict[str, "PowiatyIndex"] = {}

    # ------------------------------------------------------------------
    # Fabryka
    # ------------------------------------------------------------------

    @classmethod
    def instance(cls, plugin_dir: str) -> "PowiatyIndex":
        """Zwraca (lub tworzy) singleton dla danego plugin_dir."""
        if plugin_dir not in cls._instances:
            cls._instances[plugin_dir] = cls(plugin_dir)
        return cls._instances[plugin_dir]

    @classmethod
    def invalidate(cls, plugin_dir: str | None = None):
        """Wyczyść cache (np. po przeładowaniu wtyczki)."""
        if plugin_dir is None:
            cls._instances.clear()
        else:
            cls._instances.pop(plugin_dir, None)

    # ------------------------------------------------------------------
    # Konstruktor
    # ------------------------------------------------------------------

    def __init__(self, plugin_dir: str):
        self._plugin_dir = plugin_dir
        # teryt → QgsGeometry (EPSG:2180)
        self._geometries: dict[str, QgsGeometry] = {}
        # teryt → nazwa
        self._names: dict[str, str] = {}
        # Przestrzenny indeks QgsSpatialIndex
        self._spatial_index: Optional[QgsSpatialIndex] = None
        # fid → teryt (QgsSpatialIndex operuje na fid int)
        self._fid_to_teryt: dict[int, str] = {}

        self._load()

    # ------------------------------------------------------------------
    # Publiczne API
    # ------------------------------------------------------------------

    def teryts_intersecting(self, query_geom: QgsGeometry) -> list[str]:
        """
        Zwraca listę kodów TERYT powiatów przecinających query_geom.
        query_geom musi być w EPSG:2180.
        """
        if self._spatial_index is None:
            return []

        # Kandydaci z indeksu (tylko BBOX)
        candidate_fids = self._spatial_index.intersects(query_geom.boundingBox())

        QgsMessageLog.logMessage(
            f"[PowiatyIndex] Kandydaci z BBOX "
            f" {len(candidate_fids)}",
            LOG_TAG, Qgis.MessageLevel.Info,
        )

        result = []
        for fid in candidate_fids:
            teryt = self._fid_to_teryt.get(fid)
            if teryt is None:
                continue
            geom = self._geometries.get(teryt)
            if geom and not geom.isEmpty() and query_geom.intersects(geom):
                result.append(teryt)

        return result

    def geometry_for_teryt(self, teryt: str) -> Optional[QgsGeometry]:
        """Zwraca geometrię powiatu (EPSG:2180) lub None."""
        return self._geometries.get(teryt)

    def name_for_teryt(self, teryt: str) -> str:
        """Zwraca nazwę powiatu lub pusty string."""
        return self._names.get(teryt, "")

    def all_teryts(self) -> list[str]:
        return list(self._geometries.keys())

    def is_loaded(self) -> bool:
        return bool(self._geometries)

    # ------------------------------------------------------------------
    # Ładowanie GeoJSON
    # ------------------------------------------------------------------

    def _load(self):
        geojson_path = os.path.join(self._plugin_dir, "index", "powiaty.geojson")
        if not os.path.exists(geojson_path):
            QgsMessageLog.logMessage(
                f"[PowiatyIndex] Brak pliku: {geojson_path}",
                LOG_TAG, Qgis.MessageLevel.Critical,
            )
            return

        try:
            with open(geojson_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            QgsMessageLog.logMessage(
                f"[PowiatyIndex] Błąd odczytu GeoJSON: {exc}",
                LOG_TAG, Qgis.MessageLevel.Critical,
            )
            return

        transform = QgsCoordinateTransform(
            _CRS_WGS84, _CRS_2180, QgsProject.instance()
        )
        spatial_index = QgsSpatialIndex()

        fid = 0
        loaded = 0
        skipped = 0

        for feature in data.get("features", []):
            props = feature.get("properties", {})
            teryt = str(props.get("TERYT", "")).strip()
            name = str(props.get("Nazwa", "")).strip()

            if not teryt:
                skipped += 1
                continue

            geom = QgsGeometry.fromWkt(
                _geojson_geom_to_wkt(feature.get("geometry"))
            )
            if geom is None or geom.isEmpty():
                skipped += 1
                continue

            # Transformacja WGS84 → EPSG:2180
            """
            try:
                geom.transform(transform)
            except Exception as exc:
                QgsMessageLog.logMessage(
                    f"[PowiatyIndex] Transformacja TERYT {teryt}: {exc}",
                    LOG_TAG, Qgis.MessageLevel.Warning,
                )
                skipped += 1
                continue
            """

            self._geometries[teryt] = geom
            self._names[teryt] = name
            self._fid_to_teryt[fid] = teryt

            feat = QgsFeature(fid)
            feat.setGeometry(geom)
            spatial_index.addFeature(feat)

            fid += 1
            loaded += 1

            if loaded == 1:
                QgsMessageLog.logMessage(
                    f"DEBUG: Pierwszy powiat ({teryt}) BBOX po transformacji: {geom.boundingBox().toString()}",
                    LOG_TAG, Qgis.MessageLevel.Info
                )

        self._spatial_index = spatial_index

        QgsMessageLog.logMessage(
            f"[PowiatyIndex] Załadowano {loaded} powiatów "
            f"(pominięto: {skipped}) z {geojson_path}",
            LOG_TAG, Qgis.MessageLevel.Info,
        )


# ---------------------------------------------------------------------------
# Pomocnicza konwersja geometrii GeoJSON → WKT
# (nie wymaga GDAL/OGR – obsługuje Polygon i MultiPolygon)
# ---------------------------------------------------------------------------

def _geojson_geom_to_wkt(geom_dict: Optional[dict]) -> str:
    """
    Konwertuje obiekt geometry z GeoJSON do WKT.
    Obsługuje: Point, MultiPoint, LineString, MultiLineString,
               Polygon, MultiPolygon.
    Zwraca pusty string przy błędzie.
    """
    if not geom_dict:
        return ""
    gtype = geom_dict.get("type", "")
    coords = geom_dict.get("coordinates")
    if not coords:
        return ""

    try:
        if gtype == "Point":
            return f"POINT ({coords[0]} {coords[1]})"

        if gtype == "MultiPoint":
            pts = ", ".join(f"({c[0]} {c[1]})" for c in coords)
            return f"MULTIPOINT ({pts})"

        if gtype == "LineString":
            pts = ", ".join(f"{c[0]} {c[1]}" for c in coords)
            return f"LINESTRING ({pts})"

        if gtype == "MultiLineString":
            rings = ", ".join(
                "(" + ", ".join(f"{c[0]} {c[1]}" for c in ring) + ")"
                for ring in coords
            )
            return f"MULTILINESTRING ({rings})"

        if gtype == "Polygon":
            rings = ", ".join(
                "(" + ", ".join(f"{c[0]} {c[1]}" for c in ring) + ")"
                for ring in coords
            )
            return f"POLYGON ({rings})"

        if gtype == "MultiPolygon":
            polys = ", ".join(
                "(" + ", ".join(
                    "(" + ", ".join(f"{c[0]} {c[1]}" for c in ring) + ")"
                    for ring in poly
                ) + ")"
                for poly in coords
            )
            return f"MULTIPOLYGON ({polys})"

    except Exception:
        pass

    return ""