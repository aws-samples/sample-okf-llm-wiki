# OKF concept templates

Copy-paste starting points per concept kind. Replace placeholders; delete
sections that don't apply. All examples are valid OKF and mirror real bundle
output. `timestamp` is ISO 8601 UTC; set it to the time of the change (or let
your tooling fill it).

> **Dialect warning.** The SQL in these templates uses **Amazon Athena (Trino,
> engine v3) DML** — double-quoted `"db"."table"` identifiers and Athena/Trino
> type names (`varchar`, `bigint`, `double`, `timestamp`, `row(...)`). If your
> source is not Athena/Glue, **rewrite every snippet in your source's dialect**:
> identifier quoting, type names (Redshift `varchar`/`numeric`/`super`, Postgres
> `text`/`numeric`, SQLite `INTEGER`/`TEXT`/`REAL`, …), and functions. Copying one
> engine's syntax onto another source produces SQL that is actively misleading to
> a consuming agent. Pin the dialect once on the bundle-root doc
> (`dialect: <engine>`) per SKILL.md Pass 0, and load the matching source adapter
> in `references/sources/`.

Frontmatter key order that reads well: `type`, `resource`, `title`,
`description`, `tags`, `timestamp`. Only `type` is strictly required by the spec;
include `title`/`description`/`timestamp` for quality, and `resource` when the
concept maps to a real asset.

---

## Dataset / collection (a container of other concepts)

```markdown
---
type: Glue Database
resource: arn:aws:glue:<region>:<account_id>:database/<database>
title: <Display name>
description: <One sentence: what this dataset is and what it contains.>
tags: [<domain>, <domain>]
timestamp: 2026-05-28T00:00:00Z
---

# Overview
<1–2 paragraphs: what the dataset is, who produces it, what it's used for.
 Describe the tables and their grains; avoid baking in volatile row counts —
 see "Capture the essence, not the volatile numbers" in SKILL.md.>

# Using the dataset
<How to access/query it. A short, representative sample query if helpful — prefer
 one that shows a real analytical shape (a join, a group-by) over a bare
 `COUNT(*)`, which models nothing about what the data means.>

```sql
SELECT <dimension>, <aggregate>
FROM "<database>"."<table>"
GROUP BY <dimension>
```

# Citations
- arn:aws:glue:<region>:<account_id>:database/<database>
```

---

## Table / structured asset (the workhorse)

State the **grain** ("one row per X"), the time range, and any sampling or
obfuscation caveats in the prose. Populate `# Schema` from real metadata — never
invent fields.

```markdown
---
type: Glue Table
resource: arn:aws:glue:<region>:<account_id>:table/<database>/<table>
title: <Display name>
description: <One sentence describing the table and its grain.>
tags: [<tag>, <tag>]
timestamp: 2026-05-28T00:00:00Z
---

# Overview
<What this table is. The grain: "one row per ___". Time range. Caveats.>

# Schema

| Column        | Type      | Description                                  |
|---------------|-----------|----------------------------------------------|
| `id`          | varchar   | Globally unique identifier.                  |
| `customer_id` | varchar   | FK to [customers](customers.md).             |
| `total_usd`   | double    | Total in US dollars.                         |
| `created_at`  | timestamp | When the row was created.                    |

<!-- For nested struct/array fields (Glue Hive type struct<…>, array<struct<…>>;
     Trino DML row(…)/array(…)), use ## sub-headings and indented bullets:
## items (array<struct<…>>)
- `items.item_id` (varchar): The item ID.
- `items.price` (double): Unit price.
    - `items.price.value` (double): nested sub-field.
-->

<!-- WIDE TABLE (hundreds of columns / repeating families): do NOT enumerate every
     column. Enumerate the individually-meaningful ones, then collapse each repeating
     family into one entry with its pattern, type, and example members. Real example:
     the SQLite `Match` table (european_football_2) has 115 columns.

This table has 115 columns: 11 individually-meaningful columns (below) plus three
repeating column families described after.

| Column             | Type    | Description                                   |
|--------------------|---------|-----------------------------------------------|
| `id`               | INTEGER | Unique match id (PK).                         |
| `league_id`        | INTEGER | FK to [League](league.md).                    |
| `season`           | TEXT    | Season label, e.g. "2008/2009".               |
| `date`             | TEXT    | Match date.                                   |
| `home_team_api_id` | INTEGER | FK to [Team](team.md) — home side.            |
| `home_team_goal`   | INTEGER | Goals scored by the home team.                |
| `away_team_goal`   | INTEGER | Goals scored by the away team.                |

## Column families

- **Player-slot FKs** — regex `^(home|away)_player_([1-9]|1[01])$` (22 cols,
  INTEGER). FK to [Player](player.md) for each of the 11 lineup slots per side;
  `home_player_1` … `away_player_11`.
- **Player pitch coordinates** — regex `^(home|away)_player_[XY]([1-9]|1[01])$`
  (44 cols, INTEGER). X/Y formation grid position per slot. NOTE: distinct from
  the player-slot FKs above despite the shared prefix — `home_player_X1` is a
  coordinate, `home_player_1` is a player id.
- **Bookmaker odds** — regex `^[A-Z0-9]+[HDA]$` (30 cols, REAL). Pre-match
  win/draw/loss odds as `<bookmaker>{H,D,A}` triples for 10 bookmakers
  (e.g. `B365H`/`B365D`/`B365A` = Bet365 home/draw/away).
-->

# Common query patterns

```sql
SELECT customer_id, SUM(total_usd) AS revenue
FROM "<database>"."<table>"
GROUP BY customer_id
ORDER BY revenue DESC
```

# Joins
- [customers](../references/joins/customers__orders.md) — join on `customer_id` to attach customer attributes.

# Metrics
- [Revenue per customer](../references/metrics/revenue_per_customer.md) — SUM(total_usd) per customer.

# Gotchas
<!-- Include only when this table has a confusable sibling. State the wrong
     reach and the right one. Delete this section if nothing is confusable. -->
- `status` (this table) is the **current** order status, not the historical
  status timeline — for status-over-time use [order_events](order_events.md), do
  NOT read it from here.

# Citations
- arn:aws:glue:<region>:<account_id>:table/<database>/<table>
- .context/<uploaded-doc>          # only if an uploaded doc informed this table
```

