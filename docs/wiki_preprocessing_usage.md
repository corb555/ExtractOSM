# Wikipedia lookup preprocessing

Looking up wikipedia articles with short names can be ambiguous. A feature named “Eagle” could refer to a bird, a town, or 
a viewpoint. Adding the feature’s category—such as “Eagle viewpoint”—makes 
the  lookup more specific and can reduce unrelated matches and also find potentially missed articles.

The rules avoid adding redundant context when the name already contains it, such as “Blue Cave.”
`wiki_lookup_preprocessing.yml` supplies rules to `wikipedia_fetch.py` for adding category context to short feature names 
used as fallback Wikipedia lookup titles. It changes the lookup title only; it does not edit the input CSV, rename features, or change their classification.

## When rules apply

If a row has a nonblank `wikipedia` value, the script parses that explicit reference and bypasses these rules. Otherwise, it uses `item_name` as a fallback:

1. Replace `#` with spaces, collapse whitespace, and trim the name.
2. Find a rule matching the row’s `sub_category`. Category matching is case-sensitive; surrounding whitespace in the CSV value is stripped.
3. Apply the rule only if the normalized name’s character count is **less than or equal to** `max_length`. Spaces and punctuation count.
4. Leave the name unchanged if it already contains a word from `append` or `synonyms`.
5. Otherwise, append one space and the configured text.

Missing or unmatched categories leave the normalized name unchanged. A missing name with no explicit Wikipedia reference produces no Wikipedia title from that row. Having a Wikidata ID does not disable name preprocessing; Wikidata-derived article titles are handled separately.

## Rule format

```yaml
augment_context:
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

| Field | Meaning |
|---|---|
| `augment_context` | Mapping of exact sub-category values to rules. |
| `max_length` | Required positive integer: maximum name length eligible for augmentation. It does not truncate names. |
| `append` | Required nonblank string added to eligible names. |
| `synonyms` | Optional list of words indicating sufficient context is already present. These suppress augmentation; they do not generate alternative searches or replace words. |

Matching ignores case and strips common punctuation from the ends of whitespace-separated words. It checks simple singular/plural forms by adding or removing a trailing `s`; this is not linguistic stemming. Hyphenated compounds are not split.

For multiword phrases, **any individual word** can suppress augmentation. For example, either `hot` or `spring` prevents appending `hot spring`.

## Current rules

| Sub-category | Maximum characters | Appended text | Additional suppression terms |
|---|---:|---|---|
| `viewpoint` | 10 | `viewpoint` | None |
| `sand` | 30 | `sand` | gulch, desert, bar, hills, wash, dunes, flat, valley, lake, mesa, arroyo, basin |
| `cave_entrance` | 10 | `cave` | None |
| `hot_spring` | 10 | `hot spring` | None |
| `valley` | 8 | `valley` | basin, canyon, gorge, flat, gulch |

Examples below assume the `wikipedia` field is blank:

| Name | Sub-category | Lookup title | Reason |
|---|---|---|---|
| Eagle | viewpoint | Eagle viewpoint | Short name without context. |
| Blue | cave_entrance | Blue cave | Short name without context. |
| Blue Cave | cave_entrance | Blue Cave | Already contains `cave`. |
| Big Basin | valley | Big Basin | Exceeds eight characters. |
| Basin | valley | Basin | Contains a suppression term. |
| Red Dunes | sand | Red Dunes | Contains a suppression term. |
| Warm | hot_spring | Warm hot spring | Short name without context. |
| Hot Creek | hot_spring | Hot Creek | `hot` alone suppresses the append phrase. |
| Eagle | peak | Eagle | No configured rule for `peak`. |

## Run and preview changes

The default configuration path is `config/wiki_lookup_preprocessing.yml`, resolved from the process’s working directory. To use a file named `wiki_preprocessing.yml`, pass that path explicitly.

```bash
python wikipedia_fetch.py \
  --input features.csv \
  --project LandWeaver \
  --email your-email@example.com \
  --preprocessing-config config/wiki_lookup_preprocessing.yml \
  --dry-run
```

Replace the contact email with your actual address. Dry-run prints changed fallback names and planned API activity without HTTP requests or file writes. It reads the input and existing cache; cached entries can affect which requests are planned. Remove `--dry-run` to fetch metadata and update the shared JSON cache.

The CSV must contain `item_name`, `sub_category`, `wikipedia`, and `wikidata`, although individual cells can be blank. Override the column names with `--name-col`, `--sub-category-col`, `--wiki-col`, and `--wikidata-col`.

To add a rule, add an entry beneath `augment_context` using an exact sub-category value from the CSV. To disable all augmentation, use:

```yaml
augment_context: {}
```

The configuration file must still exist. Invalid rule structure causes loading to fail. Fallback titles are currently tagged as English, so `--lang` values other than `en` filter them out. These rules prepare candidate titles; they do not establish that an article is the correct geographic match.
