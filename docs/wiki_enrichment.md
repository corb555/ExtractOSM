# Wikipedia/Wikidata Enrichment

## Purpose

The Wikipedia/Wikidata enrichment system adds useful public knowledge to an existing dataset while protecting downstream applications from ambiguous or incorrect name matches.

A name alone is not a reliable identifier for Wikipedia. The same name can refer to a place, person, organization, product, work, event, or many other unrelated entities. Even when Wikipedia returns a page with the expected name, that page may still represent the wrong real-world entity.

The system therefore separates enrichment into two distinct operations:

- **`wikipedia_cache`** acquires and caches raw Wikipedia/Wikidata facts.
- **`wikipedia_match`** decides whether the cached entity is an acceptable match for a source record.

This separation is intentional. Wikipedia/Wikidata access is comparatively expensive and network-dependent, while match policy changes frequently as categories, distance thresholds, and classification rules are refined. The cache allows acquisition to happen once and matching to be rerun locally as often as needed.

The result is a reusable enrichment layer with three important properties:

- **Fast repeated processing** — previously acquired Wikipedia/Wikidata data is reused without another network request.
- **Deterministic matching** — matching runs against stable local data instead of changing remote API results during every run.
- **Safer enrichment** — downstream applications receive only records that pass explicit identity, classification, and geographic checks.

> A Wikipedia title match is evidence, not proof, that two records represent the same entity.

### Why use a cache?

A typical dataset may contain tens or hundreds of thousands of names, and many names may recur across files or processing runs. Directly querying Wikipedia and Wikidata every time would be slow, wasteful, and unnecessarily dependent on external services.

`wikipedia_cache` keeps a shared local cache of the raw facts already acquired. A lookup first checks that hot cache. Only cache misses are sent to Wikimedia. Successful lookups, negative lookups, redirects, disambiguation information, and relevant Wikidata facts can therefore be reused by later records and later jobs.

The cache also allows acquisition to be optimized independently of matching. The current implementation streams logical lookup keys through an iterator, batches exact-title requests internally, keeps completed results in memory, and writes the cache only at checkpoints or job boundaries. Callers do not need to know Wikimedia API batch sizes or request mechanics.

### Why use a separate matching utility?

Caching answers **"What does Wikipedia/Wikidata say about this name?"**

Matching answers **"Does that cached entity actually represent the source record we intended?"**

Those are different questions. Keeping them separate means:

- raw acquisition remains generic and reusable;
- category-specific policy does not contaminate the cache;
- matching rules can evolve without repeating network acquisition;
- rejected and uncertain candidates remain available for diagnostics;
- the same cached entity can be evaluated differently for different datasets or categories.

For example, a source name such as `El Capitan` may refer to a geographic feature, an operating-system release, a computer system, or another entity. The cache records the available Wiki* facts; the matcher uses classification and location evidence to determine whether a particular cached entity is appropriate for the source record.

---

## System Overview

```text
Source CSV
   │
   ▼
wikipedia_cache
   │
   ├── Wikipedia metadata
   ├── Wikidata metadata
   ├── redirects / resolved titles
   ├── disambiguation candidates
   └── shared local cache
   │
   ▼
wikipedia_match
   │
   ├── identity checks
   ├── P31/P279 classification checks
   ├── geographic checks
   └── diagnostics
   │
   ├── accepted enrichment CSV
   └── diagnostic CSV
```

---

## `wikipedia_cache` — Acquire and Cache Wiki* Metadata

`wikipedia_cache` reads the source CSV, derives Wikipedia lookup identities, checks the shared cache, and acquires only information that is not already cached.

It performs **acquisition only**. It does not decide whether a result is the correct entity for a source row and does not produce the final enrichment output.

### Cache identity

Wikipedia cache identity is the exact lookup key supplied to the cache layer:

```text
lang:Requested Title
```

For example:

```text
en:Washington
```

The supplied key remains the cache identity even when Wikipedia normalizes or redirects the title. A resolved title is stored as data; it does not replace the original cache key.

This distinction is important because preprocessing determines lookup identity before acquisition. Matching must reconstruct the same lookup key so it can retrieve the same cached record.

### Cached Wikipedia fields

Where available, Wikipedia entries contain data such as:

- requested/resolved title
- article length
- incoming link count
- article summary
- coordinates
- Wikidata QID
- missing state
- disambiguation state
- disambiguation candidate titles
- optional fallback-search candidate titles

### Cached Wikidata fields

Where available, Wikidata entries contain data such as:

- QID
- P31 `instance of` values
- Wikidata sitelink count
- English Wikipedia title
- coordinates

The shared JSON cache contains both Wikipedia and Wikidata sections. Applications should treat it as internal acquisition state rather than as a final enrichment interface.

