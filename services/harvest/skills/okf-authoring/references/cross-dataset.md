# Cross-dataset reference docs — methodology

How to author the docs that represent knowledge SPANNING two datasets: verified
cross-dataset joins, conformed dimensions, cross-dataset metrics, and the pair
overview. Read this when your task is a cross-dataset run — i.e. you were given
ONE counterpart dataset (its catalog snapshot and, usually, its published
bundle) and asked to document the relationship between it and the dataset you
are working in. Everything in SKILL.md still applies (verify, don't assume;
self-contain; capture essence, not volatile numbers); this file adds what is
DIFFERENT when the knowledge crosses a bundle boundary.

## The one framing rule

A cross-dataset doc has ONE home (the bundle of the dataset that ran the cross
task) but TWO audiences: consumers of the counterpart dataset are routed to it
too (typically via a cross-reference signal on their dataset's listing). So
every doc must be **symmetric and self-contained**:

- Write prose that reads correctly from EITHER dataset's perspective. Never
  "this dataset's orders table" or "the remote side" — name every table by its
  **fully-qualified identifier** (`"db"."table"`), every dataset by its id.
- **Link BOTH sides' docs — a link is an address, and addresses may go
  stale.** Every join/metric doc links the table docs it involves on both
  sides, in two forms:
  - *Home side* (this bundle): a normal file-relative link — e.g. from a
    `joins/` doc, `../../../../tables/orders.md`. These resolve in the link
    graph and are what stitch the pair docs into it: the relationship becomes
    discoverable from the table via backlinks instead of the pair subtree
    floating as an island.
  - *Counterpart side* (the other bundle): a bundle-escaping relative link —
    e.g. from a `joins/` doc,
    `../../../../../../<counterpart_domain>/<counterpart_dataset>/tables/customers.md`
    (two more `../` than the home form: up out of this bundle to the bundle
    tree root, then down into the counterpart). All bundles share one tree, so
    this is a real address that resolves when the tree is browsed as plain
    files. The per-bundle link graph deliberately IGNORES it (OKF tolerates
    dangling cross-bundle links), and if the counterpart is re-harvested or
    removed the address may dangle — that is accepted; the qualified SQL
    identifier in the prose remains the durable reference.
  Links between the pair's own docs (overview ↔ joins/metrics) are fine as
  always. The prose stays symmetric: both sides get their link.

## Phase 1 — Understand both datasets FIRST (no SQL yet)

The question a cross-dataset run answers is a **business** question — *do these
two datasets describe any of the same real-world entities or processes, such
that a consumer of one would ever reach for the other?* — not a mechanical one
(*which columns happen to join?*). Column matching cannot be the discovery
spine: enterprise column vocabularies overlap by coincidence everywhere
(`id`, `name`, `code`, `year`, `date`, `city`, `state`, `country` appear in
almost every dataset and prove nothing), while real convergences sometimes hide
behind differently-named columns. So the FIRST pass is reading, not querying:

1. **Read both published wikis.** Each side's dataset overview, usage
   guardrails, glossary, and the table docs' verified grain statements ("one
   row per X" — X is the business entity each table describes). Both bundles
   were already verified by their own harvests; they are the richest statement
   of what each dataset IS.
2. **Write down each side's entity inventory.** The handful of real-world
   entities and processes each dataset describes (customers, orders, drivers,
   races, schools, test scores…), at what grain, over what time span and
   geography/population.
3. **Look for genuine convergence points.** A convergence is a SHARED
   real-world entity or process — the same customers, the same products, the
   same events, the same geographic units *at a grain where linking is
   meaningful*. For each candidate convergence, articulate in one sentence the
   consumer question that would need both datasets ("revenue by customer
   segment", "lap times by tyre supplier"). If you cannot state such a
   question, it is not a convergence.

**The plausibility gate — unrelated is a valid, common outcome.** Two datasets
picked from a catalog usually DON'T meaningfully relate. If Phase 1 surfaces no
genuine shared entity — say, a motorsport results dataset and a school test
score dataset — then the honest result of the run is **no relationship**:
author NOTHING (no overview, no join docs) and state plainly in your summary
what you compared and why no convergence exists. Do NOT go looking for column
coincidences to justify a doc; a `year` column on both sides, two `state`
fields, or a generic `name` join is vocabulary overlap, not knowledge, and a
fabricated "relationship" doc actively misleads every consumer and agent that
later reads it. Weak or forced connections are worse than none.

## Phase 2 — Candidate evidence, only for plausible convergences

Only once a convergence passed the gate do you drop to column level, looking
for the MECHANISM that realizes it. Work from both sides' column indexes and
docs:

1. **Shared and near-synonym keys** — grep both column indexes for the
   convergence's identifier-shaped columns. A same-named column is the obvious
   lead; a differently-named column with the same value shape (`customer_id`
   vs `party_nbr`) is the valuable one — probe by VALUES, not names alone.
2. **Conformed dimensions** — the shared entity described on both sides at a
   linkable grain (the grain statements from Phase 1 are the tell).
3. **Shared enum vocabularies** — coded columns whose decoded legends overlap.
   A shared vocabulary usually means a shared upstream; a *nearly* shared one
   (same codes, different meanings) is a trap worth documenting.
4. **Metrics that only exist joined** — a measure on one side that is only
   meaningful cut by a dimension on the other. These justify the pair's
   existence to a consumer; each must trace back to a Phase-1 consumer
   question.

## Phase 3 — The verification bar: every candidate, BEFORE authoring

SQL enters HERE, and its job is to **test the specific hypotheses Phases 1–2
produced** — never to fish (don't run join probes across arbitrary column
pairs hoping something matches; a query you can't tie to a named convergence
is a query you shouldn't run). For each candidate relationship, measure with
real cross-database SQL (qualify every table with its database):

- **Key overlap, both directions.** What fraction of side A's keys exist in B,
  and of B's in A? `SELECT COUNT(DISTINCT a.k) ... WHERE b.k IS NULL` style
  anti-joins, or the two containment ratios. Overlap is asymmetric — a
  small-fact ⊆ large-dimension join is normal; near-zero overlap refutes the
  candidate no matter how good the names look.
- **Cardinality, measured.** 1:1, 1:N, or M:N — the group-by-having-count>1
  probe on each side of the join key. State what you MEASURED, not what an ERD
  or the names imply.
- **Orphan behavior.** Which side has keys the other lacks, and what do those
  rows mean (late-arriving? out-of-scope? deleted upstream)? Consumers must
  know whether to inner- or left-join.
- **Value-format agreement.** Types, casing, whitespace/zero-padding, sentinel
  values. If the join only works through a cast or `UPPER()`/`TRIM()`, the doc
  must carry that exact normalization in its SQL — an undocumented cast is a
  silent empty join for the next consumer.

**A refuted candidate is still knowledge.** If a Phase-1-plausible candidate
does NOT hold in the data (wrong grain, disjoint values, homonym names), do
not author a join doc — record it in the pair overview's caveats as a verified
NON-join, with the query that refutes it. That trap is exactly what a consumer
would fall into. (This applies only to candidates that were plausible enough
to verify — a pair that failed the Phase-1 gate gets no docs at all, so there
is no overview to carry caveats.)

## What to author, and where

Cross-dataset docs mirror the normal fact-typed reference conventions inside
the pair's folder (your runtime names the exact path; conventionally
`external/<counterpart_domain>/<counterpart_dataset>/`):

- `overview.md` — REQUIRED whenever ANY doc is authored, and written last. The
  relationship map: which entities the two datasets share (the Phase-1
  convergences, each with its consumer question), every verified join path
  with its measured cardinality and overlap, usage guidance (when to reach
  across; which side owns which grain and facts; inner vs left join advice
  from the orphan analysis), and the caveats — refuted candidates, format
  normalizations, freshness asymmetries between the sides. Link the pair's
  other docs.
- `joins/<a>__<b>.md` — one per VERIFIED join path, named by the two tables.
  The measured cardinality/overlap/orphan facts, the exact working SQL
  (with any normalization baked in), and when to use it. LINK the table docs
  on BOTH sides (see the framing rule: home form + counterpart address form)
  so the join is reachable from the home table via backlinks and the
  counterpart is one address away.
- `metrics/<name>.md` — one per cross-dataset metric, owning its verified SQL
  and linking the docs it draws on (both sides, same two forms).
- The other canonical folders (`enums/`, `named_sets/`, `known_issues/`) when a
  verified fact of that type genuinely spans the pair — e.g. a shared code
  vocabulary, or a data-quality issue visible only in the join. Never invent
  new folder names.

Right-size the set: a pair with one real join needs one join doc and a short
overview, not a doc per lens — and a pair with NO real convergence needs no
docs at all. Every doc carries a `# Citations` section naming both tables'
resources (and any published doc you mined).

## Quality bar (in addition to SKILL.md's checklist)

- Every authored doc traces back to a NAMED business convergence with a
  consumer question — no doc exists because two columns merely happened to
  match. If the pair has no genuine convergence, NOTHING was authored.
- Every join/metric doc's SQL was RUN, cross-database, and returned sane rows.
- Every stated cardinality/overlap was measured this run — never copied from
  either side's existing docs without re-running.
- SQL tested specific hypotheses; no fishing queries over arbitrary column
  pairs.
- Prose is symmetric (reads correctly from both audiences' perspective) and
  every table reference is fully qualified.
- Every join/metric doc LINKS the table docs it involves on BOTH sides — the
  home form (stitches the pair subtree into the link graph) and the
  counterpart address form (may dangle later; accepted). Home-side links must
  resolve TODAY (no dangling targets within this bundle).
- The overview names the refuted candidates a consumer would plausibly try.
