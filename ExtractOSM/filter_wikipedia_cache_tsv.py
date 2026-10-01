#!/usr/bin/env python3
"""
Create a TSV subset of Wikipedia cache records with wikipedia_length > 0.

Each matching record is preserved as a row, with summary text normalized by
clean_summary() so every TSV record remains on one physical line.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re
import sys
from typing import Any

LENGTH_FIELD = "wikipedia_length"
SUMMARY_FIELD = "summary"


def load_json(path: Path) -> dict[str, Any]:
    """Load and validate a JSON object."""
    if not path.exists():
        raise FileNotFoundError(f"Input JSON file not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")

    return data


def get_wikipedia_records(cache: dict[str, Any]) -> dict[str, Any]:
    """
    Return the Wikipedia record mapping.

    Schema-v2 shared caches store records under ``wikipedia``. A direct
    Wikipedia record mapping is also accepted.
    """
    if "wikipedia" in cache:
        wikipedia = cache["wikipedia"]
        if not isinstance(wikipedia, dict):
            raise ValueError("Top-level 'wikipedia' value must be an object")
        return wikipedia

    return cache


def clean_summary(text: str) -> str:
    """Normalize summary whitespace while preserving the text content."""
    return re.sub(r"\s+", " ", text).strip()


def filtered_records(records: dict[str, Any]):
    """Yield full records whose wikipedia_length is numeric and greater than zero."""
    for key, record in records.items():
        if not isinstance(record, dict):
            raise ValueError(f"Wikipedia record must be an object: {key!r}")

        if LENGTH_FIELD not in record:
            raise ValueError(f"Wikipedia record {key!r} is missing required field {LENGTH_FIELD!r}")

        length = record[LENGTH_FIELD]

        if length is None:
            continue

        if isinstance(length, bool) or not isinstance(length, (int, float)):
            raise ValueError(f"Wikipedia record {key!r} has non-numeric "
                             f"{LENGTH_FIELD}: {length!r}")

        if length <= 0:
            continue

        row = dict(record)

        summary = row.get(SUMMARY_FIELD)
        if summary is None:
            row[SUMMARY_FIELD] = ""
        elif not isinstance(summary, str):
            raise ValueError(f"Wikipedia record {key!r} has non-string summary: {summary!r}")
        else:
            row[SUMMARY_FIELD] = clean_summary(summary)

        yield key, row


def scalarize(value: Any) -> str | int | float | bool | None:
    """Convert nested values to compact JSON so all record fields fit in TSV cells."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def write_tsv(records: dict[str, Any], output_path: Path) -> tuple[int, int]:
    """Filter records, clean summaries, and write the result as TSV."""
    selected = list(filtered_records(records))

    fieldnames: list[str] = ["cache_key"]
    seen = {"cache_key"}

    for _, record in selected:
        for field in record:
            if field not in seen:
                seen.add(field)
                fieldnames.append(field)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")

    with temp_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, delimiter="\t",
            quoting=csv.QUOTE_MINIMAL, lineterminator="\n", )
        writer.writeheader()

        for key, record in selected:
            row = {"cache_key": key}
            row.update({field: scalarize(value) for field, value in record.items()})
            writer.writerow(row)

    temp_path.replace(output_path)
    return len(selected), len(records)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=("Create a TSV subset of Wikipedia cache records where "
                     "wikipedia_length > 0, with summary whitespace normalized."))
    parser.add_argument("--cache", type=Path, required=True,
        help="Input Wikipedia or shared Wiki metadata JSON cache.", )
    parser.add_argument("--output", type=Path, required=True, help="Output TSV file.", )
    args = parser.parse_args()

    try:
        cache = load_json(args.cache)
        records = get_wikipedia_records(cache)
        written, total = write_tsv(records, args.output)
    except (FileNotFoundError, ValueError, json.JSONDecodeError, OSError) as exc:
        sys.exit(f"❌ ERROR: {exc}")

    print(f"✅ Wrote {written} of {total} Wikipedia records "
          f"with {LENGTH_FIELD} > 0 to {args.output}")


if __name__ == "__main__":
    main()
