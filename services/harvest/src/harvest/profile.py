"""Snapshot-time column profiles under ``.metadata/profile/``.

One markdown sheet per table — null share, approximate distinct count, min/max,
and top-K values for low-cardinality columns — computed with the source's live
query engine ONCE at harvest start, so the table-author sub-agents read a file
instead of hand-writing the same probe queries (null share, enum scan, range
check) per table.

Cost posture (the reason this module is more than one query):

* **Scan budget.** A table whose byte-size hint exceeds
  ``OKF_HARVEST_PROFILE_SAMPLE_ABOVE_BYTES`` is profiled from a sample sized to
  ``OKF_HARVEST_PROFILE_TARGET_SAMPLE_BYTES`` (a table with NO size hint is
  treated as large; Iceberg tables — whose Glue Parameters never carry Hive
  stats — are sized exactly from their ``$files`` metadata sum via the
  source's ``iceberg_data_bytes`` capability before that default applies).
  Everything derived from a sample is stamped INDICATIVE —
  the sheet says so in a banner, per column, and in the manifest — because a
  sampled value list is never proof of a closed enum.
* **Bounded enumeration.** Only columns whose approximate distinct count is at
  most ``OKF_HARVEST_PROFILE_ENUM_MAX_DISTINCT`` get a value list, capped at
  ``OKF_HARVEST_PROFILE_TOPK`` values; higher-cardinality columns report the
  approximate count only ("not enumerated") so a hundreds-of-values legend
  never floods the sheet or the scan bill.
* **Reuse across runs.** ``.metadata/`` persists on the mount between runs, so
  the previous run's sheets are readable at the next run's start (before the
  snapshot wipe). A per-table fingerprint (catalog update time + version +
  column set) decides reuse: incremental runs re-profile only the changed
  tables, cross runs only fingerprint-mismatched/missing ones, full runs
  everything (a full harvest is the explicit "re-read the data" action).
* **Best-effort.** Any query failure downgrades to a per-table note in the
  manifest — profiling can never fail the snapshot, and a source that lacks
  the SQL capabilities below simply gets no profiles.

Source capabilities (looked up with ``getattr`` so injected fakes without them
keep working): ``sql_table_ref(table)`` (a fully-qualified, quoted table
reference), ``sql_approx_distinct(col_sql)`` (engine's approximate-distinct
aggregate; falls back to exact ``COUNT(DISTINCT …)``), and
``sql_sample_clause(percent)`` → ``(from_suffix, where_predicate)`` (how the
engine samples; without it a too-large table is skipped, never full-scanned).
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
from harvest.source_base import Source

log = logging.getLogger(__name__)

PROFILE_DIR = "profile"
MANIFEST_NAME = "manifest.tsv"
#: Machine-readable companion to the sheets: the enum-like columns' observed
#: value lists ({table: {column: {values, distinct, exhaustive}}} + a
#: per-table profiled_at). The sheets are for the authoring agent; this file
#: feeds the Attested Computations ADVISORY parameter-value check (a miss
#: warns, never refuses — data may have evolved since the scan; see
#: okf_core.computations) plus lint's enum-vs-evidence check and the UI's
#: type-ahead. Carried across runs under the sheets' fingerprint policy,
#: like relationships/evidence.json.
_DOMAINS_NAME = "domains.json"
_MANIFEST_HEADER = "table\tfingerprint\tstatus\tsample_pct\tprofiled_at"

#: Types eligible for profiling (top-level, primitive). Two type vocabularies
#: meet here: Hive/Glue names AND the PostgreSQL names Redshift's
#: SVV_ALL_COLUMNS emits (``numeric(10,2)``, ``real``, ``text``, ``character
#: varying(255)``, ``time[stamp] with/without time zone`` — see
#: redshift_source._format_type). Prefix matching absorbs the length/precision
#: suffixes; "int" also covers "integer", "char" covers "character varying",
#: "double" covers "double precision", "time" covers time AND timestamp with
#: or without tz. Complex/binary columns are listed on the sheet as "not
#: profiled" so their absence is not ambiguous.
_PROFILABLE_PREFIXES = (
    "tinyint", "smallint", "int", "bigint", "float", "double", "decimal",
    "string", "varchar", "char", "boolean", "date", "timestamp",
    "numeric", "real", "text", "time", "bpchar", "nchar", "nvarchar",
)
#: Of those, the ones MIN/MAX is meaningful and safe for.
_ORDERABLE_PREFIXES = (
    "tinyint", "smallint", "int", "bigint", "float", "double", "decimal",
    "string", "varchar", "char", "date", "timestamp",
    "numeric", "real", "text", "time", "bpchar", "nchar", "nvarchar",
)
_VALUE_MAX_CHARS = 48  # rendered min/max/top-K cell cap — sheets stay greppable


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _accepts_kwarg(fn: Any, name: str) -> bool:
    """True when ``fn`` can take ``name`` as a keyword (incl. **kwargs)."""
    if not callable(fn):
        return False
    try:
        import inspect

        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):  # builtins/C callables: can't tell
        return False
    if name in params:
        return True
    return any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


@dataclass(frozen=True)
class ProfileConfig:
    """Env-derived knobs (documented in CONVENTIONS.md)."""

    enabled: bool = True
    sample_above_bytes: int = 1 << 30       # 1 GiB
    target_sample_bytes: int = 256 << 20    # 256 MiB
    enum_max_distinct: int = 50
    topk: int = 20
    max_enum_queries: int = 15
    max_columns: int = 100
    budget_s: int = 1800
    # Per-query ceiling. Deliberately as large as the whole pass: a profile
    # that COMPLETES yields a sheet plus a fingerprint-cached size
    # measurement, while one cancelled at 60s bills a partial scan for
    # nothing (Athena charges bytes scanned up to cancellation). The pass
    # budget still bounds the wall clock — _run clamps every query's timeout
    # to the time remaining, so one slow table can consume the budget but
    # never overrun it.
    query_timeout_s: int = 1800

    @classmethod
    def from_env(cls) -> "ProfileConfig":
        return cls(
            enabled=os.environ.get("OKF_HARVEST_PROFILE_ENABLED", "1") != "0",
            sample_above_bytes=_env_int(
                "OKF_HARVEST_PROFILE_SAMPLE_ABOVE_BYTES", 1 << 30
            ),
            target_sample_bytes=_env_int(
                "OKF_HARVEST_PROFILE_TARGET_SAMPLE_BYTES", 256 << 20
            ),
            enum_max_distinct=_env_int("OKF_HARVEST_PROFILE_ENUM_MAX_DISTINCT", 50),
            topk=_env_int("OKF_HARVEST_PROFILE_TOPK", 20),
            max_enum_queries=_env_int("OKF_HARVEST_PROFILE_MAX_ENUM_QUERIES", 15),
            max_columns=_env_int("OKF_HARVEST_PROFILE_MAX_COLUMNS", 100),
            budget_s=_env_int("OKF_HARVEST_PROFILE_BUDGET_S", 1800),
            query_timeout_s=_env_int("OKF_HARVEST_PROFILE_QUERY_TIMEOUT_S", 1800),
        )


@dataclass
class _CachedProfiles:
    """The previous run's sheets, read from the mount BEFORE the snapshot wipe."""

    fingerprints: dict[str, str] = field(default_factory=dict)
    manifest_rows: dict[str, str] = field(default_factory=dict)  # table -> raw row
    sheets: dict[str, str] = field(default_factory=dict)         # table -> markdown
    # table -> machine-readable enum domains (see _DOMAINS_NAME) — carried
    # forward for reused sheets under the same fingerprint policy.
    domains: dict[str, dict[str, Any]] = field(default_factory=dict)


