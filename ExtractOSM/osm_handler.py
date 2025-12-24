import osmium
from osmium import osm, filter as osm_filter

from ExtractOSM.osm_data import OSMData


class OSMHandler(osmium.SimpleHandler):
    """
    Osmium handler for filtering OSM data and extracting fields based on config.
    """

    def __init__(self, osm_data: OSMData, configuration):
        super().__init__()
        self.osm_data = osm_data

        # Load  filter keys and values
        self.filter_keys = configuration.get("keys", {})
        self.feature_weights = configuration.get("features", {})

    def node(self, n: osmium.osm.Node):
        self.process_osm_element(n)

    def way(self, w: osmium.osm.Way):
        self.process_osm_element(w)

    def relation(self, r: osmium.osm.Relation):
        self.process_osm_element(r)

    def process_osm_element(self, n):
        # ✅ Match if any key-value pair matches the config
        for osm_category, config in self.filter_keys.items():
            sub_category = n.tags.get(osm_category)
            if sub_category in config.get("filters", []):
                self.osm_data.extract_features(n, osm_category, sub_category)
                return

    def apply_file(self, filename, locations=False, idx='flex_mem', filters=None):
        # ✅ Set Osmium TagFilter
        tag_pairs = []
        for key, config in self.filter_keys.items():
            tag_pairs.extend((key, val) for val in config.get("filters", []))

        tag_filter = osm_filter.TagFilter(*tag_pairs).enable_for(osm.NODE)
        filters = [tag_filter]

        # Apply file.  Osmium will call node, way, relation for each node in the file
        super().apply_file(filename, locations=locations, idx=idx, filters=filters)
