"""Deterministic whole-bundle lint.

The write-time guard (``okf_core.guard``) validates one document at a time;
nothing validates the BUNDLE — the cross-file invariants a finished wiki must
satisfy. This module owns those checks, pure and offline (no AWS, no agent
deps), so the harvest agent's ``lint_bundle`` tool, the tests, and any future
CLI share one implementation.

Steps run in order and are isolated — a crashing step reports ``failed`` and
the rest still run:

1. ``coverage``      — every table in the ``.metadata/`` snapshot has its
                       ``tables/<table>.md``; a table doc with no snapshot
                       table is flagged as stale.
2. ``required_docs`` — ``references/usage_guardrails.md`` and a dataset
                       overview exist.
3. ``frontmatter``   — every published doc parses and carries the required
                       frontmatter keys (re-asserts the guard bundle-wide).
4. ``links``         — every intra-bundle link target exists; reference docs
                       nothing links to are flagged; the guardrails doc must
                       be linked from a dataset overview.
5. ``joins``         — join docs' ``a.x = b.y`` conditions name existing
                       columns whose Hive type families are comparable.

SQL fences are classified once by :func:`collect_sql_fences` (runnable
statement vs bare ON-clause fragment, placeholder-templated or not) so the
harvest tool's live ``EXPLAIN`` step and these offline checks agree on what
counts as runnable SQL.

Everything is derived from the bundle on disk: the ``.metadata/`` snapshot is
the source of truth for "which tables exist" (``metadata_export`` writes it at
run start), which keeps :func:`lint_bundle` argument-free for the agent and
correct however many source databases fed the snapshot. When the snapshot is
absent, the steps that need it report ``skipped`` instead of guessing.

Checks deliberately NOT here: whether a link that resolves is semantically
*right*, or whether SQL that parses returns what the prose claims — that is
judgment, owned by the reviewer subagents, not lint. Likewise there is no
``# Schema``-vs-source column check: extracting "which columns does this doc
claim" from free-form markdown proved impossible to do consistently (value
legends, nullability notes, and column-family prose all read as claimed
columns and produced false errors on real bundles), and a lint whose errors
can be wrong gets ignored — schemas are verified by the reviewers against
live data instead. The join step is the precise remnant: it checks only bare
``table.col = table.col`` pairs, which have no ambiguity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from okf_core.document import OKFDocument
from okf_core.links import extract_links

# The snapshot dir name is harvest's contract (harvest.metadata_export.
# METADATA_DIR); okf_core cannot import harvest, so the literal is repeated.
_METADATA_DIR = ".metadata"
_COLUMNS_TSV = "columns.tsv"

GUARDRAILS_PATH = "references/usage_guardrails.md"

# Reserved/generated names and internal dirs — mirror link_graph/index_gen:
# generated index.md carries no frontmatter and log.md is free-form, so
# neither is a lintable concept doc (but both may be LINK TARGETS).
_RESERVED_BASENAMES = {"index.md", "log.md"}
_INTERNAL_DIRS = {"large_tool_results", "conversation_history"}


@dataclass
class LintFinding:
    """One lint problem. ``severity`` is ``error`` (must fix before the bundle
    is publishable) or ``warning`` (judgment call)."""

    severity: str
    code: str
    path: str  # bundle-relative doc path, "" for bundle-level findings
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


@dataclass
class LintStep:
    """One step's outcome: ``ok`` | ``issues`` | ``skipped`` | ``failed``."""

    name: str
    status: str
    note: str = ""
    findings: list[LintFinding] = field(default_factory=list)


@dataclass
class LintReport:
    steps: list[LintStep] = field(default_factory=list)

    @property
    def findings(self) -> list[LintFinding]:
        return [f for s in self.steps for f in s.findings]

    @property
    def ok(self) -> bool:
        # A failed step means unverified invariants — that is not a pass.
        return not any(s.status == "failed" for s in self.steps) and not any(
            f.severity == "error" for f in self.findings
        )


# ---------------------------------------------------------------------------
# SQL fence collection (shared with the harvest tool's EXPLAIN step)
# ---------------------------------------------------------------------------

_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([A-Za-z0-9_+\-]*)\s*$")

