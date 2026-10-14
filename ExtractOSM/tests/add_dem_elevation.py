#!/usr/bin/env python3
"""Validate and correct CSV elevations using a DEM.
The DEM is treated as a rough reference. Existing CSV elevations are preserved
unless one of the correction rules applies.

The input CSV must contain latitude, longitude, and elevation columns.

Processing rules:
    - Rows outside the DEM extent are omitted from the output.
    - DEM NoData leaves the CSV elevation unchanged.
    - Replace the CSV elevation when:
        * the CSV elevation is missing and the DEM elevation is valid;
        * the absolute CSV/DEM difference is greater than 1000 m; or
        * the CSV elevation is 0 m and the DEM elevation is greater than 10 m.


"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import rasterio
from pyproj import Transformer
from tqdm import tqdm


DEFAULT_LAT_COLUMN = "lat"
DEFAULT_LON_COLUMN = "lon"
DEFAULT_ELEV_COLUMN = "ele"

ELEVATION_DELTA_THRESHOLD_M = 1000.0
ZERO_ELEV_DEM_THRESHOLD_M = 10.0


@dataclass(frozen=True, slots=True)
class DemSample:
    """Result of sampling one coordinate from the DEM."""

    in_extent: bool
    elevation: float | None


def count_csv_rows(csv_path: Path) -> int:
    """Return the number of data rows in a CSV, excluding the header."""
    with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
        return max(sum(1 for _ in handle) - 1, 0)


def iter_rows(
    reader: csv.DictReader,
    limit: int | None,
) -> Iterator[dict[str, str]]:
    """Yield rows, stopping after *limit* rows when specified."""
    for index, row in enumerate(reader):
        if limit is not None and index >= limit:
            return
        yield row


def parse_coordinate(
    row: dict[str, str],
    column: str,
    row_number: int,
) -> float:
    """Parse one coordinate value and raise a useful error if invalid."""
    raw_value = row.get(column)
    if raw_value is None:
        raise ValueError(f"CSV is missing required column '{column}'.")

    try:
        return float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid {column!r} value on CSV row {row_number}: {raw_value!r}"
        ) from exc


def parse_elevation(
    row: dict[str, str],
    column: str,
    row_number: int,
) -> float | None:
    """Parse an optional elevation, treating an empty value as missing."""
    raw_value = row.get(column)
    if raw_value is None:
        raise ValueError(f"CSV is missing required column '{column}'.")

    raw_value = raw_value.strip()
    if not raw_value:
        return None

    try:
        return float(raw_value)
    except ValueError as exc:
        raise ValueError(
            f"Invalid {column!r} value on CSV row {row_number}: {raw_value!r}"
        ) from exc


def sample_dem_value(dem, x: float, y: float) -> DemSample:
    """Sample the DEM, distinguishing outside extent from NoData."""
    row, col = dem.index(x, y)

    if row < 0 or row >= dem.height or col < 0 or col >= dem.width:
        return DemSample(in_extent=False, elevation=None)

    sampled = next(dem.sample([(x, y)], indexes=1, masked=True))[0]

    if sampled is None or getattr(sampled, "mask", False):
        return DemSample(in_extent=True, elevation=None)

    elevation = float(sampled)
    if dem.nodata is not None and elevation == dem.nodata:
        return DemSample(in_extent=True, elevation=None)

    return DemSample(in_extent=True, elevation=elevation)


def should_replace_elevation(
    csv_elevation: float | None,
    dem_elevation: float,
) -> bool:
    """Return whether the DEM value should replace the CSV elevation."""
    if csv_elevation is None:
        return True

    if abs(csv_elevation - dem_elevation) > ELEVATION_DELTA_THRESHOLD_M:
        return True

    return csv_elevation == 0 and dem_elevation > ZERO_ELEV_DEM_THRESHOLD_M


def format_elevation(elevation: float) -> str:
    """Format a DEM-derived elevation in whole meters."""
    return str(round(elevation))


def correct_dem_elevation(
    input_csv: Path,
    dem_path: Path,
    output_csv: Path,
    *,
    lat_column: str = DEFAULT_LAT_COLUMN,
    lon_column: str = DEFAULT_LON_COLUMN,
    elev_column: str = DEFAULT_ELEV_COLUMN,
    limit: int | None = None,
) -> None:
    """Validate and correct CSV elevations using a DEM."""
    input_csv = Path(input_csv)
    dem_path = Path(dem_path)
    output_csv = Path(output_csv)

    row_count = count_csv_rows(input_csv)
    rows_to_process = min(row_count, limit) if limit is not None else row_count

    corrected_count = 0
    outside_count = 0
    nodata_count = 0
    written_count = 0

    with (
        rasterio.open(dem_path) as dem,
        input_csv.open("r", newline="", encoding="utf-8-sig") as input_handle,
        output_csv.open("w", newline="", encoding="utf-8") as output_handle,
    ):
        if dem.crs is None:
            raise ValueError(f"DEM has no CRS: {dem_path}")

        reader = csv.DictReader(input_handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {input_csv}")

        required_columns = (lat_column, lon_column, elev_column)
        missing = [name for name in required_columns if name not in reader.fieldnames]
        if missing:
            raise ValueError(
                f"CSV is missing required column(s): {', '.join(missing)}"
            )

        writer = csv.DictWriter(output_handle, fieldnames=reader.fieldnames)
        writer.writeheader()

        transformer = Transformer.from_crs(
            "EPSG:4326",
            dem.crs,
            always_xy=True,
        )

        with tqdm(
            total=rows_to_process,
            desc="Checking elevations",
            unit="row",
            dynamic_ncols=True,
        ) as progress:
            for row_number, row in enumerate(iter_rows(reader, limit), start=2):
                try:
                    lat = parse_coordinate(row, lat_column, row_number)
                    lon = parse_coordinate(row, lon_column, row_number)
                    csv_elevation = parse_elevation(row, elev_column, row_number)

                    x, y = transformer.transform(lon, lat)
                    sample = sample_dem_value(dem, x, y)

                    if not sample.in_extent:
                        outside_count += 1
                        continue

                    if sample.elevation is None:
                        nodata_count += 1
                    elif should_replace_elevation(csv_elevation, sample.elevation):
                        row[elev_column] = format_elevation(sample.elevation)
                        corrected_count += 1

                    writer.writerow(row)
                    written_count += 1
                finally:
                    progress.update(1)

    print(
        "Elevation check complete: "
        f"{written_count} rows written, "
        f"{corrected_count} elevations corrected, "
        f"{outside_count} rows outside DEM removed, "
        f"{nodata_count} DEM NoData samples."
    )


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Validate and correct CSV elevations using a DEM. "
            "Rows outside the DEM extent are omitted."
        )
    )
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("dem", type=Path)
    parser.add_argument("output_csv", type=Path)
    parser.add_argument("--lat-column", default=DEFAULT_LAT_COLUMN)
    parser.add_argument("--lon-column", default=DEFAULT_LON_COLUMN)
    parser.add_argument("--elev-column", default=DEFAULT_ELEV_COLUMN)
    parser.add_argument(
        "--limit",
        type=int,
        help="Process only the first N data rows.",
    )

    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be greater than zero.")
    return args


def main() -> None:
    """Run the command-line tool."""
    args = parse_args()
    correct_dem_elevation(
        args.input_csv,
        args.dem,
        args.output_csv,
        lat_column=args.lat_column,
        lon_column=args.lon_column,
        elev_column=args.elev_column,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
