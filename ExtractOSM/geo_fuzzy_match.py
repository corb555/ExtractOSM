# geo_fuzzy.py
"""A generic utility to link records between two geospatial datasets.

This script solves the common challenge of joining two geospatial datasets that
lack a shared, unique identifier. It identifies the best match for each record
in an "auxiliary" dataset by evaluating its spatial proximity and name similarity
against records in a "master" dataset.

The matching algorithm is a multi-stage process that combines spatial and
textual analysis to identify high-quality matches:

1.  **Spatial Index Construction:** The script builds an in-memory R-tree spatial
    index from the coordinates of all records in the master file. This data
    structure enables computationally efficient geographic queries.

2.  **Candidate Search (Spatial Filter):** For each auxiliary record, the script
    queries the spatial index to identify a small subset of candidate matches
    from the master file. This search is limited to records within a configured
    spatial radius (e.g., 150 meters), significantly reducing the number of
    name comparisons required.

3.  **Text Normalization:** To ensure accurate comparisons, all names from both
    datasets are processed through a normalization pipeline. This involves
    Unicode normalization (to resolve accents and special characters like ‘ vs. ’),
    removal of punctuation, and the stripping of configurable 'noise words.'

4.  **Configurable Text Scoring:** The script calculates a name similarity score
    using a configurable strategy. The recommended 'harmonic_partial' mode
    combines an overall similarity metric (`token_sort_ratio`) with a core
    substring match metric (`partial_ratio`) to produce a score that is robust
    to both minor typos and variations in naming specificity.

5.  **Piecewise Quality Scoring:** The script translates both the raw name score
    and the raw distance (in meters) into normalized quality scores (0-100) using
    configurable, piecewise linear functions. This allows for the modeling of
    non-linear relationships, such as creating a "dead zone" where very close
    points receive a maximum score or a steep "falloff" for poor name matches.

6.  **Final Combined Score & Match Decision:** A final, holistic match score is
    calculated by combining the name and distance quality scores using a
    harmonic mean. This method strongly penalizes imbalance, ensuring a
    candidate must score highly on BOTH proximity and name similarity to qualify.
    A candidate is considered a definitive match only if this final combined
    score is above a configured threshold.

Usage:
  This utility is designed to be run as a command-line tool which can be integrated
  into a data pipeline. It consumes two CSV files and a YAML configuration file,
  and produces an enrichment CSV containing the matched data.

Input File Requirements:
  - --master: A CSV file for the master dataset. Must contain at least the
    following columns as defined in the config: a unique ID,
    a name, 'lon', and 'lat'.
  - --auxiliary: A CSV file for the auxiliary dataset. Must contain at least
    a name, 'lon', and 'lat', as well as any data columns to be merged.

Example Usage:
    python geo_fuzzy.py \\
        --master /path/to/restaurants.csv \\
        --auxiliary /path/to/health_ratings.csv \\
        --config /path/to/match_config.yml \\
        --output /path/to/matched_scores.csv
"""
import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List

import geopandas as gpd
import pandas as pd
from tqdm import tqdm

from GeoTier.spatial_index import SpatialIndex
from YMLEditor.yaml_reader import ConfigLoader
from ExtractOSM.piecewise_score import (
    prepare_and_validate_curve,
    calculate_piecewise_score,
)
from ExtractOSM.text_similarity import text_similarity, clean_text, harmonic_mean

# Converts distance from m into 1-100 score. Distances under 30m are given high scores
DEFAULT_DISTANCE_CURVE = [
    {'name': "Perfect", 'end_value': 18, 'start_score': 100, 'end_score': 100},
    {'name': "Excellent", 'end_value': 30, 'start_score': 100, 'end_score': 95},
    {'name': "Good", 'end_value': 60, 'start_score': 95, 'end_score': 70},
    {'name': "Acceptable", 'end_value': 80, 'start_score': 70, 'end_score': 50},
    {'name': "Poor", 'end_value': 120, 'start_score': 50, 'end_score': 0}
]

# Readjusts the scale for RabidFuzz - promotes high scores and gives a steep
# dropoff
DEFAULT_NAME_SCORE_CURVE = [
    {'name': "Bad", 'end_value': 50, 'start_score': 0, 'end_score': 10},
    {'name': "Poor", 'end_value': 65, 'start_score': 10, 'end_score': 50},
    {'name': "Acceptable", 'end_value': 68, 'start_score': 50, 'end_score': 70},
    {'name': "Good", 'end_value': 79, 'start_score': 70, 'end_score': 90},
    {'name': "Excellent", 'end_value': 100, 'start_score': 90, 'end_score': 100}
]