# Authoring-template placeholders that make SQL non-runnable as written:
# <table>-style angle tokens (but not Hive/Trino generic types), {{jinja}},
# ${shell}, :named params (but not :: casts), and elided "..." SQL.
_ANGLE_RE = re.compile(r"<([A-Za-z_][A-Za-z0-9_.\-]*)>")
_SQL_TYPE_WORDS = frozenset(
    "varchar char character string int integer bigint smallint tinyint double "
    "float real boolean date time timestamp decimal binary varbinary array "
    "map row struct interval".split()
)
_PLACEHOLDER_RES = (
    re.compile(r"\{\{[^}]*\}\}"),
    re.compile(r"\$\{[^}]*\}"),
    re.compile(r"(?<![:\w]):[A-Za-z_]\w*"),
    re.compile(r"\.\.\."),
)

_FIRST_WORD_RE = re.compile(r"[A-Za-z]+")


@dataclass
class SqlFence:
    """One ```sql fence, classified for the EXPLAIN step.

    ``statements`` holds the EXPLAIN-able statements found in the fence
    (SELECT/WITH, any authored leading ``EXPLAIN [ANALYZE]`` stripped so it is
    never doubled — and never executed via ANALYZE). ``fragment`` marks a bare
    expression fence (a join doc's ON clause) that is valid content but not
    runnable SQL. ``templated`` fences contain placeholders and must never be
    sent to an engine.
    """

    path: str
    text: str
    statements: list[str] = field(default_factory=list)
    templated: bool = False
    fragment: bool = False


def _sql_fences_in(body: str) -> list[str]:
    """Text of every fenced code block tagged ``sql`` (case-insensitive)."""
    fences: list[str] = []
    open_marker: str | None = None
    is_sql = False
    lines: list[str] = []
    for line in body.splitlines():
        m = _FENCE_OPEN_RE.match(line)
        if open_marker is None:
            if m and m.group(2):
                open_marker = m.group(1)
                is_sql = m.group(2).lower() == "sql"
                lines = []
        else:
            closes = (
                m is not None
                and not m.group(2)
                and m.group(1)[0] == open_marker[0]
                and len(m.group(1)) >= len(open_marker)
            )
            if closes:
                if is_sql and lines:
                    fences.append("\n".join(lines).strip())
                open_marker = None
            else:
                lines.append(line)
    # An unclosed fence at EOF (truncated write, fence as the doc's last
    # element) still RENDERS as code in CommonMark — dropping it would let its
    # SQL escape both EXPLAIN and the join checks, and earn the doc a false
    # "no join condition" warning.
    if open_marker is not None and is_sql and lines:
        fences.append("\n".join(lines).strip())
    return fences


def _mask_sql(text: str, *, idents: bool = True) -> str:
    """``text`` with string literals, comments, and (optionally) double-quoted
    identifiers blanked to spaces — LENGTH-PRESERVING, so original/masked
    indices stay aligned. Every structural scan (statement splitting, first
    token, the placeholder and fragment heuristics, join-pair matching) reads
    the mask; the ORIGINAL text is what ships to the engine. This is what
    keeps a ``;``, ``:word``, or ``...`` inside a literal or comment from
    steering classification. ``idents=False`` keeps double-quoted identifier
    CONTENTS visible (the join-pair regex must still see ``"orders"``)."""
    out = list(text)

    def blank(a: int, b: int) -> None:
        for j in range(a, min(b, len(text))):
            if text[j] != "\n":
                out[j] = " "

    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "'":  # string literal; '' escapes a quote
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            blank(i + 1, j)
            i = j + 1
        elif c == '"':  # quoted identifier
            j = text.find('"', i + 1)
            j = n if j < 0 else j
            if idents:
                blank(i + 1, j)
            i = j + 1
        elif text.startswith("--", i):  # line comment (markers included)
            j = text.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif text.startswith("/*", i):  # block comment (markers included)
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        else:
            i += 1
    return "".join(out)


def _is_templated(masked: str) -> bool:
    """Placeholder scan over the MASK — a ``:word`` in a JSON string literal
    or a ``...`` in a comment/LIKE pattern is content, not a template."""
    for m in _ANGLE_RE.finditer(masked):
        if m.group(1).lower() not in _SQL_TYPE_WORDS:
            return True
    return any(r.search(masked) for r in _PLACEHOLDER_RES)


# Trino/Athena EXPLAIN forms: EXPLAIN [ (option, ...) ] stmt and
# EXPLAIN ANALYZE [VERBOSE] stmt — strip whichever leads so we never double
# the keyword (and never execute via ANALYZE).
_EXPLAIN_PREFIX_RE = re.compile(
    r"explain\s*(?:\([^)]*\)\s*)?(?:analyze\s+)?(?:verbose\s+)?",
    re.IGNORECASE,
)


