#!/usr/bin/env python3
"""
Validate cached Wikipedia/Wikidata lookups and build trusted output.

Inputs:
- feature CSV
- Wikipedia/Wikidata metadata cache
- wiki_categories.json containing P31 labels and recursive P279 parents
- category-specific validation YAML

Outputs:
- *_wikipedia_enrich.csv: ACCEPT rows only; safe for unconditional downstream use
- *_wikipedia_diagnostics.csv: every attempted row with full validation context
- *_wikipedia_validated.tsv: ACCEPT rows with the full resolved Wikipedia record
  plus OSM/validation provenance; includes normalized summary plus summary_descriptive
  with item-name and common placename boilerplate removed for text analysis

This utility performs no HTTP requests and never modifies either cache.
"""

from __future__ import annotations

import argparse
from difflib import SequenceMatcher
import json
import logging
import math
from pathlib import Path
import re
import time
from typing import Any, Dict, Iterable, List, Set

from ExtractOSM.wikipedia import Wikipedia
from ExtractOSM.wikipedia_cache import (load_lookup_preprocessing, parse_wiki_tag,
                                        parse_wikidata_tag, )
import pandas as pd
import yaml

LOGGER = logging.getLogger(__name__)

WikipediaEntry = Dict[str, object]
WikidataEntry = Dict[str, object]
EARTH_RADIUS_M = 6_371_008.8
MIN_CANDIDATE_SCORE = 5.0
MIN_CANDIDATE_SCORE_MARGIN = 1.0

# TODO MOVE OUT ALL HARDCODED PARAMETERS!
STOP_WORDS = ["santa", "los", "california", "washington", "arizona", "colorado", "new", "san",
              "spanish", "capital", "county seat", "john"]


def load_json_mapping(path: Path, *, description: str) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"{description} not found: {path}")
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"{description} root must be a mapping")
    return data


def load_category_cache(cache_path: Path) -> dict[str, dict[str, object]]:
    data = load_json_mapping(cache_path, description="Wikidata category cache")
    if "p31" not in data or not isinstance(data["p31"], dict):
        raise ValueError("Wikidata category cache must contain a top-level 'p31' mapping")

    for qid, entry in data["p31"].items():
        _validate_qid(qid, f"wiki_categories.p31.{qid}")
        if not isinstance(entry, dict):
            raise ValueError(f"wiki_categories.p31.{qid} must be a mapping")
        label = entry["label"]
        parents = entry["subclass_of"]

        # A Wikidata class may legitimately have no label in the requested
        # language. Labels are diagnostic metadata; P279 relationships are the
        # authoritative data needed for validation.
        if label is not None and (not isinstance(label, str) or not label.strip()):
            raise ValueError(f"wiki_categories.p31.{qid}.label must be non-empty text or null")

        if not isinstance(parents, list):
            raise ValueError(f"wiki_categories.p31.{qid}.subclass_of must be a list")
        for parent in parents:
            _validate_qid(parent, f"wiki_categories.p31.{qid}.subclass_of")

    return data["p31"]


def _validate_qid(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.startswith("Q") or not value[1:].isdigit():
        raise ValueError(f"{path} contains invalid Wikidata QID: {value!r}")
    return value


def _load_qid_list(value: object, path: str) -> Set[str]:
    if value is None:
        return set()
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a list")
    return {_validate_qid(item, path) for item in value}


def _load_p31_rule(value: object, path: str) -> dict[str, object]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a mapping")

    sub_categories_raw = value.get("sub_categories", {})
    if not isinstance(sub_categories_raw, dict):
        raise ValueError(f"{path}.sub_categories must be a mapping")

    sub_categories: dict[str, dict[str, Set[str]]] = {}
    for sub_category, rule in sub_categories_raw.items():
        if not isinstance(rule, dict):
            raise ValueError(f"{path}.sub_categories.{sub_category} must be a mapping")
        sub_categories[str(sub_category)] = {
            "accept": _load_qid_list(rule.get("accept"),
                                     f"{path}.sub_categories.{sub_category}.accept", ),
            "reject": _load_qid_list(rule.get("reject"),
                                     f"{path}.sub_categories.{sub_category}.reject", ),
        }

    return {
        "accept": _load_qid_list(value.get("accept"), f"{path}.accept"),
        "reject": _load_qid_list(value.get("reject"), f"{path}.reject"),
        "sub_categories": sub_categories,
    }


def _load_location(value: object, path: str, inherited: float | None) -> float | None:
    if value is None:
        return inherited
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a mapping")

    if "max_distance_m" not in value:
        return inherited

    distance = value["max_distance_m"]
    if not isinstance(distance, (int, float)) or isinstance(distance, bool) or distance <= 0:
        raise ValueError(f"{path}.max_distance_m must be a positive number")
    return float(distance)


def _load_rate(value: object, path: str) -> float:
    """Load a normalized 0..1 rate from validation YAML."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{path} must be a number between 0 and 1")
    rate = float(value)
    if not 0.0 <= rate <= 1.0:
        raise ValueError(f"{path} must be between 0 and 1")
    return rate


def _load_sanity(value: object) -> dict[str, object]:
    """
    Load aggregate job-health thresholds.

    Sanity checks apply only when the number of validation rows is at least
    ``min_records``. Warnings do not fail the job. Fail thresholds are evaluated
    after output files have been written so diagnostics remain available.
    """
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("validation.sanity must be a mapping")

    min_records = value.get("min_records", 100)
    if not isinstance(min_records, int) or isinstance(min_records, bool) or min_records < 0:
        raise ValueError("validation.sanity.min_records must be a non-negative integer")

    accept_rate = value.get("accept_rate", {})
    if not isinstance(accept_rate, dict):
        raise ValueError("validation.sanity.accept_rate must be a mapping")

    uncertain_rate = value.get("uncertain_rate", {})
    if not isinstance(uncertain_rate, dict):
        raise ValueError("validation.sanity.uncertain_rate must be a mapping")

    warn_accept = _load_rate(accept_rate.get("warn_below", 0.20),
                             "validation.sanity.accept_rate.warn_below", )
    fail_accept = _load_rate(accept_rate.get("fail_below", 0.01),
                             "validation.sanity.accept_rate.fail_below", )
    warn_uncertain = _load_rate(uncertain_rate.get("warn_above", 0.25),
                                "validation.sanity.uncertain_rate.warn_above", )
    fail_uncertain = _load_rate(uncertain_rate.get("fail_above", 0.75),
                                "validation.sanity.uncertain_rate.fail_above", )

    if fail_accept > warn_accept:
        raise ValueError("validation.sanity.accept_rate.fail_below must be <= warn_below")
    if fail_uncertain < warn_uncertain:
        raise ValueError("validation.sanity.uncertain_rate.fail_above must be >= warn_above")

    return {
        "min_records": min_records, "accept_rate": {
            "warn_below": warn_accept, "fail_below": fail_accept,
        }, "uncertain_rate": {
            "warn_above": warn_uncertain, "fail_above": fail_uncertain,
        },
    }


def load_validation_config(
        config_path: Path, map_category: str, ) -> dict[str, object]:
    """Load defaults plus the selected map category's validation policy.

    Args:
        config_path: Path to the YAML validation configuration file.
        map_category: Map category whose validation rules should be loaded.

    Returns:
        Resolved validation policy with defaults merged into the selected
        category.

    Raises:
        FileNotFoundError: If the validation configuration file does not exist.
        ValueError: If the validation configuration is missing required
            sections or contains invalid values.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Validation config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}

    update_config_msg = (f"Please update the validation config file: {config_path}")

    if not isinstance(data, dict):
        raise ValueError("Validation config must contain named sections at the top level, "
                         "such as 'validation:'. "
                         f"{update_config_msg}")

    validation = data.get("validation")
    if not isinstance(validation, dict):
        raise ValueError("Validation config is missing a valid top-level 'validation:' "
                         "section containing the validation settings. "
                         f"{update_config_msg}")

    defaults = validation.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("'validation.defaults' must contain named validation settings, "
                         "not a list or single value. "
                         f"{update_config_msg}")

    sanity = _load_sanity(validation.get("sanity"))

    categories = validation.get("categories")
    if not isinstance(categories, dict):
        raise ValueError("'validation.categories' must contain named map categories and "
                         "their validation rules. "
                         f"{update_config_msg}")

    if map_category not in categories:
        raise ValueError(f"No validation rules are configured for map category "
                         f"'{map_category}'. Add a '{map_category}:' section under "
                         "'validation.categories'. "
                         f"{update_config_msg}")

    category_raw = categories[map_category]
    if not isinstance(category_raw, dict):
        raise ValueError(f"Validation rules for map category '{map_category}' must contain "
                         "named settings, not a list or single value. "
                         f"Expected location: validation.categories.{map_category}. "
                         f"{update_config_msg}")

    default_p31 = _load_p31_rule(defaults.get("p31"), "validation.defaults.p31", )
    category_p31 = _load_p31_rule(category_raw.get("p31"),
                                  f"validation.categories.{map_category}.p31", )

    default_distance = _load_location(defaults.get("location"), "validation.defaults.location",
                                      None, )
    max_distance_m = _load_location(category_raw.get("location"),
                                    f"validation.categories.{map_category}.location",
                                    default_distance, )

    if max_distance_m is None:
        raise ValueError(f"No maximum location distance is configured for map category "
                         f"'{map_category}'. Set 'location.max_distance_m' under "
                         f"'validation.categories.{map_category}', or provide a default "
                         "under 'validation.defaults.location'. "
                         f"{update_config_msg}")

    sub_categories = dict(default_p31["sub_categories"])
    for name, rule in category_p31["sub_categories"].items():
        inherited = sub_categories.get(name, {"accept": set(), "reject": set()}, )
        sub_categories[name] = {
            "accept": set(inherited["accept"]) | set(rule["accept"]),
            "reject": set(inherited["reject"]) | set(rule["reject"]),
        }

    return {
        "map_category": map_category,
        "accept": (set(default_p31["accept"]) | set(category_p31["accept"])),
        "reject": (set(default_p31["reject"]) | set(category_p31["reject"])),
        "sub_categories": sub_categories, "location": {
            "max_distance_m": max_distance_m,
        }, "sanity": sanity,
    }


