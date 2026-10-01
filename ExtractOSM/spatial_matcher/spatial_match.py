"""
Command-line entry point for generic spatial matching.
Given two lists of points, match the external points to OSM points using a spatial join
and fuzzy text matching.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

import psycopg
import yaml

from .orchestrator import SpatialMatchOrchestrator
from .outputs import default_output_paths

DEFAULT_MATCH_CONFIG = "spatial_match.yml"
DEFAULT_DB_CONFIG = "config/db_config.yml"
DATABASE_CONFIG_TYPE = "Database"


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Match an external point dataset to OSM point features.")
    parser.add_argument("osm_name", help="OSM feature CSV")
    parser.add_argument("external_name", help="External point CSV")
    parser.add_argument("extent_name", choices=("CONUS", "USWEST"), type=str.upper,
        help="Named processing extent", )
    parser.add_argument("--config", default=DEFAULT_MATCH_CONFIG,
        help=f"YAML matching configuration (default: {DEFAULT_MATCH_CONFIG})", )
    parser.add_argument("--db-config", default=DEFAULT_DB_CONFIG,
        help=f"YAML database configuration (default: {DEFAULT_DB_CONFIG})", )
    parser.add_argument("--output", type=Path,
        help="Final match CSV. Defaults beside the OSM input CSV.", )
    parser.add_argument("--touch-only", action="store_true",
        help="Touch the primary output file and exit without performing spatial matching.", )
    parser.add_argument("--external-output", type=Path,
        help="Filtered external CSV with external_id. Defaults beside the external CSV.", )
    parser.add_argument("-v", "--verbose", action="store_true",
        help="Enable verbose diagnostic logging.", )
    return parser


def load_db_connection_config(path: str | Path) -> dict[str, Any]:
    """Load psycopg connection parameters from a database YAML file."""
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"Database configuration not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("Database configuration must be a YAML mapping")
    if raw.get("config_type") != DATABASE_CONFIG_TYPE:
        raise ValueError(f"Database configuration must have config_type: {DATABASE_CONFIG_TYPE!r}")

    connection = raw.get("connection")
    if not isinstance(connection, dict):
        raise ValueError("Database configuration requires a 'connection' mapping")

    required = ("host", "port", "dbname")
    missing = [field for field in required if connection.get(field) in (None, "")]
    if missing:
        raise ValueError("Database connection is missing required field(s): " + ", ".join(missing))

    allowed = {"host", "port", "dbname", "user", "password", "connect_timeout", "sslmode", }
    return {key: value for key, value in connection.items() if key in allowed and value is not None}


def main() -> int:
    """Run the command-line application."""
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s", )
    default_match, default_external = default_output_paths(osm_name=args.osm_name,
        external_name=args.external_name, extent_name=args.extent_name, )
    match_output = args.output or default_match
    external_output = args.external_output or default_external

    if args.touch_only:
        try:
            match_output.parent.mkdir(parents=True, exist_ok=True)
            match_output.touch()
        except OSError as exc:
            logging.error("Error: %s", exc)
            return 1

        logging.info("Step not required. Skipping")
        return 0

    try:
        db_config = load_db_connection_config(args.db_config)
        with psycopg.connect(**db_config) as connection:
            result = SpatialMatchOrchestrator().run(osm_name=args.osm_name,
                external_name=args.external_name, extent_name=args.extent_name,
                config_name=args.config, connection=connection, match_output=match_output,
                external_output=external_output, )
    except (FileNotFoundError, ValueError, psycopg.Error, OSError) as exc:
        logging.error("Error: %s", exc)
        return 1

    logging.info("Extent: %s", result.extent.name)
    logging.info("OSM features: %s input -> %s retained", f"{result.osm_input_rows:,}",
        f"{len(result.osm):,}", )
    logging.info("External features: %s input -> %s retained", f"{result.external_input_rows:,}",
        f"{len(result.external):,}", )

    osm_match = result.config.osm.match_field or "(spatial only)"
    external_match = result.config.external.match_field or "(spatial only)"
    logging.info("Optional field match: OSM=%s, external=%s", osm_match, external_match, )

    if result.spatial_indexes is not None:
        logging.info("Spatial indexes: OSM=%s, external=%s", result.spatial_indexes.osm_table,
            result.spatial_indexes.external_table, )

    if result.candidates is not None:
        logging.info("Candidates: %s total, %s plausible, %s rejected, %s scored",
            f"{result.candidates.rows:,}", f"{result.candidates.plausible_rows:,}",
            f"{result.candidates.rejected_rows:,}", f"{result.candidates.scored_rows:,}", )

    if result.reverse_matches is not None:
        logging.info("Reverse matching: %s candidates, %s plausible, %s OSM points matched",
            f"{result.reverse_matches.candidate_rows:,}",
            f"{result.reverse_matches.plausible_rows:,}",
            f"{result.reverse_matches.matched_osm_rows:,}", )

    if result.final_matches is not None:
        logging.info("Final matching: %s forward winners, %s mutual-best, %s one-to-one matches",
            f"{result.final_matches.forward_best_rows:,}",
            f"{result.final_matches.mutual_best_rows:,}", f"{result.final_matches.final_rows:,}", )

    if result.output_files is not None:
        logging.info("Match CSV: %s", result.output_files.matches)
        logging.info("External CSV: %s", result.output_files.external)
        if result.output_files.attention_unmatched is not None:
            logging.info("High-attention unmatched: %s rows -> %s",
                f"{result.output_files.attention_unmatched_rows:,}",
                result.output_files.attention_unmatched, )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
