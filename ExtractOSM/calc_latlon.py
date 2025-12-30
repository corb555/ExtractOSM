# calc_latlon.py
"""
calc_latlon.py

This script reads an OpenStreetMap (OSM) file, extracts polygon features to geosjson
and calculates their area and centroid (latitude/longitude), and exports the
results to a CSV file.

This script uses a "filter-first" streaming architecture for high performance and
low memory usage on very large OSM files.

1.  **Declarative Filtering:** Filtering is controlled by a YAML file, which
    specifies which features to process based on their OSM tags.

2.  **Efficient Pre-filtering:** The script passes these filter rules directly
    to the `osmium` command-line tool. Osmium, a high-performance C++ utility,
    pre-filters the massive source OSM file to create a small, intermediate
    GeoJSON file containing only the features of interest.

3.  **Streaming Processing:** The script then processes this smaller GeoJSON file
    as a stream, feature by feature. This ensures that memory consumption remains
    low and constant, regardless of the number of features.

This script requires 'osmium' to be installed and in the system's PATH.
"""

import argparse
import csv
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import ijson
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform
from tqdm import tqdm

from ExtractOSM.classification_schema import CLASSIFICATION_SCHEMA
from YMLEditor.yaml_reader import ConfigLoader


def main():
    parser = argparse.ArgumentParser(
        description="Calculate the lat/lon and area for OSM polygons"
    )
    parser.add_argument("--osm-file", type=Path, required=True, help="Path to OSM file")
    parser.add_argument("--config", type=Path, required=True, help="Path to classification yml")
    parser.add_argument("--output", type=Path, required=True, help="Path for output CSV")
    parser.add_argument("--build-dir", type=Path, required=True, help="Build directory")
    parser.add_argument("--log-level", type=int, default=4, help="Set log level.")
    args = parser.parse_args()

    osm_fname = os.path.basename(args.osm_file)
    geojson_path = Path(args.build_dir, f"{os.path.splitext(osm_fname)[0]}_geo.json")
    geojson_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        print(f"--- Loading config: {args.config} ---")
        loader = ConfigLoader(CLASSIFICATION_SCHEMA)
        loader.validator.allow_unknown = True
        configuration = loader.read(args.config)
        print("✅ Configuration loaded successfully.")
    except (FileNotFoundError, ValueError) as e:
        sys.exit(f"\n❌ Configuration error: {e}\n")

    filter_map = {
        key: set(subconf.get("filters", []))
        for key, subconf in configuration.get("keys", {}).items()
    }
    print_filters(filter_map)

    # Pass the filters directly to the GeoJSON creation step.
    if not create_filtered_geojson(geojson_path, args.osm_file, filter_map):
        sys.exit(1)

    print(f"  Output csv: {args.output}")

    print("➡️ Calculating lat/lon from filtered GeoJSON stream...")
    try:
        # Process the file as a true stream, without loading into memory.
        with open(args.output, mode="w", newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["name", "osm_id", "lat", "lon", "area"])

            with open(geojson_path, 'rb') as f:
                # Use ijson to stream features one by one.
                parser = ijson.items(f, 'features.item')
                # Wrap the parser in tqdm. It won't have a total, but it will
                # show iteration speed and elapsed time, which is ideal for a stream.
                for feature in tqdm(parser, desc="Processing features"):
                    if not (feature.get("geometry") and
                            feature["geometry"].get("type") in ("Polygon", "MultiPolygon")):
                        continue

                    geom = shape(feature["geometry"])
                    props = feature["properties"]
                    osmium_id = feature.get("id")

                    try:
                        _, osm_id = get_osm_id(osmium_id)
                    except ValueError as ve:
                        print(f"⚠️  Skipping invalid ID '{osmium_id}': {ve}")
                        continue

                    name = props.get("name", "unknown")
                    area, centroid = compute_area_centroid(geom)
                    writer.writerow(
                        [name, osm_id, f"{centroid.y:.5f}", f"{centroid.x:.5f}", f"{area:.2f}"]
                    )

        print(f"\n✅ CSV export complete: {args.output}")

    except FileNotFoundError:
        sys.exit(f"\n❌ Error: GeoJSON file not found at '{geojson_path}'.\n")
    except (ijson.JSONError, ValueError) as e:
        sys.exit(f"\n❌ Error processing GeoJSON stream: {e}\n")


