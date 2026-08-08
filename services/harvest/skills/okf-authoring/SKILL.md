---
name: okf-authoring
description: Author Open Knowledge Format (OKF) bundles — knowledge represented as a directory of markdown files with YAML frontmatter. Use when asked to create, write, generate, enrich, or validate an OKF bundle or OKF concept docs; to turn a data source (Glue database, API, database, catalog), documentation, or a research topic into a portable knowledge bundle; or when the user mentions "OKF", "Open Knowledge Format", "knowledge bundle", or "concept docs".
---

# Authoring Open Knowledge Format (OKF) bundles

## What OKF is

OKF is an open, vendor-neutral format for representing **knowledge** — the
metadata, context, and curated insight around data and systems. A bundle is
**a directory of UTF-8 markdown files with YAML frontmatter**. No schema
registry, no central authority, no required tooling: if you can `cat` a file
you can read it; if you can `git clone` a repo you can ship it.

- **Knowledge Bundle** — a self-contained directory tree of knowledge docs; the unit of distribution.
- **Concept** — one unit of knowledge = one markdown file. May describe a tangible asset (a table, an API) or an abstract idea (a metric, a playbook).
- **Concept ID** — the file's path within the bundle minus `.md`. `tables/users.md` → `tables/users`.
- **Frontmatter** — the YAML block at the top, delimited by `---`. **Body** — everything after it.

The full normative spec is in `references/spec-condensed.md`. Read it when you
need exact rules; the workflow below is what to do.

## The one rule that matters most

A document is **conformant** only if its frontmatter is parseable YAML with a
non-empty `type` field. Everything else (titles, descriptions, links, indexes)
is soft guidance — consumers must tolerate its absence. So: **never emit a
concept doc without valid frontmatter and a `type`.** Get that right and the
bundle is valid; the rest is quality.

## Consumers read only the bundle

The reader of a bundle sees **only the concept docs** — not the source you
authored from. Uploaded context (a `.context/` directory), any read-only source
snapshot (e.g. a `.metadata/` catalog dump), and the live source system itself
are **authoring-time inputs, invisible at read time**. A fact that lives only in
one of those is, to the reader, missing.

So: **self-contain every fact the reader needs.** Never write body text that
sends the reader to the source to complete an answer — "for the full list see
the data dictionary", "consult the uploaded spec for the other codes",
"see `.context/…`". If you have the values, transcribe the values into the doc
(a long legend gets its own `references/enums/<column>.md` — that is a real
concept the reader CAN open, unlike the source file). Point at the source **only**
under `# Citations`, as provenance for where you copied a fact FROM — never in
the body as a place for the reader to go.

## Source and context must converge — verify, don't defer

A bundle has two authoring inputs: the **primary source** (what the data
structurally *is* — schema, real values, measured grain) and any **uploaded
context** (what humans *say* the data means — dictionaries, join docs, metric
definitions). Neither is authoritative alone. The final bundle is their
**reconciliation**: every load-bearing fact should hold in both, and where they
disagree, that disagreement is itself knowledge worth capturing.

