import yaml


class SignificantTagCounter:
    def __init__(self, yaml_path: str, debug_nodes, log_level):
        self.ignore_tags = self._load_ignore_tags(yaml_path, debug_nodes)
        self.unmatched_tags = set()
        self.log_level = log_level

    def _load_ignore_tags(self, path: str, debug_nodes) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                res = yaml.safe_load(f) or {}
                return res
        except FileNotFoundError:
            print(f"⚠️ Tag significance config not found: {path} (ignoring)")
            return {}
        except Exception as e:
            print(f"❌ Error loading tag significance config: {e}")
            return {}

    def count(self, tags, osm_id: str, log_level:int) -> int:
        count = 0
        seen_groups = set()

        for tag in tags:
            key = str(tag.k)
            behavior = self.ignore_tags.get(key, None)

            if isinstance(behavior, (int, float)):
                count += behavior
                #print(node_id, f"'{key}': 0 - Ignored\n")
                continue

            matched = False
            for pattern, rule in self.ignore_tags.items():
                if rule == "group" and pattern.endswith("*"):
                    prefix = pattern[:-1]
                    if key.startswith(prefix):
                        if prefix not in seen_groups:
                            seen_groups.add(prefix)
                            count += 1
                            #print(node_id, f"'{key}': wildcard '{pattern}' - Counted\n")
                        #else:
                            #print(node_id, f"'{key}': wildcard '{pattern}' - Skipped dup group\n")
                        matched = True
                        break

            if not matched and behavior is None:
                self.unmatched_tags.add(key)
                count += 1
                #print(node_id, f"'{key}':  Counted\n")

        #print(node_id, f"tag count: {count}")
        return count
