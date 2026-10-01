from __future__ import annotations

from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import time
from typing import Any, Dict, Iterable, Iterator, List, Set, Tuple

import requests
from tqdm import tqdm

API_URL_TEMPLATE = "https://{lang}.wikipedia.org/w/api.php"
WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"

CACHE_SCHEMA_VERSION = 2
BATCH_SIZE = 50
REQUEST_DELAY_SECONDS = 1.0
DISAMBIGUATION_REQUEST_INTERVAL_SECONDS = 0.20
PROGRESS_INTERVAL_SECONDS = 30.0
CACHE_CHECKPOINT_BATCHES = 25
DISAMBIGUATION_CHECKPOINT_SECONDS = 30.0
MAX_DISAMBIGUATION_LINKS = 500
MAX_SEARCH_RESULTS = 20
MAX_SEARCH_CANDIDATES = 5
MIN_SEARCH_TITLE_SIMILARITY = 0.88
SEARCH_CACHE_VERSION = 3

RETRYABLE_STATUS_CODES = {429, 503}
DEFAULT_RETRY_SECONDS = 5.0
MAX_RETRIES = 5
REQUEST_TIMEOUT_SECONDS = 30

WikipediaEntry = Dict[str, object]
WikidataEntry = Dict[str, object]
CacheType = Dict[str, object]


def new_cache() -> CacheType:
    """Create an empty cache using the current schema."""
    return {
        "schema_version": CACHE_SCHEMA_VERSION, "wikipedia": {}, "wikidata": {},
    }


def _wikipedia_cache(cache: CacheType) -> Dict[str, WikipediaEntry]:
    value = cache["wikipedia"]
    if not isinstance(value, dict):
        raise ValueError("Cache 'wikipedia' section must be a mapping")
    return value


def _wikidata_cache(cache: CacheType) -> Dict[str, WikidataEntry]:
    value = cache["wikidata"]
    if not isinstance(value, dict):
        raise ValueError("Cache 'wikidata' section must be a mapping")
    return value


def load_cache(cache_path: Path) -> CacheType:
    """
    Load the shared raw Wikipedia/Wikidata cache.
    """
    if not cache_path.exists():
        return new_cache()

    try:
        with cache_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except OSError as exc:
        raise ValueError(f"Unable to read Wikipedia cache: {cache_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Wikipedia cache root must be a mapping")

    schema_version = data.get("schema_version")
    if schema_version != CACHE_SCHEMA_VERSION:
        raise ValueError(f"Unsupported Wikipedia cache schema {schema_version!r}; "
                         f"expected {CACHE_SCHEMA_VERSION}. Delete the old cache and rebuild it.")

    if "wikipedia" not in data or "wikidata" not in data:
        raise ValueError("Cache schema v2 requires 'wikipedia' and 'wikidata' sections")

    _wikipedia_cache(data)
    _wikidata_cache(data)
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


