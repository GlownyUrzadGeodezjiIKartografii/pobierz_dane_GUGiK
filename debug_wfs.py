import requests
import time

url = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/EGIB/WFS/UslugaZbiorcza"

session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Content-Type': 'application/xml',
})

xml = """<wfs:GetCapabilities service="WFS" xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://www.opengis.net/wfs/2.0 http://schemas.opengis.net/wfs/2.0/wfs.xsd"/>"""

print(f"Sending GetCapabilities POST to {url}...")
try:
    response = session.post(url, data=xml, timeout=30, verify=False)
    print(f"Status Code: {response.status_code}")
    print(f"Content (first 500 chars): {response.text[:500]}")
except Exception as e:
    print(f"Error: {e}")