def _classify_fence(path: str, text: str) -> SqlFence:
    masked = _mask_sql(text)
    statements: list[str] = []
    # Split on ';' found in the MASK (never inside a literal/comment), then
    # classify each part by its first real token — leading comments are
    # spaces in the mask, so a /* header */-opened statement still counts.
    bounds = [i for i, ch in enumerate(masked) if ch == ";"] + [len(text)]
    start = 0
    for idx in bounds:
        orig, msk = text[start:idx], masked[start:idx]
        start = idx + 1
        lead = len(msk) - len(msk.lstrip())
        tail = len(msk.rstrip())
        orig, msk = orig[lead:tail], msk[lead:tail]
        if not msk:
            continue
        tok = _FIRST_WORD_RE.match(msk)
        token = tok.group(0).lower() if tok else ""
        if token == "explain":
            pm = _EXPLAIN_PREFIX_RE.match(msk)
            cut = pm.end() if pm else 0
            orig, msk = orig[cut:], msk[cut:]
            lead = len(msk) - len(msk.lstrip())
            orig, msk = orig[lead:], msk[lead:]
            tok = _FIRST_WORD_RE.match(msk)
            token = tok.group(0).lower() if tok else ""
        if token in ("select", "with"):
            statements.append(orig.strip())
    fragment = not statements and "=" in masked
    return SqlFence(
        path=path,
        text=text,
        statements=statements,
        templated=_is_templated(masked),
        fragment=fragment,
    )


def collect_sql_fences(bundle_root: str | Path) -> list[SqlFence]:
    """Every published doc's ```sql fences, classified. Never raises per-doc:
    unparseable docs contribute nothing (the ``frontmatter`` step reports
    them)."""
    ctx = _Context(Path(bundle_root))
    out: list[SqlFence] = []
    for rel in ctx.doc_paths:
        doc = ctx.parsed.get(rel)
        if doc is None:
            continue
        for text in _sql_fences_in(doc.body):
            out.append(_classify_fence(rel, text))
    return out


# ---------------------------------------------------------------------------
# Hive type comparability (for join keys)
# ---------------------------------------------------------------------------

_INT_TYPES = frozenset({"tinyint", "smallint", "int", "integer", "bigint"})
_DECIMAL_TYPES = frozenset({"float", "double", "real", "decimal", "numeric"})
_TEXT_TYPES = frozenset({"string", "varchar", "char", "character"})
_BINARY_TYPES = frozenset({"binary", "varbinary"})


def _type_family(hive_type: str) -> str | None:
    """Coarse comparability family; None = complex/unknown (skip the check).

    Covers both snapshot vocabularies: Hive/Trino (Glue sources) and the
    Postgres spellings a Redshift snapshot carries (``character varying``,
    ``double precision``, ``timestamp without time zone``)."""
    base = hive_type.strip().lower().split("(")[0].split("<")[0].strip()
    if base in _INT_TYPES:
        return "integer"
    if base in _DECIMAL_TYPES or base == "double precision":
        return "decimal"
    if base in _TEXT_TYPES or base.startswith("character"):
        return "text"
    if base in ("boolean", "bool"):
        return "boolean"
    if base == "date":
        return "date"
    if base.startswith("timestamp"):
        return "timestamp"
    if base in _BINARY_TYPES:
        return "binary"
    return None


def _families_comparable(a: str, b: str) -> bool:
    return a == b or {a, b} <= {"integer", "decimal"}


# ---------------------------------------------------------------------------
# Bundle context (enumerated/parsed once, shared by the steps)
# ---------------------------------------------------------------------------


def _is_reserved_rel(rel_parts: tuple[str, ...]) -> bool:
    if any(p.startswith(".") or p in _INTERNAL_DIRS for p in rel_parts[:-1]):
        return True
    return rel_parts[-1] in _RESERVED_BASENAMES


