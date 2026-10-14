# enrich_features.py
"""
Executable script for STAGE 2: Data Enrichment.

This script takes a base features CSV and merges it with one or more
"enhancement" CSV files. The join column is declared by `id_column` in the features configuration.

It uses the same configuration file as the extraction step to determine
the join column, which enhancement files to load, and which columns to merge.
"""
import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Dict, Any, List

from ExtractOSM.classification_schema import CLASSIFICATION_SCHEMA
from ExtractOSM.yaml_config import read_config
import pandas as pd

LOGGER = logging.getLogger(__name__)


def read_features_csv(path: Path, id_column: str, *, role: str) -> pd.DataFrame:
    """Read a joinable CSV, explaining empty files and missing ID headers."""
    try:
        frame = pd.read_csv(path, dtype={id_column: str})
    except pd.errors.EmptyDataError as exc:
        raise ValueError(
            f"{role} '{path}' has no CSV header or readable columns "
            "(it may be empty or contain only blank lines). "
            "Check the upstream generating step: it must write a header row "
            "even when there are zero data records."
        ) from exc
    except pd.errors.ParserError as exc:
        raise ValueError(f"{role} '{path}' is not a valid CSV: {exc}") from exc

    if id_column not in frame.columns:
        raise ValueError(
            f"{role} '{path}' is missing configured join column '{id_column}'. "
            f"Available columns: {list(frame.columns)}. "
            "Check the generating step or the configuration's id_column."
        )
    return frame


