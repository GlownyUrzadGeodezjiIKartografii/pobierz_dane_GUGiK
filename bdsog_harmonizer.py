# -*- coding: utf-8 -*-
"""
Harmonizator schematów dla danych pobieranych z powiatowych usług WFS.

Zmiany względem poprzedniej wersji:
  - BdsogHarmonizer zastąpiony dwoma wyspecjalizowanymi klasami:
      BdsogPoziomHarmonizer  → warstwa "Osnowa_pozioma"
      BdsogWysokHarmonizer   → warstwa "Osnowa_wysokosciowa"
  - Schemat oparty na rzeczywistych polach z geoportal2/BDSOG.
  - SWAP_XY=False – parser GML w download_task.py już zamienia kolejność
    N-first (Y,X) na E,N (X,Y) przy tworzeniu QgsPointXY.
"""
from typing import Optional
import os 

from qgis.core import (
    QgsFeature, QgsField, QgsFields, QgsGeometry,
    QgsVectorLayer, QgsProject,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsMessageLog, Qgis,
)
from qgis.PyQt.QtCore import QVariant

LOG_TAG = "PD_GUGiK"
TARGET_CRS = "EPSG:2180"


def _layer_name_to_harmonizer_key(layer_name: str, zbior: str = "bdsog") -> str:
    """
    Mapuje nazwę warstwy WFS (np. 'ewns:Osnowa_wysokosciowa')
    na klucz harmonizatora ('bdsog_wysok' lub 'bdsog_poziom').
    """
    norm = layer_name.lower().replace(":", "").replace("_", "").replace("-", "")
    if zbior == "bdsog":
        if "wysok" in norm:
            return "bdsog_wysok"
        return "bdsog_poziom"
    if zbior == "egib":
        if "budynki" in norm:
            return "egib_bud"
        if "dzialki" in norm:
                return "egib_dzi"
        if "punkty" in norm:
                return "egib_pkt"


def _coerce(value, qvariant_type: int):
    """
    Konwertuje wartość z GML do typu pola QgsField.
      - None / pusty string / "null" → None
      - Double → float (błąd → None)
      - Int/LongLong → int (błąd → None)
      - pozostałe → str
    """
    if value is None:
        return None
    s = str(value).strip()
    if s == "" or s.lower() == "null":
        return None
    if qvariant_type == QVariant.Double:
        try:
            return float(s.replace(",", "."))
        except (ValueError, TypeError):
            return None
    if qvariant_type in (QVariant.Int, QVariant.LongLong,
                         QVariant.UInt, QVariant.ULongLong):
        try:
            return int(float(s.replace(",", ".")))
        except (ValueError, TypeError):
            return None
    return s


