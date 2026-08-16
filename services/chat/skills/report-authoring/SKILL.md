# Report authoring

How to compose a report worth trusting: structure, language, and block
discipline. Read this before your first `create_report` of a conversation;
the tool validates SHAPE — this is the methodology it cannot check.

## Structure

- Open with a markdown block that answers the question: the finding, the
  number that carries it, and the caveats — a reader gets the conclusion in
  the first screen, never after the charts.
- Then the headline numbers as 2–4 ADJACENT kpi blocks (they render as one
  row of cards), values pre-formatted the way they should be read
  ("1,204", "83.4s", "12.3%").
- Then evidence in argument order — each chart/table introduced by the
  markdown around it, never dropped in bare. One claim per figure.
- Close with a short markdown block: scope limits, what the data could NOT
  answer, and anything worth a follow-up. Do not grow the report with
  adjacent findings — name them here instead.

## Language — write for an executive, not an engineer

The reader is a decision-maker who was not in this conversation and does not
know (or care) what SQL, a query, a computation, or a wiki is. The report's
BODY must never mention them — no "the query returned", no "I ran", no
"verified computation", no table/column names, no tool names. The audit
trail renders AUTOMATICALLY as a numbered methodology appendix (verified
sources vs direct data pulls, the exact statements disclosed); your job in
the body is the story, not the plumbing.

- Tell a story with an arc: the question, the answer, what it means. Each
  section advances the argument; a reader who stops after any section still
  leaves with something true and complete.
- Business language throughout: "races per season", not "COUNT(*) grouped
  by season_id"; "pit stops slowed after the refuelling ban", not "the
  pit_stops table shows".
- Chart and table titles state the CLAIM, not the axes: "Lap times cluster
  at 83 seconds", not "Laps by bucket". If the title could caption any
  dataset, it is not a title yet.
- Write like a person presenting to leadership: plain declarative
  sentences, no hedging a number you verified, no confidence you did not
  earn. Where evidence is preliminary, say what would firm it up — in
  business terms ("a fuller season-by-season read would confirm…").
- Fold the dataset's documented caveats into the narrative where a reader
  would otherwise misread ("official season totals differ from race-day
  points because…") — as facts about the business data, never as notes
  about databases.

## Evidence discipline

- Every chart/table/kpi showing query-derived numbers carries provenance:
  `{"kind": "computation", "slug", "params"}` when a verified computation
  produced it, `{"kind": "adhoc_sql", "sql"}` otherwise. Readers see
  VERIFIED vs EXPLORATORY badges — never launder an ad-hoc number.
- Prefer a verified computation for any headline figure; when a computation
  and your SQL disagree, the report says so — that IS a finding.
- Real values only, exactly as the queries returned them: no estimates, no
  re-rounding, no figures from memory. NULL is a data fact — a gap in a
  chart is honest; a fabricated zero is not.
- The SQL you attach to an adhoc figure is disclosed verbatim in the report.
  Write it like someone will read it, because they will.

## Choosing figures

- Pick the chart that shows the SHAPE of the claim: distribution →
  histogram/boxplot; composition → pie/treemap; trend → line/area; ranked
  comparison → horizontal bar; flow → sankey; two-metric relation →
  scatter; single value against a range → gauge.
- Tables are exhibits, not exports: ≤ ~20 rows of the rows that MATTER
  (extremes, violations, examples), with a title saying why they matter.
- Aggregate in SQL, not in the spec — a figure with hundreds of points is
  usually a query that stopped too early.

## Scope

- One report answers the request that was asked. If the user's question
  shifted during the conversation, confirm the report's scope in chat
  BEFORE composing — a saved report is immutable.
- If the data genuinely cannot answer the request, the report's opening
  says so plainly and shows why. That is a complete, honest report.

## Delivery

- Saving alone shows the user NOTHING. After every successful
  `create_report`, call `present_report` with the returned report_id — that
  renders the report card at the end of your response. Then summarize the
  headline findings in 2–4 sentences; never paste the report's contents.
