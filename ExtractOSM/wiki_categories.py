#!/usr/bin/env python3
"""
Build and incrementally update a local Wikidata category cache.

The utility scans the Wikidata entity section of the shared Wiki metadata cache
for P31 ("instance of") QIDs, fetches each class's human-readable label and direct
P279 ("subclass of") parents, then recursively resolves those parents until the
discovered class graph is complete.

Cache format:

{
  "p31": {
    "Q12345": {
      "label": "stratovolcano",
      "subclass_of": ["Q8072"]
    }
  }
}

DATA INTEGRITY:
Required API fields must raise if missing. Do not hide schema errors with
dict.get(..., default), swallowed exceptions, or fabricated values.
Optional missing data may use None/empty collections only when absence is a
documented domain state.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import time
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)

WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
BATCH_SIZE = 50
REQUEST_DELAY_SECONDS = 1.0
DEFAULT_LANGUAGE = "en"
DEFAULT_CATEGORY_CACHE = "wiki_categories.json"

RETRYABLE_STATUS_CODES = {429, 503}
DEFAULT_RETRY_SECONDS = 5.0
MAX_RETRIES = 5


def load_json_mapping(path: Path, *, missing_ok: bool = False) -> dict[str, Any]:
    """Load a JSON object from disk."""
    if not path.exists():
        if missing_ok:
            return {}
        raise FileNotFoundError(f"JSON file not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")

    return data


def _files_identical(path_a: Path, path_b: Path, chunk_size: int = 1024 * 1024) -> bool:
    """Return whether two files contain exactly the same bytes."""
    if path_a.stat().st_size != path_b.stat().st_size:
        return False

    with path_a.open("rb") as file_a, path_b.open("rb") as file_b:
        while True:
            chunk_a = file_a.read(chunk_size)
            chunk_b = file_b.read(chunk_size)

            if chunk_a != chunk_b:
                return False
            if not chunk_a:
                return True


def save_category_cache(cache: dict[str, Any], path: Path) -> bool:
    """Atomically save the category cache only when its contents changed.

    The cache is serialized deterministically to a temporary file. If that file
    is byte-for-byte identical to the existing cache, the temporary file is
    removed and the existing cache is left untouched so its modification time is
    preserved.

    Returns:
        True when the cache file was created or replaced, otherwise False.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")

    try:
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(cache, file, indent=2, sort_keys=True, ensure_ascii=False)
            file.write("\n")

        if path.exists() and _files_identical(path, temp_path):
            temp_path.unlink()
            return False

        temp_path.replace(path)
        return True

    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def validate_qid(value: object, *, context: str) -> str:
    """Validate and return a Wikidata QID."""
    if not isinstance(value, str) or not value.startswith("Q") or not value[1:].isdigit():
        raise ValueError(f"Invalid Wikidata QID in {context}: {value!r}")
    return value


def collect_instance_of_qids(metadata_cache: dict[str, Any]) -> set[str]:
    """Collect all P31 QIDs referenced by cached Wikidata entities."""
    schema_version = metadata_cache.get("schema_version")
    if schema_version != 2:
        raise ValueError(
            f"Unsupported metadata cache schema {schema_version!r}; expected schema_version=2")

    wikidata = metadata_cache.get("wikidata")
    if not isinstance(wikidata, dict):
        raise ValueError("Metadata cache must contain a top-level 'wikidata' object")

    qids: set[str] = set()

    for entity_qid, entry in wikidata.items():
        validate_qid(entity_qid, context="metadata_cache.wikidata")

        if not isinstance(entry, dict):
            raise ValueError(f"Metadata cache Wikidata entry must be an object: {entity_qid!r}")

        if "instance_of" not in entry:
            raise ValueError(
                f"Metadata cache Wikidata entry missing required 'instance_of': {entity_qid!r}")

        instance_of = entry["instance_of"]
        if not isinstance(instance_of, list):
            raise ValueError(f"'instance_of' must be a list: metadata_cache.wikidata.{entity_qid}")

        qids.update(
            validate_qid(qid, context=f"metadata_cache.wikidata.{entity_qid}.instance_of", ) for qid
            in instance_of)

    return qids