---

## Reference doc (a standalone, reusable definition)

Lives under a **fact-typed folder** in `references/` — `references/<type>/<slug>.md`
(e.g. `references/enums/status.md`, `references/glossary/gross_margin.md`), one doc
per item. This folder scheme is canonical (see `references/fact-types.md` Routing
summary); never put a reference doc directly under `references/`. Used for an entity
definition, enum/status catalog, field-glossary term, units/convention note —
something *referenceable by name* and useful to **two or more** concepts (or
load-bearing background for one). `type` is `Reference`; `resource` points at the
underlying asset (a Glue ARN) when there is one, and is omitted for a purely
abstract definition.

```markdown
---
type: Reference
resource: arn:aws:glue:<region>:<account_id>:table/<database>/<table>
title: <Concrete noun — e.g. "Order status codes">
description: <One sentence defining the referenced thing.>
tags: [<topic>]
timestamp: 2026-05-28T00:00:00Z
---

<One- to few-paragraph definition. Concrete values, enums, field paths.>

# Citations
- arn:aws:glue:<region>:<account_id>:table/<database>/<table>
- .context/<uploaded-doc>          # only if an uploaded doc informed this doc
```

---

## Metric reference (`references/metrics/<slug>.md`)

The reference **owns the SQL**. One file per metric. Contributing tables link to
it from a `# Metrics` section (one bullet each, no duplicated SQL).

The fenced block holds **one canonical default expression** — not a menu. When
more than one table can answer the metric, pick the default for the metric's
plainest phrasing, put only that in the SQL block, and add a `## When to use
which` subsection mapping each natural-language phrasing to exactly one source.
Never list alternatives inside the SQL as a comment — that hands a consuming
agent the ambiguity instead of resolving it.

```markdown
---
type: Reference
resource: arn:aws:glue:<region>:<account_id>:table/<database>/<table>
title: User Count
description: Total number of unique users.
tags: [metric]
timestamp: 2026-05-28T00:00:00Z
---

Total number of unique users.

```sql
COUNT(DISTINCT user_id)
```

<!-- Add only when more than one source can answer this metric:
## When to use which
- "total points to date / championship standing" → cumulative `driverStandings.points`.
- "points scored in race X" → per-race `results.points`.
-->

# Citations
- arn:aws:glue:<region>:<account_id>:table/<database>/<table>
```

---

## Join reference (`references/joins/<a>__<b>.md`)

One canonical file per table pair, the two table names sorted alphabetically and
joined by a double underscore. Owns the concrete `ON` clause. Both sides link to
it from their `# Joins` section.

```markdown
---
type: Reference
resource: arn:aws:glue:<region>:<account_id>:table/<database>/orders
title: Join Orders to Customers
description: Join order rows to the customer who placed them.
tags: [join]
timestamp: 2026-05-28T00:00:00Z
---

Join order rows to the customer who placed them.

```sql
orders.customer_id = customers.id
```

Use this join to attach customer attributes (segment, region) to each order.

# Citations
- arn:aws:glue:<region>:<account_id>:table/<database>/orders
- arn:aws:glue:<region>:<account_id>:table/<database>/customers
```

---

## Usage guardrails (`references/usage_guardrails.md`)

The ONE behavioural contract a consuming agent reads before querying — the single
exception to "one doc per fact-typed folder" (it lives directly under
`references/`, one per dataset). Concentrates the cross-cutting rules that keep
answers deterministic and hallucination-free: additivity by measure type, when to
ASK / BLOCK / REFUSE, default readings, and filter/sentinel traps. Every rule is
DERIVED from a verified harvest fact or stated in a cited `.context/` doc — never
invented. **Link it from `datasets/<dataset>.md`** so a consumer meets it first.

