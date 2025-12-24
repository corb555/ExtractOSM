
# Polygon Centroid & Area Calculator

A high-performance geospatial utility that extracts specific polygon features from  OpenStreetMap (OSM) files and 
calculates their geographic centroid and projected surface area.

This tool is critical for data pipelines that need to analyze the size or location of large features (like lakes, parks, or 
building footprints) without loading the entire OSM dataset into a heavy GIS database.

## 🚀 Key Features

*   **Filter-First Architecture:** Uses a two-stage pipeline to aggressively filter data *before* processing, ensuring low 
memory usage even on continental-scale datasets.
*   **High-Speed Filtering:** Leverages the `osmium` C++ library to rapidly parse PBF files.
*   **Accurate Area Calculation:** Automatically detects the correct UTM zone for each feature to perform accurate, 
projection-aware area calculations (in square meters).
*   **Streaming Processing:** Reads the filtered GeoJSON as a stream (`ijson`), never loading the full file into RAM.

## 🛠️ Requirements

*   Python 3.10+
*   **`osmium-tool`** (Command Line Interface) must be installed and available in your system PATH.
    *   *MacOS:* `brew install osmium-tool`
    *   *Ubuntu:* `apt install osmium-tool`

## 📦 Usage

```bash
python calc_latlon.py \
  --osm-file data/north-america.osm.pbf \
  --config config/lakes_classification.yml \
  --output data/lakes_metadata.csv \
  --build-dir build/temp_files
```

### Arguments

| Argument      | Description                                                                  |
|:--------------|:-----------------------------------------------------------------------------|
| `--osm-file`  | Path to the source OpenStreetMap file (`.osm.pbf`).                          |
| `--config`    | Path to the YAML classification file defining which features to extract.     |
| `--output`    | Path where the resulting CSV will be saved.                                  |
| `--build-dir` | Directory for storing temporary intermediate files (filtered PBFs/GeoJSONs). |
| `--log-level` | (Optional) Integer log level (default: 4).                                   |

## ⚙️ Configuration

The script uses a declarative YAML file to determine which features to extract. It looks for a `keys` dictionary where each 
entry specifies an OSM key and a list of values.

**Example `classification.yml`:**

```yaml
keys:
  natural:
    filters:
      - water
      - bay
  landuse:
    filters:
      - reservoir
      - basin
```
*This configuration will extract all features tagged `natural=water`, `natural=bay`, `landuse=reservoir`, or `landuse=basin`.*

## Output Format

The output is a simple CSV file ready for joining with other datasets.

| name          | osm_id  | lat   | lon   | area          |
|:--------------|:--------|:------|:------|:--------------|
| Lake Superior | 123456  | 47.7  | -87.5 | 82100000000.0 |
| Walden Pond   | 987654  | 42.4  | -71.3 | 240000.0      |

*   **osm_id:** The unique OpenStreetMap identifier.
*   **lat/lon:** The centroid of the polygon.
*   **area:** Surface area in square meters.