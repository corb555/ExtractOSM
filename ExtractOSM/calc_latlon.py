# calc_latlon.py
"""
calc_latlon.py

This script reads an OpenStreetMap (OSM) file, extracts polygon AND linestring features
to geojson, calculates their area/length and centroid (latitude/longitude), and
exports the results to a CSV file.
"""

import argparse
import csv
import logging
import math
import os
from pathlib import Path
import subprocess

from ExtractOSM.classification_schema import CLASSIFICATION_SCHEMA
import ijson
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform
from tqdm import tqdm
from YMLEditor.yaml_reader import ConfigLoader

LOGGER = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Calculate the lat/lon and area for OSM features")
    parser.add_argument("--osm-file", type=Path, required=True, help="Path to OSM file")
    parser.add_argument("--config", type=Path, required=True, help="Path to classification yml")
    parser.add_argument("--output", type=Path, required=True, help="Path for output CSV")
    parser.add_argument("--build-dir", type=Path, required=True, help="Build directory")
    parser.add_argument("-v", "--verbose", action="store_true",
        help="Enable verbose diagnostic logging.", )
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s", )

    osm_fname = os.path.basename(args.osm_file)
    geojson_path = Path(args.build_dir, f"{os.path.splitext(osm_fname)[0]}_geo.json")
    geojson_path.parent.mkdir(parents=True, exist_ok=True)

    # Store unique features for output: {osm_id: {data}}
    unique_features = {}

    try:
        LOGGER.debug("Loading config: %s", args.config)
        loader = ConfigLoader(CLASSIFICATION_SCHEMA)
        loader.validator.allow_unknown = True
        configuration = loader.read(args.config)
        LOGGER.debug("Configuration loaded successfully.")
    except (FileNotFoundError, ValueError) as e:
        LOGGER.error("Configuration error: %s", e)
        return 1

    filter_map = {key: set(subconf.get("filters", [])) for key, subconf in
        configuration.get("keys", {}).items()}
    log_filters(filter_map)

    # Pass the filters directly to the GeoJSON creation step.
    if not create_filtered_geojson(geojson_path, args.osm_file, filter_map):
        return 1

    LOGGER.info("Calculating lat/lon from filtered GeoJSON stream...")
    try:
        # Process the file as a true stream, without loading into memory.
        with open(geojson_path, 'rb') as f:
            # Use ijson to stream features one by one.
            parser = ijson.items(f, 'features.item')

            for feature in tqdm(parser, desc="Processing features"):
                geom_type = feature.get("geometry", {}).get("type")

                #  Accept both Polygon and LineString
                if not (feature.get("geometry") and geom_type in ("Polygon", "MultiPolygon",
                                                                  "LineString", "MultiLineString")):
                    continue

                geom = shape(feature["geometry"])
                props = feature["properties"]
                osmium_id = feature.get("id")

                try:
                    _, osm_id = get_osm_id(osmium_id)
                except ValueError as ve:
                    LOGGER.warning("Skipping invalid ID %r: %s", osmium_id, ve)
                    continue

                name = props.get("name", "unknown")

                # Helper handles both Polygons (Area) and Lines (Centroid)
                area, centroid = compute_geometry_metrics(geom)

                # if dupe, Keep the one with the largest area (Polygon wins over LineString)
                if osm_id in unique_features:
                    if area > unique_features[osm_id]['area']:
                        # Overwrite with the better (polygon) version
                        unique_features[osm_id] = {
                            "name": name, "lat": f"{centroid.y:.5f}", "lon": f"{centroid.x:.5f}",
                            "area": area
                        }
                else:
                    unique_features[osm_id] = {
                        "name": name, "lat": f"{centroid.y:.5f}", "lon": f"{centroid.x:.5f}",
                        "area": area
                    }

        # Write final CSV
        LOGGER.info("Output CSV: %s", args.output)

        with open(args.output, mode="w", newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["name", "osm_id", "lat", "lon", "area"])

            for osm_id, data in unique_features.items():
                writer.writerow(
                    [data['name'], osm_id, data['lat'], data['lon'], f"{data['area']:.2f}"])

        LOGGER.info("CSV export complete: %s", args.output)

    except FileNotFoundError:
        LOGGER.error("GeoJSON file not found: %s", geojson_path)
        return 1
    except (ijson.JSONError, ValueError) as e:
        LOGGER.error("Error processing GeoJSON stream: %s", e)
        return 1

    return 0