# --- Configuration Schema ---
MATCH_SCHEMA = {
    'config_type': {'type': 'string', 'required': True, 'allowed': ["FuzzyMatch"]},
    'name_column': {'type': 'string', 'required': True},
    'id_column': {'type': 'string', 'required': True},
    'search_radius': {'type': 'number', 'required': True},
    'combined_score_threshold': {'type': 'number', 'required': True, 'min': 0, 'max': 100},
    'name_score_mode': {
        'type': 'string', 'required': False,
        'allowed': ['harmonic_partial'], 'default': 'harmonic_partial'
    },
    'aux_output_columns': {'type': 'list', 'schema': {'type': 'string'}, 'required': True},
    'noise_words': {'type': 'list', 'schema': {'type': 'string'}, 'required': False, 'default': []},
    'noise_words2': {'type': 'list', 'schema': {'type': 'string'}, 'required': False, 'default': []},

    'text_cleaning_rules': {
        'type': 'list',
        'required': False, # Optional, defaults to an empty list
        'default': [],
        'schema': {
            'type': 'dict',
            'schema': {
                'name': {'type': 'string', 'required': True},
                'pattern': {'type': 'string', 'required': True},
                'replace': {'type': 'string', 'required': True}
            }
        }
    },
    'name_score_curve': {
        'type': 'list', 'required': False, 'schema': {
            'type': 'dict', 'schema': {
                'name': {'type': 'string'}, 'end_value': {'type': 'number'},
                'start_score': {'type': 'number'}, 'end_score': {'type': 'number'}
            }
        }
    },
    'distance_score_curve': {
        'type': 'list', 'required': False, 'schema': {
            'type': 'dict', 'schema': {
                'name': {'type': 'string'}, 'end_value': {'type': 'number'},
                'start_score': {'type': 'number'}, 'end_score': {'type': 'number'}
            }
        }
    }
}

SOURCE_CRS = "EPSG:4326"
PROJECTED_CRS = "EPSG:3857"

def load_and_prepare_gdf(file_path: Path, required_columns: List[str], id_col: str = None) -> gpd.GeoDataFrame:
    """Loads a CSV, validates columns, cleans coordinates, and creates a GeoDataFrame."""
    dtype_spec = {id_col: str} if id_col else {}
    df = pd.read_csv(file_path, dtype=dtype_spec)
    for col in required_columns:
        if col not in df.columns:
            raise ValueError(f"File {file_path.name} is missing required column: '{col}'")
    initial_count = len(df)
    df.dropna(subset=['lon', 'lat'], inplace=True)
    if len(df) < initial_count:
        print(f"   - WARNING: Removed {initial_count - len(df)} records with missing coordinates.")
    return gpd.GeoDataFrame(
        df, geometry=gpd.points_from_xy(df.lon, df.lat), crs=SOURCE_CRS
    ).to_crs(PROJECTED_CRS)

def _find_and_score_candidates(
        aux_record_row: pd.Series,
        spatial_index: SpatialIndex,
        search_radius: float,
        name_score_mode: str,
        name_curve: List[Dict],
        distance_curve: List[Dict]
) -> List[Dict]:
    """Finds and scores spatial candidates for a single auxiliary record."""

    cleaned_aux_name = aux_record_row['clean_name']
    cleaned_aux_name2 = aux_record_row['clean_name2']

    aux_geometry = aux_record_row['geometry']

    if aux_geometry.is_empty:
        return []

    near_candidates = spatial_index.find_within_distance(aux_geometry, search_radius)
    if not near_candidates:
        return []

    candidate_details = []
    for result in near_candidates:
        cleaned_target_name, cleaned_target_name2, original_target_name = result.data
        if not cleaned_target_name:
            continue

        raw_name_score = text_similarity(cleaned_aux_name, cleaned_target_name, cleaned_aux_name2, cleaned_target_name2, mode=name_score_mode)

        # Translate raw scores into quality scores using the prepared curves
        name_quality_score = calculate_piecewise_score(raw_name_score, name_curve)
        distance_quality_score = calculate_piecewise_score(result.distance, distance_curve)

        combined_score = harmonic_mean(name_quality_score, distance_quality_score)

        candidate_details.append({
            'id': result.id,
            'dist': result.distance,
            'name_sc': name_quality_score,
            'score': combined_score,
            'raw_name_sc': raw_name_score,
            'aux_name_clean': cleaned_aux_name,
            'target_name_clean': cleaned_target_name,
            'orig_name': original_target_name.lower(),
        })

    return sorted(candidate_details, key=lambda x: x['score'], reverse=True)

