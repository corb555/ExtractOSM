from typing import List, Dict


def calculate_piecewise_score(value: float, curve_config: List[Dict[str, float]]) -> float:
    """Translates a raw input metric into a quality score via piecewise linear interpolation.

    This function implements a configurable utility curve, mapping any continuous
    input value (like distance, population, etc.) to a quality score. The curve
    is defined by a list of segments, where each segment specifies a linear
    score transition between a start and end value.

    This function is a powerful tool to translate a raw, continuous metric (like distance, population, or article
    length) into a more meaningful **"quality score"** that reflects human judgment and complex non-linear
    relationships.

    Use this function when a simple linear scaling (`value * coefficient`) is insufficient to capture the true
    importance of a feature.

    The function finds the correct segment for the input value and then
    calculates the score by linearly interpolating between that segment's
    start and end scores.

    Args:
        value (float): The raw metric value to be scored.
        curve_config (List[Dict[str, float]]): A list of dictionaries, where
            each dict defines a segment of the scoring curve. The list must
            be pre-sorted by 'end_value'. Each dict should contain
            'end_value', 'start_score', and 'end_score'.

    Returns:
        float: The calculated quality score. Returns 0 if the value falls
            beyond the final segment.
    """
    start_value = 0.0 # Renamed from start_distance

    for segment in curve_config:
        end_value = segment['end_value'] # Renamed

        if value <= end_value:
            start_score = segment['start_score']
            end_score = segment['end_score']

            segment_value_range = end_value - start_value # Renamed
            segment_score_range = start_score - end_score

            if segment_value_range <= 0:
                return start_score

            progress_in_segment = (value - start_value) / segment_value_range
            score = start_score - (progress_in_segment * segment_score_range)
            return score

        start_value = end_value # Renamed

    return 0.0


def prepare_and_validate_curve(curve_config: List[Dict[str, float]]) -> List[Dict[str, float]]:
    """
    Sorts and validates the metric score curve configuration.

    This function performs two critical operations:
    1.  It sorts the curve segments based on their 'end_value' to ensure they
        are in the correct processing order.
    2.  It validates the now-sorted curve to ensure it is logically consistent
        (i.e., contains no duplicate or non-increasing 'end_value's).

    Args:
        curve_config: The raw list of segment dictionaries from the YAML file.

    Returns:
        The sorted and validated list of segment dictionaries, ready for use.

    Raises:
        ValueError: If the configuration contains duplicate or non-increasing
                    'end_value's.
    """
    if not curve_config:
        return []

    # 1. Sort First: Always work with a predictably ordered list.
    sorted_config = sorted(curve_config, key=lambda x: x.get('end_value', 0))

    # 2. Validate: Check the now-sorted list for logical consistency.
    last_value = -1.0  # Start with a value less than any possible non-negative end_value
    for segment in sorted_config:
        end_value = segment.get('end_value')

        if end_value is None:
            raise ValueError(f"Configuration Error in 'metric_score_curve': Segment is missing the required 'end_value' key. Segment data: {segment}")

        if end_value <= last_value:
            # This catches both duplicate values and any remaining sort errors.
            raise ValueError(
                f"Configuration Error in 'metric_score_curve': Segments must have "
                f"strictly increasing 'end_value' values. Found segment '{segment.get('name', 'N/A')}' "
                f"with end_value {end_value}, which is not greater than the previous "
                f"segment's end_value of {last_value}."
            )
        last_value = end_value

    return sorted_config
