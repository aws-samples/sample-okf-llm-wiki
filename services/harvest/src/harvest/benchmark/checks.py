"""The Benchmark Studio checks — solver protocol + grading, per check.

A *check* is one way of measuring the wiki (``docs/BENCHMARK_GUIDE.md``):

* **SQL EX** (``sql``, shown as "Accuracy") — the solver writes one SQL query;
  the grader executes gold + predicted and compares result sets
  (:mod:`.grader`) — deterministic, zero-LLM.
* **Behavior** (``behavior``) — the solver is a wiki-consumer agent answering
  the request in free-form text; there is NO deterministic grade. The JUDGE
  grades every run against the row's free-form ``expected_behavior``
  (hallucination checks, policy honoring, "should say the data isn't tracked",
  …) — see :mod:`.judge` ``make_behavior_grader``.

Each check gets its own INDEPENDENT solver round (never a shared solve — see
the design decision list), so this module packages, per check: the solver
system prompt, the parse of the solver's final text into a prediction string,
and (for deterministically-graded checks) a grade function returning the
shared :class:`~harvest.benchmark.grader.QuestionResult` shape. The run
orchestrator (:mod:`.report_run`) is generic over :class:`CheckSpec`;
``judge_graded`` routes a check's grading to the judge phase instead.

Every solver protocol stays **gold-blind**: no prompt or parse ever sees the
gold cell (SQL or expected behavior), and within a round every question gets
the same protocol, so no per-row signal leaks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, NamedTuple

from okf_core.benchmark_questions import (
    CHECK_BEHAVIOR,
    CHECK_SQL,
    BenchmarkQuestion,
)

from harvest.benchmark.extract import extract_sql, extract_text
from harvest.benchmark.grader import Grader, QuestionResult
from harvest.glue_source import GlueAthenaSource

# The SQL-writing solver prompts carry the source's dialect (same ⟪…⟫ token
# scheme as harvest.prompts._fill): Benchmark Studio has no source-type gate,
# so a Redshift dataset can be benchmarked — a solver told to write Athena/Trino
# against a Redshift grader would be mis-measured. The module constants below
# are the GLUE build (back-compat); solver_protocol fills the run's real
# dialect from source.prompt_profile.
_DIALECT_TOKEN = "⟪DIALECT⟫"
_GLUE_DIALECT = GlueAthenaSource.prompt_profile.dialect


def _fill_dialect(text: str, dialect: str) -> str:
    filled = text.replace(_DIALECT_TOKEN, dialect)
    if "⟪" in filled:
        raise ValueError(f"unfilled prompt token in: {filled[:200]}")
    return filled

# The shared wiki-exploration preamble every check's solver gets: the solver is
# the simulated consumer, and its ONLY knowledge source is the wiki.
_WIKI_PREAMBLE = """\
You are answering ONE analytics question using ONLY the knowledge in this data \
wiki. The wiki is a set of markdown docs about the dataset's tables, columns, \
joins, metrics, and gotchas.

Your only knowledge source is the wiki — you have read-only tools (`read_file`, \
`glob`, `grep`, `ls`) over it, plus `read_me` (a short usage primer), and \
nothing else. You CANNOT query the database, see its raw schema, or sample \
data; if the wiki doesn't say something, you must infer it from what the wiki \
does say. This mirrors a real agent that has only the wiki to go on — so a \
good wiki should let you succeed.

Method:
1. Call `read_me` — how the wiki is organized, where caveats and gotchas \
live, and how to search it well.
2. Find the relevant table/reference docs (`glob`/`grep` for table names, \
columns, metrics named in the question).
3. Read them. Note the exact table + column names, the join keys, any coded \
values / units / filters the docs call out (e.g. "status is an int code, \
1=active", "revenue excludes refunds").
"""

_SQL_SOLVER_PROMPT_TMPL = (
    _WIKI_PREAMBLE
    + """\
4. Write ONE ⟪DIALECT⟫ SQL query that answers the question, using the exact \
names and semantics the docs specify.