class BaseHarmonizer:
    """
    Bazowa klasa harmonizatora.

    Podklasy ustawiają:
        LAYER_NAME    – nazwa warstwy QGIS
        GEOM_TYPE     – typ geometrii ("Point", "MultiLineString" itp.)
        TARGET_SCHEMA – lista (nazwa_pola, QVariant.Type)
        FIELD_VARIANTS – {pole_docelowe: [możliwe_nazwy_źródłowe]}
        UNIQUE_FIELD  – pole do deduplikacji lub None
    """

    LAYER_NAME: str = "Dane_WFS"
    GEOM_TYPE: str = "Point"
    TARGET_SCHEMA: list = []
    FIELD_VARIANTS: dict = {}
    UNIQUE_FIELD: Optional[str] = None

    def build_target_fields(self) -> QgsFields:
        fields = QgsFields()
        for name, qtype in self.TARGET_SCHEMA:
            fields.append(QgsField(name, qtype))
        return fields

    def harmonize(
        self,
        features_data: list,
        teryt: str,
        wfs_url: str,
        source_crs: str = TARGET_CRS,
    ) -> list:
        if not features_data:
            return []

        target_fields = self.build_target_fields()

        transform = None
        if source_crs and source_crs != TARGET_CRS:
            transform = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem(source_crs),
                QgsCoordinateReferenceSystem(TARGET_CRS),
                QgsProject.instance(),
            )

        sample_keys = list(features_data[0].get("attrs", {}).keys())
        mapping = self._detect_mapping(sample_keys)
        QgsMessageLog.logMessage(
            f"[Harmonizer] {self.__class__.__name__} TERYT={teryt} "
            f"pola źródłowe: {sample_keys} | mapowanie: {mapping}",
            LOG_TAG, Qgis.MessageLevel.Info,
        )

        harmonized = []
        for fd in features_data:
            feat = QgsFeature(target_fields)

            geom = QgsGeometry.fromWkt(fd.get("geom", ""))
            if geom and not geom.isEmpty() and transform:
                geom.transform(transform)
            feat.setGeometry(geom)

            attrs = fd.get("attrs", {})
            for target_name, src_name in mapping.items():
                idx = target_fields.indexOf(target_name)
                if idx < 0:
                    continue
                feat.setAttribute(
                    idx,
                    _coerce(attrs.get(src_name), target_fields.at(idx).type())
                )

            for meta_field, value in [("teryt_powiatu", teryt), ("zrodlo_wfs", wfs_url)]:
                idx = target_fields.indexOf(meta_field)
                if idx >= 0:
                    feat.setAttribute(idx, value)

            harmonized.append(feat)

        return harmonized

    def get_or_create_layer(
        self,
        layer_name: Optional[str] = None,
        load_style: bool = True,
        plugin_dir: str = "",
        ) -> QgsVectorLayer:
        """
        Zwraca istniejącą warstwę o podanej nazwie lub tworzy nową.
 
        :param layer_name:  nazwa warstwy (domyślnie LAYER_NAME klasy)
        :param load_style:  czy próbować załadować styl QML z katalogu data/
        :param plugin_dir:  ścieżka do katalogu wtyczki (potrzebna do znalezienia QML)
        """
        name = layer_name or self.LAYER_NAME
        existing = QgsProject.instance().mapLayersByName(name)
        if existing:
            return existing[0]
 
        vl = QgsVectorLayer(f"{self.GEOM_TYPE}?crs={TARGET_CRS}", name, "memory")
        pr = vl.dataProvider()
        pr.addAttributes(self.build_target_fields().toList())
        vl.updateFields()

        QgsMessageLog.logMessage(
                    f"[Harmonizer] Layer name: {self.LAYER_NAME.lower()}",
                    LOG_TAG, Qgis.MessageLevel.Info,
                )
 
        # Załaduj styl QML jeśli dostępny – szukamy po bazowej nazwie klasy
        # (np. "Osnowa_pozioma" dla BdsogPoziomHarmonizer, bez sufiksu _TERYT)
        if load_style and plugin_dir:
            # Użyj bazowej nazwy klasy, nie nazwy z TERYT-em na końcu
            style_base = self.LAYER_NAME.lower()
            
            style_path = os.path.join(plugin_dir, "data", f"{style_base}.qml")
            if os.path.exists(style_path):
                vl.loadNamedStyle(style_path)
                QgsMessageLog.logMessage(
                    f"[Harmonizer] Załadowano styl: {style_path}",
                    LOG_TAG, Qgis.MessageLevel.Info,
                )
 
        QgsProject.instance().addMapLayer(vl)
        return vl

    def add_to_layer(
        self,
        features: list,
        layer_name: Optional[str] = None,
        load_style: bool = True,
        plugin_dir: str = "",
        ) -> int:
        if not features:
            return 0
        vl = self.get_or_create_layer(layer_name, load_style, plugin_dir)
        pr = vl.dataProvider()

        existing_ids: set = set()
        if self.UNIQUE_FIELD:
            idx = vl.fields().indexOf(self.UNIQUE_FIELD)
            if idx >= 0:
                existing_ids = {f.attribute(self.UNIQUE_FIELD) for f in vl.getFeatures()}

        to_add = []
        for feat in features:
            if self.UNIQUE_FIELD:
                uid = feat.attribute(self.UNIQUE_FIELD)
                if uid in existing_ids:
                    continue
                existing_ids.add(uid)
            to_add.append(feat)

        if to_add:
            pr.addFeatures(to_add)
            vl.updateExtents()
            vl.triggerRepaint()

        return len(to_add)

    def _detect_mapping(self, source_keys: list) -> dict:
        def normalize(s: str) -> str:
            return s.split(":")[-1].lower().replace("_", "").replace("-", "")

        norm_src = {normalize(k): k for k in source_keys}
        lower_src = {k.lower(): k for k in source_keys}
        mapping: dict = {}

        for target_name, _ in self.TARGET_SCHEMA:
            if target_name in ("teryt_powiatu", "zrodlo_wfs"):
                continue

            candidates = self.FIELD_VARIANTS.get(target_name, [target_name])
            found = False

            for candidate in candidates:
                # 1. Dokładne (case-insensitive)
                if candidate.lower() in lower_src:
                    mapping[target_name] = lower_src[candidate.lower()]
                    found = True
                    break
                # 2. Znormalizowane
                norm_c = normalize(candidate)
                if norm_c in norm_src:
                    mapping[target_name] = norm_src[norm_c]
                    found = True
                    break

            if not found:
                norm_target = normalize(target_name)
                if norm_target in norm_src:
                    mapping[target_name] = norm_src[norm_target]

        return mapping


