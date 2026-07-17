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

## Workflow

Authoring a bundle runs through the passes below. Run pass 1 always; run pass 2 to
cross-link; run pass 3 only when the caller provided uploaded context documents
worth folding in; always finish with pass 4 (indexes + conformance). When your
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
     *assumed* composite key, test it:
     `SELECT COUNT(*) FROM (SELECT <key cols>, COUNT(*) c FROM <t> GROUP BY <key cols> HAVING c > 1)`.
     A non-zero result means the grain is coarser than X — state the true grain
     (or weaken it: "approximately one row per X; N duplicate keys exist because …")
     and note what the duplicates represent.
   - **Disambiguate near-synonyms.** Before writing column descriptions, list
     columns whose name or meaning overlaps another column (same name across
     tables; a per-row count vs a same-named detail table; per-period vs
     cumulative values). For each confusable pair, sample real values from both
     and write an explicit contrast — see the `# Gotchas` convention under Body.
   - **Discover joins yourself — don't wait to be told.** Find candidate
     relationships by grepping the cross-table column index for every shared key
     (`grep <name> .metadata/columns.tsv`), not just the joins a context doc
     happens to mention. For each candidate, VERIFY it against live data before
     documenting it — confirm the keys actually match on both sides and establish
     the cardinality (1:1, 1:many, many:many) with a real query, e.g.
     `SELECT COUNT(*) FROM a JOIN b ON a.k = b.k` vs the row counts, or a
     duplicate-key probe on the presumed FK. Document only joins that hold; if a
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
  extraction checklist: it names the ~20 fact types worth capturing (business
  terms, metric definitions, join conditions, **code/enum legends**, filter
  rules, grain, caveats, units, lineage, named sets, …), tells you the **cue
  phrases to look for** in docs to find each, and — critically — **where each
  fact lands in the bundle** (a `# Schema` description, a `# Gotchas` note, a
  `references/` doc). Read it before authoring from an uploaded doc.
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

### Pass 4 — Generate indexes and validate (always)

1. **Regenerate `index.md`** at every directory level for progressive disclosure
   (so a reader/agent can see what's available before opening files).
2. **Validate conformance** against §9 (every non-reserved `.md` has parseable
   frontmatter with a non-empty `type`).

How these run depends on your environment: many runtimes regenerate indexes and
validate conformance **for you automatically after authoring** (and enforce
frontmatter on every write), in which case you do not run anything here — just
make sure every doc you wrote is conformant. If your environment does not, use
whatever index/validate tooling it provides. Either way, the **Conformance
checklist** at the end of this skill is the definition of done — verify against
it, and never hand-write `index.md` files.

### Pass 5 — Adversarial review (when your runtime supports it)

Conformance (pass 4) checks that a doc is well-formed; it does not check that the
doc is *true*. A grain stated but never measured, a join copied from a context doc
that actually fans out, an enum decoded from the wrong column, an SQL snippet that
errors — all pass conformance and all mislead a consumer. So when your runtime can
dispatch independent verifiers (reviewer sub-agents), run an adversarial review
over the finished bundle before declaring done.

- **Independence is the point.** The author of a doc carries the author's bias —
  it will rationalize the grain it already stated and re-run the same query that
  "confirmed" it. Route review through a SEPARATE agent given only the finished
  doc and the live source, prompted to REFUTE the load-bearing claims (grain,
  join keys + cardinality, enum decodings, gotchas, every SQL snippet) against
  live data. A finding is only real if a query reproduces it; fix only confirmed
  findings.
- **Cover the WHOLE bundle, not a subset.** Build the review list by enumerating
  the actual authored docs on disk, not from memory — one reviewer per
  `tables/*`, per `references/**/*` (joins, metrics, enums, named_sets, glossary,
  known_issues), and the `datasets/*` overview. Exclude only reserved generated
  files (`index.md`, `log.md`). Reviewing only the tables, a "representative"
  sample, or only the docs you think are risky is a spot check, not a review — the
  bugs you miss are precisely the ones in the docs you skipped. If you must bound
  the pass, say which docs went unreviewed rather than letting a partial pass read
  as a complete one.

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
| `# Schema`               | Structured description of an asset's columns/fields.       |
| `# Common query patterns`| 1–3 short, realistic SQL (or query) snippets in fenced blocks. |
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
`# Gotchas` (when the concept has a confusable sibling), then `# Citations`.
Concept-type templates are in `references/templates.md`.

Keep bodies clean: no preamble, no apologies, no reasoning narration. The body
must be valid markdown a human or downstream agent consumes directly.

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
mentioned were sought out); **volatile stats (row counts, sizes, freshness
timestamps) are omitted** unless a magnitude is stable and decision-shaping;
citations point to sources you actually used.

## Files in this skill

- `references/spec-condensed.md` — the normative OKF v0.1 rules, condensed.
- `references/templates.md` — copy-paste frontmatter+body templates per concept type.
- `references/fact-types.md` — the fact-extraction checklist: ~20 fact types (business terms, metrics, joins, code/enum legends, caveats, units, named sets, canonical recipes, …), the cue phrases to find each in docs, and where each lands in the bundle. Read it in Pass 3 (folding in uploaded context) and when mining the source for gotchas/enums.
- `references/sources/` — per-backend adapters (Athena+Glue, Redshift, …): source-specific schema extraction, `type`/`resource`/dialect conventions, type vocabulary, idioms, and gotchas. See `references/sources/index.md` to pick one or add a new one.
