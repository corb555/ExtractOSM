import re
from typing import Optional
import warnings

#  unit keyword sets
FEET_UNITS = ("'", "ft", "feet")
METER_UNITS = ("m",)
TIME_UNITS = ("minutes", "minute", "hours", "hour")

FEET_REGEX = r"\b(?:ft|feet)\b|'"
METER_REGEX = r"\bm\b"
TIME_REGEX = r"\b(?:hours?|minutes?)\b"


def to_meters(val: str, allow_multiple=True) -> Optional[float]:
    """
    Convert a string value to meters. Handles unit-suffixed values and numeric ranges.

    Args:
        val (str): The value to convert to meters.
        allow_multiple (bool): If True, allows multiple numeric values/ranges and averages them.

    Returns:
        Optional[float]: The average value in meters, or None if the input cannot be parsed.
    """
    if not val or not isinstance(val, str):
        return None

    original_val = val
    val = val.strip().lower().replace(",", " ")

    # Determine unit
    unit = "ft" if any(u in val for u in FEET_UNITS) else "m"

    # Remove unit strings
    val = re.sub(r"\b(feet|ft|m)\b|'", "", val).strip()

    try:
        if allow_multiple:
            matches = re.findall(r"(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?", val)
            numbers = [(float(a) + float(b)) / 2 if b else float(a) for a, b in matches if a]
            if not numbers:
                raise ValueError("No valid numbers found")
            avg_val = sum(numbers) / len(numbers)
        else:
            match = re.fullmatch(r"(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?", val)
            if not match:
                raise ValueError("Invalid single value or range format")
            values = [float(match.group(1))]
            if match.group(2):
                values.append(float(match.group(2)))
            avg_val = sum(values) / len(values)

        return round(avg_val * 0.3048 if unit == "ft" else avg_val, 1)

    except (ValueError, TypeError) as e:
        warnings.warn(f"Invalid format for height: '{original_val}'. Error: {e}")
        return None


def to_minutes(val: str) -> Optional[float]:
    """
    Convert a string value to minutes. Handles input with units "minutes" or "hours".

    Args:
        val (str): A string representing a duration.

    Returns:
        Optional[float]: Duration converted to minutes, or None if parsing fails.
    """
    if not val or not isinstance(val, str):
        return None

    original_val = val
    val = val.strip().lower()

    unit = "hours" if "hour" in val else "minutes"

    # Remove unit strings
    val = re.sub(TIME_REGEX + r"|'", "", val).strip()

    try:
        match = re.fullmatch(r"(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?", val)
        if not match:
            raise ValueError("Invalid time format")

        value = float(match.group(1))
        res = round(value * 60 if unit == "hours" else value, 1)
        return res

    except (ValueError, TypeError) as e:
        warnings.warn(f"Invalid format for time: '{original_val}'. Error: {e}")
        return None


def normalize_units(val: str) -> Optional[float]:
    """
    Parse a tag value string and attempt to convert it to a float.
    Normalizes values with units:
       distance units are converted to meters
       time units are converted to minutes

    Args:
        val (str): Input tag value string.

    Returns:
        Optional[float]: Numeric value in standard units (meters or minutes), or None if
        unparseable.
    """
    if not val or not isinstance(val, str):
        return None

    val = val.strip().lower()

    if any(u in val for u in FEET_UNITS) or re.search(FEET_REGEX, val):
        return to_meters(val, allow_multiple=True)
    if any(u in val for u in METER_UNITS) and re.fullmatch(r"[\d.]+\s*m", val):
        return to_meters(val, allow_multiple=True)
    if any(u in val for u in TIME_UNITS):
        return to_minutes(val)

    try:
        return float(val)
    except ValueError:
        return None
