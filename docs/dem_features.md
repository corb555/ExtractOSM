# DEM Features

dem_features.py uses Digital Elevation Model (DEM) information to augment or analyze latitude/longitude CSV data.
It provides two features for geospatial analysis: 
- --validate_elevation — validate and repair elevation values using a DEM.
- --local_relief — measure how strongly a feature rises above nearby terrain.

## Why this is useful

### Correct unreliable elevation data

Peak elevation values can be missing, zero, or occasionally incorrect. `--validate_elevation` uses a DEM as a rough independent reference and can fill 
missing values or replace clearly bad values without blindly overwriting good  data.

### Measure local terrain dominance

Elevation, formal topographic prominence, isolation, and Topographic Position Index (TPI) describe related but different aspects of terrain.

Features such as buttes, monoliths, towers, and isolated rock formations may have modest absolute elevation while rising dramatically above nearby terrain. Examples include Devils Tower, Shiprock, and the Monument Valley buttes.

`--local_relief` measures this directly:

```text
local relief = summit DEM elevation - median surrounding DEM elevation
```

The surrounding terrain is sampled from an **annulus**, not a full circle. The inner radius excludes the feature itself so that a narrow high landform does not raise its own background elevation.

For example, a `500:1500` meter ring asks:

> How far does this feature rise above the typical terrain between 500 m and 1500 m away?

Multiple rings can be calculated in one run. This makes it possible to compare relief at several spatial scales before deciding which measurement is most useful for ranking or classification.

`--local_relief` is closely related to **Topographic Position Index (TPI)**. TPI generally compares the elevation of a raster cell with the average elevation of a surrounding neighborhood:

```text
TPI = center elevation - mean neighborhood elevation
```

TPI is commonly used across an entire raster to identify terrain position such as ridges, valleys, slopes, and local high points.

`--local_relief` is more specialized:

- it evaluates known point features rather than every raster cell;
- it uses the feature's summit elevation as the center value;
- it uses the median surrounding elevation for robustness against outliers (such as adjacent canyons or cliffs);
- it samples an annulus, excluding the feature itself from the background terrain.
- it preserves directly interpretable physical units—meters of vertical 
separation—rather than a normalized or unitless terrain-position score.

Local relief is therefore TPI-like, but tailored to measuring the local vertical dominance of discrete landforms.

The terrain measures are complementary:

- **Elevation** measures height above sea level.
- **Prominence** measures height above the key saddle connecting a summit to higher terrain.
- **Isolation** measures distance to the nearest higher summit.
- **TPI** measures whether a raster location is higher or lower than its surrounding neighborhood.
- **Local relief** measures how far a known feature rises above nearby surrounding terrain using a configurable annulus.

Both operations use Rasterio to read only the portion of the DEM needed for each feature and support DEMs in either projected or geographic coordinate systems.
## Input

The input CSV must contain latitude and longitude in WGS84 decimal degrees by default:

```text
lat
lon
```

Column names can be changed with command-line options.

---

# Usage

General syntax:

```bash
python dem_features.py OPERATION input.csv dem.tif output.csv [options]
```

Exactly one operation must be selected.

## Validate elevations

```bash
python dem_features.py \
    --validate_elevation \
    peaks.csv \
    dem.tif \
    peaks_validated.csv
```

The input CSV must contain:

```text
lat
lon
ele
```

The DEM is treated as a rough validation source. Existing elevation values are preserved unless one of these conditions applies:

- the CSV elevation is missing and the DEM has a valid value;
- the CSV and DEM elevations differ by more than 1000 m;
- the CSV elevation is `0` and the DEM elevation is greater than 10 m.

Rows outside the DEM extent are omitted. DEM NoData leaves the existing CSV elevation unchanged.

### Custom column names

```bash
python dem_features.py \
    --validate_elevation \
    peaks.csv dem.tif peaks_validated.csv \
    --lat-column latitude \
    --lon-column longitude \
    --elev-column elevation
```

## Calculate local relief

```bash
python dem_features.py \
    --local_relief \
    peaks.csv \
    dem.tif \
    peaks_relief.csv
```

`--local-relief` is also accepted.

If no ring is specified, the default annulus is:

```text
500:1500 meters
```

The output gains columns named:

```text
local_relief_<range>    e.g. local_relief_500_1500m
local_relief_<range>_p25    e.g. local_relief_500_1500m_p25

```

The first column is the summit DEM elevation minus the median DEM elevation of valid cells between 500 m and 1500 m from the feature.
The second is the p25. p25 is useful for capturing a peak with one steep side.

### Specify an annulus

```bash
python dem_features.py \
    --local_relief \
    peaks.csv dem.tif peaks_relief.csv \
    --ring 250:750
```

`--ring` uses `INNER:OUTER` radii in meters.

### Calculate several spatial scales

```bash
python dem_features.py \
    --local_relief \
    peaks.csv dem.tif peaks_relief.csv \
    --ring 250:750 \
    --ring 500:1500 \
    --ring 1000:3000
```

This produces:

```text
local_relief_250_750m   local_relief_250_750m_p25
local_relief_500_1500m local_relief_500_1500m_p25
local_relief_1000_3000m  local_relief_1000_3000m_p25
```

The DEM window required for the largest ring is read once per feature and reused for all requested rings.

### Require DEM coverage

By default, at least 70% of the DEM cells in a ring must contain valid data.

```bash
python dem_features.py \
    --local_relief \
    peaks.csv dem.tif peaks_relief.csv \
    --ring 500:1500 \
    --minimum-valid-fraction 0.80
```

If a ring does not meet the required coverage, its output value is left blank.

### Change the output column prefix

```bash
python dem_features.py \
    --local_relief \
    peaks.csv dem.tif peaks_relief.csv \
    --ring 500:1500 \
    --relief-column-prefix terrain_relief
```

This produces:

```text
terrain_relief_500_1500m
```

## Limit rows during testing

Both operations support `--limit`:

```bash
python dem_features.py \
    --local_relief \
    peaks.csv dem.tif test_output.csv \
    --ring 500:1500 \
    --limit 100
```

This is useful when testing a DEM, new ring sizes, or a new input dataset.

---

# Choosing local-relief rings

There is no single ideal radius for every landform.

Smaller rings emphasize narrow, abrupt features such as towers, buttes, and monoliths. Larger rings capture broader terrain dominance.

A useful exploratory set is:

```text
250:750 m
500:1500 m
1000:3000 m
```

Compare known examples and ordinary peaks before choosing a production feature. The raw relief values can then be normalized, 
percentile-ranked, log-transformed, or weighted by a downstream scoring system.

# Notes

- Negative DEM elevations are valid and are not treated as NoData.
- Rows outside the DEM extent are removed from the output.
- Existing output-column names are not overwritten by `--local_relief`.
- Local-relief values are written as whole meters.
- A coarse DEM such as 90 m resolution can work well for local relief because the goal is surrounding terrain context rather than precise summit geometry.