- **Context is a lead, not gospel.** A join, grain, enum, or metric asserted in a
  context doc is a hypothesis to CONFIRM against the live source — not a fact to
  transcribe on faith. Run the query. If the data contradicts the doc, the data
  wins, and the discrepancy earns a `# Gotchas` note ("the dictionary lists
  status `9` but no row uses it"; "the ERD claims a 1:1 join, but N ids fan out").
- **Never let context make you lazy.** Being handed one join does not license
  skipping the rest. Still `grep .metadata/columns.tsv` for every shared key,
  still probe the plausible relationships the docs never mentioned, still measure
  the grain yourself. Context should *widen* your investigation, never cap it —
  the join a human wrote down is often not the only (or the best) one.
- **Embedded documentation is context too.** Column comments, table
  descriptions, and catalog properties — including anything an upstream tool
  (dbt, DataZone, a modeling tool) synced into them — are human claims riding
  along *inside* the primary source. Treat them exactly like an uploaded doc:
  mine them through the fact-type lens, verify them against the data before
  transcribing, and cite the source. A comment the data contradicts is a
  `# Gotchas` finding, not a description to copy.
- **Fuse, don't staple.** The essence of the data emerges from putting source and
  context in dialogue, not from concatenating them. A fact only the source
  reveals and a fact only the context explains both belong; a fact they
  contradict is the single most valuable thing you can surface for a consumer.

## Capture the essence, not the volatile numbers

Favor facts that describe what the data *is* over statistics that merely describe
its current *size*. Row counts, byte sizes, distinct-value tallies, and "latest
date" values drift with every load — bake one into the prose and the bundle is
stale by the next refresh, and the number taught the reader little about meaning
anyway.

- **Measure volatile stats to VERIFY, then leave them out.** Counting rows to
  confirm a grain, or scanning distinct values to decode an enum, is exactly
  right — that is authoring-time verification. But the *output* is the verified
  grain ("one row per race") or the decoded legend, NOT the `COUNT(*)` you ran to
  get there.
- **Omit row counts, table sizes, and freshness timestamps by default.** Include
  a magnitude only when it is genuinely load-bearing *and* reasonably stable —
  a fixed enum cardinality ("530 occupation codes"), or an order-of-magnitude that
  changes how one must query ("billions of rows — always filter the partition
  key"). A precise, decaying count is noise; a stable, decision-shaping magnitude
  is signal. When in doubt, leave the number out and state the structure instead.
- **Verification evidence follows the same rule.** When a doc must carry what
  you measured (a join's match rate, an orphan share, a key-format anomaly),
  record the **proportion and the mechanism**, never the raw tallies: "~9% of
  keys are 13 characters (leading zero absent); zero-padding to 14 resolves
  100%" survives a reload — "211 of 2,269 rows are 13 characters; 2,058 match
  raw" is a current-load snapshot, false after the next refresh. If an absolute
  figure is genuinely load-bearing, round it and stamp it indicative ("~2.3k
  rows as of this harvest").

## Workflow

Authoring a bundle runs through the passes below. Run pass 1 always; run pass 2 to
cross-link; run pass 3 only when the caller provided uploaded context documents
worth folding in; pass 4 (indexes + conformance) is the definition of done — but
most runtimes run it FOR you after authoring, so check the pass before running
anything yourself. When your
runtime supports independent verification (e.g. reviewer sub-agents), run pass 5 —
an adversarial review — over the finished bundle.

### Pass 0 — Plan the bundle (before writing anything)

1. **Identify the source of truth.** A Glue database queried via Athena? A
   Redshift dataset? An OpenAPI/Avro/Protobuf schema? A database? A docs site? A
   research topic? The source determines the concept types and the directory
   layout.
2. **Decide the concept inventory and directory layout.** The layout is
   *independent of the domain* — organize however the knowledge wants to be
   organized. Common conventions, all optional:
   - `datasets/`, `tables/` — for data catalogs.
   - `references/<type>/<slug>.md` — for standalone reference docs (reusable
     definitions). Every extracted fact that becomes its own doc lives under a
     **fact-typed parent folder** (`references/metrics/`, `references/joins/`,
     `references/enums/`, `references/named_sets/`, `references/glossary/`,
     `references/known_issues/`), one doc per item. This folder scheme is
     **canonical** — it is what keeps bundles uniform across every harvest — so
     don't file a reference doc directly under `references/` or invent another
     folder. See `references/fact-types.md` (Routing summary) for the full table.
   - Flat at root — fine for small bundles of non-reference concepts.
3. **Pick `type` values.** Short, descriptive, self-explanatory strings:
   `Glue Table`, `Glue Database`, `API Endpoint`, `Metric`, `Playbook`,
   `Reference`. Types are **not** registered anywhere; pick consistent values and
   reuse them across the bundle so consumers can route/filter on them.
4. **Pin the query dialect** (for any source you'll write SQL against). Identify
   the exact engine — Athena/Trino, Amazon Redshift SQL, PostgreSQL, SQLite, … —
   and record it once on the bundle-root doc's frontmatter (e.g.
   `dialect: athena-sql-trino`). Write **every** SQL snippet (`# Common query
   patterns`, metric expressions, join `ON` clauses) in that dialect — its
   identifier quoting, type names, and functions. SQL written in the wrong
   dialect for its source is worse than no SQL: it actively misleads a consuming
   agent. The templates default to Athena/Trino syntax; do not copy it onto a
   non-Athena source unchanged.
5. **Load the source adapter.** The guidance in this skill is *source-generic*;
   each backend has its own schema-extraction commands, `type`/`resource`/dialect
   conventions, type vocabulary, identifier quoting, and gotchas. Before
   authoring, read the matching adapter in `references/sources/` (see its
   `index.md` to pick one) and follow it for every source-specific decision. If
   no adapter exists for your source, fall back to the generic guidance here —
   and consider adding an adapter (the `index.md` says how). Do **not** mix one
   backend's idioms into another's bundle.

### Pass 1 — Author concept docs from the primary source

For **each** concept, write exactly one markdown file. One concept = one file =
one `write` action. Steps per concept:

1. **Check for an existing doc** at the target path. If one exists, **refine it,
   don't rewrite it** — preserve its structure and extend.
2. **Gather structured metadata** from the source (schema, columns, partitioning,
   types). If metadata is sparse, sample a few rows to ground the description.
   **Never invent fields, partitions, shard counts, or values not in the source.**
   - **Verify the grain before you state it.** "One row per X" is the most
     load-bearing claim in an asset doc, so measure it — don't infer it from
     column names. If X is a declared primary key you may trust it; if X is an
     *assumed* composite key, test it with the `check_grain` tool (one call:
     unique-or-not, duplicate-group count, worst fan-out, sample duplicates) —
     or, where that tool is unavailable, the equivalent probe:
     `SELECT COUNT(*) FROM (SELECT <key cols>, COUNT(*) c FROM <t> GROUP BY <key cols> HAVING c > 1)`.
     A non-unique result means the grain is coarser than X — state the true grain
     (or weaken it: "approximately one row per X; N duplicate keys exist because …")
     and note what the duplicates represent.
   - **Disambiguate near-synonyms.** Before writing column descriptions, list
     columns whose name or meaning overlaps another column (same name across
     tables; a per-row count vs a same-named detail table; per-period vs
     cumulative values). For each confusable pair, sample real values from both
     and write an explicit contrast — see the `# Gotchas` convention under Body.
   - **Profile for meaning, not just structure.** When catalog comments and
     uploaded context are thin, the semantics are still discoverable — from the
     data. START with the precomputed column profile
     (`.metadata/profile/<table>.md`, when present): it already carries the
     null share, approximate distinct count, min/max, and top values per
     column — most of the probes below answered without a single query. Honor
     its markers: a sheet stamped INDICATIVE was computed from a sample, so
     its value lists are leads (verify with `run_sql` before transcribing a
     legend), and a value list at the top-K cap is NOT a closed enum. Only
     then run the remaining probes from "No docs? Prospect in the data"
     (`references/fact-types.md`): name morphology (`_cd`/`_flg`/`is_` →
     candidate enum or flag), value ranges (implied units; out-of-band
     sentinels), and paired-null/ordering probes (exactly-one-of rules,
     `end >= start` invariants). Every probe result is a hypothesis to verify,
     never a fact to assert — and never full-scan a billed source to profile it.
   - **Discover joins yourself — don't wait to be told.** Find candidate
     relationships by grepping the cross-table column index for every shared key
     (`grep <name> .metadata/columns.tsv`), not just the joins a context doc
     happens to mention. For each candidate, VERIFY it with the
     `validate_join` tool — one call returns the key match rate in BOTH
     directions, the null-key share, and the cardinality class (1:1 / 1:N /
     N:1 / M:N); record that evidence in the join doc as proportions plus the
     mechanism, never the probe's absolute row tallies (see "Capture the
     essence, not the volatile numbers"). Where the
     tool is unavailable, establish the same facts with real queries, e.g.
     `SELECT COUNT(*) FROM a JOIN b ON a.k = b.k` vs the row counts, or a
     duplicate-key probe on the presumed FK. A sub-100% match rate IS the
     orphan analysis — dig into which side has keys the other lacks and what
     those rows mean, so the join doc can say whether to inner- or left-join
     — and check the **value format** (casing, zero-padding, types): a join
     that only works through a cast or `TRIM`/`UPPER` must carry that
     normalization in its documented `ON` clause.
     Record what you measured (cardinality class, match-rate proportions,
     orphan behavior, normalization) in the join doc — see the join template.
     Document only joins that hold; if a
     context doc's asserted join fails or has surprising cardinality, that is a
     `# Gotchas`-worthy finding.
   - **Detect column families in wide tables.** If a table has many columns
     (rule of thumb: >~30), don't reflexively enumerate them one per row. Cluster
     the column names by their shared pattern — a common prefix/suffix, a numeric
     index, or a regex — and decide which columns are *individually meaningful*
     (keys, measures, status) versus members of a *repeating family* where the
     pattern is the meaning (`home_player_1..11`, `sensor_0001..2048`,
     `<bookmaker>{H,D,A}` odds triples). Document families as a group, not row by
     row — see the `# Schema` convention under Body.
3. **Compose the frontmatter** (see Frontmatter below).
4. **Compose the body** (see Body below) and write the file.

### Pass 2 — Cross-link concepts

Weave links between concepts wherever the prose naturally references another
concept (a sibling table, the parent dataset, a reference doc). See
**Cross-linking** below. A bundle is graph-shaped, not just tree-shaped — links
are what make it more than a pile of files.

### Pass 3 — Fold in uploaded context documents (only if provided)

You have **no web access** and must **never invent external facts, sources, or
provenance**. The only additional context beyond the primary source is what the
caller explicitly uploaded — documents made available to you for this bundle
(e.g. under a `.context/` directory or otherwise handed to you). If, and only if,
such documents are present:

- **Read them through the fact-type lens.** `references/fact-types.md` is the
  extraction checklist: it names the ~25 fact types worth capturing (business
  terms, metric definitions, join conditions, **code/enum legends**, filter
  rules, grain, caveats, units, lineage, named sets, conditional population,
  hierarchies, deprecations, …), tells you the **cue phrases to look for** in
  docs to find each, and — critically — **where each fact lands in the bundle**
  (a `# Schema` description, a `# Gotchas` note, a `references/` doc). Read it
  before authoring from an uploaded doc.
- **Read them, then augment** the relevant concept docs with what they actually
  state — following the augmentation rules (preserve the existing doc; add, don't
  shrink). One uploaded doc may inform multiple concepts.
- **Verify every context claim against the live source; don't just transcribe it.**
  A join condition, grain statement, metric formula, or enum value from a context
  doc is a hypothesis — confirm it with `run_sql`/`sample_rows` before it enters
  the bundle (see "Source and context must converge"). Where the data contradicts
  the doc, the data wins and the discrepancy becomes a `# Gotchas` note. And a
  context doc that documents *one* join/metric does not excuse you from probing
  the relationships and columns it left out — context widens the investigation,
  it never caps it.
- **Decode coded columns.** A data dictionary or code list you were given is the
  legend for opaque coded columns (`status`, `region_code`, education/occupation
  codes). Transcribe the code→meaning mapping — small sets inline in `# Schema`,
  large sets in a `references/enums/<column>.md` doc — per the CODE_ENUM section
  of `references/fact-types.md`. This is the highest-leverage use of an uploaded
  doc and the most common thing to miss. Transcribe the WHOLE legend into the
  doc; do not summarize its structure and point the reader back at the uploaded
  file — the reader cannot see uploaded files (see "Consumers read only the
  bundle" below).
- **Mint a `references/<type>/<slug>.md` doc** only for a reusable definition
  (entity, metric, enum, join path, named set) that a provided document genuinely
  supports — always under its fact-typed folder (see `references/fact-types.md`).
- **Cite the uploaded document**, not a guessed public origin (see Citations).

**Large `.context/` folders — extract facts once, up front, don't re-read per
concept.** When the uploaded context is sizable (many docs, a multi-sheet data
dictionary, a long PDF spec), reading the whole folder afresh while authoring
*each* concept is wasteful and lossy — the tenth table pays the reading cost again
and still misses the enum that lived on page 40. Instead do a **single up-front
extraction pass**: read every context doc through the `references/fact-types.md`
lens and produce a compact, **routed fact digest** — one entry per fact tagged
with (fact type, the exact claim, the target **concept id + section** it lands in,
its verification status, and the source `.context/<file>`), with full enum legends
transcribed verbatim under their target `references/enums/<col>`. Author each
concept from that digest, not from a fresh re-read. If your runtime lets you
dispatch sub-agents (a fact-extractor / reviewer pattern), fan the extraction out
across the docs so the heavy reading happens once and off your main context, then
thread each concept's slice of the digest to whoever authors it. The digest is a
working artifact, not a bundle doc — it never ships; only the facts it routes into
concept docs do.

If no context documents were provided, **skip this pass entirely** — do not
speculate about where the data "comes from," do not add links to public datasets,
docs sites, or repositories, and do not attribute a schema to an external origin
you did not read. An unverifiable citation is worse than none.

### Pass 4 — Indexes and conformance (usually your runtime's job)

Most runtimes regenerate indexes and validate conformance **for you automatically
after authoring** (and enforce frontmatter on every write) — in which case there
is NOTHING to run here: just make sure every doc you wrote is conformant. Only if
your environment does not do this, use whatever index/validate tooling it
provides. The two outcomes either way:

1. **`index.md` regenerated** at every directory level for progressive disclosure
   (so a reader/agent can see what's available before opening files).
2. **Conformance validated** against §9 (every non-reserved `.md` has parseable
   frontmatter with a non-empty `type`).

The **Conformance checklist** at the end of this skill is the definition of
done — verify against it, and never hand-write `index.md` files.

### Pass 5 — Adversarial review (when your runtime supports it)

Conformance (pass 4) checks that a doc is well-formed; it does not check that the
doc is *true*. A grain stated but never measured, a join copied from a context doc
that actually fans out, an enum decoded from the wrong column, an SQL snippet that
errors — all pass conformance and all mislead a consumer. So when your runtime can
dispatch independent verifiers (reviewer sub-agents), run an adversarial review
over the finished bundle before declaring done. Run it ONCE: apply the confirmed
findings and finish — do not loop review → fix → review, and do not stack further
verification passes on top of it.

- **Independence is the point.** The author of a doc carries the author's bias —
  it will rationalize the grain it already stated and re-run the same query that
  "confirmed" it. Route review through a SEPARATE agent given only the finished
  docs and the live source, prompted to REFUTE the load-bearing claims (grain,
  join keys + cardinality, enum decodings, gotchas, every SQL snippet) against
  live data. A finding is only real if a query reproduces it; fix only confirmed
  findings.
- **Review in link-based clusters, not one doc at a time.** Group the docs into
  small clusters of RELATED docs (at most ~5 per cluster) and dispatch one
  reviewer per cluster. Relatedness comes from the link graph — a table with the
  `references/joins/*` and `references/enums/*` docs that link to it belongs in
  one cluster (if your runtime provides a clustering tool over the link graph,
  use it; otherwise derive clusters from the docs' links/backlinks). Clustering
  is not just cheaper than one reviewer per doc: a reviewer holding the whole
  related set can also catch **cross-doc contradictions** — a join doc whose keys
  or cardinality disagree with its table docs, an enum doc that contradicts the
  schema row linking it — which per-doc review structurally cannot see. Every
  doc in a cluster gets the full checklist; a doc skimmed because it shared a
  reviewer is a doc that ships unverified.
- **Cover the WHOLE bundle, not a subset.** Build the cluster set by enumerating
  the actual authored docs on disk, not from memory — every `tables/*`, every
  `references/**/*` (joins, metrics, enums, named_sets, glossary, known_issues),
  and the `datasets/*` overview lands in exactly one cluster. Exclude only
  reserved generated files (`index.md`, `log.md`). Reviewing only the tables, a
  "representative" sample, or only the docs you think are risky is a spot check,
  not a review — the bugs you miss are precisely the ones in the docs you
  skipped. If you must bound the pass, say which docs went unreviewed rather
  than letting a partial pass read as a complete one.

## Frontmatter

```yaml
---
type: <Type name>                  # REQUIRED — the only field consumers rely on
title: <Human-readable display name>
description: <ONE sentence>         # used verbatim in generated index.md
resource: <canonical URI of the underlying asset>   # when the concept maps to a real asset
tags: [<tag>, <tag>]
timestamp: <ISO 8601 datetime>     # last meaningful change
---
```

- `type` is the **only required** field. `title`/`description` are strongly
  recommended (they power indexes and search). `description` must be **one
  tight sentence** — it is reused verbatim in auto-generated `index.md`.
- `resource` is the canonical URI/ARN of the underlying asset (e.g. a Glue table
  ARN). Omit it for abstract concepts (a playbook, a pure definition).
- Producers MAY add **any** extra keys; consumers must preserve and tolerate
  unknown keys. Don't reject or drop keys you don't recognize.
- When refining/augmenting an existing doc, **pass the complete frontmatter
  dict** — `write` is a full replacement, so omitting a key drops it. Preserve
  existing `type`/`title`/`resource` verbatim; merge (don't replace) `tags`.

## Body

Standard markdown. **Favor structure** — headings, lists, tables, fenced code
blocks — over freeform prose; structure helps both human reading and agent
retrieval. No body section is required. These headings have **conventional**
meaning; use them when applicable:

| Heading                  | Purpose                                                    |
|--------------------------|------------------------------------------------------------|
| `# Overview`             | Prose lead of an asset doc: what it is, the verified grain, time range, caveats. |
| `# Schema`               | Structured description of an asset's columns/fields.       |
| `# Common query patterns`| 1–3 short, realistic SQL (or query) snippets in fenced blocks. |
| `# Joins`                | Bullet links to the `references/joins/*` docs this table participates in. |
| `# Metrics`              | Bullet links to the `references/metrics/*` docs this table feeds. |
| `# Gotchas`              | "Do NOT use X for Y; use Z" notes for columns/metrics an author would wrongly reach for. |
| `# Examples`             | Concrete usage examples.                                   |
| `# Citations`            | Numbered external sources backing claims. See below.       |

### `# Schema` — enumerate normal tables, summarize wide ones

For a table with a manageable column count, the `# Schema` is the familiar
`| Column | Type | Description |` table with **one row per column**. But a
one-row-per-column table is the wrong tool for a **wide table** (hundreds of
columns, or many that repeat a pattern): it bloats the doc, buries the few
columns that carry distinct meaning, and an enumerated list of
`home_player_1 … home_player_11` teaches a reader nothing the pattern wouldn't.

So for wide tables, **describe column families, not every column**:

- Enumerate individually, one row each, the columns that are *individually
  meaningful*: keys, foreign keys, timestamps, top-level measures, status/enum
  columns — the ones a query actually filters or groups on by name.
- Collapse each *repeating family* into a **single entry** that gives the family's
  membership rule (a regex or a `prefix_<index>` pattern + the index range), the
  **shared type**, one-line semantics for the whole family, and 1–2 concrete
  example members. Use a `## <family>` sub-heading or a single schema row whose
  Column cell is the pattern. Never list a family member-by-member.
- State the **column budget** in prose so the reader knows what was summarized:
  "115 columns: 11 core match/result columns enumerated below, plus three
  repeating families (44 player-position, 22 player-slot FK, 30 betting-odds)."

A family entry is itself a disambiguation aid: `home_player_X<n>` / `_Y<n>`
(pitch coordinates) are a different family from `home_player_<n>` (player-id FKs)
despite the near-identical prefix — name both families and what separates them.
Keep `# Gotchas` for the cross-column confusions that survive this.

Use `# Gotchas` whenever a concept has a confusable sibling — a near-synonym
column (`results.laps` the per-race count vs the `lapTimes` per-lap rows; `rank`
vs `position`), or an attractive-but-wrong source for a common phrasing. State
which column/source the right answer maps to **and** the one a reader would
mistakenly grab. This is the single highest-value section for steering a
text-to-SQL consumer; a confusable concept without one is incomplete.

A good asset doc body, in order: a 1–3 paragraph prose description (for a table,
state the **verified grain** — "one row per X" — plus time range and any sampling/
obfuscation caveats), then `# Schema`, then `# Common query patterns`, then
`# Joins` and `# Metrics` (bullet links to the reference docs this table
participates in), then `# Gotchas` (when the concept has a confusable sibling),
then `# Citations`. Concept-type templates are in `references/templates.md`.

Keep bodies clean: no preamble, no apologies, no reasoning narration. The body
must be valid markdown a human or downstream agent consumes directly.

Right-size every body: match a doc's length to what its content needs — cover
the substance, then stop. Do not pad with filler sections, restated overviews,
or boilerplate, and omit a conventional heading entirely when the concept has
nothing real to say under it (a `# Gotchas` with no gotcha is noise). A short,
dense doc serves a reader better than a long, padded one.

## Cross-linking

Link to other concepts with **standard markdown links**. The relationship type
(parent/child, references, joins-with) is conveyed by the surrounding prose, not
the link itself.

- **Prefer file-relative paths** so links resolve when the bundle is browsed as
  plain files (e.g. on GitHub). From `tables/orders.md`:
  - sibling: `[customers](customers.md)`
  - parent dataset: `[sales dataset](../datasets/sales.md)`
  - reference doc: `[event parameters](../references/event_parameters.md)`
- The spec also permits bundle-root-absolute links (starting with `/`), but those
  break GitHub rendering — **use file-relative** unless you have a reason not to.
- Only link to concepts that actually exist in the bundle. Don't invent targets.
  (Consumers tolerate broken links — they may be not-yet-written knowledge — but
  don't author dangling links on purpose.)
- One link per concept-mention per section is enough. Don't over-link, and don't
  link from headings, fenced code blocks, or schema field-name listings.

## Cross-dataset reference docs

When the task is to document knowledge that SPANS two datasets — verified
cross-dataset joins, conformed dimensions, cross-dataset metrics, a pair
overview — the rules change in specific ways. Above all: it is a BUSINESS
question first — understand both wikis and find genuine shared entities before
any column matching or SQL, and treat "these datasets don't relate" as a valid,
common outcome that authors nothing. Then: symmetric prose, qualified
identifiers everywhere, a per-candidate verification bar, links to the table
docs on BOTH sides (home file-relative + the bundle-escaping counterpart
address, which may dangle — accepted), refuted candidates recorded as caveats.
Read `references/cross-dataset.md` for the full methodology; SKILL.md's
quality bars all still apply on top of it.

## Citations

When a body makes claims, list what backs them under a `# Citations` heading at
the bottom. Cite **only** sources you actually consulted:

- the concept's own `resource` (e.g. the underlying asset's URI/ARN) — include it
  as the first entry when present;
- **uploaded context documents** the caller provided (cite them by their path /
  filename), if any informed the doc.

**Never invent a citation.** Do not add a URL to a public dataset, docs site,
blog, or repository that you did not read, and do not guess a schema's public
"origin" from prior knowledge — you have no web access, so any such URL is
unverifiable and must not appear. If nothing external backs a claim, the only
citation is the `resource`; an omitted citation is better than a fabricated one.

```markdown
# Citations

- arn:aws:glue:<region>:<acct>:table/<db>/<table>   # the concept's own resource
- .context/<uploaded-doc>.md                        # only if it informed this doc
```

## Reserved files

| Filename   | Meaning                                                          |
|------------|------------------------------------------------------------------|
| `index.md` | Directory listing for progressive disclosure (generated). See below. |
| `log.md`   | Optional chronological update history, newest first.             |

Both are reserved at **every** level — never name a concept doc `index.md` or
`log.md`.

- **`index.md`** has no frontmatter (the bundle-root one may optionally carry a
  single `okf_version: "0.1"` key). Body = sections grouping links by `type`,
  each entry carrying the linked concept's `description`. Let the script generate
  these.
- **`log.md`** uses `## YYYY-MM-DD` date headings (ISO 8601), newest first, with
  prose entries optionally led by a bold word (`**Update**`, `**Creation**`,
  `**Deprecation**`).

## Conformance checklist (verify before declaring done)

1. Every non-reserved `.md` file has a parseable YAML frontmatter block.
2. Every frontmatter block has a non-empty `type`.
3. `index.md` / `log.md` follow their structure where present.

Then quality (soft, but do it — conformance checks none of these): one-sentence
`description` on every doc; concepts cross-linked; `index.md` regenerated;
SQL/examples are real, not invented, **and in the source's pinned dialect**;
every asset's **grain is measured, not assumed**; wide tables **summarize
repeating column families** instead of enumerating every column; every confusable
column/metric carries a `# Gotchas` note; **every join/enum/metric taken from a
context doc was verified against live data** (and joins beyond those the context
mentioned were sought out); **every join doc states its measured cardinality and
orphan behavior** (inner- vs left-join advice); **volatile stats (row counts,
sizes, freshness timestamps) are omitted** unless a magnitude is stable and
decision-shaping — and measured evidence is recorded as proportions + mechanism,
never raw tallies; citations point to sources you actually used.

## Files in this skill

- `references/spec-condensed.md` — the normative OKF v0.1 rules, condensed.
- `references/templates.md` — copy-paste frontmatter+body templates per concept type.
- `references/fact-types.md` — the fact-extraction checklist: ~25 fact types (business terms, metrics, joins, code/enum legends, caveats, units, named sets, conditional population, hierarchies, deprecations, canonical recipes, …), the cue phrases to find each in docs, the data-side probes for when no docs exist, and where each lands in the bundle. Read it in Pass 3 (folding in uploaded context) and when mining the source for gotchas/enums.
- `references/cross-dataset.md` — the cross-dataset methodology: understand both wikis FIRST (business convergence + the plausibility gate — unrelated pairs author nothing), then the column-evidence lenses (shared/near-synonym keys, conformed dimensions, shared enum vocabularies, joined-only metrics), the per-candidate SQL verification bar (overlap, measured cardinality, orphans, format agreement), and the doc conventions for pair docs that are read by consumers of both datasets. Read it when a task names a counterpart dataset to document relationships against.
- `references/sources/` — per-backend adapters (Athena+Glue, Redshift, …): source-specific schema extraction, `type`/`resource`/dialect conventions, type vocabulary, idioms, and gotchas. See `references/sources/index.md` to pick one or add a new one.