def save_cache(cache: CacheType, cache_path: Path) -> None:
    """Atomically save the raw cache only when its serialized content changed.

    The cache is serialized deterministically to a temporary file first. If that
    file is byte-for-byte identical to the existing cache, the temporary file is
    removed and the cache itself is left untouched so its modification time is
    preserved. Otherwise the temporary file atomically replaces the cache.
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")

    try:
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(cache, file, indent=2, sort_keys=True, ensure_ascii=False)
            file.write("\n")

        if cache_path.exists() and _files_identical(cache_path, temp_path):
            temp_path.unlink()
            return

        temp_path.replace(cache_path)

    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class Wikipedia:
    """Own Wikipedia acquisition and the shared on-disk cache.

    ``lookup_many(keys)`` is the normal bulk acquisition interface. It accepts an
    arbitrary iterable of cache keys, normalizes cache identity internally, checks
    cached entries immediately, and batches uncached exact-title requests. Callers
    never need to know either the cache-key representation or Wikimedia API batch
    size.

    ``lookup(key)`` remains as a convenience operation for isolated lookups.
    Redirected and resolved Wikipedia titles remain value metadata; they never
    replace the normalized lookup identity.

    Cache changes are kept in memory. Disk writes occur only at configured
    checkpoints, when ``save()`` is explicitly called, or during the explicit
    one-time ``migrate_cache_keys()`` operation.
    """

    def __init__(
            self, cache_file: Path, headers: Dict[str, str], *, dry_run: bool = False,
            fallback_search: bool = False, trace_titles: Set[str] | None = None, ) -> None:
        self._cache_file = Path(cache_file).expanduser().resolve()
        self._cache: CacheType = {}
        self._headers = headers
        self._dry_run = dry_run
        self._fallback_search = fallback_search
        self._trace_titles = trace_titles or set()
        self._last_search_request: float | None = None
        self._last_disambiguation_request: float | None = None
        self._completed_exact_batches = 0
        self._fallback_warning_emitted = False
        self._logical_lookups = 0
        self._cache_hits = 0
        self._exact_api_lookups = 0
        self._exact_http_batches = 0
        self._fallback_search_requests = 0
        self._disambiguation_requests = 0

    def load(self) -> None:
        """Load the private cache from this instance's configured cache file."""
        self._cache = load_cache(self._cache_file)

    def save(self) -> None:
        """Persist the private cache at an explicit checkpoint or job boundary."""
        if not self._dry_run:
            save_cache(self._cache, self._cache_file)

    def cached_wikipedia(
            self, lang: str, requested_title: str,
    ) -> tuple[str, WikipediaEntry | None]:
        """Return one cached Wikipedia entry using canonical cache identity.

        This is a read-only cache operation. It never performs HTTP requests and
        never mutates the cache.

        Args:
            lang: Wikipedia language code.
            requested_title: Logical Wikipedia title supplied by the caller.

        Returns:
            The canonical cache key and cached entry, or the canonical cache key
            and ``None`` when the title is not present.
        """
        cache_key = self._normalize_key(f"{lang}:{requested_title}")
        entry = _wikipedia_cache(self._cache).get(cache_key)
        if entry is None:
            return cache_key, None
        if not isinstance(entry, dict):
            raise ValueError(f"Cache entry must be a mapping: {cache_key}")
        return cache_key, entry

    def cached_wikidata(self, qid: str) -> WikidataEntry | None:
        """Return one cached Wikidata entity by QID.

        This is a read-only cache operation. It never performs HTTP requests and
        never mutates the cache.
        """
        if not isinstance(qid, str) or not qid.startswith("Q") or not qid[1:].isdigit():
            raise ValueError(f"Invalid Wikidata ID: {qid!r}")

        entry = _wikidata_cache(self._cache).get(qid)
        if entry is None:
            return None
        if not isinstance(entry, dict):
            raise ValueError(f"Wikidata cache entry must be a mapping: {qid}")
        return entry

    def cached_wikipedia_for_wikidata(
            self, qid: str, *, lang: str = "en",
    ) -> tuple[str, WikipediaEntry | None]:
        """Resolve a cached Wikidata QID through its cached Wikipedia sitelink.

        The Wikidata entity must already be present in the cache. For English,
        ``enwiki_title`` is used. Other languages are not currently represented
        in the Wikidata cache schema, so only ``en`` is supported.

        This is a read-only cache operation. It never performs HTTP requests and
        never mutates the cache.

        Args:
            qid: Wikidata entity ID.
            lang: Wikipedia language code. Only ``en`` is currently supported.

        Returns:
            The canonical Wikipedia cache key and cached Wikipedia entry. If the
            Wikidata entity, sitelink, or Wikipedia page is absent, returns an
            empty key when no sitelink exists, otherwise the canonical key and
            ``None``.
        """
        if lang != "en":
            raise ValueError(
                "cached_wikipedia_for_wikidata currently supports only lang='en' "
                "because the cache schema stores only enwiki_title"
            )

        wikidata_entry = self.cached_wikidata(qid)
        if wikidata_entry is None:
            return "", None

        enwiki_title = wikidata_entry.get("enwiki_title")
        if not enwiki_title:
            return "", None
        if not isinstance(enwiki_title, str):
            raise ValueError(f"Wikidata enwiki_title must be text: {qid}")

        return self.cached_wikipedia(lang, enwiki_title)

    @staticmethod
    def _title_case_score(key: str) -> int:
        """Return a simple title-case score for a Wikipedia lookup key.

        The language prefix is ignored. A point is awarded for each alphabetic
        word whose first alphabetic character is uppercase. Words are separated
        by whitespace or hyphens. This intentionally measures ordinary proper-name
        capitalization only; internal capitalization is already handled by cache
        key normalization.

        Args:
            key: Wikipedia cache key in ``lang:title`` form.

        Returns:
            Number of words beginning with an uppercase alphabetic character.
        """
        _, title = Wikipedia._split_key(key)
        base_title = re.sub(r"\s*\([^()]*\)\s*$", "", title).strip()

        score = 0
        at_word_start = True
        for char in base_title:
            if char.isalpha():
                if at_word_start and char.isupper():
                    score += 1
                at_word_start = False
            elif char in {" ", "-"}:
                at_word_start = True

        return score

    @staticmethod
    def _merge_missing_entries(entries: list[WikipediaEntry]) -> WikipediaEntry | None:
        """Merge compatible historical missing entries.

        Missing entries may differ only because the same lookup was previously
        cached with different title capitalization or because one record later
        acquired additional fallback-search metadata. Case-only differences in
        ``resolved_title`` are therefore equivalent, and fields present in only
        one record are preserved. If two records contain different values for the
        same field, the entries are genuinely incompatible and cannot be merged.

        Args:
            entries: Missing Wikipedia cache entries sharing one normalized key.

        Returns:
            A merged entry when all data is compatible, otherwise ``None``.
        """
        if not entries:
            raise ValueError("Cannot merge an empty missing-entry group")

        merged = dict(entries[0])
        absent = object()

        for entry in entries[1:]:
            merged_title = merged.get("resolved_title")
            entry_title = entry.get("resolved_title")
            if (not isinstance(merged_title, str) or not isinstance(entry_title,
                                                                    str) or
                    merged_title.casefold() != entry_title.casefold()):
                return None

            for field in set(merged) | set(entry):
                if field == "resolved_title":
                    continue

                merged_value = merged.get(field, absent)
                entry_value = entry.get(field, absent)
                if merged_value is absent:
                    merged[field] = entry_value
                elif entry_value is absent or merged_value == entry_value:
                    continue
                else:
                    return None

        return merged

    def migrate_cache_keys(self) -> dict[str, int]:
        """Normalize existing Wikipedia cache keys and save the migrated cache.

        Migration policy:
        - One entry for a normalized key is kept.
        - Multiple identical entries are collapsed.
        - Compatible missing entries are merged. Case-only differences in
          ``resolved_title`` are ignored, and non-conflicting metadata present in
          only one entry is preserved.
        - If a normalized key has historical missing entries and exactly one
          successful entry, the successful entry is kept.
        - Multiple successful entries sharing the same non-null Wikidata QID are
          treated as the same entity. Volatile acquisition fields such as page
          length do not participate in identity.
        - If successful entries genuinely differ and exactly one is a
          disambiguation page, the disambiguation entry is kept so downstream
          matching can resolve the intended entity.
        - As the lowest-priority selector, if ordinary capitalization is the
          remaining distinction, prefer the original lookup key with the strongest
          title-case signal. This is useful for named geographic features, where
          proper-name capitalization is preferred over a generic concept.
        - Anything still ambiguous is accumulated as a conflict, logged with
          useful identifying data, and causes the migration to fail.

        The migration is transactional with respect to the in-memory cache: a
        replacement mapping is built separately and is assigned and saved only
        when no conflicting successful entries are found.
        """
        wikipedia_cache = _wikipedia_cache(self._cache)
        original_count = len(wikipedia_cache)

        grouped_entries: dict[str, list[tuple[str, WikipediaEntry]]] = {}
        keys_changed = 0

        for original_key, entry in wikipedia_cache.items():
            normalized_key = self._normalize_key(original_key)
            if normalized_key != original_key:
                keys_changed += 1
            grouped_entries.setdefault(normalized_key, []).append((original_key, entry))

        normalized_cache: Dict[str, WikipediaEntry] = {}
        duplicates_collapsed = 0
        missing_entries_discarded = 0
        successful_over_missing = 0
        conflicts: list[dict[str, object]] = []

        for normalized_key, group in grouped_entries.items():
            if len(group) == 1:
                normalized_cache[normalized_key] = group[0][1]
                continue

            first_entry = group[0][1]
            if all(entry == first_entry for _, entry in group[1:]):
                normalized_cache[normalized_key] = first_entry
                duplicates_collapsed += len(group) - 1
                continue

            successful = [(original_key, entry) for original_key, entry in group if
                not bool(entry.get("missing"))]
            missing = [(original_key, entry) for original_key, entry in group if
                bool(entry.get("missing"))]

            if len(successful) == 1:
                normalized_cache[normalized_key] = successful[0][1]
                discarded = len(group) - 1
                duplicates_collapsed += discarded
                missing_entries_discarded += len(missing)
                successful_over_missing += 1
                continue

            if len(successful) == 0:
                merged_missing = self._merge_missing_entries([entry for _, entry in missing])
                if merged_missing is not None:
                    normalized_cache[normalized_key] = merged_missing
                    duplicates_collapsed += len(group) - 1
                    continue

                conflicts.append({
                    "normalized_key": normalized_key, "entries": group,
                    "reason": "incompatible missing entries",
                })
                continue

            successful_values = [entry for _, entry in successful]
            if all(entry == successful_values[0] for entry in successful_values[1:]):
                normalized_cache[normalized_key] = successful_values[0]
                duplicates_collapsed += len(group) - 1
                missing_entries_discarded += len(missing)
                if missing:
                    successful_over_missing += 1
                continue

            qids = {str(entry["wikidata_id"]) for _, entry in successful if
                entry.get("wikidata_id")}
            if len(qids) == 1 and all(entry.get("wikidata_id") for _, entry in successful):
                normalized_cache[normalized_key] = successful[0][1]
                duplicates_collapsed += len(group) - 1
                missing_entries_discarded += len(missing)
                if missing:
                    successful_over_missing += 1
                continue

            disambiguation_entries = [(original_key, entry) for original_key, entry in successful if
                bool(entry.get("disambiguation"))]
            if len(disambiguation_entries) == 1:
                normalized_cache[normalized_key] = disambiguation_entries[0][1]
                duplicates_collapsed += len(group) - 1
                missing_entries_discarded += len(missing)
                if missing:
                    successful_over_missing += 1
                continue

            scored_successful = [(self._title_case_score(original_key), original_key, entry) for
                original_key, entry in successful]
            best_score = max(score for score, _, _ in scored_successful)
            title_case_winners = [(original_key, entry) for score, original_key, entry in
                scored_successful if score == best_score]
            if best_score > 0 and len(title_case_winners) == 1:
                normalized_cache[normalized_key] = title_case_winners[0][1]
                duplicates_collapsed += len(group) - 1
                missing_entries_discarded += len(missing)
                if missing:
                    successful_over_missing += 1
                continue

            conflicts.append({
                "normalized_key": normalized_key, "entries": successful,
                "reason": "differing successful entries",
            })

        if conflicts:
            print(f"❌ Wikipedia cache key migration found "
                  f"{len(conflicts)} unresolved normalized-key conflict(s).")

            diagnostic_fields = ("resolved_title", "wikidata_id", "disambiguation", "missing",)

            for conflict in conflicts:
                print(f"   - {conflict['normalized_key']!r}: "
                      f"{conflict['reason']}")
                entries = conflict["entries"]
                for original_key, entry in entries:
                    details = ", ".join(
                        f"{field}={entry.get(field)!r}" for field in diagnostic_fields)
                    print(f"       {original_key!r}: {details}")

            raise ValueError("Wikipedia cache key migration failed because unresolved "
                             "normalized-key conflicts remain. Cache was not modified or saved.")

        self._cache["wikipedia"] = normalized_cache
        self.save()

        return {
            "original_keys": original_count, "normalized_keys": len(normalized_cache),
            "keys_changed": keys_changed, "duplicates_collapsed": duplicates_collapsed,
            "missing_entries_discarded": missing_entries_discarded,
            "successful_over_missing": successful_over_missing, "conflicts_found": 0,
        }

    @classmethod
    def _normalize_key(cls, key: str) -> str:
        """Return the private canonical identity used for Wikipedia cache access.

        A trailing parenthetical qualifier is ignored when deciding whether title
        capitalization is significant. Ordinary word-initial capitalization is
        normalized away. If the base title contains an uppercase alphabetic
        character anywhere other than the first alphabetic character of a word,
        the original title capitalization is preserved.

        Spaces and hyphens begin new words for this test. This preserves meaningful
        internal capitalization such as ``AppleTree``, ``BlackRock``, ``BioShock``,
        and all-caps names such as ``ARC``, while ordinary title capitalization such
        as ``Black Cat`` normalizes to lowercase.

        Examples:
            ``ARC (disambiguation)`` remains ``ARC (disambiguation)``.
            ``Arc (disambiguation)`` becomes ``arc (disambiguation)``.
            ``BlackRock`` remains ``BlackRock``.
            ``Black Rock`` becomes ``black rock``.
        """
        lang, title = cls._split_key(key)
        base_title = re.sub(r"\s*\([^()]*\)\s*$", "", title).strip()

        at_word_start = True
        preserve_case = False
        for char in base_title:
            if char.isalpha():
                if char.isupper() and not at_word_start:
                    preserve_case = True
                    break
                at_word_start = False
            elif char in {" ", "-"}:
                at_word_start = True

        normalized_title = title if preserve_case else title.lower()
        return f"{lang.lower()}:{normalized_title}"

    def stats_snapshot(self) -> dict[str, int]:
        """Return cumulative acquisition counters for phase-level reporting."""
        return {
            "logical_lookups": self._logical_lookups, "cache_hits": self._cache_hits,
            "exact_api_lookups": self._exact_api_lookups,
            "exact_http_batches": self._exact_http_batches,
            "fallback_search_requests": self._fallback_search_requests,
            "disambiguation_requests": self._disambiguation_requests,
        }

    def lookup(self, key: str) -> WikipediaEntry | None:
        """Convenience wrapper for one lookup; bulk work should use lookup_many()."""
        for _, entry in self.lookup_many([key]):
            return entry
        return None

    def lookup_many(
            self, keys: Iterable[str], ) -> Iterator[tuple[str, WikipediaEntry | None]]:
        """Resolve an arbitrary stream of cache keys using internal API batching.

        Cache hits are yielded as soon as they are encountered. Exact-title misses
        are accumulated by language and requested in batches of ``BATCH_SIZE``.
        The final partial batch for each language is sent only after the input
        iterable is exhausted.

        Fallback ``intitle:`` search is deliberately not part of the bulk protocol.
        When enabled it remains a sequential per-title operation and a warning is
        emitted once so its performance cost is visible.
        """
        if self._fallback_search and not self._fallback_warning_emitted:
            print("⚠️ WARNING: Fallback Wikipedia intitle search is enabled; "
                  "fallback searches are sequential and are not batched.")
            self._fallback_warning_emitted = True

        wikipedia_cache = _wikipedia_cache(self._cache)
        pending_by_lang: dict[str, list[tuple[str, str, str]]] = {}

        for key in keys:
            self._logical_lookups += 1
            lang, title = self._split_key(key)
            cache_key = self._normalize_key(key)
            entry = wikipedia_cache.get(cache_key)

            if entry is not None:
                self._cache_hits += 1
                if title in self._trace_titles:
                    print(f"[lookup trace] {key}: CACHE HIT; HTTP exact lookup skipped.")
                self._complete_entry(cache_key, lang, title, entry)
                yield key, dict(entry)
                continue

            if title in self._trace_titles:
                print(f"[lookup trace] {key}: CACHE MISS; queued for Wikipedia batch.")

            self._exact_api_lookups += 1
            pending = pending_by_lang.setdefault(lang, [])
            pending.append((key, cache_key, title))

            if len(pending) >= BATCH_SIZE:
                batch = pending[:BATCH_SIZE]
                del pending[:BATCH_SIZE]
                yield from self._resolve_exact_batch(lang, batch)

        for lang, pending in pending_by_lang.items():
            if pending:
                yield from self._resolve_exact_batch(lang, pending)

    @staticmethod
    def _split_key(key: str) -> tuple[str, str]:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Wikipedia lookup key must be non-empty text")
        if ":" not in key:
            raise ValueError(f"Wikipedia lookup key must include a language prefix, for example "
                             f"'en:Seattle fault'. Got {key!r}.")
        lang, title = key.split(":", 1)
        lang = lang.strip()
        title = title.strip()
        if not lang or not title:
            raise ValueError(f"Invalid Wikipedia lookup key: {key!r}")
        return lang, title

    def _resolve_exact_batch(
            self, lang: str, batch: list[tuple[str, str, str]], ) -> Iterator[
        tuple[str, WikipediaEntry | None]]:
        """Fetch one physical exact-title batch and yield completed logical results."""
        self._exact_http_batches += 1
        entries = self._fetch_exact_batch(lang, batch)

        for original_key, cache_key, title in batch:
            entry = entries.get(cache_key)
            if entry is not None:
                self._complete_entry(cache_key, lang, title, entry)
                yield original_key, dict(entry)
            else:
                yield original_key, None

        if not self._dry_run:
            self._completed_exact_batches += 1
            if self._completed_exact_batches % CACHE_CHECKPOINT_BATCHES == 0:
                self.save()

    def _fetch_exact_batch(
            self, lang: str, batch: list[tuple[str, str, str]], ) -> dict[
        str, WikipediaEntry | None]:
        """Fetch exact Wikipedia metadata for one API-sized batch of cache misses."""
        api_url = API_URL_TEMPLATE.format(lang=lang)
        titles = [title for _, _, title in batch]
        params = {
            "action": "query", "format": "json",
            "prop": "info|linkshere|extracts|pageprops|coordinates", "titles": "|".join(titles),
            "redirects": 1, "lhlimit": "max", "lhprop": "pageid", "exintro": 1, "explaintext": 1,
            "exchars": 1000,
        }

        if self._dry_run:
            request_url = requests.Request("GET", api_url, params=params).prepare().url
            print(f"   - Would request Wikipedia batch ({len(batch)} titles): {request_url}")
            return {cache_key: None for _, cache_key, _ in batch}

        data = _request_json(api_url, params=params, headers=self._headers)
        query = data["query"]
        pages = query.get("pages", {})
        if not isinstance(pages, dict):
            raise RuntimeError("MediaWiki query.pages must be a mapping when present")

        normalized = query.get("normalized", [])
        redirects = query.get("redirects", [])
        interwiki = query.get("interwiki", [])
        if not isinstance(normalized, list):
            raise RuntimeError("MediaWiki query.normalized must be a list when present")
        if not isinstance(redirects, list):
            raise RuntimeError("MediaWiki query.redirects must be a list when present")
        if not isinstance(interwiki, list):
            raise RuntimeError("MediaWiki query.interwiki must be a list when present")

        resolved_titles = {title: title for title in titles}
        for item in normalized:
            if not isinstance(item, dict):
                raise RuntimeError("Invalid MediaWiki normalized-title entry")
            source = item.get("from")
            target = item.get("to")
            if not isinstance(source, str) or not isinstance(target, str):
                raise RuntimeError("MediaWiki normalized-title entry requires from/to text")
            for requested_title, current_title in list(resolved_titles.items()):
                if current_title == source:
                    resolved_titles[requested_title] = target

        for item in redirects:
            if not isinstance(item, dict):
                raise RuntimeError("Invalid MediaWiki redirect entry")
            source = item.get("from")
            target = item.get("to")
            if not isinstance(source, str) or not isinstance(target, str):
                raise RuntimeError("MediaWiki redirect entry requires from/to text")
            for requested_title, current_title in list(resolved_titles.items()):
                if current_title == source:
                    resolved_titles[requested_title] = target

        pages_by_title: dict[str, dict[str, object]] = {}
        for page_data in pages.values():
            if not isinstance(page_data, dict):
                raise RuntimeError("MediaWiki page data must be a mapping")
            page_title = page_data.get("title")
            if not isinstance(page_title, str):
                raise RuntimeError("MediaWiki page title must be text")
            pages_by_title[page_title] = page_data

        interwiki_titles = {item["title"] for item in interwiki if
            isinstance(item, dict) and isinstance(item.get("title"), str)}

        wikipedia_cache = _wikipedia_cache(self._cache)
        results: dict[str, WikipediaEntry | None] = {}

        for original_key, cache_key, requested_title in batch:
            resolved_title = resolved_titles[requested_title]
            page_data = pages_by_title.get(resolved_title)

            if page_data is None:
                if resolved_title not in interwiki_titles:
                    raise RuntimeError("MediaWiki returned no page or interwiki result for "
                                       f"lookup key {original_key!r} (resolved as "
                                       f"{resolved_title!r})")
                entry = _missing_wikipedia_entry(resolved_title)
            elif "missing" in page_data:
                entry = _missing_wikipedia_entry(resolved_title)
            else:
                page_props = page_data.get("pageprops", {})
                coordinates = page_data.get("coordinates") or []
                primary_coordinate = coordinates[0] if coordinates else {}
                entry = {
                    "resolved_title": resolved_title, "wikipedia_length": page_data["length"],
                    "wikipedia_links": (
                        len(page_data["linkshere"]) if "linkshere" in page_data else 0),
                    "summary": page_data["extract"] if "extract" in page_data else "",
                    "disambiguation": "disambiguation" in page_props, "disambiguation_links": [],
                    "wikipedia_lat": primary_coordinate.get("lat"),
                    "wikipedia_lon": primary_coordinate.get("lon"),
                    "wikidata_id": page_props.get("wikibase_item"), "missing": False,
                }

            wikipedia_cache[self._normalize_key(cache_key)] = entry
            results[self._normalize_key(cache_key)] = entry

        return results

    def _complete_entry(
            self, key: str, lang: str, title: str, entry: WikipediaEntry, ) -> None:
        """Run optional non-batched follow-up acquisition for one exact result."""
        if entry.get("missing") and self._fallback_search:
            self._ensure_search(key, lang, title, entry)
        elif entry.get("disambiguation") and not entry.get("disambiguation_links"):
            self._ensure_disambiguation_links(key, lang, entry)

    def _ensure_search(
            self, key: str, lang: str, title: str, entry: WikipediaEntry, ) -> None:
        if (entry.get("search_complete") and entry.get("search_version") == SEARCH_CACHE_VERSION):
            if title in self._trace_titles:
                print(f"[lookup trace] {key}: cached search candidates: "
                      f"{entry.get('search_candidates', [])!r}")
            return

        params = {
            "action": "query", "format": "json", "list": "search",
            "srsearch": _build_title_search_query(title), "srnamespace": 0,
            "srlimit": MAX_SEARCH_RESULTS, "srprop": "",
        }
        if self._dry_run:
            print(f"   - Would search Wikipedia ({lang}) for missing key: {key!r}")
            return

        self._last_search_request = _throttle_request(self._last_search_request,
            REQUEST_DELAY_SECONDS)
        self._fallback_search_requests += 1
        data = _request_json(API_URL_TEMPLATE.format(lang=lang), params=params,
            headers=self._headers, )
        results = data.get("query", {}).get("search")
        if not isinstance(results, list) or not all(
                isinstance(result, dict) and isinstance(result.get("title"), str) for result in
                results):
            raise ValueError(f"Invalid Wikipedia search response for {key}")

        result_titles = [result["title"] for result in results]
        plausible = _plausible_search_titles(title, result_titles)
        candidates = [candidate_title for candidate_title, _ in plausible]
        entry["search_candidates"] = candidates
        entry["search_complete"] = True
        entry["search_version"] = SEARCH_CACHE_VERSION

        print(f"   - Search {key}: {len(candidates)} plausible candidate(s) "
              f"from {len(result_titles)} title result(s)")

    def _ensure_disambiguation_links(
            self, key: str, lang: str, entry: WikipediaEntry, ) -> None:
        resolved_title = str(entry["resolved_title"])
        api_url = API_URL_TEMPLATE.format(lang=lang)
        links: set[str] = set()
        continuation: dict[str, object] = {}

        while len(links) < MAX_DISAMBIGUATION_LINKS:
            params: dict[str, object] = {
                "action": "query", "format": "json", "prop": "links", "titles": resolved_title,
                "redirects": 1, "plnamespace": 0, "pllimit": "max",
            }
            params.update(continuation)

            if self._dry_run:
                request_url = requests.Request("GET", api_url, params=params).prepare().url
                print(f"   - Would request disambiguation links for {key!r}: {request_url}")
                return

            self._last_disambiguation_request = _throttle_request(self._last_disambiguation_request,
                DISAMBIGUATION_REQUEST_INTERVAL_SECONDS, )
            self._disambiguation_requests += 1
            data = _request_json(api_url, params=params, headers=self._headers)
            query = data["query"]
            pages = query.get("pages", {})
            if not isinstance(pages, dict):
                raise RuntimeError("MediaWiki query.pages must be a mapping when present")

            for page_data in pages.values():
                page_links = page_data.get("links", [])
                if not isinstance(page_links, list):
                    raise RuntimeError("MediaWiki page.links must be a list when present")
                for item in page_links:
                    if not isinstance(item, dict):
                        raise RuntimeError("Invalid MediaWiki page link")
                    if item.get("ns") != 0:
                        continue
                    candidate_title = item["title"]
                    if not isinstance(candidate_title, str) or not candidate_title:
                        raise RuntimeError("MediaWiki link title must be non-empty text")
                    if candidate_title != resolved_title:
                        links.add(candidate_title)
                    if len(links) >= MAX_DISAMBIGUATION_LINKS:
                        break

            continuation = data.get("continue", {})
            if not continuation:
                break
            if not isinstance(continuation, dict):
                raise RuntimeError("MediaWiki continuation metadata must be a mapping")

        entry["disambiguation_links"] = sorted(links)


