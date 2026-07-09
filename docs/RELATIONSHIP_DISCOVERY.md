# Cross-Dataset Relationship Discovery — Design Note

> Status: **design, not implemented.** This is the artifact to align on before any
> code. It describes a new subsystem that discovers *business* connections between
> **different** datasets and documents them as OKF external-connection references.
> Section numbers (§) are internal to this doc.

## §1 Problem

The harvester authors **one dataset at a time** (single Glue database per
invocation, scoped IAM — see threats #9/#60). It documents relationships *within*
a dataset (`references/joins/*`) but is structurally blind to relationships
*across* datasets. With ~1000 datasets, the naive answer — "run the harvester over
every pair and ask the LLM" — is ~500k expensive LLM+SQL calls (O(N²)). It is also
answering the wrong question.

The goal is **not** "which columns can technically join" (mechanical plumbing).
It is: **which datasets have a business connection that would enable a valuable
new data product** — e.g. `support_tickets` + `product_returns` + `order_history`
→ a churn/CX product. That is a *semantic/business* question first; technical
joinability is a downstream **feasibility** check, not the discovery mechanism.

Two hard realities shape the design:
1. **Enterprise column names are unreliable** (`PARTY_NBR`, `col_00123`), so
   column-name/value matching cannot be the discovery spine.
2. **Business convergence is often cross-domain** (shared *entity*, e.g. the
   customer, not shared *topic*), so topical similarity alone misses the most
   valuable pairings.

## §2 Approach: HyDE-style demand-driven discovery

Instead of matching dataset A's description against every B's description, we
generate **what an ideal complement to A would look like for a specific business
problem**, then semantic-search the corpus for real datasets matching that wish.
(This is Hypothetical Document Embeddings / query2doc, applied to dataset
recommendation.) One side of the match is now rich, idealized business text, which
bridges the vocabulary gap that raw A↔B matching cannot.

Discovery is **demand-driven, not autonomous**: it runs against a curated set of
business problems, not LLM-invented ones. This is the deliberate choice that keeps
precision up (LLM-invented problems produce generic "improve retention" mush that
matches everything).

## §3 End-to-end journey

```
                 curated problems (DB, human-authored, ≤1 paragraph each)
                              │
  per dataset A ┌────────────▼─────────────┐
                │ 1. LLM problem filter      │  which problems could A (+ ideal
                │    (recall gate, logged)   │  complements) help solve?
                └────────────┬───────────────┘
                             │  for each surviving problem P:
                ┌────────────▼───────────────┐
                │ 2. Generate wish-doc(A,P)  │  "the ideal complementary dataset(s)
                │    stored in the manifest  │  to solve P alongside A looks like…"
                └────────────┬───────────────┘
                             │
                ┌────────────▼───────────────┐
                │ 3. Semantic search corpus  │  wish-doc → existing vector index;
                │    (exclude A's own subtree)│  rank real datasets B, C, …
                └────────────┬───────────────┘
                             │  candidate = (A, {B,…}, P, score)
              ┌──────────────┴───────────────┐
   score in AUTO band                score in GATED band
              │                              │
   ┌──────────▼──────────┐        ┌──────────▼──────────┐
   │ 4a. Harvester        │        │ 4b. Queue for human │
   │  feasibility verify   │        │  review (show wish, │
   │  (multi-DB, scoped)   │        │  score, problem)    │
   └──────────┬───────────┘        └──────────┬──────────┘
     feasible?│                     approved → same verify path (4a)
       ┌──────┴───────┐
      yes             no
       │               │
┌──────▼──────┐  ┌─────▼─────────────────────┐
│ 5. Author    │  │ 6. Record NON-CONVERGABLE │
│  external     │  │  (keyed on dataset set +  │
│  connection   │  │  schema versions; expires │
│  refs (§5)    │  │  on re-harvest)           │
└───────────────┘  └───────────────────────────┘
```

### Step 1 — LLM problem filter (recall gate)
For dataset A, ask the LLM which curated problems A could contribute to (with ideal
complements). **Bias to recall** — a problem wrongly rejected here is invisible
forever. Store the accept/reject reasoning for audit.

### Step 2 — Wish-doc generation
For each `(A, problem)` surviving the filter, generate a rich description of the
**ideal complementary dataset(s)** for that problem. **One wish-doc per
`(dataset, problem)`** — a dataset solving 3 problems has 3 wish-docs. Store them
in the dataset's manifest, **version-keyed** to A's schema hash + the problem
paragraph's hash (see §6 staleness).

### Step 3 — Semantic search
Embed the wish-doc, query the existing global vector index (§4), excluding A's own
subtree. Returns ranked real datasets. **No new index needed** — this reuses the
`semantic_search` path over `type ∈ {Glue Table, Glue Database}` concepts.

### Step 4 — Threshold band → verify or gate
The similarity score routes the candidate into an **auto-verify band** or a
**human-gated band**. This threshold is the single riskiest number in the design —
see §7, it is NOT a fixed constant.

### Step 5 — Feasibility verification (harvester, "relationship mode")
For a candidate tuple, the harvester runs — with a session policy scoped to
**exactly the involved datasets' Glue DBs** (§8) — the confirming checks: *is there
a real convergence point* (shared key, or a resolvable shared entity like
customer/email)? Feasible → author (§5-docs). Not feasible → negative-cache (§6).

### Step 6 — Documentation / negative tracking
Feasible relationships become OKF external-connection docs (§5). Infeasible ones
are recorded as non-convergable so they are never re-verified — until a schema
change expires the verdict (§6).

## §4 Discovery signal — what we embed

Communities/clustering are **NOT** the spine (an earlier design iteration
over-weighted them). Semantic clustering of datasets buckets by *topic*, but the
most valuable connections are cross-topic (shared entity). The HyDE wish-doc match
(§2) is the spine. The existing index already embeds frontmatter (`title`, `type`,
`description`, `tags`) + the `# Overview` prose (`okf_core/embedding.py`
`build_embed_text`) — adequate for wish-doc matching. Dataset-level grouping, if
built, is a **secondary artifact** (a "these datasets form a domain" concept), not
a gate — it must never filter candidates, or it drops cross-domain matches.

Optional strengthening (deferred): a **shared-grain-entity** signal. The harvester
already verifies each table's grain ("one row per X" — X is the business entity).
Datasets exposing the same grain-entity have a business convergence point by
definition, and this catches cross-topic sharing the topical embedding misses. Not
required for v1; noted because the signal is already produced.

## §5 Where relationship docs live — DECISION

An external relationship is **documented in every involved dataset** as an
external connection, e.g. `okf/<domain>/<dataset>/references/external/<other>.md`.
Chosen over a top-level `relationships/` prefix because it stays consumable through
the **existing** tools (a consumer of `sales/orders` finds its connections via
normal `list_directory`/`read_page` in that subtree — no cross-prefix
`get_backlinks` needed to discover them).

This trades a link-resolution problem for a **distributed-consistency** problem
(N copies of one relationship). Enforce it with a single source of truth:

- **Canonical relationship record** lives in the registry / derived DB, keyed on
  the **unordered set** of involved datasets. The per-dataset `references/external/*`
  docs are **rendered projections** of that record, not independent authorings.
- **Atomic multi-write:** rendering must write all N per-dataset docs as one unit
  (all-or-nothing); a partial write leaves A claiming a connection B doesn't
  acknowledge. Writes go through the per-dataset S3 Files mount, so this needs an
  explicit commit/rollback, not a single PutObject.
- **Symmetric dedup:** `A↔B` discovered from A's wish and from B's wish are the
  same relationship — keyed on the unordered set, verified/authored once.
- **Staleness cascade:** deleting or re-harvesting B must prune A's copy pointing
  at B. `delete_domain_mapping` already cascades within a dataset; external
  connections in *other* datasets are a NEW cross-dataset cleanup it must reach.

## §6 Staleness & negative cache — DECISION

Both wish-docs and non-convergable verdicts are **snapshots of current schemas**;
without versioning, re-harvests silently serve stale wishes and permanently-wrong
negatives.

- **Wish-docs** are keyed on `(dataset schema hash, problem paragraph hash)`.
  Re-harvest of A or an edit to the problem invalidates and regenerates them.
- **Non-convergable verdicts** are stored as `(unordered dataset set, schema
  versions, verdict, reason)` — NOT a permanent tombstone. If either dataset's
  schema version changes, the negative expires and the pair is re-eligible (a
  relationship that *became* possible must not be missed forever).

## §7 The threshold — the riskiest decision, NOT a magic constant

Routing a candidate to auto-verify vs. human-gate on a hardcoded cosine cutoff
(0.90, 0.80, …) is the design's biggest trap. Why a fixed constant is wrong:

- **Cosine similarity is not calibrated probability.** A 0.80 score is not "80%
  confidence"; Titan V2 distributions are corpus-dependent.
- **Wish-doc ↔ real-doc is a cross-distribution match** (rich idealized text vs.
  terse real description); its similarity typically runs **lower** than
  doc-to-doc, so a doc-to-doc intuition for "high" is miscalibrated here.
- **Lowering the cutoff widens the AUTO band** — the *expensive* path (harvester +
  multi-DB SQL) and the one that can auto-author a wrong relationship into N
  datasets. The auto band should be **conservative (precise)**; the human-gated
  band is where recall lives. Lower ≠ safer.

**What to do instead:**
1. **Measure first.** Run existing wish-docs against the corpus for a handful of
   known-good and known-unrelated pairs; look at the score distribution and find
   the knee. There may be no clean separation — that itself is a finding.
2. **Set the auto band ABOVE the knee** (conservative), human-gate the plausible
   tail down to the knee.
3. **Hard-cap auto-verify with top-K per `(dataset, problem)`**, not "everything
   above X" — this bounds harvester spend regardless of the score distribution
   (protects the cost model; see §9).

`0.80` is acceptable only as a **starting point for measurement**, never as the
shipped rule. The band is tunable config, widened as precision data accumulates.

## §8 Security — multi-DB feasibility verification (threats #9/#60)

Feasibility verification (§5-step) needs to read **all involved datasets at once**
— which reopens the blast radius the per-invocation session-policy scoping was
built to shrink. Rules:

- A **dedicated relationship-verification invocation** whose session policy is
  scoped to **exactly the Glue DBs in the candidate tuple** — never `*`, never
  catalog-wide. The `dataset(A) != dataset(B)` construction guarantees the set is
  small and explicit.
- This is a **deliberate, bounded widening** distinct from the tightly-scoped
  single-dataset harvest data role. It gets its **own threat-model entry**.
- If the catalog uses Lake Formation, the verification role needs LF grants on the
  tuple's tables too (see `docs/LAKE_FORMATION.md`).

## §9 Cost model & why it holds

The pipeline is O(N) in datasets (per-dataset: LLM filter + wish-gen + vector
search), and the expensive O(survivors) work (harvester + multi-DB SQL) is bounded
by the **top-K auto-verify cap** (§7). The negative cache (§6) prevents re-paying
for known-dead pairs. The cost model is **precision-conditional**: if the auto band
is mis-tuned wide, survivors balloon and the O(survivors) stage becomes the O(N²)
LLM problem this design avoids — which is exactly why §7's cap is load-bearing.

## §10 Scope for v1 (explicit non-goals)

- **Single-hop, pairwise/tuple relationships only.** Multi-hop transitive chains
  (`orders → order_items → products`) are out of scope; state the gap rather than
  discover it silently.
- **Multi-dataset products (>2):** semantic search returns datasets individually.
  v1 verifies **pairwise**; assembling "orders + support + returns" into one
  product hypothesis is deferred. (The problem paragraph may *describe* a 3-way
  product; v1 records the pairwise edges that compose it.)
- **Recall is unmeasurable.** There is no ground truth for "all real relationships
  among 1000 datasets," so a clean *precision* number does NOT imply completeness.
  Ship a precision harness (sample survivors, adjudicate) and state plainly that
  recall is unmeasured — do not let a good precision figure read as "we found them
  all."

## §11 Architecture placement

This is a **new subsystem**, not a harvester mode toggle: asynchronous,
corpus-level, index-derived. It belongs beside `reindex`/`incremental` (scheduled
or event-driven over the derived layer), feeding a **new harvester relationship-
verification mode** (§8). Like the vector index, its outputs are **derived and
rebuildable** from the source-of-truth bundles.

## §12 Open decisions (resolve before building)

| # | Decision | Recommendation |
|---|----------|----------------|
| 1 | Auto-verify threshold | **Measure distribution first**; conservative band above the knee + top-K cap. `0.80` = measurement start only. |
| 2 | Dataset embedding for any grouping | centroid of table vectors (free) vs. embed dataset-overview doc (cleaner). Default centroid; note the blur on multi-purpose datasets. |
| 3 | Add shared-grain-entity signal? | Deferred; the grain is already harvested, so cheap to add later if topical recall proves weak. |
| 4 | Canonical record store | registry table vs. dedicated derived DB. Must be keyed on the unordered dataset set. |
| 5 | >2-dataset products | pairwise edges for v1; revisit set-level assembly. |

## §13 Adversarial-review findings folded in

This design is the survivor of several review passes. Rejected/adjusted ideas,
kept here so they are not re-proposed:

- **Column value-containment as the discovery spine** — rejected: enterprise
  column names/values are unreliable, and containment finds *mechanical*
  joinability, not *business* value. Demoted to the feasibility check (§5-step).
- **Jaccard/MinHash for FK detection** — rejected: Jaccard misses the common
  small-fact ⊆ large-dimension join; containment (asymmetric) is the correct
  measure *if* value matching is used at all (it's now feasibility-only).
- **Semantic communities as the O(N²)-killer / discovery spine** — rejected:
  clusters by topic, drops cross-domain (shared-entity) connections. Demoted to an
  optional secondary artifact that must never gate candidates (§4).
- **LLM-invented business problems** — rejected: generic mush, low precision.
  Replaced by the human-curated problem DB (§3-step-1).
- **Fixed cosine threshold (0.90/0.80)** — rejected as a shipped rule: un-calibrated
  metric, asymmetric match, lower≠safer. Replaced by measure-then-band + top-K (§7).
```