---

## Acquisition Flow

### 1. Read and preprocess source identities

The input CSV supplies names and may also contain explicit Wikipedia or Wikidata identities.

Explicit Wikipedia identities are used directly. When a row has no explicit Wikipedia reference, configured preprocessing can derive the lookup title from the source name. Preprocessing can normalize phrases or add category context to otherwise ambiguous short names.

The same preprocessing configuration must later be used by `wikipedia_match` so both utilities derive identical cache keys.

### 2. Acquire explicit source Wikidata identities

If source rows contain Wikidata QIDs, `wikipedia_cache` checks the Wikidata cache first and fetches only missing QIDs.

An authoritative `enwiki` sitelink returned by Wikidata can supply an additional Wikipedia title to acquire.

This allows a source record with a known Wikidata identity to contribute the corresponding Wikipedia article even when its display name differs from the Wikipedia title.

### 3. Acquire source Wikipedia metadata

The initial Wikipedia work set contains:

- titles derived from the source data; and
- authoritative Wikipedia titles discovered from source Wikidata QIDs.

The current implementation uses a streaming bulk lookup interface. Logical lookup keys are supplied as an iterable. The cache layer:

1. checks each exact key against the in-memory cache;
2. immediately reuses cache hits;
3. accumulates uncached exact-title lookups internally;
4. groups compatible requests into Wikimedia API batches;
5. stores each result under its original lookup key;
6. yields completed results back to the caller.

The caller does not know or control the physical API batch size.

### 4. Expand disambiguation pages

If a source lookup resolves to a Wikipedia disambiguation page, its linked article titles become candidate identities.

Those candidate titles are deduplicated and sent through the same cached, batched exact-title acquisition path. If a candidate was already acquired earlier in the current run or a previous run, its cache entry is reused rather than requested again.

Disambiguation expansion is useful because ambiguous names often cannot be resolved from the title alone. The candidate articles provide the metadata needed by `wikipedia_match` to select or reject possible entities using classification and location evidence.

### 5. Optional fallback title search

Exact-title fallback search is **disabled by default**.

It can be enabled with:

```text
--fallback-search
```

When enabled, a missing exact Wikipedia title may trigger a title-restricted Wikipedia search for plausible alternatives.

Fallback search is intentionally separate from the normal bulk protocol. It is performed sequentially rather than through the exact-title iterator and emits a warning because it can be substantially slower than batched acquisition.

The normal acquisition path should therefore be:

```text
exact cache lookup
    ↓
exact batched Wikipedia lookup on miss
    ↓
disambiguation expansion when applicable
```

with fallback search enabled only when its additional discovery value justifies the cost.

### 6. Acquire Wikidata for resolved Wikipedia articles

After Wikipedia source and candidate metadata have been collected, the utility gathers every referenced Wikidata QID and acquires any missing Wikidata metadata.

This gives the matching stage semantic and geographic evidence such as P31 classifications, coordinates, and sitelink counts.

---

## Cache Performance and Persistence

The cache is designed to support large workloads without turning every logical lookup into an HTTP request or disk write.

### Hot-cache behavior

Within a running job, newly acquired entries are immediately available from the in-memory cache.

If the same exact key appears again later in the run:

```text
first lookup
    cache miss
    → remote acquisition
    → store in memory

later identical lookup
    cache hit
    → no remote request
```

The same applies across completed runs once the cache has been persisted.

### Internal batching

Exact Wikipedia title requests are batched internally. The caller supplies an iterable of logical lookup keys and receives completed results without needing to know Wikimedia request limits.

Wikidata entity requests are also batched.

### Checkpointed persistence

Cache mutations remain in memory during normal processing. 

Disk writes occur at configured checkpoints and explicit phase/job boundaries. This provides restart protection without repeatedly serializing the entire cache for every lookup.

### Retry behavior

Transient Wikimedia failures such as HTTP `429` and `503` are retried with backoff. A `Retry-After` response header is honored when present.

An occasional `429` therefore does not invalidate the job; the request is delayed and retried. Persistent failures still terminate rather than silently fabricating data.

---

## `wikipedia_match` — Resolve and Validate Cached Entities

`wikipedia_match` reads the source CSV and the local Wiki* cache and determines which cached entity, if any, is a likely match for each source record.

It performs **no Wikipedia or Wikidata network acquisition**. That is a core design boundary: matching is deterministic and can be rerun repeatedly without touching Wikimedia.

A name match by itself is not sufficient. The matcher can use several independent signals:

- exact source Wikidata identity when available;
- resolved Wikipedia identity;
- Wikidata P31 `instance of` classifications;
- cached P279 `subclass of` relationships;
- source and Wiki* coordinates;
- category-specific distance thresholds;
- completeness of required ranking/enrichment metadata;
- disambiguation candidate evidence.