def load_category_cache(path: Path) -> dict[str, Any]:
    """Load or initialize the local category cache."""
    cache = load_json_mapping(path, missing_ok=True)

    if not cache:
        return {"p31": {}}

    if "p31" not in cache:
        raise ValueError("Category cache missing required top-level 'p31' object")

    if not isinstance(cache["p31"], dict):
        raise ValueError("Category cache 'p31' value must be an object")

    return cache


def cached_qids(category_cache: dict[str, Any]) -> set[str]:
    """Return validated QIDs already present in the category cache."""
    return {validate_qid(qid, context="wiki_categories.p31") for qid in category_cache["p31"]}


def _retry_delay_seconds(response: requests.Response) -> float:
    """Return the server-requested retry delay, or the local conservative default."""
    retry_after = response.headers.get("Retry-After")
    if retry_after is None:
        return DEFAULT_RETRY_SECONDS

    try:
        return max(float(retry_after), 0.0)
    except ValueError:
        return DEFAULT_RETRY_SECONDS


def request_wikidata(
        qids: list[str], *, language: str, headers: dict[str, str], ) -> dict[str, Any]:
    """Fetch Wikidata labels and direct P279 claims for one QID batch."""
    params = {
        "action": "wbgetentities", "format": "json", "ids": "|".join(qids),
        "props": "labels|claims", "languages": language, "languagefallback": 1,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        response = requests.get(WIKIDATA_API_URL, params=params, headers=headers, timeout=30, )

        if response.status_code in RETRYABLE_STATUS_CODES:
            if attempt == MAX_RETRIES:
                response.raise_for_status()

            delay = _retry_delay_seconds(response)
            LOGGER.warning("Wikidata returned HTTP %s; retrying in %gs (%s/%s)",
                response.status_code, delay, attempt, MAX_RETRIES, )
            time.sleep(delay)
            continue

        response.raise_for_status()
        data = response.json()

        if not isinstance(data, dict):
            raise ValueError("Wikidata response must be a JSON object")

        return data

    raise RuntimeError("Wikidata request retry loop exited unexpectedly")


def extract_subclass_qids(entity: dict[str, Any], qid: str) -> list[str]:
    """Extract direct P279 item targets from a Wikidata entity."""
    claims = entity["claims"]
    if not isinstance(claims, dict):
        raise ValueError(f"Wikidata claims must be an object for {qid}")

    # P279 is legitimately absent for root/top-level classes.
    p279_claims = claims["P279"] if "P279" in claims else []
    if not isinstance(p279_claims, list):
        raise ValueError(f"Wikidata P279 claims must be a list for {qid}")

    parents: set[str] = set()

    for claim in p279_claims:
        if not isinstance(claim, dict):
            raise ValueError(f"Invalid P279 claim object for {qid}")

        mainsnak = claim["mainsnak"]
        if not isinstance(mainsnak, dict):
            raise ValueError(f"Invalid P279 mainsnak for {qid}")

        snaktype = mainsnak["snaktype"]
        if snaktype != "value":
            continue

        datavalue = mainsnak["datavalue"]
        if not isinstance(datavalue, dict):
            raise ValueError(f"Invalid P279 datavalue for {qid}")

        if datavalue["type"] != "wikibase-entityid":
            continue

        value = datavalue["value"]
        if not isinstance(value, dict):
            raise ValueError(f"Invalid P279 entity value for {qid}")

        if value["entity-type"] != "item":
            continue

        parents.add(validate_qid(value["id"], context=f"{qid}.P279"))

    return sorted(parents)


def extract_label(entity: dict[str, Any], qid: str, language: str) -> str | None:
    """
    Extract a human-readable label from a Wikidata entity.

    Prefer the requested language. ``wbgetentities`` is called with
    ``languagefallback=1``, so Wikidata may legitimately return a label in another
    language when the requested one is unavailable. If the entity has no returned
    label at all, preserve that as ``None``; labels are diagnostic metadata and
    must not prevent construction of the P279 class graph.
    """
    labels = entity["labels"]
    if not isinstance(labels, dict):
        raise ValueError(f"Wikidata labels must be an object for {qid}")

    if not labels:
        return None

    if language in labels:
        label_entry = labels[language]
    elif len(labels) == 1:
        label_entry = next(iter(labels.values()))
    else:
        raise ValueError(f"Wikidata entity {qid} returned multiple labels without '{language}': "
                         f"{sorted(labels)}")

    if not isinstance(label_entry, dict):
        raise ValueError(f"Invalid Wikidata label entry for {qid}")

    label = label_entry["value"]
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"Invalid Wikidata label value for {qid}")

    return label.strip()


