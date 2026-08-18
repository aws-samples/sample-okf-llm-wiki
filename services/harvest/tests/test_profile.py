"""Column profiles: scan budget, sampling markers, top-K bounds, cache reuse.

The load-bearing properties, per the cost posture in harvest/profile.py:
a too-large table is never full-scanned (sampled, or skipped when the source
cannot sample); anything sampled is explicitly INDICATIVE; value lists never
enumerate high-cardinality columns; and incremental/cross runs reuse the
previous run's sheets instead of re-scanning unimpacted tables.
"""

from __future__ import annotations

from pathlib import Path

from harvest.profile import (
    MANIFEST_NAME,
    PROFILE_DIR,
    ProfileConfig,
    read_cached_profiles,
    table_fingerprint,
    write_profiles,
)
from harvest.source_base import SourceMetadataProfile

CFG = ProfileConfig(
    sample_above_bytes=1000,
    target_sample_bytes=100,
    enum_max_distinct=5,
    topk=3,
    max_enum_queries=2,
    budget_s=600,
    query_timeout_s=5,
)


class _Src:
    """Fake source: canned pass-1 aggregates + pass-2 top-K per column."""

    metadata_profile = SourceMetadataProfile(
        label="Glue",
        catalog_name="Glue Data Catalog",
        resource_label="Resource (ARN)",
        rowcount_param_keys=("recordCount",),
        bytesize_param_keys=("totalSize",),
    )
    name = "fake"

    def __init__(self, *, agg=None, topk=None, can_sample=True,
                 scanned_bytes=None):
        self.agg = agg or {}
        self.topk = topk or {}
        self.queries: list[str] = []
        self.query_kwargs: list[dict] = []
        self.scanned_bytes = scanned_bytes  # fills the stats sink when set
        if not can_sample:
            # Simulate a source without the sampling atom at all.
            self.sql_sample_clause = None  # getattr() sees a non-callable

    def sql_table_ref(self, table: str) -> str:
        return f'"db"."{table}"'

    def sql_approx_distinct(self, col_sql: str) -> str:
        return f"approx_distinct({col_sql})"

    def sql_sample_clause(self, percent: float):
        return (f"TABLESAMPLE BERNOULLI ({percent:g})", "")

    def run_query(self, query, **kwargs):
        self.queries.append(query)
        self.query_kwargs.append(kwargs)
        sink = kwargs.get("stats")
        if sink is not None and self.scanned_bytes is not None:
            sink["data_scanned_bytes"] = self.scanned_bytes
        if "GROUP BY" in query:
            col = query.split('"')[1]
            return self.topk.get(col, [])
        return [self.agg]


def _meta(*cols, size=None, update="2026-01-01"):
    return {
        "update_time": update,
        "version_id": "1",
        "parameters": {"totalSize": str(size)} if size else {},
        "flat_schema": [
            {"name": n, "type": t, "depth": 0} for n, t in cols
        ],
    }


def _small_table_agg():
    # 100 rows; status: 4 distinct, no nulls; driver_ref: 80 distinct, 10 nulls.
    return {
        "_n": "100",
        "nn_0": "100", "d_0": "4", "mn_0": "DNF", "mx_0": "Winner",
        "nn_1": "90", "d_1": "80", "mn_1": "1", "mx_1": "99",
    }


def test_full_scan_sheet_and_manifest(tmp_path: Path):
    src = _Src(
        agg=_small_table_agg(),
        topk={"status": [
            {"v": "Finished", "n": "60"}, {"v": "DNF", "n": "30"},
            {"v": "Winner", "n": "10"},
        ]},
    )
    meta = {"races": _meta(("status", "string"), ("driver_ref", "bigint"),
                           size=500)}
    out = write_profiles(
        src, tmp_path, tables_meta=meta,
        cache=read_cached_profiles(tmp_path), cfg=CFG,
    )
    assert out["profiled"] == 1
    sheet = (tmp_path / PROFILE_DIR / "races.md").read_text()
    assert "INDICATIVE" not in sheet                     # under the byte threshold
    assert "Full-scan profile (100 rows)" in sheet
    assert "| `status` | string | 0.0% | ~4 |" in sheet
    assert "`Finished` — 60" in sheet
    # High-cardinality column: counted, never enumerated.
    assert "`driver_ref` — ~80 distinct" in sheet
    assert "10.0%" in sheet                              # driver_ref null share
    manifest = (tmp_path / PROFILE_DIR / MANIFEST_NAME).read_text()
    assert "races\t" in manifest and "\tok\t\t" in manifest  # empty sample_pct


