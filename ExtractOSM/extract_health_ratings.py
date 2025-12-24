# health_ratings.py
"""
Processes raw King County restaurant health inspection data to produce a clean,
canonical list of the most recent graded inspection for each unique restaurant
location.
...
"""
import argparse
from pathlib import Path
import sys

import pandas as pd

def main():
    # Map King county score to a standard score for the Map Pipeline
    grade_to_score_map = {
        1.0: 10,  # Excellent
        2.0: 0,   # Good
        3.0: -20, # Needs Improvement
        4.0: -45  # Unsatisfactory
    }

    # county_state or city_state
    agencies = [
        "king_wa",
                ]

    INSPECTION_TYPE_COLUMN = "Inspection Type"
    IGNORE_INSPECTION_TYPES = ["Consultation/Education - Field"]

    parser = argparse.ArgumentParser(
        description="Extract the latest graded health inspection for each restaurant."
    )
    parser.add_argument("--input", type=Path, required=True, help="Path to the raw health ratings CSV file.")
    parser.add_argument("--output", type=Path, required=True, help="Path to save the cleaned ratings CSV file.")
    parser.add_argument("--agency", type=str, required=True, help="Source agency for records")

    args = parser.parse_args()

    print("extract-health-ratings")

    if args.agency not in agencies:
        print(f"❌ ERROR: Agency '{args.agency}' is not supported")
        print(f"Supported agencies: {agencies}")
        sys.exit(1)

    print(f"➡️ Loading raw health inspection data from: {args.input}")

    # Rename columns on load for consistency with the rest of the pipeline
    column_map = {
        'Name': 'item_name',
        'Inspection Date': 'inspection_date',
        'Longitude': 'lon',
        'Latitude': 'lat',
        'Grade': 'health_grade', # Use a distinct name for the raw grade
        INSPECTION_TYPE_COLUMN : INSPECTION_TYPE_COLUMN,
    }

    # --- START OF FIX: Add a pre-flight schema validation check ---
    try:
        # Read only the first row to get the header efficiently.
        header_df = pd.read_csv(args.input, nrows=0)
        actual_columns = set(header_df.columns)
        expected_columns = set(column_map.keys())

        # Check if all expected columns are present in the file.
        if not expected_columns.issubset(actual_columns):
            missing = sorted(list(expected_columns - actual_columns))
            error_message = f"""
    ❌ FATAL ERROR: Input file '{args.input}' is missing required columns.
       - Missing Columns: {missing}
       - Expected Columns: {sorted(list(expected_columns))}
       - Actual Columns Found: {sorted(list(actual_columns))}
       
       Please verify the input file format or update the 'column_map' in the script.
"""
            raise ValueError(error_message)

    except (FileNotFoundError, ValueError) as e:
        print(f"❌ Error validating input file: {e}")
        sys.exit(1)

    df = pd.read_csv(args.input, usecols=column_map.keys()).rename(columns=column_map)
    print(f"   - Loaded {len(df)} total inspection records.")

    # --- Step 0: Filter for records based on inspection type ---
    print("➡️ Filtering out non-routine inspection types...")
    initial_count = len(df)

    # Create a boolean mask to find rows that should be KEPT.
    keep_mask = ~df[INSPECTION_TYPE_COLUMN].isin(IGNORE_INSPECTION_TYPES)
    df = df[keep_mask].copy()

    print(f"   - Removed {initial_count - len(df)} records with ignored inspection types (e.g., 'Consultation').")
    # Drop the now-redundant inspection type column
    df.drop(columns=[INSPECTION_TYPE_COLUMN], inplace=True)

    # --- Step 1: Filter for records that have a grade ---
    initial_count = len(df)
    df.dropna(subset=['health_grade'], inplace=True)
    print(f"   - Removed {initial_count - len(df)} records without a grade.")

    # --- Step 2: Convert date column and handle errors ---
    df['inspection_date'] = pd.to_datetime(df['inspection_date'], errors='coerce')
    df.dropna(subset=['inspection_date'], inplace=True)

    # --- Step 3: Sort and de-duplicate by unique location ---
    print("➡️ Finding the most recent inspection for each unique restaurant location...")

    # Define the composite key that uniquely identifies a restaurant location.
    unique_location_key = ['item_name', 'lon', 'lat']

    # Sort by the unique key, then by date descending. This brings the most
    # recent inspection for each unique location to the top of its group.
    df.sort_values(by=unique_location_key + ['inspection_date'], ascending=[True, True, True, False], inplace=True)

    initial_count = len(df)
    # De-duplicate based on the unique location key, keeping the first (most recent) record.
    df.drop_duplicates(subset=unique_location_key, keep='first', inplace=True)
    print(f"   - Distilled {initial_count} graded records down to {len(df)} unique restaurant locations.")

    # --- Step 4: Transform 'Grade' into a standardized 'health_score' ---
    print("➡️ Transforming raw grades into a standardized health score...")
    df['health_score'] = df['health_grade'].map(grade_to_score_map)

    # Drop the original grade column, keeping only the final score and date
    df.drop(columns=['health_grade'], inplace=True)
    print("   - Transformation complete.")

    # --- Step 5: Save the clean data ---
    args.output.parent.mkdir(parents=True, exist_ok=True)
    print(f"\n➡️ Saving cleaned data with health scores to: {args.output}")
    df.to_csv(args.output, index=False)
    print("✅ Processing complete.")

if __name__ == "__main__":
    main()