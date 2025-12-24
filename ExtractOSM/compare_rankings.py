# compare_rankings.py
"""
Compares two scoring/ranking CSV files to analyze the impact of model changes.

This script is designed to help evaluate the effect of adding new features or
changing coefficients in a scoring model. It produces a new CSV file that
highlights the items whose ranks changed the most, allowing for focused
human evaluation of the model's behavior.
"""
import argparse
from pathlib import Path
import sys

import numpy as np
import pandas as pd


def main() -> None:
    """Main script execution."""
    parser = argparse.ArgumentParser(
        description="Compare two ranking CSV files to analyze the impact of model changes."
    )
    # --- TERMINOLOGY CHANGE: before -> baseline ---
    parser.add_argument(
        "--baseline", type=Path, required=True,
        help="Path to the baseline model's output CSV file."
    )
    # --- TERMINOLOGY CHANGE: after -> candidate ---
    parser.add_argument(
        "--candidate", type=Path, required=True,
        help="Path to the candidate model's output CSV file (the new version)."
    )
    parser.add_argument(
        "--output", type=Path, required=True,
        help="Path for the output comparison CSV file."
    )
    parser.add_argument(
        "--id-column", type=str, default="osm_id",
        help="The unique ID column to join the two files on. Defaults to 'osm_id'."
    )
    parser.add_argument(
        "--metric-column", type=str, default="score",
        help="The name of the column containing the ranking metric. Defaults to 'score'."
    )
    args = parser.parse_args()

    # --- Data Loading and Validation ---
    try:
        print(f"➡️ Loading baseline data from: {args.baseline}")
        df_baseline = pd.read_csv(args.baseline, dtype={args.id_column: str})

        print(f"➡️ Loading candidate data from: {args.candidate}")
        df_candidate = pd.read_csv(args.candidate, dtype={args.id_column: str})

        for df, name in [(df_baseline, "baseline"), (df_candidate, "candidate")]:
            if args.id_column not in df.columns:
                raise ValueError(f"ID column '{args.id_column}' not found in '{name}' file.")
            if args.metric_column not in df.columns:
                raise ValueError(f"Metric column '{args.metric_column}' not found in '{name}' file.")

    except (FileNotFoundError, ValueError) as e:
        print(f"   ❌ ERROR: {e}")
        sys.exit(1)

    # --- Ranking Calculation ---
    print("➡️ Calculating ranks for both datasets...")
    df_baseline['rank_baseline'] = df_baseline[args.metric_column].rank(method="first", ascending=False).astype(int)
    df_candidate['rank_candidate'] = df_candidate[args.metric_column].rank(method="first", ascending=False).astype(int)

    # --- Merging and Comparison ---
    print(f"➡️ Merging datasets on ID column: '{args.id_column}'...")

    # Select and rename columns for a clean merge.
    baseline_subset = df_baseline[[args.id_column, args.metric_column, 'rank_baseline']].rename(
        columns={args.metric_column: 'metric_baseline', 'rank_baseline': 'rank_baseline'}
    )

    # Keep all columns from the candidate file for context, renaming its metric and rank.
    candidate_subset = df_candidate.rename(
        columns={args.metric_column: 'metric_candidate', 'rank_candidate': 'rank_candidate'}
    )

    df_merged = pd.merge(
        baseline_subset,
        candidate_subset,
        on=args.id_column,
        how='inner'
    )

    if df_merged.empty:
        print(f"   ❌ ERROR: No common items found between the two files using ID column '{args.id_column}'.")
        sys.exit(1)

    print(f"   - Found {len(df_merged)} common items to compare.")

    # --- Impact Calculation ---
    print("➡️ Calculating metric and rank deltas...")
    df_merged['metric_delta'] = df_merged['metric_candidate'] - df_merged['metric_baseline']
    df_merged['rank_delta'] = df_merged['rank_baseline'] - df_merged['rank_candidate']
    df_merged['rank_abs_delta'] = df_merged['rank_delta'].abs()
    df_merged['rank_pct_change'] = (df_merged['rank_delta'] / (df_merged['rank_baseline'] + 1)) * 100
    df_merged['rank_impact_score'] = df_merged['rank_abs_delta'] * (1 / np.log1p(df_merged['rank_baseline']))


    # --- Sorting and Output ---
    print("➡️ Sorting by most impactful changes...")
    df_sorted = df_merged.sort_values(by='rank_impact_score', ascending=False).reset_index(drop=True)

    # Define the primary columns you want to see at the front of the report.
    front_columns_ideal = [
        args.id_column,
        'item_name',
        'rank_impact_score',
        'rank_abs_delta',
        'rank_pct_change',
        'rank_delta',
        'rank_baseline',
        'rank_candidate',
        'metric_delta',
        'metric_baseline',
        'metric_candidate'
    ]

    # Filter this list to only include columns that actually exist in our DataFrame.
    # This handles cases where 'item_name' might be missing.
    existing_front_columns = [col for col in front_columns_ideal if col in df_sorted.columns]

    # Get a list of all *other* columns (the original features) that are not in our front list.
    other_context_cols = [col for col in df_sorted.columns if col not in existing_front_columns]

    # Combine the two lists to create the final, complete, and correctly ordered column list.
    final_order = existing_front_columns + other_context_cols
    final_df = df_sorted[final_order]

    final_df.to_csv(args.output, index=False, float_format='%.2f')

    print(f"✅ Impact analysis complete. Results saved to '{args.output}'.")
    print("   - The top rows show the items most affected by your model changes.")


if __name__ == "__main__":
    main()