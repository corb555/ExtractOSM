# generate_area_score.py
"""Generates a category-aware 'area_score' from raw OSM feature area.

This script creates a feature that translates the raw area of a geographic
feature (e.g., a park or stadium) into a meaningful importance score. It is
designed to solve three common problems in geospatial feature engineering,
producing a final score that is comparable across different feature types.

The process is as follows:

1.  **Outlier Clipping (Optional):** To prevent extreme outliers (e.g., a state
    forest tagged as a 'park') from skewing the results, the script first
    applies a configurable clipping threshold to cap the maximum area for
    specified sub-categories.

2.  **Two-Track Scoring (Points vs. Polygons):** The script separates features
    based on their geometry type to handle inconsistent mapping in OSM:
    - **Points (area = 0):** Are assigned a fixed, constant low score. This
      acknowledges their presence without making inaccurate area imputations.
    - **Polygons (area > 0):** Are passed to a more sophisticated blended
      scoring algorithm.

3.  **Blended Score Calculation (for Polygons):** To create a score that reflects
    both a feature's rank and its absolute size, a weighted average of two
    metrics is calculated for each polygon, grouped by `sub_category`:
    - **Relative Score (Percentile Rank):** Measures how a feature's area
      ranks against its direct peers (e.g., this airport vs. all other
      airports).
    - **Absolute Score (Normalization by Effective Maximum):** Measures a
      feature's area as a percentage of a "realistic maximum" for its
      category, which is calculated as the mean area of the top percentile of
      features (e.g., the top 10%).

4.  **Final Per-Category Scaling:** The resulting 0-100 blended score is then
    scaled to a configurable maximum value for each `sub_category`. This step
    encodes the domain knowledge that area is a more significant indicator of
    importance for a 'stadium' (e.g., scaled to a max of 100) than it is for
    a 'restaurant' (e.g., scaled to a max of 15).

The final `area_score` is a consistent and comparable feature ready for use
in downstream machine learning models.
"""
import argparse
import sys
from pathlib import Path
import textwrap

import pandas as pd
from YMLEditor.yaml_reader import ConfigLoader

# Constants for the blending algorithm
EFFECTIVE_MAX_PERCENTILE = 0.90
RELATIVE_SCORE_WEIGHT = 0.2
ABSOLUTE_SCORE_WEIGHT = 0.8


