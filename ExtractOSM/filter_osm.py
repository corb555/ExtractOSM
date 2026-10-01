"""
filter_osm.py

A high-performance utility to create a filtered subset of an OSM file based
on a shared Classification configuration.

This script does NOT modify data. It creates a valid, referentially complete
OSM PBF file containing only the features matching the criteria in the
YAML config.

It acts as a wrapper around the `osmium tags-filter` CLI tool.
"""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
from typing import List, Dict

# Reuse the existing config loader to ensure rules match exactly
from ExtractOSM.classification_schema import CLASSIFICATION_SCHEMA
from YMLEditor.yaml_reader import ConfigLoader


def check_dependencies():
    """Ensures osmium-tool is installed and in the PATH."""
    if not shutil.which("osmium"):
        print("❌ Error: 'osmium' command not found.")
        print(
            "   Please install osmium-tool (e.g., 'brew install osmium-tool' or 'apt install "
            "osmium-tool').")
        sys.exit(1)


def build_filter_expressions(config: Dict) -> List[str]:
    """
    Translates the YAML configuration 'keys' section into Osmium filter expressions.

    Format:
    - Specific values: nwr/key=value1,value2
    - Any value:       nwr/key
    """
    keys_conf = config.get("osm_filter", {})
    expressions = []

    for key, subconf in keys_conf.items():
        filters = subconf.get("filters", [])

        # 'nwr' means check Nodes, Ways, and Relations
        prefix = f"nwr/{key}"

        if filters:
            # Join values with commas (e.g. "natural=peak,volcano")
            # We filter empty strings just in case
            valid_values = [str(f) for f in filters if f]
            if valid_values:
                expression = f"{prefix}={','.join(valid_values)}"
                expressions.append(expression)
        else:
            # If no filters list is provided, match ANY value for this key
            expressions.append(prefix)

    return expressions


def main():
    parser = argparse.ArgumentParser(
        description="Filter an OSM PBF file to a subset based on classification rules.")
    parser.add_argument("--input", required=True, type=Path, help="Path to input OSM PBF file.")
    parser.add_argument("--config", required=True, type=Path,
                        help="Path to classification YAML file.")
    parser.add_argument("--output", required=True, type=Path, help="Path to output OSM PBF file.")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite output file if it exists.")

    args = parser.parse_args()

    # 1. Validation
    check_dependencies()

    # 2. Load Config
    try:
        print(f"➡️  Loading configuration: {args.config}")
        loader = ConfigLoader(CLASSIFICATION_SCHEMA)
        # Allow unknown keys since we only care about the 'osm_filter' section for filtering
        loader.validator.allow_unknown = True
        config = loader.read(args.config)
    except Exception as e:
        sys.exit(f"❌ Configuration error: {e}")

    # 3. Build Filters
    expressions = build_filter_expressions(config)
    if not expressions:
        sys.exit("❌ No osm_filter items found in configuration. Nothing to extract.")

    print(f"   Found {len(expressions)} filter rules.")
    for exp in expressions:
        print(f"   - {exp}")

    # 4. Construct Command
    # osmium tags-filter input.pbf rule1 rule2 ... -o output.pbf
    cmd = ["osmium", "tags-filter", str(args.input), "--output", str(args.output), ]

    if args.overwrite:
        cmd.append("--overwrite")

    # Add the filter expressions
    cmd.extend(expressions)

    # 5. Execute
    print(f"\nCommand: {cmd}\n➡️  Running osmium tags-filter...")
    try:
        subprocess.run(cmd, check=True)
        print(f"✅ Success! Filtered file saved to: {args.output}")
    except subprocess.CalledProcessError as e:
        sys.exit(f"❌ Osmium failed:\n {e}.")


if __name__ == "__main__":
    main()
