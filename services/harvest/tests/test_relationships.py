"""harvest.relationships — the snapshot-time join/grain evidence precompute.

Drives the real module against a fake source (dict-row ``run_query``, the
same contract the probe tools use), asserting: candidate enumeration
precision (key-like names only, home-table pairing, ambiguity refusal),
probe-loop outcomes (verdicts, orphan samples, type-mismatch sheets), the
cost caps (budget / pair cap / size gate), the fingerprint cache's per-mode
reuse policy, and that the pass never raises out of the snapshot.
"""

from __future__ import annotations

import time

from harvest.relationships import (
    RelationshipConfig,
    enumerate_grain_candidates,
    enumerate_join_candidates,
    read_cached_relationships,
    write_relationship_evidence,
)


def _meta(columns: dict[str, str], **params) -> dict:
    # A small size hint by default: the gate treats NO hint as oversized
    # (assume-large), so fixtures must look small to be probed at all.
    params.setdefault("totalSize", 1 << 20)
    return {
        "flat_schema": [
            {"name": n, "type": t, "depth": 0} for n, t in columns.items()
        ],
        "parameters": {k: str(v) for k, v in params.items()},
        "update_time": "2026-08-08",
        "version_id": "1",
    }


F1_META = {
    "races": _meta({"raceid": "int", "year": "int", "name": "string"}),
    "results": _meta({"resultid": "int", "raceid": "int", "driverid": "int"}),
    "drivers": _meta({"driverid": "int", "surname": "string"}),
    "standings": _meta({"driverid": "int", "points": "double", "year": "int"}),
}


class _Profile:
    bytesize_param_keys = ("totalSize",)
    rowcount_param_keys = ("recordCount",)


class _FakeSource:
    """Dict-row run_query with scripted per-query results (FIFO by substring)."""

    metadata_profile = _Profile()

    def __init__(self, unique_sides=(), match_rate=1.0):
        self.queries: list[str] = []
        self.unique_sides = set(unique_sides)  # table tokens whose key is unique
        self.match_rate = match_rate

    def sql_table_ref(self, table: str) -> str:
        return f'"db"."{table}"'

    def run_query(self, query, **kwargs):
        self.queries.append(query)
        if "key_groups" in query:  # grain aggregate
            return [{"key_groups": 100, "total_rows": 100,
                     "dupe_groups": 0, "max_per_key": 1}]
        if "LEFT JOIN" in query:  # sampled-side combined scan (one draw)
            return [{"n": 100, "null_keys": 0, "distinct_keys": 40,
                     "m": int(100 * self.match_rate)}]
        if "null_keys" in query:  # join side aggregate
            table = query.rsplit('"db"."', 1)[1].split('"')[0]
            distinct = 100 if table in self.unique_sides else 40
            return [{"n": 100, "null_keys": 0, "distinct_keys": distinct}]
        if "EXISTS" in query and "COUNT" in query:  # matched rows
            return [{"m": int(100 * self.match_rate)}]
        if "NOT EXISTS" in query:  # orphan sample
            return [{"raceid": 99, "name": "Future GP"}]
        return [{}]


CFG = RelationshipConfig(
    enabled=True, budget_s=600, max_pairs=60,
    max_grain_per_table=2, max_tables_per_key=6, max_table_bytes=2 << 30,
)


# ---------------------------------------------------------------------------
# enumeration
# ---------------------------------------------------------------------------


def test_enumeration_pairs_holders_with_the_home_table():
    cands, notes = enumerate_join_candidates(F1_META, CFG)
    pairs = {(c["left"], c["right"], c["column_l"]) for c in cands}
    # raceid: results ↔ races (home). driverid: results ↔ drivers and
    # standings ↔ drivers — never results ↔ standings (both fact sides).
    assert ("races", "results", "raceid") in pairs
    assert ("drivers", "results", "driverid") in pairs
    assert ("drivers", "standings", "driverid") in pairs
    assert not any({a, b} == {"results", "standings"} for a, b, _ in pairs)
    # `year` is shared but not key-like — never probed.
    assert not any(col == "year" for _, _, col in pairs)
    assert notes == []


def test_enumeration_refuses_widely_shared_keys_without_a_home():
    meta = {
        f"t{i}": _meta({"customer_key": "int", "x": "string"}) for i in range(9)
    }
    cfg = RelationshipConfig(max_tables_per_key=6)
    cands, notes = enumerate_join_candidates(meta, cfg)
    assert cands == []
    assert len(notes) == 1 and "customerkey" in notes[0]  # normalized form


def test_enumeration_sees_partition_keys():
    # The hifa bug: a lake-style table whose join key is a PARTITION column
    # (flat_partition_schema, not flat_schema) was invisible to nomination,
    # so the schema's one real pair nominated nothing. Partition columns are
    # queryable like any other and are often exactly the join key.
    countries = _meta({"name": "string"})
    countries["flat_partition_schema"] = [
        {"name": "routen_id", "type": "bigint", "depth": 0}
    ]
    meta = {
        "countries": countries,
        "distribution": _meta({"routen_id": "bigint", "amount": "double"}),
    }
    cands, notes = enumerate_join_candidates(meta, CFG)
    pairs = {(c["left"], c["right"], c["column_l"], c["column_r"]) for c in cands}
    assert ("countries", "distribution", "routen_id", "routen_id") in pairs
    cand = next(c for c in cands if c["left"] == "countries")
    # The partition key's type came through, so the pair is probe-comparable.
    assert cand["type_l"] == "bigint" and cand["comparable"] is True
    assert notes == []


def test_enumeration_flags_type_mismatch_instead_of_dropping():
    meta = {
        "orders": _meta({"customer_id": "string"}),
        "customers": _meta({"customer_id": "bigint", "id": "int"}),
    }
    cands, _ = enumerate_join_candidates(meta, CFG)
    cand = next(c for c in cands if c["column_l"] == "customer_id")
    assert cand["comparable"] is False


def test_grain_candidates_prefer_the_self_naming_key():
    grains = enumerate_grain_candidates(F1_META, CFG)
    assert grains["races"] == [["raceid"]]
    assert grains["drivers"] == [["driverid"]]
    # standings has no self-naming key and no `id` — nothing to probe.
    assert "standings" not in grains


# ---------------------------------------------------------------------------
# the probe loop
# ---------------------------------------------------------------------------


