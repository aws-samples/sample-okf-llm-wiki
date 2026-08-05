"""The ``read_me`` primer: how to use a Data Wiki bundle, served as a TOOL.

One text, two consumers — the consumption MCP server (external agents) and the
Benchmark Studio solver (the simulated consumer) both expose it as a
``read_me`` tool whose description tells the agent to call it BEFORE exploring
the wiki. The point: an agent should not have to re-derive the bundle layout,
where the gotchas live, or which navigation moves actually work (index-first,
backlinks, one-token greps) mid-task, every session.

Two renderings, because the two surfaces hold DIFFERENT tools: the MCP has
``get_backlinks``/``semantic_search`` and a REGEX ``grep``; the solver has
``read_file``/``glob``/``grep``/``ls`` with a LITERAL grep and nothing else.
A primer that names tools the agent doesn't have teaches it to make failing
calls, so each surface serves only its own moves.

Deliberately SHORT — the tests pin a size budget. A primer an agent skims past
isn't a primer; every line must earn its place.
"""

from __future__ import annotations

#: The shared tool description. Both surfaces use it verbatim so the FIRST
#: thing any agent reads about the tool is when to call it.
READ_ME_DESCRIPTION = (
    "Use this tool FIRST, before exploring the wiki. Returns a short primer: "
    "how the wiki is structured, where gotchas/known issues/guardrails live, "
    "and the fastest ways to find answers with the tools you have."
)

_STRUCTURE = """\
# How to use this data wiki

## Structure (uniform across datasets)
- `index.md` at the root is the map — read it first. Every directory has its \
own `index.md` listing and linking what's inside.
- `tables/` — one page per table: the grain (what one row is), columns and \
types, join keys, coded values, units, caveats.
- `references/` — cross-cutting facts: `joins/` (how tables connect, with \
tested SQL), `metrics/` (official definitions and formulas), `named_sets/` \
(canonical filters/lifecycles), `glossary/`, `known_issues/`, `recipes/` \
(mandatory query transforms, e.g. a dedup — apply verbatim; present only when \
the dataset needs one), and `usage_guardrails.md` (the dataset's do's and \
don'ts).
- `external/<domain>/<dataset>/` — documented relationships to OTHER datasets \
(shared keys, how to join across, semantic overlaps).
"""

_TRAPS = """\
## Where the traps live
- Before answering anything non-trivial, check `references/usage_guardrails.md` \
and `references/known_issues/` — policies, quirks, and known data problems are \
recorded there, not on the table pages.
- Table pages call out coded values ("status is an int code, 1=active"), \
units, and default filters — exactly the details naive SQL gets wrong. Trust \
them over intuition.
- Curator annotations and known-issue notes INSIDE a page correct or constrain \
the text around them — they win over the surrounding prose.
"""

_SKEPTICISM = """\
## If a result looks wrong
- Treat an implausible query result (zero rows, an off-scale total, \
duplicated rows) as a bug in YOUR query until checked. Usual causes: a \
join that fans out (join docs state the cardinality), a skipped \
mandatory transform (`references/recipes/`), a sentinel value aggregated as \
real data (enum docs flag them), a non-additive measure summed across periods \
(guardrails).
- Fix once and re-run; if still odd, report the number WITH the anomaly named. \
Once the mechanics check out, a surprising number is the answer — never re-run \
variations until one matches your expectation.
"""

_SHARED_TAIL = """\
- Copy SQL specifics (exact names, join conditions, filters) from the docs' \
SQL snippets — they were verified against the live data.
- If the wiki doesn't state something, treat it as not tracked: say so rather \
than inventing a value.
"""

_MCP_NAV = (
    """\
## How to navigate (fastest moves)
- Find datasets by MEANING: `search_domains` / `semantic_search`. \
`list_domains` pages the catalog — narrow with `domain`/`query`; \
`next_cursor` continues. Every other tool takes (data_domain, dataset).
- Go index-first: `list_directory` at the root, then per subdirectory — it \
names every page, so you never guess concept ids.
- `grep` is a REGEX over page contents — use it for exact tokens (a column \
name, a coded value, a table name). `semantic_search` when you only know the \
meaning, then `read_page` the hits.
- `get_backlinks(concept_id)` is the most underused move: one call lists \
every page that references a concept — the fastest route from a table to the \
join docs, metrics, and caveats that mention it. Prefer it over guessing \
paths.
"""
    + _SHARED_TAIL
)

_SOLVER_NAV = (
    """\
## How to navigate (fastest moves)
- Go index-first: `ls` the root, read `index.md`, then a directory's \
`index.md`, then the page — don't guess paths.
- `grep` matches a LITERAL string (not a regex): search ONE short distinctive \
token (a column name, a coded value, a metric name), never a sentence.
- `glob` finds pages by name pattern (e.g. `tables/*`, `**/*orders*`).
- Follow the links between pages — a table page links to the join docs and \
metrics that use it, and those pages carry the caveats.
"""
    + _SHARED_TAIL
)

#: What the consumption MCP's ``read_me`` returns.
MCP_PRIMER = f"{_STRUCTURE}\n{_TRAPS}\n{_SKEPTICISM}\n{_MCP_NAV}"

#: What the benchmark solver's ``read_me`` returns.
SOLVER_PRIMER = f"{_STRUCTURE}\n{_TRAPS}\n{_SKEPTICISM}\n{_SOLVER_NAV}"