def _coerce_coordinate(value) -> float | None:
    """Return a finite coordinate value or None when source data is unusable."""
    if pd.isna(value):
        return None
    try:
        coordinate = float(value)
    except (TypeError, ValueError):
        return None
    return coordinate if math.isfinite(coordinate) else None


def calculate_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_rad, lon1_rad = math.radians(lat1), math.radians(lon1)
    lat2_rad, lon2_rad = math.radians(lat2), math.radians(lon2)
    delta_lat = lat2_rad - lat1_rad
    delta_lon = lon2_rad - lon1_rad

    haversine = (
            math.sin(delta_lat / 2.0) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(
        delta_lon / 2.0) ** 2)
    central_angle = 2.0 * math.asin(min(1.0, math.sqrt(haversine)))
    return EARTH_RADIUS_M * central_angle


from typing import Tuple

bbox = {
    "min_lat": 24.0,  # Southern buffer past the US-Mexico border
    "max_lat": 50.0,  # Northern buffer just past the 49th parallel (US-Canada border)
    "min_lon": -125.0,  # Pacific coastline + coastal islands buffer
    "max_lon": -98.0,  # Eastern border
}


def validate_location(
        osm_lat: float | None, osm_lon: float | None, location_lat: float | None,
        location_lon: float | None, location_source: str, location_config: dict[str, object], ) -> \
Tuple[str, str, float | None]:
    source_label = "Wikipedia" if location_source == "wikipedia" else "Wikidata"

    # Reject if reference coordinates fall outside the bounding box
    if location_lat is not None and location_lon is not None:
        # Use bbox from location_config if provided, else fallback to module-level bbox
        target_bbox = (
            location_config["bbox"] if isinstance(location_config.get("bbox"), dict) else bbox)
        is_outside = not (
                target_bbox["min_lat"] <= location_lat <= target_bbox["max_lat"] and target_bbox[
            "min_lon"] <= location_lon <= target_bbox["max_lon"])
        if is_outside:
            return ("REJECT",
                    f"{source_label} location ({location_lat:.4f}, {location_lon:.4f}) is outside "
                    f"BBOX",
                    None,)

    if osm_lat is None or osm_lon is None:
        return ("PASS", "no OSM coordinates", 0.0)

    if location_lat is None or location_lon is None:
        return ("NO_REFERENCE_COORDINATES", "Wikipedia/Wikidata coordinates unavailable", None,)

    distance_m = calculate_distance_m(osm_lat, osm_lon, location_lat, location_lon)
    max_distance_m = float(location_config["max_distance_m"])

    if distance_m > max_distance_m:
        return ("REJECT", f"{source_label} location is {distance_m:.0f} m away "
                          f"(maximum {max_distance_m:.0f} m)", distance_m,)

    return ("PASS", f"{source_label} location is {distance_m:.0f} m away", distance_m)


def validate_collected_data(
        wikipedia_entry: WikipediaEntry, wikidata_entry: WikidataEntry | None, ) -> Tuple[
    bool, str]:
    """Check raw cache completeness required for trusted enrichment."""
    if wikipedia_entry["missing"]:
        return False, "Wikipedia page missing"
    if wikipedia_entry["disambiguation"]:
        return False, "Wikipedia disambiguation page"

    resolved_title = wikipedia_entry["resolved_title"]
    wikipedia_length = wikipedia_entry["wikipedia_length"]
    wikipedia_links = wikipedia_entry["wikipedia_links"]
    wikidata_id = wikipedia_entry["wikidata_id"]

    if not isinstance(resolved_title, str) or not resolved_title.strip():
        return False, "Resolved Wikipedia title missing"
    if not isinstance(wikipedia_length, int) or wikipedia_length <= 0:
        return False, "Wikipedia article length missing or invalid"
    if not isinstance(wikipedia_links, int) or wikipedia_links < 0:
        return False, "Wikipedia incoming link count missing or invalid"
    if not isinstance(wikidata_id, str) or not wikidata_id.startswith("Q"):
        return False, "Wikidata ID missing or invalid"
    if wikidata_entry is None:
        return False, "Wikidata entity missing from cache"

    instance_of = wikidata_entry.get("instance_of")
    sitelink_count = wikidata_entry.get("wikidata_sitelink_count")
    if not isinstance(instance_of, list):
        return False, "Wikidata P31 data invalid"
    if not isinstance(sitelink_count, int) or sitelink_count < 0:
        return False, "Wikidata sitelink count missing or invalid"

    return True, "Collected data valid"


def _category_entry(category_cache: dict[str, dict[str, object]], qid: str) -> dict[str, object]:
    if qid not in category_cache:
        raise ValueError(f"Wikidata category cache is incomplete: {qid} is missing. "
                         "Run wiki_categories.py before validation.")
    return category_cache[qid]


def expand_category_graph(
        direct_qids: Iterable[str], category_cache: dict[str, dict[str, object]], ) -> Tuple[
    Set[str], Set[str]]:
    """Return (all reachable QIDs, ancestors-only QIDs) through recursive P279."""
    direct = {_validate_qid(qid, "instance_of") for qid in direct_qids}
    visited: Set[str] = set()
    stack = list(direct)

    while stack:
        qid = stack.pop()
        if qid in visited:
            continue
        entry = _category_entry(category_cache, qid)
        visited.add(qid)
        for parent in entry["subclass_of"]:
            parent_qid = _validate_qid(parent, f"wiki_categories.p31.{qid}.subclass_of")
            _category_entry(category_cache, parent_qid)
            if parent_qid not in visited:
                stack.append(parent_qid)

    return visited, visited - direct


def validate_category(
        instance_of: List[str], sub_category: str, rules: dict[str, object],
        category_cache: dict[str, dict[str, object]], ) -> Tuple[
    str, str, List[str], List[str], Set[str], Set[str]]:
    """Validate direct P31 values plus their recursive P279 ancestors."""
    if not instance_of:
        return "UNCERTAIN", "No P31 category values", [], [], set(), set()

    expanded, ancestors = expand_category_graph(instance_of, category_cache)

    accept_qids = set(rules["accept"])
    reject_qids = set(rules["reject"])
    sub_rule = rules["sub_categories"].get(sub_category)
    if sub_rule:
        accept_qids.update(sub_rule["accept"])
        reject_qids.update(sub_rule["reject"])

    reject_matches = sorted(expanded & reject_qids)
    accept_matches = sorted(expanded & accept_qids)

    if reject_matches:
        return ("REJECT", f"Category rejected by {', '.join(reject_matches)}", accept_matches,
                reject_matches, expanded, ancestors,)
    if accept_matches:
        return ("ACCEPT", f"Category accepted by {', '.join(accept_matches)}", accept_matches,
                reject_matches, expanded, ancestors,)
    return ("UNCERTAIN", f"No configured category rule matched {', '.join(sorted(expanded))}", [],
            [], expanded, ancestors,)


def _qid_label(category_cache: dict[str, dict[str, object]], qid: str) -> str:
    """Return a readable diagnostic label without requiring one to exist."""
    label = _category_entry(category_cache, qid)["label"]
    return str(label) if label is not None else "<unlabeled>"


def _format_qid_labels(
        qids: Iterable[str], category_cache: dict[str, dict[str, object]]
) -> str:
    return "|".join(f"{qid}: {_qid_label(category_cache, qid)}" for qid in sorted(set(qids)))