class _Context:
    def __init__(self, root: Path):
        self.root = root

    @cached_property
    def doc_paths(self) -> list[str]:
        """Published concept docs: bundle-relative posix paths, sorted;
        dot-dirs (.metadata/.context/.harvest), internal scratch dirs, and the
        generated/reserved index.md + log.md excluded."""
        out: list[str] = []
        if not self.root.is_dir():
            return out
        for md in self.root.rglob("*.md"):
            rel = md.relative_to(self.root)
            if _is_reserved_rel(rel.parts):
                continue
            out.append(rel.as_posix())
        return sorted(out)

    @cached_property
    def parsed(self) -> dict[str, OKFDocument | None]:
        """rel path -> parsed doc, or None when unreadable/malformed (the
        frontmatter step turns Nones into findings; other steps skip them)."""
        out: dict[str, OKFDocument | None] = {}
        for rel in self.doc_paths:
            try:
                out[rel] = OKFDocument.parse(
                    (self.root / rel).read_text(encoding="utf-8")
                )
            except Exception:  # noqa: BLE001 — malformed docs are a finding, not a crash
                out[rel] = None
        return out

    @cached_property
    def links_by_doc(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for rel, doc in self.parsed.items():
            if doc is None:
                continue
            out[rel] = extract_links(doc.body, (self.root / rel).parent, self.root)
        return out

    @cached_property
    def snapshot_tables(self) -> set[str] | None:
        """Table names per the snapshot, or None when no snapshot exists."""
        tables_dir = self.root / _METADATA_DIR / "tables"
        if tables_dir.is_dir():
            return {p.stem for p in tables_dir.glob("*.md")}
        if self.snapshot_columns is not None:
            return set(self.snapshot_columns)
        return None

    @cached_property
    def snapshot_columns(self) -> dict[str, dict[str, str]] | None:
        """columns.tsv as {table: {column: type}} (lower-cased keys), or None."""
        return _read_columns_tsv(self.root / _METADATA_DIR / _COLUMNS_TSV)

    @cached_property
    def all_known_columns(self) -> dict[str, dict[str, str]]:
        """Own snapshot plus any cross-run target snapshots under
        ``.metadata/external/<domain>/<dataset>/`` — the lookup for join docs,
        which may name counterpart tables."""
        merged: dict[str, dict[str, str]] = {
            t: dict(cols) for t, cols in (self.snapshot_columns or {}).items()
        }
        ext_root = self.root / _METADATA_DIR / "external"
        if ext_root.is_dir():
            for tsv in sorted(ext_root.glob(f"*/*/{_COLUMNS_TSV}")):
                for table, cols in (_read_columns_tsv(tsv) or {}).items():
                    # A table name that exists in BOTH snapshots (own +
                    # counterpart) UNIONS its columns — first-wins would
                    # validate a pair doc against the wrong side's schema and
                    # fire false unknown-column errors. A column whose type
                    # disagrees across snapshots drops its type so the join
                    # type check can't fire a false mismatch either.
                    bucket = merged.setdefault(table, {})
                    for col, typ in cols.items():
                        if col in bucket and bucket[col] != typ:
                            bucket[col] = ""
                        else:
                            bucket.setdefault(col, typ)
        return merged


def _read_columns_tsv(path: Path) -> dict[str, dict[str, str]] | None:
    if not path.is_file():
        return None
    out: dict[str, dict[str, str]] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines[1:]:  # skip the "table\tcolumn\ttype\tcomment" header
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0]:
            continue
        out.setdefault(parts[0].lower(), {})[parts[1].lower()] = parts[2]
    return out


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _check_coverage(ctx: _Context) -> LintStep:
    if ctx.snapshot_tables is None:
        return LintStep("coverage", "skipped", note="no .metadata snapshot")
    have = {
        rel[len("tables/") : -len(".md")]
        for rel in ctx.doc_paths
        if rel.startswith("tables/") and rel.count("/") == 1
    }
    findings: list[LintFinding] = []
    checkable: set[str] = set()
    for t in sorted(ctx.snapshot_tables):
        # A source table literally named "index" or "log" cannot have a
        # standard doc: those basenames are reserved (index.md is generated,
        # log.md is free-form) and excluded from doc_paths — demanding
        # tables/log.md would be a permanently unfixable error.
        if f"{t}.md" in _RESERVED_BASENAMES:
            findings.append(
                LintFinding(
                    "warning",
                    "reserved-table-name",
                    f"tables/{t}.md",
                    f"Source table `{t}` collides with a reserved bundle "
                    f"filename, so it cannot have its own tables/{t}.md doc — "
                    f"document it inside the dataset overview (or a related "
                    f"table's doc) instead.",
                )
            )
            continue
        checkable.add(t)
    findings += [
        LintFinding(
            "error",
            "missing-table-doc",
            f"tables/{t}.md",
            f"Source table `{t}` (see .metadata/tables/{t}.md) has no "
            f"tables/{t}.md concept doc.",
        )
        for t in sorted(checkable - have)
    ]
    findings += [
        LintFinding(
            "warning",
            "stale-table-doc",
            f"tables/{t}.md",
            f"tables/{t}.md documents a table the source no longer has "
            f"(not in the .metadata snapshot) — likely renamed or dropped.",
        )
        for t in sorted(have - ctx.snapshot_tables)
    ]
    note = f"{len(ctx.snapshot_tables)} snapshot table(s), {len(have)} table doc(s)"
    return LintStep("coverage", "issues" if findings else "ok", note, findings)


