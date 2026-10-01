import csv
import re

from bs4 import BeautifulSoup
from lxml import etree

"""
This parses the Smithsonian Global Volcanism Program:
Quaternary Volcanoes Network Link 
GVPWorldVolcanoes.kml
from
https://volcano.si.edu/ge/PlacemarkLinks.cfm
Output is a CSV file
"""

# --- CONFIGURATION ---
# https://volcano.si.edu/
KML_FILE = "/Volumes/Mike EXT/map_data/data/external/GVP_volcanoes.kml"
OUTPUT_CSV = "gvp_volcanoes.csv"
# ---------------------

print(f"Reading {KML_FILE}...")

with open(KML_FILE, "rb") as f:
    raw_bytes = f.read()

# Handle encoding issues gracefully
try:
    decoded_text = raw_bytes.decode("utf-8")
except UnicodeDecodeError:
    print("Notice: Non-UTF8 bytes detected. Decoding with latin-1 fallback...")
    decoded_text = raw_bytes.decode("latin-1", errors="replace")

# Use a tolerant/recovering XML parser
parser = etree.XMLParser(recover=True, no_network=True)
tree = etree.fromstring(decoded_text.encode("utf-8"), parser=parser)

namespaces = {
    "kml": "http://www.opengis.net/kml/2.2", "gx": "http://www.google.com/kml/ext/2.2",
}

records = []

# Find all Placemark nodes (tree is the root element)
placemarks = tree.xpath("//kml:Placemark", namespaces=namespaces)
if not placemarks:
    # Some KML files don't use namespaces properly
    placemarks = tree.xpath("//Placemark")

for placemark in placemarks:
    name = placemark.findtext("kml:name", namespaces=namespaces) or placemark.findtext("name")
    if name:
        name = name.strip()

    coords_text = (placemark.findtext(".//kml:Point/kml:coordinates",
                                      namespaces=namespaces) or placemark.findtext(
        ".//Point/coordinates"))
    if not coords_text:
        continue

    coords = [c.strip() for c in coords_text.strip().split(",")]
    try:
        lon = float(coords[0])
        lat = float(coords[1])
    except (ValueError, IndexError):
        continue

    desc_html = (placemark.findtext("kml:description", namespaces=namespaces) or placemark.findtext(
        "description") or "")

    v_id = None
    country = None
    province = None
    landform = None
    v_type = None
    elevation = None
    summary = None

    if desc_html:
        soup = BeautifulSoup(desc_html, "html.parser")

        # Smithsonian Volcano ID (vn=329020)
        vn_match = re.search(r"vn=(\d+)", desc_html)
        if vn_match:
            v_id = int(vn_match.group(1))

        # Landform
        lf_match = re.search(r"Landform:</b>\s*([^<\n\r]+)", desc_html)
        if lf_match:
            landform = lf_match.group(1).strip()

        # Volcano Type
        vt_match = re.search(r"Volcano type:</b>\s*([^<\n\r]+)", desc_html)
        if vt_match:
            v_type = vt_match.group(1).strip()

        # Elevation
        el_match = re.search(r"Elevation:</b>\s*(-?\d+)\s*m", desc_html)
        if el_match:
            elevation = int(el_match.group(1))

        # Country & Province
        font5 = soup.find("font", size="5")
        if font5:
            parts = [p.strip() for p in font5.get_text(separator="\n").split("\n") if p.strip()]
            if len(parts) >= 1:
                country = parts[0]
            if len(parts) >= 2:
                province = parts[1]

        # Summary text
        p_tag = soup.find("p", align="justify")
        if p_tag:
            summary = " ".join(p_tag.get_text().split())

    records.append({
        "gvp_id": v_id, "name": name, "latitude": lat, "longitude": lon, "country": country,
        "province": province, "landform": landform, "volcano_type": v_type,
        "elevation_m": elevation, "summary": summary,
    })

print(f"Extracted {len(records)} features. Writing to {OUTPUT_CSV}...")

fieldnames = ["gvp_id", "name", "latitude", "longitude", "country", "province", "landform",
    "volcano_type", "elevation_m", "summary"]

with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(records)

print(f"Done! Saved to {OUTPUT_CSV}")
