---
name: analysis-authoring
description: >-
  How to author an analysis worth reusing: document shape, question design,
  the computation-first sourcing ladder, and step discipline. Read BEFORE
  your first create_analysis or update_analysis of a conversation.
---
# Analysis authoring

An analysis is a saved procedure another conversation will follow without you
in it: a later agent reads the document cold, asks the user only the
questions it declares, executes its steps against this dataset, and delivers
what its Finalize section demands. Write for that agent — every step must be
executable from the document plus the wiki alone.

## Scope: atomic, or it isn't an analysis

One analysis answers ONE recurring business question ("where is churn
concentrated?", "did the campaign pay for itself?"). If the draft needs an
"and also" in its description, split it. Fixed choices belong in the steps,
not in questions; a procedure that asks about everything is a questionnaire,
not an analysis.

## Document shape

```markdown
---
title: Churn cohort deep-dive
description: Quantifies churn by signup cohort and isolates which segments drive it.
questions:
  - id: time_window
    prompt: Which period should the analysis cover?
    kind: single
    options: ["Last quarter", "Last 12 months", "All history"]
  - id: segment
    prompt: Restrict to a customer segment, or run across all?
    kind: text
---
## Purpose
One short paragraph: the question this answers and for whom.

## Grounding
- `tables/customers.md` — grain, the `signup_date` column the cohorts key on
- `joins/customers-orders.md` — the validated join this analysis rides
- computation `churn_by_cohort` — the frozen aggregation steps 2–3 run

## Steps
1. ...numbered, imperative, each self-contained...

## Finalize
What the delivered report must contain.
```

- `title` is the human name; `description` is one or two lines the model uses
  to pick this analysis from a list — say what question it answers, not how.
- The body is markdown with exactly those four sections. Purpose orients;
  Grounding anchors; Steps execute; Finalize defines done.

## Grounding: cite the wiki, or it isn't repeatable

An analysis is only as deterministic as its references. The Grounding
section lists every wiki concept doc (by concept id — `tables/<t>.md`,
`joins/…`, metric/reference pages) and every computation the steps rely on,
each with one line saying WHAT fact it contributes (the grain, the join key,
the enum values). Rules:

- Every table, join, and filterable column a step touches traces to a listed
  concept doc — the executor re-reads these before running, so a schema
  change surfaces as a grounding mismatch instead of a silently wrong query.
- Every computation a step names appears here. Cite only what you VERIFIED
  exists while authoring (read the docs, `list_computations`); a grounding
  entry you didn't open is a guess wearing a citation.
- Prose facts a step depends on ("cancelled orders carry status = 'X'")
  belong to a cited doc, not to the step's own memory.

## Questions: verbatim ask_human objects, no defaults

Each `questions` entry is byte-for-byte an `ask_human` question —
`{id, prompt, kind, options}`, kinds `single`/`multi`/`text` — because the
executing agent forwards them in ONE `ask_human` call unchanged. Rules:

- `id` is explicit, unique, stable (`time_window`, not `q1`): steps refer to
  answers as `{time_window}`, and renaming an id breaks every step that cites it.
- There are NO defaults. An analysis asks; the user answers. If a value can
  default, it isn't a question — fix it in the steps.
- Only ask what changes the procedure or its numbers. The UI always offers a
  free-text "Other" on choice kinds, so options list the LIKELY answers, not
  all of them.
- Keep prompts short, concrete, and answerable without reading the steps.

## Sourcing: the computation-first ladder

Every number a step produces must name its source, in this order of preference:

1. **Attested Computation** — cite it by name: "Run the `revenue_by_month`
   computation with `@region` = `{segment}`". Frozen SQL, human-verified,
   reproducible — the default for anything the report will assert.
2. **Semantic metric** (`query_metric`) — when the quantity is a documented
   metric with the dimensions you need but no computation freezes it.
3. **Ad-hoc SQL** — last resort, and DETERMINISTIC: embed the exact SQL
   pattern in a ```sql fence, with `{id}` placeholders where question
   answers bind — never "query the lap times and aggregate", which every
   run would improvise differently. Build the pattern from the cited
   Grounding docs (their table grains, validated join keys, documented
   filters), and mark the step: *"requires SQL to be enabled for the run"*.
   If the right computation doesn't exist yet, add a line at the end of
   Steps naming this pattern as a computation worth attesting — once a
   human freezes it, the step upgrades to rung 1 and the analysis hardens.

Ground the ladder BEFORE saving: read the dataset's docs, `list_computations`,
and the metric definitions so every cited name actually exists — everything
the ladder relies on is what the Grounding section records.

## Steps: write for the executing agent

- Numbered and imperative. Each step says what to run, with which inputs
  (question answers by `{id}`, or fixed values), and what to conclude or
  record from the result — a step whose output feeds no later step and no
  Finalize item is dead weight.
- Cite as you go: where a step's correctness rests on a wiki fact (a join
  key, a grain, an enum value), name the Grounding doc inline — "join on
  `driverId` (joins/drivers-results.md)" — so the executor can check the
  fact instead of trusting the step's memory of it.
- Business language for intent; exact names (tables, columns, computation
  slugs, SQL patterns) wherever the step executes something — precision in
  the action, prose in the purpose.
- State expectations where they exist ("row count should be within ±10% of
  the prior period; if not, stop and report the discrepancy") — that is what
  lets the executor distinguish a finding from a data problem.

## Finalize: define done

Name the deliverable precisely — almost always a report (`create_report`):
which sections, which KPIs, which verdicts or thresholds. The executor treats
this as the completion criterion; a vague Finalize gets you a vague report.

## Authoring flow

Agree scope with the user in chat first; draft the full document; save with
`create_analysis` (it validates and refuses with named problems — fix and
retry). The analysis is recorded under the user's ownership: only they can
have it edited (`update_analysis` replaces exact text you copy from
`read_analysis`, verbatim). When a non-owner wants changes, relay to the
owner or save an adapted copy under a new name. There is no delete tool —
deleting an analysis is a manual act on the app's Analysis page (owners
only); when asked, point the user there.
