#!/usr/bin/env python3
"""
Fetch raw Wikipedia/Wikidata metadata for OSM feature names.

This utility is acquisition-only. It updates a shared local JSON cache with raw
Wikipedia/Wikidata facts matching the name. A separate process is used to
validate whether the Wikipedia data is for the desired entity.

{
    "schema_version": 2,
    "wikipedia": {
        "lang:Requested Title": {
            "resolved_title": str,
            "wikipedia_length": int | None,
            "wikipedia_links": int | None,
            "summary": str,
            "disambiguation": bool,
            "disambiguation_links": list[str],
            "wikipedia_lat": float | None,
            "wikipedia_lon": float | None,
            "wikidata_id": str | None,
            "missing": bool
        }
    },
    "wikidata": {
        "Q123": {
            "instance_of": list[str],
            "wikidata_sitelink_count": int,
            "enwiki_title": str | None,
            "wikidata_lat": float | None,
            "wikidata_lon": float | None
        }
    }
}

Wikipedia cache identity remains the normalized title requested by this pipeline:
    "lang:Requested Title"

Wikidata cache identity is the QID itself.

DATA INTEGRITY:
Required API fields raise if missing. Do not hide schema/programming errors with
fabricated defaults. Optional API fields use explicit documented absence states.
Unknown numeric data is represented by None, never by 0.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, Set
from urllib.parse import unquote, urlparse

import pandas as pd
from tqdm import tqdm
from ExtractOSM.wikipedia import (
    Wikipedia,
    fetch_wikidata_entities,
    load_cache,
    save_cache,
    wikidata_enwiki_titles,
    wikipedia_qids,
)
import yaml



def _progress_bar(name: str, total: int, unit: str):
    """Create the standard low-noise progress display used by this tool."""
    return tqdm(
        total=total,
        desc=name,
        unit=unit,
        mininterval=30.0,
        maxinterval=30.0,
        dynamic_ncols=True,
        bar_format="{desc}: {percentage:5.1f}% {n_fmt}/{total_fmt} "
                   "[{elapsed}<{remaining}, {rate_fmt}]",
    )


def _format_elapsed(seconds: float) -> str:
    """Format elapsed seconds for compact phase summaries."""
    total_seconds = max(int(round(seconds)), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _percent(part: int, total: int) -> float:
    """Return a percentage while handling an empty logical work set."""
    return (100.0 * part / total) if total else 100.0


def _print_phase_summary(phase_name: str, rows: list[tuple[str, object]]) -> None:
    """Print a compact, consistent completion summary for one acquisition phase."""
    print(f"\n✅ {phase_name} complete")
    width = max((len(label) for label, _ in rows), default=0)
    for label, value in rows:
        print(f"   - {label:<{width}} : {value}")


def _word_forms(term: str) -> Set[str]:
    """Return simple singular/plural forms used for context suppression."""
    term = term.casefold().strip()
    forms = {term}
    if term.endswith("s") and len(term) > 1:
        forms.add(term[:-1])
    else:
        forms.add(f"{term}s")
    return forms

def normalize_fallback_title(item_name: str) -> str:
    """Normalize an OSM name for fallback Wikipedia lookup."""
    return " ".join(item_name.replace("#", " ").split())

def replace_lookup_phrases(
        item_name: str, sub_category: str | None,
        normalize_phrases: Dict[str, Dict[str, object]], ) -> str:
    """Replace complete phrases in a fallback name using category rules.

    Rules run once each in YAML order. Matching is case-insensitive, accepts
    whitespace between phrase words, and requires word boundaries around the
    entire phrase. Source and replacement text are literal, not regex syntax.
    Replacement capitalization is exactly as configured. No length limit applies.

    Args:
        item_name: Normalized fallback feature name.
        sub_category: Exact category key, or None when unavailable.
        normalize_phrases: Validated category rules from the YAML file.

    Returns:
        Name with configured replacements applied and whitespace normalized.
    """
    rule = normalize_phrases.get(sub_category, {}) if sub_category else {}
    name = item_name
    for replacement in rule.get("replacements", []):
        phrase = r"\s+".join(re.escape(word) for word in replacement["from"].split())
        pattern = rf"(?<!\w){phrase}(?!\w)"
        target = " ".join(replacement["to"].split())
        name = re.sub(pattern, lambda match: target, name, flags=re.IGNORECASE)
    return " ".join(name.split())

def augment_lookup_name(
        item_name: str, sub_category: str | None,
        augment_context: Dict[str, Dict[str, object]], ) -> str:
    """Add configured category context to short ambiguous fallback names."""
    name = item_name.strip()
    if not sub_category:
        return name

    rule = augment_context.get(sub_category)
    if not rule or len(name) > int(rule["max_length"]):
        return name

    words = {word.casefold().strip(".,;:!?()[]{}\"'") for word in name.split() if
        word.strip(".,;:!?()[]{}\"'")}

    suppress_terms = list(str(rule["append"]).split()) + list(rule.get("synonyms", []))
    for term in suppress_terms:
        for term_word in str(term).split():
            if words & _word_forms(term_word):
                return name

    return f"{name} {str(rule['append']).strip()}"

def parse_wiki_tag(
        row, wiki_col: str, name_col: str, sub_category_col: str,
        augment_context: Dict[str, Dict[str, object]],
        normalize_phrases: Dict[str, Dict[str, object]] | None = None, ) -> Tuple[str, str] | None:
    """Parse a reference or preprocess a fallback title.

    Explicit Wikipedia references bypass all category rules. Fallback names
    undergo whitespace normalization, phrase replacement, then augmentation.

    Args:
        row: Input feature row.
        wiki_col: Column containing explicit Wikipedia references.
        name_col: Column containing the fallback feature name.
        sub_category_col: Column containing the exact category key.
        augment_context: Short-name augmentation rules.
        normalize_phrases: Optional phrase replacement rules.

    Returns:
        Language and lookup title, or None if neither reference nor name exists.
    """
    value = row.get(wiki_col)

    if not pd.isna(value) and str(value).strip():
        val_str = str(value).strip()

        if val_str.startswith("http"):
            parsed_url = urlparse(val_str)
            host = parsed_url.netloc
            path = parsed_url.path
            lang = host.split(".")[0]
            title = unquote(path.split("/wiki/")[-1]).split("#")[0].replace("_", " ")
            return lang, title

        if ":" in val_str:
            parts = val_str.split(":", 1)
            if 2 <= len(parts[0]) <= 4 and parts[0].isalpha():
                lang = parts[0]
                title = parts[1].split("#")[0].replace("_", " ")
                return lang, title

        title = val_str.split("#")[0].replace("_", " ")
        return "en", title

    item_name = row.get(name_col)
    if not pd.isna(item_name) and str(item_name).strip():
        sub_category = row.get(sub_category_col)
        if pd.isna(sub_category):
            sub_category = None

        category = str(sub_category).strip() if sub_category is not None else None
        title = normalize_fallback_title(str(item_name))
        title = replace_lookup_phrases(title, category, normalize_phrases or {})
        title = augment_lookup_name(title, category, augment_context)
        return "en", title

    return None

def parse_wikidata_tag(value: object) -> str | None:
    """Parse an OSM Wikidata tag or Wikidata URL into a QID."""
    if pd.isna(value) or not str(value).strip():
        return None

    text = str(value).strip()

    if text.startswith("http"):
        parsed = urlparse(text)
        text = parsed.path.rstrip("/").split("/")[-1]
    elif ":" in text:
        prefix, suffix = text.split(":", 1)
        if prefix.casefold() in {"wikidata", "wd"}:
            text = suffix

    text = text.strip()
    text = "".join(char for char in text if char.isalnum())
    if not text.startswith("Q") or not text[1:].isdigit():
        raise ValueError(f"Invalid Wikidata ID: {value!r}")

    return text

def trace_lookup_rows(df, args, preprocessing: Dict[str, Any]) -> Set[str]:
    """Report preprocessing and cached results for selected feature names.

    Args:
        df: Input rows with parsed_wiki already populated.
        args: CLI arguments, including repeatable trace_name values.
        preprocessing: Validated preprocessing configuration.

    Returns:
        Final titles to trace during Wikipedia fetching in the selected language.
    """
    if not args.trace_name:
        return set()
    wanted = {name.strip().casefold() for name in args.trace_name}
    matched = set()
    titles = set()
    print(f"[lookup trace] preprocessing file: {args.preprocessing_config.resolve()}")
    print(f"[lookup trace] cache file: {args.cache_file.resolve()}")
    print(
        f"[lookup trace] configured phrase categories: "
        f"{list(preprocessing['normalize_phrases'])!r}")
    for index, row in df.iterrows():
        raw_name = row.get(args.name_col)
        if pd.isna(raw_name) or str(raw_name).strip().casefold() not in wanted:
            continue
        matched.add(str(raw_name).strip().casefold())
        raw_category = row.get(args.sub_category_col)
        category = None if pd.isna(raw_category) else str(raw_category).strip()
        wiki = row.get(args.wiki_col)
        explicit = not pd.isna(wiki) and bool(str(wiki).strip())
        print(f"[lookup trace] row={index!r}, name={raw_name!r}, category={category!r}")
        print(f"  explicit Wikipedia={wiki!r}; Wikidata={row.get(args.wikidata_col)!r}")
        if explicit:
            print("  Preprocessing BYPASSED: nonblank explicit Wikipedia reference.")
        else:
            normalized = normalize_fallback_title(str(raw_name))
            print(f"  normalized name: {normalized!r}")
            rule = preprocessing['normalize_phrases'].get(category)
            print(
                f"  phrase rule: {rule!r}" if rule else "  NO phrase rule for this exact category.")
            current = normalized
            for replacement in (rule or {}).get('replacements', []):
                single = {category: {'replacements': [replacement]}}
                updated = replace_lookup_phrases(current, category, single)
                print(f"  replacement {replacement!r}: {current!r} -> {updated!r} "
                      f"({'changed' if updated != current else 'no change'})")
                current = updated
            augmentation = preprocessing['augment_context'].get(category)
            augmented = augment_lookup_name(current, category, preprocessing['augment_context'])
            print(f"  augmentation rule: {augmentation!r}; result: {augmented!r}")
        parsed = row['parsed_wiki']
        print(f"  final parsed lookup: {parsed!r}")
        if not parsed:
            continue
        lang, title = parsed
        if lang != args.lang:
            print(f"  SKIPPED: lookup language {lang!r} differs from --lang {args.lang!r}.")
            continue
        titles.add(title)
        key = f"{lang}:{title}"
        print(f"  lookup key: {key!r}")
    for name in sorted(wanted - matched):
        print(
            f"[lookup trace] No input row matches {name!r} (exact name, ignoring case/outer "
            f"spaces).")
    return titles

def load_lookup_preprocessing(config_path: Path) -> Dict[str, Any]:
    """Load and validate category-specific lookup preprocessing rules.

    Args:
        config_path: YAML containing optional ``augment_context`` and
            ``normalize_phrases`` mappings. Existing augmentation-only files
            remain valid.

    Returns:
        Configuration containing both rule mappings.

    Raises:
        ValueError: If the file cannot be read or rules are invalid.
    """
    if not config_path.exists():
        raise ValueError(f"Wikipedia lookup preprocessing config not found: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as file:
            data = yaml.safe_load(file) or {}
    except OSError as exc:
        raise ValueError(
            f"Unable to read Wikipedia lookup preprocessing config: {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(
            f"Invalid YAML in Wikipedia lookup preprocessing config: {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Lookup preprocessing YAML root must be a mapping")

    augment_context = data.get("augment_context", {})
    if not isinstance(augment_context, dict):
        raise ValueError("'augment_context' must be a mapping")

    for sub_category, rule in augment_context.items():
        if not isinstance(rule, dict):
            raise ValueError(f"augment_context.{sub_category} must be a mapping")
        if "max_length" not in rule or "append" not in rule:
            raise ValueError(f"augment_context.{sub_category} requires 'max_length' and 'append'")
        if not isinstance(rule["max_length"], int) or rule["max_length"] < 1:
            raise ValueError(
                f"augment_context.{sub_category}.max_length must be a positive integer")
        if not isinstance(rule["append"], str) or not rule["append"].strip():
            raise ValueError(f"augment_context.{sub_category}.append must be a non-empty string")

        synonyms = rule.get("synonyms", [])
        if not isinstance(synonyms, list) or not all(isinstance(v, str) for v in synonyms):
            raise ValueError(f"augment_context.{sub_category}.synonyms must be a list of strings")

    normalize_phrases = data.get("normalize_phrases", {})
    if not isinstance(normalize_phrases, dict):
        raise ValueError("'normalize_phrases' must be a mapping")
    for sub_category, rule in normalize_phrases.items():
        path = f"normalize_phrases.{sub_category}"
        if not isinstance(sub_category, str) or not sub_category.strip():
            raise ValueError("normalize_phrases category keys must be non-empty strings")
        if not isinstance(rule, dict) or "replacements" not in rule:
            raise ValueError(f"{path} requires a 'replacements' list")
        if not isinstance(rule["replacements"], list):
            raise ValueError(f"{path}.replacements must be a list")
        for index, replacement in enumerate(rule["replacements"]):
            entry_path = f"{path}.replacements[{index}]"
            if not isinstance(replacement, dict):
                raise ValueError(f"{entry_path} must be a mapping")
            for field in ("from", "to"):
                value = replacement.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{entry_path}.{field} must be a non-empty string")

    return {"augment_context": augment_context, "normalize_phrases": normalize_phrases}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch  Wikipedia/Wikidata metadata into a shared JSON cache.")
    parser.add_argument("--input", type=Path, help="Input feature CSV")
    parser.add_argument("--cache-file", type=Path, help=f"Shared raw cache JSON ", )
    parser.add_argument("--project", help="Project name for Wikimedia User-Agent")
    parser.add_argument("--email", help="Contact email for Wikimedia User-Agent")
    parser.add_argument("--lang", default="en", help="Wikipedia language code")
    parser.add_argument("--name-col", default="item_name", help="Feature name column")
    parser.add_argument("--sub-category-col", default="sub_category",
        help="Sub-category column used only for lookup preprocessing", )
    parser.add_argument("--wiki-col", default="wikipedia", help="Wikipedia tag column")
    parser.add_argument("--wikidata-col", default="wikidata", help="Wikidata tag column")
    parser.add_argument("--preprocessing-config", type=Path, help=f"Lookup preprocessing YAML ", )
    parser.add_argument("--dry-run", action="store_true",
        help="Show lookup preprocessing and API requests without HTTP or file writes", )
    parser.add_argument("--fallback-search", action="store_true",
        help=("Enable fallback Wikipedia intitle search for exact-title misses "
              "(disabled by default)"), )
    parser.add_argument("--trace-name", action="append", default=[], metavar="NAME",
        help="Trace an exact input feature name (case-insensitive); repeat for multiple names", )
    parser.add_argument("--migrate", action="store_true",
        help="Normalize existing Wikipedia cache keys and save the migrated cache", )
    args = parser.parse_args()
    headers = {"User-Agent": f"{args.project} (Contact: {args.email})"}

    # command to migrate cache
    if args.migrate:
        wikipedia = Wikipedia(args.cache_file, headers, dry_run=args.dry_run,
            fallback_search=args.fallback_search, )
        wikipedia.load()
        stats = wikipedia.migrate_cache_keys()
        print(stats)
        sys.exit(0)

    try:
        job_started = time.monotonic()
        print(f"➡️ Reading input from '{args.input}'...")
        if not args.input.exists():
            raise ValueError(f"Input CSV not found: {args.input}")
        try:
            df = pd.read_csv(args.input)
        except OSError as exc:
            raise ValueError(f"Unable to read input CSV: {args.input}: {exc}") from exc

        # Validate required columns are present
        required_columns = [args.name_col, args.sub_category_col, args.wiki_col,
            args.wikidata_col, ]

        missing_columns = [column for column in required_columns if column not in df.columns]

        if missing_columns:
            raise ValueError("Input CSV is missing required column(s): " + ", ".join(
                f"'{column}'" for column in missing_columns))

        preprocessing = load_lookup_preprocessing(args.preprocessing_config)
        augment_context = preprocessing["augment_context"]
        normalize_phrases = preprocessing["normalize_phrases"]
        df["parsed_wiki"] = df.apply(
            lambda row: parse_wiki_tag(row, args.wiki_col, args.name_col, args.sub_category_col,
                augment_context, normalize_phrases, ), axis=1, )
        df["parsed_wikidata"] = df[args.wikidata_col].apply(parse_wikidata_tag)
        trace_titles = trace_lookup_rows(df, args, preprocessing)

        if args.dry_run:
            print("➡️ Dry-run lookup preprocessing changes:")
            changed = 0
            for _, row in df.iterrows():
                wiki_value = row.get(args.wiki_col)
                if not pd.isna(wiki_value) and str(wiki_value).strip():
                    continue
                item_name = row.get(args.name_col)
                parsed = row.get("parsed_wiki")
                if pd.isna(item_name) or not parsed:
                    continue
                _, lookup_title = parsed
                original_name = str(item_name).strip()
                if lookup_title != original_name:
                    print(f"   {original_name!r} -> {lookup_title!r}")
                    changed += 1
            print(f"   - {changed} fallback lookup names changed.")

        def filter_lang(parsed):
            return parsed if parsed and parsed[0] == args.lang else None

        df["parsed_wiki"] = df["parsed_wiki"].apply(filter_lang)

        primary_titles = {title for _, title in df["parsed_wiki"].dropna()}
        provided_qids = {qid for qid in df["parsed_wikidata"].dropna()}

        # LookupProvidedWikidata starts with Wikidata identities explicitly supplied
        # by the input data. Their enwiki sitelinks provide authoritative additional
        # titles for the primary Wikipedia lookup phase.
        cache = load_cache(args.cache_file)
        print("➡️ LookupProvidedWikidata...")
        cache = fetch_wikidata_entities(provided_qids, cache, headers, args.dry_run,
            args.cache_file, phase_name="LookupProvidedWikidata", )
        if not args.dry_run:
            save_cache(cache, args.cache_file)

        provided_wikipedia_titles = wikidata_enwiki_titles(provided_qids, cache)
        primary_article_titles = primary_titles | provided_wikipedia_titles

        # Wikipedia owns cache checks, exact-title batching, disambiguation expansion,
        # and optional fallback search. Physical request sizes remain private.
        wikipedia = Wikipedia(args.cache_file, headers, dry_run=args.dry_run,
            fallback_search=args.fallback_search, trace_titles=trace_titles, )
        wikipedia.load()

        disambiguation_match_titles: set[str] = set()
        print("➡️ LookupPrimaryArticles...")
        primary_started = time.monotonic()
        primary_before = wikipedia.stats_snapshot()
        primary_disambiguation_pages = 0

        if primary_article_titles:
            progress = _progress_bar("LookupPrimaryArticles", len(primary_article_titles), "title")
            primary_keys = (f"{args.lang}:{title}" for title in sorted(primary_article_titles))
            for _, entry in wikipedia.lookup_many(primary_keys):
                if entry is not None:
                    if entry.get("disambiguation"):
                        primary_disambiguation_pages += 1
                    disambiguation_match_titles.update(entry.get("disambiguation_links") or [])
                    if args.fallback_search:
                        disambiguation_match_titles.update(entry.get("search_candidates") or [])
                progress.update(1)
            progress.close()

        primary_after = wikipedia.stats_snapshot()
        primary_elapsed = time.monotonic() - primary_started
        primary_total = primary_after["logical_lookups"] - primary_before["logical_lookups"]
        primary_hits = primary_after["cache_hits"] - primary_before["cache_hits"]
        primary_api = primary_after["exact_api_lookups"] - primary_before["exact_api_lookups"]
        primary_batches = primary_after["exact_http_batches"] - primary_before["exact_http_batches"]
        primary_disambiguation_requests = (
                primary_after["disambiguation_requests"] - primary_before[
            "disambiguation_requests"])
        primary_fallback_requests = (primary_after["fallback_search_requests"] - primary_before[
            "fallback_search_requests"])
        primary_rate = primary_total / primary_elapsed if primary_elapsed > 0 else 0.0
        primary_summary = [("Titles examined", f"{primary_total:,}"),
            ("Cache hits", f"{primary_hits:,}"),
            ("Cache hit rate", f"{_percent(primary_hits, primary_total):.1f}%"),
            ("Exact-title API lookups", f"{primary_api:,}"),
            ("Exact-title HTTP batches", f"{primary_batches:,}"),
            ("Disambiguation pages", f"{primary_disambiguation_pages:,}"),
            ("Disambiguation matches", f"{len(disambiguation_match_titles):,}"),
            ("Disambiguation HTTP requests", f"{primary_disambiguation_requests:,}"), ]
        if args.fallback_search:
            primary_summary.append(("Fallback search requests", f"{primary_fallback_requests:,}"))
        primary_summary.extend([("Elapsed", _format_elapsed(primary_elapsed)),
            ("Overall rate", f"{primary_rate:,.2f} titles/s"), ])
        _print_phase_summary("LookupPrimaryArticles", primary_summary)

        print("➡️ LookupDisambiguationMatches...")
        matches_started = time.monotonic()
        matches_before = wikipedia.stats_snapshot()
        matches_disambiguation_pages = 0

        if disambiguation_match_titles:
            progress = _progress_bar("LookupDisambiguationMatches",
                len(disambiguation_match_titles), "title", )
            disambiguation_match_keys = (f"{args.lang}:{title}" for title in
            sorted(disambiguation_match_titles))
            for _, entry in wikipedia.lookup_many(disambiguation_match_keys):
                if entry is not None and entry.get("disambiguation"):
                    matches_disambiguation_pages += 1
                progress.update(1)
            progress.close()

        matches_after = wikipedia.stats_snapshot()
        matches_elapsed = time.monotonic() - matches_started
        matches_total = matches_after["logical_lookups"] - matches_before["logical_lookups"]
        matches_hits = matches_after["cache_hits"] - matches_before["cache_hits"]
        matches_api = matches_after["exact_api_lookups"] - matches_before["exact_api_lookups"]
        matches_batches = matches_after["exact_http_batches"] - matches_before["exact_http_batches"]
        matches_disambiguation_requests = (
                matches_after["disambiguation_requests"] - matches_before[
            "disambiguation_requests"])
        matches_fallback_requests = (matches_after["fallback_search_requests"] - matches_before[
            "fallback_search_requests"])
        matches_rate = matches_total / matches_elapsed if matches_elapsed > 0 else 0.0
        matches_summary = [("Titles examined", f"{matches_total:,}"),
            ("Cache hits", f"{matches_hits:,}"),
            ("Cache hit rate", f"{_percent(matches_hits, matches_total):.1f}%"),
            ("Exact-title API lookups", f"{matches_api:,}"),
            ("Exact-title HTTP batches", f"{matches_batches:,}"),
            ("Disambiguation pages encountered", f"{matches_disambiguation_pages:,}"),
            ("Disambiguation HTTP requests", f"{matches_disambiguation_requests:,}"), ]
        if args.fallback_search:
            matches_summary.append(("Fallback search requests", f"{matches_fallback_requests:,}"))
        matches_summary.extend([("Elapsed", _format_elapsed(matches_elapsed)),
            ("Overall rate", f"{matches_rate:,.2f} titles/s"), ])
        _print_phase_summary("LookupDisambiguationMatches", matches_summary)

        # Persist the completed Wikipedia acquisition before FetchResolvedWikidata
        # reloads the shared cache. Earlier writes occur only at checkpoints.
        wikipedia.save()

        # FetchResolvedWikidata collects Wikidata facts for every Wikipedia article
        # resolved above, plus the explicitly provided Wikidata identities.
        cache = load_cache(args.cache_file) if not args.dry_run else cache
        resolved_qids = wikipedia_qids(cache)
        resolved_wikidata_qids = provided_qids | resolved_qids

        print("➡️ FetchResolvedWikidata...")
        cache = fetch_wikidata_entities(resolved_wikidata_qids, cache, headers, args.dry_run,
            args.cache_file, phase_name="FetchResolvedWikidata", )

        if args.dry_run:
            print("\n✅ Dry run complete. No HTTP requests were sent and no files were written.")
            return

        save_cache(cache, args.cache_file)

        wikipedia_entries = cache["wikipedia"]
        wikidata_entries = cache["wikidata"]
        wikipedia_count = len(wikipedia_entries)
        wikidata_count = len(wikidata_entries)
        disambiguation_count = sum(
            1 for entry in wikipedia_entries.values() if entry.get("disambiguation"))
        candidate_count = sum(
            len(entry.get("disambiguation_links", [])) for entry in wikipedia_entries.values()
            if entry.get("disambiguation"))

        job_elapsed = time.monotonic() - job_started
        print(f"\n✅ CacheWikipedia complete: {args.cache_file}")
        print(f"   - Wikipedia entries            : {wikipedia_count:,}")
        print(f"   - Wikidata entities            : {wikidata_count:,}")
        print(f"   - Disambiguation pages         : {disambiguation_count:,}")
        print(f"   - Stored disambiguation matches: {candidate_count:,}")
        print(f"   - Elapsed                      : {_format_elapsed(job_elapsed)}")

    except ValueError as exc:
        sys.exit(f"❌ ERROR: {exc}")


if __name__ == "__main__":
    main()
