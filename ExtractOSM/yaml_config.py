from pathlib import Path
from typing import List, Dict, Union

from cerberus import Validator
import yaml


def read_config(config_file: Path, schema: Dict) -> dict:
    """
    Load and validate a YAML configuration file against a Cerberus schema.

    Args:
        config_file (Path): Path to the YAML configuration file.
        schema (dict): Cerberus schema to validate against.

    Returns:
        dict: Parsed YAML content if valid.

    Raises:
        FileNotFoundError: If the YAML file does not exist.
        ValueError: If the YAML file fails schema validation.
    """
    if not config_file.exists():
        raise FileNotFoundError(f"Configuration file '{config_file}' not found.")

    with config_file.open("r", encoding="utf-8") as f:
        yml_config = yaml.safe_load(f)

    validator = Validator(schema)
    if not validator.validate(yml_config):
        formatted_errors = "\n".join(format_cerberus_errors(validator.errors))
        raise ValueError(f"❌ Error: {config_file} has errors:\n{formatted_errors}")

    return yml_config


def format_cerberus_errors(errors: Dict[str, Union[List, Dict]], prefix: str = "") -> List[str]:
    """
    Recursively format Cerberus validation errors for display.

    Args:
        errors (dict): Nested error dictionary from cerberus.errors.
        prefix (str): Field path prefix used for nested keys.

    Returns:
        list[str]: Flattened list of error messages.
    """
    messages = []
    for field, issues in errors.items():
        full_field = f"{prefix}.{field}" if prefix else field
        if isinstance(issues, list):
            for issue in issues:
                if isinstance(issue, dict):
                    messages.extend(format_cerberus_errors(issue, prefix=full_field))
                else:
                    messages.append(f"  ❗ {full_field}: {issue}")
        elif isinstance(issues, dict):
            messages.extend(format_cerberus_errors(issues, prefix=full_field))
    return messages
