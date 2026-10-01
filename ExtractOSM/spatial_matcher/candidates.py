"""PostGIS candidate generation, plausibility gating, and scoring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import MatchingConfig
from .postgis import GEOGRAPHY_COLUMN, ROW_ID_COLUMN, SpatialIndexTables

DEFAULT_CANDIDATE_TABLE = "spatial_match_candidates"
REJECTION_MATCH_FIELD_DELTA = "MATCH_FIELD_DELTA"


@dataclass(frozen=True, slots=True)
class CandidateTable:
    """Metadata for a generated and scored candidate table."""

    schema: str
    table: str
    rows: int
    plausible_rows: int
    rejected_rows: int
    scored_rows: int


class PostGISCandidateBuilder:
    """Generate forward candidates, apply gates, and score plausible pairs."""

    def __init__(self, connection: Any, *, candidate_table: str = DEFAULT_CANDIDATE_TABLE) -> None:
        self._connection = connection
        self._candidate_table = candidate_table

    def build(self, *, tables: SpatialIndexTables, config: MatchingConfig) -> CandidateTable:
        """Generate, gate, and score external-to-OSM candidate pairs."""
        self._validate_match_fields(config)
        try:
            self._generate_candidates(tables=tables, config=config)
            self._apply_plausibility_gates(tables=tables, config=config)
            self._score_candidates(tables=tables, config=config)
            counts = self._candidate_counts(tables.schema)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return CandidateTable(schema=tables.schema, table=self._candidate_table, rows=counts[0],
            plausible_rows=counts[1], rejected_rows=counts[2], scored_rows=counts[3], )

    @staticmethod
    def _validate_match_fields(config: MatchingConfig) -> None:
        max_delta = config.matching.gates.max_match_field_delta
        if max_delta is None:
            return
        if config.osm.match_field is None or config.external.match_field is None:
            raise ValueError("matching.gates.max_match_field_delta requires match_field "
                             "for both osm and external sources")

    def _generate_candidates(self, *, tables: SpatialIndexTables, config: MatchingConfig) -> None:
        schema = _quote_identifier(tables.schema)
        osm_table = f"{schema}.{_quote_identifier(tables.osm_table)}"
        external_table = f"{schema}.{_quote_identifier(tables.external_table)}"
        candidate_table = f"{schema}.{_quote_identifier(self._candidate_table)}"
        geog = _quote_identifier(GEOGRAPHY_COLUMN)
        row_id = _quote_identifier(ROW_ID_COLUMN)
        cursor = self._connection.cursor()
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {candidate_table} CASCADE")
            cursor.execute(f"""
                    CREATE UNLOGGED TABLE {candidate_table} AS
                    SELECT
                        e.{row_id} AS external_row_id,
                        c.osm_row_id,
                        c.spatial_rank,
                        c.distance_m,
                        TRUE::boolean AS plausible,
                        NULL::text AS rejection_reason,
                        NULL::double precision AS match_field_delta,
                        NULL::double precision AS score
                    FROM {external_table} AS e
                    CROSS JOIN LATERAL (
                        SELECT
                            o.{row_id} AS osm_row_id,
                            ROW_NUMBER() OVER (
                                ORDER BY ST_Distance(o.{geog}, e.{geog})
                            )::integer AS spatial_rank,
                            ST_Distance(
                                o.{geog},
                                e.{geog}
                            )::double precision AS distance_m
                        FROM {osm_table} AS o
                        WHERE ST_DWithin(o.{geog}, e.{geog}, %s)
                        ORDER BY ST_Distance(o.{geog}, e.{geog})
                        LIMIT %s
                    ) AS c
                """, (config.matching.max_radius_m, config.matching.candidate_limit,), )
            cursor.execute(f"CREATE INDEX "
                           f"{_quote_identifier(self._candidate_table + '_external_idx')} "
                           f"ON {candidate_table} (external_row_id)")
            cursor.execute(f"CREATE INDEX "
                           f"{_quote_identifier(self._candidate_table + '_osm_idx')} "
                           f"ON {candidate_table} (osm_row_id)")
            cursor.execute(f"ANALYZE {candidate_table}")
        finally:
            cursor.close()

    def _apply_plausibility_gates(
            self, *, tables: SpatialIndexTables, config: MatchingConfig
            ) -> None:
        max_delta = config.matching.gates.max_match_field_delta
        if max_delta is None:
            return
        schema = _quote_identifier(tables.schema)
        osm_table = f"{schema}.{_quote_identifier(tables.osm_table)}"
        external_table = f"{schema}.{_quote_identifier(tables.external_table)}"
        candidate_table = f"{schema}.{_quote_identifier(self._candidate_table)}"
        row_id = _quote_identifier(ROW_ID_COLUMN)
        osm_field = _quote_identifier(config.osm.match_field)
        external_field = _quote_identifier(config.external.match_field)
        cursor = self._connection.cursor()
        try:
            cursor.execute(f"""
                    UPDATE {candidate_table} AS c
                    SET match_field_delta = ABS(
                        o.{osm_field}::double precision
                        - e.{external_field}::double precision
                    )
                    FROM {osm_table} AS o, {external_table} AS e
                    WHERE c.osm_row_id = o.{row_id}
                      AND c.external_row_id = e.{row_id}
                      AND o.{osm_field} IS NOT NULL
                      AND e.{external_field} IS NOT NULL
                """)
            cursor.execute(f"""
                    UPDATE {candidate_table}
                    SET plausible = FALSE,
                        rejection_reason = %s
                    WHERE match_field_delta > %s
                """, (REJECTION_MATCH_FIELD_DELTA, max_delta), )
            cursor.execute(f"ANALYZE {candidate_table}")
        finally:
            cursor.close()

    def _score_candidates(self, *, tables: SpatialIndexTables, config: MatchingConfig) -> None:
        """Assign a tunable penalty score to every plausible candidate."""
        table = f"{_quote_identifier(tables.schema)}.{_quote_identifier(self._candidate_table)}"
        scoring = config.matching.scoring
        cursor = self._connection.cursor()
        try:
            cursor.execute(f"""
                    UPDATE {table}
                    SET score = distance_m * %s
                        + COALESCE(match_field_delta * %s, 0.0)
                    WHERE plausible
                """, (scoring.distance_weight, scoring.match_field_weight), )
            cursor.execute(f"ANALYZE {table}")
        finally:
            cursor.close()

    def _candidate_counts(self, schema_name: str) -> tuple[int, int, int, int]:
        table = f"{_quote_identifier(schema_name)}.{_quote_identifier(self._candidate_table)}"
        cursor = self._connection.cursor()
        try:
            cursor.execute(f"""
                SELECT COUNT(*)::bigint,
                       COUNT(*) FILTER (WHERE plausible)::bigint,
                       COUNT(*) FILTER (WHERE NOT plausible)::bigint,
                       COUNT(*) FILTER (WHERE score IS NOT NULL)::bigint
                FROM {table}
            """)
            row = cursor.fetchone()
        finally:
            cursor.close()
        if row is None:
            return 0, 0, 0, 0
        return tuple(int(value) for value in row)


def _quote_identifier(value: object) -> str:
    """Quote one PostgreSQL identifier."""
    return f'"{str(value).replace(chr(34), chr(34) * 2)}"'
