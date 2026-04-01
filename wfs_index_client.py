# -*- coding: utf-8 -*-
"""
Klient WFS do pobierania skorowidzów GUGiK (Ortofotomapy, NMT itp.).
Dziedziczy z BaseWFSClient – nie duplikuje logiki sesji ani budowania filtrów.
"""
import urllib.parse
from qgis.core import QgsMessageLog, Qgis

from .base_wfs_client import BaseWFSClient

LOG_TAG = "PD_GUGiK"


class WFSIndexClient(BaseWFSClient):
    """
    Klient WFS dla usług skorowidzowych GUGiK (Ortofotomapy, NMT itp.).

    Różni się od klientów EGIB/RCN trzema rzeczami:
      - dynamiczna nazwa warstwy (przekazywana przy każdym download())
      - inne pole geometrii: gugik:msGeometry
      - inne URL (przekazywane w konstruktorze)
    """

    # Domyślne pole geometrii dla usług skorowidzowych GUGiK
    geom_field = "gugik:msGeometry"

    def __init__(self, url: str = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WFS/Skorowidze"):
        super().__init__()
        self.url = url
        # layer_name jest dynamiczne – ustawiane per-zapytanie w download()

    def download(self, filter_xml: str, layer_name: str,
                 start_index: int = 0, count: int = 1000,
                 attributes=None) -> str:
        """
        Pobiera obiekty z konkretnej warstwy skorowidza.

        :param layer_name: Nazwa typenames, np. 'gugik:SkorowidzOrtofomapy2025'
        """
        # Tymczasowo nadpisujemy layer_name na czas tego zapytania
        original = self.layer_name
        self.layer_name = layer_name
        try:
            return super().download(filter_xml, start_index=start_index,
                                    count=count, attributes=attributes)
        finally:
            self.layer_name = original