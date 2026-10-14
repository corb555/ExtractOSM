# Wikipedia / Wikidata Collection Usage

## 1. Purpose

The Wikipedia/Wikidata collection tools collect enrichment data from wikipedia that can help rank  
relative importance.

For each candidate Wikipedia article, the collection stage gathers:

- **Wikipedia article length** — a rough proxy for how extensively the item is documented.
- **Wikipedia incoming link count** — a rough proxy for how connected or prominent the subject is within Wikipedia.
- **Wikipedia summary text** — retained as supporting context for later validation or analysis.
- **Wikidata entity type** — the associated Wikidata item and its `instance of` values. `instance of` identifies what kind of thing the item represents, 
such as a glacier, valley, mountain, film, person, or business.

However, simply matching a name with a Wikipedia title  does not guarantee 
that the article actually describes the same feature.

Short or ambiguous names can resolve to unrelated articles. For example, a feature named `Octopus`, `Topaz`, or `Pine` 
may resolve to an animal, mineral, or plant article rather than the intended feature represented in the source. An 
incorrect match can contribute a large Wikipedia prominence score and significantly distort the feature ranking. Conversely,
rejecting a valid match can reduce the Wikipedia prominence score which also distorts the feature ranking.

The tools are therefore split into two stages:

1. **Collection** — collect candidate Wikipedia articles and Wikidata with matching names and cache the data.
2. **Validation** — compare the collected Wikidata `instance of` values with the feature type expected from the 
 `sub_category`.

> Only validated Wikipedia  metrics should be used by a feature-ranking model.

---

## 2. Data Flow

```text
Feature CSV
    |
    |  item_name
    |  wikipedia
    |  sub_category
    v
wikipedia_fetch.py
    |
    |-- preprocess ambiguous short fallback names
    |-- query Wikipedia
    |-- collect article metadata
    |-- collect Wikidata QID
    |-- query Wikidata P31
    v
Wikipedia/Wikidata JSON cache
    |
    +--> enhancement CSV
    |
    v
wikipedia_validate.py
    |
    |-- construct the lookup title
    |-- read cached P31 values
    |-- compare P31 against sub_category rules
    v
validation CSV
    |
    v
ranking tool
```

---

# 3. `wikipedia_fetch.py`

## Purpose

This collects raw Wikipedia and Wikidata properties.

For each feature it attempts to determine a Wikipedia title and then collects:

- article byte length
- incoming Wikipedia link count
- introductory article summary
- disambiguation-page flag
- Wikidata item ID
- direct Wikidata `P31` (`instance of`) values

The tool writes:

1. a reusable JSON metadata cache
2. a compact enhancement CSV containing ranking-oriented Wikipedia fields

---

## 4. Wikipedia Lookup Rules

### Explicit Wikipedia tag

If the input feature contains a `wikipedia` value, that title is used directly.

Supported forms include:

```text
https://en.wikipedia.org/wiki/Death_Valley
en:Death Valley
Death Valley
```

### Fallback to `item_name`

If no Wikipedia tag is available, the feature `item_name` is used as the lookup title.

Example:

```text
item_name = "Owens Valley"
lookup    = "Owens Valley"
```

### Short-name context augmentation

Short or ambiguous fallback names can be augmented with their  `sub_category` before the Wikipedia lookup.

This helps prevent ambiguous names such as:

```text
Topaz
Octopus
Pine
Spa
Dome
Bunker
Trap
```

from resolving to unrelated articles or missing valid items

Name Augmentation Example:

```text
item_name     = "Topaz"
sub_category  = "hot_spring"

lookup title  = "Topaz hot spring"
```

Augmentation applies only to `item_name` lookups. Explicit Wikipedia tags are not modified.

---

# 5. Lookup Preprocessing Configuration

Default file:

```text
config/wiki_lookup_preprocessing.yml
```

Example:

```yaml
augment_context:

  viewpoint:
    max_length: 10
    append: viewpoint

  sand:
    max_length: 9
    append: sand

  cave_entrance:
    max_length: 10
    append: cave

  hot_spring:
    max_length: 10
    append: hot spring

  valley:
    max_length: 8
    append: valley
    synonyms:
      - basin
      - canyon
      - gorge
      - flat
      - gulch
```