def update_category_cache(
        *, metadata_cache: dict[str, Any], category_cache: dict[str, Any], language: str,
        headers: dict[str, str], ) -> tuple[dict[str, Any], int]:
    """
    Recursively resolve all observed P31 classes and their P279 ancestors.

    Existing cache entries are trusted and skipped. Newly discovered parent QIDs
    are added to the pending set until no unresolved classes remain.
    """
    p31_cache = category_cache["p31"]
    known = cached_qids(category_cache)
    pending = collect_instance_of_qids(metadata_cache) - known
    fetched_count = 0

    LOGGER.info("Found %s P31 IDs in metadata cache.",
        f"{len(collect_instance_of_qids(metadata_cache)):,}", )
    LOGGER.info("%s category IDs require lookup.", f"{len(pending):,}")

    while pending:
        batch = sorted(pending)[:BATCH_SIZE]
        pending.difference_update(batch)

        data = request_wikidata(batch, language=language, headers=headers, )

        entities = data["entities"]
        if not isinstance(entities, dict):
            raise ValueError("Wikidata 'entities' must be an object")

        for qid in batch:
            entity = entities[qid]
            if not isinstance(entity, dict):
                raise ValueError(f"Wikidata entity must be an object: {qid}")

            if "missing" in entity:
                p31_cache[qid] = {
                    "label": None, "subclass_of": [], "missing": True,
                }
                known.add(qid)
                LOGGER.warning("Wikidata category entity does not exist: %s", qid)
                continue

            label = extract_label(entity, qid, language)
            parents = extract_subclass_qids(entity, qid)

            if label is None:
                LOGGER.warning(
                    "%s has no returned Wikidata label; caching category without a label.", qid, )

            p31_cache[qid] = {
                "label": label, "subclass_of": parents,
            }
            known.add(qid)
            fetched_count += 1

            pending.update(parent for parent in parents if parent not in known)

        # Stay well below Wikimedia API request limits and avoid bursty traffic.
        time.sleep(REQUEST_DELAY_SECONDS)

        LOGGER.debug("Cached %s new categories; %s unresolved IDs remain.", f"{fetched_count:,}",
            f"{len(pending):,}", )

    return category_cache, fetched_count


def main() -> int:
    parser = argparse.ArgumentParser(
        description=("Build/update wiki_categories.json from P31 IDs found in the Wikidata section "
                     "of the shared Wiki metadata cache."))
    parser.add_argument("--metadata-cache", type=Path, required=True,
        help="Shared Wiki metadata JSON cache (schema v2) containing Wikidata entities", )
    parser.add_argument("--category-cache", type=Path, default=Path(DEFAULT_CATEGORY_CACHE),
        help=f"Category cache to update (default: {DEFAULT_CATEGORY_CACHE})", )
    parser.add_argument("--project", required=True,
        help="Project/application name used in the Wikimedia User-Agent", )
    parser.add_argument("--email", required=True,
        help="Contact email used in the Wikimedia User-Agent", )
    parser.add_argument("--language", default=DEFAULT_LANGUAGE,
        help=f"Wikidata label language (default: {DEFAULT_LANGUAGE})", )
    parser.add_argument("-v", "--verbose", action="store_true",
        help="Enable verbose diagnostic logging.", )
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s", )

    headers = {
        "User-Agent": f"{args.project} (Contact: {args.email})",
    }

    try:
        LOGGER.debug("Reading metadata cache from %s", args.metadata_cache)
        metadata_cache = load_json_mapping(args.metadata_cache)

        LOGGER.debug("Reading category cache from %s", args.category_cache)
        category_cache = load_category_cache(args.category_cache)

        category_cache, fetched_count = update_category_cache(metadata_cache=metadata_cache,
            category_cache=category_cache, language=args.language, headers=headers, )

        cache_changed = save_category_cache(category_cache, args.category_cache)

    except Exception as exc:
        LOGGER.error("ERROR: %s", exc)
        return 1

    LOGGER.info("Category cache updated: %s new entries, %s total.", f"{fetched_count:,}",
        f"{len(category_cache['p31']):,}", )
    if cache_changed:
        LOGGER.info("Saved: %s", args.category_cache)
    else:
        LOGGER.info("Unchanged: %s", args.category_cache)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