def _policy_qid_label(
        category_cache: dict[str, dict[str, object]], qid: str, ) -> str:
    """Return a readable policy label without requiring the QID to be cached."""
    entry = category_cache.get(qid)
    if entry is None:
        return "<not cached>"

    label = entry["label"]
    return str(label) if label is not None else "<unlabeled>"


def log_validation_policy(
        rules: dict[str, object], category_cache: dict[str, dict[str, object]], ) -> None:
    """Log the effective validation policy for the selected map category."""
    LOGGER.debug("Validation policy for category %r:", rules["map_category"])

    accept_qids = sorted(rules["accept"])
    reject_qids = sorted(rules["reject"])

    LOGGER.debug("  Accept:")
    if accept_qids:
        for qid in accept_qids:
            LOGGER.debug("    %-12s %s", qid, _policy_qid_label(category_cache, qid))
    else:
        LOGGER.debug("    (none)")

    LOGGER.debug("  Reject:")
    if reject_qids:
        for qid in reject_qids:
            LOGGER.debug("    %-12s %s", qid, _policy_qid_label(category_cache, qid))
    else:
        LOGGER.debug("    (none)")

    sub_categories = rules["sub_categories"]
    if sub_categories:
        LOGGER.debug("  Sub-category rules:")
        for sub_category in sorted(sub_categories):
            rule = sub_categories[sub_category]
            LOGGER.debug("    %s:", sub_category)

            sub_accept = sorted(rule["accept"])
            if sub_accept:
                LOGGER.debug("      Accept:")
                for qid in sub_accept:
                    LOGGER.debug("        %-10s %s", qid, _policy_qid_label(category_cache, qid))

            sub_reject = sorted(rule["reject"])
            if sub_reject:
                LOGGER.debug("      Reject:")
                for qid in sub_reject:
                    LOGGER.debug("        %-10s %s", qid, _policy_qid_label(category_cache, qid))

            if not sub_accept and not sub_reject:
                LOGGER.debug("      (no additional rules)")

    LOGGER.debug("  Location: max distance %.0f m", float(rules["location"]["max_distance_m"]), )

    sanity = rules["sanity"]
    LOGGER.debug("  Sanity: min_records=%s, accept warn<%.0f%%, fail<%.0f%%, "
                 "uncertain warn>%.0f%%, fail>%.0f%%", sanity["min_records"],
        float(sanity["accept_rate"]["warn_below"]) * 100,
        float(sanity["accept_rate"]["fail_below"]) * 100,
        float(sanity["uncertain_rate"]["warn_above"]) * 100,
        float(sanity["uncertain_rate"]["fail_above"]) * 100, )


def _normalize_name(value: str) -> str:
    """Normalize a feature/article name for disambiguation comparison."""
    value = re.sub(r"\s*\([^)]*\)\s*$", "", value.casefold())
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _name_score(item_name: str, candidate_title: str) -> float:
    """Return a small semantic-name score used only for disambiguation."""
    item = _normalize_name(item_name)
    candidate = _normalize_name(candidate_title)
    if not item or not candidate:
        return 0.0
    if item == candidate:
        return 3.0
    if candidate.startswith(item) or item.startswith(candidate):
        return 2.0

    ratio = SequenceMatcher(None, item, candidate).ratio()
    if ratio >= 0.90:
        return 1.5
    if ratio >= 0.75:
        return 1.0
    return 0.0


def _source_coordinates(
        row, lat_col: str, lon_col: str, ) -> tuple[float | None, float | None]:
    """
    Read source coordinates.

    The current enrichment pipeline may emit 0/0 before polygon/line representative
    coordinates are merged. Treat that pair as unavailable rather than as a real
    location signal.
    """
    lat = _coerce_coordinate(row[lat_col])
    lon = _coerce_coordinate(row[lon_col])
    if lat == 0.0 and lon == 0.0:
        return None, None
    return lat, lon


def _feature_id(value: object) -> str:
    """Return a usable feature identifier or an empty string for missing data.

    Args:
        value: Raw identifier value read by pandas.

    Returns:
        A stripped identifier, or an empty string when the source value is null.
    """
    return "" if pd.isna(value) else str(value).strip()


def _article_context(
        wikipedia_entry: WikipediaEntry, wikipedia: Wikipedia, ) -> dict[str, object]:
    """Join one Wikipedia page entry to its cached Wikidata entity."""
    wikidata_id = str(wikipedia_entry.get("wikidata_id") or "")
    wikidata_entry = wikipedia.cached_wikidata(wikidata_id) if wikidata_id else None

    wikipedia_lat = _coerce_coordinate(wikipedia_entry.get("wikipedia_lat"))
    wikipedia_lon = _coerce_coordinate(wikipedia_entry.get("wikipedia_lon"))

    wikidata_lat = None
    wikidata_lon = None
    if wikidata_entry is not None:
        wikidata_lat = _coerce_coordinate(wikidata_entry.get("wikidata_lat"))
        wikidata_lon = _coerce_coordinate(wikidata_entry.get("wikidata_lon"))

    location_lat = None
    location_lon = None
    location_source = ""
    if wikipedia_lat is not None and wikipedia_lon is not None:
        location_lat = wikipedia_lat
        location_lon = wikipedia_lon
        location_source = "wikipedia"
    elif wikidata_lat is not None and wikidata_lon is not None:
        location_lat = wikidata_lat
        location_lon = wikidata_lon
        location_source = "wikidata"

    direct_qids = []
    sitelink_count = None
    enwiki_title = ""
    if wikidata_entry is not None:
        direct_qids = list(wikidata_entry.get("instance_of") or [])
        sitelink_count = wikidata_entry.get("wikidata_sitelink_count")
        enwiki_title = str(wikidata_entry.get("enwiki_title") or "")

    return {
        "wikidata_id": wikidata_id, "wikidata_entry": wikidata_entry, "direct_qids": direct_qids,
        "wikidata_sitelink_count": sitelink_count, "enwiki_title": enwiki_title,
        "wikipedia_lat": wikipedia_lat, "wikipedia_lon": wikipedia_lon,
        "wikidata_lat": wikidata_lat, "wikidata_lon": wikidata_lon, "location_lat": location_lat,
        "location_lon": location_lon, "location_source": location_source,
    }


def _evaluate_article_candidate(
        *, cache_key: str, wikipedia_entry: WikipediaEntry, source_wikidata_id: str, item_name: str,
        sub_category: str, osm_lat: float | None, osm_lon: float | None, rules: dict[str, object],
        category_cache: dict[str, dict[str, object]], wikipedia: Wikipedia,
) -> \
dict[str, object]:
    """Validate and score one non-disambiguation Wikipedia candidate."""
    context = _article_context(wikipedia_entry, wikipedia)
    wikidata_entry = context["wikidata_entry"]

    data_valid, data_reason = validate_collected_data(wikipedia_entry,
        wikidata_entry if isinstance(wikidata_entry, dict) else None, )

    category_state = "NOT_EVALUATED"
    category_reason = ""
    accept_matches: List[str] = []
    reject_matches: List[str] = []
    expanded_qids: Set[str] = set()
    ancestor_qids: Set[str] = set()

    location_state = "NOT_EVALUATED"
    location_reason = ""
    distance_m = None

    if data_valid:
        (category_state, category_reason, accept_matches, reject_matches, expanded_qids,
         ancestor_qids,) = validate_category(list(context["direct_qids"]), sub_category, rules,
            category_cache, )

        location_state, location_reason, distance_m = validate_location(osm_lat, osm_lon,
            context["location_lat"], context["location_lon"], str(context["location_source"]),
            rules["location"], )

    exact_identity = bool(
        source_wikidata_id and context["wikidata_id"] and source_wikidata_id == context[
            "wikidata_id"])

    name_score = _name_score(item_name, str(wikipedia_entry["resolved_title"]))

    score = name_score
    if category_state == "ACCEPT":
        score += 4.0
    elif category_state == "REJECT":
        score -= 100.0

    if location_state == "PASS":
        score += 3.0
    elif location_state == "REJECT":
        score -= 100.0

    if exact_identity:
        score += 1000.0

    return {
        "cache_key": cache_key, "wikipedia_entry": wikipedia_entry, **context,
        "data_valid": data_valid, "data_reason": data_reason, "category_state": category_state,
        "category_reason": category_reason, "accept_matches": accept_matches,
        "reject_matches": reject_matches, "expanded_qids": expanded_qids,
        "ancestor_qids": ancestor_qids, "location_state": location_state,
        "location_reason": location_reason, "distance_m": distance_m, "name_score": name_score,
        "score": score, "exact_identity": exact_identity,
    }


