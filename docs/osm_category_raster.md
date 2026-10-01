## OSM to Category Raster Pipeline — High-Level Specification

### Purpose

Provide a set of composable tools for converting selected OpenStreetMap polygon features into categorical rasters.

---

## 1. `FilterOSM`

### Purpose

Extract only the OSM features required for downstream processing from a source OSM PBF.

The tool filters by configured OSM keys and values, producing a smaller OSM PBF containing only the required 
features. Reducing a large 
 PBF up front makes subsequent processing faster and gives downstream tools a concise, 
reproducible input dataset.

`FilterOSM` is a configuration-driven wrapper around Osmium that builds and runs the required extraction 
and filtering commands to produce a filtered OSM PBF file.

### Inputs

* source OSM `.pbf`
* dedicated OSM extraction config

The configuration uses a key/value-oriented OSM filter layout, for example:

```yaml
osm_filter:
  geological:
    filters:
      - moraine
      - volcanic_lava_field
      - volcanic_lava_flow
```

### Processing

The tool:

1. reads the extraction config;
2. converts the configured key/value filters into the required Osmium filtering commands;
3. applies the optional configured geographic extent using Osmium spatial extraction;
4. includes polygon-capable OSM objects required by the filters, including closed ways and multipolygon relations;
5. runs Osmium;
6. fails if Osmium does not complete successfully.

### Primary Output

```text
filtered_features.osm.pbf
```

---

## 2. `CreateOSMCategoryRaster`

### Purpose

`CreateOSMCategoryRaster` converts specified OSM polygon features into an integer categorical GeoTIFF. Category 
names are mapped to numeric raster values using a QGIS QML palette. The output grid is defined by a supplied 
reference raster.

Categorical rasters are useful when polygon classes need to become part of a common gridded surface for analysis, 
modeling, masking, or combination with other raster datasets. Converting the OSM geometry up front avoids repeated 
vector intersection work and produces a simple cell-based classification that can be processed efficiently by 
standard raster tools.

The QML palette is the authoritative category definition for the generated raster. It provides category labels, 
numeric raster values, and display colors, and can be edited by QGIS. `CreateOSMCategoryRaster` reads the QML 
only to resolve configured category names to numeric raster values.

### Inputs

* OSM `.pbf`
* OSM category mapping config
* QGIS QML file
* reference raster

Category mapping config example:

```yaml
categories:

  volcanic:
    osm_filter:
      geological:
        filters:
          - volcanic_lava_field
          - volcanic_lava_flow

  moraine:
    osm_filter:
      geological:
        filters:
          - moraine
```

### Processing

The tool:

1. reads the category mapping config;
2. reads category names and numeric values from the QML `<colorPalette>`;
3. validates that every configured category name exists in the QML palette;
4. converts relevant OSM polygon geometry into a normal vector representation internally;
5. classifies each polygon according to the OSM tag rules associated with each category;
6. resolves the QML numeric value for each category;
7. assigns that value to the matching polygons;
8. rasterizes the polygons onto the exact grid of the reference raster;
9. writes an integer categorical GeoTIFF.

For example:

```text
QML:

volcanic -> 1
moraine  -> 7
```

results in raster values:

```text
0 = no contribution
1 = volcanic
7 = moraine
```

### Reference Raster Contract

The generated raster must exactly match the reference raster's:

* CRS;
* extent;
* pixel resolution;
* affine transform;
* width;
* height;
* pixel alignment.

The tool should not independently calculate a grid.

### Primary Output

```text
osm_categories.tif
```

### Temporary Data

Any intermediate representation such as GeoPackage, GeoJSON, or temporary GDAL datasets is an implementation detail.

---

## 3. `MergeCategoryRasters`

### Purpose

Combine multiple aligned categorical rasters into one categorical raster using explicit input precedence.

This is useful when categorical data comes from multiple sources—for example, a primary classification raster supplemented by more detailed or manually curated datasets. Merging them into one raster produces a single classification surface that downstream raster tools can process without needing to understand the origin of each category.

### Inputs

* two or more categorical rasters supplied in precedence order

Example:

```yaml
inputs:
  - base_categories.tif
  - osm_categories.tif
  - supplemental_categories.tif
```

### Merge Rule

Inputs are processed from first to last.

For every raster after the first:

> Every non-zero pixel replaces the current output value.

Zero means:

> no contribution from this source.

Conceptually:

```python
result = first_raster.copy()

for raster in remaining_rasters:
    mask = raster != 0
    result[mask] = raster[mask]
```

Therefore, **later inputs have higher precedence**.

Example:

```text
base raster volcanic = 1
supplemental moraine = 7

where the supplemental raster is non-zero:
    output = 7

where the supplemental raster is zero:
    existing base value remains
```

### Validation

All rasters must have identical grids.

The tool must fail if any input differs in:

* CRS;
* extent;
* transform;
* resolution;
* width;
* height;
* pixel alignment.

The tool should not silently:

* reproject;
* resample;
* crop;
* expand;
* shift;
* align.

Those are separate transformations and would violate the single-purpose contract.

It should also validate that the raster data type can represent all encountered category values.

### Primary Output

```text
merged_categories.tif
```

---

## Pipeline

The resulting workflow is intentionally explicit:

```text
source.osm.pbf
      |
      v
FilterOSM
      |
      v
filtered_features.osm.pbf
      |
      v
CreateOSMCategoryRaster
      |
      v
osm_categories.tif
      |
      |       base_categories.tif
      |                |
      +----------------+
               |
               v
      MergeCategoryRasters
               |
               v
      merged_categories.tif
               |
               v
           Analysis, modeling,
           rendering, etc.
```

The QML palette remains the authoritative definition of category identity and display:

```text
numeric pixel value
        ↓
QML label + color
        ↓
category identity
```

---

## Build Dependencies

Each step should list only inputs that can legitimately affect that step's primary output.

For example:

```text
FilterOSM
    depends on:
        source.osm.pbf
        osm_extract.yml

CreateOSMCategoryRaster
    depends on:
        filtered_features.osm.pbf
        osm_category_raster.yml
        categories.qml
        reference_raster.tif

MergeCategoryRasters
    depends on:
        base_categories.tif
        osm_categories.tif
        supplemental_categories.tif
```

A tool should declare only inputs capable of changing its primary output. Keeping configuration narrowly scoped makes incremental build decisions predictable and makes it clear why a step is being rebuilt.