# ---------------------------------------------------------------------------
# BDSOG – osnowa pozioma
# ---------------------------------------------------------------------------

class BdsogPoziomHarmonizer(BaseHarmonizer):
    """Osnowa pozioma (ewns:Osnowa_pozioma z geoportal2)."""

    LAYER_NAME = "Osnowa_pozioma"
    GEOM_TYPE = "Point"
    UNIQUE_FIELD = "nr_punktu"

    TARGET_SCHEMA = [
        ("nr_punktu",          QVariant.String),
        ("rodzaj_osnowy",      QVariant.String),
        ("x2000",              QVariant.Double),
        ("y2000",              QVariant.Double),
        ("strefa2000",         QVariant.String),
        ("mp",                 QVariant.String),
        ("typ_stabilizacji",   QVariant.String),
        ("stan_znaku",         QVariant.String),
        ("opis_topograficzny", QVariant.String),
        ("teryt_powiatu",      QVariant.String),
        ("zrodlo_wfs",         QVariant.String),
    ]

    FIELD_VARIANTS = {
        "nr_punktu":          ["NR_PUNKTU", "nr_punktu", "nrPunktu", "numerPunktu", "numer"],
        "rodzaj_osnowy":      ["RODZAJ_OSNOWY", "rodzaj_osnowy", "rodzajOsnowy", "rodzaj"],
        "x2000":              ["X2000", "x2000", "wspolrzednaX", "x"],
        "y2000":              ["Y2000", "y2000", "wspolrzednaY", "y"],
        "strefa2000":         ["STREFA2000", "strefa2000", "strefa"],
        "mp":                 ["MP", "mp", "bladPolozenia", "dokladnoscPolozenia"],
        "typ_stabilizacji":   ["TYP_STABILIZACJI", "typ_stabilizacji", "typStabilizacji", "stabilizacja"],
        "stan_znaku":         ["STAN_ZNAKU", "stan_znaku", "stanZnaku", "stan"],
        "opis_topograficzny": ["OPIS_TOPOGRAFICZNY", "opis_topograficzny", "opisTopograficzny", "opis"],
    }


# ---------------------------------------------------------------------------
# BDSOG – osnowa wysokościowa
# ---------------------------------------------------------------------------

class BdsogWysokHarmonizer(BaseHarmonizer):
    """Osnowa wysokościowa (ewns:Osnowa_wysokosciowa z geoportal2)."""

    LAYER_NAME = "Osnowa_wysokosciowa"
    GEOM_TYPE = "Point"
    UNIQUE_FIELD = "nr_punktu"

    TARGET_SCHEMA = [
        ("nr_punktu",          QVariant.String),
        ("rodzaj_osnowy",      QVariant.String),
        ("h_plevrf2007nh",     QVariant.Double),
        ("mh_plevrf2007nh",    QVariant.String),
        ("h_plkron86nh",       QVariant.Double),
        ("mh_plkron86nh",      QVariant.String),
        ("typ_stabilizacji",   QVariant.String),
        ("stan_znaku",         QVariant.String),
        ("opis_topograficzny", QVariant.String),
        ("teryt_powiatu",      QVariant.String),
        ("zrodlo_wfs",         QVariant.String),
    ]

    FIELD_VARIANTS = {
        "nr_punktu":          ["NR_PUNKTU", "nr_punktu", "nrPunktu", "numerPunktu", "numer"],
        "rodzaj_osnowy":      ["RODZAJ_OSNOWY", "rodzaj_osnowy", "rodzajOsnowy", "rodzaj"],
        "h_plevrf2007nh":     ["H_PLEVRF2007NH", "h_plevrf2007nh", "wysokoscEVRF", "wysokosc"],
        "mh_plevrf2007nh":    ["MH_PLEVRF2007NH", "mh_plevrf2007nh", "bladWysokosciEVRF"],
        "h_plkron86nh":       ["H_PLKRON86NH", "h_plkron86nh", "wysokoscKron", "h"],
        "mh_plkron86nh":      ["MH_PLKRON86NH", "mh_plkron86nh", "bladWysokosciKron", "mh"],
        "typ_stabilizacji":   ["TYP_STABILIZACJI", "typ_stabilizacji", "typStabilizacji", "stabilizacja"],
        "stan_znaku":         ["STAN_ZNAKU", "stan_znaku", "stanZnaku", "stan"],
        "opis_topograficzny": ["OPIS_TOPOGRAFICZNY", "opis_topograficzny", "opisTopograficzny", "opis"],
    }