def _perform_outlier_clipping(
        df: pd.DataFrame,
        polygon_config: list,
        clip_percentile: float = None
) -> pd.DataFrame:
    """
    Applies outlier clipping on a per-category basis and prints a full
    diagnostic report comparing manual vs. statistical thresholds.
    """
    print("➡️ Analyzing area outliers and generating diagnostic report...")
    clipped_df = df.copy()

    # --- 1. Calculate  statistical thresholds up front ---
    polygon_data = clipped_df[clipped_df['area'] > 0]
    if not polygon_data.empty:
        grouped = polygon_data.groupby('sub_category')['area']
        q1 = grouped.quantile(0.25)
        q3 = grouped.quantile(0.75)
        iqr_thresholds = q3 + (3.0 * (q3 - q1))
        mad_thresholds = grouped.median() + (3.5 * grouped.apply(lambda x: (x - x.median()).abs().median()) * 1.4826)
        percentile_thresholds = grouped.quantile(clip_percentile) if clip_percentile else pd.Series(dtype=float)
    else:
        iqr_thresholds, mad_thresholds, percentile_thresholds = [pd.Series(dtype=float)] * 3

    # --- 2. Print the rich, comparative report (The "as-is" part) ---
    print("\n--- Outlier Threshold Comparison Report ---")
    pctl_header = f"{clip_percentile:.0%} Pctl" if clip_percentile else "Pctl (Off)"
    header = (
        f"{'Sub-category':<20} | {'Manual Config':>15} | {'IQR Threshold':>15} | "
        f"{'MAD Threshold':>15} | {pctl_header:>15}"
    )
    print(header)
    print("-" * len(header))

    # We need to iterate through all categories present in the data to give a full report.
    all_sub_cats = sorted(list(clipped_df['sub_category'].unique()))

    for sub_cat in all_sub_cats:
        # Get the config for this specific sub-category
        cat_config = next((item for item in polygon_config if item['sub_category'] == sub_cat), None)

        manual_thresh = cat_config.get('clip_threshold') if cat_config else None

        # Get all statistical values for comparison
        iqr_thresh = iqr_thresholds.get(sub_cat)
        mad_thresh = mad_thresholds.get(sub_cat)
        pctl_thresh = percentile_thresholds.get(sub_cat)

        # Format for clean printing
        manual_str = f"{manual_thresh:,.0f}" if pd.notna(manual_thresh) else "N/A"
        iqr_str = f"{iqr_thresh:,.0f}" if pd.notna(iqr_thresh) else "N/A"
        mad_str = f"{mad_thresh:,.0f}" if pd.notna(mad_thresh) else "N/A"
        pctl_str = f"{pctl_thresh:,.0f}" if pd.notna(pctl_thresh) else "N/A"

        print(f"{sub_cat:<20} | {manual_str:>15} | {iqr_str:>15} | {mad_str:>15} | {pctl_str:>15}")

    print("-" * len(header))
    print("\n")

    # --- 3. Apply the clipping based on the configured logic ---
    print("➡️ Applying configured clipping thresholds...")
    for cat_config in polygon_config:
        sub_cat = cat_config['sub_category']
        manual_value = cat_config.get('clip_threshold')
        auto_method = cat_config.get('clip_method')

        final_threshold = None
        method_str = "None"

        if manual_value is not None:
            final_threshold = manual_value
            method_str = "Manual"
        elif auto_method == 'percentile' and clip_percentile:
            final_threshold = percentile_thresholds.get(sub_cat)
            method_str = f"Auto ({clip_percentile:.0%})"

        if final_threshold is not None:
            clip_mask = (clipped_df['sub_category'] == sub_cat) & (clipped_df['area'] > final_threshold)
            num_clipped = clip_mask.sum()
            if num_clipped > 0:
                clipped_df.loc[clip_mask, 'area'] = final_threshold
                print(f"   - Clipped {num_clipped} records in '{sub_cat}' using {method_str.lower()} threshold of {final_threshold:,.0f} m².")

    return clipped_df

