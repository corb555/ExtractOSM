# Using `martin-cp` to Generate Production Vector PMTiles

## Overview

LandWeaver ideally uses two environments:

* a **dynamic style editing environment** backed by PostGIS and Martin
* a **static production environment** backed by PMTiles

The dynamic environment is ideal for editing and testing because changes to the GIS database or SQL tile functions are immediately visible in Maputnik and MapLibre GL JS.

Production does not need a live database or Martin server. Instead, the same Martin tile sources are exported to a static archive that can be hosted directly by `geomaps.site`.

The production workflow is:

```text
PostGIS
   ↓
Martin MVT functions
   ↓
martin-cp
   ↓
uswest.mbtiles
   ↓
MBTiles → PMTiles
   ↓
uswest.pmtiles
   ↓
Static web server
```

---

## Dynamic Style Editing Environment

During development, Maputnik and MapLibre GL JS use Martin as the vector tile server.

Martin connects to the `gis` PostGIS database and exposes the functions in the `mvt` schema as tile sources.

A style can combine multiple Martin sources into a single vector source:

```json
"sources": {
  "omt": {
    "type": "vector",
    "tiles": [
      "http://localhost:3000/omt_peak,omt_geological,omt_ocean,omt_river_lz,omt_lake_lz,omt_fault,omt_paleo,omt_place_lz,omt_viewpoint,omt_geyser/{z}/{x}/{y}"
    ]
  }
}
```

Style layers then reference the individual MVT layers:

```json
{
  "id": "peak-regional",
  "type": "symbol",
  "source": "omt",
  "source-layer": "omt_peak",
  "minzoom": 8,
  "filter": ["all", [">=", "rank", 10], ["<", "rank", 19]]
}
```

Each Martin source is backed by a PostGIS function such as `omt_peak`.

These functions:

* receive `z`, `x`, and `y`
* calculate the tile bounds with `ST_TileEnvelope`
* select features within that tile
* apply broad zoom-dependent filtering
* generate MVT data with `ST_AsMVT`

The GL style performs additional presentation and rank filtering.

This arrangement makes development fast because database, ranking, and styling changes can be viewed immediately without rebuilding static tiles.

---

# Why Production Uses Static Tiles

The production website does not need the development stack:

```text
MapLibre
    ↓
Martin
    ↓
PostGIS
```

Running those services in production would add unnecessary infrastructure and database dependencies.

Instead, the vector dataset is pre-generated once and deployed as a PMTiles archive:

```text
MapLibre
    ↓
uswest.pmtiles
```

This provides:

* simple static hosting
* no production PostGIS dependency
* no Martin server requirement
* reproducible vector data
* the same source-layer names used during development
* nearly identical GL styles between development and production

The major style difference is simply the vector source.

Development:

```json
"sources": {
  "omt": {
    "type": "vector",
    "tiles": [
      "http://localhost:3000/.../{z}/{x}/{y}"
    ]
  }
}
```

Production:

```json
"sources": {
  "omt": {
    "type": "vector",
    "url": "pmtiles://uswest.pmtiles"
  }
}
```

The style layers themselves continue to use source layers such as:

```text
omt_peak
omt_place_lz
omt_geological
omt_fault
omt_geyser
```

---

# Generate the MBTiles Archive

`martin-cp` walks the requested Web Mercator tiles within the configured bounding box and calls the selected Martin sources for each tile.

The current western-US extent is:

```yaml
bbox: "-125,24,-102,50"
```

The established production zoom range is:

```text
5–11
```

The previous production archive confirms this range:

```bash
sqlite3 uswest.mbtiles \
"select min(zoom_level), max(zoom_level), count(*) from tiles;"
```

Result:

```text
5|11|24095
```

Run:

```bash
martin-cp \
  --config config/martin_cp.yml \
  --output-file build/uswest.mbtiles \
  --min-zoom 5 \
  --max-zoom 11 \
  --bbox=-125,24,-102,50 \
  --source omt_peak,omt_geological,omt_ocean,omt_fault,omt_paleo,omt_river_lz,omt_place_lz,omt_viewpoint,omt_geyser
```

The source list must contain every Martin layer required by the production style.

This list should eventually be maintained in one canonical location so new sources are not accidentally omitted.

---

# Convert MBTiles to PMTiles

`martin-cp` produces:

```text
uswest.mbtiles
```

Convert that with:

```bash
gdal-helper create_pmtiles build/uswest.mbtiles build/uswest.pmtiles
```

using the project's standard MBTiles → PMTiles conversion tool.

The resulting `uswest.pmtiles` is the production vector artifact deployed to `geomaps.site`.

---

## Final Production Path

```text
PostGIS GIS database
        ↓
Martin MVT access functions
        ↓
martin-cp z5–11
        ↓
uswest.mbtiles
        ↓
PMTiles conversion
        ↓
uswest.pmtiles
        ↓
geomaps.site
        ↓
MapLibre GL JS
```

The important design benefit is that development and production use the **same database tile functions and the same MVT source-layer contracts**. Only the transport changes from live Martin tiles during editing to a static PMTiles archive in production.
