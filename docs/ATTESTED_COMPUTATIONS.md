# Attested Computations — design

**Status: IMPLEMENTED** (branch `feat/attested-computations`, phases 1–5 of
§11; the VW fork port is the remaining step). Pure rules:
`okf_core/computations.py`; shared S3/engine runner:
`okf_aws/computation_run.py`; fold-in: `harvest/verification.py` (runs inside
`finalize_bundle`); contract summary: CONVENTIONS.md "Attested Computations".

## 1. Why

Ask the chat agent "how did we do last month compared to previous years?" today
and it re-derives the answer from scratch: read the wiki, find the tables, work
out the join, write the SQL, sanity-check it — minutes of latency, real token
cost, and (worst) *answer variance*: the same question on Tuesday and Friday can
route through different SQL. The knowledge that made Tuesday's answer right was
thrown away.

An **Attested Computation** is that reasoning, cached: a canonical, parameterized,
read-only SQL statement authored once during harvest, verified live at authoring
time, **human-verifiable** thereafter, and executed by only filling typed
parameter holes. The agent's job collapses from *derive the answer* to *find the
computation, pass the parameters* — deterministic, fast, and identical across
sessions.

Correctness assurance lives at two human-scale moments: the author's live
verification during harvest (grounded in the snapshot evidence sheets, reproduced
by the adversarial reviewer) and the human's verification click (signed over the
computation's content hash).