def _check_required_docs(ctx: _Context) -> LintStep:
    findings: list[LintFinding] = []
    if GUARDRAILS_PATH not in ctx.doc_paths:
        findings.append(
            LintFinding(
                "error",
                "missing-usage-guardrails",
                GUARDRAILS_PATH,
                "The dataset's behavioural contract references/"
                "usage_guardrails.md is missing — it is always authored.",
            )
        )
    if not any(r.startswith("datasets/") for r in ctx.doc_paths):
        findings.append(
            LintFinding(
                "error",
                "missing-dataset-doc",
                "datasets/",
                "No dataset overview doc exists under datasets/ — consumers "
                "land there first.",
            )
        )
    return LintStep("required_docs", "issues" if findings else "ok", "", findings)


def _check_frontmatter(ctx: _Context) -> LintStep:
    findings: list[LintFinding] = []
    for rel in ctx.doc_paths:
        doc = ctx.parsed[rel]
        if doc is None:
            findings.append(
                LintFinding(
                    "error",
                    "invalid-doc",
                    rel,
                    "Document is unreadable or its frontmatter block is "
                    "malformed YAML — re-write it as a valid OKF doc.",
                )
            )
            continue
        try:
            doc.validate()
        except Exception as e:  # OKFDocumentError names the missing keys
            findings.append(LintFinding("error", "invalid-frontmatter", rel, str(e)))
    note = f"{len(ctx.doc_paths)} doc(s) checked"
    return LintStep("frontmatter", "issues" if findings else "ok", note, findings)


def _check_links(ctx: _Context) -> LintStep:
    findings: list[LintFinding] = []
    inbound: set[str] = set()
    for rel, targets in ctx.links_by_doc.items():
        for target in targets:
            inbound.add(target)
            # A dot-segment target (.metadata/..., .context/...) EXISTS on the
            # harvest mount but the published wiki never serves it (the MCP
            # tools hide dot-prefixed segments) — a link every consumer will
            # find dead, which is exactly what this check exists to catch.
            if any(
                p.startswith(".") or p in _INTERNAL_DIRS
                for p in target.split("/")
            ):
                findings.append(
                    LintFinding(
                        "error",
                        "broken-link",
                        rel,
                        f"Links to `{target}.md`, which is a reserved/"
                        f"internal path the published wiki never serves — "
                        f"cite the fact in prose or link a published doc "
                        f"instead.",
                    )
                )
                continue
            if not (ctx.root / f"{target}.md").is_file():
                findings.append(
                    LintFinding(
                        "error",
                        "broken-link",
                        rel,
                        f"Links to `{target}.md`, which does not exist — fix "
                        f"the path or author the missing doc.",
                    )
                )
    # Reference docs exist to be found via links; one nothing points at is
    # invisible to a consumer following the wiki (indexes are generated, so
    # they don't count as intent). Warning — the fix is a link, not deletion.
    for rel in ctx.doc_paths:
        if not rel.startswith("references/") or rel == GUARDRAILS_PATH:
            continue
        if rel[: -len(".md")] not in inbound:
            findings.append(
                LintFinding(
                    "warning",
                    "orphan-reference",
                    rel,
                    "No other doc links to this reference — link it from the "
                    "table/dataset doc(s) where a consumer would look for it.",
                )
            )
    if GUARDRAILS_PATH in ctx.doc_paths:
        guardrails_id = GUARDRAILS_PATH[: -len(".md")]
        linked_from_dataset = any(
            guardrails_id in targets
            for rel, targets in ctx.links_by_doc.items()
            if rel.startswith("datasets/")
        )
        if not linked_from_dataset:
            findings.append(
                LintFinding(
                    "warning",
                    "guardrails-not-linked",
                    GUARDRAILS_PATH,
                    "references/usage_guardrails.md is not linked from the "
                    "dataset overview — a guardrail a consumer never opens "
                    "can't protect it.",
                )
            )
    return LintStep("links", "issues" if findings else "ok", "", findings)


