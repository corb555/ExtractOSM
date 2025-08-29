import pandas as pd
import re
import argparse

def extract_meters(value):
    match = re.search(r"(\d+(?:,\d+)*\s*ft)?\s*([\d,\.]+)\s*m", str(value))
    if match:
        return float(match.group(2).replace(",", ""))
    return None

def main():
    parser = argparse.ArgumentParser(description="Clean mountain CSV by extracting meters and simplifying names.")
    parser.add_argument("input_csv", help="Input CSV file")
    parser.add_argument("output_csv", nargs="?", default="clean_mountains.csv", help="Output CSV file (optional)")

    args = parser.parse_args()

    df = pd.read_csv(args.input_csv, header=None)
    df.columns = ["name", "region", "range", "elevation", "prominence"]

    df["name"] = df["name"].str.replace(r"\[.*", "", regex=True).str.strip()
    df["elevation_m"] = df["elevation"].apply(extract_meters)
    df["prominence_m"] = df["prominence"].apply(extract_meters)
    df = df.drop(columns=["elevation", "prominence"])

    df.to_csv(args.output_csv, index=False)
    print(f"✅ Cleaned data written to {args.output_csv}")

if __name__ == "__main__":
    main()

