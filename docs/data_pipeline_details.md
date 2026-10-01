
# Data Pipeline Detailed Workflow Specification

## Workflow Stages

1. Prepare Database
2. Load Sources
3. Build OMT Sources
4. Build Base Features
5. Collect Enhancements
6. Enrich Features
7. Rank Features
8. Publish Rankings

The initial redesign covers stages 2 through 4. The remaining stages retain their current behavior.

## Ranking Categories

Ranking is organized around the Martin access functions. Each access function has one corresponding ranking category and consumes 
one rank for each geographic entity it serves.

Ranking categories are configuration-defined and may be added or removed without changing the workflow architecture. The currently 
configured categories are:

* `fault`
* `geological`
* `geyser`
* `paleo`
* `peak`
* `placenames`
* `volcanic`
* `waterfall`

These names must be used consistently in:

* LiteBuild profiles.
* Category feature configurations.
* Model configurations.
* Intermediate filenames.
* Ranking outputs.
* Rank-publication configuration.
* Martin access-function mapping.

For example:

```text
uswest_fault
uswest_geological
uswest_geyser
uswest_paleo
uswest_peak
uswest_placenames
uswest_volcanic
uswest_waterfall
```

The physical PostgreSQL function names may differ, but each must be explicitly mapped to one configured ranking category. For example, the `placenames` category may map to an `omt_place` function.

Every geographic entity returned by a Martin access function must obtain its rank from that function’s corresponding ranking category. Categories must not share a rank output merely because they read from the same database table.

## Artifact Naming

Every primary output must identify its scope and artifact type.

Region-level outputs use:

```text
{region}_{artifact}.{extension}
```

Category-level outputs use:

```text
{region}_{category}_{artifact}.{extension}
```

Examples include:

```text
uswest_osm.loaded
uswest_fault_source.loaded
uswest_fault_omt.materialized
uswest_fault_base_features.csv
uswest_fault_wikipedia_enrich.csv
uswest_fault_features.csv
uswest_fault_score.csv
uswest_fault_tiers.csv
uswest_fault_rank.updated
```

Generic primary-output names such as `output.csv`, `features.csv`, or `database.updated` are not permitted.

This naming convention allows LiteBuild to:

* Distinguish region-level and category-level artifacts.
* Track each category independently.
* Determine staleness from explicit dependencies.
* Reuse shared regional outputs.
* Diagnose failures in a large pipeline.
* Prevent unrelated profiles from accidentally sharing an output.

Marker files follow the same convention. A marker is updated only after its database operation and validation have completed successfully.

## Coordinate Reference Systems

The workflow defines one project coordinate reference system:

```yaml
GENERAL:
  SRS: "EPSG:3857"
```

`GENERAL.SRS` is the authoritative CRS for every persistent geometry stored in the GIS database. All loaders transform source geometry into this CRS before writing it to a durable table. Materialized views and other derived geometry preserve it.

Martin access functions may therefore compare stored geometry directly with `ST_TileEnvelope` and must not perform routine per-feature CRS transformations. Database validation treats a missing or incorrect SRID as an error.

Spatial formats such as Shapefile and GeoPackage may use any supported source CRS. The loader reads the CRS from source metadata, rejects missing or unrecognized CRS information, and transforms the geometry to `GENERAL.SRS`.

Geographic CSV input uses a deliberately restricted coordinate contract:

* Coordinates are WGS 84 longitude and latitude in numeric decimal degrees.
* Longitude is X and latitude is Y.
* The accepted latitude headers are `lat` and `latitude`.
* The accepted longitude headers are `lon` and `longitude`.
* Header matching is case-insensitive after surrounding whitespace is removed.
* DMS strings, projected coordinates, configurable coordinate mappings, and alternative CSV source CRSs are not supported.

The CSV loader rejects missing, duplicate, nonnumeric, out-of-range, projected, or apparently reversed coordinate columns. It constructs point geometry in EPSG:4326 and transforms it to `GENERAL.SRS` before insertion.

The base-feature CSV contains WGS 84 `latitude` and `longitude` attributes for Wikipedia validation and enrichment. These values are derived by transforming an entity's point or representative point from `GENERAL.SRS` to EPSG:4326. Persistent PostGIS geometry remains in `GENERAL.SRS`.

## 1. Prepare Database

