
# extract_osm.py
"""
Extracts configured features and tags from an OSM file to a CSV file.
"""
from pathlib import Path
import sys
from typing import Dict, Any

from ExtractOSM.osm_data import OSMData
from ExtractOSM.osm_handler import OSMHandler

class ExtractOsm:
    """
    Coordinates the extraction pipeline from raw OSM to a base CSV feature file.
    """
    def __init__(
            self,
            file_paths: Dict[str, Path],
            configuration: dict,
            log_level: int = 0,
    ) -> None:
        """Initializes the pure ExtractOsm pipeline."""
        self.file_paths = file_paths
        self.configuration = configuration
        self.log_level = log_level

        try:
            self._validate_configuration()
        except Exception as e:
            print(f"Error : {e}")
            sys.exit(1)

        debug_nodes = [str(n) for n in configuration.get("debug_ids", [])]

        self.osm_data = OSMData(
            configuration,
            file_paths,
            log_level=log_level,
            debug_nodes=debug_nodes,
        )
        self.display_config()

    def run(self) -> None:
        """Executes the OSM to CSV extraction workflow."""
        self.extract_nodes()
        self.output_nodes()

    def extract_nodes(self) -> None:
        """Parses the OSM file and extracts node features."""
        osm_handler = OSMHandler(self.osm_data, self.configuration)
        self.osm_data.log_msg(f"\n➡️ Reading OSM {self.file_paths['osm_path']}")
        osm_handler.apply_file(str(self.file_paths['osm_path']), locations=True)

    def output_nodes(self) -> None:
        """Writes extracted feature data to the output CSV."""
        self.osm_data.output_features(self.file_paths["output_path"])
        self.osm_data.log_msg("✅     Extract Done")

    def display_config(self) -> None:
        """Displays the loaded configuration for keys and features."""
        keys_conf = self.configuration.get("keys")
        if keys_conf:
            self.osm_data.log_msg("OSM Filters:")
            for key, subconf in keys_conf.items():
                filters = subconf.get("filters", [])
                self.osm_data.log_msg(f"  {key:<13}: {', '.join(str(f) for f in filters)}")

        features_conf = self.configuration.get("features", [])
        if isinstance(features_conf, list):
            # Extracts feature names from a list of single-key dictionaries
            value_str = ", ".join(
                list(d.keys())[0] for d in features_conf if isinstance(d, dict) and d
            )
        else:
            value_str = str(features_conf)
        self.osm_data.log_msg(f"{'Features':<15}: {value_str}")


    def _validate_configuration(self) -> None:
        """
        Checks the loaded configuration for common structural errors, like
        using a string for a 'filters' list. Fails fast with a helpful message.
        """
        keys_config = self.configuration.get("keys", {})
        if not isinstance(keys_config, dict):
            # This case should ideally be caught by a schema validator, but we check just in case.
            return

        for key, subconf in keys_config.items():
            if 'filters' in subconf:
                filters_value = subconf['filters']
                if not isinstance(filters_value, list):
                    # This is the error we want to catch!
                    error_message = f"""
    ❌ Configuration Error in the 'keys' section for the key '{key}':
       The 'filters' value is a string, but it MUST be a list (using hyphens in YAML).

       INCORRECT (found a string):
         {key}:
           filters: {filters_value}

       CORRECT (must be a list):
         {key}:
           filters:
             - {filters_value}
       
       Please add a hyphen (-) before the filter value in your YAML file.
"""
                    # Raise a ValueError to halt execution immediately.
                    raise ValueError(error_message)