def test_large_table_is_sampled_and_marked_indicative(tmp_path: Path):
    src = _Src(agg=_small_table_agg())
    meta = {"big": _meta(("status", "string"), ("driver_ref", "bigint"),
                         size=100_000)}
    write_profiles(src, tmp_path, tables_meta=meta,
                   cache=read_cached_profiles(tmp_path), cfg=CFG)
    sheet = (tmp_path / PROFILE_DIR / "big.md").read_text()
    assert "INDICATIVE" in sheet and "sample" in sheet
    assert "TABLESAMPLE BERNOULLI (0.1)" in src.queries[0]  # 100/100000*100
    manifest = (tmp_path / PROFILE_DIR / MANIFEST_NAME).read_text()
    assert "\tok\t0.1\t" in manifest


def test_no_size_hint_is_treated_as_large(tmp_path: Path):
    src = _Src(agg=_small_table_agg())
    meta = {"mystery": _meta(("status", "string"))}
    write_profiles(src, tmp_path, tables_meta=meta,
                   cache=read_cached_profiles(tmp_path), cfg=CFG)
    assert "TABLESAMPLE" in src.queries[0]
    assert "INDICATIVE" in (tmp_path / PROFILE_DIR / "mystery.md").read_text()


def test_large_table_without_sampling_capability_is_skipped(tmp_path: Path):
    src = _Src(agg=_small_table_agg(), can_sample=False)
    meta = {"big": _meta(("status", "string"), size=100_000)}
    out = write_profiles(src, tmp_path, tables_meta=meta,
                         cache=read_cached_profiles(tmp_path), cfg=CFG)
    assert out["profiled"] == 0 and out["skipped"] == 1
    assert not (tmp_path / PROFILE_DIR / "big.md").exists()
    assert src.queries == []  # the budget rule: never full-scan a large table
    assert "\terror\t" in (tmp_path / PROFILE_DIR / MANIFEST_NAME).read_text()


def test_incremental_reuses_unchanged_tables(tmp_path: Path):
    meta = {
        "races": _meta(("status", "string"), size=500),
        "results": _meta(("pos", "bigint"), size=500),
    }
    src = _Src(agg=_small_table_agg())
    write_profiles(src, tmp_path / ".metadata", tables_meta=meta,
                   cache=read_cached_profiles(tmp_path), cfg=CFG)
    first_queries = len(src.queries)
    races_sheet = (tmp_path / ".metadata" / PROFILE_DIR / "races.md").read_text()

    # Next run: only `results` changed. The cache read happens BEFORE the
    # snapshot wipe, exactly as export_metadata sequences it.
    cache = read_cached_profiles(tmp_path)
    src2 = _Src(agg=_small_table_agg())
    out = write_profiles(src2, tmp_path / ".metadata", tables_meta=meta,
                         cache=cache, profile_mode="incremental",
                         changed_tables={"results"}, cfg=CFG)
    assert out["cached"] == 1 and out["profiled"] == 1
    assert all('"results"' in q or "GROUP BY" in q for q in src2.queries)
    reused = (tmp_path / ".metadata" / PROFILE_DIR / "races.md").read_text()
    assert reused == races_sheet
    manifest = (
        tmp_path / ".metadata" / PROFILE_DIR / MANIFEST_NAME
    ).read_text()
    assert "races\t" in manifest and "\tcached\t" in manifest
    assert first_queries > 0


def test_schema_change_invalidates_the_cache(tmp_path: Path):
    meta = {"races": _meta(("status", "string"), size=500)}
    src = _Src(agg=_small_table_agg())
    write_profiles(src, tmp_path / ".metadata", tables_meta=meta,
                   cache=read_cached_profiles(tmp_path), cfg=CFG)
    cache = read_cached_profiles(tmp_path)

    changed = {"races": _meta(("status", "string"), ("new_col", "bigint"),
                              size=500)}
    assert table_fingerprint(changed["races"]) != table_fingerprint(meta["races"])
    src2 = _Src(agg=_small_table_agg())
    out = write_profiles(src2, tmp_path / ".metadata", tables_meta=changed,
                         cache=cache, profile_mode="incremental",
                         changed_tables=set(), cfg=CFG)
    # Not in changed_tables, but the fingerprint moved -> re-profiled anyway.
    assert out["profiled"] == 1 and out["cached"] == 0


