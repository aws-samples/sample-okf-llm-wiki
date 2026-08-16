"""Snapshot-time relationship evidence under ``.metadata/relationships/``.

The single largest mechanical token sink in a harvest is each table-author
independently DISCOVERING and VERIFYING joins and grain with agent-loop tool
calls: every probe decision replays the author's whole context as input
tokens, both sides of a pair probe the same join, and the reviewers re-verify
it a third time. Yet the candidates are mechanically enumerable (shared
key-like column names across ``columns.tsv``; id-columns matching their own
table's name for grain), and the probes themselves are deterministic SQL
(:mod:`harvest.probes` — the SAME cores the ``check_grain``/``validate_join``
tools call). So this module runs them ONCE, at snapshot time, in a plain loop
with no model anywhere near it, and writes the evidence where every agent
already reads (``read_file``/``grep`` over ``.metadata/``):

    .metadata/relationships/
    ├── manifest.tsv           # kind, subject, fingerprint, status, at
    ├── joins/<a>__<b>.md      # match rates both ways, cardinality, orphans
    └── grain/<table>.md       # uniqueness verdict for the candidate key

Cost posture mirrors ``profile.py``: a wall-clock budget and a pair cap (both
env-tunable) bound the engine bill. The per-side size gate (catalog hint →
S3-listing measurement → assume large) decides the probe shape: both sides
under it → full two-direction probe; exactly ONE measured-big side (the
enterprise fact→dim shape) → that side is SAMPLED toward a byte target
against the FULL small side, reporting only the unbiased sampled→full
direction on an INDICATIVE sheet — never the reverse (a sampled CONTAINING
side collapses match rates toward the sample fraction); both sides big or an
unmeasurable size → skipped. A per-pair fingerprint (both tables' catalog
fingerprints) makes re-runs cheap under the same reuse policy profiles use
(incremental runs re-probe only pairs touching changed tables; a FULL harvest
re-probes everything — it is the explicit "re-read the data" action).
Everything is best-effort: a failed probe is a manifest row, and this module
never raises out of the snapshot.

Precision over recall, deliberately: a candidate this module misses simply
falls back to today's behavior (the author probes it live — their prompt says
"probe what the sheets don't cover"), while a junk pair costs real engine
scans. The enumeration therefore probes only key-LIKE shared columns, prefers
(holder → home-table) pairs when the column names its own table (``raceid`` →
``races``), and refuses to fan out a name shared by too many tables.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harvest.fsutil import write_text
from harvest.probes import (
    grain_stats,
    join_stats,
    sample_orphans,
    sampled_join_stats,
)
from harvest.profile import (
    _data_shape,
    _env_int,
    _fmt_value as _fmt,
    _size_hint_bytes as _size_hint,
    table_fingerprint,
)
from harvest.source_base import Source

log = logging.getLogger(__name__)

RELATIONSHIPS_DIR = "relationships"
MANIFEST_NAME = "manifest.tsv"
_MANIFEST_HEADER = "kind\tsubject\tfingerprint\tstatus\tprobed_at"
#: Sketch-nominated candidates persisted across runs. Name/role candidates
#: re-enumerate deterministically from the catalog every run, but sketch
#: nominations exist only while a FULL run's sketches are in memory — without
#: this file the first incremental run would silently drop every
#: sketch-discovered sheet (the .metadata wipe deletes it and nothing
#: re-nominates the pair).
_CANDIDATES_NAME = "candidates.json"

#: Suffixes that mark a column name as a join-key candidate outright.
_STRONG_KEY_SUFFIXES = ("_id", "_key", "_nbr", "_sk")

#: Verdict thresholds on the BEST match rate of the two sides.
_HOLDS_RATE = 0.8
_REFUTED_RATE = 0.2

#: The shared-enum false-positive signature: containment testing cannot tell
#: "A's values ⊆ B's values because A references B" from "both columns draw
#: on the same tiny code list" — two unrelated tables each carrying a
#: status_id 1..8 show ~100% match both ways. The tell is mechanical: M:N
#: cardinality (neither side key-unique) with BOTH value domains tiny
#: relative to their row counts. 50 matches the profiler's enum threshold.
_ENUM_DOMAIN_MAX = 50
_ENUM_MIN_REPEAT = 4  # each value repeats ≥ this on both sides

_ORPHAN_SAMPLE_N = 5


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class RelationshipConfig:
    """Env-derived knobs (documented in CONVENTIONS.md)."""

    enabled: bool = True
    budget_s: int = 1800
    max_pairs: int = 100
    max_grain_per_table: int = 2
    max_tables_per_key: int = 6
    max_table_bytes: int = 10 << 30  # full probes only below this per side
    # When exactly ONE side exceeds the gate (the enterprise fact→dim shape),
    # probe with THAT side sampled toward this byte target (the other side
    # stays full — sampling a CONTAINING side biases match rates); both sides
    # over, or an unmeasurable size, still skip.
    sample_target_bytes: int = 256 << 20
    # The name-blind VALUE-SKETCH nominator (KMV/bottom-k containment): finds
    # renamed keys (cds vs cdscode) that no name heuristic can see.
    sketch_enabled: bool = True
    sketch_k: int = 256           # sketch size; estimation error ~1/sqrt(k)
    sketch_min_containment: float = 0.5  # nominate at/above this estimate
    sketch_max_columns: int = 12  # sketched columns per table (priority-ranked)

    @classmethod
    def from_env(cls) -> "RelationshipConfig":
        return cls(
            enabled=os.environ.get("OKF_HARVEST_REL_ENABLED", "1") != "0",
            budget_s=_env_int("OKF_HARVEST_REL_BUDGET_S", 1800),
            max_pairs=_env_int("OKF_HARVEST_REL_MAX_PAIRS", 100),
            max_grain_per_table=_env_int("OKF_HARVEST_REL_MAX_GRAIN_PER_TABLE", 2),
            max_tables_per_key=_env_int("OKF_HARVEST_REL_MAX_TABLES_PER_KEY", 6),
            max_table_bytes=_env_int(
                "OKF_HARVEST_REL_MAX_TABLE_BYTES", 10 << 30
            ),
            sketch_enabled=os.environ.get("OKF_HARVEST_REL_SKETCH_ENABLED", "1")
            != "0",
            sketch_k=_env_int("OKF_HARVEST_REL_SKETCH_K", 256),
            sketch_min_containment=_env_float(
                "OKF_HARVEST_REL_SKETCH_MIN_CONTAINMENT", 0.5
            ),
            sketch_max_columns=_env_int("OKF_HARVEST_REL_SKETCH_MAX_COLUMNS", 12),
            sample_target_bytes=_env_int(
                "OKF_HARVEST_REL_SAMPLE_TARGET_BYTES", 256 << 20
            ),
        )


@dataclass
class _CachedRelationships:
    """The previous run's sheets, read from the mount BEFORE the snapshot wipe."""

    fingerprints: dict[tuple[str, str], str] = field(default_factory=dict)
    manifest_rows: dict[tuple[str, str], str] = field(default_factory=dict)
    sheets: dict[tuple[str, str], str] = field(default_factory=dict)
    sketch_candidates: list[dict[str, Any]] = field(default_factory=list)


def read_cached_relationships(dataset_root: str | Path) -> _CachedRelationships:
    """Load the previous run's evidence (call BEFORE the ``.metadata`` wipe)."""
    cache = _CachedRelationships()
    root = Path(dataset_root) / ".metadata" / RELATIONSHIPS_DIR
    cand_file = root / _CANDIDATES_NAME
    if cand_file.is_file():
        try:
            data = json.loads(cand_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                cache.sketch_candidates = [
                    c for c in data if isinstance(c, dict)
                ]
        except (OSError, UnicodeDecodeError, ValueError):
            pass  # corrupt candidates = none (best-effort, like the manifest)
    manifest = root / MANIFEST_NAME
    if not manifest.is_file():
        return cache
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return cache  # corrupt manifest = no cache (mirrors profile.py)
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) != 5 or parts[3] not in ("ok", "ok-sampled", "cached"):
            continue
        kind, subject, fp = parts[0], parts[1], parts[2]
        rel = (
            f"joins/{subject}.md" if kind == "join" else f"grain/{subject}.md"
        )
        try:
            cache.sheets[(kind, subject)] = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        cache.fingerprints[(kind, subject)] = fp
        cache.manifest_rows[(kind, subject)] = line
    return cache