def _retry_delay_seconds(response: requests.Response) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after is None:
        return DEFAULT_RETRY_SECONDS
    try:
        return max(float(retry_after), 0.0)
    except ValueError:
        return DEFAULT_RETRY_SECONDS


def _request_json(
        url: str, *, params: dict[str, object], headers: dict[str, str], ) -> dict[str, Any]:
    """GET JSON with Wikimedia-friendly retry handling."""
    for attempt in range(1, MAX_RETRIES + 1):
        response = requests.get(url, params=params, headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS, )

        if response.status_code in RETRYABLE_STATUS_CODES:
            if attempt == MAX_RETRIES:
                response.raise_for_status()
            delay = _retry_delay_seconds(response)
            tqdm.write(f"   - Wikimedia returned HTTP {response.status_code}; "
                       f"retrying in {delay:g}s ({attempt}/{MAX_RETRIES})")
            time.sleep(delay)
            continue

        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Wikimedia response must be a JSON object")
        if "error" in data:
            raise RuntimeError(f"Wikimedia API error: {data['error']}")
        return data

    raise RuntimeError("Wikimedia request retry loop exited unexpectedly")


def _missing_wikipedia_entry(resolved_title: str) -> WikipediaEntry:
    """Build the raw-cache representation for a lookup with no Wikipedia article."""
    return {
        "resolved_title": resolved_title, "wikipedia_length": None, "wikipedia_links": None,
        "summary": "", "disambiguation": False, "disambiguation_links": [], "wikipedia_lat": None,
        "wikipedia_lon": None, "wikidata_id": None, "missing": True,
    }