def create_filtered_geojson(geojson_path: Path, osm_path: Path, filter_map: dict) -> bool:
    """Creates a filtered GeoJSON file using osmium."""
    if geojson_path.exists() and geojson_path.stat().st_mtime >= osm_path.stat().st_mtime:
        LOGGER.info("Filtered GeoJSON is up-to-date: %s", geojson_path)
        return True

    LOGGER.info("Stale or missing GeoJSON; generating from %s", osm_path)

    # --- Build a list of separate filter expressions ---
    osmium_filters = []
    for key, values in filter_map.items():
        if values:
            values_str = ",".join(values)
            # Add filter for ways and relations (Nodes usually don't need centroid calculation
            # this way)
            osmium_filters.append(f"w/{key}={values_str}")
            osmium_filters.append(f"r/{key}={values_str}")

    if not osmium_filters:
        LOGGER.warning("No filters defined. This may process a very large file.")
        osmium_filter_expression = ["a/"]
    else:
        osmium_filter_expression = osmium_filters

    intermediate_pbf_path = geojson_path.with_suffix(".temp.pbf")

    filter_command = ["osmium", "tags-filter", str(osm_path), *osmium_filter_expression, "-o",
        str(intermediate_pbf_path), "--overwrite"]

    export_command = ["osmium", "export", str(intermediate_pbf_path), "-o", str(geojson_path),
        "--overwrite", "--add-unique-id=type_id"]

    try:
        LOGGER.debug("Running filter command: %s", " ".join(filter_command))
        subprocess.run(filter_command, check=True, capture_output=True, text=True)

        LOGGER.debug("Running export command: %s", " ".join(export_command))
        subprocess.run(export_command, check=True, capture_output=True, text=True)

        LOGGER.info("Filtered GeoJSON export complete: %s", geojson_path)
        return True
    except FileNotFoundError:
        LOGGER.error("osmium command not found. Ensure it is installed and available on PATH.")
        return False
    except subprocess.CalledProcessError as e:
        if "tags-filter" in e.args:
            failed_command = "tags-filter"
        elif "export" in e.args:
            failed_command = "export"
        else:
            failed_command = " "
        LOGGER.error("Error running osmium %r: %s", failed_command, e)
        if e.stderr:
            LOGGER.error("Osmium stderr: %s", e.stderr.strip())
        return False
    finally:
        if intermediate_pbf_path.exists():
            intermediate_pbf_path.unlink()


# --- Utility functions ---

def compute_geometry_metrics(geometry):
    """
    Calculates centroid and area.
    For Polygons: Returns projected Area.
    For LineStrings: Returns 0.0 Area (Length is calculated but we return 0 for CSV consistency).
    """
    if not geometry.is_valid:
        geometry = geometry.buffer(0)

    centroid = geometry.centroid

    # Simple area check: Only calculate projected area for Polygons
    if geometry.geom_type in ['Polygon', 'MultiPolygon']:
        utm_zone = math.floor((centroid.x + 180) / 6) + 1
        epsg_code = 32600 + utm_zone if centroid.y >= 0 else 32700 + utm_zone
        transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg_code}", always_xy=True)
        projected_geometry = transform(transformer.transform, geometry)
        area = projected_geometry.area
    else:
        # For Ways/Lines, area is effectively 0
        area = 0.0

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


def log_filters(filter_map: dict) -> None:
    """Log the configured OSM tag filters."""
    LOGGER.info("Applying the following OSM tag filters via osmium:")
    if not filter_map:
        LOGGER.info("   - No filters defined.")
        return

    max_key_length = max(len(key) for key in filter_map)
    for key, values in sorted(filter_map.items()):
        key_str = f"{key}:".ljust(max_key_length + 2)
        values_str = ", ".join(sorted(values))
        LOGGER.info("   - %s%s", key_str, values_str)


if __name__ == "__main__":
    main()