def table_fingerprint(meta: dict[str, Any], *, data_shape: str = "") -> str:
    """Catalog identity of a table's profile: update time + version + column set.

    A schema change always changes the fingerprint (the column set is part of
    it), so a cached sheet can never describe columns that no longer exist.
    ``data_shape`` folds in the source's byte-size/row-count hints (see
    :func:`_data_shape`): for a catalog that carries no update time or version
    — Redshift — it is the only signal that a data reload happened, and
    without it a cached sheet would outlive the data it describes.
    """
    cols = ",".join(
        f"{f.get('name')}:{f.get('type')}"
        for f in (meta.get("flat_schema") or [])
    )
    raw = (
        f"{meta.get('update_time')}|{meta.get('version_id')}|{data_shape}|{cols}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _data_shape(meta: dict[str, Any], keys: tuple[str, ...]) -> str:
    """The table's size/row-count hints rendered as a stable fingerprint part."""
    params = meta.get("parameters")
    if not isinstance(params, dict):
        return ""
    return ",".join(
        f"{k}={params[k]}" for k in keys if params.get(k) not in (None, "")
    )


def read_cached_profiles(dataset_root: str | Path) -> _CachedProfiles:
    """Load the previous run's profile dir (call BEFORE the ``.metadata`` wipe)."""
    cache = _CachedProfiles()
    root = Path(dataset_root) / ".metadata" / PROFILE_DIR
    dom_file = root / _DOMAINS_NAME
    if dom_file.is_file():
        try:
            data = json.loads(dom_file.read_text(encoding="utf-8"))
            for table, entry in (data.get("tables") or {}).items():
                if isinstance(entry, dict):
                    cache.domains[table] = entry
        except (OSError, UnicodeDecodeError, ValueError):
            pass  # corrupt domains = none (the sheets are still the cache)
    manifest = root / MANIFEST_NAME
    if not manifest.is_file():
        return cache
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        # A truncated/corrupt manifest (a prior run killed mid-flush on the S3
        # mount) must read as "no cache". Letting it raise would fail the whole
        # .metadata export — persistently, because this runs BEFORE the
        # snapshot wipe that would clear the corrupt file.
        return cache
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) != 5 or parts[2] not in ("ok", "cached"):
            continue  # only successful sheets are reusable
        table, fp = parts[0], parts[1]
        sheet = root / f"{table}.md"
        try:
            cache.sheets[table] = sheet.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        cache.fingerprints[table] = fp
        cache.manifest_rows[table] = line
    return cache


