"""Configuration models for the generic spatial matching pipeline."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ColumnConversion:
    """Define a numeric column conversion."""

    source: str
    target: str
    factor: float


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Describe how a CSV source maps into the matching system."""

    latitude: str
    longitude: str
    id_column: str | None = None
    name_column: str | None = None
    match_field: str | None = None
    ignore_match_values: tuple[float, ...] = ()
    conversions: tuple[ColumnConversion, ...] = ()
    retain_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlausibilityGates:
    """Hard gates applied to candidate pairs before ranking."""

    max_match_field_delta: float | None = None


@dataclass(frozen=True, slots=True)
class ScoreConfig:
    """Weights used to rank plausible candidate pairs.

    The score is a penalty, so lower values are better::

        score = distance_m * distance_weight
              + match_field_delta * match_field_weight

    The secondary-field term contributes only when a delta is available.
    """

    distance_weight: float = 1.0
    match_field_weight: float = 5.0


@dataclass(frozen=True, slots=True)
class CandidateConfig:
    """Candidate generation, gating, and scoring configuration."""

    max_radius_m: float
    candidate_limit: int = 5
    gates: PlausibilityGates = PlausibilityGates()
    scoring: ScoreConfig = ScoreConfig()


@dataclass(frozen=True, slots=True)
class OutputField:
    """Map one output CSV column to a staged source column."""

    source: str
    column: str


@dataclass(frozen=True, slots=True)
class OutputConfig:
    """Configuration for the final match CSV."""

    fields: tuple[tuple[str, OutputField], ...] = ()


@dataclass(frozen=True, slots=True)
class AttentionConfig:
    """Configuration for important unmatched external records."""

    field: str
    threshold: float


@dataclass(frozen=True, slots=True)
class ReportingConfig:
    """Optional quality-control reporting configuration."""

    attention: AttentionConfig | None = None


@dataclass(frozen=True, slots=True)
class MatchingConfig:
    """Configuration for matching two point datasets."""

    osm: SourceSpec
    external: SourceSpec
    matching: CandidateConfig
    output: OutputConfig = OutputConfig()
    reporting: ReportingConfig = ReportingConfig()