def test_budget_exhaustion_skips_not_scans(tmp_path: Path):
    src = _Src(agg=_small_table_agg())
    cfg = ProfileConfig(
        sample_above_bytes=1000, target_sample_bytes=100, enum_max_distinct=5,
        topk=3, max_enum_queries=2, budget_s=0, query_timeout_s=5,
    )
    meta = {"races": _meta(("status", "string"), size=500)}
    out = write_profiles(src, tmp_path, tables_meta=meta,
                         cache=read_cached_profiles(tmp_path), cfg=cfg)
    assert out["skipped"] == 1 and src.queries == []
    assert "skipped-budget" in (
        tmp_path / PROFILE_DIR / MANIFEST_NAME
    ).read_text()


def test_disabled_via_env_writes_nothing(tmp_path: Path):
    src = _Src(agg=_small_table_agg())
    cfg = ProfileConfig(enabled=False)
    out = write_profiles(src, tmp_path, tables_meta={"t": _meta(("a", "string"))},
                         cache=read_cached_profiles(tmp_path), cfg=cfg)
    assert out == {
        "profiled": 0, "cached": 0, "skipped": 0, "files": [],
        "profile_stats": {},
    }
    assert not (tmp_path / PROFILE_DIR).exists()


def test_corrupt_cache_manifest_is_no_cache_never_a_crash(tmp_path: Path):
    # A run killed mid-flush can leave an invalid-UTF-8 manifest on the mount.
    # read_cached_profiles runs BEFORE the snapshot wipe — raising here would
    # fail every subsequent .metadata export until someone cleans the mount by
    # hand (the wipe that would remove the corrupt file never runs).
    root = tmp_path / ".metadata" / PROFILE_DIR
    root.mkdir(parents=True)
    (root / MANIFEST_NAME).write_bytes(
        b"table\tfingerprint\tstatus\tsample_pct\tprofiled_at\n\xff\xfe\x80races"
    )
    cache = read_cached_profiles(tmp_path)  # no exception
    assert cache.sheets == {} and cache.fingerprints == {}


def test_corrupt_cached_sheet_skips_that_table_only(tmp_path: Path):
    root = tmp_path / ".metadata" / PROFILE_DIR
    root.mkdir(parents=True)
    (root / MANIFEST_NAME).write_text(
        "table\tfingerprint\tstatus\tsample_pct\tprofiled_at\n"
        "races\tabc\tok\t\tt0\n"
        "results\tdef\tok\t\tt0\n"
    )
    (root / "races.md").write_bytes(b"\xff\xfe broken")
    (root / "results.md").write_text("# Column profile: `results`\n")
    cache = read_cached_profiles(tmp_path)  # no exception
    assert "races" not in cache.sheets
    assert "results" in cache.sheets


def test_redshift_type_names_are_profilable(tmp_path: Path):
    # Redshift's SVV_ALL_COLUMNS speaks PostgreSQL: numeric(10,2), real, text,
    # time/timestamp with(out) time zone. These are exactly the money/measure
    # columns metrics get built from — they must not land in the
    # "unprofilable, complex/binary" bucket.
    agg = {
        "_n": "100",
        "nn_0": "100", "d_0": "80", "mn_0": "0.5", "mx_0": "99.9",
        "nn_1": "100", "d_1": "80", "mn_1": "1.5", "mx_1": "4.5",
        "nn_2": "100", "d_2": "80", "mn_2": "a", "mx_2": "z",
        "nn_3": "100", "d_3": "80", "mn_3": "00:00:01", "mx_3": "23:59:59",
    }
    src = _Src(agg=agg)
    meta = {"prices": _meta(
        ("price", "numeric(10,2)"), ("rating", "real"),
        ("note", "text"), ("sold_at", "time without time zone"),
        size=500,
    )}
    write_profiles(src, tmp_path, tables_meta=meta,
                   cache=read_cached_profiles(tmp_path), cfg=CFG)
    sheet = (tmp_path / PROFILE_DIR / "prices.md").read_text()
    assert "unprofilable" not in sheet
    for col in ("price", "rating", "note", "sold_at"):
        assert f"| `{col}` |" in sheet
    # All four are orderable — MIN/MAX ride the single pass-1 scan.
    assert 'MIN("price")' in src.queries[0]
    assert 'MAX("sold_at")' in src.queries[0]