def match_and_format_results(
        args: argparse.Namespace,
        aux_gdf: gpd.GeoDataFrame,
        spatial_index: SpatialIndex,
        config: dict,
        name_curve: List[Dict],
        distance_curve: List[Dict]
) -> (List[Dict], List[Dict]):

    """Iterates through auxiliary records, finds matches, and formats results."""
    id_col = config['id_column']
    name_col = config['name_column']
    combined_threshold = config['combined_score_threshold']
    search_radius = config['search_radius']
    aux_columns = config['aux_output_columns']

    all_results = []
    explain_results = []

    # Iterate through auxiliary records
    for index, aux_record_row in tqdm(aux_gdf.iterrows(), total=len(aux_gdf), desc="Matching records"):
        sorted_candidates = _find_and_score_candidates(
            aux_record_row, spatial_index, search_radius, "harmonic_partial", name_curve, distance_curve
        )

        if not sorted_candidates:
            continue

        if args.explain:
            base_info = {'aux_name_original': aux_record_row[name_col].lower()}
            for i in range(min(args.explain, len(sorted_candidates))):
                candidate = sorted_candidates[i]
                row = base_info.copy()
                row['candidate_rank'] = i + 1
                row.update(candidate)
                explain_results.append(row)

        if sorted_candidates:
            best_candidate = sorted_candidates[0]
            if best_candidate['score'] >= combined_threshold:
                # --- Include the score for de-duplication ---
                match_data = {
                    'osm_id': best_candidate['id'],
                    'score': best_candidate['score'] # Pass the score along
                }
                for col in aux_columns:
                    match_data[col] = aux_record_row[col]
                all_results.append(match_data)

    return all_results, explain_results

"""
        candidate_details.append({
            'osm_id': result.id,
            'dist': result.distance,
            'name_sc': name_quality_score,
            'score': combined_score,
            'raw_name_sc': raw_name_score,
            'aux_name_clean': cleaned_aux_name,
            'target_name_clean': cleaned_target_name,
            'orig_name': original_target_name,
        })

"""