def _candidate_title(candidate: dict[str, object]) -> str:
    """Return a diagnostic title for an evaluated article candidate."""
    entry = candidate.get("wikipedia_entry")
    if isinstance(entry, dict):
        return str(entry.get("resolved_title") or candidate.get("cache_key") or "")
    return str(candidate.get("cache_key") or "")


def _candidate_failure(
        *, candidate_kind: str, listed_count: int, evaluated_candidates: list[dict[str, object]],
        viable_candidates: list[dict[str, object]], is_search: bool, ) -> tuple[str, str]:
    """Classify and explain why candidate resolution selected no article.

    Args:
        candidate_kind: Human-readable candidate source, such as ``search``.
        listed_count: Number of candidate titles attached to the source entry.
        evaluated_candidates: Candidates with usable ordinary article metadata.
        viable_candidates: Candidates remaining after evidence rejection.
        is_search: Whether name evidence is mandatory for this candidate source.

    Returns:
        A stable failure code and a compact diagnostic description.
    """
    code_prefix = candidate_kind.upper()
    if listed_count == 0:
        failure_code = "PAGE_MISSING" if is_search else f"{code_prefix}_NO_CANDIDATES"
        return (failure_code, f"No {candidate_kind} candidates were found",)
    if not evaluated_candidates:
        return f"{code_prefix}_METADATA_MISSING", (
            f"Found {listed_count} {candidate_kind} candidate title(s), but none "
            "had cached ordinary-article metadata")

    if not viable_candidates:
        reason_counts = {
            "incomplete metadata": sum(
                not bool(candidate["data_valid"]) for candidate in evaluated_candidates),
            "weak name match": sum(
                is_search and float(candidate["name_score"]) <= 0.0 for candidate in
                evaluated_candidates), "category rejected": sum(
                candidate["category_state"] == "REJECT" for candidate in evaluated_candidates),
            "location rejected": sum(
                candidate["location_state"] == "REJECT" for candidate in evaluated_candidates),
        }
        details = ", ".join(f"{count} {reason}" for reason, count in reason_counts.items() if count)
        evaluated_count = len(evaluated_candidates)
        if reason_counts["incomplete metadata"] == evaluated_count:
            failure_code = f"{code_prefix}_INCOMPLETE_METADATA"
        elif reason_counts["weak name match"] == evaluated_count:
            failure_code = f"{code_prefix}_WEAK_NAME"
        elif reason_counts["category rejected"] == evaluated_count:
            failure_code = f"{code_prefix}_CATEGORY_REJECT"
        elif reason_counts["location rejected"] == evaluated_count:
            failure_code = f"{code_prefix}_LOCATION_REJECT"
        else:
            failure_code = f"{code_prefix}_MULTIPLE_EVIDENCE_FAILURES"
        return failure_code, (
            f"Evaluated {len(evaluated_candidates)} {candidate_kind} candidate(s); "
            f"{details or 'all failed evidence checks'}")

    winner = viable_candidates[0]
    winner_score = float(winner["score"])
    winner_title = _candidate_title(winner)
    if winner_score < MIN_CANDIDATE_SCORE:
        return f"{code_prefix}_LOW_SCORE", (
            f"Best {candidate_kind} candidate {winner_title!r} scored "
            f"{winner_score:.1f}; requires {MIN_CANDIDATE_SCORE:.1f}")

    runner_up = viable_candidates[1] if len(viable_candidates) > 1 else None
    if runner_up is not None:
        runner_up_score = float(runner_up["score"])
        margin = winner_score - runner_up_score
        if margin < MIN_CANDIDATE_SCORE_MARGIN:
            return f"{code_prefix}_AMBIGUOUS", (
                f"Ambiguous {candidate_kind} candidates: {winner_title!r} scored "
                f"{winner_score:.1f}, {_candidate_title(runner_up)!r} scored "
                f"{runner_up_score:.1f}; margin {margin:.1f} is below "
                f"{MIN_CANDIDATE_SCORE_MARGIN:.1f}")

    return (f"{code_prefix}_RULE_FAILURE",
            f"No {candidate_kind} candidate satisfied the resolution rules",)


def _resolve_feature_article(
        *, parsed: Tuple[str, str] | None, requested_lang: str, source_wikidata_id: str,
        item_name: str, sub_category: str, osm_lat: float | None, osm_lon: float | None,
        rules: dict[str, object], category_cache: dict[str, dict[str, object]],
        wikipedia: Wikipedia, ) -> \
dict[str, object]:
    """
    Resolve one OSM feature to the best cached Wikipedia article.

    Resolution priority:
      1. OSM Wikidata QID -> enwiki sitelink.
      2. Direct/fallback Wikipedia title when it is an ordinary article.
      3. Candidate resolution for disambiguation pages or missing-title searches.
    """
    lookup_title = parsed[1] if parsed else ""
    source_cache_key = ""
    source_entry: WikipediaEntry | None = None

    if parsed and parsed[0] == requested_lang:
        source_cache_key, source_entry = wikipedia.cached_wikipedia(requested_lang, parsed[1])

    # Strongest identity: an OSM-provided QID with an English Wikipedia sitelink.
    if source_wikidata_id:
        qid_cache_key, qid_page = wikipedia.cached_wikipedia_for_wikidata(
            source_wikidata_id, lang=requested_lang,
        )
        if (qid_page is not None and not qid_page["missing"] and not qid_page[
                "disambiguation"]):
            evaluated = _evaluate_article_candidate(cache_key=qid_cache_key,
                wikipedia_entry=qid_page, source_wikidata_id=source_wikidata_id,
                item_name=item_name, sub_category=sub_category, osm_lat=osm_lat,
                osm_lon=osm_lon, rules=rules, category_cache=category_cache,
                wikipedia=wikipedia, )
            evaluated.update({
                "lookup_title": lookup_title, "source_cache_key": source_cache_key,
                "source_entry": source_entry, "resolution_method": "WIKIDATA_SITELINK",
                "resolution_failure": "",
                "resolution_reason": "OSM Wikidata QID resolved through its enwiki "
                                     "sitelink",
                "candidate_count": 1, "resolution_score": evaluated["score"],
            })
            return evaluated

    # Ordinary direct/fallback Wikipedia page.
    if (source_entry is not None and not source_entry["missing"] and not source_entry[
        "disambiguation"]):
        evaluated = _evaluate_article_candidate(cache_key=source_cache_key,
            wikipedia_entry=source_entry, source_wikidata_id=source_wikidata_id,
            item_name=item_name, sub_category=sub_category, osm_lat=osm_lat, osm_lon=osm_lon,
            rules=rules, category_cache=category_cache, wikipedia=wikipedia, )
        evaluated.update({
            "lookup_title": lookup_title, "source_cache_key": source_cache_key,
            "source_entry": source_entry, "resolution_method": "DIRECT_WIKIPEDIA",
            "resolution_failure": "",
            "resolution_reason": "Direct/fallback Wikipedia lookup resolved to an ordinary article",
            "candidate_count": 1, "resolution_score": evaluated["score"],
        })
        return evaluated

    # Search candidates remain separate from the missing exact-title entry.
    if source_entry is not None and (source_entry["disambiguation"] or (
            source_entry["missing"] and source_entry.get("search_complete"))):
        is_search = bool(source_entry["missing"])
        method_prefix = "SEARCH" if is_search else "DISAMBIGUATION"
        candidate_kind = "search" if is_search else "disambiguation"
        candidate_field = "search_candidates" if is_search else "disambiguation_links"
        candidate_titles = source_entry.get(candidate_field) or []
        if not isinstance(candidate_titles, list):
            raise ValueError(f"{candidate_field} must be a list: {source_cache_key}")

        evaluated_candidates: list[dict[str, object]] = []
        seen_articles: set[str] = set()
        for candidate_title in candidate_titles:
            candidate_key, candidate_page = wikipedia.cached_wikipedia(
                requested_lang, str(candidate_title),
            )
            if (candidate_page is None or candidate_page["missing"] or candidate_page[
                "disambiguation"]):
                continue

            identity = str(candidate_page.get("wikidata_id") or candidate_page["resolved_title"])
            if identity in seen_articles:
                continue
            seen_articles.add(identity)

            evaluated_candidates.append(
                _evaluate_article_candidate(cache_key=candidate_key, wikipedia_entry=candidate_page,
                    source_wikidata_id=source_wikidata_id, item_name=item_name,
                    sub_category=sub_category, osm_lat=osm_lat, osm_lon=osm_lon, rules=rules,
                    category_cache=category_cache, wikipedia=wikipedia, ))

        # Exact QID identity is decisive.
        exact_matches = [candidate for candidate in evaluated_candidates if
                         candidate["exact_identity"]]
        if len(exact_matches) == 1:
            winner = exact_matches[0]
            winner.update({
                "lookup_title": lookup_title, "source_cache_key": source_cache_key,
                "source_entry": source_entry, "resolution_method": f"{method_prefix}_WIKIDATA",
                "resolution_failure": "",
                "resolution_reason": f"{candidate_kind.capitalize()} candidate Wikidata QID "
                                     f"matches OSM Wikidata QID",
                "candidate_count": len(evaluated_candidates), "resolution_score": winner["score"],
            })
            return winner

        viable = [candidate for candidate in evaluated_candidates if
            candidate["data_valid"] and (not is_search or candidate["name_score"] > 0) and
            candidate["category_state"] != "REJECT" and candidate["location_state"] != "REJECT"]
        viable.sort(key=lambda candidate: float(candidate["score"]), reverse=True)

        if viable:
            winner = viable[0]
            runner_up_score = float(viable[1]["score"]) if len(viable) > 1 else None
            winner_score = float(winner["score"])
            margin = (
                winner_score - runner_up_score if runner_up_score is not None else winner_score)

            # A candidate must have substantive evidence, and when another viable
            # candidate exists it must win by a meaningful margin.
            if (winner_score >= MIN_CANDIDATE_SCORE and (
                    runner_up_score is None or margin >= MIN_CANDIDATE_SCORE_MARGIN)):
                winner.update({
                    "lookup_title": lookup_title, "source_cache_key": source_cache_key,
                    "source_entry": source_entry, "resolution_method": f"{method_prefix}_RESOLVED",
                    "resolution_failure": "", "resolution_reason": (
                            f"Best {candidate_kind} candidate score {winner_score:.1f}" + (
                        f", margin {margin:.1f}" if runner_up_score is not None else "")),
                    "candidate_count": len(evaluated_candidates), "resolution_score": winner_score,
                })
                return winner

        failure_code, failure_reason = _candidate_failure(candidate_kind=candidate_kind,
            listed_count=len(candidate_titles), evaluated_candidates=evaluated_candidates,
            viable_candidates=viable, is_search=is_search, )
        return {
            "lookup_title": lookup_title, "source_cache_key": source_cache_key,
            "source_entry": source_entry, "cache_key": "", "wikipedia_entry": None,
            "wikidata_entry": None, "wikidata_id": "", "direct_qids": [],
            "wikidata_sitelink_count": None, "enwiki_title": "", "wikipedia_lat": None,
            "wikipedia_lon": None, "wikidata_lat": None, "wikidata_lon": None, "location_lat": None,
            "location_lon": None, "location_source": "", "data_valid": False, "data_reason": "",
            "category_state": "NOT_EVALUATED", "category_reason": "", "accept_matches": [],
            "reject_matches": [], "expanded_qids": set(), "ancestor_qids": set(),
            "location_state": "NOT_EVALUATED", "location_reason": "", "distance_m": None,
            "name_score": 0.0, "score": 0.0, "exact_identity": False,
            "resolution_method": f"{method_prefix}_UNRESOLVED", "resolution_failure": failure_code,
            "resolution_reason": failure_reason, "candidate_count": len(evaluated_candidates),
            "resolution_score": None,
        }

    # Missing/unavailable source lookup.
    return {
        "lookup_title": lookup_title, "source_cache_key": source_cache_key,
        "source_entry": source_entry, "cache_key": "", "wikipedia_entry": None,
        "wikidata_entry": None, "wikidata_id": "", "direct_qids": [],
        "wikidata_sitelink_count": None, "enwiki_title": "", "wikipedia_lat": None,
        "wikipedia_lon": None, "wikidata_lat": None, "wikidata_lon": None, "location_lat": None,
        "location_lon": None, "location_source": "", "data_valid": False,
        "data_reason": "No usable Wikipedia article resolved", "category_state": "NOT_EVALUATED",
        "category_reason": "", "accept_matches": [], "reject_matches": [], "expanded_qids": set(),
        "ancestor_qids": set(), "location_state": "NOT_EVALUATED", "location_reason": "",
        "distance_m": None, "name_score": 0.0, "score": 0.0, "exact_identity": False,
        "resolution_method": "UNRESOLVED", "resolution_failure": "NO_USABLE_ARTICLE",
        "resolution_reason": "No usable Wikipedia article resolved", "candidate_count": 0,
        "resolution_score": None,
    }


