# run_extract_osm.py
"""
Executable script for STAGE 1: Pure OSM feature extraction.

This script handles command-line argument parsing and orchestrates the extraction
process by configuring and running the ExtractOsm class. It reads an OSM file
and produces a base features CSV without any external data enrichment.
"""
import argparse
import sys
from pathlib import Path

from ExtractOSM.extract_osm import ExtractOsm
from ExtractOSM.yaml_config import read_config
from ExtractOSM.classification_schema import CLASSIFICATION_SCHEMA

def main() -> None:
    """Parses arguments, configures, and runs the OSM extraction pipeline."""
    parser = argparse.ArgumentParser(
        description="Extract base features from an OSM file to a CSV."
    )
    parser.add_argument("--input", required=True, type=Path, help="Path to the input OSM file (.osm or .pbf).")
    parser.add_argument("--config", required=True, type=Path, help="Path to the extraction configuration YAML file.")
    parser.add_argument("--substitutions", required=False, type=Path, help="Path to the substitutions YAML file.")
    parser.add_argument("--ignore-tags", dest="ignore_tags", required=True, type=Path, help="Path to the ignore_tags YAML file.")
    parser.add_argument("--output", required=True, type=Path, help="Path for the output CSV file.")
    parser.add_argument("--log-level", "-l", dest="log_level", type=int, choices=range(0, 6), default=5, help="Set log level.")
    args = parser.parse_args()

    file_paths = {
        "osm_path": args.input,
        "config_path": args.config,
        "substitution": args.substitutions,
        "ignore_tags": args.ignore_tags,
        "output_path": args.output,
    }

    try:
        configuration = read_config(file_paths["config_path"], CLASSIFICATION_SCHEMA)
    except Exception as e:
        print(f"\n❌ Error reading configuration file '{file_paths['config_path']}'\n {e}\n")
        sys.exit(1)

    try:
        output_path = file_paths["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)

        extractor = ExtractOsm(
            file_paths,
            configuration,
            log_level=args.log_level
        )
        extractor.run()
    except Exception as e:
        print(f"\n❌ An error occurred reading file:\n❌ {e}\n")
        sys.exit(1)

if __name__ == "__main__":
    main()