def create_filtered_geojson(geojson_path: Path, osm_path: Path, filter_map: dict) -> bool:
    """
    Creates a filtered GeoJSON file from a source OSM file using a two-stage
    osmium pipeline for high performance.

    1. `osmium tags-filter`: Pre-filters the large source PBF into a small,
       intermediate PBF file containing only the desired features.
    2. `osmium export`: Converts the small PBF file into the final GeoJSON
       format, adding the unique IDs required for processing.

    Args:
        geojson_path (Path): The path for the final output GeoJSON file.
        osm_path (Path): The path to the source OSM file (.pbf or .osm).
        filter_map (dict): The filters to apply.

    Returns:
        bool: True if successful, False otherwise.
    """
    if geojson_path.exists() and geojson_path.stat().st_mtime >= osm_path.stat().st_mtime:
        print(f"✅ Filtered GeoJSON is up-to-date: {geojson_path}")
        return True

    print(f"🔄 Stale or missing GeoJSON.\n➡️ Generating filtered GeoJSON from {osm_path}...")

    # --- Build a list of separate filter expressions ---
    # The format is  ["w/key=v1,v2", "r/key=v1,v2", "w/key2=v3", "r/key2=v3"]
    osmium_filters = []
    for key, values in filter_map.items():
        if values:
            values_str = ",".join(values)
            # Add a separate filter for ways and relations
            osmium_filters.append(f"w/{key}={values_str}")
            osmium_filters.append(f"r/{key}={values_str}")

    if not osmium_filters:
        print("⚠️ Warning: No filters defined. This may process a very large file.")
        # Fallback to a generic area filter if no specific tags are provided
        osmium_filter_expression = ["a/"]
    else:
        osmium_filter_expression = osmium_filters

    intermediate_pbf_path = geojson_path.with_suffix(".temp.pbf")

    # The filter command now takes a list of filter expressions
    filter_command = [
        "osmium", "tags-filter", str(osm_path),
        *osmium_filter_expression, # Unpack the list of filters here
        "-o", str(intermediate_pbf_path), "--overwrite"
    ]

    export_command = [
        "osmium", "export", str(intermediate_pbf_path),
        "-o", str(geojson_path), "--overwrite", "--add-unique-id=type_id"
    ]

    try:
        print(f"   - Running filter command: {' '.join(filter_command)}")
        subprocess.run(filter_command, check=True, capture_output=True, text=True)

        print(f"   - Running export command: {' '.join(export_command)}")
        subprocess.run(export_command, check=True, capture_output=True, text=True)

        print(f"✅ Filtered GeoJSON export complete: {geojson_path}")
        return True
    except FileNotFoundError:
        print("\n❌ Error: 'osmium' command not found. Please ensure it is installed and in your system's PATH.\n")
        return False
    except subprocess.CalledProcessError as e:
        # Re-check which command failed to provide a more specific error.
        # This logic needs to be more robust as the command list is now dynamic.
        if "tags-filter" in e.args:
            failed_command = "tags-filter"
        elif "export" in e.args:
            failed_command = "export"
        else:
            failed_command = " "
        print(f"\n❌ Error running osmium '{failed_command}': {e}\n   ❌ Osmium stderr: {e.stderr.strip()}\n")
        return False
    finally:
        if intermediate_pbf_path.exists():
            intermediate_pbf_path.unlink()

# --- Utility functions  ---
def compute_area_centroid(geometry):
    if not geometry.is_valid:
        geometry = geometry.buffer(0)
    centroid = geometry.centroid
    utm_zone = math.floor((centroid.x + 180) / 6) + 1
    epsg_code = 32600 + utm_zone if centroid.y >= 0 else 32700 + utm_zone
    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg_code}", always_xy=True)
    projected_geometry = transform(transformer.transform, geometry)
    area = projected_geometry.area
    return area, centroid

def get_osm_id(osmium_id):
    if not osmium_id:
        raise ValueError("Missing or empty Osmium ID")
    osmium_id = str(osmium_id)
    if osmium_id[0].isdigit():
        val = int(osmium_id)
        return ("node_row", val) if val else ("err", 9999)
    prefix = osmium_id[0]
    try:
        raw_id = int(osmium_id[1:])
    except (ValueError, IndexError):
        raise ValueError(f"Invalid numeric part in Osmium ID: {osmium_id}")
    if prefix == "a":
        return ("way", raw_id // 2) if raw_id % 2 == 0 else ("relation", ((raw_id - 1) // 2) * -1)
    elif prefix == "w":
        return "way", raw_id
    elif prefix == "r":
        return "relation", raw_id
    elif prefix == "n":
        return "node_row", raw_id
    else:
        raise ValueError(f"Unrecognized prefix '{prefix}' in Osmium ID '{osmium_id}'")


def print_filters(filter_map: dict):
    # ... (This helper function is fine, no changes needed)
    print("➡️  Applying the following OSM tag filters via osmium:")
    if not filter_map:
        print("   - No filters defined.")
        return
    max_key_length = max(len(key) for key in filter_map.keys()) if filter_map else 0
    for key, values in sorted(filter_map.items()):
        key_str = f"{key}:".ljust(max_key_length + 2)
        values_str = ", ".join(sorted(list(values)))
        print(f"   - {key_str}{values_str}")


if __name__ == "__main__":
    main()