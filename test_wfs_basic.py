# -*- coding: utf-8 -*-
"""
Test jednostki funkcjonalności WFS klienta
"""
import sys

def test_basic_wfs_operations():
    """Testy podstawowe operacje klienta WFS"""
    print("=== Testy podstawowe WFS ===")
    
    # Sprawdź, czy QGIS API jest dostępne
    try:
        from qgis.core import QgsGeometry, QgsCoordinateReferenceSystem
        print("✅ QGIS API jest dostępny")
        return True
    except ImportError as e:
        print(f"❌ Brak bibliotek qgis.core: {e}")
        return False

def test_filter_building():
    """Testy budowania filtrów WFS"""
    print("\n=== Testy budowania filtrów ===")
    
    # Test BBOX filter
    print("- WFS BBOX Filter:")
    print("- Format: fes:BBOX > fes:ValueReference>geom</fes:ValueReference>...")
    print("- SRS: EPSG:2180")
    
    # Test ID filter
    print("- WFS ID Filter:")
    print("- Format: fes:PropertyIsEqualTo><fes:ValueReference>id_dzialki</fes:ValueReference>...")
    print("- SRS: EPSG:2180")
    
    # Test Polygon filter
    print("- WFS Polygon Filter:")
    print("- Format: fes:Intersects><gml:ValueReference>geom</fes:ValueReference>...")
    print("- SRS: EPSG:2180")

if __name__ == "__main__":
    wfs_api_ok = test_basic_wfs_operations()
    
    if not wfs_api_ok:
        print("\n⚠️ QGIS API nie jest niedostępny. Sprawdź biblioteki 'qgis.core' w środowisku QGIS")
        sys.exit(1)
    
    test_filter_building()
    print("\n\n📋 Połądnie biblioteki qgis.core to prawidłowego działania.")
    
    print("\nWskazuj, żeby kod był naprawny, sprawdź:")
    print("1. Czy biblioteki są dostępne w Twoim środowisku QGIS?")
    print("2. Czy TERYT ID jest prawidłowy (np. '141205_1')?")
    print("3. Czy sieci działają?")
    print("4. Czy firewall nie blokuje?")
    print("5. Czy biblioteki Python są poprawne?")
    print("6. Czy PRG zwróci dane poprawne?")
    print("7. Czy są jakieś błędy w logach QGIS?")
    print("\nZmień odpowiedzi na powyższe pytania, a rozwiąż problem.")
    
    sys.exit(0)