# ---------------------------------------------------------------------------
# candidate enumeration — pure, zero queries
# ---------------------------------------------------------------------------


def _top_level_columns(meta: dict[str, Any]) -> dict[str, str]:
    """{column_name: type} for a table's top-level columns.

    INCLUDES partition keys: in lake-style warehouses the partition column is
    often exactly the join/grain key (seen live: a partition key on one table
    joining a storage column on another — invisible when only ``flat_schema``
    is read, so the schema's one real pair nominated nothing). The engine
    queries partition columns like any other, so every consumer of this map
    (name/role/sketch nomination, grain keys, probe type lookup) handles them
    unchanged. On a name collision the storage column wins (a column can't
    legally be both; the storage type is what a probe would scan)."""
    out: dict[str, str] = {}
    for f in meta.get("flat_partition_schema") or []:
        if int(f.get("depth") or 0) == 0 and f.get("name"):
            out[str(f["name"])] = str(f.get("type") or "")
    for f in meta.get("flat_schema") or []:
        if int(f.get("depth") or 0) == 0 and f.get("name"):
            out[str(f["name"])] = str(f.get("type") or "")
    return out


def _norm(name: str) -> str:
    """Case- and underscore-insensitive comparison form: ``driver_id`` ≡
    ``driverid`` ≡ ``DriverID``. Grouping and suffix matching happen on this
    form; the ORIGINAL per-side names are preserved on every candidate (the
    probe and the sheet must use each table's real spelling)."""
    return name.lower().replace("_", "")


def _key_prefix(name: str) -> str | None:
    """The table-name-ish prefix of a key-like column, or None when not key-like."""
    low = _norm(name)
    for suf in ("key", "nbr", "sk"):
        if low.endswith(suf) and len(low) > len(suf):
            return low[: -len(suf)]
    if low.endswith("id") and low != "id" and len(low) > 4:
        return low[:-2]
    return None


def _is_key_like(name: str, table_names: list[str]) -> bool:
    low = name.lower()
    if low.endswith(_STRONG_KEY_SUFFIXES):
        return True
    norm = _norm(name)
    if norm.endswith(("id", "key", "nbr", "sk")) and norm not in (
        "id", "key", "nbr", "sk"
    ):
        prefix = _key_prefix(name) or ""
        return len(prefix) >= 3 and any(
            _prefix_names_table(prefix, t) for t in table_names
        )
    return False


def _table_base(table: str) -> str:
    """The table's own name without any schema qualifier: Redshift tokens are
    ``schema.table`` (``public.orders``), and every NAME heuristic must match
    against ``orders`` — no column is ever called ``public_orders_id``."""
    return table.rsplit(".", 1)[-1]


def _prefix_names_table(prefix: str, table: str) -> bool:
    """Does a key prefix plausibly NAME this table? Both directions of
    startswith, because real keys carry infixes: ``race`` names ``races``
    (table extends prefix), and ``teamapi`` (from ``team_api_id``) names
    ``team`` (prefix extends table)."""
    tn = _norm(_table_base(table))
    if len(prefix) < 3 or len(tn) < 3:
        return False
    return tn.startswith(prefix) or prefix.startswith(tn)


def _is_self_key(column: str, table: str) -> bool:
    """Is this column its own table's primary-key-ish id — bare ``id`` or a
    self-naming key (``raceid`` in ``races``)? Such a column being the
    CONTAINED side of a cross-table containment is not FK evidence: FKs point
    AT PKs, so a PK contained elsewhere is either a rare 1:1 extension table
    or — overwhelmingly, on surrogate-keyed schemas — the dense-integer
    coincidence (every ``id`` is 1..N, so every smaller id space sits inside
    every larger one numerically). Seen live on MusicBrainz: 301 junk
    ``id ↔ id`` sketch nominations from exactly this."""
    if _norm(column) == "id":
        return True
    prefix = _key_prefix(column)
    return bool(prefix and _prefix_names_table(prefix, table))


def _names_table(column: str, table: str) -> bool:
    """Does this column's name refer to ``table``, under any of the FK naming
    conventions? Bare entity name (``artist`` → table ``artist``), a
    role/suffix composition (``begin_area`` → ``area``), or a key-suffixed
    form whose prefix names the table (``raceid`` → ``races``)."""
    tn = _norm(_table_base(table))
    if len(tn) < 3:
        return False
    cn = _norm(column)
    if cn == tn or cn.endswith(tn):
        return True
    prefix = _key_prefix(column)
    return bool(prefix and _prefix_names_table(prefix, table))


def _home_table(column: str, holders: list[str]) -> str | None:
    """The holder the column NAMES, when one exists: ``raceid`` → ``races``.

    That table is the probable PK side, so every other holder pairs with it
    (fact → dimension) instead of pairwise-exploding a widely shared key.
    """
    prefix = _key_prefix(column)
    if not prefix:
        return None
    matches = [t for t in holders if _prefix_names_table(prefix, t)]
    return matches[0] if len(matches) == 1 else None


def _type_fam(t: str) -> str:
    base = t.strip().lower().split("(")[0].split("<")[0].strip()
    if base in ("tinyint", "smallint", "int", "integer", "bigint"):
        return "int"
    if base in (
        "string", "varchar", "char", "character", "character varying",
        "bpchar", "nchar", "nvarchar", "text",
    ):
        # "character varying" is what Redshift's SVV_ALL_COLUMNS emits for
        # every varchar — omitting it made the sketch pass text-blind there.
        return "text"
    return base


def _comparable(type_l: str, type_r: str) -> bool:
    """Same coarse family? A varchar=bigint probe would just error the engine —
    but the mismatch itself is evidence, so the caller writes a sheet for it."""
    return _type_fam(type_l) == _type_fam(type_r)


