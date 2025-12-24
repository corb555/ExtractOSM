from pathlib import Path
import sys
from typing import Iterator, Tuple, List

import osmium

from ExtractOSM.enrichment_manager import EnrichmentManager
from ExtractOSM.normalize_units import normalize_units
from ExtractOSM.significant_tag_counter import SignificantTagCounter
from ExtractOSM.text_substitution import TextSubstitutions

MODE_FEATURE_VALUE = "value"
MODE_ASIS = "asis"
MODE_PRESENCE = "presence"
VALID_MODES = {MODE_FEATURE_VALUE, MODE_ASIS, "score", MODE_PRESENCE}

class OSMData:
    """
    Handles the extraction of features from OSM nodes using a YAML-driven
    configuration. Applies normalization, tag filtering, and enrichment data.

      - Extracts core and user specified tag fields
      - Converts units and standardizes free-text values
      - Counts significant tags based on a YAML ignore list
      - Merges external enrichment data
      - Writes structured output as CSV

    Attributes:
        feature_config (List[dict]): List of features from config YAML
        output_tag (str): OSM key to identify node category
        debug_nodes (List[str]): Node IDs to log detailed output
        log_level (int): Verbosity of logging
    """

    def __init__(self, configuration, file_paths: dict, debug_nodes: List[str], log_level: int = 0):
        """
        Initializes OSMData.

        Args:
            configuration (dict): Parsed YAML configuration.
            file_paths (dict): Dictionary of file paths used in the pipeline.
            debug_nodes (List[str]): List of node IDs for verbose output.
            log_level (int): Logging verbosity (0 = silent, 1 = verbose).
        """
        self.core_columns = ["osm_id", "item_name", "lat", "lon", "osm_category", "sub_category"]
        self.debug_nodes = debug_nodes
        self.log_level = log_level

        self.require_name = configuration.get("require_name", True)
        if self.require_name:
            self.log_msg("   ⏩ Note: Features without a 'name' tag will be dropped.")

        self.text_replace = TextSubstitutions(file_paths["substitution"], log_level)
        self.tag_counter = SignificantTagCounter(file_paths["ignore_tags"], debug_nodes, log_level)

        self.log_msg(f"Convert distances to meters: {'Enabled' if self.text_replace.convert_units else 'Disabled'}")
        self.enricher = EnrichmentManager(log_level)

        self.output_tag = configuration["output_tag"]
        self.feature_config = configuration["features"]

        self._feature_data = []
        self.counter = 0

    @property
    def feature_data(self):
        """Returns the in-memory list of extracted feature dictionaries."""
        return self._feature_data

    def extract_features(self, n: osmium.osm.Node, osm_category: str, sub_category) -> None:
        """
        Extracts feature values for an OSM node and appends results to internal list.
            The value of the feature is added if mode is MODE_FEATURE_VALUE.  The value
            may be normalized.
            Otherwise, the "presence" value is appended (e.g. 1.0 if item is present).
        Add any enrichment data available.

        Args:
            n (osmium.osm.Node): The OSM node object.
            osm_category (str): OSMCategory derived from matching tag in `output_tag`. (e.g. Natural)
            sub_category (str): SubCategory derived from matching tag in `output_tag`. (e.g. Peak)
            n_type (str): Node type (n, w, r)

        """
        #osm_id = str(n.id).strip()

        raw_osm_id = n.id
        name = n.tags.get("name")

        if isinstance(n, osmium.osm.Relation):
            # If the object is a relation, negate the ID to match osm2pgsql.
            osm_id = str(-raw_osm_id)
            if self.counter < 100:
                print(f"REL {name} {osm_id}")
                self.counter += 1
        else:
            # For nodes and ways, use the positive ID.
            osm_id = str(raw_osm_id)



        if self.require_name and not name:
            self.node_dbg(osm_id, "Dropped because 'name' tag is missing.")
            return #  do not add

        lat = n.lat if isinstance(n, osmium.osm.Node) else 0.0
        lon = n.lon if isinstance(n, osmium.osm.Node) else 0.0

        new_row = {
            "osm_id": osm_id, "item_name": name, "osm_category": osm_category, "sub_category": sub_category, "lat": lat,
            "lon": lon,
        }

        feature_values = {}
        warned_features = getattr(self, "_warned_features", set())

        # Extract specified features
        for feature, weight, mode in self._iter_feature_weights():
            if feature in self.core_columns:
                continue

            if feature == "tag_count":
                # Calculated feature - tag_count
                feature_values[feature] = self.tag_counter.count(n.tags, osm_id, self.log_level)
                continue

            raw_value = n.tags.get(feature)
            try:
                if raw_value is not None:
                    if mode == MODE_ASIS:
                        # Extract the raw string value and do nothing else.
                        feature_values[feature] = raw_value
                    elif mode == MODE_FEATURE_VALUE:
                        # Extract as numeric feature
                        raw_value = self.text_replace.substitute(raw_value)
                        value = normalize_units(
                            raw_value
                            ) if self.text_replace.convert_units else float(raw_value)
                        feature_values[feature] = value
                    else:
                        # Extract as presence indicator

                        # Log warning once per feature if the value is numeric-like
                        if feature not in warned_features:
                            try:
                                float(raw_value)
                                self.log_msg(
                                    f"⚠️ Feature '{feature}' may be numeric "
                                    f"('{raw_value}') but is configured as presence (1.0)."
                                    )
                                warned_features.add(feature)
                                self._warned_features = warned_features
                            except (ValueError, TypeError):
                                pass
                        # Assign presence indicator
                        feature_values[feature] = 1.0
                else:
                    if mode == MODE_ASIS:
                        feature_values[feature] = ""  # Default for strings is an empty string
                    else:
                        feature_values[feature] = 0.0 # Default for numeric/presence is 0.0
            except Exception as e:
                self.log_msg(f"⚠️ Failed to process tag '{feature}': {e}")
                feature_values[feature] = "" if mode == MODE_ASIS else 0.0

        new_row.update(feature_values)
        self.node_dbg(osm_id, f"Extracted {new_row}")

        # Add any enrichment data
        self.enricher.apply_to_node(osm_id, new_row, self.node_dbg)

        self._feature_data.append(new_row)

    def _iter_feature_weights(self) -> Iterator[Tuple[str, float, str]]:
        """
        Iterate the features config and yield (feature, weight, mode) triples.
        The weight field is optional. 0 will be returned if not present.

        YAML format:
            features:
              - wikipedia:
                  weight: 5
                  mode: "presence"
              - name:
                  mode: "presence"
              - prominence:
                  weight: 20
                  mode: "value"

        Returns:
            Iterator[Tuple[str, float, str]]: (feature, weight, mode)

        Raises:
            ValueError: If the feature definition is invalid or missing required keys.
        """
        for entry in self.feature_config:
            if isinstance(entry, dict) and len(entry) == 1:
                feature, config = next(iter(entry.items()))
                try:
                    mode = config["mode"]
                    if mode not in VALID_MODES:
                        raise ValueError(
                            f"Invalid mode '{mode}' for feature '{feature}'. "
                            f"Expected one of {sorted(VALID_MODES)}."
                        )
                    weight = config.get("weight", 0.0)
                    yield feature, weight, mode
                except KeyError as e:
                    raise ValueError(f"Missing required key in feature config for '{feature}': {e}")
            else:
                raise ValueError(f"Invalid feature config entry: {entry}")

    def output_features(self, filepath: Path) -> None:
        """
        Writes feature rows to a CSV file.

        Args:
            filepath (Path): Output file path.
        """
        import csv
        feature_keys = [f for f, _, _ in self._iter_feature_weights() if f not in self.core_columns]
        enrichment_keys = self.enricher.keys()
        dynamic_columns = sorted(set(feature_keys + enrichment_keys) - set(self.core_columns))
        columns = self.core_columns + dynamic_columns

        print(f"➡️ Saving extract file: {filepath}")
        print(f"   Rows: {len(self._feature_data)}")
        print(f"   Columns: {columns}")

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
            writer.writeheader()
            for row in self._feature_data:
                writer.writerow(row)

    def read_enrichment_file(self, path: Path, columns: List[str]):
        """
        Loads an enrichment CSV and registers it for lookup during extraction.

        Args:
            path (Path): Path to the enrichment CSV.
            columns (List[str]): List of enrichment column names to retain.
        """
        try:
            self.enricher.read_file(path, columns)
        except Exception as e:
            print(f"Enrichment File error: {e}")
            sys.exit(1)

    def node_dbg(self, osm_id: str, msg: str):
        """Logs debug messages for selected nodes."""
        if self.debug_nodes:
            if osm_id in self.debug_nodes:
                print(f"🟣 {osm_id}: {msg}")

    def log_msg(self, msg: str):
        """Logs general messages if log_level >= 1."""
        if self.log_level >= 1:
            print(msg)
