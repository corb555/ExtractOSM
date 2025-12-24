# poi_richness.py
"""
Quantifies the significance of a place by measuring the density and quality of nearby POIs.
...
"""
import argparse
import logging
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from tqdm import tqdm

from GeoTier.spatial_index import SpatialIndex
from YMLEditor.yaml_reader import ConfigLoader

POI_SCHEMA = {
    'config_type': {'type': 'string', 'required': True, 'allowed': ["PoiScores"]},
    'city_radius_meters': {'type': 'number', 'required': True},
    'locale_radius_meters': {'type': 'number', 'required': True}
}

SOURCE_CRS = "EPSG:4326"
PROJECTED_CRS = "EPSG:3857"

def add_pois_to_index(pois_gdf: gpd.GeoDataFrame, spatial_index: SpatialIndex, logger: logging.Logger) -> None:
    """Adds pre-scored POIs from a GeoDataFrame to a spatial index."""
    logger.info(f"➡️ Building spatial index with {len(pois_gdf)} pre-scored POIs...")
    for poi in tqdm(pois_gdf.itertuples(), total=len(pois_gdf), desc="Indexing POIs"):
        if not poi.geometry.is_empty:
            # The 'score' from the POI's own importance model is used as the data payload
            spatial_index.add_point(int(poi.osm_id), poi.geometry, data=poi.score)
    logger.info("✅ Spatial index built.")


def calculate_place_scores(
        places_gdf: gpd.GeoDataFrame,
        spatial_index: SpatialIndex,
        city_radius: float,
        default_radius: float,
        logger: logging.Logger
) -> list:
    """Calculates a distance-decayed POI significance score for each place."""
    logger.info("➡️ Calculating distance-decayed POI scores for each place...")
    final_scores = []

    spatial_index.approximate_distance = False

    for place in tqdm(places_gdf.itertuples(), total=len(places_gdf), desc="Scoring places"):
        total_score = 0.0

        if not place.geometry.is_empty:
            # ---  Proportional Radius Logic ---
            if place.sub_category == 'city':
                full_weight_radius = city_radius
                search_radius = city_radius * 1.5
            else:
                full_weight_radius = default_radius
                search_radius = default_radius * 1.5

            nearby_pois = spatial_index.find_within_distance(place.geometry, search_radius)

            for _osm_id, poi_score, distance in nearby_pois:
                if poi_score is not None and poi_score > 0:
                    if distance <= full_weight_radius:
                        decayed_score = poi_score
                    else:
                        distance_from_core = distance - full_weight_radius
                        decayed_score = poi_score / (distance_from_core + 1)
                    total_score += decayed_score

        final_scores.append(total_score)

    logger.info("✅ Place scoring complete.")
    return final_scores


def main() -> None:
    """Orchestrates the end-to-end process of generating POI scores.

    This function handles command-line argument parsing, file I/O, configuration
    loading, data validation, coordinate reprojection, and calling the core
    processing functions.
    """
    parser = argparse.ArgumentParser(
        description="Generate POI significance scores for places based on proximity to pre-scored POIs."
    )
    parser.add_argument(
        "--places", type=Path, required=True,
        help="Path to the input CSV file for places. Must contain 'osm_id', 'lon', 'lat', and 'sub_category' columns."
    )
    parser.add_argument(
        "--pois", type=Path, required=True,
        help="Path to the input CSV file for POIs. Must contain 'osm_id', 'lon', 'lat', and a 'score' column."
    )
    parser.add_argument(
        "--config", type=Path, required=True,
        help="Path to the YAML configuration file defining search radiuses."
    )
    parser.add_argument(
        "--output", type=Path, required=True,
        help="Path to save the output CSV file, which will be the original places file with an added 'poi_score' column."
    )
    parser.add_argument(
        "--log-level", type=int, default=2,
        help="Set log level (1=DEBUG, 2=INFO, 3=WARNING, 4=ERROR, 5=CRITICAL)."
    )
    args = parser.parse_args()

    logger = _setup_logger(args.log_level)

    logger.info("\n*️⃣  Configuration:")
    logger.info(f"   - Places Input:    {args.places}")
    logger.info(f"   - POIs Input:      {args.pois}")
    logger.info(f"   - Config:          {args.config}")
    logger.info(f"   - Output:          {args.output}")

    try:
        logger.info("\n➡️ Loading configuration and data...")
        loader = ConfigLoader(POI_SCHEMA)
        config = loader.read(config_file=Path(args.config))

        city_radius = config['city_radius_meters']
        default_radius = config['locale_radius_meters']

        logger.info(f"   - City Core Radius (Full Weight):   {city_radius}m Extended Radius (with Decay): {city_radius * 1.5}m")
        logger.info(f"   - Locale Core Radius (Full Weight): {default_radius}m Extended Radius (with Decay): {default_radius * 1.5}m")

        places_df = pd.read_csv(args.places, dtype={'osm_id': str})
        pois_df = pd.read_csv(args.pois, dtype={'osm_id': str})

        # Filter out places/POIs with null coordinates
        initial_place_count = len(places_df)
        places_df = places_df[(places_df['lon'] != 0) & (places_df['lat'] != 0)].copy()
        if initial_place_count > len(places_df):
            logger.warning(f"   - Removed {initial_place_count - len(places_df)} places with null (0,0) coordinates.")

        initial_poi_count = len(pois_df)
        pois_df = pois_df[(pois_df['lon'] != 0) & (pois_df['lat'] != 0)].copy()
        if initial_poi_count > len(pois_df):
            logger.warning(f"   - Removed {initial_poi_count - len(pois_df)} POIs with null (0,0) coordinates.")

        logger.info(f"\n   - Processing {len(places_df)} places and {len(pois_df)} POIs.")

        if 'score' not in pois_df.columns:
            raise ValueError("Input POIs file must contain a 'score' column from an importance model.")

        logger.info(f"\n➡️ Projecting coordinates from {SOURCE_CRS} to {PROJECTED_CRS} (meters)...")
        places_gdf_proj = gpd.GeoDataFrame(
            places_df, geometry=gpd.points_from_xy(places_df.lon, places_df.lat), crs=SOURCE_CRS
        ).to_crs(PROJECTED_CRS)
        pois_gdf_proj = gpd.GeoDataFrame(
            pois_df, geometry=gpd.points_from_xy(pois_df.lon, pois_df.lat), crs=SOURCE_CRS
        ).to_crs(PROJECTED_CRS)
        logger.info("   - Coordinate projection complete.")

    except (FileNotFoundError, ValueError, KeyError) as e:
        logger.error(f"\n   ❌ Error loading files or config: {e}")
        sys.exit(1)

    spatial_index = SpatialIndex(approximate_distance=False)

    add_pois_to_index(pois_gdf_proj, spatial_index, logger)

    poi_scores = calculate_place_scores(
        places_gdf_proj, spatial_index, city_radius, default_radius, logger
    )

    final_df = places_df.copy()
    final_df['poi_score'] = poi_scores

    args.output.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"\n➡️ Saving {len(final_df)} items with POI scores to {args.output}...")
    final_df.to_csv(args.output, index=False, float_format='%.2f')
    logger.info("✅ POI score generation complete.")

def _setup_logger(level: int) -> logging.Logger:
    """Initializes and configures the logger for the script."""
    log_level = max(1, min(5, level)) * 10
    logger = logging.getLogger(Path(__file__).stem)
    logger.setLevel(log_level)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger

if __name__ == "__main__":
    main()