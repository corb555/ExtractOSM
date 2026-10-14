#!/usr/bin/env python3
"""
Rasterize weighted river centerlines onto the exact grid of a reference raster.

Each river reach is assigned a geographic width by linearly scaling a numeric
weight field, buffered by half that width, and rasterized as a binary mask.

The output exactly matches the reference raster's CRS, transform, extent, width,
and height. River pixels are 1 by default. Unused pixels are explicitly written
as 0, and 0 is also the default NoData value.

Example:

    river_rasterize.py \
        data/shapefiles/hydroshed/uswest_rivers.gpkg \
        build/Sedona/interim/Sedona_EVT_classes.tif \
        build/Sedona/interim/Sedona_river_mask.tif \
        --min-weight 20 \
        --min-width-m 10 \
        --max-width-m 250 \
        --exclude-mask data/shapefiles/hydroshed/dry_basins.gpkg

Default weight mapping:

    weight 0.05 -> --min-width-m
    weight 600  -> --max-width-m

Widths are in metres, not pixels. The reference raster must therefore use a
projected CRS with metre units, such as EPSG:3857.

One or more ``--exclude-mask`` vector files may be supplied. Their polygon
geometries are unioned and removed from buffered river geometry before
rasterization, so excluded areas remain background/NoData.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize
from rasterio.windows import (
    Window,
    bounds as window_bounds,
    transform as window_transform,
)
from shapely.geometry import box
from shapely.ops import unary_union
from shapely.strtree import STRtree


DEFAULT_LAYER = "rivers"
DEFAULT_WEIGHT_FIELD = "weight"
DEFAULT_INPUT_MIN = 0.05
DEFAULT_INPUT_MAX = 600.0
DEFAULT_MIN_WIDTH_M = 10.0
DEFAULT_MAX_WIDTH_M = 250.0
DEFAULT_BURN_VALUE = 1
DEFAULT_BACKGROUND_VALUE = 0
DEFAULT_NODATA_VALUE = 0
DEFAULT_TILE_SIZE = 2048


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Rasterize weighted river centerlines to the exact grid of a reference raster."
        )
    )
    parser.add_argument("input_gpkg", type=Path, help="Input weighted river GeoPackage")
    parser.add_argument(
        "reference_raster",
        type=Path,
        help="Raster whose CRS, extent, transform, and dimensions define the output",
    )
    parser.add_argument("output_raster", type=Path, help="Output uint8 river-mask GeoTIFF")

    parser.add_argument(
        "--layer",
        default=DEFAULT_LAYER,
        help=f"Input GeoPackage layer (default: {DEFAULT_LAYER})",
    )
    parser.add_argument(
        "--weight_field",
        default=DEFAULT_WEIGHT_FIELD,
        help=f"Numeric weight field (default: {DEFAULT_WEIGHT_FIELD})",
    )
    parser.add_argument(
        "--min_weight",
        type=float,
        default=None,
        help=(
            "Skip river reaches whose weight is below this value. "
            "By default no weight-based filtering is applied."
        ),
    )
    parser.add_argument(
        "--input_min",
        type=float,
        default=DEFAULT_INPUT_MIN,
        help=f"Weight mapped to minimum width (default: {DEFAULT_INPUT_MIN})",
    )
    parser.add_argument(
        "--input_max",
        type=float,
        default=DEFAULT_INPUT_MAX,
        help=f"Weight mapped to maximum width (default: {DEFAULT_INPUT_MAX})",
    )
    parser.add_argument(
        "--min_width_m",
        type=float,
        default=DEFAULT_MIN_WIDTH_M,
        help=f"Minimum river width in metres (default: {DEFAULT_MIN_WIDTH_M})",
    )
    parser.add_argument(
        "--max_width_m",
        type=float,
        default=DEFAULT_MAX_WIDTH_M,
        help=f"Maximum river width in metres (default: {DEFAULT_MAX_WIDTH_M})",
    )
    parser.add_argument(
        "--burn_value",
        type=int,
        default=DEFAULT_BURN_VALUE,
        help=f"Value written for river pixels (default: {DEFAULT_BURN_VALUE})",
    )
    parser.add_argument(
        "--background_value",
        type=int,
        default=DEFAULT_BACKGROUND_VALUE,
        help=f"Value written for unused pixels (default: {DEFAULT_BACKGROUND_VALUE})",
    )
    parser.add_argument(
        "--nodata_value",
        type=int,
        default=DEFAULT_NODATA_VALUE,
        help=(
            "GeoTIFF NoData value. Default is 0, so unused pixels are both zero "
            "and NoData."
        ),
    )
    parser.add_argument(
        "--tile_size",
        type=int,
        default=DEFAULT_TILE_SIZE,
        help=f"Rasterization tile size in pixels (default: {DEFAULT_TILE_SIZE})",
    )
    parser.add_argument(
        "--all_touched",
        action="store_true",
        help="Burn every pixel touched by a buffered river polygon",
    )
    parser.add_argument(
        "--exclude_mask",
        type=Path,
        action="append",
        default=[],
        help=(
            "Polygon vector file whose area is excluded from the river mask. "
            "Repeat --exclude-mask to apply multiple masks. For multi-layer "
            "containers, the first layer is used."
        ),
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    """Validate input paths and numeric parameters."""
    if not args.input_gpkg.is_file():
        raise ValueError(f"Input GeoPackage not found: {args.input_gpkg}")
    if not args.reference_raster.is_file():
        raise ValueError(f"Reference raster not found: {args.reference_raster}")

    for mask_path in args.exclude_mask:
        if not mask_path.is_file():
            raise ValueError(f"Exclude mask not found: {mask_path}")

    if args.output_raster.resolve() == args.reference_raster.resolve():
        raise ValueError("Output raster must not overwrite the reference raster")

    if args.min_weight is not None and not math.isfinite(args.min_weight):
        raise ValueError("--min-weight must be finite")

    if not math.isfinite(args.input_min) or not math.isfinite(args.input_max):
        raise ValueError("--input-min and --input-max must be finite")
    if args.input_max <= args.input_min:
        raise ValueError("--input-max must be greater than --input-min")

    if args.min_width_m <= 0 or args.max_width_m <= 0:
        raise ValueError("--min-width-m and --max-width-m must be greater than zero")
    if args.max_width_m < args.min_width_m:
        raise ValueError("--max-width-m must be >= --min-width-m")

    for name in ("burn_value", "background_value", "nodata_value"):
        value = getattr(args, name)
        if not 0 <= value <= 255:
            option = name.replace("_", "-")
            raise ValueError(f"--{option} must be in the uint8 range 0..255")

    if args.burn_value == args.background_value:
        raise ValueError("--burn-value must differ from --background-value")

    if args.tile_size < 64:
        raise ValueError("--tile-size must be at least 64 pixels")


def ensure_meter_crs(crs) -> None:
    """Require a projected CRS whose horizontal units are metres."""
    if crs is None:
        raise ValueError("Reference raster has no CRS")
    if not crs.is_projected:
        raise ValueError(
            f"Reference raster CRS must be projected for metre widths; got {crs}"
        )

    units = (crs.linear_units or "").lower()
    if units not in {"metre", "meter", "metres", "meters", "m"}:
        raise ValueError(
            f"Reference raster CRS must use metre units; got {units!r} for {crs}"
        )


def scale_width(
    weight: float,
    *,
    input_min: float,
    input_max: float,
    min_width_m: float,
    max_width_m: float,
) -> float:
    """Map one river weight to a clamped geographic width in metres."""
    clamped = min(max(weight, input_min), input_max)
    fraction = (clamped - input_min) / (input_max - input_min)
    return min_width_m + fraction * (max_width_m - min_width_m)


def load_exclusion_geometry(
    mask_paths: list[Path],
    *,
    target_crs,
    target_bounds,
):
    """Load, reproject, clip, and union all exclusion-mask polygons."""
    if not mask_paths:
        return None

    target_extent = box(
        target_bounds.left,
        target_bounds.bottom,
        target_bounds.right,
        target_bounds.top,
    )
    mask_geometries = []

    for mask_path in mask_paths:
        print(f"Reading exclude mask: {mask_path}")
        mask = gpd.read_file(mask_path)

        if mask.empty:
            print("  Mask contains no features; skipping.")
            continue

        if mask.crs is None:
            raise ValueError(f"Exclude mask has no CRS: {mask_path}")

        if mask.crs != target_crs:
            mask = mask.to_crs(target_crs)

        for geometry in mask.geometry:
            if geometry is None or geometry.is_empty:
                continue

            clipped = geometry.intersection(target_extent)
            if not clipped.is_empty:
                mask_geometries.append(clipped)

    if not mask_geometries:
        print("Exclude masks do not intersect the reference extent.")
        return None

    exclusion = unary_union(mask_geometries)
    print(
        f"Loaded {len(mask_geometries):,} exclusion polygon feature(s) "
        f"from {len(mask_paths):,} mask file(s)."
    )
    return exclusion


def read_and_buffer_rivers(
    args: argparse.Namespace,
    *,
    target_crs,
    target_bounds,
    exclusion_geometry,
) -> list:
    """Read reaches near the target extent and return buffered river polygons."""
    margin = args.max_width_m / 2.0
    expanded_bounds = (
        target_bounds.left - margin,
        target_bounds.bottom - margin,
        target_bounds.right + margin,
        target_bounds.top + margin,
    )

    print(f"Reading layer '{args.layer}' from {args.input_gpkg}")
    rivers = gpd.read_file(
        args.input_gpkg,
        layer=args.layer,
        bbox=expanded_bounds,
    )

    if rivers.empty:
        print("No river reaches intersect the reference extent.")
        return []

    if rivers.crs is None:
        raise ValueError(f"Input layer '{args.layer}' has no CRS")

    if rivers.crs != target_crs:
        rivers = rivers.to_crs(target_crs)

    if args.weight_field not in rivers.columns:
        raise ValueError(
            f"Weight field {args.weight_field!r} not found in layer '{args.layer}'"
        )

    numeric_weights = pd.to_numeric(rivers[args.weight_field], errors="coerce")
    invalid = numeric_weights.isna()
    if invalid.any():
        raise ValueError(
            f"{int(invalid.sum())} river feature(s) have missing or non-numeric "
            f"{args.weight_field!r} values"
        )

    if args.min_weight is not None:
        keep = numeric_weights >= args.min_weight
        removed_count = int((~keep).sum())
        rivers = rivers.loc[keep].copy()
        numeric_weights = numeric_weights.loc[keep]

        print(
            f"Minimum weight {args.min_weight:g}: "
            f"kept {len(rivers):,} reach(es), removed {removed_count:,}."
        )

        if rivers.empty:
            print("No river reaches remain after minimum-weight filtering.")
            return []

    widths = numeric_weights.map(
        lambda value: scale_width(
            float(value),
            input_min=args.input_min,
            input_max=args.input_max,
            min_width_m=args.min_width_m,
            max_width_m=args.max_width_m,
        )
    )

    polygons = []
    for geometry, width_m in zip(rivers.geometry, widths):
        if geometry is None or geometry.is_empty:
            continue

        buffered = geometry.buffer(width_m / 2.0)

        # Apply exclusions after buffering so wide rivers and line-end caps
        # cannot bleed back into suppressed areas.
        if exclusion_geometry is not None and not buffered.is_empty:
            buffered = buffered.difference(exclusion_geometry)

        if not buffered.is_empty:
            polygons.append(buffered)

    print(f"Buffered {len(polygons):,} river reach(es).")
    return polygons


def query_tree(tree: STRtree, geometries: list, query_geometry) -> list:
    """Return query hits for either Shapely 1.x or 2.x STRtree behavior."""
    result = tree.query(query_geometry)
    if len(result) == 0:
        return []

    first = result[0]
    if isinstance(first, (int, np.integer)):
        return [geometries[int(index)] for index in result]

    return list(result)


def iter_windows(width: int, height: int, tile_size: int):
    """Yield windows covering the complete output raster."""
    for row_off in range(0, height, tile_size):
        win_height = min(tile_size, height - row_off)
        for col_off in range(0, width, tile_size):
            win_width = min(tile_size, width - col_off)
            yield Window(col_off, row_off, win_width, win_height)


def rasterize_rivers(args: argparse.Namespace) -> None:
    """Create the exact-grid binary river mask."""
    with rasterio.open(args.reference_raster) as reference:
        ensure_meter_crs(reference.crs)

        profile = reference.profile.copy()
        target_crs = reference.crs
        target_bounds = reference.bounds
        width = reference.width
        height = reference.height
        transform = reference.transform

    print("Reference grid")
    print(f"  CRS:          {target_crs}")
    print(f"  Size:         {width:,} x {height:,}")
    print(f"  Pixel size:   {abs(transform.a):g} x {abs(transform.e):g} m")
    print(f"  Weight range: {args.input_min:g} -> {args.input_max:g}")
    print(
        f"  Min weight:   "
        f"{args.min_weight:g}" if args.min_weight is not None else "  Min weight:   none"
    )
    print(f"  Width range:  {args.min_width_m:g} -> {args.max_width_m:g} m")
    print(f"  Exclude masks: {len(args.exclude_mask)}")

    exclusion_geometry = load_exclusion_geometry(
        args.exclude_mask,
        target_crs=target_crs,
        target_bounds=target_bounds,
    )

    polygons = read_and_buffer_rivers(
        args,
        target_crs=target_crs,
        target_bounds=target_bounds,
        exclusion_geometry=exclusion_geometry,
    )

    profile.update(
        driver="GTiff",
        count=1,
        dtype="uint8",
        nodata=args.nodata_value,
        compress="deflate",
        predictor=2,
        tiled=True,
        BIGTIFF="IF_SAFER",
    )
    profile.pop("photometric", None)

    args.output_raster.parent.mkdir(parents=True, exist_ok=True)

    tree = STRtree(polygons) if polygons else None
    total_windows = (
        math.ceil(width / args.tile_size) * math.ceil(height / args.tile_size)
    )
    completed = 0

    with rasterio.open(args.output_raster, "w", **profile) as dst:
        for window in iter_windows(width, height, args.tile_size):
            tile = np.full(
                (int(window.height), int(window.width)),
                args.background_value,
                dtype=np.uint8,
            )

            if tree is not None:
                left, bottom, right, top = window_bounds(window, transform)
                candidates = query_tree(
                    tree,
                    polygons,
                    box(left, bottom, right, top),
                )

                if candidates:
                    tile = rasterize(
                        ((geometry, args.burn_value) for geometry in candidates),
                        out_shape=tile.shape,
                        transform=window_transform(window, transform),
                        fill=args.background_value,
                        dtype="uint8",
                        all_touched=args.all_touched,
                    )

            dst.write(tile, 1, window=window)

            completed += 1
            if completed % 100 == 0 or completed == total_windows:
                print(
                    f"\rRasterizing tiles: {completed:,}/{total_windows:,}",
                    end="",
                    flush=True,
                )

    print()
    print(f"Created: {args.output_raster}")
    print(f"  River value:      {args.burn_value}")
    print(f"  Background value: {args.background_value}")
    print(f"  NoData value:     {args.nodata_value}")


def main() -> int:
    """Run the command."""
    args = parse_args()

    try:
        validate_args(args)
        rasterize_rivers(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