Database preparation is out of scope for the initial redesign.

The existing process remains responsible for creating:

* PostgreSQL and PostGIS schemas.
* Required tables and columns.
* OMT-compatible table structures.
* Supporting database functions.
* Spatial and attribute indexes.
* Martin vector-tile access functions.

Materialized views that depend on loaded source data are handled by the Build OMT Sources stage.

## 2. Load Sources

### 2.1 Purpose

The Load Sources stage imports geographic source data into PostGIS.

Supported source types initially include:

* OpenStreetMap PBF extracts.
* Shapefiles.
* GeoPackages and other GDAL-supported vector formats.
* CSV files containing geographic entities.

Current examples are:

* Regional OSM data.
* USGS fault data supplied as vector files.
* Volcanic-field nodes supplied as CSV.

Source loading is concerned with getting source records into PostGIS safely and reproducibly. It does not select entities for ranking and does not calculate model features.

### 2.2 Source-loading adapters

Each source type is handled by an adapter.

Initial adapters are:

* **OSM adapter:** Uses osm2pgsql and the project’s flex configuration.
* **Vector adapter:** Uses GDAL/OGR for Shapefiles, GeoPackages, and similar vector formats.
* **Geographic CSV adapter:** Loads tabular records and constructs point geometries from the required latitude and longitude columns.

Each adapter must:

1. Validate its source files and configuration.
2. Load the source records into PostGIS.
3. Preserve or create stable entity identifiers.
4. Normalize field types where required.
5. Construct or import geometry.
6. Normalize the geometry coordinate reference system.
7. Create required indexes on loaded source tables.
8. Validate the loaded data.
9. Update its source-load marker only after successful completion.

### 2.3 Source-loading units

Each independently changeable source is a separate LiteBuild loading unit.

Examples include:

```text
uswest_osm.loaded
uswest_fault_source.loaded
uswest_volcanic_source.loaded
```

Each loading unit has:

* Explicit source files.
* A source configuration.
* A database-preparation dependency.
* Any adapter-specific configuration.
* Exactly one primary marker output.
* A defined set of database tables that it owns.

Unrelated sources must not be combined into one loading unit. Changing the volcanic-field CSV, for example, must not cause the regional OSM PBF or USGS fault data to be reloaded.

### 2.4 Source scope

A source may be regional or category-specific.

The regional OSM PBF is shared by several categories and is loaded once per region.

External data is normally category-specific. Examples include:

* USGS fault data for `fault`.
* Volcanic-field CSV data for `volcanic`.

A category profile depends on every source marker required to construct its OMT-facing data.

### 2.5 Source configuration

Each source configuration identifies:

* Source name.
* Source type.
* Region.
* Input file or files.
* Input layer, when applicable.
* Loader adapter.
* Destination source table.
* Stable identifier column.
* Geometry field, when applicable.
* Input coordinate reference system for spatial vector formats.
* Expected geometry type.
* Required source fields.
* Source-to-database field mappings.
* Constant field values.
* Load mode.
* Validation requirements.
* Primary marker output.

Source configuration is separate from category feature configuration.

The source configuration answers:

> How is this dataset loaded into PostGIS?

The category feature configuration answers:

> Which OMT entities and model features are exported for ranking?

### 2.6 OSM loading

The OSM adapter loads the regional PBF using osm2pgsql and the project’s flex configuration.

The OSM import is region-level. It is not repeated separately for `peak`, `geyser`, `geological`, or other OSM-derived ranking categories.

Category filtering occurs after loading, using the OMT-compatible database model.

The OSM loading unit depends only on inputs that affect ingestion:

* Regional PBF.
* osm2pgsql flex configuration.
* Ingestion-time normalization or substitution configuration.
* Database-preparation marker.
* Source-loading configuration.

It must not depend on:

* Wikipedia configuration or caches.
* Ranking-model configuration.
* Model-feature definitions.
* Tier configuration.
* Rank-publication configuration.
* MapLibre style files.

The OSM loader guarantees stable and unique OSM-derived identifiers according to the project’s identifier convention.

### 2.7 Vector loading

The vector adapter loads sources such as Shapefiles and GeoPackages into source tables.

The loading sequence is:

1. Validate the vector file.
2. Validate the requested source layer.
3. Determine or validate its coordinate reference system.
4. Import it into a source or staging table.
5. Convert field types as configured.
6. Reproject geometry when required.
7. Validate identifiers and geometries.
8. Create source-table indexes.
9. Run `ANALYZE`.
10. Update the source-load marker.

The loaded table does not need to be the final table queried by Martin. External vector sources commonly require cleanup, aggregation, or materialization before they satisfy the project’s OMT-facing contract.

### 2.8 Geographic CSV loading

The geographic CSV adapter loads tabular geographic data such as the volcanic-field node dataset.

Its configuration defines:

* Identifier column.
* Name column.
* Target table.
* Class and subclass values.
* Additional attributes to retain.

Coordinate column names and the CSV source CRS are fixed by the project CRS contract rather than configured per source. The file must provide `lat` or `latitude` and `lon` or `longitude` as WGS 84 numeric decimal degrees.

The adapter must:

1. Validate the CSV structure.
2. Validate required identifiers and names.
3. Locate exactly one accepted latitude column and one accepted longitude column.
4. Parse longitude and latitude as numeric decimal degrees.
5. Reject missing, ambiguous, reversed, or out-of-range coordinates.
6. Construct PostGIS point geometry in EPSG:4326.
7. Transform the geometry to `GENERAL.SRS`.
8. Load the configured source attributes.
9. Create required indexes.
10. Validate the loaded row count.
11. Update the source-load marker.

The source CSV remains an explicit LiteBuild dependency.

### 2.9 Stable identifiers

Every geographic entity used for ranking must have a stable identifier.

For OSM-derived entities, the identifier follows the convention guaranteed by the OSM loader.

For external sources, the preferred identifier is an authoritative source identifier, such as `fault_id`.

If no suitable identifier exists, one must be generated deterministically from stable source values. Import order and sequential row numbers must not be used.

Multiple source records may represent one geographic entity. For example, several fault-segment records may share one `fault_id`. The identifier therefore does not need to be unique in a raw or segmented source table.

It must be unique in the OMT-facing entity set exported for ranking.

### 2.10 Source ownership

Each loading unit declares the database records or source tables it owns.

A loader must not replace records belonging to another source or region.

Ownership may be established through:

* A dedicated source table.
* A region column.
* A source column.
* A database partition.
* Another explicit and unambiguous boundary.

### 2.11 Validation

Before reporting success, a loading unit validates:

* Input files exist and are readable.
* Required source fields exist.
* Identifiers are populated.
* Coordinate reference system is known.
* Geometry type is supported.
* Geometry is present and valid where required.
* Coordinates fall within expected bounds.
* Target source tables exist or can be populated as configured.
* Loaded row count satisfies configured expectations.
* Required indexes exist.

Source-specific tests may add:

* Minimum row counts.
* Known-record checks.
* Allowed class or type values.
* Geographic bounding-box checks.
* Identifier uniqueness at the appropriate source level.

### 2.12 Failure behavior

A failed load must:

* Return a nonzero status.
* Identify the failed source and validation.
* Preserve the previous usable data where practical.
* Avoid updating the source-load marker.
* Avoid reporting partially loaded data as current.

## 3. Build OMT Sources

### 3.1 Purpose

The Build OMT Sources stage creates the shared entity relation used by both the category's Martin access function and its base-feature extraction query.

This stage is the boundary between source-specific storage and the rest of the system. Downstream stages operate on the shared relation and do not need to know whether its records originated in OSM, a Shapefile, a GeoPackage, a CSV, or another source.

### 3.2 Shared Entity Relation

Each configured ranking category has one database relation representing every geographic entity eligible for that category. Centralizing category membership in this relation prevents Martin and base-feature extraction from developing different selection rules.

The relation may be:

* An existing OMT-compatible table when it already contains exactly one row per category entity.
* A regular view for inexpensive filtering, projection, geometry conversion, or `UNION ALL` operations.
* A materialized view for expensive aggregation, geometry processing, deduplication, or source normalization.

Custom geology and fault relations follow the same OMT design principles as OSM-derived relations. Source and staging tables may retain their original structure, but Martin and base-feature extraction do not query them directly.

### 3.3 Relation Contract

The shared relation contains exactly one row per geographic entity ranked by the category. It exposes, as applicable:

* Stable entity identifier.
* Geometry in `GENERAL.SRS`.
* Name.
* Short or alternate name.
* Class.
* Subclass.
* Current rank.
* Attributes required by Martin or the MapLibre style.
* Database-derived model features required by base-feature extraction.

Category membership rules belong in this relation. These may include:

* Class and subclass restrictions.
* Source restrictions.
* Aggregation by entity identifier.
* Geometry conversion.
* Name selection and cleanup.
* Deduplication.

The relation includes every entity eligible to be ranked. It must not exclude an entity because its current rank is zero, null, or outside a Martin zoom threshold. Otherwise, the existing rank would determine whether the entity could be considered for a new rank.

### 3.4 Category Configuration

Each category configuration identifies its shared relation and its Martin mapping:

```yaml
category: volcanic
entity_relation: map_entities.volcanic
martin_function: mvt.omt_volcanic
id_column: id
geometry_column: way
```

The physical PostgreSQL function name may differ from the category name, but the mapping must be explicit.

### 3.5 Martin and Extraction SELECTs

The shared relation defines **which entities belong to the category**. Martin and extraction provide separate wrappers defining **how those entities are consumed**.

The Martin wrapper adds:

* Tile-envelope intersection.
* Zoom-dependent rank thresholds.
* `ST_AsMVTGeom` conversion.
* `ST_AsMVT` encoding.
* Attributes required by the MapLibre style.

The extraction wrapper adds:

* Entity identifier and canonical classification fields.
* Database-derived model features.
* WGS 84 latitude and longitude.
* Deterministic output ordering.

The extraction query does not apply tile bounds, zoom conditions, current-rank thresholds, MVT geometry conversion, or MVT encoding.

A simple volcanic relation could be defined as:

```sql
CREATE VIEW map_entities.volcanic AS
SELECT
    v.id,
    v.gvp_id,
    v.name,
    v.class,
    v.subclass,
    v.rank,
    v.geom AS way
FROM volcanic_fields AS v
WHERE v.class = 'geological';
```

`volcanic_fields.geom` is already stored in `GENERAL.SRS`. Martin can therefore query the shared relation without a runtime CRS transformation:

```sql
SELECT
    ST_AsMVTGeom(v.way, bbox_geom, 4096, 64, true) AS way,
    v.id,
    v.gvp_id,
    v.name,
    v.class,
    v.rank,
    v.subclass
FROM map_entities.volcanic AS v
WHERE
    v.way && bbox_geom
    AND v.rank > 0;
```

Base-feature extraction queries the same relation but transforms only its representative point to EPSG:4326:

```sql
SELECT
    v.id,
    v.name,
    v.class,
    v.subclass,
    v.gvp_id,
    ST_X(ST_Transform(ST_PointOnSurface(v.way), 4326)) AS longitude,
    ST_Y(ST_Transform(ST_PointOnSurface(v.way), 4326)) AS latitude
FROM map_entities.volcanic AS v
ORDER BY v.id;
```

### 3.6 Categories Using Multiple OMT Tables

A shared relation may combine multiple OMT tables. For example, `geological` may combine OSM points with polygon representative points:

```sql
CREATE VIEW map_entities.geological AS
SELECT
    osm_id AS entity_id,
    way,
    name,
    class,
    subclass,
    rank
FROM planet_osm_point
WHERE class = 'geological'

UNION ALL

SELECT
    osm_id AS entity_id,
    ST_PointOnSurface(way) AS way,
    name,
    class,
    subclass,
    rank
FROM planet_osm_polygon
WHERE class = 'geological';
```

Martin and extraction query `map_entities.geological` rather than maintaining separate copies of the union and its category filters. The entity identifier remains suitable for ranking and rank publication; a separate display identifier may be exposed to MapLibre when needed.

### 3.7 Materialized Views

External sources commonly require a materialized shared relation. A materialized view may:

* Group multiple source records into one geographic entity.
* Merge or simplify geometry.
* Select canonical names.
* Assign class and subclass.
* Preserve the stable entity identifier.
* Expose the rank column.
* Select attributes required by Martin and ranking.

For example, fault segments may be grouped by `fault_id` into a `faults` materialized view. Martin and base-feature extraction both read that view, which contains one row per ranked fault.

The materialized-view definition is an explicit LiteBuild dependency. When the definition or loaded source changes, the view is recreated or refreshed before base features are rebuilt.

