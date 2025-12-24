
# --- Test Cases  ---
SHOULD_MATCH = [
    ("graduate seattle", "graduate by hilton seattle"),
    ("phnom penh noodle house", "phnom penh noodle house"),
    ("sheraton grand seattle", "sheraton seattle"),
    ("jellyfish brewing greenlake", "jellyfish brewing"),
    ("hotwire coffee house", "hotwire online coffeehouse"),
    ("arosa cafe", "arosa cafe"),
    ("blue moon", "blue moon tavern"),
    ("dark room lounge", "dark room"),
    ("mulleadys irish pub", "mulleadys"),
    ("armistice coffee", "armistice"),
    ("wild ginger restaurant", "wild ginger seattle"),
    ("gallito restaurant", "gallito"),
    ("seattle hyatt regency", "hyatt regency seattle"),
    ("ampersand cafe alki", "ampersand"),
    ("queen mary", "queen mary tea room"),
    ("crowne plaza hotel", "crowne plaza seattle downtown"),
    ("barolo restaurant", "barolo ristorante"),
    ("marination columbia city", "marination"),
    ("taneda", "taneda sushi"),
    ("mor fire", "morfire"),
    ("mc gilvras bar restaurant", "mcgilvras"),
]

SHOULD_NOT_MATCH = [
    ("belltown pizza", "belltown pizza game room"),
    ("madison park bakery", "cactus madison park"),
    ("biscuit bitch belltown", "belltown inn"),
    ("evergreens salads columbia center", "elemental pizza columbia center"),
    ("oriental market", "sanitary public market"),
    ("pizza mart", "big marios pizza"),
    ("wines washington tasting ro", "white horse trading" ),
    ("belltown market", "macrina bakery belltown" ),
    ("grand hyatt seattle", "chan seattle"),
    ("cocina de oaxaca", "social tea"),
    ("chebogz beacon hill", "beacon hill"),
]

import pytest
import re
from ExtractOSM.geo_fuzzy import clean_text, text_similarity


CLEANING_RULES = [
    {"pattern": r'\b(mc|mac)\s?', "replace": 'mac'},  # Handles Mc and Mac
    {"pattern": r'\b(o)\s?\'', "replace": 'o'}, # Handles O'
]

NOISE_WORDS_1 = {
    "the", "a", "an", "of", "el", "at", "le", "la", "inc", "llc", "and", "co", "by",
    "corp", "company", "on", "ltd","pizza"
}

NOISE_WORDS_2 = {
    "bar", "pub", "restaurant", "cafe", "deli", "grill", "market", "seattle", "pizza",
    "lounge", "downtown", "tavern", "sushi", "taqueria", "bakery",
    "west seattle",
    "beacon hill", "ballard", "wallingford", "capital hill", "pioneer square",
    "phinney", "queen anne", "u district", "u village", "belltown", "lake union",

    "westlake", "eastlake", "greenlake", "fremont", "leschi", "madrona",
    "columbia city", "madison park","columbia center"
}

# --- Pytest Fixture to Prepare Cleaning Tools ---
@pytest.fixture(scope="module")
def text_cleaners():
    """Pre-compiles regex patterns for text cleaning."""
    noise_pattern1 = re.compile(r"\b(" + "|".join(re.escape(w) for w in NOISE_WORDS_1) + r")\b", re.IGNORECASE)

    all_noise_words2 = set(NOISE_WORDS_1 | NOISE_WORDS_2)
    noise_pattern2 = re.compile(r"\b(" + "|".join(re.escape(w) for w in all_noise_words2) + r")\b", re.IGNORECASE)

    return {
        "rules": CLEANING_RULES,
        "noise_pattern1": noise_pattern1,
        "noise_pattern2": noise_pattern2
    }

THRESHOLD = 65
MODE = "harmonic_partial" # Define the mode we are testing

# === Test Suite for the 'harmonic_partial' Scoring Mode ===

@pytest.mark.parametrize("name1, name2", SHOULD_MATCH)
def test_should_match(name1, name2, text_cleaners):
    """Tests that the scoring mode correctly identifies good matches."""
    rules = text_cleaners["rules"]
    noise1 = text_cleaners["noise_pattern1"]
    noise2 = text_cleaners["noise_pattern2"]

    cleaned1_aux = clean_text(name1, noise1, rules)
    cleaned1_target = clean_text(name2, noise1, rules)

    cleaned2_aux = clean_text(name1, noise2, rules)
    cleaned2_target = clean_text(name2, noise2, rules)

    # Call the text_similarity function with all four cleaned strings
    score = text_similarity(
        cleaned1_aux, cleaned1_target,
        cleaned2_aux, cleaned2_target,
        mode=MODE
    )

    print(f"'{name1}' vs '{name2}' -> Score: {score:.2f}")
    assert score >= THRESHOLD, f"Expected a high score for '{name1}' vs '{name2}', but got {score:.2f}"


@pytest.mark.parametrize("name1, name2", SHOULD_NOT_MATCH)
def test_should_not_match(name1, name2, text_cleaners):
    """Tests that the scoring mode correctly rejects bad matches."""
    rules = text_cleaners["rules"]
    noise1 = text_cleaners["noise_pattern1"]
    noise2 = text_cleaners["noise_pattern2"]

    cleaned1_aux = clean_text(name1, noise1, rules)
    cleaned1_target = clean_text(name2, noise1, rules)

    cleaned2_aux = clean_text(name1, noise2, rules)
    cleaned2_target = clean_text(name2, noise2, rules)

    score = text_similarity(
        cleaned1_aux, cleaned1_target,
        cleaned2_aux, cleaned2_target,
        mode=MODE
    )

    print(f"'{name1}' vs '{name2}' -> Score: {score:.2f}")
    assert score < THRESHOLD, f"Expected a low score for '{name1}' vs '{name2}', but got {score:.2f}"