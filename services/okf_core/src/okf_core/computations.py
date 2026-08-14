"""Attested Computations: parse / hash / validate / substitute.

An Attested Computation (``references/computations/<slug>.md``, frontmatter
``type: Attested Computation`` — see docs/ATTESTED_COMPUTATIONS.md) carries ONE
frozen, read-only, parameterized SQL statement in a ```sql fence under its
``# Computation`` heading. Agents may only ever supply values for the typed
``@name`` holes; the platform substitutes them as dialect-rendered literals and
executes. This module owns every pure rule of that contract:

* **doc shape** — exactly one runnable SELECT/WITH statement, every ``@hole``
  declared as a parameter and every declared parameter used, no authoring
  placeholders left behind;
* **the content hash** — sha256 over the fence text (trailing-whitespace-
  stripped lines), the canonical JSON of ``parameters``, and the ``runtime``
  string. Human verification signs THIS hash; any edit changes it and the
  verification reads as stale;
* **parameter contracts** — declared constraints (``enum``/``min``/``max``)
  hard-refuse at run time because they are inside the hash (the human verified
  those bounds); profiled evidence (``domains.json``) is advisory only —
  warn and run;
* **substitution** — typed literal rendering (the injection guard) and
  splicing into the ``@name`` sites over lint's literal/comment mask, so a
  ``@word`` inside a string literal is content, never a hole.

Shared identically by the write-time guard, lint, the executors (consumption
MCP / chat / Control API), and the verification fold-in — pure ``str``/``dict``
in, verdict out; no AWS, no agent deps.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from okf_core.document import OKFDocument
from okf_core.lint import _classify_fence, _mask_sql, _sql_fences_in

#: Concept-id prefix — one doc per computation, slug = basename minus ``.md``.
COMPUTATIONS_PREFIX = "references/computations/"
#: The frontmatter ``type`` (OKF v0.2, verbatim — consumers route on it).
COMPUTATION_TYPE = "Attested Computation"
#: The heading whose FIRST ```sql fence is the frozen statement.
COMPUTATION_SECTION = "# Computation"
#: Human-verification frontmatter triple. ``null`` until a human acts; agents
#: may only preserve existing values, never set them (see okf_core.guard).
VERIFICATION_FIELDS = ("verified", "verified_by", "verified_sha256")
#: Parameter ``type`` vocabulary (scalar-only, by design — a hole that could
#: inject SQL structure would make the executed statement unpinnable).
PARAM_TYPES = ("string", "integer", "number", "date", "timestamp", "boolean")
#: ``runtime`` selects the executor.
RUNTIMES = ("athena", "redshift")
#: Bundle-relative path of the profile pass's machine-readable enum domains
#: (harvest/profile.py) — the ADVISORY evidence layer.
DOMAINS_REL = ".metadata/profile/domains.json"

#: Sanity cap — a computation is one anticipated question, not an API surface.
MAX_PARAMETERS = 16

# A parameter hole: ``@name`` outside string literals/comments (callers scan
# the mask). The lookbehind keeps ``a@b``/``@@x`` from reading as holes.
_HOLE_RE = re.compile(r"(?<![\w@])@([A-Za-z_]\w*)")
_NAME_RE = re.compile(r"^[A-Za-z_]\w*$")
_COLUMN_REF_RE = re.compile(r"^[A-Za-z_][\w.]*\.[\w]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Numeric literal shape (same rule as the metric layer): plain decimal /
# scientific only, so `nan`, `inf`, and `1_000` (all accepted by float())
# never ship as bare unquoted tokens.
_NUMERIC_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2}(\.\d+)?)?)?")

_KNOWN_PARAM_KEYS = frozenset(
    {"name", "type", "required", "example", "default", "enum", "min", "max", "column"}
)


class ComputationError(ValueError):
    """A validation failure whose message is written FOR the calling agent:
    it names what was wrong and the legal alternatives, so a retry converges."""


def is_computation_path(rel_path: str) -> bool:
    """True iff ``rel_path`` is a computation concept doc (directly under the
    computations folder — no nesting; the slug is the basename)."""
    rel = str(rel_path).lstrip("/")
    return (
        rel.startswith(COMPUTATIONS_PREFIX)
        and rel.endswith(".md")
        and "/" not in rel[len(COMPUTATIONS_PREFIX) :]
    )


def slug_for_path(rel_path: str) -> str:
    return str(rel_path).lstrip("/").rsplit("/", 1)[-1][: -len(".md")]


# ---------------------------------------------------------------------------
# Fence extraction + the content hash
# ---------------------------------------------------------------------------


def _section_text(body: str, heading: str) -> str | None:
    """Raw text of the top-level ``# heading`` section (verbatim lines,
    blanks preserved — the hash needs exact fence text), or None when the
    heading is absent. Fence-aware like the guard's section scanner: a ``#``
    line inside a code fence never opens or closes a section."""
    in_section = False
    in_fence = False
    found = False
    out: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            if in_section:
                out.append(line)
            continue
        if not in_fence and stripped.startswith("# "):
            in_section = stripped == heading
            found = found or in_section
            continue
        if in_section:
            out.append(line)
    return "\n".join(out) if found else None


def computation_fences(body: str) -> list[str]:
    """The ```sql fences inside the ``# Computation`` section (a valid doc has
    exactly ONE — callers enforce the count so the error can say which rule)."""
    section = _section_text(body, COMPUTATION_SECTION)
    if section is None:
        return []
    return _sql_fences_in(section)


def canonical_sql(sql: str) -> str:
    """The hash's view of the fence: trailing-whitespace-stripped lines,
    ``\\n`` joined — so editor-trimmed trailing spaces never void a human's
    verification, but any CONTENT change does."""
    return "\n".join(line.rstrip() for line in sql.splitlines()).strip("\n")


def computation_sha256(
    sql: str, parameters: list[dict[str, Any]], runtime: str
) -> str:
    """The content hash a human's verification signs: *this statement with
    these parameter contracts on this engine* — nothing else. NUL separators
    keep the three parts unambiguous."""
    payload = (
        canonical_sql(sql).encode("utf-8")
        + b"\x00"
        + json.dumps(
            parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        + b"\x00"
        + str(runtime).encode("utf-8")
    )
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Parameter contracts
# ---------------------------------------------------------------------------


def normalize_value(ptype: str, value: Any) -> tuple[Any, str | None]:
    """``(normalized, error)`` — normalize one supplied value against a
    declared type. Normalized forms: int (integer), validated literal str
    (number), bool (boolean), ISO str (date/timestamp — ``T`` folded to a
    space), str (string)."""
    if value is None:
        return None, "value is null — omit optional parameters instead"
    if not isinstance(value, (str, int, float, bool)):
        return None, f"value must be a scalar, not {type(value).__name__}"
    if ptype == "integer":
        if isinstance(value, bool):
            return None, f"{value!r} is not an integer"
        if isinstance(value, int):
            return value, None
        s = str(value).strip()
        if re.fullmatch(r"[+-]?\d+", s):
            return int(s), None
        return None, f"{value!r} is not an integer"
    if ptype == "number":
        if isinstance(value, bool):
            return None, f"{value!r} is not a number"
        s = str(value).strip()
        if _NUMERIC_RE.fullmatch(s):
            return s, None
        return None, f"{value!r} is not a numeric literal"
    if ptype == "boolean":
        if isinstance(value, bool):
            return value, None
        s = str(value).strip().lower()
        if s in ("true", "false"):
            return s == "true", None
        return None, f"use true/false, not {value!r}"
    if ptype == "date":
        s = str(value).strip()
        if _DATE_RE.fullmatch(s):
            return s, None
        return None, f"pass ISO `YYYY-MM-DD`, not {value!r}"
    if ptype == "timestamp":
        s = str(value).strip().replace("T", " ")
        if _TIMESTAMP_RE.fullmatch(s):
            return s, None
        return None, f"pass ISO `YYYY-MM-DD[ HH:MM[:SS]]`, not {value!r}"
    # string — must BE a string: YAML 1.1 parses unquoted `yes`/`no` as
    # booleans and `007`/`1.10` as numbers, and a silent str() coercion would
    # hash + enforce values ('True', '7', '1.1') that appear nowhere in the
    # doc a human reads and verifies. Corrective: quote it.
    if not isinstance(value, str):
        return None, (
            f"{value!r} is not a string — YAML parsed it as "
            f"{type(value).__name__}; quote the value in the doc (\"...\") "
            f"or send a JSON string"
        )
    if "\x00" in value:
        return None, "string values may not contain NUL bytes"
    return value, None


def _compare_key(ptype: str, normalized: Any) -> Any:
    """A comparable key for enum/min/max checks: numbers compare numerically,
    dates/timestamps lexicographically (ISO makes that correct), the rest as
    their normalized selves."""
    if ptype == "number":
        return float(normalized)
    return normalized


def parse_parameters(raw: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate + normalize a computation's ``parameters`` frontmatter list.

    Returns ``(normalized, errors)`` — normalized is ``[]`` when errors exist.
    Every parameter needs ``example`` (lint's EXPLAIN substitutes it; describe
    shows it), and every optional parameter needs ``default`` — every declared
    hole is USED in the SQL, so something must fill it on every run.
    """
    errors: list[str] = []
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], ["`parameters` must be a YAML list of mappings"]
    if len(raw) > MAX_PARAMETERS:
        return [], [f"at most {MAX_PARAMETERS} parameters (got {len(raw)})"]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        where = f"parameters[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        unknown = sorted(set(item) - _KNOWN_PARAM_KEYS)
        if unknown:
            errors.append(
                f"{where}: unknown key(s) {', '.join(unknown)} — allowed: "
                f"{', '.join(sorted(_KNOWN_PARAM_KEYS))}"
            )
            continue
        name = item.get("name")
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            errors.append(f"{where}: `name` must be an identifier ([A-Za-z_]\\w*)")
            continue
        where = f"parameter `{name}`"
        if name in seen:
            errors.append(f"{where}: duplicate name")
            continue
        seen.add(name)
        ptype = item.get("type")
        if ptype not in PARAM_TYPES:
            errors.append(
                f"{where}: `type` must be one of {', '.join(PARAM_TYPES)}"
            )
            continue
        norm: dict[str, Any] = {"name": name, "type": ptype}

        required = item.get("required", "default" not in item)
        if not isinstance(required, bool):
            errors.append(f"{where}: `required` must be true/false")
            continue
        if required and "default" in item:
            errors.append(f"{where}: a required parameter cannot have a `default`")
            continue
        if not required and "default" not in item:
            errors.append(
                f"{where}: an optional parameter needs a `default` — every "
                f"declared hole is filled on every run"
            )
            continue
        norm["required"] = required

        if "example" not in item:
            errors.append(f"{where}: `example` is required (lint EXPLAINs with it)")
            continue
        example, err = normalize_value(ptype, item["example"])
        if err:
            errors.append(f"{where}: bad `example`: {err}")
            continue
        norm["example"] = example

        bad = False
        if "default" in item:
            default, err = normalize_value(ptype, item["default"])
            if err:
                errors.append(f"{where}: bad `default`: {err}")
                bad = True
            else:
                norm["default"] = default
        if "enum" in item:
            enum_raw = item["enum"]
            if not isinstance(enum_raw, list) or not enum_raw:
                errors.append(f"{where}: `enum` must be a non-empty list")
                bad = True
            elif ptype == "boolean":
                errors.append(f"{where}: `enum` is meaningless for a boolean")
                bad = True
            else:
                enum_norm = []
                for v in enum_raw:
                    nv, err = normalize_value(ptype, v)
                    if err:
                        errors.append(f"{where}: bad `enum` value: {err}")
                        bad = True
                        break
                    enum_norm.append(nv)
                else:
                    norm["enum"] = enum_norm
        for bound in ("min", "max"):
            if bound in item:
                if ptype not in ("integer", "number", "date", "timestamp"):
                    errors.append(
                        f"{where}: `{bound}` needs an orderable type "
                        f"(integer/number/date/timestamp), not {ptype}"
                    )
                    bad = True
                    continue
                bv, err = normalize_value(ptype, item[bound])
                if err:
                    errors.append(f"{where}: bad `{bound}`: {err}")
                    bad = True
                else:
                    norm[bound] = bv
        if "column" in item:
            col = item["column"]
            if not isinstance(col, str) or not _COLUMN_REF_RE.fullmatch(col):
                errors.append(f"{where}: `column` must be `table.column`")
                bad = True
            else:
                norm["column"] = col.lower()
        if bad:
            continue
        if (
            "min" in norm
            and "max" in norm
            and _compare_key(ptype, norm["min"]) > _compare_key(ptype, norm["max"])
        ):
            errors.append(f"{where}: `min` exceeds `max`")
            continue
        # The contract must be internally satisfiable: example and default
        # themselves obey enum/min/max, or the human is asked to verify bounds
        # the doc's own examples violate.
        for key in ("example", "default"):
            if key in norm:
                err = check_constraints(norm, norm[key])
                if err:
                    errors.append(f"{where}: `{key}` violates the contract: {err}")
        out.append(norm)
    if errors:
        return [], errors
    return out, errors


def check_constraints(param: dict[str, Any], normalized: Any) -> str | None:
    """Declared-constraint check (the CONTRACT layer — refuse on violation).
    ``normalized`` must already be `normalize_value`d."""
    ptype = param["type"]
    key = _compare_key(ptype, normalized)
    enum = param.get("enum")
    if enum is not None and key not in [_compare_key(ptype, v) for v in enum]:
        shown = ", ".join(repr(v) for v in enum[:20])
        return f"{normalized!r} is not among the declared values: {shown}"
    if "min" in param and key < _compare_key(ptype, param["min"]):
        return f"{normalized!r} is below the declared min {param['min']!r}"
    if "max" in param and key > _compare_key(ptype, param["max"]):
        return f"{normalized!r} is above the declared max {param['max']!r}"
    return None


def resolve_values(
    parameters: list[dict[str, Any]], values: dict[str, Any] | None
) -> dict[str, Any]:
    """Supplied values -> a complete ``{name: normalized}`` map, defaults
    filled. Raises :class:`ComputationError` naming every problem at once
    (unknown names, missing required, type/constraint violations) so the
    caller self-corrects in one retry."""
    values = dict(values or {})
    errors: list[str] = []
    declared = {p["name"] for p in parameters}
    for name in sorted(set(values) - declared):
        legal = ", ".join(sorted(declared)) or "none — it takes no parameters"
        errors.append(f"unknown parameter `{name}` (declared: {legal})")
    out: dict[str, Any] = {}
    for p in parameters:
        name = p["name"]
        if name not in values:
            if "default" in p:
                out[name] = p["default"]
            else:
                errors.append(f"missing required parameter `{name}` ({p['type']})")
            continue
        normalized, err = normalize_value(p["type"], values[name])
        if err:
            errors.append(f"parameter `{name}` ({p['type']}): {err}")
            continue
        err = check_constraints(p, normalized)
        if err:
            errors.append(f"parameter `{name}`: {err}")
            continue
        out[name] = normalized
    if errors:
        raise ComputationError("; ".join(errors))
    return out


# ---------------------------------------------------------------------------
# Literal rendering + substitution (the injection guard)
# ---------------------------------------------------------------------------


def render_literal(ptype: str, normalized: Any, runtime: str) -> str:
    """One typed SQL literal for a `normalize_value`d value, dialect-directed.

    ``''`` doubling is universal. Redshift additionally honours BACKSLASH as
    an escape inside ordinary string literals, so a value ending in ``\\``
    would escape the closing quote there — parameter values come from an LLM
    or a browser form, so that is an injection, not a formatting bug.
    Trino/Athena treat backslash as an ordinary character, where doubling
    would CORRUPT the value; hence dialect-directed, not uniform.
    """
    if ptype == "integer":
        s = str(int(normalized))
        # Parenthesize negatives: spliced next to a minus sign (`a -@adj`,
        # `@a-@b`) a bare `-5` forms the LINE-COMMENT token `--` and silently
        # truncates the statement to end-of-line on every engine we run.
        return f"({s})" if s.startswith("-") else s
    if ptype == "number":
        s = str(normalized).strip()
        return f"({s})" if s.startswith("-") else s
    if ptype == "boolean":
        return "TRUE" if normalized else "FALSE"
    if ptype == "date":
        return f"DATE '{normalized}'"
    if ptype == "timestamp":
        return f"TIMESTAMP '{normalized}'" if " " in str(normalized) else f"DATE '{normalized}'"
    s = str(normalized).replace("'", "''")
    if str(runtime).strip().lower() == "redshift":
        s = s.replace("\\", "\\\\")
    return "'" + s + "'"


def substitute(sql: str, literals: dict[str, str]) -> str:
    """Splice rendered literals into the ``@name`` sites, scanned over lint's
    literal/comment mask so a ``@word`` inside a string stays content. The
    result is the ONLY SQL that ever reaches an engine — the caller (executor)
    guarantees ``literals`` covers every hole via the doc's shape check."""
    masked = _mask_sql(sql)
    out: list[str] = []
    last = 0
    for m in _HOLE_RE.finditer(masked):
        name = m.group(1)
        if name not in literals:
            raise ComputationError(f"no value for hole `@{name}`")
        out.append(sql[last : m.start()])
        out.append(literals[name])
        last = m.end()
    out.append(sql[last:])
    return "".join(out)


def hole_names(sql: str) -> set[str]:
    """Every ``@name`` hole in the statement (mask-scanned)."""
    return {m.group(1) for m in _HOLE_RE.finditer(_mask_sql(sql))}


# ---------------------------------------------------------------------------
# The doc
# ---------------------------------------------------------------------------


@dataclass
class Computation:
    """One parsed, shape-valid computation doc."""

    slug: str
    path: str  # bundle-relative doc path
    title: str
    description: str
    runtime: str
    parameters: list[dict[str, Any]]
    sql: str  # the fence text, verbatim
    statement: str  # the single classified SELECT/WITH statement
    sha256: str
    verified: str | None = None
    verified_by: str | None = None
    verified_sha256: str | None = None
    tags: list[str] = field(default_factory=list)

    def example_values(self) -> dict[str, Any]:
        return {p["name"]: p["example"] for p in self.parameters}

    def rendered(self, values: dict[str, Any] | None) -> str:
        """Validated values -> the exact SQL to execute."""
        resolved = resolve_values(self.parameters, values)
        literals = {
            p["name"]: render_literal(p["type"], resolved[p["name"]], self.runtime)
            for p in self.parameters
        }
        return substitute(self.statement, literals)


def parse_computation(
    rel_path: str, doc: OKFDocument
) -> tuple[Computation | None, list[str]]:
    """Parse + shape-check one computation doc. Returns ``(comp, errors)`` —
    comp is None when errors exist. The rules ARE the attestation surface:
    exactly one runnable read-only statement, holes == declared parameters,
    nothing an engine can't take verbatim once the holes are filled."""
    fm = doc.frontmatter
    errors: list[str] = []
    if fm.get("type") != COMPUTATION_TYPE:
        errors.append(
            f"a doc under {COMPUTATIONS_PREFIX} must carry "
            f"`type: {COMPUTATION_TYPE}` (got {fm.get('type')!r})"
        )
    runtime = fm.get("runtime")
    if runtime not in RUNTIMES:
        errors.append(f"`runtime` must be one of {', '.join(RUNTIMES)}")
    parameters, perrors = parse_parameters(fm.get("parameters"))
    errors += perrors

    fences = computation_fences(doc.body)
    sql = ""
    statement = ""
    if not fences:
        errors.append(
            f"no ```sql fence under `{COMPUTATION_SECTION}` — the frozen "
            f"statement must live there"
        )
    elif len(fences) > 1:
        errors.append(
            f"{len(fences)} ```sql fences under `{COMPUTATION_SECTION}` — one "
            f"canonical statement, not a menu (variants are separate "
            f"computations)"
        )
    else:
        sql = fences[0]
        cls = _classify_fence(rel_path, sql)
        # _classify_fence COLLECTS only SELECT/WITH parts — a fence like
        # `SELECT ...; DROP TABLE t` would count as one statement while the
        # DROP text rides inside the hashed, human-reviewed fence (shown at
        # Verify time, never executed — the attestation surface and the
        # executed statement must be the same text). Any mask-level `;`
        # followed by real content means a second statement, whatever it is.
        masked_fence = _mask_sql(sql)
        has_extra_part = any(
            ch == ";" and masked_fence[i + 1 :].strip()
            for i, ch in enumerate(masked_fence)
        )
        if len(cls.statements) != 1 or has_extra_part:
            errors.append(
                "the fence must hold exactly ONE read-only SELECT/WITH "
                f"statement (found {max(len(cls.statements), 2) if has_extra_part else len(cls.statements)})"
            )
        else:
            statement = cls.statements[0]
            # Authoring placeholders (`<table>`, `{{x}}`, `:name`, `...`)
            # make the statement un-runnable however the @holes are filled.
            # Blank the holes first so they don't read as placeholders
            # themselves under the shared `_is_templated` scan.
            from okf_core.lint import _is_templated

            masked = _mask_sql(statement)
            deholed = _HOLE_RE.sub(lambda m: " " * len(m.group(0)), masked)
            if _is_templated(deholed):
                errors.append(
                    "the statement still contains authoring placeholders "
                    "(`<...>`, `{{...}}`, `:name`, or `...`) — write concrete "
                    "SQL; the ONLY holes are `@parameter` sites"
                )
            if not errors:
                holes = hole_names(statement)
                declared = {p["name"] for p in parameters}
                for name in sorted(holes - declared):
                    errors.append(
                        f"hole `@{name}` is not declared in `parameters`"
                    )
                for name in sorted(declared - holes):
                    errors.append(
                        f"parameter `{name}` is declared but `@{name}` never "
                        f"appears in the statement"
                    )

    # YAML parses an unquoted ISO stamp into datetime — normalize back to the
    # ISO string so consumers see one type however the doc was serialized.
    verified = fm.get("verified")
    if hasattr(verified, "isoformat"):
        verified = verified.isoformat()
    for key, v in (("verified", verified), ("verified_by", fm.get("verified_by"))):
        if v is not None and not isinstance(v, str):
            errors.append(f"`{key}` must be null or a string")
    vs = fm.get("verified_sha256")
    if vs is not None and (
        not isinstance(vs, str) or not _SHA256_RE.fullmatch(vs)
    ):
        errors.append("`verified_sha256` must be null or a sha256 hex digest")

    if errors:
        return None, errors
    return (
        Computation(
            slug=slug_for_path(rel_path),
            path=str(rel_path).lstrip("/"),
            title=str(fm.get("title") or ""),
            description=str(fm.get("description") or ""),
            runtime=str(runtime),
            parameters=parameters,
            sql=sql,
            statement=statement,
            sha256=computation_sha256(sql, parameters, str(runtime)),
            verified=verified,
            verified_by=fm.get("verified_by"),
            verified_sha256=vs,
            tags=list(fm.get("tags") or []),
        ),
        [],
    )