If a materialized relation contains a rank copied from an underlying table, it must be refreshed after rank publication so Martin sees the new value. A regular view may instead join rank from its maintained source table.

### 3.8 Name Cleanup

Some external sources require name normalization before their shared relation is built. The project has a configuration-driven tool that applies ordered pattern-and-replacement rules and is currently used for faults.

The cleanup configuration is an explicit dependency of the Build OMT Sources step. Changing a rule rebuilds the shared relation and its dependent category outputs without reloading the original source file.

Name cleanup must be deterministic, repeatable, ordered, diagnosable, and safe to rerun. Where practical, the original imported name is preserved and the cleaned name is produced in a separate column or during relation construction.

The cleanup process reports records examined, records changed, rules matched, unchanged records, and conflicting or invalid results.

### 3.9 Processing Order

The stage runs in this order:

1. Confirm that all required source-load markers are current.
2. Apply configured name cleanup and source normalization.
3. Create or refresh the shared table, view, or materialized view.
4. Create or refresh required indexes.
5. Run `ANALYZE` when the relation is materialized.
6. Validate the shared relation.
7. Update the OMT-ready or materialization marker.

For a category whose existing OMT table already satisfies the relation contract, the stage validates that table and produces its marker without rebuilding the underlying OSM data. A profile requiring no transformation may use the established touch-only behavior.

### 3.10 Validation

The shared relation is validated for:

* Required columns.
* One row per geographic entity.
* Non-null and unique entity identifiers.
* Valid geometry in `GENERAL.SRS`.
* Expected geometry type.
* Required names.
* Valid class and subclass values.
* Presence of the rank column.
* Attributes required by Martin.
* Database-derived model features required by extraction.
* Required spatial and attribute indexes when materialized.
* Expected row-count thresholds.

The entity identifier must be unique in the shared relation even when it is not unique in the loaded source table.

### 3.11 Primary Output

The primary output is a category-scoped marker identifying the completed shared relation:

```text
uswest_fault_omt.materialized
uswest_volcanic_omt.ready
uswest_peak_omt.ready
```

The marker is updated only after cleanup, relation construction, indexing, and validation succeed.

## 4. Build Base Features

### 4.1 Purpose

The Build Base Features stage creates the base model-feature CSV for one region and ranking category.

It queries the category's shared entity relation. It does not read the original PBF, Shapefile, GeoPackage, CSV, or source table directly.

The output contains:

* One row per geographic entity ranked for the corresponding Martin access function.
* The database-derived model features required by the ranking system.
* WGS 84 latitude and longitude for Wikipedia validation and enrichment.

### 4.2 Relationship to Martin access functions

The category's shared entity relation is authoritative for both Martin and ranking. The base-feature step reads that relation without independently repeating its class filters, unions, aggregation, deduplication, or geometry-selection rules.

For each ranking category:

1. Martin and extraction read the same shared entity relation.
2. Extraction writes one base-feature row per relation row.
3. The ranking system assigns one rank to each entity.
4. Rank publication updates the rank consumed by the Martin access function.

For the `fault` category, the relationship is:

```text
fault source records
    → faults materialized view
    → uswest_fault_base_features.csv
    → fault ranking
    → faults.rank
    → Martin fault access function
```

The Martin function may apply tile bounds and zoom-dependent rank thresholds, but it must not define a different category population from the ranking pipeline.

### 4.3 Extraction Contract

The base-feature step performs a straightforward projection from the configured shared entity relation. It selects the identifier, classification, representative coordinates, and configured database-derived model features.

It does not perform category membership filtering, joins, geometry merging, source-priority selection, aggregation, or deduplication. Those responsibilities belong to the Build OMT Sources stage.

### 4.4 Inputs

The step depends on:

* Category OMT-ready or materialization marker.
* Category feature configuration.
* Database connection configuration.

It does not depend on:

* Wikipedia cache contents.
* Prominence enrichment.
* Ranking models.
* Tier configuration.
* Rank-publication configuration.
* MapLibre style files.

### 4.5 Primary output

The primary output follows:

```text
{region}_{category}_base_features.csv
```

Examples include:

```text
uswest_fault_base_features.csv
uswest_geological_base_features.csv
uswest_peak_base_features.csv
uswest_volcanic_base_features.csv
```