def enumerate_join_candidates(
    tables_meta: dict[str, dict[str, Any]], cfg: RelationshipConfig
) -> tuple[list[dict[str, Any]], list[str]]:
    """(candidate pairs, notes-about-what-was-not-enumerated).

    Each candidate: {left, right, column_l, column_r, type_l, type_r,
    comparable, via} — per-side ORIGINAL column names, because two sources
    nominate pairs whose spellings differ:

    * ``via="name"`` — the same normalized name in two tables (case- and
      underscore-insensitive: ``driver_id`` groups with ``driverid``).
    * ``via="role"`` — a ROLE-named foreign key: a column whose normalized
      name ends with (but is longer than) another table's self-naming key
      (``home_team_api_id`` → ``team.team_api_id``). The shared-name
      primitive can never see these (the role column exists in one table).

    Pair ids are deterministic (sorted table names), so sheets/fingerprints
    are stable across runs.
    """
    table_names = sorted(tables_meta)
    columns_by_table = {t: _top_level_columns(m) for t, m in tables_meta.items()}
    # norm name -> [(table, original column name)]
    holders_by_norm: dict[str, list[tuple[str, str]]] = {}
    for t in table_names:
        for c in columns_by_table[t]:
            holders_by_norm.setdefault(_norm(c), []).append((t, c))

    candidates: list[dict[str, Any]] = []
    notes: list[str] = []
    seen: set[tuple[str, str, str, str]] = set()

    def _add(a: str, col_a: str, b: str, col_b: str, via: str) -> None:
        (left, col_l), (right, col_r) = sorted(((a, col_a), (b, col_b)))
        key = (left, right, _norm(col_l), _norm(col_r))
        if left == right or key in seen:
            return
        seen.add(key)
        candidates.append(
            {
                "left": left,
                "right": right,
                "column_l": col_l,
                "column_r": col_r,
                "type_l": columns_by_table[left].get(col_l, ""),
                "type_r": columns_by_table[right].get(col_r, ""),
                "via": via,
            }
        )

    # --- source 1: shared (normalized) key-like names ---------------------
    for norm, holders in sorted(holders_by_norm.items()):
        if len(holders) < 2:
            continue
        # Key-like if ANY holder's original spelling qualifies (a strong
        # `driver_id` vouches for its underscore-less twin `driverid`).
        if not any(_is_key_like(c, table_names) for _, c in holders):
            continue
        holder_tables = [t for t, _ in holders]
        col_of = dict(holders)
        home = _home_table(holders[0][1], holder_tables)
        if home is not None:
            pairs = [(t, home) for t in holder_tables if t != home]
        elif len(holders) > cfg.max_tables_per_key:
            notes.append(
                f"`{norm}` is shared by {len(holders)} tables with no "
                "obvious home table — not probed (too ambiguous); probe "
                "specific pairs live if a doc needs them"
            )
            continue
        else:
            pairs = [
                (a, b)
                for i, a in enumerate(holder_tables)
                for b in holder_tables[i + 1 :]
            ]
        for a, b in pairs:
            _add(a, col_of[a], b, col_of[b], "name")

    # --- source 2: role-named foreign keys --------------------------------
    # Self-naming keys per table (raceid in races, team_api_id in team) are
    # the referent set; any OTHER table's column whose norm strictly ends
    # with one is a role-named reference to it.
    self_keys: list[tuple[str, str]] = []  # (table, key column)
    for t in table_names:
        for c in columns_by_table[t]:
            prefix = _key_prefix(c)
            if prefix and _prefix_names_table(prefix, t):
                self_keys.append((t, c))
    for home, key_col in self_keys:
        key_norm = _norm(key_col)
        if len(key_norm) < 5:  # a referent like `id` matches everything
            continue
        for t in table_names:
            if t == home:
                continue
            for c in columns_by_table[t]:
                c_norm = _norm(c)
                if c_norm != key_norm and c_norm.endswith(key_norm):
                    _add(t, c, home, key_col, "role")

    for cand in candidates:
        cand["comparable"] = _comparable(cand["type_l"], cand["type_r"])
    return candidates, notes


def enumerate_grain_candidates(
    tables_meta: dict[str, dict[str, Any]], cfg: RelationshipConfig
) -> dict[str, list[list[str]]]:
    """{table: [key_column_lists]} — the self-naming id column first."""
    out: dict[str, list[list[str]]] = {}
    for table, meta in sorted(tables_meta.items()):
        cols = _top_level_columns(meta)
        cands: list[list[str]] = []
        for c in cols:
            prefix = _key_prefix(c)
            if prefix and _prefix_names_table(prefix, table):
                cands.append([c])
        for c in cols:
            if c.lower() == "id" and ["id"] not in cands:
                cands.append([c])
        if cands:
            out[table] = cands[: cfg.max_grain_per_table]
    return out


# ---------------------------------------------------------------------------
# value-sketch nomination (name-blind: catches renamed keys)
# ---------------------------------------------------------------------------

_HASH_SPAN = 2.0**64  # signed 64-bit hash range, normalized below


def _kmv_cardinality(sketch: list[int], k: int) -> int:
    """Distinct-count estimate from a bottom-k sketch.

    A sketch SHORTER than k saw every distinct value — the count is exact.
    Otherwise the k-th smallest hash's position in the (signed) 64-bit range
    tells how densely the values fill it: k points reaching only fraction f
    of the range imply ~(k-1)/f distinct values."""
    n = len(sketch)
    if n == 0 or n < k:
        return n
    frac = (max(sketch) + 2**63 + 1) / _HASH_SPAN
    return max(n, int((k - 1) / frac)) if frac > 0 else n


def _containments(
    sk_a: list[int], sk_b: list[int], k: int
) -> tuple[float, float]:
    """(containment of A in B, of B in A), estimated from two bottom-k sketches.

    The merge trick: the k smallest of ``bottom-k(A) ∪ bottom-k(B)`` are
    exactly the k smallest of A∪B (each side's sketch is complete below its
    own k-th minimum), i.e. a uniform sample of the union's distinct values.
    The fraction of that sample present in BOTH sketches estimates Jaccard;
    scaled by the union cardinality it yields |A∩B|, and dividing by each
    side's own cardinality gives the two containments. Error ~1/sqrt(k)."""
    a_set, b_set = set(sk_a), set(sk_b)
    if not a_set or not b_set:
        return 0.0, 0.0
    union_sample = sorted(a_set | b_set)[:k]
    inter = sum(1 for h in union_sample if h in a_set and h in b_set)
    jaccard = inter / len(union_sample)
    union_card = _kmv_cardinality(union_sample, k)
    inter_card = jaccard * union_card
    card_a = _kmv_cardinality(sorted(a_set), k)
    card_b = _kmv_cardinality(sorted(b_set), k)
    return (
        min(1.0, inter_card / card_a) if card_a else 0.0,
        min(1.0, inter_card / card_b) if card_b else 0.0,
    )


def _sketchable_columns(cols: dict[str, str], cap: int) -> list[str]:
    """Which columns earn a sketch, priority-ranked then capped.

    Only int/text families (join keys live there); key-looking names first so
    the cap trims descriptive columns, not keys. Broader than key-LIKE on
    purpose — the whole point is name-blindness (``cds`` is nobody's idea of
    a key name), so plain columns still qualify, just at lower priority."""
    hints = ("id", "key", "code", "num", "nbr", "cd", "no")

    def score(name: str) -> int:
        low = name.lower()
        if low.endswith(_STRONG_KEY_SUFFIXES) or _key_prefix(name):
            return 0
        return 1 if any(h in low for h in hints) else 2

    eligible = [
        c for c, t in cols.items() if _type_fam(t) in ("int", "text")
    ]
    return sorted(eligible, key=lambda c: (score(c), c))[:cap]


