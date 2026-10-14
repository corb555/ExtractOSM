#!/usr/bin/env python3
"""Calculate a named derived column in a GeoPackage layer.

Example:
    gpkg-calc input.gpkg output.gpkg \
        --layer rivers \
        --column weight \
        --calc catchment_strahler

The input GeoPackage is copied to the output first, then only the requested
layer is modified. Other layers, indexes, metadata, and attributes are preserved.

GeoPackage writes are performed through GDAL/OGR rather than Python's raw
``sqlite3`` connection. A spatially indexed GeoPackage may contain triggers
that call GeoPackage spatial functions such as ``ST_IsEmpty``; those functions
are registered by the GDAL GeoPackage driver but are not available in a plain
Python SQLite connection.

Calculators:
- catchment_strahler - Cartographic river weight from catchment area and Strahler order
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Calculator:
    """Definition of one named calculated-column operation."""

    name: str
    required_columns: tuple[str, ...]
    output_sql_type: str
    description: str
    sql_expression: str


CALCULATORS: dict[str, Calculator] = {
    "catchment_strahler": Calculator(
        name="catchment_strahler",
        required_columns=("UPLAND_SKM", "ORD_STRAH"),
        output_sql_type="REAL",
        description="Cartographic river weight from catchment area and Strahler order.",
        sql_expression="""
            MAX(
                POWER("UPLAND_SKM", 0.4),
                CASE
                    WHEN "ORD_STRAH" >= 8 THEN 40.0
                    WHEN "ORD_STRAH" = 7 THEN 25.0
                    WHEN "ORD_STRAH" = 6 THEN 15.0
                    WHEN "ORD_STRAH" = 5 THEN 8.0
                    WHEN "ORD_STRAH" = 4 THEN 4.0
                    ELSE 1.0
                END
            )
        """,
    ),
}


def quote_identifier(name: str) -> str:
    """Quote one SQLite identifier safely."""
    if not name:
        raise ValueError("Identifier must not be empty")
    return '"' + name.replace('"', '""') + '"'


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Calculate a named derived column in a GeoPackage layer."
    )
    parser.add_argument("input_gpkg", type=Path, help="Input GeoPackage")
    parser.add_argument("output_gpkg", type=Path, help="Output GeoPackage")
    parser.add_argument("--layer", required=True, help="GeoPackage layer/table to update")
    parser.add_argument("--column", required=True, help="Destination column")
    parser.add_argument(
        "--calc",
        required=True,
        choices=sorted(CALCULATORS),
        help="Named calculation to apply",
    )
    return parser.parse_args()


def validate_paths(input_gpkg: Path, output_gpkg: Path) -> None:
    """Validate source and destination paths."""
    if not input_gpkg.is_file():
        raise ValueError(f"Input GeoPackage not found: {input_gpkg}")
    if input_gpkg.resolve() == output_gpkg.resolve():
        raise ValueError("Input and output GeoPackage must be different files")
    if not output_gpkg.parent.exists():
        raise ValueError(f"Output directory does not exist: {output_gpkg.parent}")


def require_program(name: str) -> str:
    """Return an executable path or fail with a clear message."""
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"Required executable not found on PATH: {name}")
    return executable


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    """Return whether an ordinary SQLite table exists."""
    return connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = ?
        LIMIT 1
        """,
        (table,),
    ).fetchone() is not None


def column_info(connection: sqlite3.Connection, table: str) -> dict[str, str]:
    """Return column-name to declared-type mapping for a table."""
    rows = connection.execute(
        f"PRAGMA table_info({quote_identifier(table)})"
    ).fetchall()
    return {str(row[1]): str(row[2] or "") for row in rows}