def _checkpoint_cache(
        cache: CacheType, cache_path: Path | None, completed_batches: int, *, dry_run: bool,
        phase: str, ) -> None:
    """Atomically checkpoint cache state after a configured number of batches."""
    if dry_run or cache_path is None:
        return
    if completed_batches % CACHE_CHECKPOINT_BATCHES != 0:
        return

    save_cache(cache, cache_path)


def _normalize_search_title(title: str, *, remove_parenthetical: bool = False) -> str:
    """Normalize an article title for conservative identity comparison.

    Args:
        title: Lookup or candidate article title.
        remove_parenthetical: Whether to remove a trailing parenthetical qualifier.

    Returns:
        A case-insensitive, punctuation-normalized title.
    """
    normalized = title
    if remove_parenthetical:
        normalized = re.sub(r"\s*\([^()]*\)\s*$", "", normalized)
    return " ".join(re.findall(r"[\w]+", normalized.casefold(), flags=re.UNICODE))


def _search_title_similarity(requested_title: str, candidate_title: str) -> float:
    """Return the strongest full-title or base-title similarity score."""
    requested_forms = {_normalize_search_title(requested_title),
        _normalize_search_title(requested_title, remove_parenthetical=True), }
    candidate_forms = {_normalize_search_title(candidate_title),
        _normalize_search_title(candidate_title, remove_parenthetical=True), }
    requested_forms.discard("")
    candidate_forms.discard("")
    return max(
        (SequenceMatcher(None, requested, candidate).ratio() for requested in requested_forms for
        candidate in candidate_forms), default=0.0, )