def _parse_sketch(value: Any) -> list[int]:
    """A sketch cell as sorted DISTINCT ints: engines return arrays as lists
    or JSON-ish text. Deduped defensively — KMV needs distinct hashes, and a
    multiset sketch silently collapses the cardinality estimate."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            import json as _json

            value = _json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for v in value:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def _collect_sketches(
    source: Any,
    tables_meta: dict[str, dict[str, Any]],
    cfg: RelationshipConfig,
    *,
    skip_table: Any,
    past_deadline: Any,
    on_progress: Any = None,
) -> tuple[dict[str, dict[str, list[int]]], list[str]]:
    """{table: {column: sketch}} via whichever capability the source has.

    Preferred: an n-smallest aggregate EXPRESSION (Athena/Trino ``min(x, k)``)
    — every column of a table in ONE columnar scan. Fallback: a per-column
    query (Redshift, which lacks the aggregate but is columnar anyway).
    Neither → no sketches, and sketch nomination silently contributes nothing.
    """
    expr_fn = getattr(source, "sql_bottomk_sketch", None)
    colq_fn = getattr(source, "sql_sketch_column_query", None)
    table_ref = getattr(source, "sql_table_ref", None)
    notes: list[str] = []
    if not callable(table_ref) or not (callable(expr_fn) or callable(colq_fn)):
        return {}, notes
    from harvest.probes import quote_ident

    sketches: dict[str, dict[str, list[int]]] = {}
    _all = sorted(tables_meta)
    for _si, table in enumerate(_all, start=1):
        if on_progress is not None:
            try:
                on_progress(_si, len(_all), table)
            except Exception:  # noqa: BLE001 - a tick must never break the pass
                pass
        cols = _sketchable_columns(
            _top_level_columns(tables_meta[table]), cfg.sketch_max_columns
        )
        if not cols:
            continue
        if past_deadline():
            notes.append(f"sketching stopped at the budget before `{table}`")
            break
        if skip_table(table):  # after the deadline check — sizing does S3 LISTs
            continue
        ref = table_ref(table)
        try:
            if callable(expr_fn):
                selects = [
                    f"{expr_fn(quote_ident(c), cfg.sketch_k)} AS s{i}"
                    for i, c in enumerate(cols)
                ]
                rows = source.run_query(
                    f"SELECT {', '.join(selects)} FROM {ref}"
                )
                row = rows[0] if rows else {}
                sketches[table] = {
                    c: _parse_sketch(row.get(f"s{i}"))
                    for i, c in enumerate(cols)
                }
            else:
                per_col: dict[str, list[int]] = {}
                for c in cols:
                    if past_deadline():
                        break
                    rows = source.run_query(
                        colq_fn(ref, quote_ident(c), cfg.sketch_k)
                    )
                    per_col[c] = sorted(
                        {int(r.get("h")) for r in rows if r.get("h") is not None}
                    )
                sketches[table] = per_col
        except Exception as e:  # noqa: BLE001 — a lost sketch loses nominations only
            notes.append(f"sketching `{table}` failed: {_fmt(e)}")
    return sketches, notes


def _sketch_nominate(
    sketches: dict[str, dict[str, list[int]]],
    tables_meta: dict[str, dict[str, Any]],
    candidates: list[dict[str, Any]],
    cfg: RelationshipConfig,
    notes: list[str] | None = None,
) -> None:
    """Append value-containment nominations to ``candidates`` (in place).

    Dedups against the name/role nominations, suppresses the shared-enum
    signature pre-probe (two tiny domains contain each other trivially —
    probing them would only earn a SUSPECT verdict anyway), and records the
    containment estimate on the candidate for the sheet. Key-sized pairs
    whose distinct counts are too lopsided for the merge trick to resolve
    are counted into ``notes`` — real containment there reads as ~zero, and
    silence would look like "compared and refuted"."""
    types = {t: _top_level_columns(m) for t in sketches for m in [tables_meta[t]]}
    existing = {
        (c["left"], c["right"], frozenset({_norm(c["column_l"]), _norm(c["column_r"])}))
        for c in candidates
    }
    cards = {
        (t, c): _kmv_cardinality(sk, cfg.sketch_k)
        for t, cols in sketches.items()
        for c, sk in cols.items()
    }
    # Same-named columns across tables (norm form): used to collapse
    # holder↔holder pairs toward the column's HOME table, mirroring the name
    # source (`tag` in fifteen *_tag tables must pair each holder with the
    # `tag` table, not pairwise — seen live on MusicBrainz: 70 tag↔tag
    # nominations). The home here is the table the column NAMES outright.
    norm_holders: dict[str, set[str]] = {}
    for t, cols in sketches.items():
        for c in cols:
            norm_holders.setdefault(_norm(c), set()).add(t)
    home_cache: dict[str, str | None] = {}

    def _sketch_home(norm: str) -> str | None:
        if norm not in home_cache:
            matches = [
                t for t in tables_meta if _prefix_names_table(norm, t)
            ]
            if len(matches) != 1:
                # `driverid`-style keys name their home via the PREFIX, like
                # the name source's `_home_table` does.
                prefix = _key_prefix(norm)
                if prefix:
                    matches = [
                        t for t in tables_meta if _prefix_names_table(prefix, t)
                    ]
            home_cache[norm] = matches[0] if len(matches) == 1 else None
        return home_cache[norm]

    lopsided = 0
    pk_contained = 0
    collapsed = 0
    unnamed_vs_pk = 0
    # Beyond this distinct-count ratio the smaller side keeps <8 expected
    # survivors in the merged bottom-k — the estimate is noise, not evidence.
    power = max(1, cfg.sketch_k // 8)
    tables = sorted(sketches)
    for i, a in enumerate(tables):
        for b in tables[i + 1 :]:
            for col_a, sk_a in sketches[a].items():
                for col_b, sk_b in sketches[b].items():
                    if not sk_a or not sk_b:
                        continue
                    key = (a, b, frozenset({_norm(col_a), _norm(col_b)}))
                    if key in existing:
                        continue
                    norm_a, norm_b = _norm(col_a), _norm(col_b)
                    if norm_a == norm_b:
                        home = _sketch_home(norm_a)
                        if home is not None and home not in (a, b):
                            collapsed += 1  # holder↔holder: the home edge wins
                            continue
                        if (
                            home is None
                            and len(norm_holders.get(norm_a, ())) > cfg.max_tables_per_key
                        ):
                            collapsed += 1  # widely shared, no home: refuse
                            continue
                    card_a, card_b = cards[(a, col_a)], cards[(b, col_b)]
                    c_ab, c_ba = _containments(sk_a, sk_b, cfg.sketch_k)
                    best = max(c_ab, c_ba)
                    if best < cfg.sketch_min_containment:
                        if (
                            min(card_a, card_b) > _ENUM_DOMAIN_MAX
                            and max(card_a, card_b)
                            > power * min(card_a, card_b)
                        ):
                            lopsided += 1
                        continue
                    # The CONTAINED side must itself look like a key domain.
                    # A tiny domain is contained in almost anything — status
                    # codes 1..8 sit inside any dense id range 1..N — so its
                    # containment is no evidence at all (the tiny⊂huge trap,
                    # which also subsumes the tiny⊂tiny shared-code-list
                    # case). A genuinely tiny-but-real FK degrades to the
                    # authors' live probing: precision over recall.
                    contained_card = card_a if c_ab >= c_ba else card_b
                    if contained_card <= _ENUM_DOMAIN_MAX:
                        continue
                    contained_tbl, contained_col = (
                        (a, col_a) if c_ab >= c_ba else (b, col_b)
                    )
                    if _is_self_key(contained_col, contained_tbl):
                        pk_contained += 1  # a PK contained elsewhere ≠ an FK
                        continue
                    containing_tbl, containing_col = (
                        (b, col_b) if c_ab >= c_ba else (a, col_a)
                    )
                    if (
                        _is_self_key(containing_col, containing_tbl)
                        and _type_fam(
                            types[containing_tbl].get(containing_col, "")
                        )
                        == "int"
                        and not _names_table(contained_col, containing_tbl)
                    ):
                        # A dense integer surrogate PK (1..N) numerically
                        # contains EVERY smaller int column — ordering
                        # columns, counts, unrelated FKs alike — so
                        # containment against it proves nothing by itself.
                        # Demand the second, independent witness real FKs
                        # carry: the contained column's NAME must refer to
                        # the containing table (`artist` → `artist.id`,
                        # `begin_area` → `area.id`). Deliberately scoped to
                        # INT-family PKs: hash/UUID keys are sparse domains
                        # where containment alone IS strong evidence, and
                        # non-PK containers (`cdscode`) are exactly the
                        # renamed-key case sketches exist for.
                        unnamed_vs_pk += 1
                        continue
                    existing.add(key)
                    candidates.append(
                        {
                            "left": a,
                            "right": b,
                            "column_l": col_a,
                            "column_r": col_b,
                            "type_l": types[a].get(col_a, ""),
                            "type_r": types[b].get(col_b, ""),
                            "comparable": _comparable(
                                types[a].get(col_a, ""), types[b].get(col_b, "")
                            ),
                            "via": "sketch",
                            "sketch_containment": round(best, 3),
                        }
                    )
    if (pk_contained or collapsed or unnamed_vs_pk) and notes is not None:
        notes.append(
            f"sketch: suppressed {pk_contained} pair(s) whose contained side "
            f"is its own table's PK (dense-id coincidence, not FK evidence), "
            f"{collapsed} same-named holder pair(s) (collapsed toward the "
            f"column's home table), and {unnamed_vs_pk} pair(s) contained in "
            "a dense int PK without a name link (values prove nothing "
            "against 1..N)"
        )
    if lopsided and notes is not None:
        notes.append(
            f"sketch: {lopsided} key-sized column pair(s) too lopsided to "
            f"compare (distinct-count ratio beyond ~k/8, k={cfg.sketch_k}) — "
            "containment there is invisible to the sketch, not refuted; a "
            "renamed key between very differently-sized key spaces falls "
            "back to live probing"
        )


def _revalidate_sketch_candidates(
    cands: list[dict[str, Any]], tables_meta: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Cached sketch candidates that still exist in the CURRENT catalog,
    with types refreshed (the cached copy predates any schema change)."""
    out: list[dict[str, Any]] = []
    for c in cands:
        left, right = c.get("left"), c.get("right")
        col_l, col_r = c.get("column_l"), c.get("column_r")
        if not all(isinstance(v, str) and v for v in (left, right, col_l, col_r)):
            continue
        if left not in tables_meta or right not in tables_meta:
            continue
        cols_l = _top_level_columns(tables_meta[left])
        cols_r = _top_level_columns(tables_meta[right])
        if col_l not in cols_l or col_r not in cols_r:
            continue
        cand = dict(c)
        cand["type_l"], cand["type_r"] = cols_l[col_l], cols_r[col_r]
        cand["comparable"] = _comparable(cand["type_l"], cand["type_r"])
        cand["via"] = "sketch"
        out.append(cand)
    return out


