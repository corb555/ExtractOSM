"""CSV output and unmatched-attention reporting."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .candidates import DEFAULT_CANDIDATE_TABLE, _quote_identifier
from .finalize import DEFAULT_FINAL_TABLE, DEFAULT_FORWARD_BEST_TABLE
from .models import MatchingConfig
from .postgis import ROW_ID_COLUMN, SpatialIndexTables
from .sources import EXTERNAL_ID_COLUMN

DEFAULT_ATTENTION_SUFFIX = "_high_attention_unmatched.csv"


@dataclass(frozen=True, slots=True)
class OutputFiles:
    """Files written by the final output stage."""

    matches: Path
    external: Path
    attention_unmatched: Path | None = None
    attention_unmatched_rows: int = 0


def default_output_paths(
        *, osm_name: str | Path, external_name: str | Path, extent_name: str, ) -> tuple[
    Path, Path]:
    """Derive default match and prepared-external output paths."""
    osm_path = Path(osm_name)
    external_path = Path(external_name)
    extent = extent_name.lower()

    match_output = osm_path.with_name(f"{osm_path.stem}_matched.csv")
    external_output = external_path.with_name(f"{external_path.stem}_{extent}_prepared.csv")
    return match_output, external_output


class MatchOutputWriter:
    """Write final generic match output and QC files."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def write(
            self, *, tables: SpatialIndexTables, config: MatchingConfig,
            external_frame: pd.DataFrame, match_output: str | Path, external_output: str | Path,
            final_table: str = DEFAULT_FINAL_TABLE,
            candidate_table: str = DEFAULT_CANDIDATE_TABLE, ) -> OutputFiles:
        """Write final matches and the manageable filtered external dataset."""
        match_path = Path(match_output).expanduser()
        external_path = Path(external_output).expanduser()
        match_path.parent.mkdir(parents=True, exist_ok=True)
        external_path.parent.mkdir(parents=True, exist_ok=True)

        self._write_matches(tables=tables, config=config, output=match_path,
            final_table=final_table, )
        self._write_external(external_frame, external_path)

        attention_path = None
        attention_rows = 0
        if config.reporting.attention is not None:
            attention_path = match_path.with_name(f"{match_path.stem}{DEFAULT_ATTENTION_SUFFIX}")
            attention_rows = self._write_attention_unmatched(tables=tables, config=config,
                output=attention_path, final_table=final_table, candidate_table=candidate_table, )

        return OutputFiles(matches=match_path, external=external_path,
            attention_unmatched=attention_path, attention_unmatched_rows=attention_rows, )

    @staticmethod
    def _write_external(frame: pd.DataFrame, output: Path) -> None:
        """Write filtered normalized external data without internal coordinates."""
        columns = [column for column in frame.columns if column not in {"match_x", "match_y"}]
        frame.loc[:, columns].to_csv(output, index=False)

    def _write_matches(
            self, *, tables: SpatialIndexTables, config: MatchingConfig, output: Path,
            final_table: str, ) -> None:
        """Write configured columns for accepted one-to-one matches."""
        if not config.output.fields:
            raise ValueError("output.fields must define at least one final CSV column")

        schema = _quote_identifier(tables.schema)
        final = f"{schema}.{_quote_identifier(final_table)}"
        osm = f"{schema}.{_quote_identifier(tables.osm_table)}"
        external = f"{schema}.{_quote_identifier(tables.external_table)}"
        row_id = _quote_identifier(ROW_ID_COLUMN)

        select_parts: list[str] = []
        for output_name, field in config.output.fields:
            alias = "o" if field.source == "osm" else "e"
            select_parts.append(f"{alias}.{_quote_identifier(field.column)} "
                                f"AS {_quote_identifier(output_name)}")

        query = f"""
            SELECT {", ".join(select_parts)}
            FROM {final} AS f
            JOIN {osm} AS o
              ON o.{row_id} = f.osm_row_id
            JOIN {external} AS e
              ON e.{row_id} = f.external_row_id
            ORDER BY f.osm_row_id
        """
        self._copy_query_to_csv(query, output)

    def _write_attention_unmatched(
            self, *, tables: SpatialIndexTables, config: MatchingConfig, output: Path,
            final_table: str, candidate_table: str, ) -> int:
        """Write important unmatched records with actionable diagnostics.

        The report distinguishes records that had no spatial candidate, records
        whose spatial candidates all failed a plausibility gate, and records that
        had a plausible forward winner but lost final one-to-one resolution.
        It also exposes both source match-field values so bad secondary data can
        be identified without another database query.
        """
        attention = config.reporting.attention
        if attention is None:
            return 0

        schema = _quote_identifier(tables.schema)
        final = f"{schema}.{_quote_identifier(final_table)}"
        forward_best = f"{schema}.{_quote_identifier(DEFAULT_FORWARD_BEST_TABLE)}"
        candidates = f"{schema}.{_quote_identifier(candidate_table)}"
        osm = f"{schema}.{_quote_identifier(tables.osm_table)}"
        external = f"{schema}.{_quote_identifier(tables.external_table)}"
        row_id = _quote_identifier(ROW_ID_COLUMN)
        attention_field = _quote_identifier(attention.field)

        osm_name_select = (
            f"o.{_quote_identifier(config.osm.name_column)}" if config.osm.name_column else
            "NULL::text")
        osm_id_select = (
            f"o.{_quote_identifier(config.osm.id_column)}" if config.osm.id_column else f"o."
                                                                                        f"{row_id}")
        osm_lat_select = f"o.{_quote_identifier(config.osm.latitude)}"
        osm_lon_select = f"o.{_quote_identifier(config.osm.longitude)}"
        osm_match_select = (
            f"o.{_quote_identifier(config.osm.match_field)}" if config.osm.match_field else
            "NULL::double precision")
        external_match_select = (
            f"e.{_quote_identifier(config.external.match_field)}" if config.external.match_field
            else "NULL::double precision")

        query = f"""
            SELECT
                e.{_quote_identifier(EXTERNAL_ID_COLUMN)} AS external_id,
                e.{_quote_identifier(config.external.latitude)} AS external_lat,
                e.{_quote_identifier(config.external.longitude)} AS external_lon,
                e.{attention_field} AS attention_value,
                CASE
                    WHEN nearest.osm_row_id IS NULL THEN 'NO_OSM_CANDIDATE'
                    WHEN fb.external_row_id IS NOT NULL THEN 'ONE_TO_ONE_CONFLICT'
                    WHEN plausible_candidate.osm_row_id IS NULL
                        THEN COALESCE(nearest.rejection_reason, 'NO_PLAUSIBLE_CANDIDATE')
                    ELSE 'UNMATCHED'
                END AS final_rejection_reason,
                nearest.osm_row_id AS closest_osm_row_id,
                {osm_id_select} AS closest_osm_id,
                {osm_name_select} AS closest_osm_name,
                {osm_lat_select} AS closest_osm_lat,
                {osm_lon_select} AS closest_osm_lon,
                nearest.distance_m AS closest_distance_m,
                {external_match_select} AS external_match_value,
                {osm_match_select} AS closest_osm_match_value,
                nearest.match_field_delta AS closest_match_field_delta,
                nearest.plausible AS closest_plausible,
                nearest.rejection_reason AS closest_rejection_reason,
                nearest.score AS closest_score,
                plausible_candidate.osm_row_id AS best_plausible_osm_row_id,
                plausible_candidate.distance_m AS best_plausible_distance_m,
                plausible_candidate.match_field_delta AS best_plausible_match_field_delta,
                plausible_candidate.score AS best_plausible_score,
                fb.osm_row_id AS forward_best_osm_row_id,
                fb_osm_id.osm_id AS forward_best_osm_id,
                fb_osm_id.osm_name AS forward_best_osm_name,
                fb.score AS forward_best_score,
                fb.second_best_score,
                fb.score_margin,
                fb.mutual_best,
                winner.external_row_id AS winning_external_row_id,
                winner_external.{_quote_identifier(EXTERNAL_ID_COLUMN)} AS winning_external_id,
                winner.score AS winning_score
            FROM {external} AS e
            LEFT JOIN {final} AS accepted
              ON accepted.external_row_id = e.{row_id}
            LEFT JOIN {forward_best} AS fb
              ON fb.external_row_id = e.{row_id}
            LEFT JOIN LATERAL (
                SELECT
                    c.osm_row_id,
                    c.distance_m,
                    c.match_field_delta,
                    c.plausible,
                    c.rejection_reason,
                    c.score
                FROM {candidates} AS c
                WHERE c.external_row_id = e.{row_id}
                ORDER BY c.distance_m ASC, c.osm_row_id ASC
                LIMIT 1
            ) AS nearest ON TRUE
            LEFT JOIN LATERAL (
                SELECT
                    c.osm_row_id,
                    c.distance_m,
                    c.match_field_delta,
                    c.score
                FROM {candidates} AS c
                WHERE c.external_row_id = e.{row_id}
                  AND c.plausible
                  AND c.score IS NOT NULL
                ORDER BY c.score ASC, c.distance_m ASC, c.osm_row_id ASC
                LIMIT 1
            ) AS plausible_candidate ON TRUE
            LEFT JOIN {osm} AS o
              ON o.{row_id} = nearest.osm_row_id
            LEFT JOIN LATERAL (
                SELECT
                    {('o2.' + _quote_identifier(config.osm.id_column)) if config.osm.id_column else ('o2.' + row_id)} AS osm_id,
                    {('o2.' + _quote_identifier(config.osm.name_column)) if config.osm.name_column else 'NULL::text'} AS osm_name
                FROM {osm} AS o2
                WHERE o2.{row_id} = fb.osm_row_id
                LIMIT 1
            ) AS fb_osm_id ON TRUE
            LEFT JOIN {final} AS winner
              ON winner.osm_row_id = fb.osm_row_id
            LEFT JOIN {external} AS winner_external
              ON winner_external.{row_id} = winner.external_row_id
            WHERE accepted.external_row_id IS NULL
              AND e.{attention_field} >= %s
            ORDER BY e.{attention_field} DESC,
                     e.{_quote_identifier(EXTERNAL_ID_COLUMN)}
        """
        return self._copy_query_to_csv(query, output, params=(attention.threshold,), )

    def _copy_query_to_csv(
            self, query: str, output: Path, *, params: tuple[object, ...] = (), ) -> int:
        """Execute a SELECT and stream its rows to a CSV file."""
        cursor = self._connection.cursor()
        try:
            cursor.execute(query, params)
            names = [description.name for description in cursor.description]
            count = 0
            with output.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(names)
                while rows := cursor.fetchmany(10_000):
                    writer.writerows(rows)
                    count += len(rows)
            return count
        finally:
            cursor.close()
