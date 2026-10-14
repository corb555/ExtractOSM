def prepare_text(raw_text, config):
    passides a text-matching utility for comparing real-world entity names that may differ in spelling, punctuation, 
word order, abbreviations, or the presence of extra words.

Its strength is that it combines two complementary fuzzy-matching strategies: one compares the full set of words while ignoring word order, 
and the other looks for a strong partial match after more aggressive cleanup. The harmonic mean then penalizes cases where only one of 
those comparisons looks strong, which helps reduce false positives in messy real-world names.

## Stages
The module  separates matching into two stages:

1. **Preprocessing** with `prepare_text()`
2. **Comparison** with `text_similarity()`

This separation allows text to be normalized once and then compared repeatedly without repeating the cleanup work.

## Inputs

The primary input to `prepare_text()` is a raw text value plus an optional `TextPreprocessConfig`.

```python
prepared = prepare_text(raw_text, config)
```

`prepare_text()` returns a `PreparedText` object containing two representations:

```python
PreparedText(
    normalized="...",
    cleaned="...",
)
```

### `normalized`

The normalized representation preserves the meaningful words in the original text while applying the configured normalization rules.

It is used by RapidFuzz's `token_sort_ratio()`.

### `cleaned`

The cleaned representation starts from the same normalized text but additionally removes configured noise words or phrases.

It is used by RapidFuzz's `partial_ratio()`.

Keeping both forms is intentional. The full normalized name is useful for evaluating the overall similarity of two names, while the more aggressively cleaned form can recognize cases where descriptive or unimportant words differ.

Non-string input produces empty prepared values. Empty or fully stripped values are treated as **no comparison evidence**, rather than as a successful match.

## Preprocessing

Text preprocessing is controlled by `TextPreprocessConfig`.

The default configuration is:

```python
TextPreprocessConfig(
    unicode_form="NFKC",
    ascii_fold=False,
    lowercase=True,
    remove_punctuation=True,
    cleaning_rules=(),
    noise_words=(),
)
```

The preprocessing pipeline is:

1. Unicode normalization
2. Optional ASCII folding
3. Lowercasing
4. Configured regex substitutions
5. Noise-word removal for the `cleaned` representation
6. Optional punctuation removal
7. Whitespace normalization

### Unicode normalization

`unicode_form` may be one of:

- `NFC`
- `NFD`
- `NFKC`
- `NFKD`
- `None`

The default is `NFKC`.

Unicode normalization helps make visually or semantically equivalent Unicode representations compare consistently.

For example, compatibility normalization can reduce differences caused by alternate Unicode representations of the same text.

Set `unicode_form=None` to disable Unicode normalization.

### ASCII folding

`ascii_fold=True` converts text toward an ASCII-only representation by first decomposing characters with NFKD and then dropping characters that cannot be represented in ASCII.

This can be useful for datasets where accented Latin characters should compare with their unaccented equivalents:

```text
José  → jose
Cañón → canon
```

ASCII folding is disabled by default because it is destructive for many non-Latin writing systems.

Unicode normalization and ASCII folding are therefore separate options.

### Lowercasing

With the default `lowercase=True`, capitalization differences are removed:

```text
Mount Rainier
MOUNT RAINIER
mount rainier
```

all normalize to the same lowercase form.

### Cleaning rules

`cleaning_rules` is an ordered collection of regex substitutions.

Each rule contains:

```python
CleaningRule(
    pattern="...",
    replacement="...",
)
```

Rules are applied in order before noise-word removal.

They are useful for dataset-specific transformations such as standardizing known abbreviations or removing known prefixes.

`cleaning_rules_from_mappings()` is provided as a convenience for creating rules from configuration dictionaries, including data loaded from YAML.

For example:

```yaml
cleaning_rules:
  - pattern: "\bmt\.?\b"
    replacement: "mount"

  - pattern: "\bst\.?\b"
    replacement: "saint"
```

### Noise words

`noise_words` contains words or phrases that should be removed from the aggressively cleaned representation.

Noise words are removed using word boundaries so that removing a word does not unintentionally remove the same sequence of characters from inside another word.

For example, a dataset might consider terms such as:

```text
restaurant
company
store
location
```

to be less useful when deciding whether two entity names refer to the same thing.

Noise words are removed only from `PreparedText.cleaned`. The normalized representation retains them.

That distinction is important because the two similarity metrics intentionally examine different views of the same name.

### Punctuation and whitespace

If `remove_punctuation=True`, ASCII punctuation is removed after the configurable substitutions and noise-word processing.

Finally, repeated whitespace is collapsed and leading/trailing whitespace is removed.

## Comparison

`text_similarity()` compares two `PreparedText` objects:

```python
result = text_similarity(first, second)
```

It returns:

```python
TextSimilarityResult(
    score=...,
    token_sort_score=...,
    partial_score=...,
)
```

