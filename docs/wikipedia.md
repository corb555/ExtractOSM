
# Wikipedia Length Fetcher

A  utility that enriches a dataset with the **byte length** of associated Wikipedia articles.

This metric can be used as a proxy for "importance" or "popularity" in ranking algorithms (e.g., distinguishing 
a major landmark with a 50kb article from a local park with a 2kb stub).

## Features

*   **API Compliance:** Respects Wikipedia's User-Agent, batching, and rate-limiting policies.
*   **Caching:** Uses a local JSON cache (`wikipedia_cache.json`) to store results, ensuring subsequent runs are instant and 
do not hammer the API.
*   **Smart Parsing:** Handles raw tags like `en:Mount Rainier`, `https://en.wikipedia.org/wiki/Mount_Rainier`, or just 
`Mount Rainier`.
*   **Pipeline Ready:** Outputs a lean "enhancement" CSV (ID + Length) designed to be joined back to your main dataset.

## Usage

```bash
python enhance_wikipedia_lengths.py \
  --input data/features.csv \
  --output data/wiki_lengths.csv \
  --id osm_id \
  --project "MyMapProject/1.0" \
  --email "me@example.com"
```

### Arguments

| Argument    | Description                                                                                      |
|:------------|:-------------------------------------------------------------------------------------------------|
| `--input`   | Path to the source CSV containing the features. Must have an ID column and a `wikipedia` column. |
| `--output`  | Path where the resulting enhancement CSV will be saved.                                          |
| `--id`      | The name of the unique ID column in your CSV (e.g., `osm_id`).                                   |
| `--project` | Your project name (Required for API User-Agent).                                                 |
| `--email`   | Your contact email (Required for API User-Agent).                                                |
| `--lang`    | (Optional) Language code to target (default: `en`).                                              |

## Output Format

The output file will contain exactly two columns: the original ID and the new length.

| osm_id   | article_length  |
|:---------|:----------------|
| 349483   | 45201           |
| 882910   | 1204            |
| 110239   | 0               |

*(0 indicates the article was not found or had no Wikipedia tag).*