
# Generic Geospatial Fuzzy Join Utility (`geo_fuzzy.py`)

## 1. Purpose & Benefits

This script solves a common and difficult problem in data pipelines: **joining two different geographic datasets that do not share a common, unique ID.**

This utility finds the best match for records by using a combination of **spatial proximity** and **text similarity.** for datasets without a shared key.
It then outputs data from the auxiliary file with the matching key from the master file.
Both datasets must have a name column and location (lat/lon).  The master dataset must have a unique key column.

### Core Benefits:

*   **Enrich Your Master Dataset:** Allows you to enrich a large, comprehensive master dataset with valuable information from smaller, specialized external 
datasets (e.g., health inspection scores,  business listings, government records).
*   **High Performance:** Uses a combination of an R-tree spatial index for fast proximity searching and the high-performance `rapidfuzz` library for text
matching, allowing it to process millions of records efficiently.
*   **Robust & Configurable:** Handles variations in the name field by cleaning text and removing "noise words." All key matching parameters (search radius, 
similarity threshold) are controlled via a simple YAML configuration file.
*   **Pipeline-Ready:** Designed as a standard command-line tool that can be easily integrated as a step in a larger data processing pipeline.

---

## 2. How It Works: 

1.  **Master File Spatial Index:** The script begins by loading every record from the **master file** into a high-performance R-tree spatial index in memory. 
This creates a virtual grid that provides fast geographic lookups.

2.  **Location Search:** It then iterates through each record in the  **auxiliary file**. For each auxiliary record, it uses the spatial 
index to find all master records that fall within a circle of `search_radius_meters`. This produces a 
short list of nearby master dataset nodes (candidates).

3.  **Fuzzy Text Match:** It compares the name of the auxiliary record to the name of each spatial candidate. It cleans both names (removing 
noise words and punctuation) and calculates a text similarity score (0-100). The candidate with the highest score above a configured `name_similarity_threshold` 
is declared the  match.

4. **Output:** The script outputs a new CSV file containing one row for each successful match with the following:
    *   `id`: The unique ID from the matched record in the master file.
    *   All columns specified in the `aux_columns_to_keep` list from the configuration.
---

## 3. Usage

The script is run from the command line, providing paths to the two datasets, a configuration file, and the desired output file.

### Choosing Your Input Files:

*   `--master-file`: This should be your **larger, more comprehensive dataset** (e.g., your full OSM extract). The script will build its spatial index from this file.
*   `--aux-file`: This should be your **smaller, specialized dataset** that you want to match against the master (e.g.,  County health ratings). The script will 
iterate through this file.

### Command-Line Syntax:

```bash
python geo_fuzzy.py \
    --master-file /path/to/osm_extract.csv \
    --aux-file /path/to/health_ratings.csv \
    --config /path/to/match_config.yml \
    --output /path/to/matched_scores.csv \
```

### Command-Line Arguments:

| Argument        | Required | Description                                                           |
|:----------------|:---------|:----------------------------------------------------------------------|
| `--master-file` | Yes      | Path to the master CSV file to be indexed (e.g., OSM data).           |
| `--aux-file`    | Yes      | Path to the auxiliary CSV file to be matched against the master.      |
| `--config`      | Yes      | Path to the YAML configuration file that controls the matching logic. |
| `--output`      | Yes      | Path where the final matched enrichment CSV will be saved.            |
| `--explain`     | Optional | Generates an output CSV for tuning parameters                         |


---

## 4. Configuration File

The script's behavior is controlled by a YAML configuration file.

**Example `match_config.yml`:**
```yaml
config_type: "FuzzyMatch"

name_column: item_name
id_column: osm_id
search_radius_meters: 150
name_similarity_threshold: 85

# A list of data columns from the auxiliary file to include in the final output.
aux_columns_to_keep:
  - "health_score"
  - "inspection_date"

# (Optional) A list of common words to remove from names before comparison.
noise_words:
  - "the"
  - "north"
  - "south"
```

---

## 5. Input & Output File Formats

### Input File Requirements:

*   **Master File (`--master-file`):**
    *   Must be a CSV file.
    *   Must contain the following columns: `id`, `item_name`, `lon`, `lat`. (The `id` column, e.g., `osm_id`, is the unique identifier that will be used in the output).
*   **Auxiliary File (`--aux-file`):**
    *   Must be a CSV file.
    *   Must contain the following columns: `item_name`, `lon`, `lat`.
    *   Must also contain any data columns listed in the `aux_columns_to_keep` section of the config file.

### Output File Format:

The script produces a new CSV file containing one row for each successful match.

*   **Columns:**
    *   `id`: The unique ID from the matched record in the master file.
    *   All columns specified in the `aux_columns_to_keep` list from the configuration.

**Example Output (`matched_scores.csv`):**
```csv
id,health_score,inspection_date
1234567,10.0,"2025-08-15"
7654321,-20.0,"2025-07-22"
...
```

Test Results (at best threshold)

| Test             | Threshold | "Match" Err   | "Not Match" Err  |
|------------------|----------:|--------------:|-----------------:|
| harmonic noise   |        68 |             4 |                3 |
| harmonic partial |        69 |             2 |                3 |
| combined         |        69 |             2 |                3 |