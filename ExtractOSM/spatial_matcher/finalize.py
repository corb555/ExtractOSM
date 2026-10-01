"""Final match selection, one-to-one resolution, and output tables."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .candidates import DEFAULT_CANDIDATE_TABLE, _quote_identifier
from .postgis import SpatialIndexTables
from .reverse import DEFAULT_REVERSE_MATCH_TABLE

DEFAULT_FORWARD_BEST_TABLE = "spatial_match_forward_best"
DEFAULT_FINAL_TABLE = "spatial_match_final"


@dataclass(frozen=True, slots=True)
class FinalMatchTable:
    """Metadata for final one-to-one matches."""

    schema: str
    forward_best_table: str
    final_table: str
    forward_best_rows: int
    mutual_best_rows: int
    final_rows: int


class PostGISFinalMatchBuilder:
    """Select forward winners, annotate mutual-best, and enforce one-to-one."""

    def __init__(
            self, connection: Any, *, candidate_table: str = DEFAULT_CANDIDATE_TABLE,
            reverse_best_table: str = DEFAULT_REVERSE_MATCH_TABLE,
            forward_best_table: str = DEFAULT_FORWARD_BEST_TABLE,
            final_table: str = DEFAULT_FINAL_TABLE, ) -> None:
        self._connection = connection
        self._candidate_table = candidate_table
        self._reverse_best_table = reverse_best_table
        self._forward_best_table = forward_best_table
        self._final_table = final_table

    def build(self, *, tables: SpatialIndexTables) -> FinalMatchTable:
        """Build final matches from the forward and reverse candidate results.

        Phase 9 selects the best and second-best plausible OSM candidate for each
        external point and records whether the best pair is also the reverse-best
        pair.

        Phase 10 conservatively enforces one-to-one assignment by allowing only
        the strongest forward winner for each OSM point. Mutual-best status is
        retained as diagnostic evidence but is not a hard requirement.
        """
        try:
            self._build_forward_best(tables.schema)
            self._build_final(tables.schema)
            counts = self._counts(tables.schema)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

        return FinalMatchTable(schema=tables.schema, forward_best_table=self._forward_best_table,
            final_table=self._final_table, forward_best_rows=counts[0], mutual_best_rows=counts[1],
            final_rows=counts[2], )

    def _build_forward_best(self, schema_name: str) -> None:
        schema = _quote_identifier(schema_name)
        candidates = f"{schema}.{_quote_identifier(self._candidate_table)}"
        reverse_best = f"{schema}.{_quote_identifier(self._reverse_best_table)}"
        forward_best = f"{schema}.{_quote_identifier(self._forward_best_table)}"

        cursor = self._connection.cursor()
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {forward_best} CASCADE")
            cursor.execute(f"""
                    CREATE UNLOGGED TABLE {forward_best} AS
                    WITH ranked AS (
                        SELECT
                            c.*,
                            ROW_NUMBER() OVER (
                                PARTITION BY external_row_id
                                ORDER BY
                                    score ASC,
                                    distance_m ASC,
                                    osm_row_id ASC
                            ) AS score_rank,
                            LEAD(score) OVER (
                                PARTITION BY external_row_id
                                ORDER BY
                                    score ASC,
                                    distance_m ASC,
                                    osm_row_id ASC
                            ) AS second_best_score
                        FROM {candidates} AS c
                        WHERE plausible
                          AND score IS NOT NULL
                    )
                    SELECT
                        r.external_row_id,
                        r.osm_row_id,
                        r.distance_m,
                        r.match_field_delta,
                        r.score,
                        r.second_best_score,
                        CASE
                            WHEN r.second_best_score IS NULL THEN NULL
                            ELSE r.second_best_score - r.score
                        END AS score_margin,
                        (
                            rb.osm_row_id IS NOT NULL
                            AND rb.external_row_id = r.external_row_id
                        ) AS mutual_best
                    FROM ranked AS r
                    LEFT JOIN {reverse_best} AS rb
                      ON rb.osm_row_id = r.osm_row_id
                    WHERE r.score_rank = 1
                """)
            cursor.execute(f"CREATE UNIQUE INDEX "
                           f"{_quote_identifier(self._forward_best_table + '_external_idx')} "
                           f"ON {forward_best} (external_row_id)")
            cursor.execute(f"CREATE INDEX "
                           f"{_quote_identifier(self._forward_best_table + '_osm_idx')} "
                           f"ON {forward_best} (osm_row_id)")
            cursor.execute(f"ANALYZE {forward_best}")
        finally:
            cursor.close()

    def _build_final(self, schema_name: str) -> None:
        """Keep at most one external winner for each OSM feature."""
        schema = _quote_identifier(schema_name)
        forward_best = f"{schema}.{_quote_identifier(self._forward_best_table)}"
        final = f"{schema}.{_quote_identifier(self._final_table)}"

        cursor = self._connection.cursor()
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {final} CASCADE")
            cursor.execute(f"""
                    CREATE UNLOGGED TABLE {final} AS
                    SELECT
                        external_row_id,
                        osm_row_id,
                        distance_m,
                        match_field_delta,
                        score,
                        second_best_score,
                        score_margin,
                        mutual_best
                    FROM (
                        SELECT
                            f.*,
                            ROW_NUMBER() OVER (
                                PARTITION BY osm_row_id
                                ORDER BY
                                    mutual_best DESC,
                                    score ASC,
                                    distance_m ASC,
                                    external_row_id ASC
                            ) AS osm_rank
                        FROM {forward_best} AS f
                    ) AS ranked
                    WHERE osm_rank = 1
                """)
            cursor.execute(f"CREATE UNIQUE INDEX "
                           f"{_quote_identifier(self._final_table + '_osm_idx')} "
                           f"ON {final} (osm_row_id)")
            cursor.execute(f"CREATE UNIQUE INDEX "
                           f"{_quote_identifier(self._final_table + '_external_idx')} "
                           f"ON {final} (external_row_id)")
            cursor.execute(f"ANALYZE {final}")
        finally:
            cursor.close()

    def _counts(self, schema_name: str) -> tuple[int, int, int]:
        schema = _quote_identifier(schema_name)
        forward_best = f"{schema}.{_quote_identifier(self._forward_best_table)}"
        final = f"{schema}.{_quote_identifier(self._final_table)}"

        cursor = self._connection.cursor()
        try:
            cursor.execute(f"""
                    SELECT
                        (SELECT COUNT(*)::bigint FROM {forward_best}),
                        (
                            SELECT COUNT(*)::bigint
                            FROM {forward_best}
                            WHERE mutual_best
                        ),
                        (SELECT COUNT(*)::bigint FROM {final})
                """)
            row = cursor.fetchone()
        finally:
            cursor.close()

        if row is None:
            return 0, 0, 0
        return tuple(int(value) for value in row)
