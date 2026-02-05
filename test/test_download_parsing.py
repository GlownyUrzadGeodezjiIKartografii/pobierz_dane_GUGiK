
import unittest
from qgis.core import QgsApplication
from pobieranie_egib.download_task import DownloadTask

class TestDownloadParsing(unittest.TestCase):
    def setUp(self):
        # We need QGIS app for QDomDocument and QgsGeometry to work? 
        # QDomDocument is QtXml, doesn't need QGIS app running, but QgsGeometry does.
        # Assuming QGIS_APP is set up in test runner or globally if needed.
        pass

    def test_parse_gml_with_namespace(self):
        gml = """<?xml version='1.0' encoding="UTF-8" ?>
<wfs:FeatureCollection
   xmlns:ms="http://mapserver.gis.umn.edu/mapserver"
   xmlns:gml="http://www.opengis.net/gml/3.2"
   xmlns:wfs="http://www.opengis.net/wfs/2.0">
    <wfs:member>
      <ms:dzialki>
        <ms:geom>
          <gml:Polygon srsName="EPSG:2180">
             <gml:exterior><gml:LinearRing><gml:posList>0 0 0 10 10 10 10 0 0 0</gml:posList></gml:LinearRing></gml:exterior>
          </gml:Polygon>
        </ms:geom>
        <ms:ID_DZIALKI>123.456</ms:ID_DZIALKI>
      </ms:dzialki>
    </wfs:member>
</wfs:FeatureCollection>
"""
        task = DownloadTask(None)
        features = task._parse_gml(gml)
        
        self.assertEqual(len(features), 1)
        self.assertIsNotNone(features[0]['geom'])
        self.assertEqual(features[0]['attrs'].get('ID_DZIALKI'), '123.456')

    def test_parse_gml_empty(self):
        gml = "<root></root>"
        task = DownloadTask(None)
        features = task._parse_gml(gml)
        self.assertEqual(len(features), 0)

if __name__ == '__main__':
    unittest.main()