def main() -> None:
    """Orchestrates the fuzzy join process."""
    parser = argparse.ArgumentParser(description="Fuzzy join two geospatial CSV files.")
    parser.add_argument("--master", type=Path, required=True, help="Path to the master data CSV.")
    parser.add_argument("--auxiliary", type=Path, required=True, help="Path to the auxiliary data CSV.")
    parser.add_argument("--config", type=Path, required=True, help="Path to the YAML configuration file.")
    parser.add_argument("--output", type=Path, required=True, help="Path for the output file.")
    parser.add_argument("--explain", type=int, nargs='?', const=2, default=None, help="Activate explain mode.")
    parser.add_argument(
        "--ignore-errors",
        action="store_true", # This makes it a boolean flag.
        help="If present, the script will log warnings for data errors instead of exiting."
    )
    args = parser.parse_args()

    try:
        print(f"➡️ Loading configuration: {args.config}")
        loader = ConfigLoader(MATCH_SCHEMA)
        config = loader.read(args.config)
        name_col, id_col = config['name_column'], config['id_column']

        print("➡️ Preparing scoring and text cleaning configurations...")
        # If the key exists in the config, use it. Otherwise, use the default constant.
        name_curve_config = config.get('name_score_curve', DEFAULT_NAME_SCORE_CURVE)
        name_curve = prepare_and_validate_curve(name_curve_config)

        distance_curve_config = config.get('distance_score_curve', DEFAULT_DISTANCE_CURVE)
        distance_curve = prepare_and_validate_curve(distance_curve_config)

        cleaning_rules = config.get('text_cleaning_rules', [])

        noise_words = config.get('noise_words', [])
        noise_pattern = re.compile(r"\b(" + "|".join(re.escape(w) for w in noise_words) + r")\b", re.IGNORECASE)

        noise_words2 = config.get('noise_words2', [])
        noise_pattern2 = re.compile(r"\b(" + "|".join(re.escape(w) for w in (set(noise_words) | set(noise_words2))) + r")\b", re.IGNORECASE)
        print("   - Configurations prepared.")

        print("\n➡️ Loading and preparing datasets...")
        target_gdf = load_and_prepare_gdf(args.master, [id_col, name_col, 'lon', 'lat'], id_col)
        aux_gdf = load_and_prepare_gdf(args.auxiliary, [name_col, 'lon', 'lat'])
        print("   - Datasets loaded and reprojected.")

        if len(aux_gdf) > len(target_gdf) * 1.1:
            print(f"\n  ⚠️  - WARNING: Auxiliary file ({len(aux_gdf)}) is larger than master ({len(target_gdf)}).")
            print("      - --master and --auxiliary arguments may be swapped.\n")

        print("➡️ Pre-cleaning names...")
        target_gdf['clean_name'] = target_gdf[name_col].apply(lambda x: clean_text(x, noise_pattern,cleaning_rules)
                                                              )
        aux_gdf['clean_name'] = aux_gdf[name_col].apply(lambda x: clean_text(x, noise_pattern,cleaning_rules))

        target_gdf['clean_name2'] = target_gdf[name_col].apply(lambda x: clean_text(x, noise_pattern2,cleaning_rules)
                                                               )
        aux_gdf['clean_name2'] = aux_gdf[name_col].apply(lambda x: clean_text(x, noise_pattern2,cleaning_rules))
        print("   - Name cleaning complete.")

        spatial_index = SpatialIndex(approximate_distance=False)
        print("➡️ Building spatial index...")
        for index, target_row in tqdm(target_gdf.iterrows(), total=len(target_gdf), desc="Indexing master records"):
            try:
                if target_row['geometry'].is_empty: continue
                payload = (target_row['clean_name'], target_row['clean_name2'], target_row[name_col])
                spatial_index.add_point(int(target_row[id_col]), target_row['geometry'], data=payload)
            except Exception as e:
                if args.ignore_errors:
                    print(f"Spatial Index error: {e}  {args.master}, Row={index}, ID='{target_row[id_col]}'")
                    continue
                raise ValueError(f"❌Spatial Index error: {e}\n{args.master}, Row={index}, ID='{target_row[id_col]}'")

    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"❌ Error during setup: {e}")
        sys.exit(1)

    standard_results, explain_results = match_and_format_results(
        args, aux_gdf, spatial_index, config, name_curve, distance_curve
    )

    if not standard_results:
        print("⚠️ No valid matches found based on the threshold. Standard output file will be empty.")

    initial_output_df = pd.DataFrame(standard_results)

    # Ensure there are no duplicates
    if not initial_output_df.empty:
        print(f"➡️ Found {len(initial_output_df)} initial matches. De-duplicating...")

        # Sort by the combined_score in descending order. This brings the best
        # match for each target_id to the top of its group.
        initial_output_df.sort_values(by='score', ascending=False, inplace=True)

        # Drop duplicates on the 'id_column', keeping the first (highest score) entry.
        final_output_df = initial_output_df.drop_duplicates(subset=[id_col], keep='first')

        # Now, drop the temporary 'score' column before saving.
        final_output_df.drop(columns=['score'], inplace=True)

        num_dupes_removed = len(initial_output_df) - len(final_output_df)
        if num_dupes_removed > 0:
            print(f"   - Removed {num_dupes_removed} duplicate matches to ensure a unique output.")

        output_df = final_output_df

    args.output.parent.mkdir(parents=True, exist_ok=True)
    print(f"\n➡️ Saving enrichment data to: {args.output}")
    output_df.to_csv(args.output, index=False, float_format='%.2f')

    if args.explain:
        if not explain_results:
            print("⚠️ No candidates found for any records. Explain file will not be generated.")
        else:
            out_p = args.output
            explain_path = out_p.with_name(f"{out_p.stem}_explain{out_p.suffix}")
            explain_df = pd.DataFrame(explain_results)
            print(f"➡️ Saving explain report to: {explain_path}")
            explain_df.to_csv(explain_path, index=False, float_format='%.2f')

    print("\n*️⃣  Geo Fuzzy Summary:")
    print(f"   - Master records:          {len(target_gdf)}")
    print(f"   - Auxiliary records:       {len(aux_gdf)}")
    successful_matches = len(output_df)
    print(f"   - Successful matches found: {successful_matches}")
    if len(aux_gdf) > 0:
        match_rate = (successful_matches / len(aux_gdf)) * 100
        print(f"   - Match Rate:              {match_rate:.2f}%")
    print("\n✅ Geo Fuzzy done.")

if __name__ == "__main__":
    main()