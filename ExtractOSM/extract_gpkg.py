#!/usr/bin/env python3
"""Extract GeoPackage layers to the extent and CRS of a reference raster."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import rasterio

DEFAULT_LCO = "SPATIAL_INDEX=YES"


@dataclass(frozen=True)
class LayerSpec:
    source: str
    destination: str


def parse_layer_spec(value: str) -> LayerSpec:
    if not value:
        raise argparse.ArgumentTypeError("--layer must not be empty")

    if ":" in value:
        source, destination = value.split(":", 1)
    else:
        source = destination = value

    source = source.strip()
    destination = destination.strip()

    if not source:
        raise argparse.ArgumentTypeError("source layer name must not be empty")
    if not destination:
        raise argparse.ArgumentTypeError("destination layer name must not be empty")

    return LayerSpec(source, destination)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract GeoPackage layers to a reference raster extent and CRS."
    )
    parser.add_argument("input_gpkg", type=Path)
    parser.add_argument("output_gpkg", type=Path)
    parser.add_argument("reference_raster", type=Path)
    parser.add_argument(
        "--layer",
        action="append",
        required=True,
        type=parse_layer_spec,
        metavar="SOURCE[:DEST]",
        help="Repeat for multiple layers.",
    )
    parser.add_argument(
        "--where",
        help="Optional OGR attribute filter applied to all selected layers.",
    )
    parser.add_argument(
        "--lco",
        action="append",
        metavar="NAME=VALUE",
        help=f"Repeatable layer creation option. Default: {DEFAULT_LCO}",
    )
    return parser


def require_program(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"Required executable not found on PATH: {name}")
    return path


def validate_args(args: argparse.Namespace) -> None:
    if not args.input_gpkg.is_file():
        raise ValueError(f"Input GeoPackage does not exist: {args.input_gpkg}")
    if not args.reference_raster.is_file():
        raise ValueError(f"Reference raster does not exist: {args.reference_raster}")
    if args.input_gpkg.resolve() == args.output_gpkg.resolve():
        raise ValueError("Input and output GeoPackage must be different files")
    if args.where is not None and not args.where.strip():
        raise ValueError("--where must not be empty")
    if not args.output_gpkg.parent.exists():
        raise ValueError(f"Output directory does not exist: {args.output_gpkg.parent}")

    destinations = [x.destination for x in args.layer]
    duplicates = sorted({x for x in destinations if destinations.count(x) > 1})
    if duplicates:
        raise ValueError("Duplicate output layer name(s): " + ", ".join(duplicates))


def validate_layers(ogrinfo: str, input_gpkg: Path, layers: list[LayerSpec]) -> None:
    for spec in layers:
        result = subprocess.run(
            [ogrinfo, "-ro", "-so", "-q", str(input_gpkg), spec.source],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or "ogrinfo failed"
            raise RuntimeError(
                f"Source layer {spec.source!r} is unavailable in {input_gpkg}: {detail}"
            )


def read_reference(reference_raster: Path):
    with rasterio.open(reference_raster) as ds:
        if ds.crs is None:
            raise ValueError(f"Reference raster has no CRS: {reference_raster}")
        b = ds.bounds
        return (b.left, b.bottom, b.right, b.top), ds.crs.to_string()


def extract_layer(
    ogr2ogr: str,
    *,
    input_gpkg: Path,
    output_gpkg: Path,
    layer: LayerSpec,
    bounds,
    crs: str,
    where: str | None,
    lcos: list[str],
    update: bool,
) -> None:
    xmin, ymin, xmax, ymax = bounds

    cmd = [
        ogr2ogr,
        "-f", "GPKG",
        str(output_gpkg),
        str(input_gpkg),
        layer.source,
        "-spat",
        f"{xmin:.15g}", f"{ymin:.15g}", f"{xmax:.15g}", f"{ymax:.15g}",
        "-spat_srs", crs,
        "-t_srs", crs,
        "-nln", layer.destination,
        "-overwrite",
    ]

    if update:
        cmd.append("-update")
    if where is not None:
        cmd.extend(["-where", where])
    for lco in lcos:
        cmd.extend(["-lco", lco])

    print(f"Extracting {layer.source!r} -> {layer.destination!r}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise RuntimeError(
            f"ogr2ogr failed for layer {layer.source!r} "
            f"with exit code {result.returncode}"
        )


def main() -> int:
    args = build_parser().parse_args()

    try:
        validate_args(args)
        ogr2ogr = require_program("ogr2ogr")
        ogrinfo = require_program("ogrinfo")
        validate_layers(ogrinfo, args.input_gpkg, args.layer)

        bounds, crs = read_reference(args.reference_raster)
        lcos = args.lco or [DEFAULT_LCO]

        print(f"Reference raster: {args.reference_raster}")
        print(f"Reference extent: {', '.join(f'{x:.15g}' for x in bounds)}")
        print(f"Reference CRS: {crs}")
        if args.where is not None:
            print(f"Where: {args.where}")
        print("Layer creation options: " + ", ".join(lcos))

        # Recreate the data source so stale layers from older runs cannot survive.
        if args.output_gpkg.exists():
            args.output_gpkg.unlink()

        for i, layer in enumerate(args.layer):
            extract_layer(
                ogr2ogr,
                input_gpkg=args.input_gpkg,
                output_gpkg=args.output_gpkg,
                layer=layer,
                bounds=bounds,
                crs=crs,
                where=args.where,
                lcos=lcos,
                update=i > 0,
            )

        print(f"Created {args.output_gpkg} with {len(args.layer)} layer(s).")
        return 0

    except (ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