def _reason_code(
        *, parsed: Tuple[str, str] | None, requested_lang: str, resolution: dict[str, object],
) -> \
Tuple[str, str]:
    """
    Convert a resolved article plus validation evidence into the final row state.

    An exact OSM Wikidata QID match is authoritative identity evidence. Once the
    resolved Wikipedia article itself has complete/valid ranking metadata, category
    and location checks remain useful diagnostics but must not block enrichment.
    """
    source_entry = resolution.get("source_entry")
    wikipedia_entry = resolution.get("wikipedia_entry")

    if not parsed:
        return "UNCERTAIN", "NO_LOOKUP"
    if parsed[0] != requested_lang:
        return "UNCERTAIN", "LANGUAGE_MISMATCH"

    method = str(resolution.get("resolution_method") or "")
    if method in {"DISAMBIGUATION_UNRESOLVED", "SEARCH_UNRESOLVED"}:
        failure_code = str(resolution.get("resolution_failure") or method)
        if failure_code == "PAGE_MISSING":
            return "REJECT", failure_code
        return "UNCERTAIN", failure_code

    if wikipedia_entry is None:
        if isinstance(source_entry, dict) and source_entry.get("missing"):
            return "REJECT", "PAGE_MISSING"
        if source_entry is None:
            return "UNCERTAIN", "CACHE_MISSING"
        return "UNCERTAIN", "UNRESOLVED"

    if not resolution["data_valid"]:
        return "UNCERTAIN", "INCOMPLETE_DATA"

    # Exact identity is stronger evidence than category/location validation.
    # Those checks are still calculated and emitted in diagnostics so unexpected
    # mismatches remain visible for data-quality investigation.
    if bool(resolution.get("exact_identity")):
        return "ACCEPT", "ACCEPT_WIKIDATA_IDENTITY"

    if resolution["category_state"] == "REJECT":
        return "REJECT", "CATEGORY_REJECT"
    if resolution["location_state"] == "REJECT":
        return "REJECT", "LOCATION_TOO_FAR"
    if resolution["category_state"] != "ACCEPT":
        return "UNCERTAIN", "CATEGORY_NOT_MATCHED"
    if resolution["location_state"] == "NO_OSM_COORDINATES":
        return "UNCERTAIN", "NO_OSM_COORDINATES"
    if resolution["location_state"] == "NO_REFERENCE_COORDINATES":
        return "UNCERTAIN", "NO_REFERENCE_COORDINATES"
    if resolution["location_state"] != "PASS":
        return "UNCERTAIN", "LOCATION_UNKNOWN"
    return "ACCEPT", "ACCEPT"


def evaluate_job_sanity(
        counts: dict[str, int], sanity: dict[str, object], ) -> tuple[list[str], list[str]]:
    """
    Return (warnings, failures) for aggregate validation results.

    Per-row REJECT/UNCERTAIN states are normal validation outcomes. These checks
    look only for aggregate distributions that strongly suggest broken or
    incomplete validation policy.
    """
    total = sum(counts.values())
    min_records = int(sanity["min_records"])
    if total < min_records:
        return [], []

    accept_rate = counts["ACCEPT"] / total if total else 0.0
    uncertain_rate = counts["UNCERTAIN"] / total if total else 0.0

    warnings: list[str] = []
    failures: list[str] = []

    accept_cfg = sanity["accept_rate"]
    uncertain_cfg = sanity["uncertain_rate"]

    if accept_rate < float(accept_cfg["fail_below"]):
        failures.append(f"Acceptance rate {accept_rate:.2%} is below fail threshold "
                        f"{float(accept_cfg['fail_below']):.2%}")
    elif accept_rate < float(accept_cfg["warn_below"]):
        warnings.append(f"Acceptance rate {accept_rate:.2%} is below warning threshold "
                        f"{float(accept_cfg['warn_below']):.2%}")

    if uncertain_rate > float(uncertain_cfg["fail_above"]):
        failures.append(f"Uncertain rate {uncertain_rate:.2%} exceeds fail threshold "
                        f"{float(uncertain_cfg['fail_above']):.2%}")
    elif uncertain_rate > float(uncertain_cfg["warn_above"]):
        warnings.append(f"Uncertain rate {uncertain_rate:.2%} exceeds warning threshold "
                        f"{float(uncertain_cfg['warn_above']):.2%}")

    return warnings, failures


