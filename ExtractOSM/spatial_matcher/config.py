"""YAML configuration loading and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import (AttentionConfig, CandidateConfig, ColumnConversion, MatchingConfig,
                     OutputConfig, OutputField, PlausibilityGates, ReportingConfig, ScoreConfig,
                     SourceSpec, )


def _require_mapping(value: object, section: str) -> dict[str, Any]:
    """Return ``value`` as a mapping or raise a useful error."""
    if not isinstance(value, dict):
        raise ValueError(f"{section} must be a YAML mapping")
    return value


def _optional_mapping(value: object, section: str) -> dict[str, Any]:
    """Return an optional mapping."""
    if value is None:
        return {}
    return _require_mapping(value, section)


def _optional_string(value: object, field_name: str) -> str | None:
    """Normalize an optional string configuration value."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _required_string(value: object, field_name: str) -> str:
    """Normalize a required string configuration value."""
    result = _optional_string(value, field_name)
    if result is None:
        raise ValueError(f"{field_name} is required")
    return result


def _required_positive_float(value: object, field_name: str) -> float:
    """Parse a required positive floating-point value."""
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if result <= 0:
        raise ValueError(f"{field_name} must be greater than zero")
    return result


def _optional_positive_float(value: object, field_name: str) -> float | None:
    """Parse an optional positive floating-point value."""
    if value is None:
        return None
    return _required_positive_float(value, field_name)


def _optional_nonnegative_float(
        value: object, field_name: str, *, default: float, ) -> float:
    """Parse an optional non-negative floating-point value."""
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if result < 0:
        raise ValueError(f"{field_name} must be zero or greater")
    return result


def _positive_int(value: object, field_name: str, *, default: int) -> int:
    """Parse an optional positive integer."""
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive integer") from exc
    if result <= 0:
        raise ValueError(f"{field_name} must be greater than zero")
    return result


def _parse_conversions(
        value: object, section_name: str, ) -> tuple[ColumnConversion, ...]:
    """Parse zero or more column multiplication conversions."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{section_name}.conversions must be a YAML list")

    conversions: list[ColumnConversion] = []
    for index, item in enumerate(value):
        item_name = f"{section_name}.conversions[{index}]"
        mapping = _require_mapping(item, item_name)
        try:
            factor = float(mapping["factor"])
        except KeyError as exc:
            raise ValueError(f"{item_name}.factor is required") from exc
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{item_name}.factor must be numeric") from exc

        conversions.append(ColumnConversion(
            source=_required_string(mapping.get("source"), f"{item_name}.source", ),
            target=_required_string(mapping.get("target"), f"{item_name}.target", ),
            factor=factor, ))
    return tuple(conversions)


def _parse_retain_fields(value: object, section_name: str) -> tuple[str, ...]:
    """Parse optional pass-through fields."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{section_name}.retain_fields must be a YAML list")
    return tuple(_required_string(item, f"{section_name}.retain_fields[{index}]") for index, item in
                 enumerate(value))


def _parse_ignore_match_values(
        value: object, section_name: str, ) -> tuple[float, ...]:
    """Parse numeric match-field values that should be treated as missing."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{section_name}.ignore_match_values must be a YAML list")

    values: list[float] = []
    for index, item in enumerate(value):
        try:
            values.append(float(item))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{section_name}.ignore_match_values[{index}] must be numeric") from exc
    return tuple(values)


def _parse_source(value: object, section_name: str) -> SourceSpec:
    """Parse one source definition."""
    mapping = _require_mapping(value, section_name)
    return SourceSpec(
        latitude=_required_string(mapping.get("latitude"), f"{section_name}.latitude", ),
        longitude=_required_string(mapping.get("longitude"), f"{section_name}.longitude", ),
        id_column=_optional_string(mapping.get("id"), f"{section_name}.id", ),
        name_column=_optional_string(mapping.get("name"), f"{section_name}.name", ),
        match_field=_optional_string(mapping.get("match_field"), f"{section_name}.match_field", ),
        ignore_match_values=_parse_ignore_match_values(mapping.get("ignore_match_values"),
            section_name, ),
        conversions=_parse_conversions(mapping.get("conversions"), section_name, ),
        retain_fields=_parse_retain_fields(mapping.get("retain_fields"), section_name, ), )


def _parse_matching(value: object) -> CandidateConfig:
    """Parse candidate generation, gating, and scoring settings."""
    mapping = _require_mapping(value, "matching")
    gates_mapping = _optional_mapping(mapping.get("gates"), "matching.gates")
    scoring_mapping = _optional_mapping(mapping.get("scoring"), "matching.scoring", )

    return CandidateConfig(max_radius_m=_required_positive_float(mapping.get("max_radius_m"),
        "matching.max_radius_m", ),
        candidate_limit=_positive_int(mapping.get("candidate_limit"), "matching.candidate_limit",
            default=5, ), gates=PlausibilityGates(max_match_field_delta=_optional_positive_float(
            gates_mapping.get("max_match_field_delta"), "matching.gates.max_match_field_delta", )),
        scoring=ScoreConfig(
            distance_weight=_optional_nonnegative_float(scoring_mapping.get("distance_weight"),
                "matching.scoring.distance_weight", default=1.0, ),
            match_field_weight=_optional_nonnegative_float(
                scoring_mapping.get("match_field_weight"), "matching.scoring.match_field_weight",
                default=5.0, ), ), )


def _parse_output(value: object) -> OutputConfig:
    """Parse generic final-output column mappings."""
    mapping = _optional_mapping(value, "output")
    fields_mapping = _optional_mapping(mapping.get("fields"), "output.fields")

    fields: list[tuple[str, OutputField]] = []
    for output_name, field_value in fields_mapping.items():
        field_name = _required_string(output_name, "output field name")
        field_mapping = _require_mapping(field_value, f"output.fields.{field_name}", )
        source = _required_string(field_mapping.get("source"),
            f"output.fields.{field_name}.source", ).lower()
        if source not in {"osm", "external"}:
            raise ValueError(f"output.fields.{field_name}.source must be 'osm' or 'external'")
        fields.append((field_name, OutputField(source=source,
            column=_required_string(field_mapping.get("column"),
                f"output.fields.{field_name}.column", ), ),))

    return OutputConfig(fields=tuple(fields))


def _parse_reporting(value: object) -> ReportingConfig:
    """Parse optional quality-control reporting settings."""
    mapping = _optional_mapping(value, "reporting")
    attention_mapping = _optional_mapping(mapping.get("attention"), "reporting.attention", )
    if not attention_mapping:
        return ReportingConfig()

    field = _required_string(attention_mapping.get("field"), "reporting.attention.field", )
    threshold = _required_positive_float(attention_mapping.get("threshold"),
        "reporting.attention.threshold", )
    return ReportingConfig(attention=AttentionConfig(field=field, threshold=threshold))


def load_matching_config(path: str | Path) -> MatchingConfig:
    """Load a spatial matching configuration from YAML."""
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"Configuration not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {config_path}: {exc}") from exc

    root = _require_mapping(raw, "configuration")
    return MatchingConfig(osm=_parse_source(root.get("osm"), "osm"),
        external=_parse_source(root.get("external"), "external"),
        matching=_parse_matching(root.get("matching")), output=_parse_output(root.get("output")),
        reporting=_parse_reporting(root.get("reporting")), )
