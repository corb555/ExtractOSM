"""Generic CSV loading, validation, and source normalization."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .models import SourceSpec


def _read_csv(path: str | Path) -> pd.DataFrame:
    """Read a non-empty CSV and normalize its column names."""
    csv_path = Path(path).expanduser().resolve()
    if not csv_path.is_file():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    try:
        frame = pd.read_csv(csv_path)
    except Exception as exc:
        raise ValueError(f"Unable to read CSV {csv_path}: {exc}") from exc

    if frame.empty:
        raise ValueError(f"CSV contains no rows: {csv_path}")

    columns = [str(column).lstrip("\ufeff").strip() for column in frame.columns]
    duplicates = sorted({column for column in columns if columns.count(column) > 1})
    if duplicates:
        raise ValueError(f"CSV contains duplicate columns after normalization: "
                         f"{', '.join(duplicates)}")

    frame.columns = columns
    return frame


def _required_input_columns(spec: SourceSpec) -> set[str]:
    """Return columns that must exist in the original CSV."""
    columns = {spec.latitude, spec.longitude, *spec.retain_fields, }

    for optional in (spec.id_column, spec.name_column):
        if optional:
            columns.add(optional)

    conversion_targets = {conversion.target for conversion in spec.conversions}
    if spec.match_field and spec.match_field not in conversion_targets:
        columns.add(spec.match_field)

    columns.update(conversion.source for conversion in spec.conversions)
    return columns


def _validate_columns(
        frame: pd.DataFrame, spec: SourceSpec, source_label: str, ) -> None:
    """Ensure the CSV has every configured source column."""
    missing = sorted(_required_input_columns(spec) - set(frame.columns))
    if missing:
        raise ValueError(f"{source_label} is missing configured columns: "
                         f"{', '.join(missing)}")


def _convert_numeric(
        frame: pd.DataFrame, column: str, source_label: str, ) -> pd.Series:
    """Convert a column to numeric values and reject invalid non-null data."""
    values = pd.to_numeric(frame[column], errors="coerce")
    invalid = values.isna() & frame[column].notna()
    if invalid.any():
        count = int(invalid.sum())
        raise ValueError(f"{source_label}.{column} contains {count} non-numeric values")
    return values


def load_source(
        path: str | Path, spec: SourceSpec, source_label: str, ) -> pd.DataFrame:
    """Load a configured point source and normalize its coordinates.

    Numeric conversions are intentionally deferred until after geographic
    filtering. This avoids normalizing fields for rows that will never enter the
    matching set, which is important for very large external datasets.

    Args:
        path: CSV file path.
        spec: Column-role and conversion configuration.
        source_label: Name used in validation errors.

    Returns:
        Dataframe with validated numeric latitude and longitude columns.

    Raises:
        ValueError: If required columns or coordinates are invalid.
    """
    frame = _read_csv(path)
    _validate_columns(frame, spec, source_label)

    prepared = frame.copy()
    prepared[spec.latitude] = _convert_numeric(prepared, spec.latitude, source_label, )
    prepared[spec.longitude] = _convert_numeric(prepared, spec.longitude, source_label, )

    invalid_coordinates = (prepared[spec.latitude].isna() | prepared[spec.longitude].isna())
    if invalid_coordinates.any():
        count = int(invalid_coordinates.sum())
        raise ValueError(f"{source_label} contains {count} rows with missing coordinates")

    return prepared


def normalize_source(
        frame: pd.DataFrame, spec: SourceSpec, source_label: str, ) -> pd.DataFrame:
    """Apply configured numeric normalization to a filtered source.

    Each configured conversion creates a target column using::

        target = source * factor

    A configured numeric match field is also normalized to numeric values after
    conversions have been applied. This allows the match field to refer either
    to an original source column or to a conversion target.

    Args:
        frame: Extent-filtered source dataframe.
        spec: Source configuration.
        source_label: Name used in validation errors.

    Returns:
        Copy with conversions and numeric match-field normalization applied.
    """
    prepared = frame.copy()

    for conversion in spec.conversions:
        source_values = _convert_numeric(prepared, conversion.source, source_label, )
        prepared[conversion.target] = source_values * conversion.factor

    if spec.match_field:
        prepared[spec.match_field] = _convert_numeric(prepared, spec.match_field, source_label, )
        if spec.ignore_match_values:
            prepared.loc[prepared[spec.match_field].isin(
                spec.ignore_match_values), spec.match_field,] = float("nan")

    return prepared


EXTERNAL_ID_COLUMN = "external_id"


def ensure_external_id(
        frame: pd.DataFrame, spec: SourceSpec, ) -> pd.DataFrame:
    """Guarantee a stable external identifier before filtering.

    If the external source declares an ID column, its values are copied to the
    canonical ``external_id`` column. Otherwise a one-based ID is generated from
    the original CSV row order. Because this happens before extent filtering, the
    generated ID remains traceable to the original source file.

    Args:
        frame: Loaded external dataframe.
        spec: External source configuration.

    Returns:
        Copy containing ``external_id``.
    """
    prepared = frame.copy()

    if spec.id_column:
        prepared[EXTERNAL_ID_COLUMN] = prepared[spec.id_column]
    elif EXTERNAL_ID_COLUMN not in prepared.columns:
        prepared[EXTERNAL_ID_COLUMN] = pd.RangeIndex(start=1, stop=len(prepared) + 1, )

    if prepared[EXTERNAL_ID_COLUMN].isna().any():
        raise ValueError("external_id contains missing values")
    if prepared[EXTERNAL_ID_COLUMN].duplicated().any():
        raise ValueError("external_id contains duplicate values")

    return prepared