def _build_title_search_query(title: str) -> str:
    """Build a case-insensitive CirrusSearch title query.

    A trailing parenthetical qualifier is omitted from discovery because the
    corresponding Wikipedia title may not include it. The original full title
    is still used by the conservative similarity filter.

    Args:
        title: Requested Wikipedia title.

    Returns:
        An ``intitle:`` exact-phrase query suitable for ``srsearch``.
    """
    search_title = re.sub(r"\s*\([^()]*\)\s*$", "", title).strip()
    escaped_title = search_title.replace("\\", "\\\\").replace('"', '\\"')
    return f'intitle:"{escaped_title}"'


def _plausible_search_titles(
        requested_title: str, result_titles: List[str], ) -> List[tuple[str, float]]:
    """Select strongly name-matched titles, ordered by similarity and API rank."""
    scored = [(title, _search_title_similarity(requested_title, title)) for title in
        dict.fromkeys(result_titles)]
    plausible = [(title, score) for title, score in scored if score >= MIN_SEARCH_TITLE_SIMILARITY]
    plausible.sort(key=lambda item: item[1], reverse=True)
    return plausible[:MAX_SEARCH_CANDIDATES]


def _throttle_request(last_request_time: float | None, minimum_interval: float) -> float:
    """
    Pace sequential API requests without imposing a fixed delay after every page.

    Returns the monotonic timestamp immediately before the next request. Network
    latency itself counts toward the interval, so a slow request does not incur an
    unnecessary additional sleep.
    """
    now = time.monotonic()
    if last_request_time is not None:
        remaining = minimum_interval - (now - last_request_time)
        if remaining > 0:
            time.sleep(remaining)
    return time.monotonic()


