
### **Requirements: Geospatial Fuzzy Join Utility - V 1.0**

#### **1. Overview & Goal**

The goal is to create a generic, command-line utility, `fuzzy_join.py`, that enriches a "target" dataset (from OSM) with data from an "auxiliary" dataset by finding matches based on a combination of **spatial proximity** and **text similarity.**

This tool is designed to solve the common problem of joining two geospatial datasets that lack a shared, unique key, such as matching OSM points of interest with an external data source like health ratings or business listings.

#### **2. Inputs & Outputs**

*   **[REQ-2.1] Input 1: Target OSM Data CSV (`--target-file`)**
    *   **Purpose:** The primary dataset to be enriched (e.g., your OSM POIs).
    *   **Required Columns:**
        *   `osm_id`: The unique identifier for each feature.
        *   `item_name`: The name of the feature for text matching.
        *   `lon`, `lat`: The geographic coordinates.

*   **[REQ-2.2] Input 2: Auxiliary Data CSV (`--aux-file`)**
    *   **Purpose:** The external dataset containing the data to be joined (e.g., your pre-processed health scores).
    *   **Required Columns:**
        *   `item_name`: The name of the feature for text matching.
        *   `lon`, `lat`: The geographic coordinates.
    *   **Data Columns:** This file can contain any number of additional data columns (e.g., `health_score`, `review_count`) that will be merged into the final output.

*   **[REQ-2.3] Output: Matched Enrichment CSV (`--output`)**
    *   **Purpose:** An "enhancement" file ready to be used in your pipeline.
    *   **Columns:**
        *   `osm_id`: The identifier from the target OSM file.
        *   All data columns from the auxiliary file that were specified for merging.


### **Requirements: Geospatial Fuzzy Join Utility - V 1.0**

#### **1. Overview & Goal**

The goal is to create a generic, command-line utility, `fuzzy_join.py`, that enriches a "target" dataset (from OSM) with data from an "auxiliary" dataset by finding matches based on a combination of **spatial proximity** and **text similarity.**

This tool is designed to solve the common problem of joining two geospatial datasets that lack a shared, unique key, such as matching OSM points of interest with an external data source like health ratings or business listings.

#### **2. Inputs & Outputs**

*   **[REQ-2.1] Input 1: Target OSM Data CSV (`--target-file`)**
    *   **Purpose:** The primary dataset to be enriched (e.g., your OSM POIs).
    *   **Required Columns:**
        *   `osm_id`: The unique identifier for each feature.
        *   `item_name`: The name of the feature for text matching.
        *   `lon`, `lat`: The geographic coordinates.

*   **[REQ-2.2] Input 2: Auxiliary Data CSV (`--aux-file`)**
    *   **Purpose:** The external dataset containing the data to be joined (e.g., your pre-processed health scores).
    *   **Required Columns:**
        *   `item_name`: The name of the feature for text matching.
        *   `lon`, `lat`: The geographic coordinates.
    *   **Data Columns:** This file can contain any number of additional data columns (e.g., `health_score`, `review_count`) that will be merged into the final output.

*   **[REQ-2.3] Output: Matched Enrichment CSV (`--output`)**
    *   **Purpose:** An "enhancement" file ready to be used in your pipeline.
    *   **Columns:**
        *   `osm_id`: The identifier from the target OSM file.
        *   All data columns from the auxiliary file that were specified for merging.


#### **3. Core Logic & Configuration**
*(Modified to include the new components)*

*   **[REQ-3.1] Dependencies:** The utility will require the `rapidfuzz` library for high-performance fuzzy string matching.

*   **[REQ-3.2] Configuration:**
    *   `search-radius-meters`: The spatial radius (in meters) to search for candidate target POIs around each auxiliary record.
    *   `name-similarity-threshold`: The minimum fuzzy text matching score (from 0 to 100) required to consider two names a match.
    *   `aux-columns`: A list of the data columns from the auxiliary file to include in the final output.
    *   `noise-words`: (Optional) A list of common words (e.g., "north", "south", "the") to be removed from both names before comparison. This can be specified on the command line or in a configuration file.

*   **[REQ-3.3] Geospatial Preparation:**
    Both input DataFrames must be converted to GeoDataFrames and reprojected to a meter-based CRS (e.g., EPSG:3857).

*   **[REQ-3.4] Spatial Indexing:**
    All features from the **target OSM dataset** must be loaded into an R-tree spatial index.

*   **[REQ-3.5] Name Normalization and Cleaning:**
    *   Before any comparison, all `item_name` values from both the target and auxiliary datasets must be passed through a **standard cleaning pipeline**:
        1.  Convert the string to lowercase.
        2.  Remove any specified `--noise-words`.
        3.  Remove all punctuation.
        4.  Collapse multiple whitespace characters into a single space.
    *   This cleaning process ensures that the comparison is made on a canonical, normalized version of the names.

*   **[REQ-3.6] Fuzzy Text Matching:**
    *   The script shall use the `rapidfuzz.fuzz.token_sort_ratio` algorithm to perform the fuzzy string comparison on the **cleaned names**.

*   **[REQ-3.7] Iteration and Match Decision Logic:**
    *   The script shall iterate through each record of the **auxiliary data**.
    *   For each record, it will find all *spatial candidates* from the target dataset using the spatial index.
    *   For each spatial candidate, it will calculate the `token_sort_ratio` between the cleaned auxiliary name and the cleaned target name.
    *   A definitive match is found by selecting the candidate that is both **spatially close** and has the **highest name similarity score**, provided that score is above the `--name-similarity-threshold`.
    
*   **[REQ-3.7] Output Generation:**
    *   If a definitive match is found, a new row is created containing the matched target feature's `osm_id` and the requested data values from the auxiliary record's `aux-columns`.
    *   This list of matched records is saved as the final output CSV.

#### **4. Logging and Diagnostics**

*   **[REQ-4.1] Progress Reporting:** The main loop iterating through the auxiliary records must be wrapped in a `tqdm` progress bar.
*   **[REQ-4.2] Statistics Summary:** Upon completion, the script must print a summary of the join, including:
    *   Total records in the target file.
    *   Total records in the auxiliary file.
    *   Number of successful matches found.
    *   Match rate percentage (successful matches / total auxiliary records).
*   **[REQ-3.7] Output Generation:**
    *   If a definitive match is found, a new row is created containing the matched target feature's `osm_id` and the requested data values from the auxiliary record's `aux-columns`.
    *   This list of matched records is saved as the final output CSV.

#### **4. Logging and Diagnostics**

*   **[REQ-4.1] Progress Reporting:** The main loop iterating through the auxiliary records must be wrapped in a `tqdm` progress bar.
*   **[REQ-4.2] Statistics Summary:** Upon completion, the script must print a summary of the join, including:
    *   Total records in the target file.
    *   Total records in the auxiliary file.
    *   Number of successful matches found.
    *   Match rate percentage (successful matches / total auxiliary records).