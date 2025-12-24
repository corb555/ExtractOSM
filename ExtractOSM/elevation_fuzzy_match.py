# elevation_fuzzy_match.py
"""A utility to match records based on name and elevation similarity.

This script enriches an auxiliary dataset (e.g., a list of peaks with prominence)
by finding the best-matching record from a master dataset (e.g., OSM data)
and adding its unique identifier.

The matching algorithm is designed for datasets that lack geographic coordinates
in the auxiliary file but share a common numeric attribute, like elevation.

The process is as follows:

1.  **Configuration Loading:** The script is driven by a declarative YAML
    configuration file that defines column names, matching thresholds, and
    text cleaning rules. This separates the "what" from the "how"
    (Principle 1.1.4).

2.  **Master Data Indexing:** To avoid repeatedly scanning the master file, the
    script builds an in-memory dictionary. This index groups all master records
    by their normalized name, enabling instant lookup of all potential candidates.

3.  **Candidate Matching Loop:** For each record in the auxiliary file, the script:
    a.  Finds all master records with the same normalized name using the index.
    b.  Scores the name similarity between the *original* auxiliary record name
        and each candidate's *original* name using the high-performance
        `rapidfuzz` library.
    c.  Filters out candidates that fall below a configured name similarity
        threshold.
    d.  Calculates the elevation difference between the auxiliary record and
        the remaining candidates, filtering out any that exceed a configured
        percentage tolerance.

4.  **Best Match Selection:** From the final, valid candidates for an auxiliary
    record, the one with the smallest absolute elevation difference is selected
    as the definitive match.

5.  **Deduplication:** To ensure a clean one-to-one mapping, the script performs
    a final deduplication pass. If multiple auxiliary records match the same
    master record, only the one with the highest name similarity score is kept.

6.  **Output Generation:** The script produces a new CSV file containing all
    the original data from the auxiliary file, now enriched with the matched
    `osm_id`.

Usage:
  This utility is designed to be run as a command-line tool.

Example Usage:
    python elevation_fuzzy_match.py \\
        --master /path/to/osm_peaks.csv \\
        --auxiliary /path/to/peak_prominence.csv \\
        --config /path/to/elevation_match_config.yml \\
        --output /path/to/prominence_with_osm_ids.csv
"""
import argparse
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
from rapidfuzz import fuzz  # MODIFIED: Using the faster rapidfuzz library
from tqdm import tqdm

from YMLEditor.yaml_reader import ConfigLoader

# --- Configuration Schema (Principle 1.3.3) ---
# Defines the structure and rules for the YAML configuration file.
MATCH_SCHEMA = {
    'config_type': {'type': 'string', 'required': True, 'allowed': ["ElevationMatch"]},
    'master_name_col': {'type': 'string', 'required': True},
    'master_ele_col': {'type': 'string', 'required': True},
    'master_id_col': {'type': 'string', 'required': True},
    'aux_name_col': {'type': 'string', 'required': True},
    'aux_ele_col': {'type': 'string', 'required': True},
    'name_score_threshold': {
        'type': 'integer', 'required': True, 'min': 0, 'max': 100
    },
    'elevation_percent_tolerance': {
        'type': 'float', 'required': True, 'min': 0
    },
    'noise_words': {
        'type': 'list', 'schema': {'type': 'string'}, 'required': False, 'default': []
    }
}


def clean_text(text: Any, noise_pattern: re.Pattern = None) -> str:
    """
    Performs robust text normalization for accurate name matching.

    Args:
        text (Any): The input text to clean.
        noise_pattern (re.Pattern): A pre-compiled regex pattern for removing
            noise words.

    Returns:
        str: The normalized and cleaned text.
    """
    if not isinstance(text, str):
        return ""
    # NFKC normalization handles special characters and diacritics.
    text = unicodedata.normalize('NFKC', text).lower()
    text = re.sub(r'[^\w\s-]', '', text)  # Remove punctuation except hyphens
    if noise_pattern:
        text = noise_pattern.sub('', text)
    return " ".join(text.split())  # Normalize whitespace


def build_master_lookup(df: pd.DataFrame, name_col: str, ele_col: str, id_col: str) -> Dict[str, List[Dict]]:
    """
    Builds an in-memory lookup dictionary from the master DataFrame.

    This pre-processing step is critical for performance, as it allows for
    O(1) lookup of candidates by name, avoiding a full table scan for each
    auxiliary record.

    Args:
        df (pd.DataFrame): The master data.
        name_col (str): The column name for the feature's name.
        ele_col (str): The column name for the feature's elevation.
        id_col (str): The column name for the feature's unique ID.

    Returns:
        A dictionary where keys are cleaned names and values are lists of
        candidate records, each with its elevation, ID, and original name.
    """
    lookup = {}
    for record in tqdm(df.itertuples(), total=len(df), desc="Indexing master records"):
        name = getattr(record, name_col, "")
        cleaned_name = getattr(record, 'clean_name', clean_text(name))
        elevation = getattr(record, ele_col, 0.0)
        record_id = getattr(record, id_col)

        if not cleaned_name or pd.isna(elevation):
            continue

        # Store the original name for more accurate scoring later.
        candidate = {
            'ele': float(elevation),
            'id': record_id,
            'original_name': name
        }
        if cleaned_name not in lookup:
            lookup[cleaned_name] = []
        lookup[cleaned_name].append(candidate)
    return lookup


