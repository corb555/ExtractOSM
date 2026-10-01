
# OSM Mask Creation - High Level Spec

## Purpose

Create raster masks representing major transportation features that commonly produce visible
anthropogenic artifacts in DEM and categorical raster data. These masks will be used with
`inpaint_raster` to reduce human artifacts from DEM rasters and Landfire categorical rasters.

For this use case, transportation masks will be generated from OpenStreetMap.

---

## OSM Transportation Extraction

Initial feature classes:

```text
highway=motorway
highway=motorway_link
highway=trunk
highway=trunk_link

railway=rail
```

Additional feature classes should only be added if they are found to produce visible artifacts.

Bridges are intentionally included because bridge decks and approaches can produce visible artifacts
in the DEM.

Tunnels must be excluded because the transportation feature passes below the natural terrain surface.
Masking a tunnel could incorrectly reconstruct or flatten terrain above it.

The OSM extraction should occur once against the shared USWest OSM source rather than repeatedly
parsing the complete OSM PBF for each AOI.

Conceptually:

```text
external/uswest.osm.pbf
↓
extract selected transportation features
↓
exclude tunnel segments
↓
shared USWest transportation vector
```

The resulting vector dataset can then be reused for both shared USWest processing and individual AOIs.

---

## Example Osmium Extraction

Because transportation filtering and tunnel exclusion are easier to understand and debug as separate
operations, use a two-stage Osmium filter.

Extract the transportation classes:

```bash
osmium tags-filter \
  external/uswest.osm.pbf \
  w/highway=motorway,motorway_link,trunk,trunk_link \
  w/railway=rail \
  -o cache/uswest_transport_all.osm.pbf \
  -O
```

Then remove objects carrying a tunnel tag:

```bash
osmium tags-filter \
  --invert-match \
  cache/uswest_transport_all.osm.pbf \
  w/tunnel \
  -o cache/uswest_transport.osm.pbf \
  -O
  
osmium tags-filter \
  --invert-match \
  cache/uswest_transport_all.osm.pbf \
  w/tunnel=yes,building_passage,culvert,avalanche_protector \
  -o cache/uswest_transport.osm.pbf \
  -O  
  
```

This intentionally:

```text
includes bridges
excludes tunnels
```

The filtered PBF can then be converted to the shared GeoPackage.

---

## Vector Intermediate

The preferred intermediate representation is a vector dataset covering the USWest region:

```text
cache/uswest_transportation.gpkg
```

This preserves transportation geometry at full vector precision.

The GeoPackage should include a spatial index so AOI-specific extraction can efficiently query only
features intersecting the target area.

AOI-specific processing should operate from this vector layer rather than from a medium-resolution
USWest raster mask.

This avoids:

- reparsing the large OSM PBF for every AOI,
- losing positional accuracy by resampling a lower-resolution mask,
- coupling the transportation source to any particular target raster resolution.

```bash
ogr2ogr \
  -f GPKG cache/uswest_transportation.gpkg \
  cache/uswest_transport.osm.pbf \
  lines \
  -nln transportation \
  -nlt PROMOTE_TO_MULTI \
  -lco SPATIAL_INDEX=YES

```

---

## Target-Specific Mask Creation

For each raster being reconstructed:

```text
shared transportation vector
↓
query / clip to target vicinity
↓
transform to a suitable metric working CRS
↓
buffer features by transportation class
↓
transform buffered geometry to target raster CRS
↓
rasterize directly to the exact target raster grid
↓
transportation mask
```

The mask should be generated directly against the target raster grid.

This allows the same vector source to produce:

```text
USWest EVT mask
Sedona 10m DEM mask
Craters 10m DEM mask
other AOI masks
```

without loss of vector precision.

Overlapping buffered polygons do not need to be dissolved or merged before rasterization.

The rasterizer effectively performs the required union at pixel resolution: if any buffered
transportation polygon touches a target pixel, that pixel is burned into the mask.

Avoiding a vector dissolve reduces unnecessary geometry processing and memory use.

---

## CRS and Buffering

Raw OSM geometry is normally stored in WGS 84 geographic coordinates.

Transportation widths must be defined in physical distance, not degrees or raster pixels.

Therefore buffering must occur in a projected CRS suitable for distance measurement.

Conceptually:

```text
OSM / shared vector geometry
↓
clip to target vicinity
↓
transform to suitable metric working CRS
↓
buffer in meters
↓
transform buffered polygons to target raster CRS
↓
rasterize
```

The metric working CRS does not need to be the same as the target raster CRS.

In particular, a target raster using Web Mercator should not automatically imply that Web Mercator
distance is used for precise physical buffer widths.

---

## Transportation Width

OSM generally represents roads and railroads as centerlines.

The mask must cover the physical terrain disturbance rather than only the centerline.

Buffer widths should therefore be assigned by feature class.

Initial relative behavior:

```text
motorway       wider
motorway_link  medium
trunk          medium
trunk_link     medium
rail           narrower
```

Exact widths will be determined experimentally.

Buffers should be expressed in meters before rasterization rather than as a fixed number of pixels.

This keeps the physical mask width consistent across target rasters with different resolutions.

The buffered geometry should account for:

- pavement or track width,
- shoulders,
- medians,
- embankments,
- cuts,
- fills,
- grading adjacent to the transportation feature.

At interchanges and crossings, overlapping buffered polygons are simply rasterized into the same
mask pixels. No vector dissolve is required.

---

## Mask Raster Format

Transportation masks use a simple binary raster representation:

```text
Data type: uint8

0 = preserve input pixel / valid donor region
1 = reconstruct input pixel

NoData = none
```

The mask must exactly match the target raster:

- CRS,
- extent,
- dimensions,
- resolution,
- pixel alignment.

Typical creation options:

```text
TILED=YES
COMPRESS=DEFLATE
PREDICTOR=1
```

The exact compression method is an output-storage choice and does not affect mask semantics.

---

## Mask Generation Responsibilities

Transportation-mask generation owns:

- OSM feature selection,
- tunnel exclusion,
- bridge inclusion,
- creation of the shared transportation vector,
- spatial indexing,
- AOI querying / clipping,
- transformation to a suitable metric working CRS,
- feature-specific buffering,
- transformation to the target raster CRS,
- rasterization,
- exact target-grid alignment.

It does not need to dissolve or merge overlapping buffered geometries before rasterization.

`inpaint_raster` owns only reconstruction of pixels selected by the completed raster mask.

This separation keeps `inpaint_raster` generic and reusable.

---

# Conceptual Pipeline

```text
OSM PBF
↓
extract transportation once
↓
exclude tunnels
↓
shared spatially indexed transportation vector
↓
query / clip to target vicinity
↓
transform to suitable metric working CRS
↓
buffer transportation centerlines by feature class
↓
transform buffered geometry to target raster CRS
↓
rasterize directly to exact target grid
↓
transportation mask
↓
inpaint_raster
    --method nearest       categorical raster
    --method idw           continuous raster
    --method telea         continuous raster
    --method ns            continuous raster
    --method biharmonic    continuous raster
↓
reconstructed raster
↓
downstream processing
```

For EVT:

```text
EVT raster
+
transportation mask
↓
inpaint_raster --method nearest
↓
categorical smoothing / rendering
```

For DEM:

```text
DEM
+
transportation mask
↓
inpaint_raster --method telea
↓
dual-gain hillshade
↓
relief rendering
```

The transportation vector is extracted once, while the raster mask is created late and separately
for each consumer grid.


