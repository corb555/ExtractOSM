


# ExtractOSM

## Overview

**ExtractOSM** is a utility that performs a filtered extract of OpenStreetMap (OSM) data into a 
 CSV file. It
filters
OSM nodes based on supplied categories, extracts specified fields, optionally normalizes text, and can enrich the output with
additional attributes.
The result is a dataset for:

* Training and applying classification or regression models
* Feeding structured inputs into GIS platforms or PostGIS databases
---

## 🟢 Features

* Filters user-specified OSM categories using `osmium` for high-performance extraction.
* Extracts a fixed set of **core fields** (e.g., `osm_id`, `item_name`, `lat`, `lon`).
* Extracts a configurable set of **feature fields** from tag keys with these modes:
    * Numeric parsing
    * Binary presence indicator
* Computes additional **derived fields**:
    * `tag_count` — the number of _significant_ tags after applying tag filter rules
* Optionally applies  **text normalization** using regex-based substitutions from YAML configuration.
* Optionally standardizes units for fields such as distance.  This will convert fields tagged as "ft", etc. to meters.
* Supports creating loading **external enrichment data** via CSV files keyed by `osm_id`.
* Outputs a structured **CSV file** for use in machine learning pipelines, PostGIS, or statistical tools.
* Includes an optional tool for finding the wikipedia length of articles for each item
* Includes an optional tool for calculating lat/lon and area for polygons in an OSM file

---

## 🔍 Details

### Core Fields

Core fields are always included in the output:

* `osm_id` — Unique identifier of the OSM node.
* `item_name` — Value of the `name` tag, if present.
* `lat`, `lon` — Coordinates of the node, _if available_.
* `osm_category` — Assigned category based on OSM tags.

---

### Configured Fields

Additional fields beyond the core fields are defined in the `features` section of the YAML config. Each field uses a **mode** to
determine how its value is extracted and placed in the output CSV.

* **`value`** — Interpret the tag value as a numeric `float`.
* **`presence`** — Encodes **presence** of the tag as `1.0` if present, otherwise `0.0`. This acts as a
  **binary indicator variable** (one-hot feature).
* **`score`** — Behaves identically to `presence` during extraction, but may be treated differently in downstream
  workflows.

If a tag is missing, malformed, or cannot be converted to a numeric value, the feature value defaults to `0.0`.

#### Data Normalization

OSM data can have inconsistent formatting and units. ExtractOSM can optionally provide some cleanup with the following:

* Normalize detected imperial distances (e.g., `ft`) to meters.
* Normalize durations (e.g., `hours`) to minutes.
* Apply regex substitutions defined in `config/text_substitutions.yml` to normalize text phrases.

---

### 🟢 Derived Fields

These fields are automatically computed based on tag presence and metadata:

### 🔍 `tag_count` 

* **`tag_count`** — Count of **significant tags** attached to the node, using the filtering rules defined
  in `ignore_tags.yml`.

`tag_count` can serve as a **proxy for feature richness**. OSM nodes with more tags are
better described and potentially more important.

* A city node with `population`, `wikidata`, `wikipedia`, and `official_name` tags may be more significant than one
* with only `name`.

The ignore_tags config file adds additional processing rules for counting the tags.
Not all tags contribute meaningful context. For example the following don't indicate richness of the data:

* **Administrative** (e.g., `source`, `check_date`)
* **Redundant** (e.g., `addr:housenumber`, `addr:street`)
* **Overly granular** (e.g., `internet_access:ssid`)

The `ignore_tags.yml` file helps to:

*  **Exclude** tags with little semantic value from the count.
*  **Group** tag families like `addr:*` or `contact:*` into a single count to avoid overrepresentation.

This helps `tag_count` better reflect **meaningful descriptive richness** rather than just tag volume.

 `ignore_tags.yml` Configuration

ignore_tags.yml includes a list of tags for special handling using the action as specified below:

| Rule       | Description                                                              |
|------------|--------------------------------------------------------------------------|
| `0`        | Tag is excluded from the count.                                          |
| `group`    | Tags with a common prefix (e.g., `addr:*`) are counted _once_ per group. |
| *Unlisted* | Tags not in the file are counted individually by default.                |

---

### Enrichment

CSV-based enrichment files can be loaded at runtime and merged into each node’s record to add data that OSM
doesn't contain. Each enrichment file
must contain a osm_id column and any number of additional attributes. These attributes are appended as new
columns during feature extraction. Multiple enrichment sources may be loaded and merged sequentially.

### Filtering

Tag filtering is defined in a YAML file (e.g., *_classification.yml) and passed to osmium to filter the input
data. Each tag key (e.g. 'amenity') is associated with a set of accepted values (e.g. 'restaurant', 'bar'). Only
nodes matching at least one of the configured
filters are retained.

### Output

The output is a  CSV file.
Each row corresponds to one OSM node. Columns include static fields, derived fields, configured features,
and enrichment attributes.

---

## ▶️ Usage

Run `ExtractOSM` from the command line to extract filtered and enriched node data from an OSM file.

```bash
extract-osm --input x --config x --substitutions x --ignore-tags x --enrichment-dir x --segment x --output x --log-level x
```

###  Arguments

* `-l`, `--log_level` — Log level:

    * `0`: quiet
    * `1`: info
    * `2`: debug


---

## Sample Files

### YAML Configuration (`config/poi_classification.yml`)

```yaml
config_type: "Classification"

# Items to extract from OSM file
keys:
  leisure:
    filters:
      - fitness_centre
      - stadium
  shop:
    filters:
      - department_store
      - bakery

```

### Ignore Tags (`config/ignore_tags.yml`)

```yaml
config_type: "IgnoreTags"

addr:*: group
building: 0
source: 0
```

ExtractOSM can provide a count of the tags for a node. This file lists tags to ignore
for that count to provide a more representative number.

* `0`:  exclude this tag from tag count
* `group`: all tags with this prefix are counted as one (e.g., `addr:*`)
* Tags not listed are counted as one by default

### Enrichment File (`data/external/parks_enrich.csv`)

```csv
osm_id,visitor_count,heritage_rank
123456,8000,1
987654,12000,2
```

Enrichment files add supplemental data to the matching OSM `osm_id`. In this example, it adds visitor
counts and heritage rank, which can be useful in scoring importance.

### Text Substitutions (`config/text_substitutions.yml`)

```yaml
convert_units: 1
substitutions:
  "\\bunknown\\b": "0"
  "\\bfew\\b|\\bfew(?=-)": "3"
```

This file provides regex to apply to tag values for cleanup.
It also includes a flag, `convert_units`. If this is set to `1` then distances will be converted to meters
and time will be converted to minutes. E.g "12 ft" will be converted to 3.6576.  
