# enhance_wikipedia_lengths.py
"""
Fetches Wikipedia article lengths for items.

This script reads a CSV file containing feature names, extracts the Wikipedia
article titles, and uses the MediaWiki API to fetch the byte length of each
article.

Key Features:
- Follows Wikipedia's API Terms of Use (batching, rate-limiting, User-Agent).
- Caches results locally in a JSON file to minimize API calls on subsequent runs.
- Produces an "enhancement" CSV file containing only the id and the new
  'article_length' feature, ready to be merged by other pipeline steps.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Set, Tuple, List
from urllib.parse import unquote, urlparse

import pandas as pd
import requests
from tqdm import tqdm

# API_URL is  a template string
API_URL_TEMPLATE = "https://{lang}.wikipedia.org/w/api.php"
BATCH_SIZE = 50
REQUEST_DELAY_SECONDS = 1
DEFAULT_CACHE_FILE = "wikipedia_cache.json"

def load_cache(cache_path: Path) -> Dict[str, int]:
    """Loads the title-to-length cache from a JSON file."""
    if cache_path.exists():
        with open(cache_path, 'r') as f:
            return json.load(f)
    return {}

def save_cache(cache: Dict[str, int], cache_path: Path) -> None:
    """Saves the cache to a JSON file."""
    with open(cache_path, 'w') as f:
        json.dump(cache, f, indent=2)

def parse_wiki_tag(value: str) -> Tuple[str, str] | None:
    """
    parses a Wikipedia tag, handling URLs, language codes, and fragments.

    Args:
        value (str): The raw string value from the 'wikipedia' tag.

    Returns:
        A tuple of (language_code, article_title) if parsing is successful,
        otherwise None. Defaults to 'en' if no language is specified.
    """
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
        # Simple check for a valid-looking language code
        if 2 <= len(parts[0]) <= 4 and parts[0].isalpha():
            lang = parts[0]
            title = parts[1].split('#')[0].replace('_', ' ')
            return lang, title

    # Default to 'en' if no other language code pattern is found
    title = value.split('#')[0].replace('_', ' ')
    return 'en', title

def fetch_wikipedia_lengths(
        titles: Set[str],
        lang: str,
        cache: Dict[str, int],
        headers: Dict[str, str],
        dry_run: bool = False
) -> Tuple[Dict[str, int], Set[str], List[List[str]]]:
    """
    Fetches article lengths, explicitly handling "not found" and request errors.

    Args:
        titles: A set of article titles to check for the given language.
        lang: The two-letter language code for the Wikipedia instance.
        cache: The current cache of article lengths.
        headers: The HTTP headers to use for the API request.
        dry_run: If True, simulates API calls without executing them.

    Returns:
        A tuple containing:
        - The updated cache including newly fetched lengths.
        - A set of titles that were not found.
        - A list of batches that failed due to request errors.
    """
    updated_cache = cache.copy()
    not_found_titles = set()
    failed_batches = []
    api_url = API_URL_TEMPLATE.format(lang=lang)

    new_titles = list(titles - {k.split(':', 1)[1] for k in cache.keys() if k.startswith(f"{lang}:")})

    if not new_titles:
        print(f"   - All {len(titles)} titles for language '{lang}' found in cache.")
        return updated_cache, not_found_titles, failed_batches

    print(f"   - Found {len(new_titles)} new titles to fetch for language '{lang}' from {api_url}...")

    for i in tqdm(range(0, len(new_titles), BATCH_SIZE), desc=f"     Fetching '{lang}' articles", unit="batch"):
        batch = new_titles[i:i + BATCH_SIZE]
        params = {"action": "query", "format": "json", "prop": "info", "titles": "|".join(batch)}

        if dry_run:
            continue

        try:
            response = requests.get(api_url, params=params, headers=headers)
            # 1) Handle "error with request" (e.g., 500 server error, network issues)
            response.raise_for_status()
            data = response.json()

            pages = data.get("query", {}).get("pages", {})
            for _, page_data in pages.items():
                title = page_data.get("title")
                if not title:
                    continue

                # 2) Distinguish between "not found" and a valid response
                if "missing" in page_data:
                    # Condition 2: Request valid, but page not found
                    tqdm.write(f"   - ℹ️ INFO: Article '{title}' not found on '{lang}'.wikipedia.org.")
                    not_found_titles.add(title)
                elif "length" in page_data:
                    # Condition 1: Request valid, page found
                    length = page_data.get("length", 0)
                    cache_key = f"{lang}:{title}"
                    updated_cache[cache_key] = length

        except requests.RequestException as e:
            # Condition 3: A request-level error occurred
            tqdm.write(f"   - ⚠️ WARNING: API request failed for a batch: {e}. Skipping.")
            failed_batches.append(batch)
            continue

        time.sleep(REQUEST_DELAY_SECONDS)

    if dry_run:
        print("     [DRY RUN] Would have requested all batches.")

    return updated_cache, not_found_titles, failed_batches

def main() -> None:
    """Main script execution."""
    parser = argparse.ArgumentParser(
        description="Extract Wikipedia article lengths."
    )
    parser.add_argument("--input", type=Path, required=True, help="Path to the input features CSV file.")
    parser.add_argument("--output", type=Path, required=True, help="Path for the output enhancement CSV file.")
    parser.add_argument("--cache-file", type=Path, default=DEFAULT_CACHE_FILE, help="Path to the JSON file for caching results.")
    parser.add_argument("--dry-run", action="store_true", help="Perform a dry run without making API calls or writing files.")
    parser.add_argument("--project", type=str, default=None, help="Your project name for the API User-Agent (e.g., 'MyProject/1.0').")
    parser.add_argument("--email", type=str, default=None, help="Your contact email for the API User-Agent.")
    parser.add_argument("--lang", type=str, default='en', help="The Wikipedia language code to process (e.g., 'en', 'fr', 'es'). Defaults to 'en'.")
    parser.add_argument("--id", type=str, default='en', help="The ID column.")

    args = parser.parse_args()

    if not args.project or not args.email:
        print("\n   ❌ ERROR: API compliance requires a User-Agent.")
        print("      Please provide both --project and --email arguments.")
        print("      Example: --project \"MyMap/1.0\" --email \"user@example.com\"")
        sys.exit(1)

    headers = {
        'User-Agent': f"{args.project} (Contact: {args.email})"
    }
    print(f"➡️ Using User-Agent for API requests: '{headers['User-Agent']}'")

    if args.dry_run:
        print("\n--- ❗ Performing a DRY RUN. No files will be written and no API calls will be made. ---\n")

    id_col = args.id_col
    wiki_col = "wikipedia"

    try:
        print(f"➡️ Reading input features from '{args.input}'...")
        df = pd.read_csv(args.input, dtype={id_col: str})

        if id_col not in df.columns or wiki_col not in df.columns:
            raise ValueError(f"Input CSV must contain both '{id_col}' and '{wiki_col}' columns.")

        print("➡️ Validating 'wikipedia' column format...")
        if df[wiki_col].isnull().all():
            print("   - ✅ Validation OK: The 'wikipedia' column is present but contains no data to process.")
        else:
            # Drop nulls for a clean check, convert to string just in case
            valid_entries = df[wiki_col].dropna().astype(str)

            # Check for any value that looks purely like a number (e.g., "0", "0.0", "123").
            numeric_like_values = valid_entries[valid_entries.str.match(r'^-?\d+(\.\d+)?$')]

            if not numeric_like_values.empty:
                first_bad_value = numeric_like_values.iloc[0]
                error_message = f"""
   ❌ ERROR: Invalid data format in '{wiki_col}' column.
      - The data contains values that look like numbers, but it should be a string representing a Wikipedia article title.
      - Example bad value found: '{first_bad_value}'
      - Example good value: 'en:Los Angeles' or 'Los Angeles'
      - This is often caused by an upstream process that defaults missing tags to 0.0.
