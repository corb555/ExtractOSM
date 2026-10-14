# river_rasterize

`river_rasterize` converts a weighted river
centerline vector layer into a raster river mask with width based on weight.  the output spatial attributes will match 
the supplied
reference raster grid.

`river_rasterize`  defines width in
metres and rasterizes approprately scaled onto the target grid. The same geographic
river therefore remains approximately the same width while higher-resolution
rasters receive a more detailed representation.

The reference raster defines the complete output grid contract:

- CRS
- extent
- transform
- pixel size
- width and height
- pixel alignment

This avoids resampling a river mask between resolutions.

## Requirements

Python packages:

```text
geopandas
numpy
pandas
rasterio
shapely
```

The reference raster must use a projected CRS with metre units, such as
`EPSG:3857`.

## Input river layer

The default input layer is:

```text
rivers
```

The default weight field is:

```text
weight
```

The weight must already exist in the GeoPackage and contain numeric values.

A typical source-preparation workflow is:

```text
HydroSHEDS North America river data
        ↓
clip to the desired broad working extent
        ↓
filter small streams, such as ORD_STRAH >= 4
        ↓
calculate a river weight
        ↓
save the weighted regional GeoPackage
```

For example, a useful experimental weight is:

```text
max(
    UPLAND_SKM ^ 0.4,
    Strahler floor
)
```

The exact weight model is intentionally outside this utility. The rasterizer
only consumes the finished numeric weight field.

## Basic usage

```bash
river-rasterize \
  data/shapefiles/hydroshed/uswest_rivers.gpkg \
  build/Sedona/interim/Sedona_EVT_classes.tif \
  build/Sedona/interim/Sedona_river_mask.tif \
  --min-width-m 10 \
  --max-width-m 250
```

Arguments:

```text
input_gpkg        weighted river GeoPackage
reference_raster  raster whose exact grid defines the output
output_raster     output uint8 river-mask GeoTIFF
```

## Weight to width mapping

By default:

```text
weight 0.05 → minimum width
weight 600  → maximum width
```

These endpoints can be changed with:

```text
--input-min
--input-max
```

The physical river widths are controlled with:

```text
--min-width-m
--max-width-m
```

For example:

```bash
river-rasterize \
  uswest_rivers.gpkg \
  Sedona_EVT_classes.tif \
  Sedona_river_mask.tif \
  --input-min 0.05 \
  --input-max 600 \
  --min-width-m 10 \
  --max-width-m 250
```

Weights outside the input range are clamped to the nearest endpoint.

The mapping is linear:

```text
input-min  → min-width-m
input-max  → max-width-m
```

The tool buffers each river centerline by half of the calculated width before
rasterization.

## Exclusion masks

One or more polygon masks can suppress river augmentation in selected areas.

This is useful where a drainage-network representation is cartographically
misleading, such as very arid basins where large contributing area does not
imply a large perennial river.

Use `--exclude-mask` once per mask:

```bash
river-rasterize \
  build/USWest/uswest_rivers.gpkg \
  build/USWest/USWest_DEM.tif \
  build/USWest/USWest_river_mask.tif \
  --min-weight 20 \
  --min-width-m 10 \
  --max-width-m 250 \
  --all-touched \
  --burn-value 4 \
  --exclude-mask input/sources/arid_mask/arid_mask.gpkg 
  
river-rasterize \
  build/Sedona/Sedona_rivers.gpkg \
  build/Sedona/Sedona_DEM.tif \
  build/Sedona/Sedona_river_mask.tif \
  --min-width-m 10 \
  --max-width-m 250 \
  --all-touched   \
  --burn-value 4 \
  --exclude-mask input/sources/arid_mask/arid_mask.gpkg
```

All supplied mask geometries are:

1. reprojected to the reference raster CRS when necessary
2. clipped to the target raster extent
3. unioned together
4. subtracted from the buffered river geometry

The masks are applied **after buffering**. This is intentional: a wide river or
buffer end-cap cannot bleed back into an excluded area.

The masks are ordinary editable vector layers, so they can be adjusted directly
in QGIS when a cartographic boundary needs refinement.

## Output values and NoData

The output is a single-band `uint8` GeoTIFF.

Defaults:

```text
river pixels      = 1
unused pixels     = 0
NoData            = 0
```

Therefore every unused pixel is explicitly initialized to `0`, and `0` is also
marked as NoData.

These values can be changed with:

```text
--burn-value
--background-value
--nodata-value
```

Example:

```bash
--burn-value 1 --background-value 0 --nodata-value 0
```

The burn value must differ from the background value.

## Rasterization behavior

The raster is processed in tiles rather than as one full in-memory array.

Default tile size:

```text
2048 × 2048 pixels
```

Change it with:

```text
--tile-size 4096
```

Use:

```text
--all-touched
```

to burn every pixel touched by a buffered river polygon. Without this option,
Rasterio uses its normal center-based polygon rasterization behavior.

## Typical LandWeaver workflow

A useful production pattern is:

```text
ONE-TIME SOURCE PREPARATION

HydroSHEDS source
    ↓
regional extract / stream-order filter
    ↓
calculate weight
    ↓
weighted uswest_rivers.gpkg


PER BUILD REGION

weighted uswest_rivers.gpkg
    +
optional basin suppression masks
    +
region reference raster
        ↓
river-rasterize
        ↓
region_river_mask.tif
        ↓
merge river pixels into main category raster as WATER
        ↓
LandWeaver renders WATER normally
```

This keeps hydrology-specific preprocessing outside LandWeaver. LandWeaver sees
only the final categorical `WATER` pixels and can apply the same water palette,
noise, texture, and other rendering treatment already used for lakes and wider
EVT-derived rivers.

## Design notes

### Why use a reference raster?

Each AOI may have a different resolution or extent. Using the existing regional
raster as the reference guarantees exact alignment and avoids later resampling.

### Why use metres instead of pixels?

Pixel-based widths would change geographic river width between a 30 m regional
raster and a 10 m AOI raster. Metre-based widths preserve the intended physical
scale.

### Why apply exclusion masks after buffering?

If centerlines were clipped first and then buffered, the resulting buffer could
extend back across the mask boundary. Subtracting the mask after buffering
guarantees that excluded areas remain excluded.

### Why keep the river mask separate from the main category raster at first?

During tuning, a separate mask is easy to inspect in QGIS. Width parameters and
suppression polygons can be adjusted without repeatedly modifying the
categorical source raster. Once the behavior is satisfactory, the mask can be
merged into the main category raster as a later build step.

## Command reference

```text
--layer NAME
    Input GeoPackage layer.
    Default: rivers

--weight-field NAME
    Numeric field used to calculate river width.
    Default: weight

--input-min VALUE
    Weight mapped to --min-width-m.
    Default: 0.05

--input-max VALUE
    Weight mapped to --max-width-m.
    Default: 600

--min-width-m VALUE
    Minimum river width in metres.
    Default: 10

--max-width-m VALUE
    Maximum river width in metres.
    Default: 250

--burn-value VALUE
    Value written for river pixels.
    Default: 1

--background-value VALUE
    Value written for unused pixels.
    Default: 0

--nodata-value VALUE
    GeoTIFF NoData value.
    Default: 0

--exclude-mask PATH
    Polygon vector mask to remove from the buffered river geometry.
    Repeat for multiple masks.

--tile-size PIXELS
    Rasterization tile size.
    Default: 2048

--all-touched
    Burn all pixels touched by buffered river geometry.
