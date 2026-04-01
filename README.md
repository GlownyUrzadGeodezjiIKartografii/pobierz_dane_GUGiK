# ![ikona](icon.png) Pobierz dane GUGiK

Wtyczka służy do pobierania obiektów ze zbiorczych usług WFS udostępnianych przez Główny Urząd Geodezji i Kartografii.

## Lista zmian
### 1.2
#### Poprawiono:
- mechanizm pobierania obiektów dla warstw poligonowych
- style qml dla poszczególnych warstw
#### Dodano obsługę pobierania:
- PRG między innymi: adrey, ulice, place
- PRNG
  - O1_UrzedoweNazwyObiektowFizjograficznych (PRNG)
  - O2_ZestandaryzowaneNazwyObiektowFizjograficznych (PRNG)
  - O3_PozostaleNazwyObiektowFizjograficznych (PRNG)
  - M1_UrzedoweNazwyMiejscowosci (PRNG)
  - M2_PozostaleNazwyMiejscowosci (PRNG)
- Dane ze skorowidzów:
  - ortofotomapy
  - prawdziwej ortofotomapy
  - LIDAR (KRON86, EVRF2007)
  - NMT (KRON86, EVRF2007)
  - NMPT (KRON86, EVRF2007)
- Danych z powiatowych usług WFS zgłoszonych do EZiUDP
### 1.1
#### Dodano:
- obsługę pobierania budynków (EGIB), dzialek (RCN), budynków (RCN), lokali(RCN)
- nowa metoda pobieranie danych z OPENDATA w przypadku całej polski, województwa
- wybór metody pobierania pomiedzy opendata i wfs dla powiatu
- poprawa obłsugi enklaw w obiektach PRG
#### Poprawiono:
- działanie wtyczki dla wersji QGIS z biblioteką urllib3 < 1.26.0
### 1.0 Pierwsze publiczne wydanie wtyczki
#### Dodano możliwość pobierania działek po przez:
- Wybranie obszaru administracyjnego (województwa, powiaty, gminy, obręby) - W Przypadku województw i powiatów sugerowane jest pobieranie paczek z modułu Pobierz dane w geoportalu krajowym (https://mapy.geoportal.gov.pl/imapnext/imap/index.html?moduleId=modulPD)
- Identyfikator działki lub listę indentyfikatorów działek rozdzielonych znakiem nowej linii
- Wskazanie zasięgu w oknie mapy
- Wskazanie warstwy poligonowej (powyżej 100 wierzchołków pojedynczego obiektu wykorzystywany jest zasięg obiektu)
- Zakres aktualnego widoku mapy
- Wskazanie nazwy obrębu oraz numeru działki
---

[![GUGiK © 2026](https://www.geoportal.gov.pl/wp-content/themes/geoportal/assets/images/gugik-logo.png)](https://www.gov.pl/web/gugik)

[GUGiK 2026](https://www.gov.pl/web/gugik)
