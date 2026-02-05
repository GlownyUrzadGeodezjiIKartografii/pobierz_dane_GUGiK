import unittest
from pobieranie_egib.wfs_client import WFSClient

class TestWFSClient(unittest.TestCase):
    def test_build_spatial_filter(self):
        client = WFSClient()
        wkt = "POLYGON((0 0, 0 10, 10 10, 10 0, 0 0))"
        xml = client.build_spatial_filter(wkt)
        self.assertIn("fes:Intersects", xml)
        self.assertIn("gml:Polygon", xml)
        # Check coordinates transformation (commas to spaces in posList?)
        self.assertIn("0 0 0 10 10 10 10 0 0 0", xml)

    def test_build_id_filter(self):
        client = WFSClient()
        ids = ["141201_1.0001.100"]
        xml = client.build_id_filter(ids)
        self.assertIn("fes:PropertyIsEqualTo", xml)
        self.assertIn("141201_1.0001.100", xml)
        
    def test_build_id_filter_wildcard(self):
        client = WFSClient()
        ids = ["141201_1.0001.*"]
        xml = client.build_id_filter(ids)
        self.assertIn("fes:PropertyIsLike", xml)

if __name__ == '__main__':
    unittest.main()