def fetch_wikidata_entities(
        qids: Set[str], cache: CacheType, headers: Dict[str, str], dry_run: bool = False,
        checkpoint_path: Path | None = None, *, phase_name: str = "LookupWikidata", ) -> CacheType:
    """Fetch uncached Wikidata facts and report phase-level cache/API statistics."""
    started = time.monotonic()
    wikidata_cache = _wikidata_cache(cache)
    total_qids = len(qids)
    cached_qids = qids & set(wikidata_cache)
    new_qids = sorted(qids - set(wikidata_cache))
    cache_hits = len(cached_qids)
    missing: list[str] = []
    http_batches = (len(new_qids) + BATCH_SIZE - 1) // BATCH_SIZE if new_qids else 0

    if new_qids:
        print(f"   - {len(new_qids)} new Wikidata entities require lookup.")
        progress = _progress_bar(phase_name, len(new_qids), "entity")

        for i in range(0, len(new_qids), BATCH_SIZE):
            batch = new_qids[i:i + BATCH_SIZE]
            params = {
                "action": "wbgetentities", "format": "json", "ids": "|".join(batch),
                "props": "claims|sitelinks",
            }

            if dry_run:
                request_url = requests.Request("GET", WIKIDATA_API_URL, params=params).prepare().url
                print(f"\n   Wikidata request {i // BATCH_SIZE + 1}:")
                print(f"   {request_url}")
                progress.update(len(batch))
                continue

            data = _request_json(WIKIDATA_API_URL, params=params, headers=headers)
            entities = data["entities"]
            if not isinstance(entities, dict):
                raise RuntimeError("Wikidata 'entities' must be a mapping")

            for qid in batch:
                entity = entities[qid]
                if not isinstance(entity, dict):
                    raise RuntimeError(f"Wikidata entity must be a mapping: {qid}")
                if "missing" in entity:
                    tqdm.write(f"   - ⚠️ Wikidata item does not exist: {qid}")
                    missing.append(qid)
                    if len(missing) > 20:
                        non_existent = ", ".join(missing)
                        raise ValueError(f"Missing over 20 Wikidata entries: {non_existent}")
                    continue

                claims = entity["claims"]
                if not isinstance(claims, dict):
                    raise RuntimeError(f"Wikidata claims must be a mapping: {qid}")

                sitelinks = entity["sitelinks"]
                if not isinstance(sitelinks, dict):
                    raise RuntimeError(f"Wikidata sitelinks must be a mapping: {qid}")

                wikidata_lat, wikidata_lon = _extract_coordinate(claims, "P625")

                wikidata_cache[qid] = {
                    "instance_of": _extract_item_ids(claims, "P31"),
                    "wikidata_sitelink_count": len(sitelinks),
                    "enwiki_title": _extract_enwiki_title(entity), "wikidata_lat": wikidata_lat,
                    "wikidata_lon": wikidata_lon,
                }

            completed_batches = i // BATCH_SIZE + 1
            _checkpoint_cache(cache, checkpoint_path, completed_batches, dry_run=dry_run,
                phase=phase_name, )
            progress.update(len(batch))
            time.sleep(REQUEST_DELAY_SECONDS)

        progress.close()
    else:
        print(f"   - All {total_qids} Wikidata entities are already cached.")

    elapsed = time.monotonic() - started
    overall_rate = (total_qids / elapsed) if elapsed > 0 else 0.0
    _print_phase_summary(phase_name,
        [("Entities examined", f"{total_qids:,}"), ("Cache hits", f"{cache_hits:,}"),
            ("Cache hit rate", f"{_percent(cache_hits, total_qids):.1f}%"),
            ("API lookups", f"{len(new_qids):,}"), ("HTTP batches", f"{http_batches:,}"),
            ("Missing entities", f"{len(missing):,}"), ("Elapsed", _format_elapsed(elapsed)),
            ("Overall rate", f"{overall_rate:,.2f} entities/s"), ], )
    return cache












