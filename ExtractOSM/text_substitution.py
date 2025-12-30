from pathlib import Path
import re

import yaml


class TextSubstitutions:
    def __init__(self, yaml_path:Path, log_level):
        self.substitutions = {}
        self.convert_units = False
        self.yaml_path = yaml_path
        if self.yaml_path:
            self._load_config(yaml_path, log_level)

    def _load_config(self, yaml_path: Path, log_level):
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as e:
            raise ValueError(f"Failed to read substitution file: '{yaml_path}': {e}")

        if not isinstance(data, dict) or "substitutions" not in data:
            raise ValueError("Missing 'substitutions' section in YAML config.")

        subs = data["substitutions"]
        if not isinstance(subs, dict):
            raise ValueError("'substitutions' must be a dictionary.")

        self.substitutions = subs
        self.convert_units = bool(data.get("convert_units", False))

    def substitute(self, val: str) -> str:
        if not isinstance(val, str) or not self.yaml_path:
            return val

        val = val.strip().lower()
        for pattern, replacement in self.substitutions.items():
            val = re.sub(pattern, replacement, val)
        return val
