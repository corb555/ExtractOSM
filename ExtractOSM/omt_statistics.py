#!/usr/bin/env python3

import argparse
import math
import random
from statistics import mean

import psycopg

SOURCES = ["omt_peak", "omt_geological", "omt_ocean", "omt_fault", "omt_paleo", "omt_lake_lz",
    "omt_river_lz", "omt_place_lz", "omt_viewpoint", "omt_geyser", ]


def lon_to_tile_x(lon: float, z: int) -> int:
    n = 2 ** z
    return int((lon + 180.0) / 360.0 * n)


def lat_to_tile_y(lat: float, z: int) -> int:
    lat_rad = math.radians(lat)
    n = 2 ** z
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return int(y)


def tile_bounds_for_bbox(bbox, z):
    west, south, east, north = bbox

    min_x = lon_to_tile_x(west, z)
    max_x = lon_to_tile_x(east, z)

    # Web Mercator Y increases southward.
    min_y = lat_to_tile_y(north, z)
    max_y = lat_to_tile_y(south, z)

    return min_x, min_y, max_x, max_y


def sample_tiles(bbox, z, count):
    min_x, min_y, max_x, max_y = tile_bounds_for_bbox(bbox, z)

    tiles = set()

    max_possible = (max_x - min_x + 1) * (max_y - min_y + 1)
    count = min(count, max_possible)

    while len(tiles) < count:
        x = random.randint(min_x, max_x)
        y = random.randint(min_y, max_y)
        tiles.add((x, y))

    return list(tiles)


def measure_source(conn, schema, source, z, tiles):
    sizes = []

    sql = f"""
        SELECT octet_length({schema}.{source}(%s, %s, %s))
    """

    with conn.cursor() as cur:
        for x, y in tiles:
            cur.execute(sql, (z, x, y))
            size = cur.fetchone()[0]

            sizes.append(size or 0)

    populated = [size for size in sizes if size > 0]

    return {
        "source": source, "samples": len(sizes), "nonempty": len(populated),
        "avg_bytes": mean(sizes), "avg_nonempty_bytes": mean(populated) if populated else 0,
        "max_bytes": max(sizes, default=0), "total_sample_bytes": sum(sizes),
    }


def format_bytes(value):
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.2f} MB"
    if value >= 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value:.0f} B"


def main():
    parser = argparse.ArgumentParser(
        description="Sample Martin/PostGIS MVT source sizes within a bbox.")

    parser.add_argument("--db", default="postgresql://localhost:5432/gis",
        help="PostgreSQL connection string", )

    parser.add_argument("--bbox", required=True, help="west,south,east,north", )

    parser.add_argument("--zoom", type=int, default=11, )

    parser.add_argument("--samples", type=int, default=250, )

    parser.add_argument("--schema", default="mvt", )

    parser.add_argument("--seed", type=int, default=12345, )

    args = parser.parse_args()

    bbox = tuple(float(v) for v in args.bbox.split(","))

    if len(bbox) != 4:
        raise ValueError("bbox must be west,south,east,north")

    random.seed(args.seed)

    tiles = sample_tiles(bbox=bbox, z=args.zoom, count=args.samples, )

    print(f"Sampling {len(tiles)} tiles at z{args.zoom} "
          f"inside bbox {args.bbox}")
    print()

    with psycopg.connect(args.db) as conn:
        results = [
            measure_source(conn=conn, schema=args.schema, source=source, z=args.zoom, tiles=tiles, )
            for source in SOURCES]

    results.sort(key=lambda r: r["avg_bytes"], reverse=True, )

    print(f"{'Source':22} "
          f"{'Nonempty':>10} "
          f"{'Avg/tile':>12} "
          f"{'Avg nonempty':>14} "
          f"{'Max':>12}")

    print("-" * 76)

    for result in results:
        print(f"{result['source']:22} "
              f"{result['nonempty']:>5}/{result['samples']:<4} "
              f"{format_bytes(result['avg_bytes']):>12} "
              f"{format_bytes(result['avg_nonempty_bytes']):>14} "
              f"{format_bytes(result['max_bytes']):>12}")


if __name__ == "__main__":
    main()