def parse_computation_text(
    rel_path: str, text: str
) -> tuple[Computation | None, list[str]]:
    """Convenience over :func:`parse_computation` for raw doc text (the S3
    serving paths hold text, not parsed docs)."""
    try:
        doc = OKFDocument.parse(text)
    except Exception as e:  # noqa: BLE001 — a malformed doc is a finding, not a crash
        return None, [f"document does not parse: {e}"]
    return parse_computation(rel_path, doc)


def find_computations(
    bundle_root: str | Path,
) -> list[tuple[str, Computation | None, list[str]]]:
    """Every doc under the computations folder on a local bundle:
    ``(rel_path, comp | None, errors)``, sorted by path. Used by lint and the
    harvest-side surfaces; the S3 serving paths list the prefix themselves."""
    root = Path(bundle_root)
    folder = root / COMPUTATIONS_PREFIX
    out: list[tuple[str, Computation | None, list[str]]] = []
    if not folder.is_dir():
        return out
    for md in sorted(folder.glob("*.md")):
        rel = COMPUTATIONS_PREFIX + md.name
        try:
            text = md.read_text(encoding="utf-8")
        except OSError as e:
            out.append((rel, None, [f"unreadable: {e}"]))
            continue
        comp, errors = parse_computation_text(rel, text)
        out.append((rel, comp, errors))
    return out