_JOIN_IDENT = r'"?[A-Za-z_][A-Za-z0-9_]*"?'
_JOIN_EQ_RE = re.compile(
    rf"((?:{_JOIN_IDENT}\.)*{_JOIN_IDENT})\.({_JOIN_IDENT})\s*=\s*"
    rf"((?:{_JOIN_IDENT}\.)*{_JOIN_IDENT})\.({_JOIN_IDENT})"
)


def _bare(ident: str) -> str:
    return ident.replace('"', "").lower()


def _is_join_doc(rel: str) -> bool:
    return (rel.startswith("references/joins/") and rel.endswith(".md")) or (
        rel.startswith("external/") and "/joins/" in rel
    )


def _check_joins(ctx: _Context) -> LintStep:
    """Cheap, metadata-only join checks: for every ``table.col = table.col``
    pair in a join doc's sql fences, the columns must exist and their type
    families must be comparable. Deliberately best-effort: cast/function-
    wrapped keys don't match the pattern (the cast IS the normalization), and
    a table the snapshot doesn't know (the cross-run counterpart without its
    target snapshot) is skipped, not guessed. Measuring match rates is
    ``validate_join``'s job — never lint's."""
    join_docs = [rel for rel in ctx.doc_paths if _is_join_doc(rel)]
    if not join_docs:
        return LintStep("joins", "ok", "no join docs")
    if not ctx.all_known_columns:
        return LintStep("joins", "skipped", note="no .metadata snapshot")

    findings: list[LintFinding] = []
    for rel in join_docs:
        doc = ctx.parsed[rel]
        if doc is None:
            continue
        pairs: set[tuple[str, str, str, str]] = set()
        for fence in _sql_fences_in(doc.body):
            # Scan through the literal/comment mask (identifiers kept visible)
            # so a commented-out `a.x = b.y` can't register as a condition.
            for m in _JOIN_EQ_RE.finditer(_mask_sql(fence, idents=False)):
                lt = _bare(m.group(1)).split(".")[-1]
                rt = _bare(m.group(3)).split(".")[-1]
                pairs.add((lt, _bare(m.group(2)), rt, _bare(m.group(4))))
        if not pairs:
            findings.append(
                LintFinding(
                    "warning",
                    "join-doc-no-condition",
                    rel,
                    "No `table.column = table.column` condition found in a "
                    "```sql fence — a join doc must ship its ON clause.",
                )
            )
            continue
        for lt, lc, rt, rc in sorted(pairs):
            # Each side checks INDEPENDENTLY: an unknown left table (an alias,
            # a counterpart dataset's) must not shield a typo'd column on the
            # known right table.
            types: list[str] = []
            for table, col, side in ((lt, lc, "left"), (rt, rc, "right")):
                cols = ctx.all_known_columns.get(table)
                if cols is None:
                    continue  # unknown table (alias / counterpart) — skip side
                if col not in cols:
                    findings.append(
                        LintFinding(
                            "error",
                            "join-key-unknown-column",
                            rel,
                            f"Join condition uses `{table}.{col}` ({side} "
                            f"side), but the snapshot has no such column on "
                            f"`{table}`.",
                        )
                    )
                    continue
                types.append(cols[col])
            if len(types) == 2:
                fam_l, fam_r = _type_family(types[0]), _type_family(types[1])
                if fam_l and fam_r and not _families_comparable(fam_l, fam_r):
                    findings.append(
                        LintFinding(
                            "warning",
                            "join-key-type-mismatch",
                            rel,
                            f"Join key types look incomparable: `{lt}.{lc}` is "
                            f"{types[0]} but `{rt}.{rc}` is {types[1]} — "
                            f"verify, and bake any needed cast into the ON "
                            f"clause.",
                        )
                    )
    note = f"{len(join_docs)} join doc(s) checked"
    return LintStep("joins", "issues" if findings else "ok", note, findings)


_STEPS = (
    ("coverage", _check_coverage),
    ("required_docs", _check_required_docs),
    ("frontmatter", _check_frontmatter),
    ("links", _check_links),
    ("joins", _check_joins),
)


def lint_bundle(bundle_root: str | Path) -> LintReport:
    """Run every offline lint step against the bundle at ``bundle_root``."""
    ctx = _Context(Path(bundle_root))
    report = LintReport()
    for name, fn in _STEPS:
        try:
            step = fn(ctx)
        except Exception as e:  # noqa: BLE001 — one broken step must not hide the rest
            step = LintStep(name, "failed", note=f"{type(e).__name__}: {e}")
        report.steps.append(step)
    return report