def main() -> None:
    """Orchestration layer: Handles I/O, config, and calls the main pipeline."""
    parser = argparse.ArgumentParser(description="Fuzzy match records by name and elevation.")
    parser.add_argument("--master", type=Path, required=True, help="Path to the master data CSV (e.g., OSM).")
    parser.add_argument("--auxiliary", type=Path, required=True, help="Path to the auxiliary data to enrich.")
    parser.add_argument("--config", type=Path, required=True, help="Path to the YAML configuration file.")
    parser.add_argument("--output", type=Path, required=True, help="Path for the output enriched CSV file.")
    args = parser.parse_args()

    try:
        # --- STAGE 1: Load and Validate Configuration (Principle 1.3.2, 1.3.3) ---
        print(f"➡️ Loading configuration from: {args.config}")
        loader = ConfigLoader(MATCH_SCHEMA)
        config = loader.read(args.config)

        # --- STAGE 2: Load and Prepare Data ---
        print("➡️ Loading and preparing datasets...")
        master_df = pd.read_csv(args.master, dtype={config['master_id_col']: str})
        aux_df = pd.read_csv(args.auxiliary)

        noise_words = config.get('noise_words', [])
        noise_pattern = re.compile(r"\b(" + "|".join(re.escape(w) for w in noise_words) + r")\b", re.IGNORECASE) if noise_words else None

        print("   - Pre-cleaning names for matching...")
        master_df['clean_name'] = master_df[config['master_name_col']].apply(lambda x: clean_text(x, noise_pattern))
        aux_df['clean_name'] = aux_df[config['aux_name_col']].apply(lambda x: clean_text(x, noise_pattern))

        # --- STAGE 3: Build Master Data Index for Fast Lookups ---
        master_lookup = build_master_lookup(
            master_df, config['master_name_col'], config['master_ele_col'], config['master_id_col']
        )

    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"\n❌ FATAL ERROR during setup: {e}")
        sys.exit(1)

    # --- STAGE 4: Match Auxiliary Records ---
    all_matches = []
    ele_tolerance = config['elevation_percent_tolerance'] / 100.0
    name_threshold = config['name_score_threshold']

    for aux_record in tqdm(aux_df.itertuples(), total=len(aux_df), desc="Matching auxiliary records"):
        aux_clean_name = getattr(aux_record, 'clean_name')
        aux_original_name = getattr(aux_record, config['aux_name_col'])
        aux_ele = getattr(aux_record, config['aux_ele_col'])

        if not aux_clean_name or pd.isna(aux_ele):
            continue

        # Find candidates instantly using the pre-built index
        candidates = master_lookup.get(aux_clean_name, [])
        if not candidates:
            continue

        valid_candidates = []
        for cand in candidates:
            # --- MODIFIED: More robust scoring logic ---
            # 1. Score similarity between ORIGINAL names for better accuracy.
            name_score = fuzz.token_sort_ratio(aux_original_name, cand['original_name'])
            if name_score < name_threshold:
                continue

            # 2. Check if elevation is within tolerance
            ele_diff = abs(aux_ele - cand['ele'])
            if aux_ele > 0 and (ele_diff / aux_ele) > ele_tolerance:
                continue

            valid_candidates.append({
                'id': cand['id'],
                'ele_diff': ele_diff,
                'name_score': name_score
            })

        if valid_candidates:
            # Find the best candidate by the closest elevation
            best_candidate = min(valid_candidates, key=lambda x: x['ele_diff'])
            all_matches.append({
                'aux_index': aux_record.Index,
                config['master_id_col']: best_candidate['id'],
                'match_score': best_candidate['name_score'] # For tie-breaking
            })

    if not all_matches:
        print(f"\n❌ ERROR - No matches found")
        sys.exit(1)
    else:
        # --- STAGE 5: Deduplicate and Format Output ---
        print(f"➡️ Found {len(all_matches)} potential matches. Deduplicating...")
        match_df = pd.DataFrame(all_matches)

        match_df.sort_values('match_score', ascending=False, inplace=True)
        final_matches = match_df.drop_duplicates(subset=[config['master_id_col']], keep='first')

        num_dupes_removed = len(match_df) - len(final_matches)
        if num_dupes_removed > 0:
            print(f"   - Removed {num_dupes_removed} duplicate matches to ensure a unique master ID mapping.")

        # ---  Use an INNER merge to keep only successful matches ---
        # This is the key change that prevents 'NaN' osm_id values in the output.
        output_df = aux_df.merge(
            final_matches[['aux_index', config['master_id_col']]],
            left_index=True,
            right_on='aux_index',
            how='inner'  # Changed from 'left' to 'inner'
        )
        output_df.drop(columns=['aux_index', 'clean_name'], inplace=True, errors='ignore')


    # --- STAGE 6: Save Results ---
    args.output.parent.mkdir(parents=True, exist_ok=True)
    print(f"\n➡️ Saving {len(output_df)} matched records to: {args.output}")
    output_df.to_csv(args.output, index=False)

    # --- MODIFIED: The summary now accurately reflects the output file content ---
    print("\n*️⃣  Elevation Fuzzy Match Summary:")
    print(f"   - Master records indexed:      {len(master_df)}")
    print(f"   - Auxiliary records processed: {len(aux_df)}")
    successful_matches = len(output_df)
    print(f"   - Records successfully matched: {successful_matches}")
    if len(aux_df) > 0:
        match_rate = (successful_matches / len(aux_df)) * 100
        print(f"   - Match Rate:                 {match_rate:.2f}%")
    print("\n✅ Elevation Fuzzy Match done.")


if __name__ == "__main__":
    main()