def save_unmatched_category_breakdown(
        diagnostic_df: pd.DataFrame, category_cache: dict[str, dict[str, object]],
        output_path: Path, ) -> None:
    """Save direct Wikidata P31 categories not covered by validation rules.

    Only rows whose validation result is ``CATEGORY_NOT_MATCHED`` are included.
    Each direct P31 value is counted once per feature. The resulting CSV is
    intended to help build or extend validation policy for a map category.

    Args:
        diagnostic_df: Full validation diagnostics.
        category_cache: Wikidata category metadata keyed by QID.
        output_path: Destination CSV path.
    """
    unmatched_df = diagnostic_df[diagnostic_df["validation_reason"] == "CATEGORY_NOT_MATCHED"]

    p31_ids = (unmatched_df["p31_ids"].dropna().astype(str).str.split("|").explode().str.strip())
    p31_ids = p31_ids[p31_ids != ""]

    if p31_ids.empty:
        breakdown_df = pd.DataFrame(columns=["qid", "label", "count"], )
    else:
        counts = p31_ids.value_counts()

        breakdown_df = pd.DataFrame([{
            "qid": qid, "label": _policy_qid_label(category_cache, qid), "count": int(count),
        } for qid, count in counts.items()])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    breakdown_df.to_csv(output_path, index=False)


def clean_summary(value: object) -> str:
    """Normalize summary whitespace while preserving the original text content."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"Wikipedia summary must be text or null, got {type(value).__name__}")
    return re.sub(r"\s+", " ", value).strip()


def _split_summary_sentences(summary: str) -> list[str]:
    """
    Split summary text into sentences while protecting common dotted initialisms.

    Wikipedia lead text commonly contains abbreviations such as ``U.S.``. A small
    protected-token pass avoids treating those periods as sentence boundaries
    without adding an NLP dependency.
    """
    sentinel = "\uE000"

    def protect_initialism(match: re.Match[str]) -> str:
        return match.group(0).replace(".", sentinel)

    protected = re.sub(r"\b(?:[A-Za-z]\.){2,}", protect_initialism, summary)
    for abbreviation in ("Mr.", "Mrs.", "Ms.", "Dr.", "St.", "Mt."):
        protected = protected.replace(abbreviation, abbreviation.replace(".", sentinel))

    sentences = re.split(r"(?<=[.!?])\s+", protected)
    return [sentence.replace(sentinel, ".").strip() for sentence in sentences if sentence.strip()]


def strip_summary_boilerplate(summary: object, item_name: str, stop_words) -> str:
    """
    Build descriptive text for downstream lexical models.

    The feature's own name is removed from each sentence to reduce entity-name
    memorization. Entire sentences are then discarded when they are dominated by
    common Wikipedia placename boilerplate:

    - census/estimated population statements;
    - simple administrative location statements such as
      "is a city in Los Angeles County, California, United States.";
    - generic elevation fragments such as "at an elevation of 348 feet (106 m)".

    Location sentences containing additional descriptive geography are intentionally
    retained unless they match the narrow administrative-location pattern. Stop
    words are then filtered out to emphasize lexical core terms.
    """
    cleaned = clean_summary(summary)
    name = str(item_name or "").strip()
    if not cleaned:
        return ""

    name_pattern = (re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.IGNORECASE) if name else None)
    population_pattern = re.compile(r"\bpopulation\b.*\b(?:census|estimated|estimate)\b"
                                    r"|\b(?:census|estimated|estimate)\b.*\bpopulation\b",
        re.IGNORECASE, )
    administrative_location_pattern = re.compile(r"\bis\s+(?:a|an)\s+"
                                                 r"(?:home\s+rule\s+)?"
                                                 r"(?:city|town|village|municipality|"
                                                 r"census-designated\s+place(?:\s*\(CDP\))?|"
                                                 r"unincorporated\s+community)"
                                                 r"\s+in\s+[^.!?]*?\bCounty,\s*[^,.!?]+,\s*"
                                                 r"(?:the\s+)?United\s+States\b", re.IGNORECASE, )
    elevation_fragment_pattern = re.compile(r"\bat\s+an?\s+elevation\s+of\s+"
                                            r"\d[\d,]*(?:\.\d+)?\s*(?:feet|ft\.?)"
                                            r"(?:\s*\(\s*\d[\d,]*(?:\.\d+)?\s*m\s*\))?",
        re.IGNORECASE, )

    # Compile a fast word-boundary lookup regex for stop words if provided
    stop_words_set = {sw.lower() for sw in stop_words} if stop_words else set()
    stop_word_pattern = (
        re.compile(r"\b(?:" + "|".join(re.escape(sw) for sw in stop_words_set) + r")\b",
                   re.IGNORECASE) if stop_words_set else None)

    kept: list[str] = []
    for sentence in _split_summary_sentences(cleaned):
        if name_pattern is not None:
            sentence = name_pattern.sub("", sentence)
        sentence = clean_summary(sentence)
        if not sentence:
            continue
        if population_pattern.search(sentence):
            continue
        if administrative_location_pattern.search(sentence):
            continue

        # Elevation is generic placename metadata rather than descriptive lexical
        # signal. Remove the fragment while preserving any useful remainder of
        # the sentence.
        sentence = elevation_fragment_pattern.sub("", sentence)
        sentence = clean_summary(sentence)

        # Remove stop words if configured
        if stop_word_pattern is not None:
            sentence = stop_word_pattern.sub("", sentence)
            # Clean up any leftover double spaces created by the removal
            sentence = re.sub(r"\s+", " ", sentence).strip()

        if sentence:
            kept.append(sentence)

    return clean_summary(" ".join(kept))


def _validated_tsv_path(enrichment_path: Path) -> Path:
    """Derive the validated text-corpus TSV path from the enrichment output path."""
    stem = enrichment_path.stem
    if stem.endswith("_enrich"):
        stem = stem[:-len("_enrich")]
    return enrichment_path.with_name(f"{stem}_validated.tsv")


def _tsv_value(value: object) -> object:
    """Make nested cache values safe and deterministic inside one TSV cell."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return value


def _temporary_output_path(output_path: Path) -> Path:
    """Return the private temporary path used until validation succeeds."""
    return output_path.with_suffix(output_path.suffix + ".tmp")


def _remove_if_exists(path: Path) -> None:
    """Remove an artifact if present."""
    if path.exists():
        path.unlink()


def log_reason_breakdown(
        diagnostic_df: pd.DataFrame, state: str, ) -> None:
    """Log validation-reason counts for one aggregate validation state."""
    state_df = diagnostic_df[diagnostic_df["validation_state"] == state]
    state_total = len(state_df)
    if state_total == 0:
        return

    reason_counts = state_df["validation_reason"].value_counts(dropna=False)

    for reason, count in reason_counts.items():
        reason_text = str(reason) if pd.notna(reason) else "<missing>"
        state_rate = count / state_total
        LOGGER.info("    %-28s %7s  (%6.2f%%)", reason_text, f"{count:,}", state_rate * 100)