def test_data_reload_reprofiles_a_versionless_catalog(tmp_path: Path):
    # Redshift's catalog carries no update_time/version_id, so the size/row
    # hints are the ONLY reload signal: same schema + changed size must
    # invalidate the cross-mode cache (which otherwise reuses wholesale) —
    # without this a cached sheet outlives the data it describes.
    def rs_meta(size):
        return {
            "update_time": None, "version_id": None,
            "parameters": {"totalSize": str(size)},
            "flat_schema": [{"name": "status", "type": "string", "depth": 0}],
        }

    src = _Src(agg=_small_table_agg())
    write_profiles(src, tmp_path / ".metadata",
                   tables_meta={"races": rs_meta(500)},
                   cache=read_cached_profiles(tmp_path), cfg=CFG)

    cache = read_cached_profiles(tmp_path)
    src2 = _Src(agg=_small_table_agg())
    out = write_profiles(src2, tmp_path / ".metadata",
                         tables_meta={"races": rs_meta(700)},
                         cache=cache, profile_mode="cross", cfg=CFG)
    assert out["profiled"] == 1 and out["cached"] == 0

    # Unchanged shape -> still reused (the fingerprint is stable, not volatile).
    cache2 = read_cached_profiles(tmp_path)
    src3 = _Src(agg=_small_table_agg())
    out2 = write_profiles(src3, tmp_path / ".metadata",
                          tables_meta={"races": rs_meta(700)},
                          cache=cache2, profile_mode="cross", cfg=CFG)
    assert out2["cached"] == 1 and src3.queries == []


def test_source_without_atoms_is_a_clean_no_op(tmp_path: Path):
    class _NoAtoms:
        metadata_profile = _Src.metadata_profile
        name = "bare"

        def run_query(self, q, **kw):  # pragma: no cover - must not be called
            raise AssertionError("profiling must not query a source w/o atoms")

    out = write_profiles(_NoAtoms(), tmp_path,
                         tables_meta={"t": _meta(("a", "string"))},
                         cache=read_cached_profiles(tmp_path), cfg=CFG)
    assert out["files"] == []



# ---------------------------------------------------------------------------
# domains.json — the machine-readable enum domains the semantic layer folds
# ---------------------------------------------------------------------------


def _domains(root: Path) -> dict:
    import json

    return json.loads((root / PROFILE_DIR / "domains.json").read_text())


def test_domains_json_written_with_exhaustive_flag(tmp_path: Path):
    # A FULL scan that returned FEWER groups than the top-K cap (2 < topk=3)
    # proves the GROUP BY enumerated the whole column -> exhaustive, with the
    # count taken from the values we actually hold (never approx_distinct).
    src = _Src(
        agg={"_n": "100", "nn_0": "100", "d_0": "2", "mn_0": "A", "mx_0": "B"},
        topk={"status": [{"v": "A", "n": "70"}, {"v": "B", "n": "30"}]},
    )
    meta = {"races": _meta(("status", "string"), size=500)}
    write_profiles(src, tmp_path, tables_meta=meta,
                   cache=read_cached_profiles(tmp_path), cfg=CFG)
    entry = _domains(tmp_path)["tables"]["races"]
    assert entry["columns"]["status"] == {
        "values": ["A", "B"], "distinct": 2, "exhaustive": True,
    }
    assert entry["profiled_at"]


def test_domains_at_the_topk_cap_are_never_exhaustive(tmp_path: Path):
    # Exactly `topk` groups came back: the column may hold more values the
    # LIMIT cut off. `d_0` (approx_distinct, ~2% error) must NOT be trusted to
    # settle it — a real 4-distinct column whose estimate reads 3 would
    # otherwise be stamped provably exhaustive.
    src = _Src(
        agg={"_n": "100", "nn_0": "100", "d_0": "3", "mn_0": "A", "mx_0": "C"},
        topk={"status": [
            {"v": "A", "n": "50"}, {"v": "B", "n": "30"}, {"v": "C", "n": "20"},
        ]},
    )
    write_profiles(src, tmp_path, tables_meta={
        "races": _meta(("status", "string"), size=500)
    }, cache=read_cached_profiles(tmp_path), cfg=CFG)
    col = _domains(tmp_path)["tables"]["races"]["columns"]["status"]
    assert col["exhaustive"] is False and col["values"] == ["A", "B", "C"]