The purpose is not merely to find similarly named pages. It is to resolve the source record to an entity that satisfies the configured semantic and geographic contract.

### Classification matching

Wikidata P31 values identify what an entity is. Validation rules operate on QIDs rather than human-readable labels.

Cached P279 relationships can allow a specific observed class to match a configured parent class. For example, a specific subtype can satisfy a rule accepting a broader parent classification.

Human-readable labels are diagnostic aids; QIDs remain the stable policy identifiers.

### Geographic matching

When coordinates are available, geographic distance provides an independent identity signal.

Distance thresholds are category-specific because different entity types have different spatial semantics. A point-like object may require a very tight match, while a broad administrative or regional entity may legitimately have a much looser representative coordinate.

### Exact source identities

An explicit source Wikidata QID provides stronger identity evidence than a name alone. The matcher can use that identity directly while still retaining category and location diagnostics for quality review.

---

## Matching Configuration

Validation policy can define global defaults and category-specific refinements.

A generic configuration shape is:

```yaml
validation:
  defaults:
    location:
      max_distance_m: 5000

    p31:
      accept:
        - Q123456
      reject:
        - Q654321

  categories:
    landmarks:
      location:
        max_distance_m: 2000
      p31:
        accept:
          - Q111111
        reject: []

      sub_categories:
        memorial:
          p31:
            accept:
              - Q222222
            reject: []

    places:
      location:
        max_distance_m: 15000
      p31:
        accept:
          - Q333333
        reject: []
```

The QIDs above are placeholders showing configuration structure only. Real rules should be developed from classifications observed in the data and from matching diagnostics.

---

## Match States

Matching distinguishes three broad outcomes:

```text
ACCEPT
REJECT
UNCERTAIN
```

Typical reasons include conditions such as:

```text
ACCEPT
ACCEPT_WIKIDATA_IDENTITY
CATEGORY_REJECT
CATEGORY_NOT_MATCHED
DISAMBIGUATION_UNRESOLVED
PAGE_MISSING
CACHE_MISSING
LOCATION_TOO_FAR
NO_SOURCE_COORDINATES
NO_REFERENCE_COORDINATES
INCOMPLETE_DATA
```

The exact diagnostic vocabulary may evolve, but the contract remains the same:

- **ACCEPT** — sufficient evidence exists to use the enrichment.
- **REJECT** — available evidence indicates the candidate is not acceptable.
- **UNCERTAIN** — the system does not have enough evidence to safely accept or definitively reject the candidate.

Rejection and uncertainty are normal entity-resolution outcomes, not acquisition failures.

---

## Diagnostics

Diagnostics are a first-class output of the matching system.

The clean enrichment file contains accepted matches only. A separate diagnostic file records the evidence and decision for every attempted match so validation policy can be inspected and refined.

Useful diagnostic fields may include:

```text
id
item_name
category
sub_category
lookup_title
resolved_title
wikidata_id
p31_ids
p31_labels
p31_parent_labels
validation_state
validation_reason
resolution_method
candidate_count
accept_matches
reject_matches
lat
lon
wikipedia_lat
wikipedia_lon
wikidata_lat
wikidata_lon
distance_m
location_state
location_reason
disambiguation
missing
article_length
link_count
wikidata_sitelink_count
```

Diagnostics should make it possible to answer questions such as:

- Why was this row accepted, rejected, or left uncertain?
- Which Wikipedia title was requested and which title resolved?
- Was the source identity explicit or inferred from a name?
- Was the source page a disambiguation page?
- Which candidate was selected?
- Which P31 classifications were observed?
- Which configured accept/reject rules matched?
- Which P279 ancestors affected classification matching?
- Were coordinates available from the source, Wikipedia, or Wikidata?
- How far apart were the compared entities?
- Which classifications occur frequently but are not yet covered by policy?
- Are rejection or uncertainty rates unexpectedly high?

The diagnostic output is therefore both a forensic artifact and a policy-development tool.

---

## Primary Output Contract

The matching stage produces two conceptually different outputs.

### Enrichment output

The enrichment CSV contains **accepted rows only**. It is intended to be safe for unconditional downstream application.

Useful provenance fields can include:

```text
id
item_name
resolved_title
wikidata_id
p31_labels
article_length
link_count
wikidata_sitelink_count
wikipedia_lat
wikipedia_lon
distance_m
```

### Diagnostic output

The diagnostic CSV contains **every attempted match** and enough supporting evidence to explain the result.

```text
enrichment.csv
    accepted rows only
    downstream-safe data
    compact provenance

diagnostics.csv
    every attempted row
    full resolution and validation context
    used for tuning, investigation, and QA
```