def inspect_layer(
    gpkg: Path,
    *,
    layer: str,
    calculator: Calculator,
    destination_column: str,
) -> dict[str, str]:
    """Validate the target layer and return its column definitions.

    Plain sqlite3 is intentionally used only for read-only schema inspection.
    All GeoPackage writes are delegated to GDAL/OGR.
    """
    connection = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
    try:
        if not table_exists(connection, layer):
            raise ValueError(f"Layer/table not found: {layer!r}")

        columns = column_info(connection, layer)

        missing = [
            required
            for required in calculator.required_columns
            if required not in columns
        ]
        if missing:
            raise ValueError(
                f"Calculator {calculator.name!r} requires missing column(s) "
                f"in layer {layer!r}: {', '.join(missing)}"
            )

        if destination_column in columns:
            declared_type = columns[destination_column].upper()
            expected_type = calculator.output_sql_type.upper()
            if declared_type and declared_type != expected_type:
                raise ValueError(
                    f"Existing destination column {destination_column!r} has type "
                    f"{declared_type!r}; calculator requires {expected_type!r}"
                )

        return columns
    finally:
        connection.close()


def run_gdal_sql(ogrinfo: str, gpkg: Path, sql: str) -> None:
    """Execute one modifying SQLite statement through the GDAL GeoPackage driver."""
    command = [
        ogrinfo,
        "-update",
        "-dialect",
        "SQLite",
        "-sql",
        sql,
        str(gpkg),
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "ogrinfo failed"
        raise RuntimeError(f"GDAL SQL failed: {detail}")


def calculate_column(
    ogrinfo: str,
    output_gpkg: Path,
    *,
    layer: str,
    column: str,
    calculator: Calculator,
) -> None:
    """Create or replace one calculated column through GDAL/OGR."""
    columns = inspect_layer(
        output_gpkg,
        layer=layer,
        calculator=calculator,
        destination_column=column,
    )

    table_sql = quote_identifier(layer)
    column_sql = quote_identifier(column)

    if column not in columns:
        run_gdal_sql(
            ogrinfo,
            output_gpkg,
            (
                f"ALTER TABLE {table_sql} "
                f"ADD COLUMN {column_sql} {calculator.output_sql_type}"
            ),
        )

    expression = " ".join(calculator.sql_expression.split())
    run_gdal_sql(
        ogrinfo,
        output_gpkg,
        f"UPDATE {table_sql} SET {column_sql} = {expression}",
    )

    # Read-only validation/statistics are safe through Python sqlite3.
    connection = sqlite3.connect(f"file:{output_gpkg}?mode=ro", uri=True)
    try:
        null_count = connection.execute(
            f"SELECT COUNT(*) FROM {table_sql} WHERE {column_sql} IS NULL"
        ).fetchone()[0]
        if null_count:
            raise ValueError(
                f"Calculation produced NULL for {null_count:,} row(s) "
                f"in {layer!r}.{column!r}"
            )

        row_count = connection.execute(
            f"SELECT COUNT(*) FROM {table_sql}"
        ).fetchone()[0]
        minimum, maximum = connection.execute(
            f"SELECT MIN({column_sql}), MAX({column_sql}) FROM {table_sql}"
        ).fetchone()
    finally:
        connection.close()

    print(f"Calculated: {layer}.{column}")
    print(f"  Calculator: {calculator.name}")
    print(f"  Rows:       {row_count:,}")
    if minimum is not None and maximum is not None:
        print(f"  Range:      {minimum:g} -> {maximum:g}")


def main() -> int:
    """Run the command."""
    args = parse_args()

    try:
        validate_paths(args.input_gpkg, args.output_gpkg)
        ogrinfo = require_program("ogrinfo")
        calculator = CALCULATORS[args.calc]

        print(f"Input:      {args.input_gpkg}")
        print(f"Output:     {args.output_gpkg}")
        print(f"Layer:      {args.layer}")
        print(f"Column:     {args.column}")
        print(f"Calculator: {calculator.name}")

        # Validate the source before creating/replacing the output.
        inspect_layer(
            args.input_gpkg,
            layer=args.layer,
            calculator=calculator,
            destination_column=args.column,
        )

        shutil.copy2(args.input_gpkg, args.output_gpkg)

        try:
            calculate_column(
                ogrinfo,
                args.output_gpkg,
                layer=args.layer,
                column=args.column,
                calculator=calculator,
            )
        except Exception:
            try:
                args.output_gpkg.unlink()
            except OSError:
                pass
            raise

        print(f"Created: {args.output_gpkg}")
        return 0

    except (ValueError, RuntimeError, sqlite3.Error, OSError, OverflowError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