def main() -> None:
    """Main script execution."""
    parser = argparse.ArgumentParser(description="Generate a blended, normalized area_score for OSM features.")
    parser.add_argument("--input", type=Path, required=True, help="Path to the input features CSV.")
    parser.add_argument("--config", type=Path, required=True, help="Path to the area score configuration YAML file.")
    parser.add_argument("--explain", action="store_true", help="Include intermediate calculation columns in the output.")
    parser.add_argument("--output", type=Path, required=True, help="Path for the output enhancement CSV.")
    args = parser.parse_args()

    try:
        print(f"➡️ Loading configuration from: {args.config}")
        # This schema needs to be defined or imported
        AREA_SCORE_SCHEMA = {
            'config_type': {'type': 'string', 'required': True},
            'point_feature_score': {'type': 'number', 'required': True},

            'clip_percentile': {'type': 'float', 'required': False, 'min': 0.0, 'max': 1.0, 'default': 0.95},

            'sub_categories': {
                'type': 'list', 'required': True, 'schema': {
                    'type': 'dict', 'schema': {
                        'sub_category': {'type': 'string', 'required': True},
                        'max_score': {'type': 'number', 'required': True},
                        'clip_threshold': {'type': 'number', 'required': False},
                        'clip_method': {'type': 'string', 'required': False, 'allowed': ['percentile']},
                    }
                }
            }
        }

        loader = ConfigLoader(AREA_SCORE_SCHEMA)
        config = loader.read(args.config)

        point_feature_score = config['point_feature_score']
        polygon_config = config['sub_categories']
        clip_percentile = config.get('clip_percentile', 0.95)

        max_score_map = {item['sub_category']: item['max_score'] for item in polygon_config}
        sub_categories_to_process = set(max_score_map.keys())

        print(f"➡️ Loading input features from: {args.input}")
        df = pd.read_csv(args.input, dtype={'osm_id': str})

        required_cols = ['osm_id', 'sub_category', 'area']
        for col in required_cols:
            if col not in df.columns:
                raise ValueError(f"Input CSV is missing required column: '{col}'")
        print(f"   - Loaded {len(df)} total records.")

    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"   ❌ ERROR: {e}")
        sys.exit(1)

    print(f"➡️ Filtering for {len(sub_categories_to_process)} configured sub-categories...")
    initial_count = len(df)
    all_data_sub_cats = set(df['sub_category'].unique())
    dropped_sub_cats = all_data_sub_cats - sub_categories_to_process
    keep_mask = df['sub_category'].isin(sub_categories_to_process)
    df_filtered = df[keep_mask].copy()

    print(f"   - Kept {len(df_filtered)} of {initial_count} initial records.")
    if dropped_sub_cats:
        print(f"   - ⚠️  Dropped {len(dropped_sub_cats)} sub-categories not found in config:")
        dropped_list_str = ", ".join(sorted(list(dropped_sub_cats)))
        print(textwrap.fill(f"     {dropped_list_str}", width=80, subsequent_indent='     '))

    clipped_df = _perform_outlier_clipping(df_filtered, polygon_config, clip_percentile)

    print("➡️ Separating features by geometry type (point vs. polygon)...")
    point_mask = (clipped_df['area'].fillna(0) == 0)
    polygon_df = clipped_df[~point_mask].copy()
    point_df = clipped_df[point_mask].copy()

    print(f"   - Found {len(polygon_df)} polygon features and {len(point_df)} point features.")
    # --- DE-DUPLICATION LOGIC ---
    #  Identify any IDs that exist as both a point and a polygon.
    polygon_ids = set(polygon_df['osm_id'])
    point_ids_to_remove = point_df[point_df['osm_id'].isin(polygon_ids)]

    if not point_ids_to_remove.empty:
        print(f"   - ⚠️  Found {len(point_ids_to_remove)} features that exist as both points and polygons.")
        print("      - Discarding the point versions and prioritizing the polygon data.")
        # 2. Filter the point_df to remove these duplicates.
        point_df = point_df[~point_df['osm_id'].isin(polygon_ids)].copy()


    point_df['area_score'] = point_feature_score

    if not polygon_df.empty:
        print("➡️ Calculating blended area scores for polygon features...")
        polygon_df['relative_score'] = polygon_df.groupby('sub_category')['area'].rank(pct=True) * 100

        effective_max_map = {}
        for sub_cat, group in polygon_df.groupby('sub_category'):
            top_tier_threshold = group['area'].quantile(EFFECTIVE_MAX_PERCENTILE)
            top_tier_items = group[group['area'] >= top_tier_threshold]
            effective_max = top_tier_items['area'].mean()
            effective_max_map[sub_cat] = effective_max if pd.notna(effective_max) and effective_max > 0 else 1

        polygon_df['effective_max'] = polygon_df['sub_category'].map(effective_max_map).fillna(1)
        polygon_df['absolute_score'] = (polygon_df['area'] / polygon_df['effective_max']).clip(upper=1.0) * 100
        polygon_df['absolute_score'] = polygon_df['absolute_score'].fillna(0)

        polygon_df['blended_score_0_100'] = (
                (polygon_df['relative_score'] * RELATIVE_SCORE_WEIGHT) +
                (polygon_df['absolute_score'] * ABSOLUTE_SCORE_WEIGHT)
        )

        polygon_df['max_score'] = polygon_df['sub_category'].map(max_score_map)
        polygon_df['area_score'] = (polygon_df['blended_score_0_100'] / 100) * polygon_df['max_score']
    else:
        print("   - No polygon features found to score.")

    print("➡️ Combining scored points and polygons...")
    final_df = pd.concat([polygon_df, point_df], ignore_index=True)

    if args.explain:
        output_cols = [
            'osm_id', 'item_name', 'sub_category', 'area', 'relative_score',
            'absolute_score', 'blended_score_0_100', 'max_score', 'area_score'
        ]
    else:
        output_cols = ['osm_id', 'area_score']

    final_output_cols = [col for col in output_cols if col in final_df.columns]
    output_df = final_df[final_output_cols]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    print(f"\n➡️ Saving {len(output_df)} records with area scores to: {args.output}")
    output_df.to_csv(args.output, index=False, float_format='%.2f')

    print("✅ Area score generation complete.")

if __name__ == "__main__":
    main()