All scores use the RapidFuzz `0` to `100` similarity scale.

If either prepared input does not contain enough text to provide meaningful evidence, the scores are `None`.

## `token_sort_ratio`

`token_sort_ratio()` comes from `rapidfuzz.fuzz`.

Its purpose here is to compare the **overall names while being tolerant of word order**.

Conceptually, it tokenizes the strings, sorts the tokens, and then evaluates their similarity.

This makes comparisons such as:

```text
Smith Family Restaurant
Family Restaurant Smith
```

much less sensitive to ordering than a direct character-by-character comparison.

It is also useful when two sources contain the same important terms but arrange them differently.

The matcher applies `token_sort_ratio()` to the `normalized` representations:

```python
token_sort_ratio(
    first.normalized,
    second.normalized,
)
```

This score answers roughly:

> Do these two complete names contain substantially the same textual content, even if the words are arranged differently?

## `partial_ratio`

`partial_ratio()` also comes from `rapidfuzz.fuzz`.

It looks for a strong partial-string relationship rather than requiring the entire strings to align equally well.

This is valuable when one data source contains extra words that another source omits.

For example:

```text
Joe's Pizza
Joe's Pizza Downtown Seattle
```

A full-string comparison is penalized by the additional words, while a partial comparison can recognize the strong shared core.

The matcher applies `partial_ratio()` to the more aggressively cleaned representations:

```python
partial_ratio(
    first.cleaned,
    second.cleaned,
)
```

This score answers roughly:

> Is the important core of one name strongly represented within the other?

## Why use both scores?

Neither metric is sufficient by itself for messy real-world names.

`token_sort_ratio()` is very good at reordered words, but it can be penalized when one source includes meaningful-looking extra terms.

`partial_ratio()` is very good at missing or extra words, but a strong substring match can sometimes look better than the overall names really deserve.

The two metrics therefore provide complementary evidence:

| Metric | Primary strength |
| --- | --- |
| `token_sort_ratio` | Word reordering and overall name agreement |
| `partial_ratio` | Extra or missing terms and shared core names |

The module combines them rather than allowing either one to determine the match independently.

## Why the harmonic mean?

The final similarity score is the harmonic mean of the two RapidFuzz scores:

```text
                    2 × token_score × partial_score
final_score = ---------------------------------------------
                         token_score + partial_score
```

The harmonic mean is deliberately conservative.

Unlike the ordinary arithmetic mean, it is strongly influenced by the lower of the two values. A candidate therefore needs to perform reasonably well under **both** comparison strategies to receive a high final score.

For example:

```text
token_sort_score = 95
partial_score    = 95
harmonic score   = 95
```

Both metrics strongly agree, so the final score remains high.

But:

```text
token_sort_score = 95
partial_score    = 40
harmonic score   ≈ 56
```

One metric sees a strong match while the other sees a significant weakness. The harmonic mean pulls the final score toward the weaker evidence.

This behavior is useful for entity matching because fuzzy metrics can occasionally produce deceptively high scores for different reasons. Requiring both the overall-name comparison and the core-name comparison to perform well reduces those false positives.

The implementation returns `0.0` if either component score is zero or negative.

## Detailed results

The comparison result preserves all three scores:

```python
result.token_sort_score
result.partial_score
result.score
```

This is useful for debugging and threshold tuning.

For example, two candidate pairs may have similar final scores for very different reasons:

```text
Candidate A
token_sort_score = 91
partial_score    = 70

Candidate B
token_sort_score = 79
partial_score    = 80
```

Keeping the component scores makes those differences visible instead of hiding everything behind a single number.

`TextSimilarityResult.has_evidence` provides a convenient way to distinguish a real comparison from missing or unusable text.

## Typical usage

```python
config = TextPreprocessConfig(
    unicode_form="NFKC",
    ascii_fold=False,
    noise_words=("restaurant", "company"),
)

first = prepare_text("Joe's Restaurant", config)
second = prepare_text("Joes", config)

result = text_similarity(first, second)

if result.has_evidence:
    print(result.score)
    print(result.token_sort_score)
    print(result.partial_score)
```

For workloads that compare one value against many candidates, prepare each input once and reuse the resulting `PreparedText` objects.

```python
prepared_names = [
    prepare_text(name, config)
    for name in names
]
```

This avoids repeatedly performing Unicode normalization, regex substitutions, noise-word removal, and punctuation cleanup during candidate comparisons.

## Design summary

The module intentionally separates concerns:

```text
raw text
    ↓
prepare_text()
    ↓
normalized representation
cleaned representation
    ↓
text_similarity()
    ↓
token_sort_ratio
partial_ratio
    ↓
harmonic mean
    ↓
TextSimilarityResult
```

The design is intended for real-world entity names where differences in formatting, word order, spelling, and optional descriptive terms make exact string comparison unreliable.