def test_domain_values_are_verbatim_and_drop_the_null_group(tmp_path: Path):
    # The SHEET truncates long values and prints NULL as a word; the machine
    # domains must not — a truncated literal matches nothing, and `= 'NULL'`
    # is not how SQL matches nulls.
    long_value = "x" * 120
    src = _Src(
        agg={"_n": "10", "nn_0": "9", "d_0": "2", "mn_0": "a", "mx_0": "z"},
        topk={"path": [
            {"v": long_value, "n": "8"}, {"v": None, "n": "1"},
        ]},
    )
    write_profiles(src, tmp_path, tables_meta={
        "races": _meta(("path", "string"), size=500)
    }, cache=read_cached_profiles(tmp_path), cfg=CFG)
    col = _domains(tmp_path)["tables"]["races"]["columns"]["path"]
    assert col["values"] == [long_value]  # full length, no "…"
    assert "NULL" not in col["values"]
    # The rendered sheet still shows its display forms.
    sheet = (tmp_path / PROFILE_DIR / "races.md").read_text()
    assert "…" in sheet and "NULL" in sheet


def test_domains_from_samples_or_truncated_lists_are_not_exhaustive(tmp_path: Path):
    topk = {"status": [{"v": "A", "n": "5"}, {"v": "B", "n": "3"}, {"v": "C", "n": "1"}]}
    # Sampled (over the byte threshold): never exhaustive.
    src = _Src(
        agg={"_n": "50", "nn_0": "50", "d_0": "3", "mn_0": "A", "mx_0": "C"},
        topk=topk,
    )
    write_profiles(src, tmp_path / "a", tables_meta={
        "races": _meta(("status", "string"), size=10**12)
    }, cache=read_cached_profiles(tmp_path / "a"), cfg=CFG)
    assert (
        _domains(tmp_path / "a")["tables"]["races"]["columns"]["status"]["exhaustive"]
        is False
    )
    # Full scan but top-K-truncated (4 distinct, 3 fetched): not exhaustive.
    src2 = _Src(
        agg={"_n": "100", "nn_0": "100", "d_0": "4", "mn_0": "A", "mx_0": "D"},
        topk=topk,
    )
    write_profiles(src2, tmp_path / "b", tables_meta={
        "races": _meta(("status", "string"), size=500)
    }, cache=read_cached_profiles(tmp_path / "b"), cfg=CFG)
    assert (
        _domains(tmp_path / "b")["tables"]["races"]["columns"]["status"]["exhaustive"]
        is False
    )


def test_domains_carry_forward_on_incremental_reuse(tmp_path: Path):
    meta = {"races": _meta(("status", "string"), size=500)}
    src = _Src(
        agg={"_n": "100", "nn_0": "100", "d_0": "3", "mn_0": "A", "mx_0": "C"},
        topk={"status": [
            {"v": "A", "n": "50"}, {"v": "B", "n": "30"}, {"v": "C", "n": "20"},
        ]},
    )
    write_profiles(src, tmp_path / ".metadata", tables_meta=meta,
                   cache=read_cached_profiles(tmp_path), cfg=CFG)
    # Incremental with nothing changed: the sheet reuses, and the domain entry
    # must survive the carry (a fresh source that would return NO values
    # proves it came from the cache, not a re-probe).
    cache = read_cached_profiles(tmp_path)
    src2 = _Src(agg={}, topk={})
    write_profiles(src2, tmp_path / ".metadata", tables_meta=meta,
                   cache=cache, profile_mode="incremental",
                   changed_tables=frozenset(), cfg=CFG)
    entry = _domains(tmp_path / ".metadata")["tables"]["races"]
    assert entry["columns"]["status"]["values"] == ["A", "B", "C"]
    assert src2.queries == []  # nothing re-probed


# ---------------------------------------------------------------------------
# Iceberg: the $files capability sizes hint-less tables exactly
# ---------------------------------------------------------------------------


def test_iceberg_capability_small_table_gets_exact_full_scan(tmp_path: Path):
    src = _Src(agg=_small_table_agg())
    src.iceberg_data_bytes = lambda table: 500  # under sample_above_bytes=1000
    meta = {"orders": _meta(("status", "string"), ("driver_ref", "bigint"))}
    write_profiles(src, tmp_path, tables_meta=meta,
                   cache=read_cached_profiles(tmp_path), cfg=CFG)
    sheet = (tmp_path / PROFILE_DIR / "orders.md").read_text()
    assert "INDICATIVE" not in sheet          # exact, not assume-large
    assert "TABLESAMPLE" not in src.queries[0]
    manifest = (tmp_path / PROFILE_DIR / MANIFEST_NAME).read_text()
    assert "\tok\t\t" in manifest             # empty sample_pct


