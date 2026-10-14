# extract_gpkg

Extracts GeoPackage data for a raster-based map build.

`extract_gpkg` saves you from manually calculating vector clip bounds and CRS
arguments. Instead of passing numeric extents to `ogr2ogr`, you give the utility a
**reference raster**. The script reads that raster's extent and CRS, then uses
them to extract and reproject one or more GeoPackage layers onto the same map
area.

This makes build configuration simpler, safer, and easier to reuse across
regions and AOIs.

## Why use it

A normal `ogr2ogr` extraction requires you to know and repeat details such as:

- target bounds
- the CRS those bounds are expressed in
- target output CRS
- source layer names
- output layer names
- optional attribute filters
- GeoPackage layer creation options

In a raster-oriented build pipeline, most of that spatial information already
exists in the target raster.

`extract_gpkg` lets the raster define the spatial contract:

```text
reference raster
    ↓
extent + CRS
    ↓
extract matching vector data
    ↓
output GeoPackage aligned to the same map region
```

This is especially useful when the same source GeoPackage is reused for:

- a full regional build
- multiple AOIs
- different raster resolutions

Each build can simply point to its own reference raster.

## Basic usage

```bash
extract-gpkg \
  input.gpkg \
  output.gpkg \
  reference.tif \
  --layer rivers
```

This extracts the `rivers` layer to the extent of `reference.tif` and reprojects
it to the raster CRS.

## Rename an output layer

Use:

```text
--layer SOURCE:DEST
```

For example:

```bash
extract-gpkg \
  hydrosheds.gpkg \
  uswest_rivers.gpkg \
  USWest_DEM.tif \
  --layer north_america_RIV_STREAMS_1s_v2r0:rivers
```

The source layer:

```text
north_america_RIV_STREAMS_1s_v2r0
```

is written to the output GeoPackage as:

```text
rivers
```

If `:DEST` is omitted, the source layer name is preserved.

## Extract multiple layers

`--layer` may be repeated:

```bash
extract-gpkg \
  hydrosheds.gpkg \
  uswest_hydro.gpkg \
  USWest_DEM.tif \
  --layer north_america_RIV_STREAMS_1s_v2r0:rivers \
  --layer north_america_RIV_NODES_1s_v2r0:river_nodes
```

Each source layer is extracted into the same output GeoPackage.

## Attribute filtering

Use `--where` to pass an OGR attribute filter:

```bash
extract-gpkg \
  hydrosheds.gpkg \
  uswest_rivers.gpkg \
  USWest_DEM.tif \
  --layer north_america_RIV_STREAMS_1s_v2r0:rivers \
  --where "ORD_STRAH >= 4"
```

The filter is applied to every selected layer.

This works well when the source contains far more detail than the map needs.

## Reference raster behavior

The reference raster provides:

- extraction extent
- output CRS

Internally the wrapper supplies the equivalent of:

```text
-spat xmin ymin xmax ymax
-spat_srs <reference raster CRS>
-t_srs <reference raster CRS>
```

This avoids manually transforming raster bounds into the source layer CRS.

The output vector data is therefore clipped to the raster's geographic area and
stored in the same CRS as the raster.

The utility does **not** snap vector geometry to raster pixels. The raster is
used only to define extent and CRS.

## Layer creation options

By default the output layer uses:

```text
SPATIAL_INDEX=YES
```

Additional or replacement layer creation options can be supplied with
repeatable `--lco` arguments:

```bash
extract-gpkg \
  input.gpkg \
  output.gpkg \
  reference.tif \
  --layer rivers \
  --lco SPATIAL_INDEX=YES \
  --lco FID=id
```

If any `--lco` option is supplied, those supplied options are used.

##  HydroSHEDS example

```bash
extract-gpkg \
  data/shapefiles/hydroshed/hydrosheds_v2_na_rivers.gpkg \
  input/sources/hydroshed/uswest_rivers.gpkg \
  build/USWest/USWest_DEM.tif \
  --layer north_america_RIV_STREAMS_1s_v2r0:rivers \
  --where "ORD_STRAH >= 4"
```

This replaces the more verbose manual pattern:

```bash
ogr2ogr \
  output.gpkg \
  input.gpkg \
  north_america_RIV_STREAMS_1s_v2r0 \
  -spat xmin ymin xmax ymax \
  -spat_srs EPSG:3857 \
  -where "ORD_STRAH >= 4" \
  -t_srs EPSG:3857 \
  -nln rivers \
  -overwrite \
  -lco SPATIAL_INDEX=YES
```

The practical difference is that the build no longer needs to duplicate the
raster extent or CRS in configuration.

## Output handling

The output GeoPackage is recreated for each run.

This is intentional. It prevents stale layers from an older invocation from
remaining in the file when the requested layer list changes.

For multiple requested layers:

1. the first layer creates the output GeoPackage
2. later layers are added using update mode

## Validation and errors

The utility fails before extraction when possible.

It checks for:

- missing input GeoPackage
- missing reference raster
- input and output referring to the same file
- missing output directory
- empty `--where`
- duplicate destination layer names
- unavailable source layers
- missing raster CRS
- missing `ogr2ogr`
- missing `ogrinfo`
- failed `ogr2ogr` execution

Requested source layers are validated with `ogrinfo` before the output
GeoPackage is created.

## Requirements

Python package:

```text
rasterio
```

External GDAL utilities:

```text
ogr2ogr
ogrinfo
```

Both GDAL commands must be available on `PATH`.

## Command reference

```text
extract-gpkg INPUT_GPKG OUTPUT_GPKG REFERENCE_RASTER
             --layer SOURCE[:DEST]
             [--layer SOURCE[:DEST] ...]
             [--where EXPRESSION]
             [--lco NAME=VALUE ...]
```

### Positional arguments

`INPUT_GPKG`

: Source GeoPackage.

`OUTPUT_GPKG`

: Destination GeoPackage.

`REFERENCE_RASTER`

: Raster whose extent and CRS define the extraction.

### Options

`--layer SOURCE[:DEST]`

: Source layer to extract. Repeat for multiple layers. If `DEST` is supplied,
  the output layer is renamed. Otherwise the source layer name is preserved.

`--where EXPRESSION`

: Optional OGR attribute filter applied to every selected layer.

`--lco NAME=VALUE`

: GeoPackage layer creation option. Repeat for multiple options. Default when
  omitted: `SPATIAL_INDEX=YES`.

## Design boundary

`extract_gpkg` is intentionally small.

It does not:

- interpret domain-specific fields
- choose filters automatically
- modify attributes
- rasterize geometry
- infer map categories
- perform workflow dependency management

Its job is simply:

```text
GeoPackage + raster spatial contract
    ↓
filtered regional GeoPackage
```

That makes it useful beyond hydrology for any vector source that needs to be
cropped and reprojected consistently with raster build regions.