# ---------------------------------------------------------------------------
# Pozostałe zbiory
# ---------------------------------------------------------------------------
class EgibPktHarmonizer(BaseHarmonizer):
    LAYER_NAME = "Punkty graniczne"
    GEOM_TYPE = "Point"
    UNIQUE_FIELD = "NUMER_PUNKTU"

    TARGET_SCHEMA = [
        ("NUMER_PUNKTU",    QVariant.String),
        ("STABILIZACJA",  QVariant.String),
        ("ISD",      QVariant.String),
        ("SPD",      QVariant.String),
    ]

    FIELD_VARIANTS = {
        "NUMER_PUNKTU":   ["NUMER_PUNKTU", "numer_punktu"],
        "STABILIZACJA": ["STABILIZACJA", "stabilizacja"],
        "ISD":     ["ISD", "isd"],
        "SPD":     ["SPD", "spd"],
    }

class EgibBudHarmonizer(BaseHarmonizer):
    LAYER_NAME = "Budynki"
    GEOM_TYPE = "Polygon"
    UNIQUE_FIELD = "ID_BUDYNKU"

    TARGET_SCHEMA = [
        ("ID_BUDYNKU",    QVariant.String),
        ("RODZAJ",  QVariant.String),
        ("KONDYGNACJE_NADZIEMNE",      QVariant.String),
        ("KONDYGNACJE_PODZIEMNE",      QVariant.String),
    ]

    FIELD_VARIANTS = {
        "ID_BUDYNKU":   ["ID_BUDYNKU", "id_budynku"],
        "RODZAJ": ["RODZAJ", "rodzaj", "funkcja", "FUNKCJA"],
        "KONDYGNACJE_NADZIEMNE":     ["KONDYGNACJE_NADZIEMNE", "kondygnacje_nadziemne"],
        "KONDYGNACJE_PODZIEMNE":     ["KONDYGNACJE_PODZIEMNE", "kondygnaje_podziemne"],
    }

class EgibDziHarmonizer(BaseHarmonizer):
    LAYER_NAME = "Dzialki"
    GEOM_TYPE = "Polygon"
    UNIQUE_FIELD = "ID_DZIALKI"

    TARGET_SCHEMA = [
        ("ID_DZIALKI",    QVariant.String),
        ("NUMER_DZIALKI",  QVariant.String),
        ("NUMER_OBREBU",      QVariant.String),
        ("NUMER_JEDNOSTKI",      QVariant.String),
        ("NAZWA_GMINY",      QVariant.String),
        ("POLE_EWIDENCYJNE",      QVariant.String),
        ("KLASOUZYTKI_EGIB",      QVariant.String),
        ("GRUPA_REJESTROWA",      QVariant.String),
        ("DATA",      QVariant.String)
    ]

    FIELD_VARIANTS = {
        "ID_DZIALKI":   ["ID_DZIALKI", "id_dzialki"],
        "NUMER_DZIALKI": ["NUMER_DZIALKI", "numer_dzialki"],
        "NUMER_OBREBU":     ["NUMER_OBREBU", "numer_obrebu"],
        "NUMER_JEDNOSTKI":     ["NUMER_JEDNOSTKI", "numer_jednostki"],
        "NAZWA_OBREBU":     ["NAZWA_OBREBU", "nazwa_obrebu"],
        "NAZWA_GMINY":     ["NAZWA_GMINY", "nazwa_gminy"],
        "POLE_EWIDENCYJNE":     ["POLE_EWIDENCYJNE", "pole_ewidencyjne"],
        "KLASOUZYTKI_EGIB":     ["KLASOUZYTKI_EGIB", "klasouzytki_egib"],
        "GRUPA_REJESTROWA":     ["GRUPA_REJESTROWA", "grupa_rejestrowa"],
        "DATA":     ["DATA", "data"],
    }