def test_iceberg_capability_big_table_sizes_the_sample(tmp_path: Path):
    src = _Src(agg=_small_table_agg())
    src.iceberg_data_bytes = lambda table: 100_000
    meta = {"orders": _meta(("status", "string"))}
    write_profiles(src, tmp_path, tables_meta=meta,
                   cache=read_cached_profiles(tmp_path), cfg=CFG)
    # Percent from the REAL size (100/100000*100 = 0.1), not the 10% fallback.
    assert "TABLESAMPLE BERNOULLI (0.1)" in src.queries[0]
    assert "INDICATIVE" in (tmp_path / PROFILE_DIR / "orders.md").read_text()


def test_iceberg_capability_none_keeps_assume_large(tmp_path: Path):
    src = _Src(agg=_small_table_agg())
    src.iceberg_data_bytes = lambda table: None  # non-Iceberg / query failed
    meta = {"mystery": _meta(("status", "string"))}
    write_profiles(src, tmp_path, tables_meta=meta,
                   cache=read_cached_profiles(tmp_path), cfg=CFG)
    assert "TABLESAMPLE BERNOULLI (10)" in src.queries[0]  # the old fallback


# ---------------------------------------------------------------------------
# Pass-1 measurements: scanned bytes + per-column stats (profile_stats)
# ---------------------------------------------------------------------------


def test_scanned_bytes_and_column_stats_measured_and_persisted(tmp_path: Path):
    src = _Src(agg=_small_table_agg(), scanned_bytes=4321)
    meta = {"races": _meta(("status", "string"), ("driver_ref", "bigint"),
                           size=500)}
    out = write_profiles(src, tmp_path, tables_meta=meta,
                         cache=read_cached_profiles(tmp_path), cfg=CFG)
    stats = out["profile_stats"]["races"]
    assert stats["scanned_bytes"] == 4321
    # 90/100 non-null driver_ref -> 10.0% null share; ~80 distinct.
    assert stats["columns"]["driver_ref"] == {"distinct": 80, "null_pct": 10.0}
    assert stats["columns"]["status"] == {"distinct": 4, "null_pct": 0.0}
    # The measurement rides domains.json so it survives runs with the cache.
    dom = _domains(tmp_path)["tables"]["races"]
    assert dom["scanned_bytes"] == 4321
    assert dom["column_stats"]["status"]["distinct"] == 4


def test_profile_stats_carried_on_incremental_reuse(tmp_path: Path):
    meta = {"races": _meta(("status", "string"), size=500)}
    src = _Src(agg=_small_table_agg(), scanned_bytes=999)
    write_profiles(src, tmp_path / ".metadata", tables_meta=meta,
                   cache=read_cached_profiles(tmp_path), cfg=CFG)
    # Second run, unchanged fingerprint, incremental: sheet reused from cache
    # and the measurement carried with it.
    src2 = _Src(agg=_small_table_agg())
    out = write_profiles(src2, tmp_path / ".metadata", tables_meta=meta,
                         cache=read_cached_profiles(tmp_path),
                         profile_mode="incremental", cfg=CFG)
    assert out["cached"] == 1 and src2.queries == []
    assert out["profile_stats"]["races"]["scanned_bytes"] == 999
    assert out["profile_stats"]["races"]["columns"]["status"]["distinct"] == 4


def test_query_timeout_clamped_to_remaining_budget(tmp_path: Path):
    # A 30-min per-query ceiling must never overrun the pass deadline: with
    # a 3s budget the query gets ~3s, not query_timeout_s.
    cfg = ProfileConfig(
        sample_above_bytes=1000, target_sample_bytes=100, enum_max_distinct=5,
        topk=3, max_enum_queries=2, budget_s=3, query_timeout_s=1800,
    )
    src = _Src(agg=_small_table_agg())
    write_profiles(src, tmp_path,
                   tables_meta={"races": _meta(("status", "string"), size=500)},
                   cache=read_cached_profiles(tmp_path), cfg=cfg)
    assert src.query_kwargs, "no query ran"
    assert all(k["timeout_s"] <= 3.0 for k in src.query_kwargs)