"""
                raise ValueError(error_message)
    except (FileNotFoundError, ValueError) as e:
        print(f"   ❌ ERROR: {e}")
        sys.exit(1)

    print(f"➡️ Loading Wikipedia cache from '{args.cache_file}'...")
    cache = load_cache(args.cache_file)

    print(f"➡️ Parsing Wikipedia tags and filtering for language '{args.lang}'...")
    df['parsed_wiki'] = df[wiki_col].apply(parse_wiki_tag)

    def filter_by_lang(parsed_tuple: Tuple[str, str] | None) -> Tuple[str, str] | None:
        if parsed_tuple and parsed_tuple[0] == args.lang:
            return parsed_tuple
        return None

    df['parsed_wiki'] = df['parsed_wiki'].apply(filter_by_lang)

    titles_to_check = {title for lang, title in df['parsed_wiki'].dropna() if lang == args.lang}

    if not titles_to_check:
        print(f"   - No articles found for the specified language '{args.lang}'.")
    else:
        updated_cache, not_found, failed = fetch_wikipedia_lengths(
            titles_to_check,
            lang='en',
            cache=cache,
            headers=headers
        )

        if not_found:
            print("\nThe following articles could not be found:")
            for title in sorted(list(not_found)):
                print(f"  - {title}")

        if failed:
            print("\nThe following batches failed to process due to network/API errors:")
            for i, batch in enumerate(failed):
                print(f"  - Batch #{i+1}: {', '.join(batch[:3])}...")

        cache = updated_cache

    print("➡️ Generating enhancement file...")
    def get_length_from_parsed(parsed_tuple: Tuple[str, str] | None) -> int:
        if not parsed_tuple:
            return 0
        lang, title = parsed_tuple
        return cache.get(f"{lang}:{title}", 0)

    df['article_length'] = df['parsed_wiki'].apply(get_length_from_parsed)

    output_df = df[[id_col, 'article_length']]

    if not args.dry_run:
        print(f"➡️ Saving updated cache to '{args.cache_file}'...")
        save_cache(cache, args.cache_file)

        print(f"➡️ Saving enhancement file to '{args.output}'...")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        output_df.to_csv(args.output, index=False)

        print(f"\n✅ Enhancement file with {len(output_df)} records saved to '{args.output}'.")
    else:
        print("\n[DRY RUN] Skipped saving cache and output file.")
        print(f"[DRY RUN] Would have generated {len(output_df)} records.")

if __name__ == "__main__":
    main()