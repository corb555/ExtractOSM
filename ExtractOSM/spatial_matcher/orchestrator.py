"""Orchestration for generic external-to-OSM spatial matching."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ExtractOSM.spatial_matcher import MatchingConfig, load_matching_config
from ExtractOSM.spatial_matcher.candidates import CandidateTable, PostGISCandidateBuilder
from ExtractOSM.spatial_matcher.extents import Extent, get_extent
from ExtractOSM.spatial_matcher.finalize import (FinalMatchTable, PostGISFinalMatchBuilder, )
from ExtractOSM.spatial_matcher.outputs import MatchOutputWriter, OutputFiles
from ExtractOSM.spatial_matcher.postgis import (PostGISSpatialIndexBuilder, SpatialIndexTables, )
from ExtractOSM.spatial_matcher.reverse import (PostGISReverseMatchBuilder, ReverseMatchTable, )
from ExtractOSM.spatial_matcher.sources import (ensure_external_id, load_source, normalize_source, )
from ExtractOSM.spatial_matcher.spatial import filter_points_to_extent
import pandas as pd


@dataclass(frozen=True, slots=True)
class PreparedSources:
    """Prepared sources and results from the complete matching pipeline."""

    extent: Extent
    config: MatchingConfig
    osm: pd.DataFrame
    external: pd.DataFrame
    osm_input_rows: int
    external_input_rows: int
    spatial_indexes: SpatialIndexTables | None = None
    candidates: CandidateTable | None = None
    reverse_matches: ReverseMatchTable | None = None
    final_matches: FinalMatchTable | None = None
    output_files: OutputFiles | None = None


class SpatialMatchOrchestrator:
    """Run the complete generic point-matching pipeline."""

    def run(
            self, osm_name: str | Path, external_name: str | Path, extent_name: str,
            config_name: str | Path, *, connection: Any | None = None,
            match_output: str | Path | None = None,
            external_output: str | Path | None = None, ) -> PreparedSources:
        """Run source preparation, matching, final selection, and output.

        Processing order:

        1. Load and validate source coordinates.
        2. Assign a stable ``external_id`` before filtering.
        3. Project and filter both sources to the requested extent.
        4. Apply configured numeric conversions.
        5. Build PostGIS staging tables and spatial indexes.
        6. Generate forward candidates and apply plausibility gates.
        7. Score plausible forward candidates.
        8. Run the same process in reverse and retain reverse-best candidates.
        9. Select forward best/second-best candidates and mark mutual-best pairs.
        10. Enforce one-to-one assignment.
        11. Write final match CSV and the filtered external CSV.
        12. Optionally write high-attention unmatched diagnostics.
        """
        config = load_matching_config(config_name)
        extent = get_extent(extent_name)

        osm = load_source(osm_name, config.osm, "OSM")
        external = ensure_external_id(load_source(external_name, config.external, "external"),
            config.external, )

        filtered_osm = filter_points_to_extent(osm, longitude=config.osm.longitude,
            latitude=config.osm.latitude, extent=extent, )
        filtered_external = filter_points_to_extent(external, longitude=config.external.longitude,
            latitude=config.external.latitude, extent=extent, )

        normalized_osm = normalize_source(filtered_osm, config.osm, "OSM")
        normalized_external = normalize_source(filtered_external, config.external, "external", )

        spatial_indexes = None
        candidates = None
        reverse_matches = None
        final_matches = None
        output_files = None

        if connection is not None:
            spatial_indexes = PostGISSpatialIndexBuilder(connection).build(osm=normalized_osm,
                external=normalized_external, epsg=extent.epsg, )
            candidates = PostGISCandidateBuilder(connection).build(tables=spatial_indexes,
                config=config, )
            reverse_matches = PostGISReverseMatchBuilder(connection).build(tables=spatial_indexes,
                config=config, )
            final_matches = PostGISFinalMatchBuilder(connection).build(tables=spatial_indexes, )

            if match_output is not None and external_output is not None:
                output_files = MatchOutputWriter(connection).write(tables=spatial_indexes,
                    config=config, external_frame=normalized_external, match_output=match_output,
                    external_output=external_output, )

        return PreparedSources(extent=extent, config=config, osm=normalized_osm,
            external=normalized_external, osm_input_rows=len(osm),
            external_input_rows=len(external), spatial_indexes=spatial_indexes,
            candidates=candidates, reverse_matches=reverse_matches, final_matches=final_matches,
            output_files=output_files, )
