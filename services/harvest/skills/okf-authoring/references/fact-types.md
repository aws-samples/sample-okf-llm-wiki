# Fact-type extraction guide

When you read a **context document** (Pass 3) — a data dictionary, code list,
glossary, methodology PDF, wiki page, query log — or mine the **primary source**
(Pass 1), you are hunting for *facts* that make the data queryable. This guide is
the checklist: it names the fact types worth capturing, tells you **what to look
for in the docs** to find each, and — the part that matters most — **where each
fact lands in the OKF bundle**.

OKF does not add new frontmatter for these. A fact type is a *lens for reading
and routing*, not a schema. You capture a fact by writing it into the right
existing place (a schema-row description, a `# Gotchas` note, a `references/` doc)
so a consuming agent finds it where it expects to. Never invent a fact the source
or an uploaded doc does not actually state.

## How to use this (the extraction loop)

For each context doc, and for each table you author:

1. **Skim for the cue phrases** in the "Look for" column below — they are where
   these facts hide in real documentation.
2. **Route each fact** to its OKF home (the "Lands in" column). One doc usually
   yields several fact types across several concepts.
3. **Ground it**: a fact from a context doc is a *hypothesis*, not a settled fact.
   VERIFY it against the primary source (`run_sql`/`sample_rows`) before it enters
   the bundle — confirm the join keys match on both sides and check the
   cardinality, run the metric SQL, sample the coded column to see the codes the
   doc claims. Where the data contradicts the doc, the data wins and the
   discrepancy is a CAVEAT worth a `# Gotchas` note. Cite the uploaded doc under
   `# Citations`. And a context doc naming one join/metric does not cap your
   search: still probe the shared keys and relationships it never mentioned
   (`grep .metadata/columns.tsv`) — context widens the investigation, never limits
   it.

The single most common miss is **CODE_ENUM** — coded columns whose meanings sit
in a data dictionary or code list you were given but didn't fully transcribe.
Decode them (see the dedicated section below); that is usually the difference
between a wiki an agent can query and one it can't.

## Core fact types