# ---------------------------------------------------------------------------
# Verification state (doc frontmatter + off-mount overlay)
# ---------------------------------------------------------------------------


def is_frozen(comp: Computation) -> bool:
    """True iff the doc ITSELF carries a binding stamp: ``verified`` set and
    ``verified_sha256`` matching the current content hash. A frozen
    computation is immutable to agents in the IN-PLACE run modes (the guard
    refuses write/edit/delete) — the human's attestation would be meaningless
    if an agent could swap the SQL under it. Only a human Unverify (Control
    API overlay tombstone) unlocks it; a FULL harvest is deliberately
    destructive and re-authors computations like every other doc. Overlay-only
    verification (clicked but not yet folded in) is resolved by the harvest
    runtime, which unions this with the overlay at run start — see
    ``harvest.verification.frozen_computation_paths``."""
    return bool(comp.verified) and comp.verified_sha256 == comp.sha256


def verification_state(
    sha256: str,
    frontmatter: dict[str, Any] | None = None,
    overlay_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge the two verification sources into one serving verdict.

    The overlay (the Control API's off-mount ``verification/`` object — the
    Verify click's landing spot until the runtime folds it into frontmatter)
    WINS on disagreement; a hash mismatch on either side surfaces as
    ``stale``, never hidden. A ``revoked`` tombstone (the Unverify click,
    needed because the doc may already CARRY a folded stamp the overlay's
    absence would resurrect) reads as unverified regardless of frontmatter.
    Returns ``{verification, verified, verified_by}`` with ``verification``
    one of ``verified`` / ``unverified`` / ``stale``.
    """
    src: dict[str, Any] | None = None
    if overlay_entry and overlay_entry.get("revoked"):
        return {"verification": "unverified", "verified": None, "verified_by": None}
    if overlay_entry and overlay_entry.get("verified"):
        src = {
            "verified": overlay_entry.get("verified"),
            "verified_by": overlay_entry.get("verified_by"),
            "sha256": overlay_entry.get("sha256"),
        }
    elif frontmatter and frontmatter.get("verified"):
        src = {
            "verified": frontmatter.get("verified"),
            "verified_by": frontmatter.get("verified_by"),
            "sha256": frontmatter.get("verified_sha256"),
        }
    if src is None:
        return {"verification": "unverified", "verified": None, "verified_by": None}
    status = "verified" if src.get("sha256") == sha256 else "stale"
    return {
        "verification": status,
        "verified": src.get("verified"),
        "verified_by": src.get("verified_by"),
    }


def fold_verification_entry(text: str, entry: dict[str, Any]) -> str | None:
    """Doc text with an overlay entry folded into the frontmatter triple, or
    None when the entry does not bind (not a valid computation, or the signed
    hash no longer matches — the doc changed since the human clicked Verify,
    so the stamp must NOT be written in). A ``revoked`` tombstone folds as
    NULLING the doc's triple (hash-independent — revocation is unconditional),
    returning None when the doc carries no stamp to remove. Platform code only
    (the runtime's run-start fold-in through the mount); serialization goes
    through the same canonical serializer the guard uses."""
    if entry.get("revoked"):
        # Revocation is unconditional and must not require the doc to still
        # be a shape-VALID computation — a doc that went invalid while
        # carrying a folded stamp would otherwise get its tombstone dropped
        # as "satisfied", and a later repair to equivalent content would
        # RESURRECT a verification the human explicitly revoked.
        try:
            doc = OKFDocument.parse(text)
        except Exception:  # noqa: BLE001 - unparseable: keep the tombstone
            return None
        fm = doc.frontmatter
        if not (
            fm.get("verified") or fm.get("verified_by") or fm.get("verified_sha256")
        ):
            return None
        stamp = (None, None, None)
    else:
        comp, _errors = parse_computation_text("", text)
        if comp is None or entry.get("sha256") != comp.sha256:
            return None
        stamp = (entry.get("verified"), entry.get("verified_by"), entry.get("sha256"))
        doc = OKFDocument.parse(text)
    doc.frontmatter["verified"] = stamp[0]
    doc.frontmatter["verified_by"] = stamp[1]
    doc.frontmatter["verified_sha256"] = stamp[2]
    return doc.serialize()


# ---------------------------------------------------------------------------
# Profile evidence (the ADVISORY layer)
# ---------------------------------------------------------------------------


def read_domains(bundle_root: str | Path) -> dict[str, dict[str, Any]]:
    """``domains.json`` flattened to ``{"table.column": {values, distinct,
    exhaustive}}`` (lower-cased keys), ``{}`` when absent/corrupt — the
    advisory layer degrades to silence, never to a crash."""
    path = Path(bundle_root) / DOMAINS_REL
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return flatten_domains(raw)


def flatten_domains(raw: Any) -> dict[str, dict[str, Any]]:
    """Flatten a parsed domains.json payload (see :func:`read_domains`)."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for table, entry in (raw.get("tables") or {}).items():
        for col, dom in ((entry or {}).get("columns") or {}).items():
            if isinstance(dom, dict) and isinstance(dom.get("values"), list):
                out[f"{str(table).lower()}.{str(col).lower()}"] = dom
    return out


def lookup_domain(
    domains: dict[str, dict[str, Any]], col: str
) -> dict[str, Any] | None:
    """Domain for a ``table.column`` binding: exact key first, then a UNIQUE
    dotted-suffix match — a Redshift snapshot keys tables ``schema.table``
    while a doc may bind the short ``orders.region``. Ambiguity resolves to
    None (guessing between schemas would be worse than staying silent)."""
    dom = domains.get(col.lower())
    if dom is not None:
        return dom
    hits = [k for k in domains if k.endswith("." + col.lower())]
    return domains[hits[0]] if len(hits) == 1 else None


def value_observed(ptype: str, normalized: Any, observed: list) -> bool:
    """Is a `normalize_value`d value among an engine-profiled value list?

    Canonicalized per type — a boolean normalizes to Python True but profiles
    as 'true'; a number may normalize to '10.0' against an observed '10' —
    so naive str() equality would false-warn on every run (and, in lint's
    exhaustive-enum check, false-ERROR a correct doc).
    """
    if ptype == "boolean":
        return ("true" if normalized else "false") in {
            str(v).strip().lower() for v in observed
        }
    if ptype in ("integer", "number"):
        try:
            target = float(normalized)
        except (TypeError, ValueError):
            return False
        for v in observed:
            try:
                if float(str(v).strip()) == target:
                    return True
            except (TypeError, ValueError):
                continue
        return False
    return str(normalized) in {str(v) for v in observed}


def domain_warnings(
    parameters: list[dict[str, Any]],
    resolved: dict[str, Any],
    domains: dict[str, dict[str, Any]],
) -> list[str]:
    """Advisory (warn-and-run) check of resolved values against profiled
    domains, for parameters that bind a ``column``. A miss is a WARNING — the
    profile is a snapshot-time scan and data legitimately evolves past it."""
    warnings: list[str] = []
    for p in parameters:
        col = p.get("column")
        if not col or p["name"] not in resolved:
            continue
        dom = lookup_domain(domains, col)
        if not dom:
            continue
        values = [str(v) for v in dom.get("values") or []]
        if not value_observed(p["type"], resolved[p["name"]], values):
            n = dom.get("distinct") or len(values)
            kind = "the" if dom.get("exhaustive") else f"the {n} observed"
            shown = ", ".join(repr(v) for v in values[:8])
            warnings.append(
                f"value {resolved[p['name']]!r} for `{p['name']}` is not among "
                f"{kind} values for `{col}` (e.g. {shown}) — running anyway; "
                f"an empty result likely means a typo'd value"
            )
    return warnings