The output contains one row for every geographic entity to be ranked by that category.

### 4.6 Required columns

Every base-feature CSV contains:

* Stable entity identifier.
* Entity name.
* Class, where applicable.
* Subclass, where applicable.
* `latitude`.
* `longitude`.
* Configured database-derived model features.

The identifier column is category-configurable. Examples include:

* `osm_id`.
* `fault_id`.
* A stable volcanic-field identifier.

The configured identifier becomes the join key for enhancement, ranking, and rank publication.

### 4.7 Latitude and longitude

Latitude and longitude are mandatory in the base-feature CSV because they are required for Wikipedia article validation and enrichment.

They are derived from the PostGIS geometry during base-feature generation and do not need to be stored as persistent columns in the shared entity relation.

The coordinate derivation must:

1. Produce an appropriate representative point from the entity geometry.
2. Transform the representative point to WGS 84.
3. Export longitude and latitude as numeric values.
4. Validate longitude within `-180` to `180`.
5. Validate latitude within `-90` to `90`.

Point entities use their point geometry.

Line, polygon, and multipart entities use a configured representative-point policy. The default must produce a point associated with the geometry rather than assuming that a geometric centroid is always suitable.

Latitude and longitude remain present even when a category’s Wikipedia validation policy chooses not to use location as acceptance evidence.

### 4.8 Names

When `require_name` is enabled, null, empty, or whitespace-only names are excluded.

Source-specific name cleanup has already occurred in the Build OMT Sources stage. The base-feature step does not apply another general name-normalization pass.

Wikipedia-specific title preprocessing remains part of Wikipedia collection and validation. It does not modify the geographic entity’s canonical database name.

### 4.9 Model features

The base-feature CSV contains the initial model features available from the OMT-facing database source.

Examples include:

* Elevation.
* Population.
* Geometry-derived size.
* Feature type.
* Source attributes used by the category’s WLM or RFR model.

Each configured model feature defines:

* Output column name.
* Source column or approved simple calculation.
* Expected data type.
* Null policy.
* Optional default value.

External enrichment features such as article length, link count, or prominence are added later.

### 4.10 Identifier validation

The base-feature output must contain:

* A non-null identifier for every row.
* Exactly one row per entity identifier.
* No ambiguous duplicate identifiers.

If duplicate identifiers exist in the shared relation, the step fails. Deduplication or aggregation must be fixed when constructing that relation rather than performed implicitly during CSV export.

### 4.11 Performance

The step must:

* Query PostGIS instead of rereading source files.
* Select only required rows and columns.
* Derive representative coordinates in PostGIS.
* Stream large result sets.
* Avoid serializing complete geometry into the CSV.
* Write records in deterministic order.
* Write the output atomically.

### 4.12 Output validation

Before replacing the existing CSV, the step validates:

* Required columns are present.
* Identifiers are non-null and unique.
* Required names are populated.
* Class and subclass values are allowed.
* Latitude and longitude are present and valid.
* Model-feature values have the expected types.
* Row count satisfies configured thresholds.
* CSV header and row widths are consistent.

The CSV is written to a temporary file and moved into place only after validation succeeds.

### 4.13 Diagnostics

The step reports:

* Shared entity relation queried.
* Martin ranking category.
* Database records examined.
* Geographic entities written.
* Records excluded for missing names.
* Invalid or missing identifiers.
* Invalid or missing geometry.
* Invalid representative coordinates.
* Query time.
* CSV writing and validation time.

## 5. Collect Enhancements

Out of scope for the initial redesign.

The existing enhancement processes remain unchanged. They consume the new base-feature CSV, including its latitude and longitude columns.

## 6. Enrich Features

Out of scope for the initial redesign.

The existing process continues to join enrichment outputs to the base-feature CSV using the category’s configured entity identifier.

## 7. Rank Features

Out of scope for the initial redesign.

The existing WLM and RFR processes remain unchanged.

Each configured ranking category produces the ranks consumed by its corresponding Martin access function.

## 8. Publish Rankings

Out of scope for the initial redesign.

Minor configuration changes may be needed for source-specific identifiers and target tables.

Rank publication must update the exact rank column read by the category’s Martin access function. A single ranked entity may update several physical source records when those records share an entity identifier, although the OMT-facing materialized view exposes one ranked entity.
