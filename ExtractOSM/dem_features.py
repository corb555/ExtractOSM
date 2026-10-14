#!/usr/bin/env python3
"""Extract and validate CSV features using a DEM.

This utility provides shared DEM access for feature-processing operations that use
latitude/longitude CSV rows.

Operations
----------
``--validate_elevation``
    Validate and, when appropriate, correct  elevation values using the DEM as
    a rough reference.

``--local_relief``
    Measure how far each feature rises above the median and 25th-percentile terrain
    in one or more surrounding annuli. This is useful for identifying locally dominant landforms
    such as buttes, towers, monoliths, and isolated peaks without conflating the
    result with formal topographic prominence.

The input CSV must contain latitude and longitude columns. ``--validate_elevation`` also
requires an elevation column.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Iterator, Self

import numpy as np
import rasterio
from pyproj import Geod, Transformer
from rasterio.io import DatasetReader
from rasterio.windows import Window, from_bounds
from tqdm import tqdm


DEFAULT_LAT_COLUMN = "lat"
DEFAULT_LON_COLUMN = "lon"
DEFAULT_ELEV_COLUMN = "ele"
DEFAULT_LOCAL_RELIEF_INNER_RADIUS_M = 500.0
DEFAULT_LOCAL_RELIEF_OUTER_RADIUS_M = 1500.0
DEFAULT_MINIMUM_VALID_FRACTION = 0.70
DEFAULT_RELIEF_COLUMN_PREFIX = "local_relief"

ELEVATION_DELTA_THRESHOLD_M = 1000.0
ZERO_ELEV_DEM_THRESHOLD_M = 10.0
WGS84_CRS = "EPSG:4326"
WGS84_GEOD = Geod(ellps="WGS84")


@dataclass(frozen=True, slots=True)
class DemSample:
    """Result of sampling one coordinate from a DEM.

    Attributes:
        in_extent: Whether the coordinate lies inside the DEM raster extent.
        elevation: DEM elevation in raster units, or ``None`` for NoData.
    """

    in_extent: bool
    elevation: float | None


@dataclass(frozen=True, slots=True)
class ReliefRing:
    """Annulus used to measure surrounding terrain.

    Args:
        inner_radius_m: Radius excluded around the feature, in meters.
        outer_radius_m: Outer radius of the surrounding terrain sample, in meters.
    """

    inner_radius_m: float
    outer_radius_m: float

    def __post_init__(self) -> None:
        if self.inner_radius_m < 0:
            raise ValueError("Relief-ring inner radius must be zero or greater.")
        if self.outer_radius_m <= self.inner_radius_m:
            raise ValueError("Relief-ring outer radius must exceed inner radius.")

    @property
    def output_suffix(self) -> str:
        """Return a stable suffix suitable for an output CSV column name."""
        return f"{_format_radius(self.inner_radius_m)}_{_format_radius(self.outer_radius_m)}m"


@dataclass(frozen=True, slots=True)
class LocalReliefMeasurement:
    """Local-relief result for one annulus.

    Attributes:
        ring: Annulus used for the measurement.
        median_elevation: Median valid DEM elevation in the annulus.
        p25_elevation: 25th-percentile valid DEM elevation in the annulus.
        median_relief_m: Summit DEM elevation minus annulus median elevation.
        p25_relief_m: Summit DEM elevation minus annulus 25th-percentile elevation.
        valid_fraction: Fraction of annulus cells containing valid DEM data.
    """

    ring: ReliefRing
    median_elevation: float | None
    p25_elevation: float | None
    median_relief_m: float | None
    p25_relief_m: float | None
    valid_fraction: float


class DemRaster:
    """Open a DEM once and provide reusable coordinate-based sampling.

    The class owns the Rasterio dataset and WGS84/DEM coordinate transformers.
    Point sampling and local-relief window sampling share the same raster setup,
    extent checks, CRS handling, and NoData rules.

    Args:
        dem_path: Path to the DEM raster.

    Raises:
        ValueError: If the DEM has no CRS.
    """

    def __init__(self, dem_path: Path) -> None:
        self._dem_path = Path(dem_path)
        self._dataset: DatasetReader | None = None
        self._to_dem: Transformer | None = None
        self._to_wgs84: Transformer | None = None

    def __enter__(self) -> Self:
        dataset = rasterio.open(self._dem_path)
        if dataset.crs is None:
            dataset.close()
            raise ValueError(f"DEM has no CRS: {self._dem_path}")

        self._dataset = dataset
        self._to_dem = Transformer.from_crs(WGS84_CRS, dataset.crs, always_xy=True)
        self._to_wgs84 = Transformer.from_crs(dataset.crs, WGS84_CRS, always_xy=True)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._dataset is not None:
            self._dataset.close()
        self._dataset = None
        self._to_dem = None
        self._to_wgs84 = None

    @property
    def dataset(self) -> DatasetReader:
        """Return the open Rasterio dataset.

        Raises:
            RuntimeError: If the DEM is not open inside a context manager.
        """
        if self._dataset is None:
            raise RuntimeError("DEM is not open. Use DemRaster as a context manager.")
        return self._dataset

    def transform(self, lon: float, lat: float) -> tuple[float, float]:
        """Transform WGS84 longitude/latitude into the DEM CRS."""
        if self._to_dem is None:
            raise RuntimeError("DEM is not open. Use DemRaster as a context manager.")
        return self._to_dem.transform(lon, lat)

    def sample_lat_lon(self, lat: float, lon: float) -> DemSample:
        """Sample the DEM at a WGS84 latitude/longitude coordinate."""
        x, y = self.transform(lon, lat)
        return self.sample_xy(x, y)

    def sample_xy(self, x: float, y: float) -> DemSample:
        """Sample the DEM in raster coordinates."""
        dem = self.dataset
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

    def local_relief(
        self,
        lat: float,
        lon: float,
        rings: tuple[ReliefRing, ...],
        minimum_valid_fraction: float,
    ) -> tuple[DemSample, tuple[LocalReliefMeasurement, ...]]:
        """Measure summit relief above median surrounding terrain.

        The DEM is read once using a window large enough for the largest requested
        annulus. Each ring then reuses that window. The summit itself is excluded
        by the inner radius, preventing a narrow butte or monolith from raising its
        own surrounding-terrain median.

        Args:
            lat: Feature latitude in decimal degrees.
            lon: Feature longitude in decimal degrees.
            rings: One or more annuli to measure.
            minimum_valid_fraction: Minimum fraction of annulus cells that must
                contain valid DEM values. Rings below this threshold return no
                relief measurement.

        Returns:
            A tuple containing the summit DEM sample and one measurement per ring.
            If the summit is outside the DEM or is NoData, all relief values are
            ``None``.
        """
        if not rings:
            raise ValueError("At least one relief ring is required.")
        if not 0.0 <= minimum_valid_fraction <= 1.0:
            raise ValueError("minimum_valid_fraction must be between 0 and 1.")

        x, y = self.transform(lon, lat)
        summit = self.sample_xy(x, y)
        if not summit.in_extent or summit.elevation is None:
            return summit, tuple(
                LocalReliefMeasurement(ring, None, None, None, None, 0.0) for ring in rings
            )

        max_radius_m = max(ring.outer_radius_m for ring in rings)
        window = self._window_for_radius(lat, lon, x, y, max_radius_m)
        if window.width <= 0 or window.height <= 0:
            return summit, tuple(
                LocalReliefMeasurement(ring, None, None, None, None, 0.0) for ring in rings
            )

        dem = self.dataset
        values = dem.read(
            1,
            window=window,
            masked=True,
            boundless=True,
        )
        distances_m = self._cell_center_distances_m(window, lat, lon, x, y)
        value_mask = np.ma.getmaskarray(values)
        finite_mask = np.isfinite(np.asarray(values.data, dtype=float))
        valid_data_mask = ~value_mask & finite_mask

        measurements: list[LocalReliefMeasurement] = []
        for ring in rings:
            annulus_mask = (
                (distances_m >= ring.inner_radius_m)
                & (distances_m <= ring.outer_radius_m)
            )
            annulus_cell_count = int(np.count_nonzero(annulus_mask))
            valid_mask = annulus_mask & valid_data_mask
            valid_cell_count = int(np.count_nonzero(valid_mask))
            valid_fraction = (
                valid_cell_count / annulus_cell_count if annulus_cell_count else 0.0
            )

            if (
                annulus_cell_count == 0
                or valid_cell_count == 0
                or valid_fraction < minimum_valid_fraction
            ):
                measurements.append(
                    LocalReliefMeasurement(ring, None, None, None, None, valid_fraction)
                )
                continue

            valid_values = np.asarray(values.data, dtype=float)[valid_mask]
            median_elevation = float(np.median(valid_values))
            p25_elevation = float(np.percentile(valid_values, 25.0))
            median_relief_m = summit.elevation - median_elevation
            p25_relief_m = summit.elevation - p25_elevation
            measurements.append(
                LocalReliefMeasurement(
                    ring=ring,
                    median_elevation=median_elevation,
                    p25_elevation=p25_elevation,
                    median_relief_m=median_relief_m,
                    p25_relief_m=p25_relief_m,
                    valid_fraction=valid_fraction,
                )
            )

        return summit, tuple(measurements)

    def _window_for_radius(
        self,
        lat: float,
        lon: float,
        x: float,
        y: float,
        radius_m: float,
    ) -> Window:
        """Return the full raster window covering a radius around a point.

        The window is intentionally not clipped to the DEM extent. Local-relief
        reads use Rasterio boundless mode so cells outside the DEM remain masked
        and count against the requested annulus coverage. This prevents features
        near DEM edges from appearing to have complete terrain coverage when only
        part of the annulus is available.
        """
        dem = self.dataset

        if dem.crs is not None and dem.crs.is_projected:
            unit_to_m = _projected_unit_to_meters(dem)
            radius_units = radius_m / unit_to_m
            bounds = (
                x - radius_units,
                y - radius_units,
                x + radius_units,
                y + radius_units,
            )
        else:
            west_lon, _, _ = WGS84_GEOD.fwd(lon, lat, 270.0, radius_m)
            east_lon, _, _ = WGS84_GEOD.fwd(lon, lat, 90.0, radius_m)
            _, south_lat, _ = WGS84_GEOD.fwd(lon, lat, 180.0, radius_m)
            _, north_lat, _ = WGS84_GEOD.fwd(lon, lat, 0.0, radius_m)
            west_x, south_y = self.transform(west_lon, south_lat)
            east_x, north_y = self.transform(east_lon, north_lat)
            bounds = (
                min(west_x, east_x),
                min(south_y, north_y),
                max(west_x, east_x),
                max(south_y, north_y),
            )

        raw_window = from_bounds(*bounds, transform=dem.transform)
        return raw_window.round_offsets().round_lengths()

    def _cell_center_distances_m(
        self,
        window: Window,
        lat: float,
        lon: float,
        x: float,
        y: float,
    ) -> np.ndarray:
        """Return feature-to-cell-center distances for a raster window in meters."""
        dem = self.dataset
        rows = np.arange(int(window.height))
        cols = np.arange(int(window.width))
        col_grid, row_grid = np.meshgrid(cols, rows)
        transform = dem.window_transform(window)

        center_x = (
            transform.c
            + (col_grid + 0.5) * transform.a
            + (row_grid + 0.5) * transform.b
        )
        center_y = (
            transform.f
            + (col_grid + 0.5) * transform.d
            + (row_grid + 0.5) * transform.e
        )

        if dem.crs is not None and dem.crs.is_projected:
            unit_to_m = _projected_unit_to_meters(dem)
            return np.hypot(center_x - x, center_y - y) * unit_to_m

        if self._to_wgs84 is None:
            raise RuntimeError("DEM is not open. Use DemRaster as a context manager.")

        cell_lon, cell_lat = self._to_wgs84.transform(center_x, center_y)
        origin_lon = np.full(center_x.shape, lon, dtype=float)
        origin_lat = np.full(center_y.shape, lat, dtype=float)
        _, _, distance_m = WGS84_GEOD.inv(
            origin_lon,
            origin_lat,
            cell_lon,
            cell_lat,
        )
        return np.asarray(distance_m, dtype=float)


def _projected_unit_to_meters(dem: DatasetReader) -> float:
    """Return meters represented by one projected CRS unit.

    Args:
        dem: Open projected Rasterio dataset.

    Returns:
        Linear conversion factor from CRS units to meters.

    Raises:
        ValueError: If the projected CRS does not expose a usable linear unit.
    """
    crs = dem.crs
    if crs is None or not crs.is_projected:
        raise ValueError("DEM CRS is not projected.")

    try:
        _, factor = crs.linear_units_factor
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"Cannot determine linear units for DEM CRS: {crs}") from exc

    factor = float(factor)
    if factor <= 0:
        raise ValueError(f"Invalid linear-unit conversion for DEM CRS: {crs}")
    return factor


def _format_radius(radius_m: float) -> str:
    """Format a radius for a stable CSV column name."""
    return str(int(radius_m)) if radius_m.is_integer() else str(radius_m).replace(".", "p")


def count_csv_rows(csv_path: Path) -> int:
    """Return the number of data rows in a CSV, excluding the header."""
    with Path(csv_path).open("r", newline="", encoding="utf-8-sig") as handle:
        return max(sum(1 for _ in handle) - 1, 0)


def iter_rows(
    reader: csv.DictReader,
    limit: int | None,
) -> Iterator[dict[str, str]]:
    """Yield CSV rows, stopping after ``limit`` rows when specified."""
    for index, row in enumerate(reader):
        if limit is not None and index >= limit:
            return
        yield row


def parse_coordinate(
    row: dict[str, str],
    column: str,
    row_number: int,
) -> float:
    """Parse one coordinate value."""
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


def require_columns(fieldnames: list[str], required_columns: tuple[str, ...]) -> None:
    """Validate that a CSV contains all required columns."""
    missing = [name for name in required_columns if name not in fieldnames]
    if missing:
        raise ValueError(f"CSV is missing required column(s): {', '.join(missing)}")


def should_replace_elevation(
    csv_elevation: float | None,
    dem_elevation: float,
) -> bool:
    """Return whether the DEM elevation should replace the CSV value."""
    if csv_elevation is None:
        return True

    if abs(csv_elevation - dem_elevation) > ELEVATION_DELTA_THRESHOLD_M:
        return True

    return csv_elevation == 0 and dem_elevation > ZERO_ELEV_DEM_THRESHOLD_M


def format_elevation(elevation: float) -> str:
    """Format a DEM-derived elevation in whole meters."""
    return str(round(elevation))


def validate_elevations(
    input_csv: Path,
    dem_path: Path,
    output_csv: Path,
    *,
    lat_column: str = DEFAULT_LAT_COLUMN,
    lon_column: str = DEFAULT_LON_COLUMN,
    elev_column: str = DEFAULT_ELEV_COLUMN,
    limit: int | None = None,
) -> None:
    """Validate and correct  CSV elevations using a DEM."""
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
        DemRaster(dem_path) as dem,
        input_csv.open("r", newline="", encoding="utf-8-sig") as input_handle,
        output_csv.open("w", newline="", encoding="utf-8") as output_handle,
    ):
        reader = csv.DictReader(input_handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {input_csv}")

        require_columns(reader.fieldnames, (lat_column, lon_column, elev_column))

        writer = csv.DictWriter(output_handle, fieldnames=reader.fieldnames)
        writer.writeheader()

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

                    sample = dem.sample_lat_lon(lat, lon)
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


def add_local_relief(
    input_csv: Path,
    dem_path: Path,
    output_csv: Path,
    *,
    rings: tuple[ReliefRing, ...],
    minimum_valid_fraction: float = DEFAULT_MINIMUM_VALID_FRACTION,
    output_column_prefix: str = DEFAULT_RELIEF_COLUMN_PREFIX,
    lat_column: str = DEFAULT_LAT_COLUMN,
    lon_column: str = DEFAULT_LON_COLUMN,
    limit: int | None = None,
) -> None:
    """Add DEM-derived local-relief columns to a CSV.

    Args:
        input_csv: Source CSV containing latitude and longitude.
        dem_path: DEM raster used for terrain measurements.
        output_csv: Destination CSV.
        rings: Annuli used to measure surrounding terrain.
        minimum_valid_fraction: Minimum valid DEM coverage required per annulus.
        output_column_prefix: Prefix used for generated relief columns.
        lat_column: Latitude column name.
        lon_column: Longitude column name.
        limit: Optional maximum number of rows to process.
    """
    input_csv = Path(input_csv)
    dem_path = Path(dem_path)
    output_csv = Path(output_csv)

    if not rings:
        raise ValueError("At least one relief ring is required.")
    if not output_column_prefix.strip():
        raise ValueError("Local-relief output column prefix cannot be empty.")

    output_columns = [
        column
        for ring in rings
        for column in (
            f"{output_column_prefix}_{ring.output_suffix}_median",
            f"{output_column_prefix}_{ring.output_suffix}_p25",
        )
    ]
    if len(output_columns) != len(set(output_columns)):
        raise ValueError("Relief rings produce duplicate output column names.")

    row_count = count_csv_rows(input_csv)
    rows_to_process = min(row_count, limit) if limit is not None else row_count

    outside_count = 0
    summit_nodata_count = 0
    incomplete_ring_count = 0
    written_count = 0

    with (
        DemRaster(dem_path) as dem,
        input_csv.open("r", newline="", encoding="utf-8-sig") as input_handle,
        output_csv.open("w", newline="", encoding="utf-8") as output_handle,
    ):
        reader = csv.DictReader(input_handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {input_csv}")

        require_columns(reader.fieldnames, (lat_column, lon_column))
        existing_conflicts = [
            column for column in output_columns if column in reader.fieldnames
        ]
        if existing_conflicts:
            raise ValueError(
                "Output column(s) already exist in CSV: "
                + ", ".join(existing_conflicts)
            )

        fieldnames = [*reader.fieldnames, *output_columns]
        writer = csv.DictWriter(output_handle, fieldnames=fieldnames)
        writer.writeheader()

        with tqdm(
            total=rows_to_process,
            desc="Calculating local relief",
            unit="row",
            dynamic_ncols=True,
        ) as progress:
            for row_number, row in enumerate(iter_rows(reader, limit), start=2):
                try:
                    lat = parse_coordinate(row, lat_column, row_number)
                    lon = parse_coordinate(row, lon_column, row_number)
                    summit, measurements = dem.local_relief(
                        lat,
                        lon,
                        rings,
                        minimum_valid_fraction,
                    )

                    if not summit.in_extent:
                        outside_count += 1
                        continue
                    if summit.elevation is None:
                        summit_nodata_count += 1

                    for measurement in measurements:
                        median_column = (
                            f"{output_column_prefix}_{measurement.ring.output_suffix}_median"
                        )
                        p25_column = (
                            f"{output_column_prefix}_{measurement.ring.output_suffix}_p25"
                        )

                        if (
                            measurement.median_relief_m is None
                            or measurement.p25_relief_m is None
                        ):
                            row[median_column] = ""
                            row[p25_column] = ""
                            incomplete_ring_count += 1
                        else:
                            row[median_column] = format_elevation(
                                measurement.median_relief_m
                            )
                            row[p25_column] = format_elevation(measurement.p25_relief_m)

                    writer.writerow(row)
                    written_count += 1
                finally:
                    progress.update(1)

    print(
        "Local-relief analysis complete: "
        f"{written_count} rows written, "
        f"{outside_count} rows outside DEM removed, "
        f"{summit_nodata_count} summit NoData samples, "
        f"{incomplete_ring_count} ring measurements unavailable."
    )


def parse_relief_ring(value: str) -> ReliefRing:
    """Parse ``INNER:OUTER`` meters into a :class:`ReliefRing`."""
    try:
        inner_text, outer_text = value.split(":", maxsplit=1)
        return ReliefRing(float(inner_text), float(outer_text))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "Relief ring must be INNER:OUTER in meters, for example 500:1500."
        ) from exc


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Extract or validate CSV features using a DEM. Select one operation."
    )

    operation_group = parser.add_mutually_exclusive_group(required=True)
    operation_group.add_argument(
        "--validate_elevation",
        "--validate-elevation",
        dest="validate_elevation",
        action="store_true",
        help="Validate and correct  elevation values using the DEM.",
    )
    operation_group.add_argument(
        "--local_relief",
        "--local-relief",
        dest="local_relief",
        action="store_true",
        help=(
            "Add summit-minus-surrounding median and p25 local-relief features."
        ),
    )

    parser.add_argument("input_csv", type=Path)
    parser.add_argument("dem", type=Path)
    parser.add_argument("output_csv", type=Path)
    parser.add_argument("--lat-column", default=DEFAULT_LAT_COLUMN)
    parser.add_argument("--lon-column", default=DEFAULT_LON_COLUMN)
    parser.add_argument("--elev-column", default=DEFAULT_ELEV_COLUMN)
    parser.add_argument(
        "--ring",
        dest="rings",
        action="append",
        type=parse_relief_ring,
        help=(
            "Local-relief annulus as INNER:OUTER meters. Repeat for multiple scales. "
            f"Default: {int(DEFAULT_LOCAL_RELIEF_INNER_RADIUS_M)}:"
            f"{int(DEFAULT_LOCAL_RELIEF_OUTER_RADIUS_M)}."
        ),
    )
    parser.add_argument(
        "--minimum-valid-fraction",
        type=float,
        default=DEFAULT_MINIMUM_VALID_FRACTION,
        help=(
            "Minimum valid DEM-cell fraction required in each relief ring "
            f"(default: {DEFAULT_MINIMUM_VALID_FRACTION:.2f})."
        ),
    )
    parser.add_argument(
        "--relief-column-prefix",
        default=DEFAULT_RELIEF_COLUMN_PREFIX,
        help=(
            "Prefix for generated local-relief columns "
            f"(default: {DEFAULT_RELIEF_COLUMN_PREFIX})."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Process only the first N data rows.",
    )

    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be greater than zero.")
    if not 0.0 <= args.minimum_valid_fraction <= 1.0:
        parser.error("--minimum-valid-fraction must be between 0 and 1.")

    if args.local_relief and not args.rings:
        args.rings = [
            ReliefRing(
                DEFAULT_LOCAL_RELIEF_INNER_RADIUS_M,
                DEFAULT_LOCAL_RELIEF_OUTER_RADIUS_M,
            )
        ]
    return args


def main() -> None:
    """Run the selected DEM feature operation."""
    args = parse_args()

    if args.validate_elevation:
        validate_elevations(
            args.input_csv,
            args.dem,
            args.output_csv,
            lat_column=args.lat_column,
            lon_column=args.lon_column,
            elev_column=args.elev_column,
            limit=args.limit,
        )
        return

    if args.local_relief:
        add_local_relief(
            args.input_csv,
            args.dem,
            args.output_csv,
            rings=tuple(args.rings),
            minimum_valid_fraction=args.minimum_valid_fraction,
            output_column_prefix=args.relief_column_prefix,
            lat_column=args.lat_column,
            lon_column=args.lon_column,
            limit=args.limit,
        )
        return

    raise RuntimeError("No DEM operation selected.")


if __name__ == "__main__":
    main()
