"""Deterministic policy rules — declarative SQL checks on ``policies.yaml`` entries.

A COMPUTATIONAL policy entry may carry a ``rules:`` list — machine-checkable
bindings of the policy's anti-pattern, drawn from a CLOSED dimension catalog
(:data:`DIMENSIONS`). At query time the chat policy check evaluates them
against the agent's SQL *before* the LLM judge fleet runs: a decisively
evaluated policy (violation or pass, proven on the query's AST) never reaches
the judges; anything the evaluator cannot PROVE returns ``unknown`` and falls
through to the fleet exactly as before. The tri-state is the load-bearing
contract — the deterministic tier only ever flags-with-proof or stays
silent, never guesses:

* ``violation`` — the full resolution chain is proven: every implicated
  column resolved to a real ``(database, table)`` through the schema, and the
  forbidden shape is present (or the required one absent).
* ``pass`` — proven clean for this rule.
* ``unknown`` — anything unprovable: parse/qualify failure, a column flowing
  through a CTE/derived source whose name — or renamed projection alias
  (``points AS pts``) — overlaps the rule's bindings, an
  ``OR`` at the top of a WHERE, sqlglot not installed.

One rule = one dimension + dataset-specific bindings + a mandatory self-test
(``examples.violation`` / ``examples.pass``). The author gate runs the
self-test at submit time — a rule enters the document only after proving it
fires on its violation example and stays silent on its pass example (the same
move as qgen's submit-time gold execution).

Schema comes from the ``rules_schema.json`` sidecar authored next to
``policies.yaml`` (``okf_aws.ar_policy``): ``{"databases": {db: {table:
[columns]}}}`` — column MEMBERSHIP only, no types (cast targets live in the
SQL itself). Attribution mirrors the engine's own resolution semantics
(default database + qualified names), so the evaluator can never attribute a
column differently than Athena/Redshift will.

Rules are ADVISORY by design: a proven violation rides back to the model as
one system-reminder note per dataset (the query still executes) — the tier
never refuses. Its other job is judge-shard triage: decided policies
(violation or pass) are excluded from that query's judge shards.

Pure Python. ``sqlglot`` (>=30, the series this was developed against) is an
OPTIONAL dependency (``okf-core[sqlrules]``):
absent, every evaluation returns ``unknown`` and the judge fleet carries the
whole load — today's behavior exactly.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = [
    "DIMENSIONS",
    "RulesError",
    "parse_rules",
    "rule_label",
    "check_rules_schema",
    "self_test",
    "evaluate_policies",
    "sqlglot_available",
]


class RulesError(ValueError):
    """A ``rules:`` block that cannot be used, with a fix-it message."""


_TABLE_COL_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_NAME_RE = re.compile(r"^[a-z0-9_]+$")

_AGG_NAMES = ("sum", "avg", "max", "min", "count")
_USAGE_CONTEXTS = ("order", "aggregate", "compare", "arithmetic")
_PREDICATE_OPS = ("is_null", "is_not_null", "eq", "neq", "bounded")
_DEFAULT_CAST_TYPES = (
    "int", "bigint", "smallint", "tinyint", "double", "float", "decimal",
)

#: Author-friendly cast-type spellings -> the names sqlglot's DataType enum
#: reports (``cast.to.this.name``): authors write ANSI SQL (`INTEGER`,
#: `REAL`), the evaluator sees sqlglot's canonical vocabulary (REAL parses
#: as FLOAT, INTEGER as INT) — without this map those bindings could never
#: match and would read as decisive passes.
_CAST_TYPE_ALIASES = {
    "integer": "int",
    "real": "float",
    "numeric": "decimal",
}


def _func_key(name: str) -> str:
    """Function names compare underscore-insensitively.

    Authors write the Trino spelling (``date_diff``, ``regexp_extract``);
    sqlglot's parsed classes report squashed names (``DateDiff`` ->
    ``datediff``). Comparing raw strings silently unbinds every multi-word
    builtin.
    """
    return name.replace("_", "").lower()

#: The closed dimension catalog. The policy author may only BIND these —
#: an unknown dimension is a gate error, and a policy that fits none simply
#: stays prose-only (judge-fleet territory). Each dimension names the fields
#: :func:`parse_rules` accepts beyond the common ``dimension``/``examples``.
DIMENSIONS: dict[str, str] = {
    "forbidden_aggregation": (
        "an aggregate function over a column it corrupts (cumulative "
        "snapshots, running totals). Fields: targets [table.column, …], "
        "aggs [sum|avg|max|min|count] (default [sum, avg])."
    ),
    "forbidden_usage": (
        "a column used in a context its storage type makes wrong (text "
        "times ordered/compared/aggregated as durations). Fields: targets, "
        "contexts subset of [order, aggregate, compare, arithmetic] "
        "(default all)."
    ),
    "forbidden_function": (
        "a function applied to a column that must keep its precision or "
        "form (ROUND on point values, CAST-to-int). Fields: targets, "
        "functions [name, …] and/or cast_types [int|bigint|…]."
    ),
    "forbidden_grouping": (
        "a column that must never be a GROUP BY key (brand-scoped "
        "spellings, generation-scoped codes, consolidation roles). Fields: "
        "targets [table.column, …]. Any GROUP BY key touching the column "
        "counts (ordinals and expressions included); reading or filtering "
        "it stays legal, and SELECT DISTINCT is deliberately out of scope."
    ),
    "required_predicate": (
        "a table whose use (or aggregation) requires a WHERE conjunct "
        "(soft-deletes, status filters, sentinel exclusion, explicit period "
        "bounds). Fields: table, "
        "when [aggregate|use] (default aggregate), when_columns [col, …] "
        "(default: any column), require {column, op "
        "[is_null|is_not_null|eq|neq|bounded], value? (eq/neq only)}, "
        "or_group_by col?. `bounded` = any positive equality/range/IN "
        "conjunct on the column, whatever the value — the op for 'must pin "
        "an explicit window' policies."
    ),
    "required_guard": (
        "a numeric CAST of a mixed-content text column that must be guarded "
        "(regexp_like) or TRY_CAST. Fields: targets, cast_types (default "
        "numeric set), guard_functions (default [regexp_like])."
    ),
    "forbidden_sequencing_key": (
        "a column that must never define order because it is not "
        "chronological/sequential: a window ORDER BY, or `ORDER BY … LIMIT 1` "
        "as a \"latest row\" probe (a plain ORDER BY with any other LIMIT is "
        "presentational — the run_sql contract asks for a LIMIT on every "
        "query — and never violates). Fields: targets."
    ),
    "required_distinct": (
        "a COUNT that fans out at the table's grain and must be "
        "COUNT(DISTINCT …). Fields: table, group_by col, count_distinct col, "
        "when_filtered {column, op, value?} (optional trigger — without it "
        "the rule fires on EVERY plain count at that grouping, so bind it "
        "unless any such count is genuinely wrong)."
    ),
}


# ---------------------------------------------------------------------------
# Parsing / shape validation (pure — no sqlglot needed)
# ---------------------------------------------------------------------------


def _norm_name(value: Any, field: str, where: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value.strip().lower()):
        raise RulesError(
            f"{where} `{field}` {value!r} must be a bare lowercase "
            "table/column identifier"
        )
    return value.strip().lower()


def _norm_targets(raw: Any, where: str) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise RulesError(f"{where} needs a non-empty `targets` list")
    out: list[str] = []
    for t in raw:
        if not isinstance(t, str) or not _TABLE_COL_RE.fullmatch(t.strip().lower()):
            raise RulesError(
                f"{where} target {t!r} must be `table.column` (lowercase)"
            )
        norm = t.strip().lower()
        if norm not in out:
            out.append(norm)
    return out


def _norm_choice_list(
    raw: Any, field: str, allowed: tuple[str, ...], default: list[str], where: str
) -> list[str]:
    if raw is None:
        return list(default)
    if not isinstance(raw, list) or not raw:
        raise RulesError(f"{where} `{field}` must be a non-empty list when given")
    out: list[str] = []
    for v in raw:
        s = str(v).strip().lower()
        if s not in allowed:
            raise RulesError(
                f"{where} `{field}` value {v!r} must be one of: "
                + ", ".join(allowed)
            )
        if s not in out:
            out.append(s)
    return out


def _norm_examples(raw: Any, where: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise RulesError(
            f"{where} needs `examples` with a `violation` and a `pass` SQL "
            "statement — they are the rule's self-test"
        )
    out: dict[str, str] = {}
    for key in ("violation", "pass"):
        v = raw.get(key)
        if not isinstance(v, str) or not v.strip():
            raise RulesError(f"{where} `examples.{key}` must be a SQL statement")
        out[key] = " ".join(v.split())
    return out


def parse_rules(raw: Any, *, where: str = "rules") -> list[dict[str, Any]]:
    """Normalize one policy's ``rules:`` list. Shape-only; raises :class:`RulesError`.

    Returned rules are plain dicts with every name lowercased and defaults
    filled — the exact material the evaluator consumes. Messages are written
    for the AUTHOR (the gate forwards them).
    """
    if not isinstance(raw, list) or not raw:
        raise RulesError(f"{where} must be a non-empty list of rule mappings")
    rules: list[dict[str, Any]] = []
    for i, r in enumerate(raw):
        rw = f"{where}[{i}]"
        if not isinstance(r, dict):
            raise RulesError(f"{rw} is not a mapping")
        dim = str(r.get("dimension") or "").strip().lower()
        if dim not in DIMENSIONS:
            raise RulesError(
                f"{rw} dimension {r.get('dimension')!r} is not in the catalog: "
                + ", ".join(sorted(DIMENSIONS))
            )
        rule: dict[str, Any] = {"dimension": dim}
        if dim == "forbidden_aggregation":
            rule["targets"] = _norm_targets(r.get("targets"), rw)
            rule["aggs"] = _norm_choice_list(
                r.get("aggs"), "aggs", _AGG_NAMES, ["sum", "avg"], rw
            )
        elif dim == "forbidden_usage":
            rule["targets"] = _norm_targets(r.get("targets"), rw)
            rule["contexts"] = _norm_choice_list(
                r.get("contexts"), "contexts", _USAGE_CONTEXTS,
                list(_USAGE_CONTEXTS), rw,
            )
        elif dim == "forbidden_function":
            rule["targets"] = _norm_targets(r.get("targets"), rw)
            funcs = r.get("functions") or []
            if not isinstance(funcs, list):
                raise RulesError(f"{rw} `functions` must be a list")
            rule["functions"] = [
                _norm_name(f, "functions", rw) for f in funcs
            ]
            casts = r.get("cast_types") or []
            if not isinstance(casts, list):
                raise RulesError(f"{rw} `cast_types` must be a list")
            rule["cast_types"] = [
                _CAST_TYPE_ALIASES.get(n, n)
                for n in (_norm_name(c, "cast_types", rw) for c in casts)
            ]
            if not rule["functions"] and not rule["cast_types"]:
                raise RulesError(
                    f"{rw} needs `functions` and/or `cast_types` (both empty)"
                )
        elif dim == "required_predicate":
            rule["table"] = _norm_name(r.get("table"), "table", rw)
            when = str(r.get("when") or "aggregate").strip().lower()
            if when not in ("aggregate", "use"):
                raise RulesError(f"{rw} `when` must be `aggregate` or `use`")
            rule["when"] = when
            wc = r.get("when_columns") or []
            if not isinstance(wc, list):
                raise RulesError(f"{rw} `when_columns` must be a list")
            rule["when_columns"] = [_norm_name(c, "when_columns", rw) for c in wc]
            req = r.get("require")
            if not isinstance(req, dict):
                raise RulesError(
                    f"{rw} needs `require` with `column` and `op` "
                    f"({'|'.join(_PREDICATE_OPS)})"
                )
            op = str(req.get("op") or "").strip().lower()
            if op not in _PREDICATE_OPS:
                raise RulesError(
                    f"{rw} `require.op` must be one of: " + ", ".join(_PREDICATE_OPS)
                )
            rule["require"] = {
                "column": _norm_name(req.get("column"), "require.column", rw),
                "op": op,
            }
            if op in ("eq", "neq"):
                value = req.get("value")
                if value is None or (isinstance(value, str) and not value.strip()):
                    raise RulesError(f"{rw} `require.value` is required for {op}")
                rule["require"]["value"] = str(value).strip()
            if r.get("or_group_by") is not None:
                rule["or_group_by"] = _norm_name(
                    r.get("or_group_by"), "or_group_by", rw
                )
        elif dim == "required_guard":
            rule["targets"] = _norm_targets(r.get("targets"), rw)
            casts = r.get("cast_types")
            if casts is None:
                rule["cast_types"] = list(_DEFAULT_CAST_TYPES)
            else:
                if not isinstance(casts, list) or not casts:
                    raise RulesError(f"{rw} `cast_types` must be a non-empty list")
                rule["cast_types"] = [
                    _CAST_TYPE_ALIASES.get(n, n)
                    for n in (_norm_name(c, "cast_types", rw) for c in casts)
                ]
            guards = r.get("guard_functions")
            if guards is None:
                rule["guard_functions"] = ["regexp_like"]
            else:
                if not isinstance(guards, list) or not guards:
                    raise RulesError(
                        f"{rw} `guard_functions` must be a non-empty list"
                    )
                rule["guard_functions"] = [
                    _norm_name(g, "guard_functions", rw) for g in guards
                ]
        elif dim == "forbidden_grouping":
            rule["targets"] = _norm_targets(r.get("targets"), rw)
        elif dim == "forbidden_sequencing_key":
            rule["targets"] = _norm_targets(r.get("targets"), rw)
        elif dim == "required_distinct":
            rule["table"] = _norm_name(r.get("table"), "table", rw)
            rule["group_by"] = _norm_name(r.get("group_by"), "group_by", rw)
            rule["count_distinct"] = _norm_name(
                r.get("count_distinct"), "count_distinct", rw
            )
            trigger = r.get("when_filtered")
            if trigger is not None:
                if not isinstance(trigger, dict):
                    raise RulesError(
                        f"{rw} `when_filtered` must be a mapping with "
                        "`column`, `op`, and (for eq/neq) `value`"
                    )
                op = str(trigger.get("op") or "").strip().lower()
                if op not in _PREDICATE_OPS:
                    raise RulesError(
                        f"{rw} `when_filtered.op` must be one of: "
                        + ", ".join(_PREDICATE_OPS)
                    )
                norm_trigger: dict[str, Any] = {
                    "column": _norm_name(
                        trigger.get("column"), "when_filtered.column", rw
                    ),
                    "op": op,
                }
                if op in ("eq", "neq"):
                    value = trigger.get("value")
                    if value is None or (
                        isinstance(value, str) and not value.strip()
                    ):
                        raise RulesError(
                            f"{rw} `when_filtered.value` is required for {op}"
                        )
                    norm_trigger["value"] = str(value).strip()
                rule["when_filtered"] = norm_trigger
        rule["examples"] = _norm_examples(r.get("examples"), rw)
        rules.append(rule)
    return rules


def rule_label(rule: dict[str, Any]) -> str:
    """One rule as a compact, log-friendly label: ``dimension(bindings)``.

    Used in the runtime's trace lines and the UI, so an operator reading logs
    can tell two rules of the same dimension apart.
    """
    bindings = list(rule.get("targets") or [])
    table = rule.get("table")
    if table:
        for field in ("group_by", "count_distinct", "or_group_by"):
            if rule.get(field):
                bindings.append(f"{table}.{rule[field]}")
        req = rule.get("require") or {}
        if req:
            bindings.append(f"{table} requires {req['column']} {req['op']}")
        if not bindings:
            bindings.append(table)
    return f"{rule['dimension']}({', '.join(bindings)})"


def check_rules_schema(
    rules: list[dict[str, Any]], schema: dict[str, dict[str, list[str]]]
) -> str | None:
    """The contract layer: every table/column a rule binds must exist.

    ``schema`` is the sidecar's ``databases`` mapping. None when acceptable,
    else what to fix (author-facing). Tables are matched across ALL of the
    dataset's databases; a table name present in several is still fine —
    bindings are table-relative by design (see module doc).
    """
    tables: dict[str, set[str]] = {}
    for _db, tbls in (schema or {}).items():
        for t, cols in tbls.items():
            tables.setdefault(t.lower(), set()).update(c.lower() for c in cols)
    if not tables:
        return (
            "no rules schema is available for this dataset — omit `rules:` "
            "blocks entirely"
        )

    def _check(table: str, column: str | None, where: str) -> str | None:
        if table not in tables:
            return f"{where} references unknown table `{table}`"
        if column is not None and column not in tables[table]:
            return f"{where} references unknown column `{table}.{column}`"
        return None

    for i, rule in enumerate(rules):
        where = f"rules[{i}] ({rule['dimension']})"
        for target in rule.get("targets") or []:
            t, c = target.split(".", 1)
            err = _check(t, c, where)
            if err:
                return err
        table = rule.get("table")
        if table:
            err = _check(table, None, where)
            if err:
                return err
            for c in rule.get("when_columns") or []:
                err = _check(table, c, where)
                if err:
                    return err
            for field in ("or_group_by", "group_by", "count_distinct"):
                if rule.get(field):
                    err = _check(table, rule[field], where)
                    if err:
                        return err
            for spec in (rule.get("require"), rule.get("when_filtered")):
                if spec:
                    err = _check(table, spec["column"], where)
                    if err:
                        return err
    return None


# ---------------------------------------------------------------------------
# Evaluation (sqlglot — optional; absent means everything is `unknown`)
# ---------------------------------------------------------------------------


def sqlglot_available() -> bool:
    try:
        import sqlglot  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 - any import failure means "not usable"
        return False


def _unknown_all(
    policies: list[tuple[str, list[dict[str, Any]]]], reason: str
) -> dict[str, dict[str, Any]]:
    return {
        pid: {"verdict": "unknown", "details": [reason]} for pid, _ in policies
    }


def evaluate_policies(
    sql: str,
    policies: list[tuple[str, list[dict[str, Any]]]],
    schema: dict[str, dict[str, list[str]]],
    *,
    databases: set[str] | None = None,
    default_database: str | None = None,
    dialect: str = "trino",
) -> dict[str, dict[str, Any]]:
    """Evaluate one dataset's rule-bearing policies against one SQL statement.

    ``policies`` is ``[(policy_id, parsed_rules), …]``; ``schema`` the MERGED
    ``{db: {table: [cols]}}`` qualification schema (may span datasets on a
    cross-dataset query); ``databases`` the set of database names THIS
    dataset's rules may bind (default: all of ``schema``). Returns
    ``{policy_id: {"verdict": violation|pass|unknown, "details": [str, …]}}``.

    Per policy: any proven rule violation → ``violation``; else any
    undecidable rule → ``unknown``; else ``pass``. Never raises — every
    failure mode degrades to ``unknown``.
    """
    if not policies:
        return {}
    try:
        import sqlglot
        from sqlglot import exp
        from sqlglot.optimizer.qualify import qualify
        from sqlglot.optimizer.scope import build_scope
    except Exception:  # noqa: BLE001 - optional dep absent
        return _unknown_all(policies, "sqlglot is not installed")

    own_dbs = {d.lower() for d in (databases if databases is not None else schema)}
    qual_schema = {
        db: {t: {c: "unknown" for c in cols} for t, cols in tbls.items()}
        for db, tbls in (schema or {}).items()
    }
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
        if isinstance(tree, exp.Command):
            # EXPLAIN/SHOW/DESCRIBE parse as an opaque Command — the inner
            # statement is a bare string, so nothing can be PROVEN about it.
            # `unknown` (not `pass`): silence here would read as "checked and
            # clean" for a query the parser never actually looked inside.
            return _unknown_all(
                policies, "statement is opaque to the parser (EXPLAIN/SHOW/…)"
            )
        tree = qualify(
            tree,
            schema=qual_schema,
            db=default_database,
            dialect=dialect,
        )
        root = build_scope(tree)
        scopes = list(root.traverse()) if root is not None else []
    except Exception as e:  # noqa: BLE001 - unresolvable SQL defers to judges
        return _unknown_all(
            policies, f"could not resolve the query: {type(e).__name__}: {e}"
        )

    ctx = _QueryContext(exp, scopes, own_dbs, default_database)
    out: dict[str, dict[str, Any]] = {}
    for pid, rules in policies:
        violations: list[str] = []
        unknowns: list[str] = []
        # Per-rule breakdown, always populated: the runtime logs it verbatim so
        # an operator can see WHICH rule fired (and why the others didn't)
        # without re-running the query.
        breakdown: list[dict[str, Any]] = []
        for i, rule in enumerate(rules):
            try:
                verdict, detail = _EVALUATORS[rule["dimension"]](ctx, rule)
            except Exception as e:  # noqa: BLE001 - a rule bug must not refuse
                verdict, detail = "unknown", f"rule evaluation failed: {e}"
            breakdown.append(
                {
                    "index": i,
                    "label": rule_label(rule),
                    "dimension": rule["dimension"],
                    "verdict": verdict,
                    "detail": detail,
                }
            )
            if verdict == "violation":
                violations.append(detail)
            elif verdict == "unknown":
                unknowns.append(detail)
        if violations:
            verdict = "violation"
        elif unknowns:
            verdict = "unknown"
        else:
            verdict = "pass"
        out[pid] = {
            "verdict": verdict,
            "details": violations or unknowns or [],
            "rules": breakdown,
        }
    return out


class _QueryContext:
    """Resolved column occurrences + per-scope material, computed once."""

    def __init__(self, exp: Any, scopes: list[Any], own_dbs: set[str], default_db):
        self.exp = exp
        # Only SELECT-shaped scopes carry the projections/WHERE/ORDER material
        # every evaluator reads (a UNION's own scope is a SetOperation whose
        # branches traverse as their own scopes anyway).
        self.scopes = [
            s for s in scopes if isinstance(s.expression, exp.Select)
        ]
        self.own_dbs = own_dbs
        self.default_db = (default_db or "").lower() or None
        # (db, table, column, column_node, scope) for every provably-resolved
        # column occurrence; unresolved column NAMES (flowing through derived
        # sources) drive the conservative `unknown` overlap check.
        self.resolved: list[tuple[str, str, str, Any, Any]] = []
        self.unresolved_names: set[str] = set()
        # A set operation's ORDER BY hangs off the SetOperation scope (filtered
        # out above) and references the branches' output names — attribution to
        # one branch is not provable here, so any overlap with a rule's target
        # names must read `unknown` rather than a decisive pass.
        self.setop_order_names: set[str] = set()
        for scope in scopes:
            if isinstance(scope.expression, exp.Select):
                continue
            order = scope.expression.args.get("order")
            if order is not None:
                self.setop_order_names.update(
                    c.name.lower() for c in order.find_all(exp.Column)
                )
        # A derived scope's projection that RENAMES a column (`points AS pts`
        # in a CTE, then SUM(pts) outside) is invisible to the name-overlap
        # check — without this map it would read as a decisive `pass` and
        # exclude the policy from the judge shard too, escaping both tiers.
        # alias -> every resolved (table, column) that feeds it, recorded for
        # ANY column in the aliased expression (`points + 0 AS pts` counts).
        self.export_aliases: dict[str, set[tuple[str, str]]] = {}
        for scope in self.scopes:
            columns = list(scope.columns)
            order = scope.expression.args.get("order")
            if order is not None:
                # qualify() rewrites ORDER BY to reference projection
                # aliases (no table attr) — those never appear in
                # scope.columns, so walk them explicitly and resolve via the
                # projection map below.
                columns += [c for c in order.find_all(exp.Column) if not c.table]
            aliases = self._projection_map(scope)
            for col in columns:
                triple = self._resolve(col, scope)
                if triple is None and not col.table and col.name in aliases:
                    triple = aliases[col.name]
                if triple is None:
                    self.unresolved_names.add(col.name.lower())
                    continue
                db, table, name = triple
                self.resolved.append((db, table, name, col, scope))
            if scope.is_root:
                continue
            for proj in scope.expression.selects:
                if not isinstance(proj, exp.Alias):
                    continue
                exported = self.export_aliases.setdefault(
                    proj.alias_or_name.lower(), set()
                )
                for col in proj.this.find_all(exp.Column):
                    triple = self._resolve(col, scope)
                    if triple is not None:
                        exported.add((triple[1], triple[2]))

    def _resolve(self, column: Any, scope: Any):
        src = scope.sources.get(column.table)
        if isinstance(src, self.exp.Table):
            db = (src.text("db") or "").lower() or self.default_db
            return (db or "", src.name.lower(), column.name.lower())
        return None

    def _projection_map(self, scope: Any) -> dict[str, tuple[str, str, str]]:
        out: dict[str, tuple[str, str, str]] = {}
        for proj in scope.expression.selects:
            col = proj.unalias() if isinstance(proj, self.exp.Alias) else proj
            if isinstance(col, self.exp.Column):
                triple = self._resolve(col, scope)
                if triple:
                    out[proj.alias_or_name] = triple
        return out

    # -- shared helpers ---------------------------------------------------------

    def occurrences(self, targets: list[str]):
        """Resolved occurrences of `table.column` targets within OWN databases."""
        wanted = {tuple(t.split(".", 1)) for t in targets}
        return [
            (db, table, name, node, scope)
            for db, table, name, node, scope in self.resolved
            if (table, name) in wanted and (not db or db in self.own_dbs)
        ]

    def overlap_unknown(self, targets: set[tuple[str, str]]) -> bool:
        """Whether unresolved (derived-source) columns could hide a target.

        Matches by column NAME, widened by every derived-scope export alias
        that provably wraps a target column — so a rename inside a CTE or
        subquery reads `unknown`, never a decisive pass. Multi-hop renames
        chain for free: the intermediate reference is itself unresolved and
        collides with the first hop's alias.
        """
        names = {c for _, c in targets}
        for alias, exported in self.export_aliases.items():
            if exported & targets:
                names.add(alias)
        return bool(self.unresolved_names & names)

    def setop_order_overlap(self, column_names: set[str]) -> bool:
        """Whether a set-operation ORDER BY could be sorting by a target."""
        return bool(self.setop_order_names & column_names)

    def base_tables(self, scope: Any) -> dict[str, str]:
        """{alias/table-key: table name} for OWN-database base tables in scope."""
        out: dict[str, str] = {}
        for key, src in scope.sources.items():
            if isinstance(src, self.exp.Table):
                db = (src.text("db") or "").lower() or self.default_db
                if not db or db in self.own_dbs:
                    out[key] = src.name.lower()
        return out

    def _collect_conjuncts(self, node: Any, out: list[Any]) -> bool:
        """Flatten an AND-tree into ``out``; False iff an OR makes it undecidable.

        Parentheses are unwrapped at every level (``unnest``): agents write
        ``WHERE (status IS NULL)`` and ``WHERE (a AND b) AND c`` constantly,
        and a ``Paren`` wrapper left in place would hide a satisfied predicate
        (false refusal) or, worse, hide an OR and turn an undecidable query
        into a refusal.
        """
        exp = self.exp
        node = node.unnest()
        if isinstance(node, exp.And):
            return self._collect_conjuncts(node.this, out) and self._collect_conjuncts(
                node.expression, out
            )
        if isinstance(node, exp.Or):
            return False
        out.append(node)
        return True

    def conjuncts(self, scope: Any) -> tuple[list[Any], bool]:
        """``(proven conjuncts, complete)`` of WHERE + INNER JOIN ON clauses.

        Every returned conjunct genuinely holds for the scope's rows, so
        finding a required predicate among them PROVES it. ``complete`` is
        False when an OR (or an outer join around a flagged table) means the
        list may be missing conjuncts — then only a positive find is
        conclusive; an absence reads ``unknown``, never a violation.
        """
        out: list[Any] = []
        complete = True
        where = scope.expression.args.get("where")
        if where is not None:
            complete = self._collect_conjuncts(where.this, out)
        # HAVING on a grouped key is row-filter-equivalent (`GROUP BY x
        # HAVING x = 1` == `WHERE x = 1 GROUP BY x`) — excluding it turned
        # that spelling into a false proven violation.
        having = scope.expression.args.get("having")
        if having is not None:
            complete = self._collect_conjuncts(having.this, out) and complete
        for join in scope.expression.args.get("joins") or []:
            side = (join.side or "").upper()
            kind = (join.kind or "").upper()
            if side in ("LEFT", "RIGHT", "FULL") or kind == "CROSS":
                continue
            on = join.args.get("on")
            if on is None:
                continue
            # A disjunctive ON proves nothing about the preserved rows, but it
            # can't poison the WHERE's own proof either — collect what holds.
            self._collect_conjuncts(on, out)
        return out, complete


def _agg_ancestor(exp: Any, node: Any):
    return node.find_ancestor(exp.AggFunc)


_AGG_CLASSES = {
    "sum": "Sum",
    "avg": "Avg",
    "max": "Max",
    "min": "Min",
    "count": "Count",
}


def _eval_forbidden_aggregation(ctx: _QueryContext, rule: dict[str, Any]):
    exp = ctx.exp
    classes = tuple(
        getattr(exp, _AGG_CLASSES[a]) for a in rule["aggs"] if a in _AGG_CLASSES
    )
    for db, table, name, node, _scope in ctx.occurrences(rule["targets"]):
        agg = _agg_ancestor(exp, node)
        if agg is not None and isinstance(agg, classes):
            return (
                "violation",
                f"{type(agg).__name__.upper()}({table}.{name})",
            )
    if ctx.overlap_unknown({tuple(t.split(".", 1)) for t in rule["targets"]}):
        return "unknown", "target column may flow through a derived source"
    return "pass", ""


def _usage_context(exp: Any, node: Any) -> str | None:
    """The NEAREST enclosing usage context, or None.

    Proximity matters: an explicit ``CAST`` lifts the column out of its stored
    (text) form, so it is sanctioned — but only for what sits INSIDE the cast.
    ``CAST(MIN(time) AS varchar)`` still aggregates the raw text column, and
    an any-ancestor cast check would wave it through (a decisive `pass` that
    also excludes the policy from the judge shard — escaping both tiers).
    """
    anc = node.find_ancestor(
        exp.Cast, exp.Ordered, exp.AggFunc, exp.Binary, exp.Between
    )
    # String concatenation is TRANSPARENT, not an anchor: `MIN(time || 'x')`
    # still aggregates the stored text form. Leaving `||` as the nearest
    # Binary would return None (sanctioned) and hide the enclosing context.
    while anc is not None and isinstance(anc, exp.DPipe):
        anc = anc.find_ancestor(
            exp.Cast, exp.Ordered, exp.AggFunc, exp.Binary, exp.Between
        )
    if anc is None or isinstance(anc, exp.Cast):
        return None
    if isinstance(anc, exp.Ordered):
        return "order"
    if isinstance(anc, exp.AggFunc):
        return "aggregate"
    if isinstance(anc, (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)):
        return "compare"
    if isinstance(anc, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
        return "arithmetic"
    return None


def _eval_forbidden_usage(ctx: _QueryContext, rule: dict[str, Any]):
    exp = ctx.exp
    names = {t.split(".", 1)[1] for t in rule["targets"]}
    for db, table, name, node, _scope in ctx.occurrences(rule["targets"]):
        context = _usage_context(exp, node)
        if context and context in rule["contexts"]:
            return "violation", f"{table}.{name} used in {context} context"
    if "order" in rule["contexts"] and ctx.setop_order_overlap(names):
        return "unknown", "a set-operation ORDER BY may sort by this column"
    if ctx.overlap_unknown({tuple(t.split(".", 1)) for t in rule["targets"]}):
        return "unknown", "target column may flow through a derived source"
    return "pass", ""


def _eval_forbidden_function(ctx: _QueryContext, rule: dict[str, Any]):
    exp = ctx.exp
    cast_types = set(rule["cast_types"])
    wanted_funcs = {_func_key(f) for f in rule["functions"]}
    for db, table, name, node, _scope in ctx.occurrences(rule["targets"]):
        func = node.find_ancestor(exp.Func)
        while func is not None:
            fname = (
                func.name.lower()
                if isinstance(func, exp.Anonymous)
                else type(func).__name__.lower()
            )
            if _func_key(fname) in wanted_funcs:
                return "violation", f"{fname.upper()}({table}.{name})"
            func = func.find_ancestor(exp.Func)
        if cast_types:
            # Walk the WHOLE cast chain: an inner TRY_CAST must not shield an
            # outer forbidden cast — CAST(TRY_CAST(x AS DOUBLE) AS INT) still
            # applies the precision-destroying conversion.
            cast = node.find_ancestor(exp.Cast)
            while cast is not None:
                if not isinstance(cast, exp.TryCast) and (
                    cast.to.this.name.lower() in cast_types
                ):
                    return (
                        "violation",
                        f"CAST({table}.{name} AS {cast.to.this.name.lower()})",
                    )
                cast = cast.find_ancestor(exp.Cast)
    if ctx.overlap_unknown({tuple(t.split(".", 1)) for t in rule["targets"]}):
        return "unknown", "target column may flow through a derived source"
    return "pass", ""


def _eval_required_guard(ctx: _QueryContext, rule: dict[str, Any]):
    exp = ctx.exp
    guard_keys = {_func_key(g) for g in rule["guard_functions"]}
    cast_types = set(rule["cast_types"])
    wanted = {tuple(t.split(".", 1)) for t in rule["targets"]}

    def _guards_target(func: Any, scope: Any) -> bool:
        fname = (
            func.name if isinstance(func, exp.Anonymous) else type(func).__name__
        )
        if _func_key(fname) not in guard_keys:
            return False
        for c in func.find_all(exp.Column):
            triple = ctx._resolve(c, scope)
            if triple and (triple[1], triple[2]) in wanted:
                return True
        return False

    def _under_not(node: Any, stop: Any) -> bool:
        parent = node.parent
        while parent is not None and parent is not stop:
            if isinstance(parent, exp.Not):
                return True
            parent = parent.parent
        return False

    for db, table, name, node, scope in ctx.occurrences(rule["targets"]):
        # Walk the cast chain: a TRY_CAST anywhere below the numeric cast IS
        # the guard (the sanctioned form — it absorbs the parse failure), and
        # a non-numeric intermediate (CAST(CAST(x AS VARCHAR) AS INT)) must
        # not shield the numeric cast above it.
        cast = node.find_ancestor(exp.Cast)
        numeric_cast = None
        while cast is not None:
            if isinstance(cast, exp.TryCast):
                break
            if cast.to.this.name.lower() in cast_types:
                numeric_cast = cast
                break
            cast = cast.find_ancestor(exp.Cast)
        if numeric_cast is None:
            continue
        guarded = False
        # (1) A guard in the enclosing CASE/IF, not under NOT.
        anchor = numeric_cast.find_ancestor(exp.Case, exp.If)
        if anchor is not None:
            for f in anchor.find_all(exp.Func):
                if _guards_target(f, scope) and not _under_not(f, anchor):
                    guarded = True
        # (2) A guard as a POSITIVE conjunct of WHERE + INNER JOIN ON — the
        # same filter material required_predicate trusts as proof. A guard
        # inside an OR branch or under NOT does not hold for every row that
        # reaches the cast, so it proves nothing here.
        conjuncts, _complete = ctx.conjuncts(scope)
        for c in conjuncts:
            if isinstance(c, exp.Func) and _guards_target(c, scope):
                guarded = True
        if guarded:
            continue
        # Distinguish "provably unguarded" (no guard call on the target
        # anywhere in the scope) from "a guard exists but is not provably
        # applied" (OR branch, NOT, a CASE elsewhere) — only the former is
        # a proven violation; the latter defers to the judges.
        if any(
            _guards_target(f, scope)
            for f in scope.expression.find_all(exp.Func)
        ):
            return (
                "unknown",
                "a guard exists but is not provably applied to every row "
                "reaching the cast",
            )
        return (
            "violation",
            f"unguarded CAST({table}.{name} AS numeric) — guard with "
            + "/".join(sorted(rule["guard_functions"]))
            + " or use TRY_CAST",
        )
    if ctx.overlap_unknown({tuple(t.split(".", 1)) for t in rule["targets"]}):
        return "unknown", "target column may flow through a derived source"
    return "pass", ""


def _is_single_row_limit(exp: Any, scope: Any) -> bool:
    """True iff the scope ends in ``LIMIT 1`` — the "latest row" probe.

    Deliberately ONLY ``1``: the run_sql tool description instructs the model
    to put a LIMIT on every query, so treating any LIMIT as a sequencing probe
    would refuse ordinary paged listings that merely order by the column.
    """
    limit = scope.expression.args.get("limit")
    if limit is None:
        return False
    value = limit.expression if hasattr(limit, "expression") else None
    return isinstance(value, exp.Literal) and str(value.name).strip() == "1"


def _eval_forbidden_sequencing_key(ctx: _QueryContext, rule: dict[str, Any]):
    exp = ctx.exp
    names = {t.split(".", 1)[1] for t in rule["targets"]}
    for db, table, name, node, scope in ctx.occurrences(rule["targets"]):
        ordered = node.find_ancestor(exp.Ordered)
        if ordered is None:
            continue
        if node.find_ancestor(exp.Window) is not None:
            return (
                "violation",
                f"window ordered by {table}.{name} — not a chronological key",
            )
        if _is_single_row_limit(exp, scope):
            return (
                "violation",
                f"ORDER BY {table}.{name} … LIMIT 1 — not a chronological key",
            )
    if ctx.setop_order_overlap(names):
        return "unknown", "a set-operation ORDER BY may sort by this column"
    if ctx.overlap_unknown({tuple(t.split(".", 1)) for t in rule["targets"]}):
        return "unknown", "target column may flow through a derived source"
    return "pass", ""


def _literal_text(exp: Any, node: Any) -> str | None:
    node = node.unnest() if hasattr(node, "unnest") else node
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return "-" + node.this.name
    if isinstance(node, exp.Literal):
        return node.name
    if isinstance(node, exp.Boolean):
        return node.sql().lower()
    return None


def _values_equal(a: str, b: str) -> bool:
    """Engine-faithful literal equality.

    Numbers compare numerically (`0` == `0.0`, `-1` == `-1`), booleans
    case-insensitively (TRUE/true are the same token), strings EXACTLY —
    Athena/Trino string comparison is case-sensitive, so `status = 'd'`
    must NOT satisfy a required `status = 'D'`.
    """
    try:
        return Decimal(a) == Decimal(b)
    except (InvalidOperation, ValueError, ArithmeticError):
        pass
    if a.lower() in ("true", "false") or b.lower() in ("true", "false"):
        return a.lower() == b.lower()
    return a == b


def _comparison_on(
    exp: Any, ctx, scope, node: Any, table: str, column: str, *,
    bounding: bool = False,
) -> bool:
    """Whether ``node`` is a comparison conjunct with the column as a DIRECT
    operand. ``bounding=True`` narrows to shapes that pin/bound the value
    (equality, range, BETWEEN, IN) — NEQ and LIKE exclude/match but do not
    bound. Directness matters: a column under COALESCE is null-absorbed and
    proves nothing."""

    def _is_col(n: Any) -> bool:
        n = n.unnest() if hasattr(n, "unnest") else n
        if not isinstance(n, exp.Column):
            return False
        triple = ctx._resolve(n, scope)
        return bool(triple and triple[1] == table and triple[2] == column)

    if isinstance(node, (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        return _is_col(node.this) or _is_col(node.expression)
    if isinstance(node, exp.NEQ) and not bounding:
        return _is_col(node.this) or _is_col(node.expression)
    if isinstance(node, exp.Between):
        return _is_col(node.this)
    if isinstance(node, exp.In):
        return _is_col(node.this)
    if isinstance(node, exp.Like) and not bounding:
        return _is_col(node.this)
    return False


def _predicate_matches(exp: Any, ctx, scope, conjunct, table: str, req: dict) -> bool:
    def _resolved_col(node):
        if isinstance(node, exp.Column):
            triple = ctx._resolve(node, scope)
            if triple and triple[1] == table and triple[2] == req["column"]:
                return True
        return False

    # Peel NOT wrappers (unwrapping parens inside them), tracking polarity:
    # `NOT (col = v)` is `col <> v`, `NOT (col IS NULL)` is `col IS NOT NULL`.
    node, negated = conjunct, False
    while isinstance(node, exp.Not):
        negated = not negated
        node = node.this.unnest()

    op = req["op"]
    if op in ("is_null", "is_not_null"):
        if (
            isinstance(node, exp.Is)
            and isinstance(node.expression, exp.Null)
            and negated == (op == "is_not_null")
            and _resolved_col(node.this)
        ):
            return True
        # Comparison conjuncts IMPLY not-null: NULL fails every comparison,
        # and NOT over a NULL comparison is still NULL, so ANY row passing
        # `col = v` / `col BETWEEN …` / `NOT (col = v)` has a provably
        # non-null col. Shape-matching alone false-flagged exactly the
        # bounded queries a "must filter this column" policy exists for.
        if op == "is_not_null":
            return _comparison_on(exp, ctx, scope, node, table, req["column"])
        return False
    if op == "bounded":
        # An explicit pin/window on the column, whatever the value —
        # negation UN-bounds (`NOT (d BETWEEN …)` admits everything else).
        return not negated and _comparison_on(
            exp, ctx, scope, node, table, req["column"], bounding=True
        )
    if op in ("eq", "neq"):
        if not isinstance(node, (exp.EQ, exp.NEQ)):
            return False
        actual = "eq" if isinstance(node, exp.EQ) else "neq"
        if negated:
            actual = "neq" if actual == "eq" else "eq"
        if actual != op:
            return False
        sides = [node.this, node.expression]
        for a, b in (sides, sides[::-1]):
            if _resolved_col(a):
                literal = _literal_text(exp, b)
                if literal is not None and _values_equal(literal, req["value"]):
                    return True
    return False


def _eval_forbidden_grouping(ctx: _QueryContext, rule: dict[str, Any]):
    """GROUP BY keys only — reading/filtering the column stays legal.

    Ordinals (`GROUP BY 1`) resolve through the projection list (qualify
    rewrites alias references to their expressions already), and an
    EXPRESSION over the target (`substr(market, 1, 1)`) still groups by it.
    """
    exp = ctx.exp
    wanted = {tuple(t.split(".", 1)) for t in rule["targets"]}
    unresolved_keys: set[str] = set()
    for scope in ctx.scopes:
        group = scope.expression.args.get("group")
        if group is None:
            continue
        aliases = ctx._projection_map(scope)
        selects = scope.expression.selects
        for g in group.expressions:
            g = g.unnest()
            if isinstance(g, exp.Literal) and str(g.name).isdigit():
                i = int(g.name) - 1
                if not 0 <= i < len(selects):
                    continue
                g = selects[i]
                g = g.unalias() if isinstance(g, exp.Alias) else g
            for col in g.find_all(exp.Column):
                triple = ctx._resolve(col, scope)
                if triple is None and not col.table and col.name in aliases:
                    triple = aliases[col.name]
                if triple is None:
                    unresolved_keys.add(col.name.lower())
                    continue
                db, table, name = triple
                if (table, name) in wanted and (not db or db in ctx.own_dbs):
                    return "violation", f"GROUP BY {table}.{name}"
    # A grouping key that resolves to a derived source could be a renamed
    # export of the target — widen by export aliases, exactly like
    # overlap_unknown, but scoped to the GROUP BY keys.
    names = {c for _, c in wanted}
    for alias, exported in ctx.export_aliases.items():
        if exported & wanted:
            names.add(alias)
    if unresolved_keys & names:
        return "unknown", "a grouping key may flow through a derived source"
    return "pass", ""


def _eval_required_predicate(ctx: _QueryContext, rule: dict[str, Any]):
    exp = ctx.exp
    table = rule["table"]
    when_cols = set(rule["when_columns"])
    triggered_scopes: list[Any] = []
    for db, tbl, name, node, scope in ctx.resolved:
        if tbl != table or (db and db not in ctx.own_dbs):
            continue
        if when_cols and name not in when_cols:
            continue
        if rule["when"] == "aggregate" and _agg_ancestor(exp, node) is None:
            continue
        if scope not in triggered_scopes:
            triggered_scopes.append(scope)
    if not when_cols:
        # Table PRESENCE triggers too: `COUNT(*) FROM t` resolves no column
        # yet aggregates exactly the rows the required filter exists to
        # exclude — column occurrences alone would read it as untriggered
        # and return a false decisive pass. (`when_columns` keeps the
        # narrower authored trigger when given.)
        for scope in ctx.scopes:
            if scope in triggered_scopes:
                continue
            if table not in ctx.base_tables(scope).values():
                continue
            if rule["when"] == "aggregate":
                sel = scope.expression
                if not any(
                    a.find_ancestor(exp.Select) is sel
                    for a in sel.find_all(exp.AggFunc)
                ):
                    continue
            triggered_scopes.append(scope)
    if not triggered_scopes:
        watch = when_cols or {rule["require"]["column"]}
        if ctx.overlap_unknown({(table, c) for c in watch}):
            return "unknown", "trigger column may flow through a derived source"
        return "pass", ""
    req = rule["require"]
    for scope in triggered_scopes:
        conjuncts, complete = ctx.conjuncts(scope)
        satisfied = any(
            _predicate_matches(exp, ctx, scope, c, table, req) for c in conjuncts
        )
        if not satisfied and not complete:
            # A disjunction hid part of the predicate set: absence is not
            # proof of absence, so defer instead of refusing.
            return "unknown", "a disjunctive WHERE — cannot prove the filter"
        if not satisfied and rule.get("or_group_by"):
            group = scope.expression.args.get("group")
            if group is not None:
                for g in group.find_all(exp.Column):
                    triple = ctx._resolve(g, scope)
                    if triple and triple[1] == table and triple[2] == rule[
                        "or_group_by"
                    ]:
                        satisfied = True
        if not satisfied:
            want = f"{req['column']} {req['op'].replace('_', ' ').upper()}"
            if "value" in req:
                want += f" {req['value']}"
            return (
                "violation",
                f"{table} used without required filter `{want}`",
            )
    return "pass", ""


def _eval_required_distinct(ctx: _QueryContext, rule: dict[str, Any]):
    exp = ctx.exp
    table = rule["table"]
    trigger = rule.get("when_filtered")
    for scope in ctx.scopes:
        if table not in ctx.base_tables(scope).values():
            continue
        group = scope.expression.args.get("group")
        if group is None:
            continue
        grouped = False
        for g in group.find_all(exp.Column):
            triple = ctx._resolve(g, scope)
            if triple and triple[1] == table and triple[2] == rule["group_by"]:
                grouped = True
        if not grouped:
            continue
        if trigger is not None:
            # The rule is scoped to a specific reading of the data (e.g. "the
            # winner predicate"): without that predicate this is a different,
            # legitimate question and the rule simply does not apply.
            conjuncts, complete = ctx.conjuncts(scope)
            matched = any(
                _predicate_matches(exp, ctx, scope, c, table, trigger)
                for c in conjuncts
            )
            if not matched:
                if not complete:
                    return "unknown", "a disjunctive WHERE — cannot prove the trigger"
                continue
        for count in scope.expression.find_all(exp.Count):
            if count.find_ancestor(exp.Select) is not scope.expression:
                continue
            # ANY count-distinct is fan-out-safe by construction — and
            # COUNT(DISTINCT <other column>) is a different, legitimate
            # question, never this rule's error.
            if isinstance(count.this, exp.Distinct):
                continue
            return (
                "violation",
                f"COUNT over {table} grouped by {rule['group_by']} must be "
                f"COUNT(DISTINCT {rule['count_distinct']})",
            )
    if ctx.overlap_unknown(
        {(table, rule["group_by"]), (table, rule["count_distinct"])}
    ):
        return "unknown", "grouping column may flow through a derived source"
    return "pass", ""


_EVALUATORS = {
    "forbidden_aggregation": _eval_forbidden_aggregation,
    "forbidden_usage": _eval_forbidden_usage,
    "forbidden_function": _eval_forbidden_function,
    "forbidden_grouping": _eval_forbidden_grouping,
    "required_predicate": _eval_required_predicate,
    "required_guard": _eval_required_guard,
    "forbidden_sequencing_key": _eval_forbidden_sequencing_key,
    "required_distinct": _eval_required_distinct,
}


def self_test(
    rules: list[dict[str, Any]],
    schema: dict[str, dict[str, list[str]]],
    *,
    dialect: str = "trino",
    default_database: str | None = None,
) -> str | None:
    """Run every rule's examples through the evaluator. None when all hold.

    The violation example must evaluate ``violation`` and the pass example
    ``pass`` — for THAT rule alone (sibling rules of the same policy don't
    contaminate the verdict). ``unknown`` on either side fails too: an
    example the evaluator can't decide proves nothing. Requires sqlglot —
    the author gate treats its absence as a gate error (rules without their
    self-test are unattestable).
    """
    if not sqlglot_available():
        return (
            "sqlglot is unavailable in this runtime — rules cannot be "
            "self-tested; omit `rules:` blocks"
        )
    if default_database is None and schema and len(schema) == 1:
        default_database = next(iter(schema))
    for i, rule in enumerate(rules):
        for kind, want in (("violation", "violation"), ("pass", "pass")):
            sql = rule["examples"][kind]
            result = evaluate_policies(
                sql,
                [("SELFTEST", [rule])],
                schema,
                default_database=default_database,
                dialect=dialect,
            )["SELFTEST"]
            if result["verdict"] != want:
                detail = "; ".join(result["details"]) or "no detail"
                hint = ""
                if rule.get("functions") or rule.get("cast_types"):
                    hint = (
                        " (function names are matched underscore-"
                        "insensitively against Trino spellings; cast types "
                        "accept int/integer, float/real, decimal/numeric — "
                        "check the binding's spelling)"
                    )
                return (
                    f"rules[{i}] ({rule['dimension']}) self-test failed: the "
                    f"{kind} example evaluated `{result['verdict']}` "
                    f"(expected `{want}`): {detail}. Example: {sql}{hint}"
                )
    return None
