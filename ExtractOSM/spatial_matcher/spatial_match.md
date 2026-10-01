# Spatial Matcher

Generic preparation framework for matching an external point dataset to OSM
features.

The framework deliberately does not know about peaks, prominence, basins, or
any other domain-specific feature. A YAML configuration describes the field
roles and any simple numeric conversions required for a particular dataset.

## Current Pipeline

The orchestration layer currently:

1. accepts an OSM CSV, external CSV, and named extent;
2. loads a YAML source configuration;
3. validates the configured columns;
4. applies configured numeric multiplications;
5. optionally prepares one secondary match field on each source;
6. transforms lat/lon to EPSG:3857;
7. filters both datasets to the named rectangular extent.

The prepared sources are ready for the later PostGIS spatial candidate-matching
stage.

## Command

```bash
python -m spatial_matcher \
    uswest_peaks.csv \
    prominence.csv \
    USWEST \
    --config config/kirmse_peaks.yml
```

## Generic YAML Model

```yaml
osm:
  id: osm_id
  name: item_name
  latitude: lat
  longitude: lon
  match_field: optional_osm_field

external:
  latitude: latitude
  longitude: longitude
  match_field: converted_optional_field

  retain_fields:
    - useful_external_attribute

  conversions:
    - source: source_column
      target: converted_column
      factor: 0.3048
```

A conversion always means:

```text
target = source * factor
```

The match fields are optional. If they are omitted, later matching can operate
using spatial position alone.

Conversions are independent of matching. A converted field can be used only
as enrichment output, only as a match field, or both.

## Current Example

`config/kirmse_peaks.yml` configures the current prominence use case:

- OSM `ele` is the optional comparison field.
- External elevation is converted from feet to meters and used as the external
  comparison field.
- External prominence is also converted from feet to meters, but is not a
  matching criterion.
- Key-saddle coordinates are retained as enrichment/audit data.

`config/spatial_only.yml` demonstrates that the same framework can prepare a
pure lat/lon matching problem without any secondary field.
