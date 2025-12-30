# wikipedia_props.py
"""
Fetches Wikipedia article lengths and incoming link counts for items.

This script reads a CSV file containing feature names, extracts the Wikipedia
article titles, and uses the MediaWiki API to fetch:
1. Byte length of the article (proxy for depth/detail).
2. Number of incoming links (proxy for importance/connectivity).

Key Features:
- Follows Wikipedia's API Terms of Use (batching, rate-limiting, User-Agent).
- Caches results locally in a JSON file to minimize API calls.
- Produces an "enhancement" CSV file containing id, 'article_length', and 'link_count'.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Set, Tuple, List, Any
from urllib.parse import unquote, urlparse

import pandas as pd
import requests
from tqdm import tqdm

# API_URL is a template string
API_URL_TEMPLATE = "https://{lang}.wikipedia.org/w/api.php"
BATCH_SIZE = 50
REQUEST_DELAY_SECONDS = 1
DEFAULT_CACHE_FILE = "wikipedia_cache.json"

# Cache structure: "lang:Title": {"length": int, "links": int}
CacheType = Dict[str, Dict[str, int]]

def load_cache(cache_path: Path) -> CacheType:
    """Loads the cache from a JSON file."""
    if cache_path.exists():
        with open(cache_path, 'r') as f:
            return json.load(f)
    return {}

def save_cache(cache: CacheType, cache_path: Path) -> None:
    """Saves the cache to a JSON file."""
    with open(cache_path, 'w') as f:
        json.dump(cache, f, indent=2)

def parse_wiki_tag(value: str) -> Tuple[str, str] | None:
    """Parses a Wikipedia tag into (lang, title)."""
    if pd.isna(value):
        return None

    if value.startswith('http'):
        parsed_url = urlparse(value)
        host = parsed_url.netloc
        path = parsed_url.path
        lang = host.split('.')[0]
        title = unquote(path.split('/wiki/')[-1]).split('#')[0].replace('_', ' ')
        return lang, title

    if ':' in value:
        parts = value.split(':', 1)
        if 2 <= len(parts[0]) <= 4 and parts[0].isalpha():
            lang = parts[0]
            title = parts[1].split('#')[0].replace('_', ' ')
            return lang, title

    title = value.split('#')[0].replace('_', ' ')
    return 'en', title

def fetch_wikipedia_props(
        titles: Set[str],
        lang: str,
        cache: CacheType,
        headers: Dict[str, str],
        dry_run: bool = False
) -> Tuple[CacheType, Set[str], List[List[str]]]:
    """
    Fetches article properties (length + link count).
    """
    updated_cache = cache.copy()
    not_found_titles = set()
    failed_batches = []
    api_url = API_URL_TEMPLATE.format(lang=lang)

    # Filter out titles already in cache
    new_titles = list(titles - {k.split(':', 1)[1] for k in cache.keys() if k.startswith(f"{lang}:")})

    if not new_titles:
        print(f"   - All {len(titles)} titles for language '{lang}' found in cache.")
        return updated_cache, not_found_titles, failed_batches

    print(f"   - Found {len(new_titles)} new titles to fetch for language '{lang}'...")

    for i in tqdm(range(0, len(new_titles), BATCH_SIZE), desc=f"     Fetching '{lang}' articles", unit="batch"):
        batch = new_titles[i:i + BATCH_SIZE]

        # Request both info (length) and linkshere (count)
        params = {
            "action": "query",
            "format": "json",
            "prop": "info|linkshere",
            "titles": "|".join(batch),
            "lhlimit": "max",     # Get max links allowed (usually 500 for normal users)
            "lhprop": "pageid"    # We only need to count them, not see titles, but prop needs a value
            # Note: We aren't paging through 'continue'. If >500 links, we accept 500 as "Very Important".
        }

        if dry_run:
            continue

        try:
            response = requests.get(api_url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()

            pages = data.get("query", {}).get("pages", {})
            for _, page_data in pages.items():
                title = page_data.get("title")
                if not title: continue

                if "missing" in page_data:
                    tqdm.write(f"   - ℹ️ INFO: Article '{title}' not found.")
                    not_found_titles.add(title)
                else:
                    length = page_data.get("length", 0)

                    # Count the incoming links returned in this batch
                    # Note: Without paging, this caps at the API limit (typically 500)
                    links = len(page_data.get("linkshere", []))

                    # Store both metrics
                    cache_key = f"{lang}:{title}"
                    updated_cache[cache_key] = {
                        "length": length,
                        "links": links
                    }

        except requests.RequestException as e:
            tqdm.write(f"   - ⚠️ WARNING: API request failed: {e}")
            failed_batches.append(batch)
            continue

        time.sleep(REQUEST_DELAY_SECONDS)

    return updated_cache, not_found_titles, failed_batches

def main() -> None:
    parser = argparse.ArgumentParser(description="Extract Wikipedia article metrics.")
    parser.add_argument("--input", type=Path, required=True, help="Input CSV")
    parser.add_argument("--output", type=Path, required=True, help="Output CSV")
    parser.add_argument("--cache-file", type=Path, default=DEFAULT_CACHE_FILE, help="Cache JSON")
    parser.add_argument("--dry-run", action="store_true", help="Dry run")
    parser.add_argument("--project", type=str, required=True, help="Project name (User-Agent)")
    parser.add_argument("--email", type=str, required=True, help="Email (User-Agent)")
    parser.add_argument("--lang", type=str, default='en', help="Language code")
    parser.add_argument("--id", type=str, default='osm_id', help="ID column")

    args = parser.parse_args()

    headers = {'User-Agent': f"{args.project} (Contact: {args.email})"}
    print(f"➡️ Using User-Agent: '{headers['User-Agent']}'")

    id_col = args.id
    wiki_col = "wikipedia"

    try:
        print(f"➡️ Reading input from '{args.input}'...")
        df = pd.read_csv(args.input, dtype={id_col: str})
        if id_col not in df.columns or wiki_col not in df.columns:
            raise ValueError(f"CSV must contain '{id_col}' and '{wiki_col}'.")
    except Exception as e:
        sys.exit(f"❌ ERROR: {e}")

    # Load Cache
    cache = load_cache(args.cache_file)

    # Parse Tags
    df['parsed_wiki'] = df[wiki_col].apply(parse_wiki_tag)

    # Filter for Lang
    def filter_lang(pt): return pt if pt and pt[0] == args.lang else None
    df['parsed_wiki'] = df['parsed_wiki'].apply(filter_lang)

    # Fetch
    titles_to_check = {title for lang, title in df['parsed_wiki'].dropna()}

    if titles_to_check:
        updated_cache, _, _ = fetch_wikipedia_props(
            titles_to_check, args.lang, cache, headers, args.dry_run
        )
        cache = updated_cache

    # Apply Data
    print("➡️ Generating enhancement columns...")

    def get_metrics(parsed_tuple):
        if not parsed_tuple: return 0, 0
        lang, title = parsed_tuple
        data = cache.get(f"{lang}:{title}", {})
        # Handle legacy cache format (int) vs new format (dict)
        if isinstance(data, int):
            return data, 0 # Old cache had length only
        return data.get("length", 0), data.get("links", 0)

    # Apply returns a DataFrame if result_type='expand' is used, but tuple unpacking is safer here
    metrics = df['parsed_wiki'].apply(get_metrics)

    # Split tuples into columns
    df['article_length'] = metrics.apply(lambda x: x[0])
    df['link_count'] = metrics.apply(lambda x: x[1])

    output_df = df[[id_col, 'article_length', 'link_count']]

    if not args.dry_run:
        save_cache(cache, args.cache_file)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        output_df.to_csv(args.output, index=False)
        print(f"\n✅ Saved {len(output_df)} records to '{args.output}'.")

if __name__ == "__main__":
    main()