When you have the answer, output the final query as a single fenced SQL block:
```sql
SELECT ...
```
Output nothing after the block."""
)

SQL_SOLVER_PROMPT = _fill_dialect(_SQL_SOLVER_PROMPT_TMPL, _GLUE_DIALECT)

# The Behavior solver is the closest simulation of a REAL wiki consumer, so its
# prompt carries what a production agent would know: the wiki's structure and
# how to work it. It never sees the expected_behavior being graded against.
# Two intros: the classic wiki-only solver, and the run-configurable live-SQL
# variant (behavior_live_sql) that also holds read-only `run_sql` — a truer
# consumer simulation, since production agents can query. The body (structure +
# answer discipline) is shared so the two variants only differ in capability.
_BEHAVIOR_INTRO_WIKI_ONLY = """\
You are a data agent handling ONE user request about a dataset. Your ONLY \
knowledge source is the dataset's data wiki — authored markdown docs about the \
data — explored with read-only tools (`read_file`, `glob`, `grep`, `ls`), plus \
`read_me` (a short usage primer). You \
CANNOT query the database, see its raw schema, or sample data. This mirrors a \
real agent serving users with only the wiki to go on.
"""

_BEHAVIOR_INTRO_LIVE_SQL_TMPL = """\
You are a data agent handling ONE user request about a dataset. Your GUIDANCE \
comes from the dataset's data wiki — authored markdown docs about the data — \
explored with read-only tools (`read_file`, `glob`, `grep`, `ls`), plus \
`read_me` (a short usage primer). You also have `run_sql` — read-only \
⟪DIALECT⟫ SQL against the LIVE dataset — to execute what the wiki led you \
to and to check facts. This mirrors a real agent serving users with the wiki \
plus query access. The wiki LEADS and SQL verifies: take table names, join \
keys, coded values, filters, and policies from the docs, and honor the wiki's \
caveats and guardrails even when the raw data would let you answer otherwise.
"""

_BEHAVIOR_BODY = """\

How the wiki is structured:
- Every directory has an `index.md` listing and linking what's inside — `ls` \
the root and read `index.md` first to orient.
- Table docs live under `tables/` (one page per table: columns, types, join \
keys, coded values, units, caveats). Dataset-level pages (overview, metrics, \
reference material) live at the root or in their own directories; \
cross-dataset relationship docs live under `external/`.
- Docs cross-link by relative path, and many carry curator annotations or \
known-issue notes — read those sections; they often correct or constrain what \
the surrounding doc says.
- `grep` matches a LITERAL string (not a regex): search short, distinctive \
terms from the request (a column, a metric, a code value) and follow the hits.