def _progress_interval(total_rows: int) -> int:
    """Return a low-noise progress interval of roughly 20 updates per run."""
    if total_rows <= 0:
        return 1
    return max(100, total_rows // 20)


def _log_progress(processed: int, total: int, started_at: float) -> None:
    """Log processing rate, elapsed time, and estimated remaining time."""
    elapsed = max(time.perf_counter() - started_at, 0.000001)
    rate = processed / elapsed
    remaining = max(total - processed, 0)
    eta = remaining / rate if rate > 0 else 0.0
    percent = (processed / total * 100.0) if total else 100.0

    LOGGER.debug("MatchWikipedia progress: %s/%s (%.1f%%), %.1f rows/s, elapsed %.1fs, ETA %.1fs",
        f"{processed:,}", f"{total:,}", percent, rate, elapsed, eta, )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate cached Wiki* entities and build trusted enrichment + diagnostics "
                    "CSVs.")
    parser.add_argument("--input", type=Path, required=True, help="Original feature CSV")
    parser.add_argument("--cache-file", type=Path, required=True,
        help="Raw Wikipedia/Wikidata JSON cache", )
    parser.add_argument("--category-cache", type=Path, required=True,
        help="wiki_categories.json with P31 labels and P279 hierarchy", )
    parser.add_argument("--category", required=True,
        help="Map category selecting validation rules", )
    parser.add_argument("--output", type=Path, required=True, help="Accepted enrichment CSV")
    parser.add_argument("--diagnostics", type=Path, required=True,
        help="Full validation diagnostic CSV", )
    parser.add_argument("--validation-config", type=Path,
        default=Path("config/wiki_lookup_validation.yml"), help="Category validation YAML", )
    parser.add_argument("--preprocessing-config", type=Path,
        default=Path("config/wiki_lookup_preprocessing.yml"),
        help="Lookup preprocessing YAML used by wikipedia_cache.py", )
    parser.add_argument("--lang", default="en", help="Wikipedia language code")
    parser.add_argument("--id", default="osm_id", help="Feature ID column")
    parser.add_argument("--name-col", default="item_name", help="Feature name column")
    parser.add_argument("--sub-category-col", default="sub_category",
        help="Feature sub-category column", )
    parser.add_argument("--wiki-col", default="wikipedia", help="Wikipedia tag column")
    parser.add_argument("--wikidata-col", default="wikidata", help="Wikidata tag column")
    parser.add_argument("--lat-col", default="lat", help="OSM latitude column")
    parser.add_argument("--lon-col", default="lon", help="OSM longitude column")
    parser.add_argument("-v", "--verbose", action="store_true",
        help="Enable verbose diagnostic logging.", )
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s", )

    LOGGER.debug("MatchWikipedia starting")
    LOGGER.debug("Input CSV: %s", args.input)
    LOGGER.debug("Wikipedia cache: %s", args.cache_file)
    LOGGER.debug("Category cache: %s", args.category_cache)
    LOGGER.debug("Validation config: %s", args.validation_config)
    LOGGER.debug("Preprocessing config: %s", args.preprocessing_config)
    LOGGER.debug("Category: %s", args.category)
    LOGGER.debug("Output: %s", args.output)
    LOGGER.debug("Diagnostics: %s", args.diagnostics)

    temp_output = _temporary_output_path(args.output)
    validated_tsv_file = _validated_tsv_path(args.output)
    temp_validated_tsv = _temporary_output_path(validated_tsv_file)

    _remove_if_exists(temp_output)
    _remove_if_exists(temp_validated_tsv)
    _remove_if_exists(args.output)
    _remove_if_exists(validated_tsv_file)

    diagnostics_file = Path(args.diagnostics)
    unmatched_file = diagnostics_file.with_name("unmatched_categories.csv")

    try:
        load_started_at = time.perf_counter()

        LOGGER.debug("Loading feature CSV...")
        df = pd.read_csv(args.input, dtype={args.id: str})
        LOGGER.debug("Loaded %s feature rows.", f"{len(df):,}")

        for column in (args.id, args.name_col, args.sub_category_col, args.lat_col, args.lon_col,
                       args.wiki_col, args.wikidata_col,):
            if column not in df.columns:
                raise ValueError(f"CSV must contain '{column}'")

        LOGGER.debug("Loading lookup preprocessing configuration...")
        preprocessing = load_lookup_preprocessing(args.preprocessing_config)
        augment_context = preprocessing["augment_context"]
        normalize_phrases = preprocessing["normalize_phrases"]

        LOGGER.debug("Loading Wikipedia/Wikidata metadata cache...")
        wikipedia = Wikipedia(args.cache_file, headers={})
        wikipedia.load()
        LOGGER.debug("Wikipedia/Wikidata metadata cache loaded.")

        LOGGER.debug("Loading Wikidata category cache...")
        category_cache = load_category_cache(args.category_cache)
        LOGGER.debug("Category cache loaded: %s P31/P279 entries.", f"{len(category_cache):,}")

        LOGGER.debug("Loading validation policy...")
        rules = load_validation_config(args.validation_config, args.category)

        # log_validation_policy(rules, category_cache)
        LOGGER.debug("Initialization complete in %.2fs.", time.perf_counter() - load_started_at, )
    except Exception as exc:
        LOGGER.error("ERROR: %s", exc)
        return 1

    diagnostics: list[dict[str, object]] = []
    enrichment: list[dict[str, object]] = []
    validated_tsv_records: list[dict[str, object]] = []
    counts = {"ACCEPT": 0, "REJECT": 0, "UNCERTAIN": 0}

    try:
        total_rows = len(df)
        progress_interval = _progress_interval(total_rows)
        processing_started_at = time.perf_counter()

        LOGGER.info("Matching Wikipedia records for %s feature(s)...", f"{total_rows:,}")
        LOGGER.debug("Verbose progress interval: every %s row(s).", f"{progress_interval:,}")

        for row_number, (_, row) in enumerate(df.iterrows(), start=1):
            parsed = parse_wiki_tag(row, args.wiki_col, args.name_col, args.sub_category_col,
                augment_context, normalize_phrases, )

            source_wikidata_id = parse_wikidata_tag(row.get(args.wikidata_col)) or ""

            osm_id = _feature_id(row[args.id])
            item_name = str(row[args.name_col]).strip()
            sub_category = str(row[args.sub_category_col]).strip()
            osm_lat, osm_lon = _source_coordinates(row, args.lat_col, args.lon_col)

            resolution = _resolve_feature_article(parsed=parsed, requested_lang=args.lang,
                source_wikidata_id=source_wikidata_id, item_name=item_name,
                sub_category=sub_category, osm_lat=osm_lat, osm_lon=osm_lon, rules=rules,
                category_cache=category_cache, wikipedia=wikipedia, )

            state, reason_code = _reason_code(parsed=parsed, requested_lang=args.lang,
                resolution=resolution, )
            if not osm_id:
                state, reason_code = "REJECT", "MISSING_FEATURE_ID"
            counts[state] += 1

            wikipedia_entry = resolution.get("wikipedia_entry")
            if wikipedia_entry is not None and not isinstance(wikipedia_entry, dict):
                raise ValueError("Resolved Wikipedia entry must be a mapping")

            direct_qids = list(resolution["direct_qids"])
            ancestor_qids = set(resolution["ancestor_qids"])
            accept_matches = list(resolution["accept_matches"])
            reject_matches = list(resolution["reject_matches"])

            detail_parts = [part for part in
                (resolution["data_reason"], resolution["category_reason"],
                 resolution["location_reason"], resolution["resolution_reason"],) if part]
            validation_detail = "; ".join(detail_parts)
            stop_words = STOP_WORDS

            p31_ids_text = "|".join(sorted(direct_qids))
            p31_labels_text = (
                _format_qid_labels(direct_qids, category_cache) if direct_qids else "")
            parent_labels_text = (
                _format_qid_labels(ancestor_qids, category_cache) if ancestor_qids else "")
            accept_labels_text = (
                _format_qid_labels(accept_matches, category_cache) if accept_matches else "")
            reject_labels_text = (
                _format_qid_labels(reject_matches, category_cache) if reject_matches else "")

            source_entry = resolution.get("source_entry")
            source_disambiguation = bool(
                isinstance(source_entry, dict) and source_entry.get("disambiguation"))
            source_missing = bool(isinstance(source_entry, dict) and source_entry.get("missing"))

            resolved_title = (
                str(wikipedia_entry.get("resolved_title") or "") if isinstance(wikipedia_entry,
                                                                               dict) else "")

            diagnostic_record = {
                args.id: osm_id, "item_name": item_name, "map_category": args.category,
                "sub_category": sub_category, "lookup_title": resolution["lookup_title"],
                "source_cache_key": resolution["source_cache_key"],
                "resolved_title": resolved_title, "cache_key": resolution["cache_key"],
                "source_wikidata_id": source_wikidata_id, "wikidata_id": resolution["wikidata_id"],
                "resolution_method": resolution["resolution_method"],
                "resolution_failure": resolution["resolution_failure"],
                "resolution_score": resolution["resolution_score"],
                "resolution_reason": resolution["resolution_reason"],
                "candidate_count": resolution["candidate_count"],
                "exact_wikidata_identity": bool(resolution.get("exact_identity")),
                "p31_ids": p31_ids_text, "p31_labels": p31_labels_text,
                "p31_parent_labels": parent_labels_text, "validation_state": state,
                "validation_reason": reason_code, "validation_detail": validation_detail,
                "category_state": resolution["category_state"],
                "category_reason": resolution["category_reason"],
                "accept_matches": "|".join(accept_matches),
                "accept_match_labels": accept_labels_text,
                "reject_matches": "|".join(reject_matches),
                "reject_match_labels": reject_labels_text, "osm_lat": osm_lat, "osm_lon": osm_lon,
                "wikipedia_lat": resolution["wikipedia_lat"],
                "wikipedia_lon": resolution["wikipedia_lon"],
                "wikidata_lat": resolution["wikidata_lat"],
                "wikidata_lon": resolution["wikidata_lon"],
                "location_lat": resolution["location_lat"],
                "location_lon": resolution["location_lon"],
                "location_source": resolution["location_source"], "distance_m": (
                    round(float(resolution["distance_m"]), 1) if resolution[
                                                                     "distance_m"] is not None
                    else None),
                "max_distance_m": rules["location"]["max_distance_m"],
                "location_state": resolution["location_state"],
                "location_reason": resolution["location_reason"],
                "source_disambiguation": source_disambiguation, "source_missing": source_missing,
                "article_length": (
                    wikipedia_entry["wikipedia_length"] if isinstance(wikipedia_entry,
                                                                      dict) else None),
                "link_count": (wikipedia_entry["wikipedia_links"] if isinstance(wikipedia_entry,
                                                                                dict) else None),
                "wikidata_sitelink_count": resolution["wikidata_sitelink_count"],
            }
            diagnostics.append(diagnostic_record)

            if state == "ACCEPT":
                summary = clean_summary(wikipedia_entry["summary"])
                summary_descriptive = strip_summary_boilerplate(summary, item_name, stop_words)

                enrichment.append({
                    args.id: osm_id, "item_name": item_name, "map_category": args.category,
                    "sub_category": sub_category, "resolved_title": resolved_title,
                    "wikidata_id": resolution["wikidata_id"],
                    "resolution_method": resolution["resolution_method"],
                    "exact_wikidata_identity": bool(resolution.get("exact_identity")),
                    "p31_ids": p31_ids_text, "p31_labels": p31_labels_text,
                    "article_length": wikipedia_entry["wikipedia_length"],
                    "link_count": wikipedia_entry["wikipedia_links"],
                    "wikidata_sitelink_count": resolution["wikidata_sitelink_count"],
                    "summary": summary, "summary_descriptive": summary_descriptive,
                    "wikipedia_lat": resolution["wikipedia_lat"],
                    "wikipedia_lon": resolution["wikipedia_lon"],
                    "wikidata_lat": resolution["wikidata_lat"],
                    "wikidata_lon": resolution["wikidata_lon"],
                    "location_lat": resolution["location_lat"],
                    "location_lon": resolution["location_lon"],
                    "location_source": resolution["location_source"], "distance_m": (
                        round(float(resolution["distance_m"]), 1) if resolution[
                                                                         "distance_m"] is not
                                                                     None else None),
                })

                # The validated TSV is the ML/text-analysis corpus. Keep the full
                # resolved Wikipedia record, but attach the exact OSM/validation
                # provenance that caused this article to be accepted.
                validated_record = {
                    args.id: osm_id, "item_name": item_name, "map_category": args.category,
                    "sub_category": sub_category, "cache_key": resolution["cache_key"],
                    "validation_state": state, "validation_reason": reason_code,
                    "resolution_method": resolution["resolution_method"],
                    "exact_wikidata_identity": bool(resolution.get("exact_identity")),
                }
                for key, value in wikipedia_entry.items():
                    if key == "summary":
                        validated_record[key] = summary
                    else:
                        validated_record[key] = _tsv_value(value)
                validated_record["summary_descriptive"] = summary_descriptive
                validated_tsv_records.append(validated_record)

            if args.verbose and (row_number == total_rows or row_number % progress_interval == 0):
                _log_progress(row_number, total_rows, processing_started_at, )

        LOGGER.debug("Row matching complete in %.2fs.",
            time.perf_counter() - processing_started_at, )

        diagnostic_columns = [args.id, "item_name", "map_category", "sub_category", "lookup_title",
            "source_cache_key", "resolved_title", "cache_key", "source_wikidata_id", "wikidata_id",
            "resolution_method", "resolution_failure", "resolution_score", "resolution_reason",
            "candidate_count", "exact_wikidata_identity", "p31_ids", "p31_labels",
            "p31_parent_labels", "validation_state", "validation_reason", "validation_detail",
            "category_state", "category_reason", "accept_matches", "accept_match_labels",
            "reject_matches", "reject_match_labels", "osm_lat", "osm_lon", "wikipedia_lat",
            "wikipedia_lon", "wikidata_lat", "wikidata_lon", "location_lat", "location_lon",
            "location_source", "distance_m", "max_distance_m", "location_state", "location_reason",
            "source_disambiguation", "source_missing", "article_length", "link_count",
            "wikidata_sitelink_count", ]
        enrichment_columns = [args.id, "item_name", "map_category", "sub_category",
            "resolved_title", "wikidata_id", "resolution_method", "exact_wikidata_identity",
            "p31_ids", "p31_labels", "article_length", "link_count", "wikidata_sitelink_count",
            "summary", "summary_descriptive", "wikipedia_lat", "wikipedia_lon", "wikidata_lat",
            "wikidata_lon", "location_lat", "location_lon", "location_source", "distance_m", ]

        diagnostic_df = pd.DataFrame(diagnostics, columns=diagnostic_columns)
        enrichment_df = pd.DataFrame(enrichment, columns=enrichment_columns)
        validated_tsv_df = pd.DataFrame(validated_tsv_records)

        args.output.parent.mkdir(parents=True, exist_ok=True)
        validated_tsv_file.parent.mkdir(parents=True, exist_ok=True)
        diagnostics_file.parent.mkdir(parents=True, exist_ok=True)

        # Diagnostics are useful regardless of job success and are written first.
        output_started_at = time.perf_counter()
        LOGGER.debug("Writing outputs: %s diagnostic rows, %s accepted rows.",
            f"{len(diagnostic_df):,}", f"{len(enrichment_df):,}", )

        diagnostic_df.to_csv(diagnostics_file, index=False)
        LOGGER.debug("Wrote diagnostics CSV: %s", diagnostics_file)

        save_unmatched_category_breakdown(diagnostic_df, category_cache, unmatched_file, )

        LOGGER.debug("Wrote unmatched-category breakdown: %s", unmatched_file)

        enrichment_df.to_csv(temp_output, index=False)
        LOGGER.debug("Wrote temporary enrichment CSV: %s", temp_output)

        validated_tsv_df.to_csv(temp_validated_tsv, sep="\t", index=False)
        LOGGER.debug("Wrote temporary validated TSV: %s", temp_validated_tsv)
        LOGGER.debug("Output generation complete in %.2fs.",
            time.perf_counter() - output_started_at, )

        total = len(diagnostic_df)
        accept_rate = counts["ACCEPT"] / total if total else 0.0
        reject_rate = counts["REJECT"] / total if total else 0.0
        uncertain_rate = counts["UNCERTAIN"] / total if total else 0.0

        LOGGER.info("Validated %s lookups for category %r:", f"{total:,}", args.category)
        LOGGER.info("  ACCEPT:    %7s  (%6.2f%%)", f"{counts['ACCEPT']:,}", accept_rate * 100)
        LOGGER.info("  REJECT:    %7s  (%6.2f%%)", f"{counts['REJECT']:,}", reject_rate * 100)
        log_reason_breakdown(diagnostic_df, "REJECT")
        LOGGER.info("  UNCERTAIN: %7s  (%6.2f%%)", f"{counts['UNCERTAIN']:,}", uncertain_rate * 100)
        log_reason_breakdown(diagnostic_df, "UNCERTAIN")

        exact_identity_accepts = int(((diagnostic_df["validation_state"] == "ACCEPT") & (
                    diagnostic_df["exact_wikidata_identity"] == True)).sum())
        if exact_identity_accepts:
            LOGGER.info("  Exact Wikidata identity accepts: %s", f"{exact_identity_accepts:,}")

        LOGGER.info("Saved diagnostics: %s", diagnostics_file)

        resolution_outcomes = diagnostic_df["resolution_failure"].where(
            diagnostic_df["resolution_failure"] != "", diagnostic_df["resolution_method"], )
        resolution_counts = resolution_outcomes.value_counts()
        if not resolution_counts.empty:
            LOGGER.info("Resolution outcomes:")
            for outcome, count in resolution_counts.items():
                rate = count / total if total else 0.0
                LOGGER.info("  %-36s %7s  (%6.2f%%)", outcome, f"{count:,}", rate * 100)

        warnings, failures = evaluate_job_sanity(counts, rules["sanity"])

        for warning in warnings:
            LOGGER.warning("%s", warning)

        if failures:
            _remove_if_exists(temp_output)
            _remove_if_exists(temp_validated_tsv)
            failure_text = "; ".join(failures)
            raise RuntimeError(f"Validation job sanity check failed: {failure_text}. "
                               f"Inspect diagnostics: {diagnostics_file}")

        temp_output.replace(args.output)
        temp_validated_tsv.replace(validated_tsv_file)
        LOGGER.info("Saved enrichment: %s", args.output)
        LOGGER.info("Saved validated TSV: %s", validated_tsv_file)
        LOGGER.debug("MatchWikipedia complete: %s row(s) processed.", f"{total_rows:,}", )

    except Exception as exc:
        _remove_if_exists(temp_output)
        _remove_if_exists(temp_validated_tsv)

        if diagnostics:
            try:
                diagnostics_file.parent.mkdir(parents=True, exist_ok=True)
                pd.DataFrame(diagnostics).to_csv(diagnostics_file, index=False)
            except Exception as diagnostic_exc:
                LOGGER.error("ERROR: %s; additionally failed to save diagnostics: %s", exc,
                    diagnostic_exc, )
                return 1

        LOGGER.error("ERROR: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
