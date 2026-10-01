"""Normalize and compare real-world entity names.

The module separates text matching into two stages:

1. :func:`prepare_text` performs normalization once before candidate matching.
2. :func:`text_similarity` compares two prepared values and returns both the
   component scores and their conservative harmonic-mean score.

This is useful for  entity matching where  candidates may contain
minor spelling differences, reordered words, abbreviations, or extra terms.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import string
from typing import Iterable, Mapping
import unicodedata

from rapidfuzz.fuzz import partial_ratio, token_sort_ratio

DEFAULT_UNICODE_FORM = "NFKC"
VALID_UNICODE_FORMS = frozenset({"NFC", "NFD", "NFKC", "NFKD"})
ASCII_ENCODING = "ascii"
UTF8_ENCODING = "utf-8"


@dataclass(frozen=True, slots=True)
class CleaningRule:
    """Regex substitution applied during text preparation.

    Args:
        pattern: Regular expression to replace.
        replacement: Replacement string.
    """

    pattern: str
    replacement: str = ""


@dataclass(frozen=True, slots=True)
class TextPreprocessConfig:
    """Configuration for entity-name preprocessing.

    Args:
        unicode_form: Unicode normalization form. Use ``None`` to disable
            Unicode normalization.
        ascii_fold: If true, remove accents and characters that cannot be
            represented as ASCII. This is useful for some Latin-script datasets
            but should normally remain false for multilingual data.
        lowercase: Convert text to lowercase.
        remove_punctuation: Remove ASCII punctuation.
        cleaning_rules: Ordered regex substitutions applied before noise-word
            removal.
        noise_words: Words or phrases removed only from the aggressively cleaned
            representation.
    """

    unicode_form: str | None = DEFAULT_UNICODE_FORM
    ascii_fold: bool = False
    lowercase: bool = True
    remove_punctuation: bool = True
    cleaning_rules: tuple[CleaningRule, ...] = ()
    noise_words: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate configuration values."""
        if self.unicode_form is not None:
            normalized = self.unicode_form.upper()
            if normalized not in VALID_UNICODE_FORMS:
                valid = ", ".join(sorted(VALID_UNICODE_FORMS))
                raise ValueError(f"Invalid Unicode normalization form "
                                 f"{self.unicode_form!r}. Expected one of: {valid}, or None.")
            object.__setattr__(self, "unicode_form", normalized)


@dataclass(frozen=True, slots=True)
class PreparedText:
    """Precomputed text representations used by the similarity scorer."""

    normalized: str
    cleaned: str


@dataclass(frozen=True, slots=True)
class TextSimilarityResult:
    """Detailed fuzzy-text comparison result."""

    score: float | None
    token_sort_score: float | None
    partial_score: float | None

    @property
    def has_evidence(self) -> bool:
        """Return whether the comparison produced usable text evidence."""
        return self.score is not None


def prepare_text(
        text: object, config: TextPreprocessConfig | None = None, ) -> PreparedText:
    """Prepare an entity name for repeated fuzzy matching.

    Preparation should normally occur once when a source row is loaded.
    Candidate comparisons can then
    reuse the prepared representations.

    The normalization pipeline is:

    1. Unicode normalization, if configured.
    2. Optional ASCII folding.
    3. Lowercasing.
    4. Ordered regex substitutions.
    5. Optional punctuation removal.
    6. Whitespace collapse.

    The ``cleaned`` representation additionally removes configured noise words
    after regex substitutions and before punctuation removal.

    Args:
        text: Raw input value. Non-string values produce empty representations.
        config: Preprocessing options. Defaults to
            :class:`TextPreprocessConfig`.

    Returns:
        Prepared normalized and aggressively cleaned representations.
    """
    settings = config or TextPreprocessConfig()
    if not isinstance(text, str):
        return PreparedText(normalized="", cleaned="")

    normalized = _normalize_base(text, settings)
    cleaned = _remove_noise_words(normalized, settings.noise_words)

    return PreparedText(normalized=_finalize_text(normalized, settings.remove_punctuation),
        cleaned=_finalize_text(cleaned, settings.remove_punctuation), )