## `max_length`

If the fallback feature name is at or below the configured maximum length, the context phrase is appended.

Example:

```text
Pine
```

with:

```yaml
hot_spring:
  max_length: 10
  append: hot spring
```

becomes:

```text
Pine hot spring
```

The lookup code may also augment one-word names regardless of character length if that behavior is enabled in the current implementation.

## `append`

Text to append to the fallback lookup title. 

Example:

```yaml
append: hot spring
```

produces:

```text
Octopus hot spring
```

## `synonyms`

Optional terms that indicate the feature name already contains sufficient category context.

Example:

```yaml
valley:
  append: valley
  synonyms:
    - basin
    - canyon
    - gorge
    - flat
    - gulch
```

These names are therefore left unchanged:

```text
Blue Basin
Mule Canyon
Fox Flat
Dry Gulch
```

> Note: The append word itself, its simple plural form, and configured synonyms suppress augmentation.

For example:

```text
Ice Caves
```

does not become:

```text
Ice Caves cave
```

---

# 6. Wikipedia Metadata Collected

The Wikipedia API request collects:

```text
info
linkshere
extracts
pageprops
```

The cache stores:

```json
{
  "en:Owens Valley": {
    "wikipedia_length": 26826,
    "wikipedia_links": 0,
    "summary": "...",
    "disambiguation": false,
    "wikidata_id": "Q2042674",
    "instance_of": [
      "Q39816"
    ],
    "wikidata_fetched": true
  }
}
```

## `wikipedia_length`

Wikipedia article size in bytes.

Used as a rough proxy for article depth or prominence.

## `wikipedia_links`

Number of incoming Wikipedia links returned by the API request.

The API result is intentionally capped by the request limit. Extremely highly linked articles therefore saturate instead of requiring continuation requests.

## `summary`

Plain-text introductory extract from the article.

This is retained in the cache so future semantic validation strategies can be developed without re-querying Wikipedia.

## `disambiguation`

`true` when Wikipedia identifies the page as a disambiguation page.

A disambiguation page is not considered a resolved feature identity and should normally be rejected by validation.

## `wikidata_id`

The Wikidata QID associated with the Wikipedia page.

Example:

```text
Q2042674
```

## `instance_of`

Direct Wikidata `P31` (`instance of`) QIDs.

Example:

```json
"instance_of": [
  "Q35666"
]
```

For a glacier, `Q35666` represents the glacier class.

Only direct `P31` values are currently collected. `P279` (`subclass of`) ancestry can be added later if 
broader class reasoning is required.

## `wikidata_fetched`

Marks that the Wikidata lookup has completed.

This is important even when the result contains no `P31` values, because it prevents repeated requests 
for items already checked.

---

# 7. Wikidata Collection Rules

Wikidata is queried only when the cached Wikipedia result is eligible.

The tool skips Wikidata when:

- the Wikipedia page is missing
- the Wikipedia result contains no usable article data
- the page is a disambiguation page
- the page has no Wikidata QID
- Wikidata has already been fetched for that cache entry

This prevents unnecessary API traffic.

---

# 8. Cache Rules

The JSON cache is keyed by Wikipedia language and lookup title:

```text
en:Owens Valley
en:Easton Glacier
en:Devils Hole
```

The cache contains only Wikipedia/Wikidata facts.

---

# 9. Dry Run

`--dry-run` is intended to answer:

> Are the API requests this tool is about to make reasonable?

A dry run:

- loads the input CSV
- loads lookup preprocessing
- builds augmented fallback names
- formats the exact Wikipedia requests
- prints the requests
- formats Wikidata requests for QIDs already present in the cache
- prints those requests
- performs no HTTP requests
- does not modify the cache
- does not write the output CSV

Example augmentation output:

```text
'Topaz' [hot_spring] -> 'Topaz hot spring'
'Octopus' [hot_spring] -> 'Octopus hot spring'
'Bunker' [sand] -> 'Bunker sand'
'Fir Draw' [valley] -> 'Fir Draw valley'
```

If a dry run starts from an empty cache, Wikidata QIDs cannot yet be known because they would normally be discovered from the Wikipedia response. In that case the tool reports that Wikidata requests cannot yet be constructed.

---