| # | Fact type | Look for in the docs | Lands in the OKF bundle |
|---|-----------|----------------------|-------------------------|
| 1 | **BUSINESS_TERM** | glossaries, "also known as / aka", acronym expansions, non-English labels, "the business calls this X" | the column's `# Schema` description; if it's a confusable alias, a `# Gotchas` note; a reusable term → `references/glossary/<term>.md` |
| 2 | **METRIC_DEFINITION** | "calculated as", "defined as", KPI formulas, "= sum(...) / ...", numerator/denominator prose | `references/metrics/<slug>.md` (owns the SQL); tables link it from `# Metrics` |
| 3 | **JOIN_CONDITION** | ER diagrams, "foreign key", "joins to … on …", "one-to-many", relationship tables — **plus joins no doc mentions:** `grep .metadata/columns.tsv` for shared keys | `references/joins/<a>__<b>.md` (owns the `ON` clause); both tables link it from `# Joins`. **Verify keys match + establish cardinality with a real query before documenting; a doc's asserted join that fails or fans out unexpectedly is a `# Gotchas` finding** |
| 4 | **CODE_ENUM** | data dictionaries, **code lists**, value tables, "1 = …, 2 = …", "valid values", category enumerations, status-flag legends | small set → inline in the `# Schema` row description; large set (>~15) → `references/enums/<column>.md`. **See below.** |
| 5 | **FILTER_RULE** | "by default we exclude", "only count active", "unless stated we filter …", source-preference rules | `# Gotchas` (the rule) and/or the metric doc's `## When to use which`; a global default → dataset overview |
| 6 | **GRAIN_STATEMENT** | "one row per", "each record represents", "unique by", primary-key notes | table prose ("one row per X") — **measure it, don't just copy** (grain-verification rule) |
| 7 | **CAVEAT** | "note that", "be careful", "known issue", "caution", footnotes, "data quality" sections | `# Gotchas` on the affected table; a cross-cutting one → `references/known_issues/<slug>.md` |
| 8 | **TEMPORAL_RULE** | fiscal-calendar definitions, timezone notes, "as of", partition/refresh/SLA docs, "snapshot vs event" | `# Gotchas` or table prose; partitioning goes in `# Schema`/prose per the source adapter |
| 9 | **MEASURED_IN** | units in parentheses/headers, "in USD", "in kg", "(%)", "amounts in thousands", scaling factors | the column's `# Schema` description (state the unit + any scale/implied decimals) |
| 10 | **DATA_LINEAGE** | "sourced from", "derived from", ETL/pipeline docs, "upstream table", refresh diagrams | dataset/table `# Overview` prose. **Only what a doc/source states — never a guessed public origin** |
| 11 | **QUERY_PATTERN** | example queries, FAQ "how do I …", saved reports, query logs, "typical analysis" | `# Common query patterns` (validate it runs, in the source's dialect) |
| 12 | **BUSINESS_CONTEXT** | intro/overview sections, domain background, "about this dataset", purpose statements | the dataset doc's `# Overview` prose (`datasets/<dataset>.md`) |
| 13 | **MEASURE** | "total", "sum of", "amount", "count of", numeric fact columns an analyst aggregates | mark it in the `# Schema` description as an aggregatable measure; feeds METRIC_DEFINITION |
| 14 | **DIMENSION** | "by region / by month / per category", grouping attributes, categorical descriptors | mark it in the `# Schema` description as a grouping/filter dimension; often also a CODE_ENUM |

## Governance fact types (extract only on explicit evidence)

These carry query-time enforcement weight — a wrong one yields a confidently
wrong answer — so capture them **only when a doc or the data explicitly supports
them**, never by inference. When present they are `# Gotchas`-grade or their own
`references/` doc, and worth stating emphatically.

| # | Fact type | Look for in the docs | Lands in the OKF bundle |
|---|-----------|----------------------|-------------------------|
| 15 | **NAMED_SET** | "the X group consists of …", region/segment definitions, "Europe = these countries", curated value lists | `references/named_sets/<name>.md` — the business name + its governed `IN (…)` list; reference it from any metric/filter that uses it |
| 16 | **LIFECYCLE_STAGE** | status-workflow diagrams, "an order is 'open' when status in …", stage→code mappings | `references/named_sets/<name>.md` (a business stage → its set of raw status/event codes is named-set-shaped) — or a `# Gotchas` note for a single simple mapping |
| 17 | **AMBIGUOUS_TERM** | the same word defined two ways, "depending on context", "this can mean either …" | `# Gotchas` on the table **AND** a line in `references/usage_guardrails.md` (see #19) — state each genuinely-different sense and which column/filter each maps to, forcing disambiguation |
| 18 | **DISJOINT_MEASURES** | "do not add", "mutually exclusive", "gross vs net", "don't UNION these", overlapping-population warnings | `# Gotchas` **AND** `references/usage_guardrails.md` — an explicit never-sum/never-union warning naming the measures/tables and why |
| 19 | **DATASET_GUARDRAIL** | any "how to work with this data correctly" rule that a consuming agent must obey to avoid a *confidently wrong* answer: measure additivity by type (flow vs stock/snapshot — what may be summed over time vs geography), when to ASK vs answer (a required dimension — period/region/grain/scope — is missing or a term resolves to >1 thing), when to BLOCK (a well-formed but semantically invalid computation, e.g. summing a non-additive stock over time; a metric the source explicitly withholds), when to REFUSE (out-of-domain / unserved capability), sentinel/reserved values that corrupt filters, default readings (a default scope/variant the data assumes), and "never fabricate — abstain if unresolvable" | `references/usage_guardrails.md` — the dataset's behavioural contract (see below). **Linked prominently from the dataset overview so a consumer reads it first.** |
| 20 | **CANONICAL_RECIPE** | a non-trivial transform that MUST be applied identically on every query of a table — a snapshot/firmness dedup (`ROW_NUMBER` + a mandatory pre-filter), a required de-duplication, a standard collapse — anything where re-deriving it slightly differently changes the result | `references/recipes/<slug>.md` — the ONE authoritative, non-decomposable SQL fragment; every metric doc and every `# Common query patterns` snippet on the affected table LINKS it and never re-derives it (see the deep case below). |

## DATASET_GUARDRAIL — the behavioural contract (`references/usage_guardrails.md`)

This is the one doc a consuming agent should read **before** it queries — the
rules that keep answers deterministic and hallucination-free. It concentrates the
cross-cutting behavioural facts (additivity, ASK/BLOCK/REFUSE triggers, ambiguous
terms, default readings, filter traps) that otherwise scatter across per-table
`# Gotchas` where a consumer reading only one table would miss them.

**Where the content comes from — derived + from `.context/`, never invented:**

- **Derived from what you VERIFIED during harvest.** You already measure each
  measure's additivity, find which terms are ambiguous, and discover which
  capabilities the source does *not* serve. Promote those verified facts into
  guardrail rules: "wins is a count — additive across seasons and circuits";
  "points_standing is a snapshot — never sum across rounds; take the final-round
  value"; "'season' term resolves to >1 column — ASK which".
- **From uploaded `.context/` docs** that state working rules explicitly (a
  query-rules doc, a methodology PDF's "do not" section, an SME guardrails file).
  Cite them under `# Citations`.
- **Never invent a rule.** A guardrail is a *verified* fact like any other: if the
  data doesn't support "never sum X" and no doc states it, don't assert it. A
  wrong guardrail yields a confidently-wrong refusal, which is as harmful as a
  fabricated number.

Shape it so a consumer can act on each rule: name the concrete measure/column/
term, the rule, and the correct alternative. Group by disposition where it helps
(what to answer directly, what to ASK about, what to BLOCK, what to REFUSE). This
doc is authored by the supervisor's `reference-author` fan-out and **must be
linked from `datasets/<dataset>.md`** (the file a consumer lands on first).

## CANONICAL_RECIPE — the deep case (author a transform ONCE, apply it everywhere)

Some tables require a non-trivial transform on **every** query — most commonly a
snapshot/firmness **dedup** (a fact re-stated across reporting cycles, where you
must pick exactly one row per business cell). When that recipe is described in
prose, or shown slightly differently in two `# Common query patterns` snippets,
consumers re-derive it inconsistently and silently change the result (a newer,
non-final snapshot wins a cell it shouldn't).

**Author it as ONE atomic, non-decomposable fragment** in
`references/recipes/<slug>.md`, and make every metric doc + every query-pattern
snippet on the affected table **link that recipe rather than restate an ORDER BY.**
The recipe fragment must fuse *all* of its required parts — for a snapshot dedup:

- the **mandatory pre-filter** (e.g. keep only final/actuals-class status rows)
  placed **inside** the dedup subquery, named explicitly; and
- the **`ROW_NUMBER` `ORDER BY` that leads with the correct key** (e.g. lead with
  the firmness/status rank, not with a timestamp that only tiebreaks) — state which
  ordering is authoritative and WHY, because a plausible re-ordering picks a
  different winner.

Verify the recipe with a real query (a row-count before/after the collapse proves
it de-duplicates to the intended grain). A consumer must be able to copy the
fragment verbatim; "dedup by latest snapshot" in prose is not enough.

## CODE_ENUM — the deep case (decode coded columns)

Coded columns (integers or short codes standing for categories — `SCHL`, `RAC1P`,
`status`, `region_code`) are opaque without their legend. Their legend almost
always lives in a **data dictionary or code-list** doc. Decoding them is the
single highest-leverage fusion you do.

**Where to find the legend.** Scan uploaded docs for: a table with a code column
and a description column; rows like `1 = Male, 2 = Female`; ordered category
prose ("categories: Very well, Well, Not well, Not at all" — often 1-indexed in
that order); a spreadsheet with a per-variable sheet; a "Valid values" / "Codes"
section. Cross-check the codes you find against real values with
`sample_rows`/`run_sql` — a code present in the data but absent from the doc (or
vice-versa) is itself a CAVEAT worth noting.

**Decode sentinel / reserved values too — they are filter traps.** Dictionaries
routinely reserve codes for "not applicable", "unknown", "not in universe", or
"suppressed" — a blank, a `b`/`bb`, a `9`/`99`/`999`, a `-1`, a top/bottom-code.
These silently corrupt `WHERE`/aggregates if an agent treats them as ordinary
values (summing a `999`-means-unknown income, counting `N/A` rows as a real
category). When you decode an enum, decode its sentinels **and flag them** — note
which value means "no data" so a query can exclude it. A wrongly-handled sentinel
is a CAVEAT worth a `# Gotchas` line on top of the enum entry.

**How to render it — size decides the shape:**

- **Small set (≤ ~15 values)** — decode **inline** in the `# Schema` row, so an
  agent reading the schema sees the meanings without another hop:

  ```markdown
  | `ten` | string | Tenure: `1` owned w/ mortgage, `2` owned free & clear, `3` rented, `4` occupied without rent. |
  ```

- **Large set (> ~15 values, e.g. `OCCP` 530 codes, `LANP` 131)** — do **not**
  bloat the schema row. Mint a dedicated enum reference and link it:

  ```markdown
  # in tables/person.md schema row:
  | `occp` | string | Occupation code (2018 Census OCC). See [OCCP codes](../references/enums/occp.md). |
  ```

  ```markdown
  # references/enums/occp.md   (type: Reference)
  ---
  type: Reference
  resource: arn:aws:glue:<region>:<acct>:table/<db>/<table>
  title: OCCP occupation codes
  description: Decodes the OCCP occupation code to its Census occupation title.
  ---
  Occupation recode (2018 Census OCC codes). 530 values.

  | Code | Meaning |
  |------|---------|
  | 0010 | Chief Executives and Legislators |
  | 0020 | General and Operations Managers |
  | …    | … |

  # Citations
  - .context/<the-code-list-or-dictionary-you-transcribed>
  ```

  An enum reference minted from an uploaded doc **must cite that doc** under
  `# Citations` (it's transcribed provenance) — same rule as every other doc.

  For a very large set, transcribe the **full** legend when the source provides
  it — a dedicated enum doc has room for hundreds of rows, and "don't bloat the
  schema row" never meant "don't record the codes anywhere." The reader cannot
  open the source dictionary (it is an authoring-time input, invisible at read
  time — see "Consumers read only the bundle" in SKILL.md), so "530 codes; see
  the source dictionary for the full list" leaves the reader with no legend at
  all. Truncate ONLY when the source itself is incomplete; if you must, say which
  codes you captured and why the rest are unavailable ("the source lists only
  these 40 of ~530") — never point at an invisible file as the completion.

**Never invent a code meaning.** If the doc gives ordered categories but not
explicit numbers, and you infer the 1-based mapping, say it's inferred; if you
can't determine a code's meaning, leave it undecoded rather than guess — a wrong
decode is worse than a missing one.

## Routing summary

- **`# Schema` row description** — BUSINESS_TERM, small CODE_ENUM, MEASURED_IN,
  MEASURE/DIMENSION marks.
- **`# Gotchas`** — CAVEAT, FILTER_RULE, TEMPORAL_RULE, AMBIGUOUS_TERM,
  DISJOINT_MEASURES, confusable BUSINESS_TERM. (The behaviour-shaping ones —
  AMBIGUOUS_TERM, DISJOINT_MEASURES, additivity, ASK/BLOCK/REFUSE triggers — also
  roll up into `references/usage_guardrails.md` so a consumer sees them in one
  place, not only on the table they happen to open.)
- **`references/usage_guardrails.md`** — DATASET_GUARDRAIL: the one behavioural
  contract read before querying (additivity, ASK/BLOCK/REFUSE rules, default
  readings, filter traps). Linked from the dataset overview.
- **`references/recipes/<slug>.md`** — CANONICAL_RECIPE: a must-apply-identically
  transform (e.g. snapshot dedup) authored once; metric + query-pattern docs LINK
  it, never re-derive it.
- **`# Common query patterns`** — QUERY_PATTERN. **On a snapshot/dedup table, the
  snippet LINKS the `references/recipes/` dedup fragment rather than re-deriving an
  ORDER BY. For any full-grain column omitted from the dedup `PARTITION BY`, state
  WHY it's safe to drop — it's collapsed by a mandatory pre-filter named INSIDE the
  subquery, or it stays in the partition.**
- **`# Overview` prose** (in `datasets/<dataset>.md` or `tables/<table>.md`) —
  GRAIN_STATEMENT, BUSINESS_CONTEXT, DATA_LINEAGE.
- **`references/<type>/<slug>.md` docs** (all carry `type: Reference` +
  `title`/`description`, and `# Citations` when minted from an uploaded doc — see
  the templates). **Every extracted fact that becomes a standalone doc lives under
  a fact-typed parent folder — one doc per item, folder named for the fact type.
  This layout is CANONICAL, not optional: it is what makes bundles uniform across
  every harvest and dataset, so a consuming agent finds a metric under
  `references/metrics/` in every bundle.** Do NOT put a reference doc directly
  under `references/` or invent a different folder name.

  | Fact type | Folder | Example path |
  |---|---|---|
  | METRIC_DEFINITION | `references/metrics/` | `references/metrics/revenue_per_customer.md` |
  | JOIN_CONDITION | `references/joins/` | `references/joins/<a>__<b>.md` |
  | large CODE_ENUM | `references/enums/` | `references/enums/<column>.md` |
  | NAMED_SET / LIFECYCLE_STAGE | `references/named_sets/` | `references/named_sets/<name>.md` |
  | reusable BUSINESS_TERM | `references/glossary/` | `references/glossary/<term>.md` |
  | cross-cutting CAVEAT | `references/known_issues/` | `references/known_issues/<slug>.md` |
  | CANONICAL_RECIPE | `references/recipes/` | `references/recipes/snapshot_dedup.md` |
  | DATASET_GUARDRAIL | `references/` (single doc) | `references/usage_guardrails.md` |

  `usage_guardrails.md` is the one exception to "one doc per item under a
  fact-typed folder": it is a **single** behavioural-contract doc per dataset
  (not a folder of many), sitting directly under `references/`, because a
  consumer must find the whole contract in one read.

  Notes: LIFECYCLE_STAGE is named-set-shaped (a business stage → its raw status/
  event codes), so it goes under `references/named_sets/` — not one column's
  legend. `glossary/` and `known_issues/` are **one doc per term / per issue**
  (each independently linkable), NOT a single collecting file. The slug is a short
  kebab/snake identifier for the item (`gross_margin`, `duplicate_race_rows`).

Every fact still obeys the core rules: real, verified against the source where
possible, in the source's SQL dialect, and cited to the doc it came from.