def _progress_bar(name: str, total: int, unit: str):
    """Create the standard low-noise progress display used by this utility."""
    return tqdm(total=total, desc=name, unit=unit, mininterval=PROGRESS_INTERVAL_SECONDS,
        maxinterval=PROGRESS_INTERVAL_SECONDS, dynamic_ncols=True,
        bar_format="{desc}: {percentage:5.1f}% {n_fmt}/{total_fmt} "
                   "[{elapsed}<{remaining}, {rate_fmt}]", )


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


def _extract_item_ids(claims: Dict[str, object], property_id: str) -> List[str]:
    """Extract item-valued targets for one Wikidata property."""
    property_claims = claims[property_id] if property_id in claims else []
    if not isinstance(property_claims, list):
        raise RuntimeError(f"Wikidata {property_id} claims must be a list")

    item_ids: set[str] = set()
    for claim in property_claims:
        if not isinstance(claim, dict):
            raise RuntimeError(f"Invalid Wikidata {property_id} claim")

        mainsnak = claim["mainsnak"]
        snaktype = mainsnak["snaktype"]
        if snaktype != "value":
            continue

        datavalue = mainsnak["datavalue"]
        if datavalue["type"] != "wikibase-entityid":
            continue

        value = datavalue["value"]
        if value["entity-type"] != "item":
            continue

        item_id = value["id"]
        if (not isinstance(item_id, str) or not item_id.startswith("Q") or not item_id[
            1:].isdigit()):
            raise RuntimeError(f"Invalid Wikidata item ID: {item_id!r}")
        item_ids.add(item_id)

    return sorted(item_ids)


