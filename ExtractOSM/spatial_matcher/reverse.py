"""Reverse OSM-to-external candidate selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .candidates import REJECTION_MATCH_FIELD_DELTA, _quote_identifier
from .models import MatchingConfig
from .postgis import GEOGRAPHY_COLUMN, ROW_ID_COLUMN, SpatialIndexTables

DEFAULT_REVERSE_CANDIDATE_TABLE = "spatial_match_reverse_candidates"
DEFAULT_REVERSE_MATCH_TABLE = "spatial_match_reverse_best"


@dataclass(frozen=True, slots=True)
class ReverseMatchTable:
    """Metadata for reverse OSM-to-external matching."""

    schema: str
    candidate_table: str
    best_table: str
    candidate_rows: int
    plausible_rows: int
    matched_osm_rows: int


class PostGISReverseMatchBuilder:
    """Run candidate selection in reverse and retain each OSM point's best pair."""

    def __init__(
            self, connection: Any, *, candidate_table: str = DEFAULT_REVERSE_CANDIDATE_TABLE,
            best_table: str = DEFAULT_REVERSE_MATCH_TABLE, ) -> None:
        self._connection = connection
        self._candidate_table = candidate_table
        self._best_table = best_table

    def build(
            self, *, tables: SpatialIndexTables, config: MatchingConfig, ) -> ReverseMatchTable:
        """Generate reverse candidates, gate/score them, and choose one per OSM."""
        try:
            self._generate_candidates(tables=tables, config=config)
            self._apply_match_field_gate(tables=tables, config=config)
            self._score_candidates(tables=tables, config=config)
            self._select_best(tables=tables)
            counts = self._counts(tables.schema)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

        return ReverseMatchTable(schema=tables.schema, candidate_table=self._candidate_table,
            best_table=self._best_table, candidate_rows=counts[0], plausible_rows=counts[1],
            matched_osm_rows=counts[2], )

    def _generate_candidates(
            self, *, tables: SpatialIndexTables, config: MatchingConfig, ) -> None:
        """Store nearest external points for every OSM point."""
        schema = _quote_identifier(tables.schema)
        osm_table = f"{schema}.{_quote_identifier(tables.osm_table)}"
        external_table = f"{schema}.{_quote_identifier(tables.external_table)}"
        table = f"{schema}.{_quote_identifier(self._candidate_table)}"
        geog = _quote_identifier(GEOGRAPHY_COLUMN)
        row_id = _quote_identifier(ROW_ID_COLUMN)

        cursor = self._connection.cursor()
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
            cursor.execute(f"""
                    CREATE UNLOGGED TABLE {table} AS
                    SELECT
                        o.{row_id} AS osm_row_id,
                        c.external_row_id,
                        c.spatial_rank,
                        c.distance_m,
                        TRUE::boolean AS plausible,
                        NULL::text AS rejection_reason,
                        NULL::double precision AS match_field_delta,
                        NULL::double precision AS score
                    FROM {osm_table} AS o
                    CROSS JOIN LATERAL (
                        SELECT
                            e.{row_id} AS external_row_id,
                            ROW_NUMBER() OVER (
                                ORDER BY ST_Distance(e.{geog}, o.{geog})
                            )::integer AS spatial_rank,
                            ST_Distance(
                                e.{geog},
                                o.{geog}
                            )::double precision AS distance_m
                        FROM {external_table} AS e
                        WHERE ST_DWithin(e.{geog}, o.{geog}, %s)
                        ORDER BY ST_Distance(e.{geog}, o.{geog})
                        LIMIT %s
                    ) AS c
                """, (config.matching.max_radius_m, config.matching.candidate_limit,), )
            cursor.execute(f"CREATE INDEX "
                           f"{_quote_identifier(self._candidate_table + '_osm_idx')} "
                           f"ON {table} (osm_row_id)")
            cursor.execute(f"CREATE INDEX "
                           f"{_quote_identifier(self._candidate_table + '_external_idx')} "
                           f"ON {table} (external_row_id)")
            cursor.execute(f"ANALYZE {table}")
        finally:
            cursor.close()

    def _apply_match_field_gate(
            self, *, tables: SpatialIndexTables, config: MatchingConfig, ) -> None:
        """Apply the same optional numeric plausibility gate as forward matching."""
        max_delta = config.matching.gates.max_match_field_delta
        if max_delta is None:
            return
        if config.osm.match_field is None or config.external.match_field is None:
            raise ValueError("matching.gates.max_match_field_delta requires match_field "
                             "for both osm and external sources")

        schema = _quote_identifier(tables.schema)
        osm_table = f"{schema}.{_quote_identifier(tables.osm_table)}"
        external_table = f"{schema}.{_quote_identifier(tables.external_table)}"
        table = f"{schema}.{_quote_identifier(self._candidate_table)}"
        row_id = _quote_identifier(ROW_ID_COLUMN)
        osm_field = _quote_identifier(config.osm.match_field)
        external_field = _quote_identifier(config.external.match_field)

        cursor = self._connection.cursor()
        try:
            cursor.execute(f"""
                    UPDATE {table} AS c
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
                    UPDATE {table}
                    SET plausible = FALSE,
                        rejection_reason = %s
                    WHERE match_field_delta > %s
                """, (REJECTION_MATCH_FIELD_DELTA, max_delta), )
            cursor.execute(f"ANALYZE {table}")
        finally:
            cursor.close()

    def _score_candidates(
            self, *, tables: SpatialIndexTables, config: MatchingConfig, ) -> None:
        """Score plausible reverse candidates with the forward scoring formula."""
        table = (f"{_quote_identifier(tables.schema)}."
                 f"{_quote_identifier(self._candidate_table)}")
        scoring = config.matching.scoring

        cursor = self._connection.cursor()
        try:
            cursor.execute(f"""
                    UPDATE {table}
                    SET score = distance_m * %s
                        + COALESCE(match_field_delta * %s, 0.0)
                    WHERE plausible
                """, (scoring.distance_weight, scoring.match_field_weight,), )
            cursor.execute(f"ANALYZE {table}")
        finally:
            cursor.close()

    def _select_best(self, *, tables: SpatialIndexTables) -> None:
        """Store the lowest-score plausible external candidate for each OSM row."""
        schema = _quote_identifier(tables.schema)
        candidates = f"{schema}.{_quote_identifier(self._candidate_table)}"
        best = f"{schema}.{_quote_identifier(self._best_table)}"

        cursor = self._connection.cursor()
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {best} CASCADE")
            cursor.execute(f"""
                    CREATE UNLOGGED TABLE {best} AS
                    SELECT
                        osm_row_id,
                        external_row_id,
                        distance_m,
                        match_field_delta,
                        score
                    FROM (
                        SELECT
                            c.*,
                            ROW_NUMBER() OVER (
                                PARTITION BY osm_row_id
                                ORDER BY
                                    score ASC,
                                    distance_m ASC,
                                    external_row_id ASC
                            ) AS score_rank
                        FROM {candidates} AS c
                        WHERE plausible
                          AND score IS NOT NULL
                    ) AS ranked
                    WHERE score_rank = 1
                """)
            cursor.execute(f"CREATE UNIQUE INDEX "
                           f"{_quote_identifier(self._best_table + '_osm_idx')} "
                           f"ON {best} (osm_row_id)")
            cursor.execute(f"CREATE INDEX "
                           f"{_quote_identifier(self._best_table + '_external_idx')} "
                           f"ON {best} (external_row_id)")
            cursor.execute(f"ANALYZE {best}")
        finally:
            cursor.close()

    def _counts(self, schema_name: str) -> tuple[int, int, int]:
        """Return reverse candidate, plausible, and selected-match counts."""
        schema = _quote_identifier(schema_name)
        candidates = f"{schema}.{_quote_identifier(self._candidate_table)}"
        best = f"{schema}.{_quote_identifier(self._best_table)}"

        cursor = self._connection.cursor()
        try:
            cursor.execute(f"""
                    SELECT
                        (SELECT COUNT(*)::bigint FROM {candidates}),
                        (
                            SELECT COUNT(*)::bigint
                            FROM {candidates}
                            WHERE plausible
                        ),
                        (SELECT COUNT(*)::bigint FROM {best})
                """)
            row = cursor.fetchone()
        finally:
            cursor.close()

        if row is None:
            return 0, 0, 0
        return tuple(int(value) for value in row)
