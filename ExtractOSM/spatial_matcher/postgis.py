"""PostGIS staging-table and spatial-index construction."""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from typing import Any

import pandas as pd

DEFAULT_SCHEMA = "public"
DEFAULT_OSM_TABLE = "spatial_match_osm"
DEFAULT_EXTERNAL_TABLE = "spatial_match_external"
GEOMETRY_COLUMN = "match_geom"
GEOGRAPHY_COLUMN = "match_geog"
ROW_ID_COLUMN = "match_row_id"


@dataclass(frozen=True, slots=True)
class SpatialIndexTables:
    """PostGIS staging tables created for spatial matching.

    Attributes:
        schema: PostgreSQL schema containing both staging tables.
        osm_table: OSM staging table name.
        external_table: External-source staging table name.
        osm_rows: Number of OSM rows loaded.
        external_rows: Number of external rows loaded.
    """

    schema: str
    osm_table: str
    external_table: str
    osm_rows: int
    external_rows: int


class PostGISSpatialIndexBuilder:
    """Load prepared points into disposable PostGIS staging tables.

    The builder accepts an open PostgreSQL DB-API connection rather than owning
    connection configuration. It supports psycopg 3 ``cursor.copy()`` and
    psycopg2 ``cursor.copy_expert()`` bulk loading.
    """

    def __init__(
            self, connection: Any, *, schema: str = DEFAULT_SCHEMA,
            osm_table: str = DEFAULT_OSM_TABLE,
            external_table: str = DEFAULT_EXTERNAL_TABLE, ) -> None:
        self._connection = connection
        self._schema = schema
        self._osm_table = osm_table
        self._external_table = external_table

    def build(
            self, *, osm: pd.DataFrame, external: pd.DataFrame, epsg: int, ) -> SpatialIndexTables:
        """Create staging tables and GiST spatial indexes for both sources.

        Existing staging tables with the configured names are replaced. The
        source data must already contain projected ``match_x`` and ``match_y``
        columns in ``epsg``.

        Args:
            osm: Prepared OSM dataframe.
            external: Prepared external dataframe.
            epsg: EPSG code represented by ``match_x`` and ``match_y``.

        Returns:
            Metadata describing the created staging tables.
        """
        self._validate_spatial_columns(osm, "OSM")
        self._validate_spatial_columns(external, "external")

        try:
            self._create_indexed_table(self._osm_table, osm, epsg)
            self._create_indexed_table(self._external_table, external, epsg)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

        return SpatialIndexTables(schema=self._schema, osm_table=self._osm_table,
            external_table=self._external_table, osm_rows=len(osm), external_rows=len(external), )

    @staticmethod
    def _validate_spatial_columns(frame: pd.DataFrame, label: str) -> None:
        """Validate projected coordinates required to build point geometry."""
        missing = {"match_x", "match_y"} - set(frame.columns)
        if missing:
            raise ValueError(f"{label} data is missing spatial columns: "
                             f"{', '.join(sorted(missing))}")

        invalid = frame["match_x"].isna() | frame["match_y"].isna()
        if invalid.any():
            raise ValueError(f"{label} data contains {int(invalid.sum())} rows with "
                             "missing projected coordinates")

    def _create_indexed_table(
            self, table_name: str, frame: pd.DataFrame, epsg: int, ) -> None:
        qualified_table = _qualified_identifier(self._schema, table_name)
        geometry = _quote_identifier(GEOMETRY_COLUMN)
        geography = _quote_identifier(GEOGRAPHY_COLUMN)
        geometry_index = _quote_identifier(f"{table_name}_{GEOMETRY_COLUMN}_gist")
        geography_index = _quote_identifier(f"{table_name}_{GEOGRAPHY_COLUMN}_gist")

        columns_sql = ", ".join(
            f"{_quote_identifier(column)} {self._postgres_type(frame[column])}" for column in
            frame.columns)
        row_id = _quote_identifier(ROW_ID_COLUMN)

        cursor = self._connection.cursor()
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {qualified_table} CASCADE")
            cursor.execute(f"CREATE UNLOGGED TABLE {qualified_table} ("
                           f"{row_id} BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
                           f"{columns_sql})")
            self._copy_dataframe(cursor, qualified_table, frame)

            cursor.execute(f"ALTER TABLE {qualified_table} "
                           f"ADD COLUMN {geometry} geometry(Point, {int(epsg)})")
            cursor.execute(f"UPDATE {qualified_table} SET {geometry} = "
                           f"ST_SetSRID(ST_MakePoint(match_x, match_y), {int(epsg)})")
            cursor.execute(f"ALTER TABLE {qualified_table} "
                           f"ADD COLUMN {geography} geography(Point, 4326)")
            cursor.execute(f"UPDATE {qualified_table} SET {geography} = "
                           f"ST_Transform({geometry}, 4326)::geography")
            cursor.execute(f"CREATE INDEX {geometry_index} ON {qualified_table} "
                           f"USING GIST ({geometry})")
            cursor.execute(f"CREATE INDEX {geography_index} ON {qualified_table} "
                           f"USING GIST ({geography})")
            cursor.execute(f"ANALYZE {qualified_table}")
        finally:
            cursor.close()

    @staticmethod
    def _copy_dataframe(cursor: Any, table: str, frame: pd.DataFrame) -> None:
        """Bulk-copy a dataframe using psycopg 3 or psycopg2 cursor APIs."""
        columns = ", ".join(_quote_identifier(column) for column in frame.columns)
        copy_sql = (f"COPY {table} ({columns}) FROM STDIN "
                    "WITH (FORMAT CSV, HEADER TRUE, NULL '')")

        buffer = StringIO()
        frame.to_csv(buffer, index=False, na_rep="")
        buffer.seek(0)

        if hasattr(cursor, "copy"):
            with cursor.copy(copy_sql) as copy:
                while chunk := buffer.read(1024 * 1024):
                    copy.write(chunk)
            return

        if hasattr(cursor, "copy_expert"):
            cursor.copy_expert(copy_sql, buffer)
            return

        raise TypeError("PostGIS staging requires a psycopg 3 or psycopg2 cursor "
                        "with COPY support")

    @staticmethod
    def _postgres_type(series: pd.Series) -> str:
        """Map common pandas dtypes to staging-table PostgreSQL types."""
        dtype = series.dtype
        if pd.api.types.is_bool_dtype(dtype):
            return "BOOLEAN"
        if pd.api.types.is_integer_dtype(dtype):
            return "BIGINT"
        if pd.api.types.is_float_dtype(dtype):
            return "DOUBLE PRECISION"
        if pd.api.types.is_datetime64_any_dtype(dtype):
            return "TIMESTAMPTZ"
        return "TEXT"


def _quote_identifier(value: object) -> str:
    """Quote one PostgreSQL identifier."""
    return f'"{str(value).replace(chr(34), chr(34) * 2)}"'


def _qualified_identifier(schema: str, table: str) -> str:
    """Quote a schema-qualified PostgreSQL table name."""
    return f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
