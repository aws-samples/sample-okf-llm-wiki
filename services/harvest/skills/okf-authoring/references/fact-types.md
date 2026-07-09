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
3. **Ground it**: quote/verify against the primary source where you can
   (`run_sql`/`sample_rows`); cite the uploaded doc under `# Citations`.

The single most common miss is **CODE_ENUM** — coded columns whose meanings sit
in a data dictionary or code list you were given but didn't fully transcribe.
Decode them (see the dedicated section below); that is usually the difference
between a wiki an agent can query and one it can't.

## Core fact types

| # | Fact type | Look for in the docs | Lands in the OKF bundle |
|---|-----------|----------------------|-------------------------|
| 1 | **BUSINESS_TERM** | glossaries, "also known as / aka", acronym expansions, non-English labels, "the business calls this X" | the column's `# Schema` description; if it's a confusable alias, a `# Gotchas` note; a reusable term → `references/glossary.md` |
| 2 | **METRIC_DEFINITION** | "calculated as", "defined as", KPI formulas, "= sum(...) / ...", numerator/denominator prose | `references/metrics/<slug>.md` (owns the SQL); tables link it from `# Metrics` |
| 3 | **JOIN_CONDITION** | ER diagrams, "foreign key", "joins to … on …", "one-to-many", relationship tables | `references/joins/<a>__<b>.md` (owns the `ON` clause); both tables link it from `# Joins` |
| 4 | **CODE_ENUM** | data dictionaries, **code lists**, value tables, "1 = …, 2 = …", "valid values", category enumerations, status-flag legends | small set → inline in the `# Schema` row description; large set (>~15) → `references/enums/<column>.md`. **See below.** |
| 5 | **FILTER_RULE** | "by default we exclude", "only count active", "unless stated we filter …", source-preference rules | `# Gotchas` (the rule) and/or the metric doc's `## When to use which`; a global default → dataset overview |
| 6 | **GRAIN_STATEMENT** | "one row per", "each record represents", "unique by", primary-key notes | table prose ("one row per X") — **measure it, don't just copy** (grain-verification rule) |
| 7 | **CAVEAT** | "note that", "be careful", "known issue", "caution", footnotes, "data quality" sections | `# Gotchas` on the affected table; a cross-cutting one → `references/known_issues.md` |
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
| 17 | **AMBIGUOUS_TERM** | the same word defined two ways, "depending on context", "this can mean either …" | `# Gotchas` — state each genuinely-different sense and which column/filter each maps to, forcing disambiguation |
| 18 | **DISJOINT_MEASURES** | "do not add", "mutually exclusive", "gross vs net", "don't UNION these", overlapping-population warnings | `# Gotchas` — an explicit never-sum/never-union warning naming the measures/tables and why |

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
  DISJOINT_MEASURES, confusable BUSINESS_TERM.
- **`# Common query patterns`** — QUERY_PATTERN.
- **`# Overview` prose** (in `datasets/<dataset>.md` or `tables/<table>.md`) —
  GRAIN_STATEMENT, BUSINESS_CONTEXT, DATA_LINEAGE.
- **`references/…` docs** (all carry `type: Reference` + `title`/`description`,
  and `# Citations` when minted from an uploaded doc — see the templates):
  `references/metrics/<slug>.md` — METRIC_DEFINITION; `references/joins/<a>__<b>.md`
  — JOIN_CONDITION; `references/enums/<column>.md` — large CODE_ENUM;
  `references/named_sets/<name>.md` — NAMED_SET **and** LIFECYCLE_STAGE (a
  business-stage→raw-codes mapping is named-set-shaped, not one column's legend);
  `references/glossary.md` — reusable BUSINESS_TERM; `references/known_issues.md`
  — cross-cutting CAVEAT. These subdirs are conventional, not enforced — any
  `references/…` path with `type: Reference` is valid.

Every fact still obeys the core rules: real, verified against the source where
possible, in the source's SQL dialect, and cited to the doc it came from.