def _size_hint_bytes(meta: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    params = meta.get("parameters")
    if not isinstance(params, dict):
        return None
    for key in keys:
        val = params.get(key)
        try:
            n = int(str(val))
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return None


def _profilable_columns(meta: dict[str, Any], cfg: ProfileConfig) -> tuple[
    list[dict[str, Any]], list[str]
]:
    """(columns to profile, top-level column names skipped as unprofilable)."""
    cols: list[dict[str, Any]] = []
    skipped: list[str] = []
    for f in meta.get("flat_schema") or []:
        if int(f.get("depth") or 0) != 0:
            continue
        name = f.get("name") or ""
        typ = (f.get("type") or "").lower()
        if typ.startswith(_PROFILABLE_PREFIXES):
            cols.append(f)
        else:
            skipped.append(name)
    return cols[: cfg.max_columns], skipped


def _fmt_value(v: Any) -> str:
    s = "NULL" if v is None else str(v)
    s = s.replace("\n", " ").replace("\t", " ")
    return s if len(s) <= _VALUE_MAX_CHARS else s[: _VALUE_MAX_CHARS - 1] + "…"


def _to_int(v: Any) -> int:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return 0


class TableProfiler:
    """Profiles one source's tables within a shared wall-clock budget."""

    def __init__(self, source: Source, cfg: ProfileConfig) -> None:
        self.source = source
        self.cfg = cfg
        self._deadline = time.monotonic() + cfg.budget_s
        # Capability lookup: fakes without the SQL atoms simply get no profiles.
        self._table_ref = getattr(source, "sql_table_ref", None)
        self._approx = getattr(source, "sql_approx_distinct", None)
        self._sample = getattr(source, "sql_sample_clause", None)
        self._iceberg_bytes = getattr(source, "iceberg_data_bytes", None)
        self._takes_stats = _accepts_kwarg(
            getattr(source, "run_query", None), "stats"
        )

    @property
    def supported(self) -> bool:
        return callable(self._table_ref)

    def out_of_budget(self) -> bool:
        return time.monotonic() > self._deadline

    def _run(
        self, sql: str, stats: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        # Clamp to the remaining pass budget: at a 30-min per-query ceiling an
        # unclamped query started near the deadline would overrun the whole
        # pass by its own length (at the old 60s ceiling the overrun was
        # noise). ``stats`` is the caller-owned sink run_query fills with the
        # execution's data_scanned_bytes; it is only forwarded to sources
        # whose run_query accepts it (checked once at init) — the
        # measurement is optional, and a signature-mismatch retry would
        # re-bill the query.
        remaining = self._deadline - time.monotonic()
        timeout = max(1.0, min(float(self.cfg.query_timeout_s), remaining))
        kwargs: dict[str, Any] = {"timeout_s": timeout}
        if stats is not None and self._takes_stats:
            kwargs["stats"] = stats
        return self.source.run_query(sql, **kwargs)

    def _distinct_sql(self, col_sql: str) -> str:
        if callable(self._approx):
            return self._approx(col_sql)
        return f"COUNT(DISTINCT {col_sql})"

    def profile_table(
        self, table: str, meta: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
        """Return ``(markdown_sheet, sample_pct, domains, table_stats)`` —
        sample_pct "" = full scan; ``domains`` maps enum-like columns to their
        observed value lists (``{values, distinct, exhaustive}``) for
        ``domains.json``; ``table_stats`` is ``{"scanned_bytes", "columns"}``
        — the pass-1 execution's data_scanned_bytes (a real size measurement
        of the profilable columns: BERNOULLI reads full bytes, so even a
        sampled profile measures the true footprint) plus per-column
        ``{distinct, null_pct}`` the relationship pass uses to rank sketch
        columns.

        Raises on query failure; the caller records the error in the manifest.
        """
        cfg = self.cfg
        ref = self._table_ref(table)  # type: ignore[misc]
        cols, unprofilable = _profilable_columns(meta, cfg)
        if not cols:
            raise RuntimeError("no profilable (top-level primitive) columns")

        size = _size_hint_bytes(
            meta, getattr(
                self.source.metadata_profile, "bytesize_param_keys", ()
            ),
        )
        if size is None and callable(self._iceberg_bytes):
            # Iceberg tables never carry Hive stats Parameters, but their own
            # metadata knows the current snapshot's exact size ($files). The
            # capability answers None for non-Iceberg tables without querying,
            # so the Hive assume-large default below is untouched.
            try:
                size = self._iceberg_bytes(table)
            except Exception:  # noqa: BLE001 - sizing is best-effort
                size = None
        # No size hint => assume large: an unbudgeted full scan is the one
        # outcome this module exists to prevent.
        needs_sample = size is None or size > cfg.sample_above_bytes
        sample_pct = ""
        from_suffix, where_pred = "", ""
        if needs_sample:
            if not callable(self._sample):
                raise RuntimeError(
                    "table exceeds the profile scan budget and the source "
                    "cannot sample"
                )
            pct = 100.0
            if size:
                pct = max(0.01, min(100.0, cfg.target_sample_bytes / size * 100))
            else:
                pct = 10.0
            sample_pct = f"{pct:g}"
            from_suffix, where_pred = self._sample(pct)

        where_sql = f" WHERE {where_pred}" if where_pred else ""
        from_sql = f"{ref} {from_suffix}".strip()

        # Pass 1 — one scan: row count + per-column null/distinct/min/max.
        selects = ["COUNT(*) AS _n"]
        for i, c in enumerate(cols):
            col_sql = f'"{c.get("name")}"'
            selects.append(f"COUNT({col_sql}) AS nn_{i}")
            selects.append(f"{self._distinct_sql(col_sql)} AS d_{i}")
            if (c.get("type") or "").lower().startswith(_ORDERABLE_PREFIXES):
                selects.append(f"MIN({col_sql}) AS mn_{i}")
                selects.append(f"MAX({col_sql}) AS mx_{i}")
        scan_stats: dict[str, Any] = {}
        rows = self._run(
            f"SELECT {', '.join(selects)} FROM {from_sql}{where_sql}",
            stats=scan_stats,
        )
        agg = rows[0] if rows else {}
        n = _to_int(agg.get("_n"))
        column_stats: dict[str, Any] = {}
        for i, c in enumerate(cols):
            entry: dict[str, Any] = {"distinct": _to_int(agg.get(f"d_{i}"))}
            if n:
                entry["null_pct"] = round(
                    100.0 * (1 - _to_int(agg.get(f"nn_{i}")) / n), 1
                )
            column_stats[c.get("name") or ""] = entry

        # Pass 2 — top-K values for the most enum-like columns only.
        enum_candidates = sorted(
            (
                (i, c)
                for i, c in enumerate(cols)
                if 0 < _to_int(agg.get(f"d_{i}")) <= cfg.enum_max_distinct
            ),
            key=lambda ic: _to_int(agg.get(f"d_{ic[0]}")),
        )[: cfg.max_enum_queries]
        topk: dict[str, list[tuple[str, int]]] = {}
        # Same groups UNFORMATTED, for the machine-readable domains below.
        raw_topk: dict[str, list[Any]] = {}
        for i, c in enum_candidates:
            if self.out_of_budget():
                break
            col_sql = f'"{c.get("name")}"'
            try:
                vals = self._run(
                    f"SELECT {col_sql} AS v, COUNT(*) AS n FROM {from_sql}"
                    f"{where_sql} GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {cfg.topk}"
                )
            except Exception:  # noqa: BLE001 - a failed enum scan loses one list
                continue
            topk[c.get("name") or ""] = [
                (_fmt_value(r.get("v")), _to_int(r.get("n"))) for r in vals
            ]
            raw_topk[c.get("name") or ""] = [r.get("v") for r in vals]

        # Machine-readable domains for the computations advisory layer. Two rules make these
        # values usable as SQL literals, unlike the sheet's display strings:
        #
        # * VERBATIM values — the sheet truncates at `_VALUE_MAX_CHARS` with a
        #   `…` suffix and renders NULL as the word "NULL". A truncated value
        #   can never match the real data (the advisory would warn about a
        #   value that exists, and the UI would suggest one that returns zero
        #   rows), and `= 'NULL'` is not how SQL matches nulls (`is null` is).
        # * EXHAUSTIVE is proven by the GROUP BY, not by comparing against the
        #   APPROXIMATE distinct count: asking for `topk` groups and getting
        #   fewer means the scan enumerated the whole column. `d_<i>` is
        #   `approx_distinct` (~2% error), so `distinct <= len(values)` could
        #   stamp a cap-truncated list "exhaustive". When the proof holds, the
        #   count is the exact number of values we hold.
        domains: dict[str, Any] = {}
        for i, c in enum_candidates:
            name = c.get("name") or ""
            groups = raw_topk.get(name)
            if not groups:
                continue
            values = [str(v) for v in groups if v is not None]
            if not values:
                continue
            exhaustive = not sample_pct and len(groups) < cfg.topk
            domains[name] = {
                "values": values,
                "distinct": len(values) if exhaustive else _to_int(agg.get(f"d_{i}")),
                "exhaustive": exhaustive,
            }

        scanned = scan_stats.get("data_scanned_bytes")
        table_stats: dict[str, Any] = {
            "scanned_bytes": int(scanned) if scanned is not None else None,
            "columns": column_stats,
        }
        return (
            _render_sheet(
                table, cols, unprofilable, agg, n, topk,
                sample_pct=sample_pct, cfg=cfg, size_hint=size,
            ),
            sample_pct,
            domains,
            table_stats,
        )


def _render_sheet(
    table: str,
    cols: list[dict[str, Any]],
    unprofilable: list[str],
    agg: dict[str, Any],
    n: int,
    topk: dict[str, list[tuple[str, int]]],
    *,
    sample_pct: str,
    cfg: ProfileConfig,
    size_hint: int | None,
) -> str:
    sampled = bool(sample_pct)
    title = f"# Column profile: `{table}`"
    parts = [title, ""]
    if sampled:
        parts += [
            f"> **INDICATIVE — profiled from a ≈{sample_pct}% sample** "
            f"({n:,} rows scanned). Every statistic below is an estimate from "
            "that sample: value lists are NOT exhaustive enums, null shares "
            "and ranges are approximate, and a value absent here may still "
            "exist in the table. Verify with `run_sql` before documenting a "
            "legend or invariant.",
            "",
        ]
    else:
        parts += [
            f"> Full-scan profile ({n:,} rows). Distinct counts are "
            "approximate (~). Value lists are capped at the top "
            f"{cfg.topk} by frequency — a list at the cap is NOT proof of a "
            "closed enum.",
            "",
        ]
    if size_hint:
        parts.append(f"- Size hint: {size_hint:,} bytes (catalog, unverified)")
    parts += [
        f"- Columns profiled: {len(cols)}"
        + (f" (unprofilable, complex/binary: {', '.join(unprofilable)})"
           if unprofilable else ""),
        "",
        "| Column | Type | Null share | ~Distinct | Min | Max |",
        "|---|---|---|---|---|---|",
    ]
    for i, c in enumerate(cols):
        name = c.get("name") or ""
        nn = _to_int(agg.get(f"nn_{i}"))
        d = _to_int(agg.get(f"d_{i}"))
        null_pct = f"{(1 - nn / n) * 100:.1f}%" if n else "?"
        mn = agg.get(f"mn_{i}")
        mx = agg.get(f"mx_{i}")
        parts.append(
            f"| `{name}` | {c.get('type')} | {null_pct} | ~{d:,} | "
            f"{_fmt_value(mn) if mn is not None else ''} | "
            f"{_fmt_value(mx) if mx is not None else ''} |"
        )
    if topk:
        parts += [
            "",
            f"## Low-cardinality values (top {cfg.topk} by frequency"
            + (", from the sample — indicative only" if sampled else "")
            + ")",
        ]
        for col, vals in topk.items():
            d = next(
                (
                    _to_int(agg.get(f"d_{i}"))
                    for i, c in enumerate(cols)
                    if (c.get("name") or "") == col
                ),
                0,
            )
            suffix = " — list may be incomplete" if len(vals) >= cfg.topk else ""
            parts += ["", f"### `{col}` (~{d} distinct{suffix})", ""]
            parts += [f"- `{v}` — {cnt:,}" for v, cnt in vals]
    high_card = [
        (c.get("name") or "", _to_int(agg.get(f"d_{i}")))
        for i, c in enumerate(cols)
        if _to_int(agg.get(f"d_{i}")) > cfg.enum_max_distinct
    ]
    if high_card:
        parts += [
            "",
            "## High-cardinality columns (values not enumerated)",
            "",
        ]
        parts += [f"- `{name}` — ~{d:,} distinct" for name, d in high_card]
    return "\n".join(parts) + "\n"


def write_profiles(
    source: Source,
    meta_root: Path,
    *,
    tables_meta: dict[str, dict[str, Any]],
    cache: _CachedProfiles,
    profile_mode: str = "full",
    changed_tables: frozenset[str] | set[str] = frozenset(),
    cfg: ProfileConfig | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Write ``.metadata/profile/`` for this run. Never raises.

    Reuse policy by ``profile_mode``: ``"full"`` profiles everything fresh;
    ``"incremental"`` re-profiles only ``changed_tables`` (plus any table whose
    fingerprint changed — a schema change invalidates its cache regardless);
    ``"cross"`` re-profiles only fingerprint-mismatched or missing tables.
    """
    cfg = cfg or ProfileConfig.from_env()
    out: dict[str, Any] = {
        "profiled": 0, "cached": 0, "skipped": 0, "files": [],
        # {table: {"scanned_bytes": int|None, "columns": {col: {distinct,
        # null_pct}}}} — fresh measurements plus cache-carried ones. The
        # relationship pass consumes this: scanned_bytes is a sizing rung
        # (a completed profile scan IS a size measurement) and the column
        # stats rank sketch columns.
        "profile_stats": {},
    }
    if not cfg.enabled:
        return out
    profiler = TableProfiler(source, cfg)
    if not profiler.supported:
        log.info("Source %s has no SQL profile capabilities; skipping profiles",
                 getattr(source, "name", "?"))
        return out

    profile_root = meta_root / PROFILE_DIR
    now = datetime.now(timezone.utc).isoformat()
    manifest_lines = [_MANIFEST_HEADER]
    # Machine-readable enum domains (see _DOMAINS_NAME): fresh probes add
    # entries, reused sheets carry theirs forward.
    domains: dict[str, dict[str, Any]] = {}
    mp = getattr(source, "metadata_profile", None)
    shape_keys = tuple(getattr(mp, "bytesize_param_keys", ()) or ()) + tuple(
        getattr(mp, "rowcount_param_keys", ()) or ()
    )

    # Live-feed progress: one coalescing tick per table (phase "profiles").
    # Best-effort like everything here — a broken callback never breaks the
    # pass. Ticks fire at the TOP of each iteration ("working on i of N");
    # the final tick after the loop completes the bar.
    total_tables = len(tables_meta)

    def _tick(done: int, label: str) -> None:
        if progress is None or not total_tables:
            return
        try:
            progress("profiles", done, total_tables, label)
        except Exception:  # noqa: BLE001 - a progress tick must never break the pass
            pass

    for _i, (table, meta) in enumerate(tables_meta.items(), start=1):
        _tick(_i, table)
        fp = table_fingerprint(meta, data_shape=_data_shape(meta, shape_keys))
        cached_ok = cache.fingerprints.get(table) == fp and table in cache.sheets
        reuse = cached_ok and (
            (profile_mode == "incremental" and table not in changed_tables)
            or profile_mode == "cross"
        )
        if reuse:
            write_text(profile_root / f"{table}.md", cache.sheets[table])
            old = cache.manifest_rows[table].split("\t")
            manifest_lines.append(
                f"{table}\t{fp}\tcached\t{old[3]}\t{old[4]}"
            )
            cached_domains = cache.domains.get(table)
            if cached_domains:
                domains[table] = cached_domains
                # Carry the prior run's measurement forward with the sheet:
                # the fingerprint (update time + version + column set + size
                # hints) vouches that the data it measured is unchanged.
                stats_entry = {
                    "scanned_bytes": cached_domains.get("scanned_bytes"),
                    "columns": cached_domains.get("column_stats") or {},
                }
                if stats_entry["scanned_bytes"] is not None or stats_entry["columns"]:
                    out["profile_stats"][table] = stats_entry
            out["cached"] += 1
            out["files"].append(f"profile/{table}.md")
            continue
        if profiler.out_of_budget():
            manifest_lines.append(f"{table}\t{fp}\tskipped-budget\t\t{now}")
            out["skipped"] += 1
            continue
        try:
            sheet, sample_pct, table_domains, table_stats = (
                profiler.profile_table(table, meta)
            )
        except Exception as e:  # noqa: BLE001 - per-table best-effort
            log.info("Profile failed for table %s: %s", table, e)
            manifest_lines.append(
                f"{table}\t{fp}\terror\t\t{now}"
            )
            out["skipped"] += 1
            continue
        write_text(profile_root / f"{table}.md", sheet)
        manifest_lines.append(f"{table}\t{fp}\tok\t{sample_pct}\t{now}")
        # Every profiled table gets a domains.json entry now — the sibling
        # scanned_bytes/column_stats keys ride the same file (and its cache
        # carry) so the measurement survives runs. flatten_domains ignores
        # entries whose columns are empty, so downstream consumers are
        # unaffected.
        entry: dict[str, Any] = {"profiled_at": now, "columns": table_domains}
        if table_stats.get("scanned_bytes") is not None:
            entry["scanned_bytes"] = table_stats["scanned_bytes"]
        if table_stats.get("columns"):
            entry["column_stats"] = table_stats["columns"]
        if sample_pct:
            entry["sample_pct"] = sample_pct
        if table_domains or len(entry) > 2:
            domains[table] = entry
        if table_stats.get("scanned_bytes") is not None or table_stats.get("columns"):
            out["profile_stats"][table] = table_stats
        out["profiled"] += 1
        out["files"].append(f"profile/{table}.md")

    _tick(total_tables, "done")
    if out["files"] or out["skipped"]:
        write_text(profile_root / MANIFEST_NAME, "\n".join(manifest_lines) + "\n")
        write_text(
            profile_root / _DOMAINS_NAME,
            json.dumps({"version": 1, "tables": domains}, indent=1, sort_keys=True)
            + "\n",
        )
        out["files"].append(f"profile/{_DOMAINS_NAME}")
    return out