def _extract_coordinate(
        claims: Dict[str, object], property_id: str, ) -> tuple[float | None, float | None]:
    """Extract the first valid globe-coordinate claim for a Wikidata property."""
    property_claims = claims[property_id] if property_id in claims else []
    if not isinstance(property_claims, list):
        raise RuntimeError(f"Wikidata {property_id} claims must be a list")

    for claim in property_claims:
        if not isinstance(claim, dict):
            raise RuntimeError(f"Invalid Wikidata {property_id} claim")

        mainsnak = claim["mainsnak"]
        if not isinstance(mainsnak, dict):
            raise RuntimeError(f"Invalid Wikidata {property_id} mainsnak")
        if mainsnak["snaktype"] != "value":
            continue

        datavalue = mainsnak["datavalue"]
        if not isinstance(datavalue, dict):
            raise RuntimeError(f"Invalid Wikidata {property_id} datavalue")
        if datavalue["type"] != "globecoordinate":
            continue

        value = datavalue["value"]
        if not isinstance(value, dict):
            raise RuntimeError(f"Invalid Wikidata {property_id} coordinate value")

        latitude = value["latitude"]
        longitude = value["longitude"]

        if not isinstance(latitude, (int, float)) or isinstance(latitude, bool):
            raise RuntimeError(f"Invalid Wikidata {property_id} latitude: {latitude!r}")
        if not isinstance(longitude, (int, float)) or isinstance(longitude, bool):
            raise RuntimeError(f"Invalid Wikidata {property_id} longitude: {longitude!r}")

        return float(latitude), float(longitude)

    return None, None


def _extract_enwiki_title(entity: Dict[str, object]) -> str | None:
    """Extract the English Wikipedia sitelink title from a Wikidata entity."""
    sitelinks = entity["sitelinks"]
    if not isinstance(sitelinks, dict):
        raise RuntimeError("Wikidata entity.sitelinks must be a mapping")

    enwiki = sitelinks.get("enwiki")
    if enwiki is None:
        return None
    if not isinstance(enwiki, dict):
        raise RuntimeError("Wikidata enwiki sitelink must be a mapping")

    title = enwiki["title"]
    if not isinstance(title, str) or not title:
        raise RuntimeError("Wikidata enwiki title must be non-empty text")
    return title


def wikipedia_qids(cache: CacheType) -> Set[str]:
    """Collect Wikidata QIDs referenced by cached Wikipedia pages."""
    result: set[str] = set()
    for entry in _wikipedia_cache(cache).values():
        qid = entry.get("wikidata_id")
        if qid:
            result.add(str(qid))
    return result


def wikidata_enwiki_titles(qids: Set[str], cache: CacheType) -> Set[str]:
    """Return English Wikipedia titles exposed by cached Wikidata entities."""
    wikidata_cache = _wikidata_cache(cache)
    titles: set[str] = set()

    for qid in qids:
        entry = wikidata_cache.get(qid)
        if not entry:
            continue
        title = entry.get("enwiki_title")
        if title:
            titles.add(str(title))

    return titles




