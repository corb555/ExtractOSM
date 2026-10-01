# run_extract_osm.py
"""
Executable script for  Pure OSM feature extraction.

This script handles command-line argument parsing and orchestrates the osm extraction
process by configuring and running the ExtractOsm class. It reads an OSM file
and produces a base features CSV without any external data enrichment.
"""
import argparse
import logging
from pathlib import Path
import sys

from ExtractOSM.classification_schema import CLASSIFICATION_SCHEMA
from ExtractOSM.extract_osm import ExtractOsm
from ExtractOSM.yaml_config import read_config

LOGGER = logging.getLogger(__name__)


def main() -> None:
    """Parses arguments, configures, and runs the OSM extraction pipeline."""
    parser = argparse.ArgumentParser(
        description="Extract base features from an OSM file to a CSV or OSM file.")
    parser.add_argument("--input", required=True, type=Path,
                        help="Path to the input OSM file (.osm or .pbf).")
    parser.add_argument("--config", required=True, type=Path,
                        help="Path to the extraction configuration YAML file.")
    parser.add_argument("--output", required=True, type=Path, help="Path for the output file.")
    parser.add_argument("--touch-only", action="store_true",
        help="Touch the primary output file and exit without performing spatial matching.", )
    parser.add_argument("--substitutions", required=False, type=Path,
                        help="Path to the substitutions YAML file.")
    parser.add_argument("--ignore-tags", dest="ignore_tags", required=False, type=Path,
                        help="Path to the ignore_tags YAML file.")
    parser.add_argument("-v", "--verbose", action="store_true",
        help="Enable verbose debug logging.", )
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s", )

    file_paths = {
        "osm_path": args.input, "config_path": args.config, "substitution": args.substitutions,
        "ignore_tags": args.ignore_tags, "output_path": args.output,
    }

    if args.touch_only:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.touch()
        except OSError as exc:
            LOGGER.error("Could not touch output file %s: %s", args.output, exc)
            return 1

        LOGGER.info("OSM export not required; skipping")
        return 0

    try:
        configuration = read_config(file_paths["config_path"], CLASSIFICATION_SCHEMA)
    except MemoryError as exc:
        LOGGER.error("Error reading configuration file %s: %s", file_paths["config_path"], exc, )
        sys.exit(1)

    try:
        output_path = file_paths["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)

        extractor = ExtractOsm(file_paths, configuration, )
        extractor.run()
    except MemoryError as exc:
        LOGGER.error("An error occurred reading the OSM file: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
