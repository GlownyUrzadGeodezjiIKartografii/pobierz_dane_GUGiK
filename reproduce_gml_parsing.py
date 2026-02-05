
import sys
from qgis.core import QgsApplication, QgsOgcUtils, QgsGeometry
from qgis.PyQt.QtXml import QDomDocument, QDomNode

# Initialize QGIS Application
qgs = QgsApplication([], False)
qgs.initQgis()

xml_content = """<?xml version='1.0' encoding="UTF-8" ?>
<wfs:FeatureCollection
   xmlns:ms="http://mapserver.gis.umn.edu/mapserver"
   xmlns:gml="http://www.opengis.net/gml/3.2"
   xmlns:wfs="http://www.opengis.net/wfs/2.0"
   xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
   xsi:schemaLocation="http://mapserver.gis.umn.edu/mapserver https://mapy.geoportal.gov.pl/wss/service/PZGIK/EGIB/WFS/UslugaZbiorcza?SERVICE=WFS&amp;VERSION=2.0.0&amp;REQUEST=DescribeFeatureType&amp;TYPENAME=ms:dzialki&amp;OUTPUTFORMAT=application%2Fgml%2Bxml%3B%20version%3D3.2 http://www.opengis.net/wfs/2.0 http://schemas.opengis.net/wfs/2.0/wfs.xsd http://www.opengis.net/gml/3.2 http://schemas.opengis.net/gml/3.2.1/gml.xsd"
   timeStamp="2026-01-22T15:47:45" numberMatched="unknown" numberReturned="1">
    <wfs:member>
      <ms:dzialki gml:id="dzialki.022602_1.0003.59">
        <ms:geom>
          <gml:Polygon gml:id="dzialki.022602_1.0003.59.1" srsName="urn:ogc:def:crs:EPSG::2180">
            <gml:exterior>
              <gml:LinearRing>
                <gml:posList srsDimension="2">366372.232396 284207.279042 366371.030839 284212.981591 366366.270562 284221.514638 366358.338632 284228.893734 366331.386864 284212.786887 366330.750453 284211.600669 366330.674379 284210.054407 366332.384717 284202.547023 366320.608112 284179.184877 366317.823658 284173.675975 366317.177123 284171.387690 366318.359943 284169.709660 366327.269243 284163.875800 366343.031441 284153.377938 366344.123524 284153.367272 366344.732087 284153.961162 366347.959716 284158.502818 366367.251860 284186.369599 366369.690056 284191.248817 366372.371616 284199.936503 366372.232396 284207.279042 </gml:posList>
              </gml:LinearRing>
            </gml:exterior>
          </gml:Polygon>
        </ms:geom>
      </ms:dzialki>
    </wfs:member>
</wfs:FeatureCollection>
"""

def test_parsing():
    doc = QDomDocument()
    if not doc.setContent(xml_content, True):
        print("SetContent failed")
        return

    root = doc.documentElement()
    members = root.elementsByTagNameNS('http://www.opengis.net/wfs/2.0', 'member')
    
    print(f"Members found: {members.count()}")

    for i in range(members.count()):
        member_node = members.item(i)
        
        feature_elem = None
        child = member_node.firstChild()
        while not child.isNull():
            if child.nodeType() == QDomNode.ElementNode:
                feature_elem = child.toElement()
                break
            child = child.nextSibling()
        
        if feature_elem is None:
            print("No feature element")
            continue

        prop = feature_elem.firstChild()
        while not prop.isNull():
            if prop.nodeType() == QDomNode.ElementNode:
                elem = prop.toElement()
                name = elem.localName()
                if not name:
                    name = elem.tagName().split(':')[-1]
                
                print(f"Property: {name}, Tag: {elem.tagName()}")
                
                if name == 'geom':
                    geom_child = elem.firstChild()
                    while not geom_child.isNull():
                        if geom_child.nodeType() == QDomNode.ElementNode:
                            g_elem = geom_child.toElement()
                            print(f"  Geom Child: {g_elem.tagName()}")
                            
                            try:
                                ggeom = QgsOgcUtils.geometryFromGML(g_elem)
                                if ggeom and not ggeom.isEmpty():
                                    print(f"  Parsed Geometry: {ggeom.asWkt()[:50]}...")
                                else:
                                    print("  QgsOgcUtils returned invalid geometry")
                            except Exception as e:
                                print(f"  Exception parsing geometry: {e}")
                            
                            break
                        geom_child = geom_child.nextSibling()
            prop = prop.nextSibling()

test_parsing()
qgs.exitQgis()