def test_probe_loop_writes_sheets_manifest_and_verdicts(tmp_path):
    src = _FakeSource(unique_sides={"races", "drivers"}, match_rate=0.96)
    out = write_relationship_evidence(
        src, tmp_path, tables_meta=F1_META,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    assert out["joins_probed"] >= 3 and out["grain_probed"] >= 2

    sheet = (
        tmp_path / "relationships" / "joins" / "races__results--raceid.md"
    ).read_text()
    assert "HOLDS" in sheet and ("1:N" in sheet or "N:1" in sheet)
    assert "96.0%" in sheet
    # Sub-100% match rate -> the orphan sample rides the sheet.
    assert "Orphan sample" in sheet and "Future GP" in sheet
    # The footer forbids re-probing and raw-count copying.
    assert "do not re-probe" in sheet
    assert "never these raw counts" in sheet

    grain = (tmp_path / "relationships" / "grain" / "races.md").read_text()
    assert "UNIQUE" in grain and "one row per" in grain

    manifest = (tmp_path / "relationships" / "manifest.tsv").read_text()
    assert "join\traces__results--raceid" in manifest
    assert "grain\traces" in manifest
    assert all(
        line.split("\t")[3] == "ok" for line in manifest.splitlines()[1:]
    )


def test_low_match_rate_is_refuted(tmp_path):
    src = _FakeSource(unique_sides={"races"}, match_rate=0.03)
    write_relationship_evidence(
        src, tmp_path, tables_meta=F1_META,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    sheet = (
        tmp_path / "relationships" / "joins" / "races__results--raceid.md"
    ).read_text()
    assert "REFUTED" in sheet and "Do NOT document" in sheet


def test_type_mismatch_writes_a_sheet_without_querying(tmp_path):
    meta = {
        "orders": _meta({"customer_id": "string"}),
        "customers": _meta({"customer_id": "bigint"}),
    }
    src = _FakeSource()
    write_relationship_evidence(
        src, tmp_path, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    sheet = (
        tmp_path / "relationships" / "joins" / "customers__orders--customer_id.md"
    ).read_text()
    assert "TYPE MISMATCH" in sheet and "cast" in sheet
    # No JOIN probe ran (the '=' would error the engine); the grain pass may
    # still legitimately probe customers.customer_id.
    assert not any("null_keys" in q or "EXISTS" in q for q in src.queries)


def test_size_gate_and_pair_cap_skip_with_notes(tmp_path):
    meta = {
        "big": _meta({"raceid": "int"}, totalSize=str(10 << 30)),
        "races": _meta({"raceid": "int", "name": "string"}),
    }
    src = _FakeSource()
    out = write_relationship_evidence(
        src, tmp_path, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    manifest = (tmp_path / "relationships" / "manifest.tsv").read_text()
    assert "skipped-size" in manifest
    assert out["skipped"] >= 1
    assert not any("EXISTS" in q for q in src.queries)  # no join probes ran


def test_budget_exhaustion_skips_the_rest(tmp_path, monkeypatch):
    from harvest import relationships as rel

    class _Clock:
        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            self.now += CFG.budget_s + 1
            return self.now

    monkeypatch.setattr(rel, "time", _Clock())
    src = _FakeSource()
    out = write_relationship_evidence(
        src, tmp_path, tables_meta=F1_META,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    assert out["joins_probed"] == 0 and out["grain_probed"] == 0
    assert src.queries == []
    manifest = (tmp_path / "relationships" / "manifest.tsv").read_text()
    assert "skipped-budget" in manifest


def test_probe_failure_is_a_manifest_row_not_a_crash(tmp_path):
    class _Boom(_FakeSource):
        def run_query(self, query, **kwargs):
            raise RuntimeError("engine down")

    out = write_relationship_evidence(
        _Boom(), tmp_path, tables_meta=F1_META,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    manifest = (tmp_path / "relationships" / "manifest.tsv").read_text()
    assert "error:" in manifest
    assert out["joins_probed"] == 0


def test_source_without_table_ref_is_a_noop(tmp_path):
    class _NoRef:
        metadata_profile = _Profile()

    out = write_relationship_evidence(
        _NoRef(), tmp_path, tables_meta=F1_META,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    assert out["files"] == []
    assert not (tmp_path / "relationships").exists()


# ---------------------------------------------------------------------------
# cache reuse (mirrors write_profiles' mode policy)
# ---------------------------------------------------------------------------


def _run(src, root, mode="full", changed=frozenset()):
    return write_relationship_evidence(
        src, root / ".metadata", tables_meta=F1_META,
        cache=read_cached_relationships(root),
        profile_mode=mode, changed_tables=changed, cfg=CFG,
    )


def test_incremental_reuses_unchanged_pairs_and_reprobes_changed(tmp_path):
    src = _FakeSource(unique_sides={"races", "drivers"})
    first = _run(src, tmp_path)  # fresh run persists sheets + manifest
    assert first["cached"] == 0
    queries_after_first = len(src.queries)

    # Incremental with `results` changed: pairs touching results re-probe;
    # drivers__standings (untouched) comes from the cache.
    second = _run(src, tmp_path, mode="incremental", changed={"results"})
    assert second["cached"] >= 1
    assert any(
        "drivers__standings" in f and second["cached"] for f in second["files"]
    )
    assert len(src.queries) > queries_after_first  # changed pairs did re-probe

    # A FULL run is the explicit re-read: nothing comes from the cache.
    third = _run(src, tmp_path, mode="full")
    assert third["cached"] == 0


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------


def test_snapshot_export_runs_the_evidence_pass_after_profiles():
    import inspect

    from harvest import metadata_export as me

    src = inspect.getsource(me.export_metadata)
    assert "read_cached_relationships(dataset_root)" in src
    assert src.index("write_profiles(") < src.index("write_relationship_evidence(")
    assert "has_relationships=" in src


def test_prompts_route_agents_to_the_sheets_first():
    from harvest import prompts

    for p in (
        prompts.SUPERVISOR_PROMPT,
        prompts.TABLE_AUTHOR_PROMPT,
        prompts.REVIEWER_PROMPT,
    ):
        norm = " ".join(p.split())
        assert ".metadata/relationships/" in norm
        assert "READ THESE FIRST" in norm
    # The table-author's join step leads with the sheets and reserves live
    # probes for what they don't cover.
    body = " ".join(prompts.TABLE_AUTHOR_PROMPT.split())
    assert "evidence sheets first, probes second" in body
    assert "A REFUTED sheet: do not document it" in body


def test_snapshot_summary_emits_a_relationship_feed_line():
    # The pass yields ONE live-feed status line (like "Column profiles: ...")
    # so an operator sees the evidence phase happen — and stays SILENT when
    # nothing ran (a "0 joins" line would read as a failure).
    from harvest.runner import _emit_profile_summary

    class _Emitter:
        def __init__(self):
            self.lines = []

        def emit_status(self, text):
            self.lines.append(text)

    em = _Emitter()
    _emit_profile_summary(
        em,
        {
            "profiles": {"profiled": 3},
            "relationships": {
                "joins_probed": 4, "grain_probed": 2, "cached": 1, "skipped": 5
            },
        },
    )
    assert em.lines == [
        "Column profiles: 3 profiled",
        "Relationship evidence: 4 join(s), 2 grain check(s), "
        "1 reused from cache, 5 skipped",
    ]

    # Ran-but-empty gets an explicit line (a dataset whose key names defeat
    # every nominator must not look like a broken pass — seen live on
    # california_schools); a pass that never ran stays silent.
    empty = _Emitter()
    _emit_profile_summary(
        empty, {"profiles": {"profiled": 3}, "relationships": {"ran": True}}
    )
    assert empty.lines[-1] == (
        "Relationship evidence: no join/grain candidates found "
        "(authors will probe relationships live)"
    )

    quiet = _Emitter()
    _emit_profile_summary(
        quiet, {"profiles": {"profiled": 3}, "relationships": {}}
    )
    assert quiet.lines == ["Column profiles: 3 profiled"]


def test_shared_enum_signature_yields_suspect_not_holds():
    # Two unrelated tables sharing a tiny code domain (status 1..8) show
    # ~100% containment both ways — the classic containment false positive.
    # The signature (M:N + tiny, heavily-repeated domains on BOTH sides)
    # must trump the numerically-good match rate.
    from harvest.relationships import _verdict

    def side(rows, distinct, rate=1.0):
        return {
            "rows": rows, "null_key_rows": 0, "distinct_keys": distinct,
            "matched_rows": int(rows * rate), "match_rate": rate,
        }

    suspect = _verdict(
        {"left": side(1000, 8), "right": side(500, 8), "cardinality": "M:N"}
    )
    assert "SUSPECT" in suspect and "SHARED CODE LIST" in suspect
    assert "enum/named-set" in suspect

    # A genuine M:N bridge (large key domains) keeps its HOLDS.
    real = _verdict(
        {"left": side(1000, 800), "right": side(900, 700),
         "cardinality": "M:N"}
    )
    assert real.startswith("HOLDS")

    # A 1:N star join with a small dimension is NOT the signature (the
    # dimension side is key-unique — cardinality isn't M:N).
    star = _verdict(
        {"left": side(40, 40), "right": side(1000, 38), "cardinality": "1:N"}
    )
    assert star.startswith("HOLDS")

    # REFUTED still wins below the floor even with the enum signature.
    refuted = _verdict(
        {"left": side(1000, 8, rate=0.05), "right": side(500, 8, rate=0.1),
         "cardinality": "M:N"}
    )
    assert refuted.startswith("REFUTED")


def test_unhinted_tables_are_assumed_large_and_skipped(tmp_path):
    # A table with NO byte-size hint (a view, an un-crawled external table)
    # must be treated as oversized — profile.py's assume-large posture — not
    # as size 0: the workgroup has no scan cutoff, so this gate is the only
    # thing standing between the probe loop and an unbounded full scan.
    meta = {
        "races": _meta({"raceid": "int", "name": "string"}),
        "results": _meta({"resultid": "int", "raceid": "int"}, totalSize=""),
    }
    src = _FakeSource(unique_sides={"races"})
    out = write_relationship_evidence(
        src, tmp_path, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    manifest = (tmp_path / "relationships" / "manifest.tsv").read_text()
    # The join pair touches the unhinted table -> skipped; so does its grain.
    assert "join\traces__results--raceid\t" in manifest
    assert manifest.count("skipped-size") >= 2
    assert not any("EXISTS" in q for q in src.queries)
    # races itself is hinted small: its grain probe still ran.
    assert (tmp_path / "relationships" / "grain" / "races.md").exists()
    assert out["grain_probed"] == 1


def test_grain_probe_respects_the_size_gate(tmp_path):
    meta = {"orders": _meta({"orderid": "int"}, totalSize=str(10 << 30))}
    src = _FakeSource()
    out = write_relationship_evidence(
        src, tmp_path, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    assert out["grain_probed"] == 0 and src.queries == []
    manifest = (tmp_path / "relationships" / "manifest.tsv").read_text()
    assert "grain\torders\t" in manifest and "skipped-size" in manifest


def test_normalized_names_pair_underscore_twins():
    # `driver_id` and `driverid` are the same key spelled by two authors —
    # normalization groups them, and the candidate carries each side's
    # ORIGINAL spelling (the probe SQL must use each table's real column).
    meta = {
        "drivers": _meta({"driverid": "int", "surname": "string"}),
        "laps": _meta({"lap_id": "int", "driver_id": "int"}),
    }
    cands, _ = enumerate_join_candidates(meta, CFG)
    twin = next(
        c for c in cands if {c["column_l"], c["column_r"]} == {"driver_id", "driverid"}
    )
    assert {twin["left"], twin["right"]} == {"drivers", "laps"}
    assert twin["via"] == "name"
    # The side mapping follows the sorted table order.
    assert (twin["left"], twin["column_l"]) == ("drivers", "driverid")
    assert (twin["right"], twin["column_r"]) == ("laps", "driver_id")


def test_role_named_keys_pair_with_the_home_tables_key():
    # The european-soccer shape: Match carries home_/away_-prefixed
    # references to Team's self-naming key. The shared-name primitive can
    # never see these (each role column exists in ONE table).
    meta = {
        "team": _meta({"id": "int", "team_api_id": "int", "name": "string"}),
        "match": _meta(
            {
                "id": "int",
                "home_team_api_id": "int",
                "away_team_api_id": "int",
                "season": "string",
            }
        ),
    }
    cands, _ = enumerate_join_candidates(meta, CFG)
    roles = sorted(
        (c["column_l"], c["column_r"]) for c in cands if c["via"] == "role"
    )
    assert roles == [
        ("away_team_api_id", "team_api_id"),
        ("home_team_api_id", "team_api_id"),
    ]
    # Distinct subjects: home/away siblings must not collide on one sheet.
    from harvest.relationships import _subject

    subjects = {_subject(c) for c in cands if c["via"] == "role"}
    assert len(subjects) == 2
    # And `season` never nominated anything.
    assert not any("season" in (c["column_l"], c["column_r"]) for c in cands)


def test_role_sheets_carry_both_spellings_and_the_role_note(tmp_path):
    meta = {
        "team": _meta({"team_api_id": "int"}),
        "match": _meta({"home_team_api_id": "int"}),
    }
    src = _FakeSource(unique_sides={"team"})
    write_relationship_evidence(
        src, tmp_path, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    sheet = (
        tmp_path / "relationships" / "joins"
        / "match__team--home_team_api_id--team_api_id.md"
    ).read_text()
    assert "`match.home_team_api_id` = `team.team_api_id`" in sheet
    assert "ROLE-named key" in sheet
    # The probe used each side's real spelling.
    assert any('"home_team_api_id"' in q and '"team_api_id"' in q
               for q in src.queries)


def test_grain_detects_infixed_self_keys():
    # `team_api_id` in `team`: the key prefix ("teamapi") EXTENDS the table
    # name — the two-direction prefix rule must still claim it.
    meta = {"team": _meta({"id": "int", "team_api_id": "int"})}
    grains = enumerate_grain_candidates(meta, CFG)
    assert [["team_api_id"], ["id"]] == grains["team"] or [
        ["team_api_id"]
    ] == grains["team"][:1]


def test_unhinted_table_is_measured_via_the_source_before_assuming_large(tmp_path):
    # The live "44 skipped" case: DDL-registered tables carry no totalSize
    # Parameter, and pure assume-large skipped every probe. When the source
    # can MEASURE the location (S3 listing — no query, no scan), a small
    # unhinted table probes normally; only an unmeasurable one stays skipped.
    meta = {
        "races": _meta({"raceid": "int", "name": "string"}, totalSize=""),
        "results": _meta({"resultid": "int", "raceid": "int"}, totalSize=""),
    }

    class _Measuring(_FakeSource):
        def __init__(self):
            super().__init__(unique_sides={"races"})
            self.measured = []

        def estimate_table_bytes(self, table, *, stop_at):
            self.measured.append(table)
            return 1 << 20  # small: under the gate

    src = _Measuring()
    out = write_relationship_evidence(
        src, tmp_path, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    assert out["joins_probed"] == 1 and out["grain_probed"] >= 1
    # Measured once per table (cached across the join AND grain gates).
    assert sorted(set(src.measured)) == ["races", "results"]
    assert len(src.measured) == len(set(src.measured))


def test_unmeasurable_unhinted_table_still_assumed_large(tmp_path):
    meta = {
        "races": _meta({"raceid": "int"}, totalSize=""),
        "results": _meta({"raceid": "int", "resultid": "int"}, totalSize=""),
    }

    class _CannotTell(_FakeSource):
        def estimate_table_bytes(self, table, *, stop_at):
            return None  # a view / listing denied

    out = write_relationship_evidence(
        _CannotTell(), tmp_path, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    assert out["joins_probed"] == 0 and out["grain_probed"] == 0
    manifest = (tmp_path / "relationships" / "manifest.tsv").read_text()
    assert "skipped-size" in manifest


# ---------------------------------------------------------------------------
# value-sketch nomination
# ---------------------------------------------------------------------------


def test_containment_estimator_is_exact_on_small_sets():
    from harvest.relationships import _containments

    a = list(range(1, 91))          # 90 distinct values
    b = list(range(1, 101))         # superset: 100 values
    c_ab, c_ba = _containments(sorted(a), sorted(b), k=256)
    # Both sketches shorter than k -> the sets are complete -> exact.
    assert c_ab == 1.0
    assert abs(c_ba - 0.9) < 1e-9


def test_containment_estimator_tracks_truth_on_kmv_sketches():
    from harvest.relationships import _containments

    span = 2**64
    step = span // 2000
    b_vals = [-(2**63) + i * step for i in range(2000)]  # 2000 uniform "hashes"
    a_vals = b_vals[::2]                                  # 1000 of them: A ⊆ B
    sk_a, sk_b = sorted(a_vals)[:256], sorted(b_vals)[:256]
    c_ab, c_ba = _containments(sk_a, sk_b, k=256)
    assert c_ab > 0.9          # A is fully contained in B
    assert 0.35 < c_ba < 0.65  # ~half of B is in A (±1/sqrt(k))


def test_parse_sketch_dedupes_multiset_arrays():
    from harvest.relationships import _parse_sketch

    # Belt-and-braces behind the SQL-side DISTINCT: KMV needs distinct
    # hashes, so repeated values must never occupy several sketch slots.
    assert _parse_sketch([7, 5, 5, 3, 3, 3]) == [3, 5, 7]
    assert _parse_sketch("[9, 9, 1]") == [1, 9]


def test_sketch_notes_lopsided_pairs_it_cannot_compare():
    from harvest.relationships import _sketch_nominate

    span = 2**64
    # Big side: 256 hashes packed near the bottom of the range -> the KMV
    # estimator reads ~10k distinct. Small side: 200 distinct (complete
    # sketch), all far above big's range so the merged bottom-k keeps no
    # small-side survivors — real containment would be invisible here.
    big = sorted(-(2**63) + i * (span // 10_000) for i in range(256))
    small = sorted(i * (span // 400) for i in range(1, 201))
    meta = {
        "events": _meta({"k": "bigint"}),
        "cohort": _meta({"k2": "bigint"}),
    }
    candidates, notes = [], []
    _sketch_nominate(
        {"events": {"k": big}, "cohort": {"k2": small}},
        meta, candidates, CFG, notes=notes,
    )
    assert candidates == []
    assert any("lopsided" in n for n in notes)


def test_sketchable_columns_rank_keys_first_and_cap():
    from harvest.relationships import _sketchable_columns

    cols = {
        "notes": "string", "cds": "string", "customer_id": "bigint",
        "score": "double", "city": "string",
    }
    ranked = _sketchable_columns(cols, cap=3)
    assert ranked[0] == "customer_id"    # key-like first
    assert ranked[1] == "cds"            # name hint ("cd") second
    assert len(ranked) == 3 and "score" not in ranked  # double: not a key family


class _SketchSource(_FakeSource):
    """Athena-style batched bottom-k expression + scripted sketch values."""

    def __init__(self, sketch_values, **kw):
        super().__init__(**kw)
        self.sketch_values = sketch_values  # {table: {column: [ints]}}
        self.sketch_queries = 0

    def sql_bottomk_sketch(self, col_sql, k):
        return f"BOTTOMK({col_sql},{k})"

    def run_query(self, query, **kwargs):
        if "BOTTOMK(" in query:
            import re

            self.sketch_queries += 1
            table = query.rsplit('"db"."', 1)[1].split('"')[0]
            cols = re.findall(r'BOTTOMK\("([^"]+)"', query)
            return [
                {
                    f"s{i}": self.sketch_values.get(table, {}).get(c, [])
                    for i, c in enumerate(cols)
                }
            ]
        return super().run_query(query, **kwargs)


def test_sketch_nominates_renamed_keys_end_to_end(tmp_path):
    # The california_schools shape: satscores.cds = schools.cdscode — no name
    # heuristic can see it; value containment can.
    meta = {
        "satscores": _meta({"cds": "string", "avgscr": "int"}),
        "schools": _meta({"cdscode": "string", "city": "string"}),
    }
    src = _SketchSource(
        {
            "satscores": {"cds": list(range(1, 91))},
            "schools": {"cdscode": list(range(1, 101))},
        },
        unique_sides={"schools"},
        match_rate=0.9,
    )
    out = write_relationship_evidence(
        src, tmp_path, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    assert out["joins_probed"] >= 1
    sheet = (
        tmp_path / "relationships" / "joins"
        / "satscores__schools--cds--cdscode.md"
    ).read_text()
    assert "VALUE-SKETCH containment" in sheet
    assert "`satscores.cds` = `schools.cdscode`" in sheet
    # The nomination graduated to the REAL probe (match rates in the sheet).
    assert "90.0%" in sheet


def test_sketch_suppresses_shared_enum_domains_and_dedups_name_pairs(tmp_path):
    meta = {
        "races": _meta({"raceid": "int", "status_cd": "string"}),
        "results": _meta({"raceid": "int", "state_cd": "string"}),
    }
    shared_codes = list(range(1, 9))  # 8 distinct: a code list, not a key
    src = _SketchSource(
        {
            "races": {"raceid": list(range(1, 200)), "status_cd": shared_codes},
            "results": {"raceid": list(range(1, 180)), "state_cd": shared_codes},
        },
        unique_sides={"races"},
    )
    write_relationship_evidence(
        src, tmp_path, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    manifest = (tmp_path / "relationships" / "manifest.tsv").read_text()
    # raceid↔raceid is the NAME nomination — exactly once, no sketch double.
    assert manifest.count("races__results--raceid\t") == 1
    # The tiny code domain was suppressed pre-probe in BOTH shapes: the
    # shared code list (status_cd↔state_cd) and the dense-integer trap
    # (state_cd 1..8 is numerically inside raceid 1..199 — no evidence).
    assert "status_cd" not in manifest and "state_cd" not in manifest


def test_sketch_uses_per_column_fallback_when_no_batch_aggregate(tmp_path):
    # Redshift-style: no n-smallest aggregate, one query per column.
    meta = {
        "satscores": _meta({"cds": "string"}),
        "schools": _meta({"cdscode": "string"}),
    }

    class _PerColumn(_FakeSource):
        def __init__(self):
            super().__init__(unique_sides={"schools"}, match_rate=0.9)
            self.values = {
                "cds": list(range(1, 91)),
                "cdscode": list(range(1, 101)),
            }

        def sql_sketch_column_query(self, ref_sql, col_sql, k):
            return f"COLSKETCH {col_sql} FROM {ref_sql}"

        def run_query(self, query, **kwargs):
            if query.startswith("COLSKETCH"):
                col = query.split('"')[1]
                return [{"h": v} for v in self.values.get(col, [])]
            return super().run_query(query, **kwargs)

    out = write_relationship_evidence(
        _PerColumn(), tmp_path, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    assert out["joins_probed"] >= 1
    assert (
        tmp_path / "relationships" / "joins"
        / "satscores__schools--cds--cdscode.md"
    ).exists()


def test_sketching_runs_on_full_calls_only_and_respects_the_flag(tmp_path):
    meta = {"satscores": _meta({"cds": "string"})}
    src = _SketchSource({"satscores": {"cds": [1, 2, 3]}})
    write_relationship_evidence(
        src, tmp_path, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"),
        profile_mode="incremental", cfg=CFG,
    )
    assert src.sketch_queries == 0  # non-full mode: cached sheets carry over

    from dataclasses import replace

    src2 = _SketchSource({"satscores": {"cds": [1, 2, 3]}})
    write_relationship_evidence(
        src2, tmp_path / "b", tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"),
        cfg=replace(CFG, sketch_enabled=False),
    )
    assert src2.sketch_queries == 0


# ---------------------------------------------------------------------------
# sampled-fact-side probes (the enterprise fact→dim shape)
# ---------------------------------------------------------------------------


class _SamplingSource(_FakeSource):
    def sql_sampled_ref(self, ref_sql, percent):
        return f"{ref_sql} SAMPLE({percent:g})"


def test_one_big_side_probes_sampled_instead_of_skipping(tmp_path):
    meta = {
        "big": _meta({"raceid": "int", "points": "double"},
                     totalSize=str(10 << 30)),
        "races": _meta({"raceid": "int", "name": "string"}),
    }
    src = _SamplingSource(unique_sides={"races"}, match_rate=0.96)
    out = write_relationship_evidence(
        src, tmp_path, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    assert out["joins_probed"] == 1 and out["skipped"] == 0

    manifest = (tmp_path / "relationships" / "manifest.tsv").read_text()
    assert "ok-sampled" in manifest and "skipped-size" not in manifest

    sheet = (tmp_path / "relationships" / "joins" / "big__races--raceid.md").read_text()
    # 256 MiB target over a 10 GiB table = a 2.5% sample.
    assert "INDICATIVE" in sheet and "~2.5% sample" in sheet
    assert "HOLDS (sampled)" in sheet and "96.0%" in sheet
    assert "`races` key is unique" in sheet
    # One direction only: the sheet says so, and orphans come from the
    # SAMPLED side only.
    assert "Only ONE direction is measured" in sheet
    assert "Orphan sample — sampled `big` rows" in sheet
    # The probe actually queried the sampled reference.
    assert any("SAMPLE(2.5)" in q for q in src.queries)


def test_both_sides_big_or_unmeasurable_still_skip(tmp_path):
    both_big = {
        "a_facts": _meta({"raceid": "int"}, totalSize=str(10 << 30)),
        "races": _meta({"raceid": "int", "name": "string"},
                       totalSize=str(10 << 30)),
    }
    src = _SamplingSource()
    out = write_relationship_evidence(
        src, tmp_path / "bb", tables_meta=both_big,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    manifest = (tmp_path / "bb" / "relationships" / "manifest.tsv").read_text()
    assert "skipped-size" in manifest and "ok-sampled" not in manifest

    # An UNMEASURABLE big side can't size its sample percent -> skip.
    unmeasurable = {
        "big": _meta({"raceid": "int"}, totalSize=""),
        "races": _meta({"raceid": "int", "name": "string"}),
    }
    out = write_relationship_evidence(
        _SamplingSource(), tmp_path / "um", tables_meta=unmeasurable,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    manifest = (tmp_path / "um" / "relationships" / "manifest.tsv").read_text()
    assert "skipped-size" in manifest


def test_sampled_sheets_are_cache_reusable(tmp_path):
    meta = {
        "big": _meta({"raceid": "int"}, totalSize=str(10 << 30)),
        "races": _meta({"raceid": "int", "name": "string"}),
    }
    src = _SamplingSource(unique_sides={"races"})
    _run_with = lambda mode: write_relationship_evidence(  # noqa: E731
        src, tmp_path / ".metadata", tables_meta=meta,
        cache=read_cached_relationships(tmp_path),
        profile_mode=mode, cfg=CFG,
    )
    first = _run_with("full")
    assert first["joins_probed"] == 1
    second = _run_with("cross")  # fingerprints unchanged -> reuse
    assert second["cached"] >= 1 and second["joins_probed"] == 0


def test_sampled_join_stats_shape_and_no_uniqueness_leak():
    from harvest.probes import sampled_join_stats

    class _Src:
        def __init__(self):
            self.queries = []

        def run_query(self, query, **kwargs):
            self.queries.append(query)
            if "SAMPLE" in query:
                return [{"n": 1000, "null_keys": 10,
                         "distinct_keys": 500, "m": 950}]
            return [{"n": 200, "null_keys": 0, "distinct_keys": 200}]

    src = _Src()
    stats = sampled_join_stats(
        src, '("db"."big" SAMPLE(2.5))', ["raceid"], '"db"."races"', ["raceid"]
    )
    assert stats["sampled"]["match_rate"] == round(950 / 990, 4)
    assert "_unique" not in stats["sampled"]  # unknowable from a sample
    assert stats["full_unique"] is True
    assert stats["full"]["rows"] == 200
    # Every sampled-side number must come from ONE scan: each reference to a
    # TABLESAMPLE relation draws an INDEPENDENT sample, so splitting the
    # matched count into its own query would divide numbers from different
    # samples (match rates over 100% on a perfect join).
    sampled_queries = [q for q in src.queries if "SAMPLE" in q]
    assert len(sampled_queries) == 1
    assert "LEFT JOIN" in sampled_queries[0]


# ---------------------------------------------------------------------------
# review-fix regressions (xhigh sweep)
# ---------------------------------------------------------------------------


def test_sketch_candidates_survive_incremental_runs(tmp_path):
    # Reproduced live by the review: a full run discovers cds=cdscode by
    # sketch; the next incremental run cannot re-sketch, so without the
    # persisted candidates the sheet silently vanished with the wipe.
    meta = {
        "satscores": _meta({"cds": "string", "avgscr": "int"}),
        "schools": _meta({"cdscode": "string", "city": "string"}),
    }
    root = tmp_path / ".metadata"
    src = _SketchSource(
        {
            "satscores": {"cds": list(range(1, 91))},
            "schools": {"cdscode": list(range(1, 101))},
        },
        unique_sides={"schools"},
        match_rate=0.9,
    )
    first = write_relationship_evidence(
        src, root, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    assert first["joins_probed"] >= 1
    assert (root / "relationships" / "candidates.json").is_file()

    cache = read_cached_relationships(tmp_path)  # read BEFORE the wipe
    assert cache.sketch_candidates

    src2 = _SketchSource({}, unique_sides={"schools"}, match_rate=0.9)
    second = write_relationship_evidence(
        src2, tmp_path / "fresh", tables_meta=meta, cache=cache,
        profile_mode="incremental", cfg=CFG,
    )
    assert src2.sketch_queries == 0  # incremental never sketches...
    assert second["cached"] >= 1     # ...but the sheet is reused, not lost
    assert (
        tmp_path / "fresh" / "relationships" / "joins"
        / "satscores__schools--cds--cdscode.md"
    ).is_file()
    # And the chain holds: the incremental run re-persists the candidates.
    assert (tmp_path / "fresh" / "relationships" / "candidates.json").is_file()


def test_grain_probe_failure_is_an_error_row_and_not_reused(tmp_path):
    class _GrainBoom(_FakeSource):
        def run_query(self, query, **kwargs):
            if "key_groups" in query:
                raise RuntimeError("throttled\nRate exceeded")
            return super().run_query(query, **kwargs)

    root = tmp_path / ".metadata"
    write_relationship_evidence(
        _GrainBoom(unique_sides={"races"}, match_rate=0.96), root,
        tables_meta=F1_META,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    manifest = (root / "relationships" / "manifest.tsv").read_text()
    grain_rows = [
        ln for ln in manifest.splitlines() if ln.startswith("grain\t")
    ]
    assert grain_rows and all("error:" in ln for ln in grain_rows)
    # Multi-line engine errors are flattened — one row stays one row.
    assert all("\n" not in ln for ln in grain_rows)
    # A transient failure must NOT be served as cached evidence to every
    # later incremental/cross run ('ok' rows are cache-eligible).
    cache = read_cached_relationships(tmp_path)
    assert all(kind != "grain" for kind, _ in cache.sheets)


def test_footers_match_their_sheet_kind(tmp_path):
    # Sampled sheets must not claim 'full-scan aggregates … do not re-probe'
    # (their own INDICATIVE banner says estimates), and TYPE MISMATCH sheets
    # must ASK for the live cast-join instead of forbidding re-probing.
    meta = {
        "big": _meta({"raceid": "int"}, totalSize=str(10 << 30)),
        "races": _meta({"raceid": "int", "name": "string"}),
    }
    src = _SamplingSource(unique_sides={"races"}, match_rate=0.96)
    write_relationship_evidence(
        src, tmp_path, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    sampled = (
        tmp_path / "relationships" / "joins" / "big__races--raceid.md"
    ).read_text()
    assert "full-scan aggregates" not in sampled
    assert "estimates" in sampled

    mm = {
        "orders": _meta({"customer_id": "bigint"}),
        "customers": _meta({"customer_id": "string"}),
    }
    write_relationship_evidence(
        _FakeSource(), tmp_path / "mm", tables_meta=mm,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    sheet = (
        tmp_path / "mm" / "relationships" / "joins"
        / "customers__orders--customer_id.md"
    ).read_text()
    assert "TYPE MISMATCH" in sheet
    assert "do not re-probe" not in sheet
    assert "verify the cast-join live" in sheet


def test_subject_disambiguates_dbt_style_names():
    from harvest.relationships import _subject

    a = _subject({"left": "stg__orders", "right": "customers",
                  "column_l": "k", "column_r": "k"})
    b = _subject({"left": "stg", "right": "orders__customers",
                  "column_l": "k", "column_r": "k"})
    assert a != b  # ('a__b','c') and ('a','b__c') each get their own file
    plain = _subject({"left": "races", "right": "results",
                      "column_l": "raceid", "column_r": "raceid"})
    assert plain == "races__results--raceid"  # historical form untouched


def test_budget_exhaustion_labels_skips_and_stops_sizing(tmp_path):
    from dataclasses import replace

    class _CountingSize(_FakeSource):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.size_calls = 0

        def estimate_table_bytes(self, table, *, stop_at):
            self.size_calls += 1
            return 1024

    meta = {  # NO size hints: sizing would need the S3 estimator
        "races": _meta({"raceid": "int"}, totalSize=""),
        "results": _meta({"raceid": "int"}, totalSize=""),
    }
    src = _CountingSize()
    out = write_relationship_evidence(
        src, tmp_path, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"),
        cfg=replace(CFG, budget_s=0),
    )
    manifest = (tmp_path / "relationships" / "manifest.tsv").read_text()
    # Post-budget pairs are BUDGET skips, and a dead budget must not keep
    # billing S3 LIST calls just to write skip rows.
    assert "skipped-budget" in manifest and "skipped-size" not in manifest
    assert src.size_calls == 0
    assert out["skipped"] >= 1


def test_sampled_percent_uses_the_complete_listing_not_the_gate_bound(tmp_path):
    class _TwoTier(_SamplingSource):
        def __init__(self, exact, **kw):
            super().__init__(**kw)
            self.exact = exact

        def estimate_table_bytes(self, table, *, stop_at):
            if stop_at is None:
                return self.exact    # complete listing (percent contract)
            return (2 << 30) + 1     # gate probe: early-exited lower bound

    meta = {
        "big": _meta({"raceid": "int"}, totalSize=""),
        "races": _meta({"raceid": "int", "name": "string"}),
    }
    src = _TwoTier(100 << 30, unique_sides={"races"}, match_rate=0.96)
    write_relationship_evidence(
        src, tmp_path, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    # 256 MiB target over the TRUE 100 GiB = 0.25% — not ~12% off the ~2 GiB
    # gate bound, which is a lower bound and would over-sample 50x.
    assert any("SAMPLE(0.25)" in q for q in src.queries)

    # When even the complete listing can't tell, skip rather than guess.
    src2 = _TwoTier(None, unique_sides={"races"})
    write_relationship_evidence(
        src2, tmp_path / "b", tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    manifest = (tmp_path / "b" / "relationships" / "manifest.tsv").read_text()
    assert "skipped-size" in manifest and "ok-sampled" not in manifest


def test_schema_qualified_tables_match_name_heuristics():
    # Redshift table tokens are 'schema.table' — every name heuristic must
    # match against the BASE name (no column is called public_orders_id).
    meta = {
        "public.orders": _meta({"orderid": "int", "total": "double"}),
        "public.lines": _meta({"orderid": "int", "qty": "int"}),
    }
    cands, _ = enumerate_join_candidates(meta, CFG)
    pairs = {(c["left"], c["right"]) for c in cands}
    assert ("public.lines", "public.orders") in pairs
    grain = enumerate_grain_candidates(meta, CFG)
    assert grain.get("public.orders") == [["orderid"]]


def test_type_fam_maps_redshift_character_varying():
    from harvest.relationships import _sketchable_columns, _type_fam

    assert _type_fam("character varying(256)") == "text"
    # Without the mapping, no Redshift varchar was ever sketched — the
    # renamed-text-key case (cds/cdscode) was structurally unreachable there.
    assert _sketchable_columns({"cds": "character varying(14)"}, cap=5) == ["cds"]


def _join_statuses(root):
    manifest = (root / "relationships" / "manifest.tsv").read_text()
    join_rows = [ln for ln in manifest.splitlines() if ln.startswith("join\t")]
    return {ln.split("\t")[1]: ln.split("\t")[3] for ln in join_rows}


def test_pair_cap_is_a_probe_budget_free_skips_dont_consume(tmp_path):
    from dataclasses import replace

    # Two candidates, probe budget of one. The FIRST-enumerated pair
    # (alphaid sorts before raceid) is both-big — it yields a skipped-size
    # row, which is FREE and must not consume the budget: the probeable
    # raceid pair still gets probed.
    meta = {
        "alphas_x": _meta({"alpha_id": "int"}, totalSize=str(10 << 30)),
        "alphas_y": _meta({"alpha_id": "int"}, totalSize=str(10 << 30)),
        "races": _meta({"raceid": "int", "name": "string"}),
        "results": _meta({"raceid": "int", "points": "int"}),
    }
    src = _FakeSource(unique_sides={"races"}, match_rate=0.96)
    out = write_relationship_evidence(
        src, tmp_path, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"),
        cfg=replace(CFG, max_pairs=1),
    )
    assert out["joins_probed"] == 1
    by_subject = _join_statuses(tmp_path)
    assert by_subject["races__results--raceid"] == "ok"
    assert by_subject["alphas_x__alphas_y--alpha_id"] == "skipped-size"


def test_cached_reuse_does_not_consume_probe_slots(tmp_path):
    from dataclasses import replace

    # Run 1 (full) probes both pairs. Run 2 (incremental, ONE probe slot,
    # one changed table): the unchanged pair's cached sheet is free, so the
    # single slot goes to re-probing the changed pair — nothing skipped-cap.
    meta = {
        "races": _meta({"raceid": "int", "name": "string"}),
        "results": _meta({"raceid": "int", "points": "int"}),
        "drivers": _meta({"driverid": "int", "surname": "string"}),
        "standings": _meta({"driverid": "int", "points": "int"}),
    }
    src = _FakeSource(unique_sides={"races", "drivers"}, match_rate=0.96)
    first = write_relationship_evidence(
        src, tmp_path / ".metadata", tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    assert first["joins_probed"] == 2
    second = write_relationship_evidence(
        src, tmp_path / "fresh", tables_meta=meta,
        cache=read_cached_relationships(tmp_path),
        profile_mode="incremental", changed_tables={"results"},
        cfg=replace(CFG, max_pairs=1),
    )
    assert second["joins_probed"] == 1
    statuses = _join_statuses(tmp_path / "fresh")
    assert statuses["races__results--raceid"] == "ok"  # changed -> re-probed
    assert statuses["drivers__standings--driverid"] == "cached"  # free
    assert "skipped-cap" not in statuses.values()


def test_probe_budget_is_spread_across_tables(tmp_path):
    from dataclasses import replace

    # Four candidates over two hubs (drivers, races), budget of two.
    # First-come-first-served would give BOTH slots to driverid pairs
    # (alphabetical); the spread gives every hub its first pair instead.
    meta = {
        "races": _meta({"raceid": "int", "name": "string"}),
        "results": _meta({"resultid": "int", "raceid": "int", "driverid": "int"}),
        "drivers": _meta({"driverid": "int", "surname": "string"}),
        "standings": _meta({"driverid": "int", "raceid": "int"}),
    }
    src = _FakeSource(unique_sides={"races", "drivers"}, match_rate=0.96)
    out = write_relationship_evidence(
        src, tmp_path, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"),
        cfg=replace(CFG, max_pairs=2),
    )
    assert out["joins_probed"] == 2
    by_subject = _join_statuses(tmp_path)
    probed = {s for s, status in by_subject.items() if status == "ok"}
    probed_tables = {t for s in probed for t in s.split("--")[0].split("__")}
    # Every hub earned its first sheet before any hub got its second.
    assert "drivers" in probed_tables and "races" in probed_tables
    assert sum(1 for st in by_subject.values() if st == "skipped-cap") == 2


def test_skipped_cap_pairs_are_never_sized(tmp_path):
    from dataclasses import replace

    class _CountingSize(_FakeSource):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.size_calls = 0

        def estimate_table_bytes(self, table, *, stop_at):
            self.size_calls += 1
            return 1 << 20  # small: probeable

    # Three disjoint pairs, all hint-less -> sizing needs S3 LISTs. Key
    # names deliberately do NOT self-name their tables: a self-naming key
    # would add grain candidates, whose loop sizes tables on its own
    # (legitimately — grain has its own gate, outside the join cap).
    meta = {
        "pa": _meta({"k1_id": "int"}, totalSize=""),
        "pb": _meta({"k1_id": "int"}, totalSize=""),
        "qa": _meta({"k2_id": "int"}, totalSize=""),
        "qb": _meta({"k2_id": "int"}, totalSize=""),
        "ra": _meta({"k3_id": "int"}, totalSize=""),
        "rb": _meta({"k3_id": "int"}, totalSize=""),
    }
    src = _CountingSize(match_rate=0.96)
    out = write_relationship_evidence(
        src, tmp_path, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"),
        cfg=replace(CFG, max_pairs=1),
    )
    # One probed pair = two tables sized; the two skipped-cap pairs must
    # not keep billing LIST calls just to write their skip rows.
    assert out["joins_probed"] == 1
    assert src.size_calls == 2
    assert sum(
        1 for st in _join_statuses(tmp_path).values() if st == "skipped-cap"
    ) == 2


def test_spread_prefers_sketch_on_coverage_ties():
    from harvest.relationships import _spread_candidates

    name_pair = {"left": "a", "right": "b", "via": "name"}
    sketch_pair = {"left": "c", "right": "d", "via": "sketch"}
    ordered = _spread_candidates([name_pair, sketch_pair])
    # A skipped name pair is one columns.tsv grep away for an author; a
    # skipped renamed-key (sketch) pair is unrecoverable live.
    assert ordered[0] is sketch_pair


def test_progress_ticks_cover_sketching_and_joins(tmp_path):
    ticks = []
    meta = {
        "satscores": _meta({"cds": "string", "avgscr": "int"}),
        "schools": _meta({"cdscode": "string", "city": "string"}),
    }
    src = _SketchSource(
        {
            "satscores": {"cds": list(range(1, 91))},
            "schools": {"cdscode": list(range(1, 101))},
        },
        unique_sides={"schools"},
        match_rate=0.9,
    )
    write_relationship_evidence(
        src, tmp_path, tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
        progress=lambda ph, d, t, label: ticks.append((ph, d, t, label)),
    )
    assert ticks and all(t[0] == "relationships" for t in ticks)
    labels = " | ".join(t[3] for t in ticks)
    assert "sketching" in labels and "join" in labels
    # Each stage completes its bar (a final done == total tick).
    assert any(d == t and d > 0 for _, d, t, _ in ticks)
    # A broken callback never breaks the pass.
    out = write_relationship_evidence(
        src, tmp_path / "b", tables_meta=meta,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
        progress=lambda *a: 1 / 0,
    )
    assert out["joins_probed"] >= 1


# ---------------------------------------------------------------------------
# sketch flood guards (the MusicBrainz lessons: 3,505 nominations, 59 probed)
# ---------------------------------------------------------------------------


def test_sketch_suppresses_pairs_whose_contained_side_is_a_self_pk():
    from harvest.relationships import _sketch_nominate

    meta = {
        "artist": _meta({"id": "int"}),
        "release": _meta({"id": "int"}),
        "credit": _meta({"artist": "int"}),
    }
    sketches = {
        "artist": {"id": list(range(1, 201))},   # dense PK 1..200
        "release": {"id": list(range(1, 101))},  # dense PK 1..100
        "credit": {"artist": list(range(1, 91))},  # FK values ⊆ artist.id
    }
    cands, notes = [], []
    _sketch_nominate(sketches, meta, cands, CFG, notes=notes)
    cols = {(c["column_l"], c["column_r"]) for c in cands}
    # release.id ⊆ artist.id is the dense-id coincidence — a PK contained
    # elsewhere is not FK evidence. Suppressed (and counted in the notes).
    assert ("id", "id") not in cols
    # credit.artist ⊆ artist.id is the real FK shape — kept.
    assert ("id", "artist") in cols or ("artist", "id") in cols
    assert any("contained side" in n and "PK" in n for n in notes)


def test_sketch_same_named_pairs_collapse_toward_the_home_table():
    from harvest.relationships import _sketch_nominate

    meta = {
        "tag": _meta({"id": "int", "name": "string"}),
        "area_tag": _meta({"tag": "int"}),
        "artist_tag": _meta({"tag": "int"}),
    }
    sketches = {
        "tag": {"id": list(range(1, 201))},
        "area_tag": {"tag": list(range(1, 121))},
        "artist_tag": {"tag": list(range(1, 111))},
    }
    cands, notes = [], []
    _sketch_nominate(sketches, meta, cands, CFG, notes=notes)
    pairs = {(c["left"], c["right"], c["column_l"], c["column_r"]) for c in cands}
    # holder↔holder (tag↔tag) is noise once a home exists: every *_tag table
    # contains tag.id's values — the edges that matter all point at `tag`.
    assert ("area_tag", "artist_tag", "tag", "tag") not in pairs
    # each holder→home edge survives
    assert ("area_tag", "tag", "tag", "id") in pairs
    assert ("artist_tag", "tag", "tag", "id") in pairs


def test_sketch_refuses_widely_shared_same_named_columns_with_no_home():
    from harvest.relationships import _sketch_nominate

    # entity0-style polymorphic link columns: same name in MANY tables, no
    # table named for it — pairwise nomination would explode (l_* tables).
    meta = {f"l_{i}": _meta({"entity0": "int"}) for i in range(8)}
    sketches = {
        f"l_{i}": {"entity0": list(range(1, 150))} for i in range(8)
    }
    cands, notes = [], []
    _sketch_nominate(sketches, meta, cands, CFG, notes=notes)
    assert cands == []
    assert any("same-named holder pair" in n for n in notes)


def test_sketch_never_pairs_columns_within_one_table():
    from harvest.relationships import _sketch_nominate

    # Two columns of ONE table with identical values: intra-table pairs are
    # structurally impossible (the pair loop is strictly cross-table).
    meta = {"t": _meta({"a": "int", "b": "int"})}
    sketches = {"t": {"a": list(range(1, 100)), "b": list(range(1, 100))}}
    cands = []
    _sketch_nominate(sketches, meta, cands, CFG, notes=[])
    assert cands == []


def test_sketch_scan_is_bounded_to_half_the_budget(tmp_path):
    from dataclasses import replace

    # Clock advances 30s per engine query; budget 100s → the sketch scan must
    # stop at ~50s (2 tables in), leaving the other half for probes.
    import harvest.relationships as R

    clock = {"t": 1000.0}

    class _SlowSketch(_SketchSource):
        def run_query(self, query, **kwargs):
            clock["t"] += 30.0
            return super().run_query(query, **kwargs)

    meta = {
        "races": _meta({"raceid": "int", "name": "string"}),
        "results": _meta({"raceid": "int", "points": "int"}),
        "satscores": _meta({"cds": "string"}),
        "schools": _meta({"cdscode": "string"}),
    }
    src = _SlowSketch(
        {
            "satscores": {"cds": list(range(1, 91))},
            "schools": {"cdscode": list(range(1, 101))},
        },
        unique_sides={"races"},
        match_rate=0.96,
    )
    real_monotonic = time.monotonic
    R.time.monotonic = lambda: clock["t"]
    try:
        out = write_relationship_evidence(
            src, tmp_path, tables_meta=meta,
            cache=read_cached_relationships(tmp_path / "nowhere"),
            cfg=replace(CFG, budget_s=100),
        )
    finally:
        R.time.monotonic = real_monotonic
    # Timeline: grain runs first (races.raceid, 30s -> t=1030), then the
    # sketch scan hits its HALF-budget cutoff (t=1050) after one table —
    # it must NOT keep scanning up to the full deadline (t=1100)...
    assert src.sketch_queries == 1
    # ...so probing still had budget: the raceid name pair got its sheet.
    assert out["joins_probed"] >= 1


def test_grain_runs_before_joins(tmp_path):
    src = _FakeSource(unique_sides={"races", "drivers"}, match_rate=0.96)
    write_relationship_evidence(
        src, tmp_path, tables_meta=F1_META,
        cache=read_cached_relationships(tmp_path / "nowhere"), cfg=CFG,
    )
    manifest = (tmp_path / "relationships" / "manifest.tsv").read_text()
    kinds = [ln.split("\t")[0] for ln in manifest.splitlines()[1:]]
    # Grain first: one cheap aggregate per table, high author value — a
    # candidate-flooded join loop must not starve it to zero sheets again.
    assert kinds[0] == "grain"
    assert kinds.index("join") > kinds.count("grain") - 1


def test_sketch_requires_a_name_link_against_dense_int_pks():
    from harvest.relationships import _sketch_nominate

    meta = {
        "area": _meta({"id": "int"}),
        "l_area_area": _meta({"link_order": "int", "begin_area": "int"}),
    }
    sketches = {
        "area": {"id": list(range(1, 201))},
        "l_area_area": {
            "link_order": list(range(1, 100)),
            "begin_area": list(range(1, 90)),
        },
    }
    cands, notes = [], []
    _sketch_nominate(sketches, meta, cands, CFG, notes=notes)
    cols = {(c["column_l"], c["column_r"]) for c in cands}
    # link_order ⊆ area.id is numeric coincidence (a 1..N surrogate contains
    # every smaller int column) — no name link, suppressed.
    assert ("id", "link_order") not in cols
    assert ("link_order", "id") not in cols
    # begin_area ⊆ area.id carries the role-suffix name link — kept.
    assert ("id", "begin_area") in cols or ("begin_area", "id") in cols
    assert any("name link" in n for n in notes)


def test_sketch_keeps_renamed_keys_against_sparse_string_pks():
    from harvest.relationships import _sketch_nominate

    # A string/UUID `id` is a SPARSE domain — containment against it IS
    # strong evidence (Data Vault hash keys), so no name witness is demanded
    # even though the contained column names nothing.
    meta = {
        "customer": _meta({"id": "string"}),
        "orders": _meta({"buyer_ref": "string"}),
    }
    sketches = {
        "customer": {"id": list(range(1, 201))},
        "orders": {"buyer_ref": list(range(1, 100))},
    }
    cands = []
    _sketch_nominate(sketches, meta, cands, CFG, notes=[])
    assert any(
        {c["column_l"], c["column_r"]} == {"id", "buyer_ref"} for c in cands
    )


def test_sk_suffix_is_first_class_key_vocabulary():
    from harvest.relationships import (
        _is_key_like,
        _is_self_key,
        _sketchable_columns,
    )

    # Kimball surrogate keys: fact.customer_sk = dim.customer_sk.
    assert _is_key_like("customer_sk", ["dim_customer"])
    assert _is_self_key("customer_sk", "customer")
    assert _sketchable_columns(
        {"customer_sk": "bigint", "notes": "string"}, cap=1
    ) == ["customer_sk"]
    meta = {
        "fact_orders": _meta({"customer_sk": "bigint", "amount": "double"}),
        "dim_customer": _meta({"customer_sk": "bigint", "name": "string"}),
    }
    cands, _ = enumerate_join_candidates(meta, CFG)
    assert any(c["column_l"] == "customer_sk" for c in cands)