def text_similarity(
        first: PreparedText, second: PreparedText, ) -> TextSimilarityResult:
    """Compare two prepared entity names.

    ``token_sort_ratio`` compares normalized full names while tolerating
    word-order changes. ``partial_ratio`` compares aggressively cleaned names
    and tolerates extra or missing terms. Their harmonic mean is intentionally
    conservative: both comparisons must be reasonably strong for the final
    score to remain high.

    Empty or fully stripped names provide no text evidence and return ``None``
    scores instead of treating missing values as a successful match.

    Args:
        first: First preprocessed entity name.
        second: Second preprocessed entity name.

    Returns:
        Component scores and the combined harmonic-mean score.
    """
    if not (first.normalized and second.normalized and first.cleaned and second.cleaned):
        return TextSimilarityResult(score=None, token_sort_score=None, partial_score=None, )

    token_score = float(token_sort_ratio(first.normalized, second.normalized))
    partial_score = float(partial_ratio(first.cleaned, second.cleaned))

    return TextSimilarityResult(score=harmonic_mean(token_score, partial_score),
        token_sort_score=token_score, partial_score=partial_score, )


def harmonic_mean(first: float, second: float) -> float:
    """Return the harmonic mean of two non-negative scores.

    Args:
        first: First score.
        second: Second score.

    Returns:
        Harmonic mean, or ``0.0`` if either score is not positive.
    """
    if first <= 0.0 or second <= 0.0:
        return 0.0
    return (2.0 * first * second) / (first + second)


def cleaning_rules_from_mappings(
        rules: Iterable[Mapping[str, str]], ) -> tuple[CleaningRule, ...]:
    """Convert configuration mappings into immutable cleaning rules.

    This helper is convenient when rules are loaded from YAML.

    Each mapping must contain ``pattern`` and may contain either ``replacement``
    or ``replace``.

    Args:
        rules: Ordered configuration mappings.

    Returns:
        Parsed cleaning rules.

    Raises:
        ValueError: If a rule does not define a pattern.
    """
    parsed: list[CleaningRule] = []
    for index, rule in enumerate(rules):
        pattern = rule.get("pattern")
        if not pattern:
            raise ValueError(f"Cleaning rule {index} is missing 'pattern'.")

        replacement = rule.get("replacement", rule.get("replace", ""))
        parsed.append(CleaningRule(pattern=str(pattern), replacement=str(replacement), ))

    return tuple(parsed)


def _normalize_base(text: str, config: TextPreprocessConfig) -> str:
    """Apply normalization shared by full and aggressive representations."""
    result = text

    if config.unicode_form is not None:
        result = unicodedata.normalize(config.unicode_form, result)

    if config.ascii_fold:
        result = unicodedata.normalize("NFKD", result)
        result = (result.encode(ASCII_ENCODING, "ignore").decode(UTF8_ENCODING, "ignore"))

    if config.lowercase:
        result = result.lower()

    for rule in config.cleaning_rules:
        result = re.sub(rule.pattern, rule.replacement, result)

    return result


def _remove_noise_words(text: str, noise_words: tuple[str, ...]) -> str:
    """Remove configured noise words or phrases using safe boundaries."""
    terms = [term.strip() for term in noise_words if term.strip()]
    if not terms:
        return text

    alternatives = "|".join(re.escape(term) for term in sorted(terms, key=len, reverse=True))
    pattern = re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", flags=re.IGNORECASE, )
    return pattern.sub(" ", text)


def _finalize_text(text: str, remove_punctuation: bool) -> str:
    """Apply final punctuation removal and whitespace normalization."""
    result = text
    if remove_punctuation:
        result = result.translate(str.maketrans("", "", string.punctuation))
    return " ".join(result.split())
