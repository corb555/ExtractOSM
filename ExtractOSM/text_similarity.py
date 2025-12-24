# text_similarity.py
"""Provides high-performance, configurable text similarity scoring.

This module contains functions for cleaning and comparing text strings to
determine their similarity. It is designed for the specific challenge of matching
real-world entity names which may have minor variations,
typos, or different word orders.

The core function, text_similarity, calculates a composite score by combining multiple rapidfuzz algorithms. This approach
mitigates the known weaknesses of individual algorithms: token_sort_ratio handles word reordering and typos, while partial_ratio
provides a check for core entity matching. The final score is the harmonic mean of these two metrics, which penalizes candidates
that do not score highly on both.
"""
import re
import string
from typing import List, Dict

import unicodedata

from rapidfuzz.fuzz import partial_ratio, token_sort_ratio

def text_similarity(text1: str, text2: str, text1_clean: str, text2_clean: str, mode: str) -> float:
    """Calculates a text similarity score.

    The  'harmonic_partial' mode calculates a composite score that is resilient to both word reordering
     and the addition or subtraction of terms.
     The noisewords are removed for the partial_ratio.

    Args:
        text1 (str): The first string to compare. Should be pre-normalized.
        text2 (str): The second string to compare. Should be pre-normalized.
        text1_clean (str): Text2 with aggressive noise word removal
        text2_clean (str): Text2 with aggressive noise word removal

        mode (str): The scoring strategy to use. Currently, supports
            'harmonic_partial'.

    Returns:
        float: The final, combined similarity score (0-100).

    Raises:
        ValueError: If an unknown mode is provided.
    """
    # Score 1: Measures overall similarity, tolerant of word order.
    score_token_sort = token_sort_ratio(text1, text2)

    # Score 2: Measures the best substring match, tolerant of extra words using aggressively cleaned text
    score_partial = partial_ratio(text1_clean, text2_clean)

    if mode == 'harmonic_partial':
        # This strategy combines the two scores using a harmonic mean, which
        # strongly penalizes cases where one score is high but the other is low.
        # This requires a candidate to match on both overall and core similarity.
        return harmonic_mean(score_token_sort, score_partial)
    else:
        raise ValueError(f"Unknown mode '{mode}' specified for text_similarity.")

def clean_text(
        text: str,
        noise_words_pattern: re.Pattern,
        cleaning_rules: List[Dict[str, str]],
) -> str:
    """
    Performs a robust cleaning and normalization pipeline on a string using
    a configurable set of regex substitutions.

    This function prepares raw text for  matching by executing a
    sequence of cleaning steps. It is designed to handle common data quality
    issues like accents, special characters, and inconsistent capitalization.

    The normalization process is as follows:
    1.  Unicode Normalization (NFKD): Decomposes characters and strips accents.
    2.  Lowercasing: Converts all characters to lowercase.
    3.  Noise Word Removal: Strips noise words using a pre-compiled regex.
    4.  Punctuation Removal: Strips all punctuation characters.
    5.  Whitespace Collapsing: Reduces multiple spaces to a single space.
    Args:
        text: The raw input string.
        noise_words_pattern: A pre-compiled regex for removing noise words.
        cleaning_rules: A list of dictionaries, where each dict contains a
                        'pattern' (regex string) and a 'replace' string.

    Returns:
        The cleaned and normalized string.
    """
    if not isinstance(text, str):
        return ""

    # Step 1: Unicode Normalization (to handle accents, special quotes, etc.)
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('utf-8', 'ignore')

    # Step 2: Lowercasing
    text = text.lower()

    # Step 3: Apply the configured list of regex prefix substitutions
    if cleaning_rules:
        for rule in cleaning_rules:
            text = re.sub(rule['pattern'], rule['replace'], text)

    # Step 4: Remove general noise words
    text = noise_words_pattern.sub("", text)

    # Step 5: Remove all remaining punctuation
    text = text.translate(str.maketrans('', '', string.punctuation))

    # Step 6: Collapse whitespace
    return " ".join(text.split())

def harmonic_mean(score1: float, score2: float) -> float:
    """Calculates the harmonic mean of two scores, safely handling zeros.

    The harmonic mean is a type of average that is strongly biased towards the
    smaller of the two values. It is used in this context to combine two
    different  metrics, ensuring that the final score is high only
    if *both* input scores are high.

    Args:
        score1 (float): The first score (0-100).
        score2 (float): The second score (0-100).

    Returns:
        float: The calculated harmonic mean.
    """
    # An epsilon is added to the denominator to prevent division by zero in the
    # edge case where both scores sum to  zero.
    epsilon = 1e-9
    if score1 <= 0 or score2 <= 0:
        return 0
    return (2 * score1 * score2) / (score1 + score2 + epsilon)