# 10. Running `wikipedia_props.py`

Example production run:

```bash
python wikipedia_props.py \
  --input build/uswest/interim/uswest_geology_base_features.csv \
  --output data/external/uswest_geology_wikipedia_enrich.csv \
  --cache-file data/external/uswest_wikipedia_cache.json \
  --project importance_ranking \
  --email user@example.com
```

Example dry run:

```bash
python wikipedia_props.py \
  --dry-run \
  --input build/uswest/interim/uswest_geology_base_features.csv \
  --output data/external/uswest_geology_wikipedia_enrich.csv \
  --cache-file data/external/uswest_wikipedia_cache.json \
  --project importance_ranking \
  --email user@example.com
```

Optional lookup preprocessing override:

```text
--preprocessing-config config/wiki_lookup_preprocessing.yml
```

Other useful options include:

```text
--lang
--id
--name-col
--sub-category-col
```

---

# 11. Enhancement CSV

The collection stage writes a compact enhancement CSV containing:

```text
osm_id
article_length
link_count
summary
```

This file contains raw collected Wikipedia evidence.

It should not be assumed that the Wikipedia article is the correct feature for a key. That decision 
done the validation stage.

---

# 12. `wikipedia_lookup_validator.py`

## Purpose

The validator compares:

```text
expected type:
    sub_category

actual type:
    cached Wikidata P31 values
```

It performs no network access and does not modify the Wikipedia cache.

Because validation is standalone, its rules can be tuned and rerun rapidly during development without repeating 
the expensive Wikipedia/Wikidata collection stage.

---

# 13. Validation States

The validator produces three states:

```text
ACCEPT
REJECT
UNCERTAIN
```

## ACCEPT

At least one cached P31 value matches an accepted class for the feature's `sub_category`.

Example:

```text
sub_category: glacier
Wikidata P31:     Q35666
```

Result:

```text
ACCEPT
```

## REJECT

A lookup is rejected when:

- the Wikipedia page is missing
- the page is a disambiguation page
- a P31 value explicitly matches a reject rule

## UNCERTAIN

Used when the available evidence does not prove either compatibility or incompatibility.

Examples:

- no P31 values
- no rule exists for the sub-category
- P31 exists but is not yet recognized by the validation config
- lookup title is absent from the cache

`UNCERTAIN` is intentional. Unknown classes should not be guessed.

For production ranking, uncertain Wikipedia evidence should normally be treated conservatively rather than assumed correct.

---

# 14. Validation Configuration

Default file:

```text
config/wiki_lookup_validation.yml
```

Example:

```yaml
p31:

  global_reject:
    - Q5
    - Q4830453
    - Q11424
    - Q223557

  global_accept: []

  sub_categories:

    glacier:
      accept:
        - Q35666

    valley:
      accept:
        - Q39816

    hot_spring:
      accept:
        - Q177380
        - Q124714

    cave_entrance:
      accept:
        - Q35509
```

## `global_reject`

P31 values incompatible with feature lookup regardless of  category.

Typical examples include humans, businesses, films, or generic unrelated objects.

A global reject match wins over an accept match.

## `global_accept`

Classes considered acceptable for every category.

This should generally remain small or empty. Broad concepts such as "geographic location" are usually too weak to 
establish that the article represents the expected physical feature.

## `sub_categories`

Rules specific to a `sub_category`.

Example:

```yaml
glacier:
  accept:
    - Q35666
```

means:

```text
sub_category == glacier
AND
P31 contains Q35666
    -> ACCEPT
```

Sub-category-specific reject lists can also be added when useful.

---

# 15. Validation Precedence

Direct P31 validation currently follows this order:

```text
1. Wikipedia page missing
      -> REJECT

2. Wikipedia disambiguation page
      -> REJECT

3. matching reject P31
      -> REJECT

4. matching accept P31
      -> ACCEPT

5. no recognized decision
      -> UNCERTAIN
```

Reject rules take precedence over accept rules.

---

# 16. Running the Validator

Example:

```bash
python wikipedia_lookup_validator.py \
  --input build/uswest/interim/uswest_geology_base_features.csv \
  --cache-file data/external/uswest_wikipedia_cache.json \
  --output build/uswest/interim/uswest_geology_wikipedia_validation.csv \
  --validation-config config/wiki_lookup_validation.yml \
  --preprocessing-config config/wiki_lookup_preprocessing.yml
```

