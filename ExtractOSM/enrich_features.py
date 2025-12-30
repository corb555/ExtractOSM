# enrich_features.py
"""
Executable script for STAGE 2: Data Enrichment.

This script takes a base features CSV and merges it with one or more
"enhancement" CSV files. The join is performed on a common 'osm_id' column.

It uses the same configuration file as the extraction step to determine
which enhancement files to load and which columns to merge.
"""
import argparse
import sys
from pathlib import Path
from typing import Dict, Any, List

import pandas as pd

from ExtractOSM.yaml_config import read_config
from ExtractOSM.classification_schema import CLASSIFICATION_SCHEMA

def main() -> None:
    """Parses arguments, loads data, and performs the enrichment merge."""
    parser = argparse.ArgumentParser(
        description="Enrich a base features CSV with data from other files."
    )
    parser.add_argument("--input", required=True, type=Path, help="Path to the base features CSV file to enrich.")
    parser.add_argument("--config", required=True, type=Path, help="Path to the configuration YAML file.")
    parser.add_argument("--enrichment-dir", dest="enrichment_dir", required=True, type=Path, help="Directory containing enrichment files.")
    parser.add_argument("--segment", required=True, type=str, help="The geographic segment name (e.g., 'salt_lake').")
    parser.add_argument("--output", required=True, type=Path, help="Path for the final, enriched output CSV file.")
    parser.add_argument("--ignore-errors", action="store_true", help="Continue after error.")

    args = parser.parse_args()

    error_flag = False

    try:
        print(f"➡️ Loading base features: {args.input}")
        base_df = pd.read_csv(args.input, dtype={'osm_id': str})

        # Validate the enhancement file
        if 'osm_id' not in base_df.columns:
            raise ValueError(f"Base file {args.input} is missing the required 'osm_id' join column.")

        print(f"➡️ Loading configuration: {args.config}")
        configuration = read_config(args.config, CLASSIFICATION_SCHEMA)

    except (FileNotFoundError, Exception) as e:
        print(f"❌ Error reading initial files: {e}")
        sys.exit(1)

    # Use  helper function to get the list of enrichment files
    enrichment_params = get_enrichment_file_paths(
        args.enrichment_dir, args.segment, configuration, args.ignore_errors
    )

    if not enrichment_params:
        print("⚠️ No enrichment files configured or found.")
        base_df.to_csv(args.output, index=False)
        sys.exit(0)

    enriched_df = base_df.copy()
    print("➡️ Loading enrichment files...")

    for params in enrichment_params:
        path = params['path']
        columns_to_merge = params['columns']
        # --- Read the update mode from the config ---
        update_mode = params.get('mode', 'safe') # Default to 'safe' if not specified

        try:
            print(f"      - Loading enhancement file: {path.name} (mode: {update_mode})")
            enhancement_df = pd.read_csv(path, dtype={'osm_id': str})

            if 'osm_id' not in enhancement_df.columns:
                raise ValueError(f"Enhancement file {path.name} is missing 'osm_id' join column.")

            # Check for duplicate 'osm_id's in the enhancement file.
            if enhancement_df['osm_id'].duplicated().any():
                # Find the actual duplicate IDs to provide a helpful error message.
                duplicates = enhancement_df[enhancement_df['osm_id'].duplicated()]['osm_id'].unique()

                # Construct a clear, actionable, fatal error message.
                error_message = f"""
❌  ERROR in enrichment file: '{path.name}'
   - This file contains duplicate 'osm_id's, which is not allowed.
   - Example duplicate ID(s) found: {list(duplicates[:5])}
   - Please fix the process that generates this file.
                """
                raise ValueError(error_message)

            merge_cols = ['osm_id'] + columns_to_merge
            merge_cols = sorted(list(set(merge_cols)))

            enriched_df = pd.merge(
                enriched_df,
                enhancement_df[merge_cols],
                on='osm_id',
                how='left',
                suffixes=('', '_new')
            )

            # --- Conditional Overwrite Logic ---
            for col in columns_to_merge:
                new_col_name = f"{col}_new"
                if new_col_name in enriched_df.columns:
                    if update_mode == 'overwrite':
                        # Mode 1: Overwrite all values.
                        # Use the new value if it exists, otherwise keep the old one.
                        enriched_df[col] = enriched_df[new_col_name].fillna(enriched_df[col])
                    else: # Default 'safe' mode
                        # Mode 2: Only update if the original value is null/zero/empty.
                        # Create a mask for rows that need updating.

                        # Check for nulls (NaN)
                        is_null = enriched_df[col].isnull()
                        # Check for zeros (if numeric)
                        is_zero = pd.to_numeric(enriched_df[col], errors='coerce').eq(0)
                        # Check for empty strings (if object/string)
                        is_empty = (enriched_df[col].astype(str).str.strip() == '')

                        # Combine conditions: update if original is null, zero, or empty.
                        update_mask = is_null | is_zero | is_empty

                        # Use the mask to update only the targeted rows.
                        enriched_df.loc[update_mask, col] = enriched_df[new_col_name]

                    # Drop the temporary '_new' column
                    enriched_df.drop(columns=[new_col_name], inplace=True)

            print(f"     ✅ Merged/updated {len(columns_to_merge)} column(s): {columns_to_merge}")

        except (FileNotFoundError, ValueError, KeyError) as e:
            print(f"❌ Error processing enrichment file {path}: {e}")
            error_flag = True

    print(f"\n➡️ Saving enriched data to: {args.output}")
    enriched_df.to_csv(args.output, index=False)
    if error_flag:
        print("❌ Errors during processing")
        sys.exit(1)
    else:
        print("✅ Enrichment complete.")


def get_enrichment_file_paths(
        enrichment_dir: Path, segment: str, configuration: Dict[str, Any], ignore_errors
) -> List[Dict[str, Any]]:
    """Constructs and validates the full paths for enrichment files."""
    # This helper function can be copied directly from the old run_extract_osm.py
    # and live inside this new script.
    if not enrichment_dir.is_dir():
        print(f"❌ Enrichment directory not found: {enrichment_dir}")
        sys.exit(1)

    enrichment_params = []
    for item in configuration.get("enrichment", []):
        suffix = item.get("file_suffix")
        columns = item.get("columns")
        mode = item.get("mode", "safe") # Default to 'safe'
        if not suffix or not columns: continue

        path = enrichment_dir / f"{segment}_{suffix}"
        if not path.exists():
            print(f"❌ Enrichment file not found: {path}")
            if ignore_errors:
                continue
            else:
                sys.exit(1)

        enrichment_params.append({"path": path, "columns": columns})
    return enrichment_params


if __name__ == "__main__":
    main()