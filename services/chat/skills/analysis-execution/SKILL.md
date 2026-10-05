---
name: analysis-execution
description: >-
  How to execute a saved analysis: resolve-then-ask for its questions, follow
  the steps as written, honor the sourcing ladder, and finalize per the
  document. Read BEFORE running your first analysis of a conversation.
---
# Analysis execution

A saved analysis is a procedure a human agreed to and owns — you are its
executor, not its editor. The document (from `read_analysis`) is the
authority for WHAT to do; this skill is only the discipline for HOW.

## 1. Gather the inputs: resolve, then ask once

The frontmatter `questions` are the analysis's declared inputs.

- First resolve from what you already have: this conversation, and answers
  the user gave unprompted. Resolve only when the answer is UNAMBIGUOUS —
  "the user mentioned Q3 revenue earlier" is not an answer to "which period
  should the analysis cover?".
- Everything unresolved goes out in ONE `ask_human` call, each question
  passed VERBATIM — same `id`, `prompt`, `kind`, `options`. Do not reword,
  merge, split, or add questions of your own; the owner chose these words.
- Never invent or default an answer: the questions exist precisely because
  the owner decided these choices are the user's. If every question is
  already resolved, say so in one line and proceed.
- Before executing, restate the resolved inputs in one short line ("Running
  churn-cohort-deep-dive: last 12 months, all segments") so the user can
  catch a wrong resolution before it costs queries.

## 2. Execute the steps as written

- The Grounding section is part of the procedure: `read_page` the cited
  concept docs you haven't already read this conversation BEFORE the steps
  that depend on them. If reality disagrees with a grounding fact (a column
  renamed, a computation gone), that is a documented deviation — not a cue
  to improvise quietly.
- In order, each step with the inputs it names (`{time_window}` means the
  answer to that question id). A step that embeds a SQL pattern runs THAT
  pattern with the placeholders bound — do not rewrite it into your own
  query.
- Honor the step's source: a step that names an Attested Computation runs
  THAT computation (`run_computation`); a step that names a metric uses
  `query_metric`; only a step that explicitly calls for ad-hoc SQL gets
  `run_sql` — and only when SQL is enabled for this run. If it isn't, or a
  named computation is missing or refuses, STOP substituting: report which
  step is blocked and why, deliver what completed, and note the gap in the
  final report. A silently swapped source produces numbers the analysis
  never promised.
- Record per step what its result was and what you concluded — the Finalize
  section will need exactly that.
- Where a step states an expectation ("row count within ±10% of prior
  period") and reality disagrees, follow the step's own instruction; absent
  one, stop and surface the discrepancy rather than building on suspect data.

## 3. Deviations are reported, never silent

Reality sometimes forces a deviation (a column renamed, an empty partition,
a cap hit). Deviate minimally, and say so — in chat as it happens and in the
report's caveats. If the document itself is broken (step cites a computation
or column that no longer exists, questions contradict the steps), report the
defect to the user; if they own the analysis, offer to fix it via
`update_analysis` — never patch the stored document as a side effect of
running it, and never edit one the user doesn't own.

## 4. Finalize per the document

The Finalize section is the completion criterion. When it demands a report —
the usual case — read `read_skill("report-authoring")` if you haven't this
conversation, build it with `create_report` from the evidence THIS run
produced, and `present_report`. State the analysis name and version in the
report (a line in the opening markdown block: *"Produced by analysis
churn-cohort-deep-dive v3"*), carry provenance on every figure as usual, and
include the resolved question answers so the report is self-describing.

Then bind the artifact: call `publish_report` with the report_id
`create_report` returned and the analysis name. This records the report as a
durable artifact OF THE ANALYSIS — reachable from it (and from the analysis
side panel) even after this conversation is deleted. Publish exactly the
report the run produced, once; ad-hoc reports outside an analysis are never
published. You are done when Finalize's list is satisfied and the report is
published — not before, not beyond.
