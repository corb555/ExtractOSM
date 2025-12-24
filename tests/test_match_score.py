import pytest


@pytest.mark.parametrize(
    "name1, name2, dist, expected_score, expected_text_score",
    [
        # Test lexical matching with small distance
        ("Grand Canyon", "Grand Canyon", 0, 100, 100), # Perfect match, zero distance
        ("Upper Falls", "Falls", 0, 81, 81), # Match with noise words removed
        ("Yosemite Park", "Yosemite National Park", 10, 74, 74), # Mismatch, but similar words

        # Tests with larger distance
        ("Old Faithful", "Faithful Geyser", 90, 40, 64), # Distant match should reduce the final score
        ("Zion", "Bryce Canyon", 100, 0, 25), # Distant and not very similar
        ("El Capitan", "El Capitan", 150, 70, 100),  # Distance beyond max – should clamp
        ("Bear Pit Lounge",	"Old Faithful Inn Dining Room",	55, 15, 24),

        # Empty input should return 0, near empty returns low score
        (" ", "Mount Hood", 10, 0, 0),
        ("", "Mount Hood", 10, 0, 0),
        (None, "Mount Hood", 10, 0, 0),

        # ✅ Slightly above a "reasonably good" threshold
        ("Mount Rainier", "Mt Rainier", 5, 87, 87),  # abbreviation
        ("Half Dome", "The Half Dome", 10, 91, 91),  # minor prefix
        ("Devils Tower", "Devil's Tower", 3, 96, 96),  # punctuation
        ("Great Sand Dunes", "Sand Dunes", 5, 77, 77),  # compound name partial

        # ❌ Slightly below a "reasonably good" threshold
        ("Crater Lake", "Crater Lake National Park", 8, 61, 61),  # suffix
        ("Mount Olympus", "Olympus Mons", 8, 88, 88),  # geographic name reused elsewhere
        ("Mt. Hood", "Mt Shasta", 12, 23, 23),  # similar form, wrong place
        ("Red Rock", "Redwood", 7, 52, 59),  # lexical similarity, semantic mismatch
        ("Lake Powell", "Powell River", 10, 52, 52),  # swapped noun suffix
        ("Stone Mountain", "Rock Hill", 20, 25, 26),  # semantic similar, lexically weak
    ]
)


def test_match_score(name1, name2, dist, expected_text,  expected_score):
    clean1 = clean_text(name1 or "", NOISE_WORDS)
    clean2 = clean_text(name2 or "", NOISE_WORDS)

    combined, text_score = match_score(name1, name2, clean1, clean2, dist, MAX_DISTANCE_METERS)
    print(f"\nTest: Dist: {dist}, '{name1}' vs '{name2}'")
    print(f"  Score={combined:.1f} TextScore: {text_score:.1f}")

    # Use percent error where expected > 0; otherwise use absolute
    if expected_text > 0:
        assert abs(text_score - expected_text) / expected_text < TOLERANCE_PCT
    else:
        assert text_score == 0

    if expected_score > 0:
        assert abs(combined - expected_score) / expected_score < TOLERANCE_PCT
    else:
        assert combined == 0