class RCNHarmonizer(BaseHarmonizer):
    LAYER_NAME = "Transakcje"
    GEOM_TYPE = "Point"
    UNIQUE_FIELD = "lokalny_id"

    TARGET_SCHEMA = [
        ("serwis_rcn",    QVariant.String),
        ("teryt",  QVariant.String),
        ("lokalny_id",      QVariant.String),
        ("data_transakcji",      QVariant.String),
        ("rodzaj_transakcji",      QVariant.String),
        ("rodzaj_nieruchomosci",      QVariant.String),
        ("link",      QVariant.String),
    ]

    FIELD_VARIANTS = {
        "serwis_rcn":   ["serwis_rcn", "SERWIS_RCN", "serwisRCN"],
        "teryt": ["teryt", "TERYT"],
        "lokalny_id":     ["lokalny_id", "LOKALNY_ID", "lokalnyId"],
        "data_transakcji":     ["data_transakcji", "DATA_TRANSAKCJI", "dataTransakcji"],
        "rodzaj_transakcji":     ["rodzaj_transakcji", "RODZAJ_TRANSAKCJI", "rodzajTransakcji"],
        "rodzaj_nieruchomosci":     ["rodzaj_nieruchomosci", "RODZAJ_NIERUCHOMOSCI", "rodzajNieruchomosci"],
        "link":     ["link", "LINK"],
    }

class BDOT500BudHarmonizer(BaseHarmonizer):
    LAYER_NAME = "Budynki i obiekty towarzyszące"
    GEOM_TYPE = "Polygon"
    UNIQUE_FIELD = "ID_IIP"

    TARGET_SCHEMA = [
        ("NAZWA_OBIEKTU",    QVariant.String),
        ("KOD_OBKIETU",  QVariant.String),
        ("ID_IIP",      QVariant.String),
        ("ETYKIETA",      QVariant.String),
        ("DATA",      QVariant.String)
    ]

    FIELD_VARIANTS = {
        "NAZWA_OBIEKTU":   ["NAZWA_OBIEKTU", "nazwa_obiektu"],
        "KOD_OBKIETU": ["KOD_OBKIETU", "kod_obiektu"],
        "ID_IIP":     ["ID_IIP", "id_iip"],
        "ETYKIETA":     ["ETYKIETA", "etykieta"],
        "DATA":     ["DATA", "data"],
    }





# ---------------------------------------------------------------------------
# Fabryka
# ---------------------------------------------------------------------------

_HARMONIZER_MAP: dict = {
    "bdsog_poziom": BdsogPoziomHarmonizer,
    "bdsog_wysok":  BdsogWysokHarmonizer,
    "bdsog":        BdsogPoziomHarmonizer,   # alias – gdy warstwa nieznana
    "egib_bud": EgibBudHarmonizer,
    "egib_dzi": EgibDziHarmonizer,
    "egib_pkt": EgibPktHarmonizer,
    "rcn": RCNHarmonizer,
    "bdot500": BDOT500BudHarmonizer
}


def get_harmonizer(zbior: str, layer_name: str = "") -> Optional[BaseHarmonizer]:
    """
    Zwraca instancję harmonizatora.

    :param zbior:      klucz zbioru, np. "bdsog"
    :param layer_name: nazwa warstwy WFS (np. "ewns:Osnowa_wysokosciowa")
                       – dla bdsog decyduje o wyborze poziom/wysok
    """
    if zbior == "bdsog" and layer_name:
        key = _layer_name_to_harmonizer_key(layer_name)
    elif zbior == "egib" and layer_name:
        key = _layer_name_to_harmonizer_key(layer_name, zbior)
    else:
        key = zbior

    cls = _HARMONIZER_MAP.get(key)
    if cls is None:
        QgsMessageLog.logMessage(
            f"[Harmonizer] Brak harmonizatora dla zbioru='{zbior}' "
            f"layer_name='{layer_name}' key='{key}'",
            LOG_TAG, Qgis.MessageLevel.Warning,
        )
        return None
    return cls()