---

## Usage

The normal workflow is:

```text
run wikipedia_cache
        ↓
shared Wiki* cache updated incrementally
        ↓
run wikipedia_match
        ↓
accepted enrichment + diagnostics
        ↓
inspect diagnostics / refine policy as needed
        ↓
rerun wikipedia_match without repeating acquisition
```

### `wikipedia_cache.py`

```bash
python wikipedia_cache.py \
  --input data/items.csv \
  --cache-file data/cache/wikipedia_cache.json \
  --preprocessing-config config/wiki_lookup_preprocessing.yml \
  --project "MyProject" \
  --email "me@example.com"
```

Important options:

| Argument | Description |
|:---|:---|
| `--input` | Source CSV containing names and optional Wikipedia/Wikidata identities. |
| `--cache-file` | Shared Wikipedia/Wikidata JSON cache. Defaults to `wikipedia_cache.json`. |
| `--project` | Project/application name used in the Wikimedia User-Agent. |
| `--email` | Contact email used in the Wikimedia User-Agent. |
| `--lang` | Wikipedia language code. Defaults to `en`. |
| `--name-col` | Source name column. Defaults to `item_name`. |
| `--sub-category-col` | Optional sub-category column used by lookup preprocessing. Defaults to `sub_category`. |
| `--wiki-col` | Wikipedia identity column. Defaults to `wikipedia`. |
| `--wikidata-col` | Wikidata identity column. Defaults to `wikidata`. |
| `--preprocessing-config` | YAML controlling lookup preprocessing. |
| `--fallback-search` | Enable sequential fallback Wikipedia title search for exact-title misses. Disabled by default. |
| `--trace-name` | Trace lookup behavior for a selected source name; may be repeated. |
| `--dry-run` | Show preprocessing and planned requests without HTTP or file writes. |

The resulting cache is reusable across categories, files, and later pipeline runs.

### `wikipedia_match.py`

```bash
python wikipedia_match.py \
  --input data/items.csv \
  --cache-file data/cache/wikipedia_cache.json \
  --category-cache data/cache/wiki_categories.json \
  --category landmarks \
  --validation-config config/wiki_lookup_validation.yml \
  --preprocessing-config config/wiki_lookup_preprocessing.yml \
  --output data/items_wikipedia_enrich.csv \
  --diagnostics data/items_wikipedia_diagnostics.csv
```

Typical options include:

| Argument | Description |
|:---|:---|
| `--input` | Source CSV to match. |
| `--cache-file` | Wikipedia/Wikidata metadata cache produced by `wikipedia_cache`. |
| `--category-cache` | Cached P31 labels and P279 relationships used by classification matching. |
| `--category` | Validation category used to select matching policy. |
| `--validation-config` | Category-specific classification and distance rules. |
| `--preprocessing-config` | Same lookup preprocessing used by `wikipedia_cache`. |
| `--output` | Accepted-only enrichment CSV. |
| `--diagnostics` | Full diagnostic CSV. |
| `--lang` | Wikipedia language code. Defaults to `en`. |
| `--id` | Source identifier column. |
| `--name-col` | Source name column. Defaults to `item_name`. |
| `--sub-category-col` | Source sub-category column. Defaults to `sub_category`. |
| `--wiki-col` | Wikipedia identity column. Defaults to `wikipedia`. |
| `--lat-col` / `--lon-col` | Source coordinate columns. Defaults to `lat` / `lon`. |

`wikipedia_match` must derive lookup keys using the same preprocessing configuration as `wikipedia_cache`; otherwise the two stages may refer to different cache identities.

---

## Design Benefits

- **Generic acquisition** — the cache layer does not encode application-specific match policy.
- **Shared caching** — previously acquired Wiki* data is reused across datasets and runs.
- **High-throughput lookup** — exact Wikipedia requests use an iterator-based bulk interface and internal API batching.
- **Hot-cache reuse** — repeated identities in the same run do not require repeated network requests.
- **Checkpointed persistence** — large cache files are written periodically rather than after every lookup.
- **Optional expensive discovery** — fallback title search is disabled by default and clearly separated from normal batched acquisition.
- **Disambiguation support** — ambiguous Wikipedia titles can be expanded into candidate articles for later validation.
- **Semantic matching** — P31/P279 data helps determine whether a candidate is the correct kind of entity.
- **Geographic matching** — coordinate distance supplies independent identity evidence.
- **Deterministic revalidation** — matching can be rerun entirely from local cache data as rules change.
- **Inspectable decisions** — diagnostics preserve the evidence behind accepted, rejected, and uncertain matches.
- **Strong downstream contract** — the primary enrichment output contains accepted rows only.