How to answer:
1. Call `read_me`, orient (`index.md`), then find and READ the docs relevant \
to the request.
2. Ground EVERY claim in something a doc actually states (cite the doc's \
path for the load-bearing facts — names, codes, units, filters, joins) or in \
a query you actually ran and its results, where you have that ability.
3. If the wiki does not state something the request needs, SAY SO plainly and \
answer only what the wiki supports — never guess, never invent tables, \
columns, values, or numbers.
4. Honor any policies, usage caveats, or restrictions the wiki states, even \
when the request pushes against them.

Asking the user: you also have `ask_human(questions)` — the escalation path a \
production agent has. When the request genuinely cannot be answered safely \
without the user's input — a required dimension (period, region, grain, scope) \
is missing, a term resolves to more than one documented thing, or the wiki's \
guardrails direct agents to ASK for this kind of request — call it with your \
specific question(s) instead of answering. Calling it ENDS the run: there is \
no user here to reply, and the ask itself is recorded as your final answer. \
Ask only when the docs cannot settle the reading — never to avoid the reading, \
and never when the wiki supports a direct answer.

Your FINAL message is your complete answer to the user — everything before it \
is working (unless you end the run with `ask_human`, which IS the answer). \
Make it self-contained and honest about any limits."""

BEHAVIOR_SOLVER_PROMPT = _BEHAVIOR_INTRO_WIKI_ONLY + _BEHAVIOR_BODY
_BEHAVIOR_SOLVER_LIVE_SQL_TMPL = _BEHAVIOR_INTRO_LIVE_SQL_TMPL + _BEHAVIOR_BODY
BEHAVIOR_SOLVER_PROMPT_LIVE_SQL = _fill_dialect(
    _BEHAVIOR_SOLVER_LIVE_SQL_TMPL, _GLUE_DIALECT
)

@dataclass(frozen=True)
class CheckSpec:
    """One check's protocol: id, human label, solver prompt, and reply parse.

    ``parse`` turns the solver's final text into the PREDICTION string the
    grading consumes (SQL text for SQL EX; the whole answer for Behavior).
    ``uses_athena`` tells the orchestrator whether deterministic grading must
    run under the Athena semaphore. ``judge_graded`` means there IS no
    deterministic grade — the judge grades every (question, run) attempt
    against the gold cell in the judge phase.
    """

    check: str
    label: str
    solver_prompt: str
    parse: Callable[[Any], str]
    uses_athena: bool
    judge_graded: bool = False


CHECK_SPECS: dict[str, CheckSpec] = {
    CHECK_SQL: CheckSpec(
        check=CHECK_SQL,
        label="SQL EX",
        solver_prompt=SQL_SOLVER_PROMPT,
        parse=extract_sql,
        uses_athena=True,
    ),
    CHECK_BEHAVIOR: CheckSpec(
        check=CHECK_BEHAVIOR,
        label="Behavior",
        solver_prompt=BEHAVIOR_SOLVER_PROMPT,
        parse=extract_text,
        uses_athena=False,
        judge_graded=True,
    ),
}


class SolverProtocol(NamedTuple):
    """One run's resolved solver protocol: the prompt and its tool grants.

    The prompt and the grants travel TOGETHER (the module invariant): a solver
    is never told about a tool it doesn't hold, or handed one its prompt
    disclaims. ``wants_ask`` grants the terminal ``ask_human`` escalation tool
    — Behavior only, on every question uniformly (gold-blind), so "should ask
    for clarification" expectations become a clear-cut structural outcome (the
    tool CALL) instead of judge-interpreted prose. SQL EX never gets it: its
    contract is one fenced query, and asking is not a gradable SQL outcome.
    """

    prompt: str
    wants_sql: bool
    wants_ask: bool


def solver_protocol(
    spec: CheckSpec, *, behavior_live_sql: bool = False, dialect: str | None = None
) -> SolverProtocol:
    """Resolve a run's solver protocol for ``spec`` (see :class:`SolverProtocol`).

    ``behavior_live_sql`` is the run-config toggle: True gives the BEHAVIOR
    solver the live-SQL prompt (and tells the caller to hand it the ``run_sql``
    tool). It NEVER applies to the SQL EX check — its solver stays data-blind
    by design (live queries would let it iterate empirically to the answer,
    measuring persistence instead of the wiki), whatever the flag says.

    ``dialect`` is the run's SQL dialect (``source.prompt_profile.dialect``) —
    the SQL-writing prompts name it so a Redshift dataset's solver is never
    told to write Athena/Trino. None defaults to the Glue dialect (back-compat
    for the module constants and legacy callers).
    """
    d = dialect or _GLUE_DIALECT
    if spec.check == CHECK_BEHAVIOR and behavior_live_sql:
        return SolverProtocol(
            _fill_dialect(_BEHAVIOR_SOLVER_LIVE_SQL_TMPL, d), True, True
        )
    if spec.check == CHECK_SQL:
        return SolverProtocol(_fill_dialect(_SQL_SOLVER_PROMPT_TMPL, d), False, False)
    return SolverProtocol(spec.solver_prompt, False, spec.check == CHECK_BEHAVIOR)


def make_grade_fn(
    spec: CheckSpec,
    *,
    grader: Grader | None,
) -> Callable[[BenchmarkQuestion, str], QuestionResult]:
    """Bind ``spec`` to its deterministic grading backend: ``grade(question, prediction)``.

    Only meaningful for deterministically-graded checks — SQL EX uses the
    shared :class:`Grader` (its gold cache makes N runs affordable: gold
    executes once per report, not once per run). A ``judge_graded`` check has
    no grade fn; asking for one is a wiring bug, surfaced loudly.
    """
    if spec.judge_graded:
        raise ValueError(f"check {spec.check!r} is judge-graded — no grade fn")
    if spec.check == CHECK_SQL:
        assert grader is not None, "SQL EX grading requires the Grader"
        return lambda q, pred: grader.grade(q.q_id, q.gold_sql, pred)
    raise ValueError(f"unknown check {spec.check!r}")