The preprocessing config must match the configuration used during Wikipedia collection so the validator reconstructs 
the same lookup titles.

Optional arguments include:

```text
--lang
--id
--name-col
--sub-category-col
--wiki-col
```

---

# 17. Validation Output

The validator writes a diagnostic CSV containing fields such as:

```text
osm_id
item_name
sub_category
lookup_title
cache_key
wikidata_id
p31
validation_state
validation_reason
accept_matches
reject_matches
disambiguation
missing
```

This file is intended both for development tuning and for deterministic production processing.

---

# 18. Development and Tuning Workflow

During development, validation rules should be tuned using real collected data.

A typical workflow is:

```text
1. Run wikipedia_props.py once
2. Inspect the JSON cache and validation output
3. Add or adjust P31 rules
4. Rerun wikipedia_lookup_validator.py
5. Review ACCEPT / REJECT / UNCERTAIN
6. Repeat until classification is reliable
```

The important design feature is that steps 3-6 require no API requests.

The validator should be tuned for high precision. A false Wikipedia match can add a large article-length/link-count 
signal and push an irrelevant feature into a high map rank.

A missed valid article is usually less damaging: the feature merely loses the Wikipedia prominence contribution.

---

# 19. Production Behavior

The full ranking pipeline is intended to run unattended.

There is no routine manual review during a production run.

Validation therefore needs deterministic behavior:

```text
ACCEPT
    -> Wikipedia prominence data may be used

REJECT
    -> Wikipedia prominence contribution should be suppressed

UNCERTAIN
    -> normally suppress Wikipedia prominence contribution
```

A single missing, rejected, or uncertain article is normal data and should not abort the run.

Only systemic failures should stop the pipeline, such as:

- unreadable cache
- invalid validation configuration
- malformed input schema
- catastrophic drop in expected metadata coverage
- output/input row-count mismatch
- validator failure affecting an entire category or file

---

# 20. Why P31 Is Useful

Wikipedia title resolution alone does not prove identity.

A bare lookup such as:

```text
Cauliflower
Topaz
Octopus
Pine
Millers Point
```

can resolve to a perfectly valid Wikipedia page that represents the wrong entity.

Wikidata P31 provides structured type evidence.

Examples seen in collected data include:

```text
Easton Glacier
    P31 = Q35666
    -> glacier

Little Si
    P31 = Q8502
    -> mountain

Owens Valley
    P31 = Q39816
    -> valley

Ojo Caliente Spring
    P31 = Q177380
    -> hot spring
```

This lets the validator compare the expected sub category against the structured Wikipedia/Wikidata entity type instead of relying only on title similarity.

---

# 21. Current Limitations

## Direct P31 only

The current collection stores direct P31 values.

Some Wikidata entities use a specialized class whose relationship to a broader known class is expressed through 
`P279` (`subclass of`).

Future validation can add cached P279 ancestry traversal:

```text
specific P31
    ↓
P279 parent
    ↓
P279 parent
    ↓
known accepted class
```

This will reduce the number of `UNCERTAIN` results without requiring large lists of every specialized Wikidata class.

## Wikipedia summaries are collected but not yet scored

Summaries are intentionally cached for future validation experiments.

They can later provide secondary evidence for cases where P31 is missing or inconclusive.

The current P31 validator does not perform summary keyword scoring.

## Lookup augmentation is heuristic

Short-name augmentation is designed to reduce obvious title ambiguity, not guarantee correct entity resolution.

The validator remains necessary even after augmentation.

---

# 22. Design Principles

The Wikipedia/Wikidata subsystem follows several important rules:

- **Collect raw evidence once.**
- **Cache external metadata aggressively.**
- **Keep the cache source-neutral.**
- **Do not trust OSM Wikipedia tags merely because they are present.**
- **Improve ambiguous lookup titles before querying.**
- **Validate separately from collection.**
- **Prefer structured Wikidata type evidence over prose inference.**
- **Treat unknown evidence as uncertain, not automatically correct.**
- **Favor precision over recall for ranking enrichment.**
- **Production runs must be deterministic and unattended.**