```markdown
---
type: Reference
title: Usage guardrails
description: Rules for querying this dataset correctly — additivity, when to ask/block/refuse, default readings, and filter traps.
tags: [guardrails, behaviour]
timestamp: 2026-05-28T00:00:00Z
---

Read this before querying. Every number returned must be executed against the
served data, never guessed; if a value can't be resolved from the model, abstain.

# Additivity by measure
- `<flow_measure>` is a FLOW — additive across time AND geography; SUM freely over a bounded period.
- `<stock_measure>` is a SNAPSHOT/STOCK — additive across geography at one point in time, but NEVER summed across periods (take the period-end value). See [the metric](metrics/<slug>.md).

# Answer directly (COMMIT)
- A fully-specified measure + dimension(s) + bounded period → answer, disclosing the default reading used (e.g. `<default_scope>` when scope is unstated).

# Ask to clarify (ASK)
- A required dimension is missing (no period / no region / no grain) → ask which; never emit an unbounded SUM.
- A term resolves to more than one thing (`<ambiguous_term>` → {A, B}) → ask which, offering the candidates.

# Block (well-formed but invalid)
- `<stock_measure>` summed across time → refuse the number and explain; offer the period-end read instead.
- `<withheld_metric>` is not served here → do not present a figure.

# Refuse (out of domain)
- `<unserved_capability>` (e.g. a metric this dataset does not hold) → decline plainly; never fabricate. (An unresolvable VALUE — a likely typo — is an ASK, not a REFUSE.)

# Filter traps
- `<sentinel>` in `<column>` means "<no-data/unknown>" — exclude it from aggregates.

# Citations
- .context/<any-rules-doc-you-transcribed>   (omit if all rules were derived from verified data)
```

---

## Canonical recipe (`references/recipes/<slug>.md`)

A transform that MUST be applied identically on every query of a table — most
often a snapshot/firmness **dedup**. Authored ONCE as an atomic, copy-verbatim
fragment; metric docs and `# Common query patterns` snippets on the affected table
LINK it and never re-derive an ORDER BY. Verify with a before/after row-count that
the collapse de-duplicates to the intended grain. See CANONICAL_RECIPE in
fact-types.md.

```markdown
---
type: Reference
resource: arn:aws:glue:<region>:<account_id>:table/<database>/<table>
title: Snapshot dedup — one row per <business cell>
description: The mandatory, non-decomposable recipe to pick exactly one row per business cell from the re-stated snapshot fact. Apply verbatim on every query.
tags: [recipe, dedup]
timestamp: 2026-05-28T00:00:00Z
---

`<table>` re-states each business cell across reporting cycles. To get one row per
cell you MUST apply this exact fragment — both parts, inside one subquery. Both
are required; dropping either silently changes the result.

```sql
-- 1) mandatory pre-filter (INSIDE the subquery): keep only <final/actuals-class> rows
-- 2) ROW_NUMBER ordered by <firmness key FIRST>, then <tiebreakers>
SELECT ... FROM (
  SELECT *,
    ROW_NUMBER() OVER (
      PARTITION BY <the business-cell grain columns>
      ORDER BY <firmness_rank> DESC, <tiebreak_1> DESC, <tiebreak_2> DESC
    ) AS rn
  FROM <table>
  WHERE <status_col> = '<final/closed>'
) WHERE rn = 1
```

Why the ordering leads with `<firmness_rank>`, not a timestamp: <reason — a newer
non-final snapshot must not win a cell>. Reduced-dedup-key note: any full-grain
column omitted from `PARTITION BY` is safe to drop ONLY because `<named pre-filter,
e.g. status = 'final'>` collapses it inside the subquery — state that, or keep the
column in the partition.

# Citations
- arn:aws:glue:<region>:<account_id>:table/<database>/<table>
```

---

## Playbook / abstract concept (no underlying asset → no `resource`)

```markdown
---
type: Playbook
title: Incident response — data freshness alert
description: Steps to triage a freshness alert on the orders pipeline.
tags: [oncall, incident]
timestamp: 2026-04-12T09:00:00Z
---

# Trigger
A freshness alert fires when `orders` lags >30 min behind its SLA. See the
[orders table](../tables/orders.md).

# Steps
1. Check the [ingestion job dashboard](https://example.com/dash).
2. ...
```

---

## `index.md` (generated — shown for reference; don't hand-write it, it's regenerated for you)

No frontmatter. Entries grouped by `type`, carrying each concept's `description`.
Subdirectories are grouped under a `# Subdirectories` heading and link to the
child `index.md`.

```markdown
# Glue Table

* [Orders table](orders.md) - One row per customer order with totals and status.

# Subdirectories

* [references](references/index.md) - Specifications for data joins and metric definitions.
```

The bundle-root `index.md` MAY (uniquely) carry frontmatter with a single key:

```markdown
---
okf_version: "0.1"
---

# Subdirectories
...
```

---

## `log.md` (optional)

```markdown
# Directory Update Log

## 2026-05-22
* **Update**: Added Glue table reference for [Customer Metrics](tables/customer-metrics.md).
* **Creation**: Established the [Freshness Playbook](playbooks/freshness.md).

## 2026-05-15
* **Initialization**: Created foundational directory structure.
```