A computation answers the question family its author anticipated. Ad-hoc
slicing (arbitrary new dimensions/filters) remains the agent-plus-`run_sql`-
plus-wiki tier — and when the agent keeps hand-deriving the same shape, that is
the signal to promote it into a computation (an annotation: "make this a
canonical metric" is the promotion path).

## 2. Spec alignment — what we take from OKF v0.2, and what we deliberately don't

The upstream spec (`GoogleCloudPlatform/knowledge-catalog`, `okf/SPEC.md`) added
**Attested Computations** in v0.2: "a `type: Attested Computation` concept carries
a sanctioned computation (`runtime`, `parameters`, `executor`, `attester`) so a
consumer can confirm a value was produced the blessed way, not improvised."

**Adopted (Google convention):**

| v0.2 element | Our use |
|---|---|
| `type: Attested Computation` | verbatim — consumers route on it |
| `runtime` (required) | `athena` \| `redshift` — selects the executor |
| `parameters: [{name, type, required}]` | verbatim shape, plus our extensions (§5) |
| `# Computation` body fence | the frozen SQL with `@param` holes |
| computation immutability | agents may only supply parameter values, never edit the sanctioned SQL |
| `verified` (human trust signal) | flattened to `verified` / `verified_by` / `verified_sha256` (§4) |
| receipt fields (`job_id`, `executed_sql`, `result`) | the run response carries the engine query id, the exact executed SQL, and the content hash |
| "SHOULD surface, not silently drop, a failing attestation" | violations are errors in the tool result, never swallowed |

**Deliberately NOT ported:**

* The remaining v0.2 provenance/lifecycle families (`sources`, `generated`,
  multi-entry `verified` lists, `status`, `stale_after`). We carry the single
  flattened verification triple; adopting the full families is future interop
  work, not v1.
* Per-doc `executor:` / `attester:` resources. In v0.2 the doc names the code
  that runs and checks it; here the platform IS the executor (Control API /
  consumption MCP runtime) and the attester is built in (hash-compare +
  typed-parameter substitution). Docs stay declarative.
* The `computation:` file-path variant — the SQL lives in the doc's fence,
  keeping the bundle self-contained.

## 3. The concept doc

Computations are their own fact type with their own folder:
**`references/computations/<slug>.md`** — one doc per computation. The doc is a
full concept doc, not a bare SQL container: the prose defines what it computes
in business terms and **links the concepts it stands on** — the table docs it
reads, the join docs whose relationships it exercises, the enum/named-set
references whose codes appear in its filters, the glossary terms it implements.
Those links make computations first-class citizens of the link graph: backlinks
answer "which computations read this table" (impact analysis when a table
changes), the graph view shows them, and `semantic_search` finds them by
business phrasing like any other doc.

```markdown
---
type: Attested Computation
title: Revenue, month vs same month in prior years
description: Recognized revenue for a month, alongside the same month 1-3 years back.
runtime: athena
parameters:
  - {name: month, type: date, required: true, example: "2026-07-01"}
verified: null
verified_by: null
tags: [finance]
timestamp: 2026-08-13T00:00:00Z
---

Recognized revenue (see [gross revenue](../glossary/gross_revenue.md)) for the
requested month and the same month one, two, and three years back. Reads
[order_items](../../tables/order_items.md) joined to
[orders](../../tables/orders.md) (see the
[join doc](../joins/order_items__orders.md)); excludes cancelled orders per the
[order status codes](../enums/order_status.md).

# Computation

```sql
WITH monthly AS (
  SELECT date_trunc('month', o.order_date) AS m,
         SUM(oi.quantity * oi.unit_price)  AS revenue
  FROM order_items oi
  JOIN orders o ON oi.order_id = o.order_id
  WHERE o.status <> 'cancelled'
  GROUP BY 1
)
SELECT m, revenue
FROM monthly
WHERE m IN (@month,
            @month - INTERVAL '1' YEAR,
            @month - INTERVAL '2' YEAR,
            @month - INTERVAL '3' YEAR)
ORDER BY m DESC
```

# Citations
...
```

Grammar: anything the engine accepts as **one read-only SELECT/WITH statement**.
Windows, CTEs, self-joins, `GROUPING SETS` — all legal. Excluded mechanically:
multiple statements, DDL/DML, and anything past the runtime caps.

**Holes are scalar values, never structure.** `@month`, `@region`, `@threshold` —
yes. "Group by region OR month, caller's choice" — no: that is two computations.
A parameter that could inject SQL fragments would make the executed statement
unpinnable, and attestation stops meaning anything. This rule is also what keeps
the injection guard airtight (§6).

The existing `references/metrics/` docs are untouched by this feature: a metric
reference remains the prose home of a metric's definition and canonical SQL. A
metric worth executing on demand gets a computation doc that links back to it
(and vice versa).

## 4. The trust model: `verified` / `verified_by`

Human verification is three frontmatter fields — `null` until a human acts:

```yaml
verified: 2026-08-14T09:30:00Z          # when the human verified it
verified_by: analyst@example.com        # the Cognito identity that clicked Verify
verified_sha256: <content hash>         # WHAT they verified — the binding
```

**The content hash** is `sha256` over the exact bytes of: the `# Computation`
fence text (trailing-whitespace-stripped lines, `\n` joined), the canonical JSON
of `parameters`, and the `runtime` string. Verification signs *this computation
with these parameter contracts on this engine* — nothing else.

**The invariant, enforced at three points (all reusing existing machinery):**

1. **Agents can never verify.** The OKF guard refuses any agent write that sets
   `verified` / `verified_by` / `verified_sha256` to anything but `null` — same
   enforcement pattern as the augmentation guard. Only the Control API sets
   them, on a human click. Without this single rule the fields are theater.
2. **Verified computations are FROZEN in the IN-PLACE modes.** For an
   incremental, annotation, or cross run the runner resolves the verified set
   at run start (folded stamps ∪ overlay, hash-checked —
   `harvest/verification.frozen_computation_paths`) and the guard refuses
   every agent write/edit/delete to those docs, on every writing sub-agent.
   The only unlock is a human Unverify. A **full harvest is deliberately
   exempt**: it is the destructive mode, the wipe is unconditional (there is
   no keep-list), and the agent re-authors computations from source — so a
   re-authored one returns to the human's verify queue rather than inheriting
   a stamp that attested different content. (Freezing there would also
   deadlock: a wiped doc whose path is frozen could never be re-authored.)
   The hash-mismatch `stale` state remains as the race backstop (a Verify
   click landing mid-run signs whatever the doc was at click time; if the
   doc then changed, serving says `stale`, never silently verified).
3. **Lint keeps it runnable — and routes frozen breakage to humans.** Every
   computation is EXPLAIN-checked at lint time with its parameters' `example`
   values substituted, so schema drift breaks loudly at the next harvest, not
   silently at runtime. On a FROZEN doc those findings downgrade to warnings
   carrying "a human must unverify before a harvest can repair it" — real,
   surfaced, but never wedging the agent's fix-to-zero gate on a doc it is
   forbidden to touch.

**What verification means:** a named human read the prose, the SQL, and the
parameter contracts, and attests the computation encodes the intended business
logic. **What it does not mean:** freshness (data changes under any query) or
authorization (§5). Unverified computations still run — the run response and the
UI badge them `unverified` / `stale` so the consumer can weigh them; refusing to
run them would push agents back to hand-derived SQL, which is strictly less
safe.

**Verification writes NEVER touch the bundle tree directly.** The S3 Files
mount maps a path materialized by a raw Lambda ``put_object`` as ROOT-owned,
which the runtime's uid-1000 mount identity then cannot write — the EACCES that
once wedged harvests when the incremental path staged ``pending.json`` that way
(see ``incremental/handler.py``: "the mount is the sole writer of the bundle
tree"). A verification PUT onto the doc would re-create exactly that trap: the
first human Verify click would make the doc un-editable for every later run.
So verification flips are a two-step:

1. **The Verify click writes an off-mount overlay** —
   ``verification/<domain>/<dataset>.json`` (same posture as the ``benchmark/``
   artifacts: Lambda-written, outside the mounted bundle prefix). One entry per
   computation: ``{slug, sha256, verified, verified_by}``. ``unverify`` writes a
   ``revoked`` **tombstone** rather than deleting the entry — the doc may
   already carry a folded-in stamp from an earlier run, and with the entry
   merely absent, serving would fall back to the doc's frontmatter and
   resurrect the verification. No lease interaction needed — overlay writes are
   safe mid-harvest. No conditional PUT needed either: if the doc changed
   between the Verify screen loading and the click, the signed hash simply no
   longer matches and the verification reads as ``stale`` — the binding
   self-corrects.
2. **The runtime folds the overlay into frontmatter through the mount** at the
   END of its next run — after all authoring, inside ``finalize_bundle``,
   before the commit marker (platform code, not the agent; end-of-run rather
   than start-of-run because a full harvest wipes authored output at start, so
   a stamp folded-and-removed before the wipe would be lost minutes later).
   Entries whose hash still matches are written into the doc's three fields
   via the same canonical frontmatter serializer the guard uses and removed
   from the overlay; a hash-mismatched entry is KEPT (serving keeps saying
   ``stale`` until a human re-verifies or unverifies); a ``revoked`` tombstone
   nulls the doc's triple and is dropped; an entry whose doc is gone is
   dropped. The bundle therefore still ends up CARRYING its verification
   (portable, spec-shaped) — eventually, at the next run, rather than
   instantly.

**Serving merges both sources:** ``list``/``describe``/the receipt/the UI badge
read the doc frontmatter AND the overlay; the overlay wins on disagreement, and
a hash mismatch on either side surfaces as ``stale``. The consumption MCP
already reads S3 — no new dependency. Audit: the overlay object and the doc
both live in the versioned bucket, so every flip is an object version;
who/when rides the entry itself.

**Lifecycle.** Unverified computations are the draft tier: agents create,
edit, and retire them freely. A human Verify makes one an immutable artifact
for every in-place run; changing it is explicitly human-gated — Unverify in
the UI (overlay tombstone) → the next run may edit or retire the doc → a
human re-verifies the new content.

**A full re-harvest resets verification**, by design: requesting one IS the
decision to rebuild the wiki from source, so the wipe takes verified
computations too and the re-authored docs need a fresh human review. Two
mitigations keep that cheap rather than safe-critical: (a) the supervisor
prompt and the skill tell authors to reproduce a still-supported fence and
its parameters VERBATIM — an identical statement hashes identically, so an
overlay click that has not yet been folded in still matches and the stamp
survives; (b) the Verify dialog shows the statement and hash, so re-verifying
unchanged substance is a short read.

## 5. Parameters: contract vs evidence

```yaml
parameters:
  - {name: month,      type: date,    required: true,  example: "2026-07-01", min: "2019-01-01"}
  - {name: region,     type: string,  required: false, default: "EMEA",
     enum: [EMEA, NA, APAC], column: customers.region}
  - {name: min_orders, type: integer, required: false, default: 1, min: 1, max: 10000}
```

Types: `string | integer | number | date | timestamp | boolean`. `example` is
required (lint's EXPLAIN needs it; `describe` shows it). `column` optionally
names the snapshot column a hole filters — it feeds the advisory layer and the
UI type-ahead.

Validation is layered, split along one line: **declared constraints are contract
(refuse); profiled evidence is advisory (warn and run).**

1. **Type** — always; a malformed value is a corrective tool error.
2. **Declared constraints** (`enum`, `min`/`max`) — hard-refuse. They are inside
   the content hash: the human who verified the computation verified these
   bounds. An agent cannot quietly widen an enum post-verification — that edit
   changes the hash and voids the stamp. Because the *author* is an LLM, lint
   validates declared enums against the profile evidence (`domains.json`)
   before any human ever sees them.
3. **Profiled domains** — when a parameter binds a column, the executor checks
   the supplied value against the observed value list and **warns but runs** on
   a miss (the profile is a snapshot-time scan; data legitimately evolves past
   it). A domain counts as exhaustive only when the scan proved it
   (`groups < topk`), never by inference.
4. **Zero-row hint** — an empty result plus an unverified parameter value
   attaches the typo hint ("`'EMAE'` is not among the 4 observed values for
   `customers.region`"), converting the most common failure into a one-round
   self-correction.

Boundary: this is **validity**, not **authorization**. An enum says "this value
is meaningful," never "this caller may see this region." Access control stays at
the dataset level (Cognito/IAM); parameter constraints must never be leaned on as
row-level security.

## 6. Execution and attestation

One executor per `runtime`, behind one tool/endpoint surface:

* **Consumption MCP:** `list_computations`, `describe_computation`,
  `run_computation(name, parameters)` — gated exactly like the rest of the
  source-data surface.
* **Chat:** same three; `run_computation` is ALWAYS bound — the sanctioned
  path must not cost the raw-SQL opt-in (that gate keeps guarding ad-hoc
  `run_sql` only). Execution capability follows the chat SQL deploy flag
  (clients + IAM); without it the receipt returns the rendered SQL
  un-executed. The policy checker, when armed, judges the post-substitution
  SQL. Proposing a NEW computation is a first-class `submit_annotation` use
  (the promotion path §8).
* **Control API / UI:** `GET .../computations[/{slug}]`,
  `POST .../computations/{slug}/run`, `POST .../{slug}/verify|unverify`.
  The Browse view lists computations with verification badges, and every
  computation is **runnable from the UI**: clicking it opens a Run modal that
  renders one input per parameter (typed, with the declared default pre-filled,
  type-ahead suggestions from `domains.json` for bound columns — free text
  always legal), executes on demand, and shows the result rows alongside the
  exact executed SQL and the receipt. The Verify screen shows prose + SQL +
  parameter contracts + the diff since the last verified hash.

The executor — not the model — does all of: parameter presence/type/constraint
validation, dialect-correct literal rendering, substitution into the frozen SQL
at the `@name` sites, read-only execution under the grader-grade caps
(timeout/row caps, same env knobs), and receipt assembly. The agent never splices
strings; that is what makes the attestation meaningful.

**The receipt** (returned on every run, v0.2-shaped):

```json
{
  "rows": [...], "row_count": 42,
  "executed_sql": "...",            // post-substitution, verbatim
  "computation_sha256": "...",
  "verification": "verified" | "unverified" | "stale",
  "verified_by": "analyst@example.com",
  "engine_query_id": "<Athena QueryExecutionId / Redshift statement id>",
  "warnings": ["value 'EMAE' not among observed values for customers.region", ...]
}
```

Attestation = the platform guarantees `executed_sql` is the sanctioned fence with
only typed, validated values in the holes, and proves which fence via the hash.
Per v0.2, attestation is per-run and lives in the response, never in the bundle.

## 7. Guards

Small, mechanical, and collectively the whole trust story:

| Guard | Where | Why it is load-bearing |
|---|---|---|
| verification-field refusal for agents | OKF guard (write path) | without it, `verified` is theater |
| hash binding | loader/serving | makes "edits void verification" real |
| typed-parameter rendering | executor | the injection guard; attestation is meaningless if values can splice SQL |
| write-time shape check | OKF guard | valid block, ONE SELECT/WITH (literal-masked check), no DDL/DML, every `@hole` declared and every declared parameter used |
| EXPLAIN with examples | lint (steps 6a + 8) | schema drift breaks at harvest, not at runtime |
| perimeter | infra + chat | read-only, caps, `enable_attested_computations` (default **false** — machine-vended MCP creds must not gain source-data read from a routine apply), chat execution behind the chat-SQL deploy grants + policy checker (the tool itself is always bound) |

Two agent-facing surfaces exist — writing the docs and supplying runtime values —
and each keeps exactly the guards that police it. Wrong-answer prevention lives
in authoring-time verification plus the human stamp; abuse prevention lives in
the table above.

## 8. Authoring (harvest + skill)

The **okf-authoring skill** gains the Attested Computation concept (this is a
vendored-skill change, shipped with the harvest image):

* `references/fact-types.md`: a new fact type — *a recurring, verified,
  parameterizable question answered by ONE canonical read-only statement* —
  routed to `references/computations/<slug>.md`, `type: Attested Computation`.
* `references/templates.md`: the template from §3, plus the rules: one canonical
  statement, not a menu; scalar holes only; qualify columns; every parameter
  needs `example` (and `column` when it filters one); constraints must come from
  evidence (profile domains, `.context/` policy), never invention; the prose
  must LINK the tables, joins, enums, and glossary terms the computation stands
  on (it is a concept doc, not a SQL container); **never set `verified` /
  `verified_by`** (the guard refuses it; verification is a human act); run the
  exact statement live (with example values) before writing.
* `SKILL.md`: the routing sentence + the promotion rule — when `.context/` names
  a KPI, or the same question shape recurs in provided context, author it as a
  computation.

Harvest wiring: reference-authors create computations in the same fan-out that
authors reference docs today, grounded in the evidence sheets (grain verdicts,
join cardinality, profile domains); the adversarial reviewer's cluster checks
extend to reproducing each computation with its example values against the
stated prose. Scoped/annotation runs may add or update computations (an
annotation "make X a canonical metric" is the promotion path) — with edits
voiding verification per §4.

## 9. Non-goals (v1)

* No structural parameters (SQL-fragment holes) — ever, by design.
* No ad-hoc recomposition tier: a computation's shape is frozen; new question
  shapes are new computations or the agent's `run_sql` tier.
* No multi-statement computations, no writeback, no scheduling/materialization.
* No cross-dataset computations (the doc, evidence, and engine scope are one
  bundle's); revisit with the cross-reference machinery.
* No row-level authorization via parameter constraints.
* No adoption of the remaining v0.2 families (`sources`/`generated`/multi-entry
  `verified` lists/`status`/`stale_after`).

## 10. Code reuse inventory

Existing code reused (cherry-picked from the repo's history where it already
exists; never developed against the old branch):

| Piece | Source | Fate |
|---|---|---|
| Athena query executor | `okf_aws/athena_query.py` (feat/metrics) | reuse nearly verbatim |
| Redshift executor (Data API) | `okf_aws/redshift_query.py` (feat/metrics) | reuse nearly verbatim |
| dialect literal rendering | `_quote_text`/`_render_literal` (feat/metrics) | extract into the parameter renderer (keep the redshift backslash-escaping behavior) |
| enum domains profile pass | `harvest/profile.py` → `profile/domains.json` (feat/metrics) | reuse verbatim (cache-carried, exhaustive-proof rule) |
| UI value type-ahead | `ValueInput` (feat/metrics `MetricRunner.jsx`) | reuse in the Run modal's parameter form |
| Run modal / Browse integration / api.js / chat tool cards | feat/metrics UI files | reuse skeletons, re-shape for parameters |
| Control API endpoint scaffolding + tests | feat/metrics handlers/app/tests | reuse shapes (add verify/unverify) |
| IAM gating | feat/metrics `infra/compute` blocks | reuse, renamed `enable_attested_computations`, default false |
| guard/lint hook points | feat/metrics guard/lint additions | reuse the hook shape; checks per §7 |

No `.harvest/` precompute in v1: `list_computations` lists the
`references/computations/` prefix and parses frontmatter (bundles are modest);
add a catalog precompute only if listing cost ever shows up.

## 11. Delivery plan

Fresh branch from current `main` (after the in-flight session work is committed —
the working tree must be clean first). Phases, each independently testable:

1. **Core + guards:** doc parsing/hash (`okf_core`), guard checks (shape,
   verification-field refusal), lint step (EXPLAIN w/ examples, enum-vs-evidence),
   parameter contract validation. Pure offline tests.
2. **Executor + tools:** parameter renderer, Athena/Redshift executors,
   receipt; consumption MCP + chat tools with gating. Fake-engine tests.
3. **Control API + UI:** list/get/run/verify/unverify (lease-aware verify),
   Browse list + Run modal + Verify screen.
4. **Authoring:** skill changes (§8), harvest wiring, reviewer extension;
   E2E offline harvest test asserting a valid computation doc.
5. **Infra + docs:** IAM var, CONVENTIONS.md contract section, CLAUDE.md
   pointer; then the VW port (fork adaptations: multi-DB source resolution,
   db-qualified snapshot layout, Anthropic-pinned models, no Redshift → single
   runtime).

The local gate per phase is the standard one: `./scripts/run_tests.sh`, UI build,
`terraform validate`.