def write_csv_atomically(frame: pd.DataFrame, path: Path) -> None:
    """Publish a complete CSV only after writing it successfully."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="",
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as output:
            temporary = Path(output.name)
            frame.to_csv(output, index=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    """Parses arguments, loads data, and performs the enrichment merge."""
    parser = argparse.ArgumentParser(
        description="Enrich a base features CSV with data from other files.")
    parser.add_argument("--input", required=True, type=Path,
                        help="Path to the base features CSV file to enrich.")
    parser.add_argument("--config", required=True, type=Path,
                        help="Path to the configuration YAML file.")
    parser.add_argument("--enrichment-dir", dest="enrichment_dir", required=True, type=Path,
                        help="Directory containing enrichment files.")
    parser.add_argument("--segment", required=True, type=str,
                        help="The geographic segment name (e.g., 'salt_lake').")
    parser.add_argument("--output", required=True, type=Path,
                        help="Path for the final, enriched output CSV file.")
    parser.add_argument("--ignore-errors", action="store_true", help="Skip missing enrichment files (other errors still fail the build).")
    parser.add_argument("-v", "--verbose", action="store_true",
        help="Enable verbose diagnostic logging.", )

    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s", )

    errors: list[str] = []

    try:
        LOGGER.debug("Loading configuration: %s", args.config)
        configuration = read_config(args.config, CLASSIFICATION_SCHEMA)
        id_column = configuration["id_column"]
        LOGGER.debug("Loading base features: %s", args.input)
        base_df = read_features_csv(args.input, id_column, role="Base features CSV")
    except Exception as exc:
        LOGGER.error("❌ ERROR loading configuration or base features: %s", exc)
        return 1

    try:
        enrichment_params = get_enrichment_file_paths(
            args.enrichment_dir, args.segment, configuration, args.ignore_errors,
        )
    except (FileNotFoundError, ValueError) as exc:
        LOGGER.error("❌ ERROR resolving enrichment files: %s", exc)
        return 1

    if not enrichment_params:
        LOGGER.warning("⚠ No enrichment files selected; output will contain base features only.")
        try:
            write_csv_atomically(base_df, args.output)
        except OSError as exc:
            LOGGER.error("❌ ERROR saving output CSV '%s': %s", args.output, exc)
            return 1
        LOGGER.info("Enrichment complete: %s rows, %s columns -> %s",
                    len(base_df), len(base_df.columns), args.output)
        return 0

    enriched_df = base_df.copy()
    LOGGER.info("Loading enrichment files...")

    for params in enrichment_params:
        path = params['path']
        columns_to_merge = params['columns']
        # --- Read the update mode from the config ---
        update_mode = params.get('mode', 'safe')  # Default to 'safe' if not specified

        try:
            LOGGER.debug("Loading enhancement file: %s (mode: %s)", path.name, update_mode)
            enhancement_df = read_features_csv(path, id_column, role="Enrichment CSV")

            # The enrichment join must be one-to-one on the configured ID.
            if enhancement_df[id_column].duplicated().any():
                duplicates = enhancement_df.loc[
                    enhancement_df[id_column].duplicated(), id_column].unique()

                raise ValueError(
                    f"Enrichment CSV '{path}' contains duplicate join IDs in "
                    f"'{id_column}' (examples: {list(duplicates[:5])}). "
                    "The enrichment join must be one-to-one; fix the generating step."
                )

            missing_columns = [col for col in columns_to_merge if col not in enhancement_df.columns]
            if missing_columns:
                raise ValueError(
                    f"Enrichment CSV '{path}' is missing configured merge column(s): "
                    f"{missing_columns}. Available columns: {list(enhancement_df.columns)}."
                )

            merge_cols = [id_column] + columns_to_merge
            merge_cols = sorted(list(set(merge_cols)))

            merged_df = pd.merge(enriched_df, enhancement_df[merge_cols], on=id_column,
                how='left', suffixes=('', '_new'))

            # --- Conditional Overwrite Logic ---
            for col in columns_to_merge:
                new_col_name = f"{col}_new"
                if new_col_name in merged_df.columns:
                    if update_mode == 'overwrite':
                        # Mode 1: Overwrite all values.
                        # Use the new value if it exists, otherwise keep the old one.
                        merged_df[col] = merged_df[new_col_name].fillna(merged_df[col])
                    else:  # Default 'safe' mode
                        # Mode 2: Only update if the original value is null/zero/empty.
                        # Create a mask for rows that need updating.

                        # Check for nulls (NaN)
                        is_null = merged_df[col].isnull()
                        # Check for zeros (if numeric)
                        is_zero = pd.to_numeric(merged_df[col], errors='coerce').eq(0)
                        # Check for empty strings (if object/string)
                        is_empty = (merged_df[col].astype(str).str.strip() == '')

                        # Combine conditions: update if original is null, zero, or empty.
                        update_mask = is_null | is_zero | is_empty

                        # Use the mask to update only the targeted rows.
                        merged_df.loc[update_mask, col] = merged_df[new_col_name]

                    # Drop the temporary '_new' column
                    merged_df.drop(columns=[new_col_name], inplace=True)

            enriched_df = merged_df
            LOGGER.info("Merged/updated %s column(s) from %s: %s", len(columns_to_merge), path.name,
                columns_to_merge, )

        except (FileNotFoundError, ValueError, KeyError, pd.errors.ParserError, OSError) as exc:
            LOGGER.error("❌ ERROR processing enrichment file '%s': %s", path, exc)
            errors.append(str(path))

    if errors:
        LOGGER.error(
            "❌ ERROR: %d enrichment file(s) failed. Output NOT written: %s. "
            "Failed inputs: %s",
            len(errors), args.output, ", ".join(errors),
        )
        return 1

    try:
        write_csv_atomically(enriched_df, args.output)
    except (OSError, ValueError) as exc:
        LOGGER.error("❌ ERROR saving enriched output CSV '%s': %s", args.output, exc)
        return 1
    LOGGER.info("Enrichment complete: %s rows, %s columns -> %s",
                len(enriched_df), len(enriched_df.columns), args.output)
    return 0


def get_enrichment_file_paths(
        enrichment_dir: Path, segment: str, configuration: Dict[str, Any], ignore_errors
) -> List[Dict[str, Any]]:
    """Constructs and validates the full paths for enrichment files."""
    # This helper function can be copied directly from the old run_extract_osm.py
    # and live inside this new script.
    if not enrichment_dir.is_dir():
        raise FileNotFoundError(f"Enrichment directory not found: {enrichment_dir}")

    enrichment_params = []
    for item in configuration.get("enrichment", []):
        suffix = item.get("file_suffix")
        columns = item.get("columns")
        mode = item.get("mode", "safe")  # Default to 'safe'
        if not suffix or not columns: continue

        path = enrichment_dir / f"{segment}_{suffix}"
        if not path.exists():
            if ignore_errors:
                LOGGER.warning("⚠ Enrichment file missing; skipping because --ignore-errors is set: %s", path)
                continue
            raise FileNotFoundError(f"Enrichment file not found: {path}")

        enrichment_params.append({"path": path, "columns": columns, "mode": mode})
    return enrichment_params


if __name__ == "__main__":
    sys.exit(main())