def _spread_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order candidates so a bound probe cap is SPREAD, not first-come.

    Enumeration order is alphabetical by key name, so a bound cap would
    starve late-alphabet tables entirely and drop sketch nominations first
    (they are appended last). Greedy instead: repeatedly take the pair whose
    endpoint tables are least covered so far (min endpoint count, then the
    pair total) — every table earns its FIRST sheet before any hub earns its
    twelfth — tie-broken by sketch nominations first (a skipped name pair is
    one columns.tsv grep away for an author; a skipped renamed-key pair is
    unrecoverable live), then enumeration order (deterministic). O(n²) over
    at most a few hundred candidates — microseconds, no queries.
    """
    coverage: dict[str, int] = {}

    def _rank(item: tuple[int, dict[str, Any]]) -> tuple[int, int, int, int]:
        idx, c = item
        l_cov = coverage.get(c["left"], 0)
        r_cov = coverage.get(c["right"], 0)
        return (
            min(l_cov, r_cov),
            l_cov + r_cov,
            0 if c.get("via") == "sketch" else 1,
            idx,
        )

    remaining = list(enumerate(candidates))
    ordered: list[dict[str, Any]] = []
    while remaining:
        item = min(remaining, key=_rank)
        remaining.remove(item)
        cand = item[1]
        ordered.append(cand)
        for t in (cand["left"], cand["right"]):
            coverage[t] = coverage.get(t, 0) + 1
    return ordered


# ---------------------------------------------------------------------------
# sheet rendering
# ---------------------------------------------------------------------------


def _looks_like_shared_enum(stats: dict[str, Any]) -> bool:
    if stats.get("cardinality") != "M:N":
        return False
    for side in (stats.get("left", {}), stats.get("right", {})):
        distinct = int(side.get("distinct_keys") or 0)
        rows = int(side.get("rows") or 0)
        if not (0 < distinct <= _ENUM_DOMAIN_MAX
                and rows >= distinct * _ENUM_MIN_REPEAT):
            return False
    return True


def _verdict(stats: dict[str, Any]) -> str:
    rates = [
        s.get("match_rate")
        for s in (stats.get("left", {}), stats.get("right", {}))
        if s.get("match_rate") is not None
    ]
    best = max(rates) if rates else None
    if best is None:
        return "INCONCLUSIVE — no eligible (non-null-key) rows"
    if best <= _REFUTED_RATE:
        return (
            f"REFUTED — best match rate {best:.0%}; almost certainly a name "
            "coincidence, not a relationship. Do NOT document this join."
        )
    # The shared-enum signature trumps a numerically-good match rate: high
    # containment between two tiny, heavily-repeated value domains is what a
    # SHARED CODE LIST looks like, not a key relationship.
    if _looks_like_shared_enum(stats):
        left_d = stats.get("left", {}).get("distinct_keys", 0)
        right_d = stats.get("right", {}).get("distinct_keys", 0)
        return (
            f"SUSPECT — best match rate {best:.0%}, but neither side is "
            f"key-unique and both value domains are tiny ({left_d} vs "
            f"{right_d} distinct over many rows): this looks like a SHARED "
            "CODE LIST, not a join relationship. Do NOT document it as a "
            "join without independent evidence (a context doc, a matching "
            "FK naming pattern); if the shared codes are meaningful, the "
            "right home is an enum/named-set reference instead."
        )
    if best >= _HOLDS_RATE:
        return f"HOLDS — {stats['cardinality']}"
    return (
        f"WEAK — {stats['cardinality']}, best match rate {best:.0%}. "
        "Investigate before documenting (a partial overlap can still be a "
        "real join with a story — or a trap)."
    )


def _side_lines(name: str, s: dict[str, Any]) -> str:
    rate = s.get("match_rate")
    rate_txt = f"{rate:.1%}" if rate is not None else "n/a"
    return (
        f"- `{name}`: {s.get('rows', 0):,} rows, "
        f"{s.get('null_key_rows', 0):,} null-key rows, "
        f"{s.get('distinct_keys', 0):,} distinct keys; "
        f"**{rate_txt}** of non-null rows have a match"
    )


def _render_join_sheet(
    cand: dict[str, Any],
    stats: dict[str, Any] | None,
    orphans: dict[str, list[dict[str, Any]]],
) -> str:
    left, right = cand["left"], cand["right"]
    col_l, col_r = cand["column_l"], cand["column_r"]
    on = (
        f"`{col_l}`"
        if col_l == col_r
        else f"`{left}.{col_l}` = `{right}.{col_r}`"
    )
    title = f"# Join evidence: `{left}` ↔ `{right}` — on {on}"
    parts = [title, ""]
    if cand.get("via") == "role":
        role_col, key_col = (
            (col_l, col_r) if len(_norm(col_l)) > len(_norm(col_r)) else (col_r, col_l)
        )
        parts.append(
            f"- Nominated as a ROLE-named key: `{role_col}` reads as a "
            f"role-qualified reference to `{key_col}` (e.g. home/away, "
            "from/to). Name the ROLE in the join doc's usage guidance."
        )
    if cand.get("via") == "sketch":
        est = cand.get("sketch_containment") or 0.0
        parts.append(
            f"- Nominated by VALUE-SKETCH containment (~{est:.0%} estimated): "
            "the column names DIFFER, so the numbers alone don't prove a "
            "relationship — confirm the semantic link (what business entity "
            "do both sides identify?) before documenting."
        )
    if stats is None:  # type-incompatible: refused before any query
        parts += [
            f"- Verdict: TYPE MISMATCH — `{col_l}` is `{cand['type_l']}` in "
            f"`{left}` but `{col_r}` is `{cand['type_r']}` in `{right}`. A "
            "join requires an explicit cast; if you document it, bake the "
            "cast into the `ON` clause and verify it live with `validate_join`.",
            "",
        ]
        return "\n".join(parts) + _footer("mismatch")
    parts += [f"- Verdict: {_verdict(stats)}", ""]
    parts.append(_side_lines(left, stats.get("left", {})))
    parts.append(_side_lines(right, stats.get("right", {})))
    for side_name in (left, right):
        rows = orphans.get(side_name) or []
        if rows:
            parts += [
                "",
                f"## Orphan sample — `{side_name}` rows with no match",
                "",
            ]
            for r in rows[:_ORPHAN_SAMPLE_N]:
                cells = ", ".join(f"{k}: {_fmt(v)}" for k, v in list(r.items())[:8])
                parts.append(f"- {{{cells}}}")
    parts.append("")
    return "\n".join(parts) + _footer()


def _sampled_verdict(rate: float | None, full_unique: bool, full_name: str) -> str:
    if rate is None:
        return "INCONCLUSIVE — no eligible (non-null-key) rows in the sample"
    card = (
        f"`{full_name}` key is unique (N:1 toward `{full_name}` likely)"
        if full_unique
        else f"`{full_name}` key is NOT unique"
    )
    if rate >= _HOLDS_RATE:
        return f"HOLDS (sampled) — ~{rate:.0%} of sampled rows resolve; {card}"
    if rate <= _REFUTED_RATE:
        return (
            f"REFUTED (sampled) — ~{rate:.0%} of sampled rows resolve; "
            "almost certainly not a relationship."
        )
    return (
        f"WEAK (sampled) — ~{rate:.0%} of sampled rows resolve; {card}. "
        "Investigate before documenting."
    )


def _render_sampled_join_sheet(
    cand: dict[str, Any],
    stats: dict[str, Any],
    orphans: dict[str, list[dict[str, Any]]],
    *,
    sampled_side: str,
    percent: float,
) -> str:
    left, right = cand["left"], cand["right"]
    col_l, col_r = cand["column_l"], cand["column_r"]
    full_side = right if sampled_side == left else left
    on = (
        f"`{col_l}`"
        if col_l == col_r
        else f"`{left}.{col_l}` = `{right}.{col_r}`"
    )
    s = stats["sampled"]
    f = stats["full"]
    rate = s.get("match_rate")
    rate_txt = f"{rate:.1%}" if rate is not None else "n/a"
    parts = [
        f"# Join evidence: `{left}` ↔ `{right}` — on {on}",
        "",
        f"> **INDICATIVE — `{sampled_side}` probed from a ~{percent:g}% "
        "sample** (it exceeds the full-probe size gate). Only ONE direction "
        f"is measured — sampled `{sampled_side}` rows resolving into the "
        f"full `{full_side}` — because the reverse (and `{sampled_side}`'s "
        "own key cardinality) cannot be measured against a sample without "
        "bias. Split-based sampling can skew rates when data is clustered "
        "by the key (e.g. time-partitioned facts); treat the rate as an "
        "estimate.",
        "",
        f"- Verdict: {_sampled_verdict(rate, stats.get('full_unique', False), full_side)}",
        "",
        f"- `{sampled_side}` (sample): {s.get('rows', 0):,} rows, "
        f"{s.get('null_key_rows', 0):,} null-key rows, "
        f"{s.get('distinct_keys', 0):,} distinct keys in the sample; "
        f"**{rate_txt}** have a match in `{full_side}`",
        f"- `{full_side}` (full): {f.get('rows', 0):,} rows, "
        f"{f.get('null_key_rows', 0):,} null-key rows, "
        f"{f.get('distinct_keys', 0):,} distinct keys",
    ]
    rows = orphans.get(sampled_side) or []
    if rows:
        parts += [
            "",
            f"## Orphan sample — sampled `{sampled_side}` rows with no match",
            "",
        ]
        for r in rows[:_ORPHAN_SAMPLE_N]:
            cells = ", ".join(f"{k}: {_fmt(v)}" for k, v in list(r.items())[:8])
            parts.append(f"- {{{cells}}}")
    parts.append("")
    return "\n".join(parts) + _footer("sampled")


def _render_grain_sheet(table: str, results: list[dict[str, Any]]) -> str:
    parts = [f"# Grain evidence: `{table}`", ""]
    for r in results:
        keys = ", ".join(f"`{c}`" for c in r["key_columns"])
        stats = r["stats"]
        if stats.get("is_unique") is None:
            parts.append(f"- ({keys}): probe failed — {_fmt(stats.get('note', ''))}")
        elif stats["is_unique"]:
            parts.append(
                f"- ({keys}): **UNIQUE** over {stats['total_rows']:,} rows — "
                f'state the grain as "one row per {keys}" (verified)'
            )
        else:
            parts.append(
                f"- ({keys}): NOT unique — {stats['duplicate_key_groups']:,} "
                f"duplicate key group(s), worst fan-out "
                f"{stats['max_rows_per_key']}. The true grain is coarser; "
                "investigate what the duplicates represent."
            )
            for dup in (stats.get("sample_duplicates") or [])[:3]:
                cells = ", ".join(f"{k}: {_fmt(v)}" for k, v in dup.items())
                parts.append(f"  - duplicate: {{{cells}}}")
    parts.append("")
    return "\n".join(parts) + _footer()


def _footer(kind: str = "full") -> str:
    """Per-sheet closing guidance. Kind-aware because the blanket 'full-scan,
    never re-probe' text contradicts the two sheet types that say otherwise
    on the same page: sampled sheets carry an INDICATIVE banner (estimates,
    not full scans), and TYPE MISMATCH sheets explicitly ask for a live
    cast-join verification."""
    if kind == "sampled":
        return (
            "\n> Measured at snapshot time by the same SQL cores as "
            "`validate_join`, from ONE sample draw — these are estimates, "
            "per the INDICATIVE banner above. Still do not re-run this "
            "sampled probe: another draw costs a real scan and answers "
            "nothing new. In the docs, record proportions + mechanism per "
            "the authoring skill, never these raw counts.\n"
        )
    if kind == "mismatch":
        return (
            "\n> No probe ran — the engine would reject the raw `=`. The "
            "type mismatch itself is this run's evidence; if the join is "
            "worth documenting, verify the cast-join live with "
            "`validate_join` (the one case a sheet ASKS for a live probe). "
            "In the docs, record proportions + mechanism per the authoring "
            "skill, never raw counts.\n"
        )
    return (
        "\n> Measured at snapshot time by the same probes as "
        "`validate_join`/`check_grain` (full-scan aggregates, not samples). "
        "Trust these numbers as this run's evidence — do not re-probe what a "
        "sheet already answers. In the docs, record proportions + mechanism "
        "per the authoring skill, never these raw counts.\n"
    )


# ---------------------------------------------------------------------------
# the precompute
# ---------------------------------------------------------------------------


def _pair_fingerprint(
    fp_left: str, fp_right: str, column: str
) -> str:
    raw = f"{fp_left}|{fp_right}|{column}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _subject(cand: dict[str, Any]) -> str:
    """Deterministic sheet/manifest id for one candidate pair. Same-name
    pairs keep the compact historical form; differing spellings (normalized
    twins, role keys) carry both sides so home/away-style siblings don't
    collide on one filename. A component that itself contains a separator
    (dbt-style ``stg__orders`` tables) would make the id ambiguous —
    ('a__b','c') and ('a','b__c') read identically — so such pairs carry a
    short hash of the raw tuple to keep every pair on its own file."""
    left, right = cand["left"], cand["right"]
    col_l, col_r = cand["column_l"], cand["column_r"]
    base = (
        f"{left}__{right}--{col_l}"
        if col_l == col_r
        else f"{left}__{right}--{col_l}--{col_r}"
    )
    if any("__" in p or "--" in p for p in (left, right, col_l, col_r)):
        raw = "\x1f".join((left, right, col_l, col_r))
        base += "--x" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return base


def write_relationship_evidence(
    source: Source,
    meta_root: Path,
    *,
    tables_meta: dict[str, dict[str, Any]],
    cache: _CachedRelationships,
    profile_mode: str = "full",
    changed_tables: frozenset[str] | set[str] = frozenset(),
    cfg: RelationshipConfig | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Probe the enumerated candidates and write the evidence sheets. Never raises.

    Reuse policy mirrors ``write_profiles``: ``"full"`` probes everything
    fresh; ``"incremental"`` re-probes only subjects touching
    ``changed_tables`` (plus fingerprint mismatches); ``"cross"`` re-probes
    only fingerprint-mismatched/missing subjects.
    """
    cfg = cfg or RelationshipConfig.from_env()
    out: dict[str, Any] = {
        "joins_probed": 0,
        "grain_probed": 0,
        "cached": 0,
        "skipped": 0,
        "files": [],
    }
    if not cfg.enabled:
        return out
    table_ref = getattr(source, "sql_table_ref", None)
    if not callable(table_ref):
        log.info(
            "Source %s lacks sql_table_ref; skipping relationship evidence",
            getattr(source, "name", "?"),
        )
        return out
    # The pass is RUNNING from here on — the feed uses this to distinguish
    # "ran and found no candidates" (worth a line: a dataset whose keys defeat
    # every nominator looks identical to a broken pass otherwise — seen live
    # on california_schools) from "didn't run at all" (disabled/unsupported).
    out["ran"] = True

    try:
        root = meta_root / RELATIONSHIPS_DIR
        deadline = time.monotonic() + cfg.budget_s
        now = datetime.now(timezone.utc).isoformat()
        manifest = [_MANIFEST_HEADER]
        mp = getattr(source, "metadata_profile", None)
        shape_keys = tuple(getattr(mp, "bytesize_param_keys", ()) or ()) + tuple(
            getattr(mp, "rowcount_param_keys", ()) or ()
        )
        byte_keys = tuple(getattr(mp, "bytesize_param_keys", ()) or ())
        fps = {
            t: table_fingerprint(m, data_shape=_data_shape(m, shape_keys))
            for t, m in tables_meta.items()
        }

        def _tick(done: int, total: int, label: str) -> None:
            # One coalescing live-feed bar for the whole pass (phase
            # "relationships"); totals re-base per stage (sketching tables →
            # probing joins → grain) and the label names the stage. Ticks at
            # the TOP of each unit; a final tick completes each stage.
            if progress is None or not total:
                return
            try:
                progress("relationships", done, total, label)
            except Exception:  # noqa: BLE001 - a tick must never break the pass
                pass

        def _changed(*tables: str) -> bool:
            return any(t in changed_tables for t in tables)

        # Catalog hint -> source-measured fallback -> assume large. The
        # fallback matters in practice: DDL-registered tables carry NO
        # totalSize Parameter (only crawlers/ETL write it), and pure
        # assume-large skipped EVERY probe on such catalogs (seen live:
        # "44 skipped" on a 13-table dataset). estimate_table_bytes lists
        # the table's S3 objects — no query, no scan — and early-exits at
        # the gate; only when even that can't tell (a view, listing denied)
        # does the table count as oversized.
        measured: dict[str, int | None] = {}

        def _table_bytes(t: str) -> int | None:
            hint = _size_hint(tables_meta[t], byte_keys)
            if hint is not None:
                return hint
            if t not in measured:
                est = getattr(source, "estimate_table_bytes", None)
                try:
                    measured[t] = (
                        est(t, stop_at=cfg.max_table_bytes)
                        if callable(est)
                        else None
                    )
                except Exception:  # noqa: BLE001 - can't tell -> assume large
                    measured[t] = None
            return measured[t]

        def _oversized(*tables: str) -> bool:
            for t in tables:
                size = _table_bytes(t)
                if size is None or size > cfg.max_table_bytes:
                    return True
            return False

        measured_exact: dict[str, int | None] = {}

        def _exact_bytes(t: str) -> int | None:
            """A size fit for computing a SAMPLE PERCENT. The gate measurement
            early-exits at the threshold, so it is only a lower bound — a 5 TB
            table 'measures' as ~the gate and the percent would over-sample it
            by orders of magnitude. This one lists to completion (stop_at=None)
            and returns None when even that can't tell (page cap, LF-governed
            location) — the caller then skips instead of guessing."""
            hint = _size_hint(tables_meta[t], byte_keys)
            if hint is not None:
                return hint
            if t not in measured_exact:
                est = getattr(source, "estimate_table_bytes", None)
                try:
                    measured_exact[t] = (
                        est(t, stop_at=None) if callable(est) else None
                    )
                except Exception:  # noqa: BLE001 - can't tell -> skip
                    measured_exact[t] = None
            return measured_exact[t]

        def _reusable(kind: str, subject: str, fp: str, *tables: str) -> bool:
            if cache.fingerprints.get((kind, subject)) != fp:
                return False
            if (kind, subject) not in cache.sheets:
                return False
            if profile_mode == "incremental":
                return not _changed(*tables)
            return profile_mode == "cross"

        def _emit(kind: str, subject: str, fp: str, status: str) -> None:
            manifest.append(f"{kind}\t{subject}\t{fp}\t{status}\t{now}")

        def _reuse(kind: str, subject: str, rel: str) -> None:
            write_text(root / rel, cache.sheets[(kind, subject)])
            old = cache.manifest_rows[(kind, subject)].split("\t")
            manifest.append(
                f"{kind}\t{subject}\t{old[2]}\tcached\t{old[4]}"
            )
            out["cached"] += 1
            out["files"].append(f"{RELATIONSHIPS_DIR}/{rel}")

        # --- grain (FIRST: one cheap aggregate per table, high author
        # value — running it after a candidate-flooded join loop starved
        # it to zero sheets on MusicBrainz) ------------------------------
        grain_sets = enumerate_grain_candidates(tables_meta, cfg)
        total_grain = len(grain_sets)
        for _gi, (table, key_sets) in enumerate(grain_sets.items(), start=1):
            _tick(_gi, total_grain, f"grain {table}")
            rel = f"grain/{table}.md"
            fp = fps[table]
            if _reusable("grain", table, fp, table):
                _reuse("grain", table, rel)
                continue
            if time.monotonic() > deadline:
                # Before _oversized, which may issue S3 LIST calls.
                _emit("grain", table, fp, "skipped-budget")
                out["skipped"] += 1
                continue
            if _oversized(table):
                # The grain probe is a whole-table GROUP BY — the same scan
                # gate as joins applies (and no hint = assume large).
                _emit("grain", table, fp, "skipped-size")
                out["skipped"] += 1
                continue
            results = [
                {"key_columns": ks, "stats": grain_stats(source, table_ref(table), ks)}
                for ks in key_sets
            ]
            write_text(root / rel, _render_grain_sheet(table, results))
            out["files"].append(f"{RELATIONSHIPS_DIR}/{rel}")
            failed = next(
                (r for r in results if r["stats"].get("is_unique") is None),
                None,
            )
            if failed is not None:
                # An engine failure is NOT reusable evidence: only 'ok' rows
                # are cache-eligible, and a throttled probe cached as ok would
                # be served to every incremental/cross run until the table's
                # fingerprint happened to change. The sheet still ships (it
                # may carry the other key's result); the row does not.
                _emit(
                    "grain", table, fp,
                    f"error: {_fmt(failed['stats'].get('note'))}",
                )
                out["skipped"] += 1
            else:
                _emit("grain", table, fp, "ok")
                out["grain_probed"] += 1

        _tick(total_grain, total_grain, "grain done")

        # --- joins -------------------------------------------------------
        candidates, notes = enumerate_join_candidates(tables_meta, cfg)
        # Value-sketch nomination (name-blind). FULL runs only: incremental/
        # cross runs reuse cached sheets and don't discover new cross-table
        # relationships; re-sketching every table there would cost scans for
        # nominations the mode never probes. Sketch nominations are the ONE
        # candidate source that does not re-enumerate from the catalog, so
        # they persist in candidates.json: without it the first non-full run
        # would silently drop every sketch-discovered sheet.
        sketch_cands: list[dict[str, Any]] = list(cache.sketch_candidates)
        if cfg.sketch_enabled and profile_mode == "full":
            sketches, sk_notes = _collect_sketches(
                source,
                tables_meta,
                cfg,
                skip_table=lambda t: _oversized(t),
                # The sketch scan gets HALF the budget, hard: on a 237-table
                # catalog the collection alone can eat the whole wall clock
                # and leave zero seconds for the probes it exists to feed
                # (seen live on MusicBrainz: ~59 of 3,505 pairs probed).
                past_deadline=lambda: time.monotonic()
                > deadline - cfg.budget_s / 2,
                on_progress=lambda i, n, t: _tick(i, n, f"sketching {t}"),
            )
            notes.extend(sk_notes)
            _sketch_nominate(sketches, tables_meta, candidates, cfg, notes=notes)
            # Full = fresh: this run's nominations replace the cached list
            # (re-sketching every full run is what ages out junk pairs).
            sketch_cands = [c for c in candidates if c.get("via") == "sketch"]
        elif cfg.sketch_enabled:
            sketch_cands = _revalidate_sketch_candidates(
                cache.sketch_candidates, tables_meta
            )
            known = {
                (c["left"], c["right"],
                 frozenset({_norm(c["column_l"]), _norm(c["column_r"])}))
                for c in candidates
            }
            for c in sketch_cands:
                key = (c["left"], c["right"],
                       frozenset({_norm(c["column_l"]), _norm(c["column_r"])}))
                if key not in known:
                    known.add(key)
                    candidates.append(c)
        for note in notes:
            log.info("Relationship enumeration: %s", note)

        # The cap bounds PROBES (engine cost), not candidates: cache
        # reuses, TYPE MISMATCH sheets, and size-skips are free and never
        # consume a slot — by construction no slot can be wasted on a pair
        # that yields no evidence, and a re-run's budget goes entirely to
        # NEW probes instead of being eaten by cached sheets. When more
        # candidates than slots exist, the probe budget is SPREAD (see
        # _spread_candidates) instead of first-come-first-served.
        if len(candidates) > cfg.max_pairs:
            candidates = _spread_candidates(candidates)
        probes_launched = 0
        total_cands = len(candidates)
        for _ci, cand in enumerate(candidates, start=1):
            _tick(_ci, total_cands, f"join {cand['left']} ↔ {cand['right']}")
            left, right = cand["left"], cand["right"]
            col_l, col_r = cand["column_l"], cand["column_r"]
            subject = _subject(cand)
            rel = f"joins/{subject}.md"
            fp = _pair_fingerprint(fps[left], fps[right], f"{col_l}={col_r}")
            if _reusable("join", subject, fp, left, right):
                _reuse("join", subject, rel)
                continue
            if not cand["comparable"]:
                # No query CAN run (the engine would reject the '='), but the
                # mismatch itself is evidence worth a sheet.
                write_text(root / rel, _render_join_sheet(cand, None, {}))
                _emit("join", subject, fp, "ok")
                out["joins_probed"] += 1
                out["files"].append(f"{RELATIONSHIPS_DIR}/{rel}")
                continue
            if time.monotonic() > deadline:
                # Budget BEFORE any size resolution: sizing an unmeasured
                # table costs up to 50 S3 LIST calls per table, and a
                # post-budget pair is a budget skip, not a size skip.
                _emit("join", subject, fp, "skipped-budget")
                out["skipped"] += 1
                continue
            if probes_launched >= cfg.max_pairs:
                # Probe budget spent. Also before sizing — skipped-cap must
                # not keep billing S3 LIST calls for pairs it won't probe.
                _emit("join", subject, fp, "skipped-cap")
                out["skipped"] += 1
                continue
            sizes = {t: _table_bytes(t) for t in (left, right)}
            over = [
                t
                for t, b in sizes.items()
                if b is None or b > cfg.max_table_bytes
            ]
            sampled_side: str | None = None
            if len(over) == 1 and sizes[over[0]] is not None:
                # The enterprise fact→dim shape: ONE measured-big side. Probe
                # it SAMPLED against the full small side — the one direction
                # sampling leaves unbiased. Requires the source to sample by
                # reference (Athena TABLESAMPLE SYSTEM); without it, skip.
                if callable(getattr(source, "sql_sampled_ref", None)):
                    sampled_side = over[0]
            if over and sampled_side is None:
                _emit("join", subject, fp, "skipped-size")
                out["skipped"] += 1
                continue
            l_ref, r_ref = table_ref(left), table_ref(right)
            if sampled_side is not None:
                exact = _exact_bytes(sampled_side)
                if not exact:
                    _emit("join", subject, fp, "skipped-size")
                    out["skipped"] += 1
                    continue
                pct = max(
                    0.01,
                    min(100.0, cfg.sample_target_bytes / exact * 100),
                )
                probes_launched += 1  # committed: queries run from here on
                if sampled_side == left:
                    s_ref = source.sql_sampled_ref(l_ref, pct)
                    stats = sampled_join_stats(
                        source, s_ref, [col_l], r_ref, [col_r]
                    )
                else:
                    s_ref = source.sql_sampled_ref(r_ref, pct)
                    stats = sampled_join_stats(
                        source, s_ref, [col_r], l_ref, [col_l]
                    )
                if not stats.get("sampled"):
                    _emit("join", subject, fp, f"error: {_fmt(stats.get('note'))}")
                    out["skipped"] += 1
                    continue
                orphans = {}
                rate = stats["sampled"].get("match_rate")
                if rate is not None and rate < 1.0:
                    # Orphans from the SAMPLED side only (they are real rows
                    # with genuinely no match; the full side's "orphans"
                    # against a sample would be artifacts).
                    if sampled_side == left:
                        orphans[left] = sample_orphans(
                            source, s_ref, [col_l], r_ref, [col_r],
                            n=_ORPHAN_SAMPLE_N,
                        )
                    else:
                        orphans[right] = sample_orphans(
                            source, s_ref, [col_r], l_ref, [col_l],
                            n=_ORPHAN_SAMPLE_N,
                        )
                write_text(
                    root / rel,
                    _render_sampled_join_sheet(
                        cand, stats, orphans,
                        sampled_side=sampled_side, percent=pct,
                    ),
                )
                _emit("join", subject, fp, "ok-sampled")
                out["joins_probed"] += 1
                out["files"].append(f"{RELATIONSHIPS_DIR}/{rel}")
                continue
            probes_launched += 1  # committed: queries run from here on
            stats = join_stats(source, l_ref, [col_l], r_ref, [col_r])
            if not stats.get("left"):
                _emit("join", subject, fp, f"error: {_fmt(stats.get('note'))}")
                out["skipped"] += 1
                continue
            orphans: dict[str, list[dict[str, Any]]] = {}
            for name, ref, cols, other_ref, other_cols in (
                (left, l_ref, [col_l], r_ref, [col_r]),
                (right, r_ref, [col_r], l_ref, [col_l]),
            ):
                side = stats["left"] if name == left else stats["right"]
                rate = side.get("match_rate")
                if rate is not None and rate < 1.0:
                    orphans[name] = sample_orphans(
                        source, ref, cols, other_ref, other_cols,
                        n=_ORPHAN_SAMPLE_N,
                    )
            write_text(root / rel, _render_join_sheet(cand, stats, orphans))
            _emit("join", subject, fp, "ok")
            out["joins_probed"] += 1
            out["files"].append(f"{RELATIONSHIPS_DIR}/{rel}")
        _tick(total_cands, total_cands, "done")
        if out["files"] or out["skipped"]:
            write_text(root / MANIFEST_NAME, "\n".join(manifest) + "\n")
            out["files"].append(f"{RELATIONSHIPS_DIR}/{MANIFEST_NAME}")
            write_text(
                root / _CANDIDATES_NAME,
                json.dumps(sketch_cands, indent=1) + "\n",
            )
            out["files"].append(f"{RELATIONSHIPS_DIR}/{_CANDIDATES_NAME}")
    except Exception:  # noqa: BLE001 — evidence is an accelerator, never a gate
        log.warning("Relationship evidence pass failed (continuing)", exc_info=True)
    return out
