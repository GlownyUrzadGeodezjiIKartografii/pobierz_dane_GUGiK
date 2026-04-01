# -*- coding: utf-8 -*-
"""
Centralny rejestr usług skorowidzowych WFS GUGiK.

Żeby dodać nową usługę wystarczy dopisać jeden wpis do SKOROWIDZ_SERVICES.
Reszta kodu (dockwidget, update_ui, run_skorowidz) adaptuje się automatycznie.

Struktura wpisu:
    label       – tekst wyświetlany w cmbObjType (musi być unikalny)
    url         – adres endpointu WFS
    layer_name  – pełna nazwa typenames (bez roku / sufiksu)
    layer_suffix– opcjonalny sufiks dopisywany po roku, domyślnie ""
    year_range  – (rok_od, rok_do) zakres comboboxa, None = brak filtra roku
    year_step   – krok iteracji lat (domyślnie -1 = malejąco)
"""
from dataclasses import dataclass, field
from typing import Optional, Tuple

@dataclass(frozen=True)
class SkorowidzService:
    label: str
    url: str
    layer_name: str               # prefix nazwy warstwy, np. "gugik:SkorowidzOrtofomapy"
    year_range: Optional[Tuple[int, int]] = None  # (max_rok, min_rok) włącznie
    layer_suffix: str = ""        # np. "" lub "_KRON86" – dopisywane po roku


# -----------------------------------------------------------------------
# REJESTR – dodaj nowy wpis tutaj
# -----------------------------------------------------------------------
SKOROWIDZ_SERVICES: list[SkorowidzService] = [

    SkorowidzService(
        label="ortofotomapa",
        url="https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WFS/Skorowidze",
        layer_name="gugik:SkorowidzOrtofomapy",
        year_range=(2025, 1995),
    ),

    SkorowidzService(
        label="prawdziwa ortofotomapa",
        url="https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WFS/SkorowidzPrawdziwejOrtofotomapy",
        layer_name="gugik:SkorowidzPrawdziwejOrtofomapy",
        year_range=None,   # Ta usługa nie ma podziału rocznikowego
    ),

    # SkorowidzService(
    #     label="zdjęcia lotnicze – środki rzutów (ZDJ)",
    #     url="https://mapy.geoportal.gov.pl/wss/service/PZGIK/ZDJ/WFS/Skorowidze_Srodki_Rzutow_Zdjec",
    #     layer_name="gugik:SkorowidzZdjecLotniczych",
    #     year_range=(2025, 2000),
    # ),

    SkorowidzService(
        label="LIDAR KRON86",
        url="https://mapy.geoportal.gov.pl/wss/service/PZGIK/DanePomiaroweLidarKRON86/WFS/Skorowidze",
        layer_name="gugik:SkorowidzDanychPomiarowychLIDAR",
        year_range=(2019, 2010),
    ),

    SkorowidzService(
        label="LIDAR EVRF2007",
        url="https://mapy.geoportal.gov.pl/wss/service/PZGIK/DanePomiaroweLidarEVRF2007/WFS/Skorowidze",
        layer_name="gugik:SkorowidzDanychPomiarowychLIDAR",
        year_range=(2025, 2018),
    ),

    SkorowidzService(
        label="NMT KRON86",
        url="https://mapy.geoportal.gov.pl/wss/service/PZGIK/NumerycznyModelTerenuKRON86/WFS/Skorowidze",
        layer_name="gugik:SkorowidzNMT",
        year_range=(2019, 2000),
    ),

    SkorowidzService(
        label="NMT EVRF2007",
        url="https://mapy.geoportal.gov.pl/wss/service/PZGIK/NumerycznyModelTerenuEVRF2007/WFS/Skorowidze",
        layer_name="gugik:SkorowidzNMT",
        year_range=(2025, 2018),
    ),

    SkorowidzService(
        label="NMPT KRON86",
        url="https://mapy.geoportal.gov.pl/wss/service/PZGIK/NumerycznyModelPokryciaTerenuKRON86/WFS/Skorowidze",
        layer_name="gugik:SkorowidzNMPT",
        year_range=(2019, 2010),
    ),

    SkorowidzService(
        label="NMPT EVRF2007",
        url="https://mapy.geoportal.gov.pl/wss/service/PZGIK/NumerycznyModelPokryciaTerenuEVRF2007/WFS/Skorowidze",
        layer_name="gugik:SkorowidzNMPT",
        year_range=(2025, 2019),
    ),

    # SkorowidzService(
    #     label="BDOT10k (skorowidz powiatów)",
    #     url="https://mapy.geoportal.gov.pl/wss/service/PZGIK/BDOT/WFS/PobieranieBDOT10k",
    #     layer_name="ms:BDOT10k_powiaty",
    #     year_range=None,
    # ),
]

# Słownik label → SkorowidzService do szybkiego wyszukiwania
SKOROWIDZ_BY_LABEL: dict[str, SkorowidzService] = {
    s.label: s for s in SKOROWIDZ_SERVICES
}