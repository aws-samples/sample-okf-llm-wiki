"""Cross-dataset references mode (Roadmap §5, OSS flat-trust): the pieces.

Offline like every other harvest test: agent building, AWS, and DynamoDB are
patched/faked. Covers the guard's pair-subtree confinement, the target
metadata snapshot, the widened session policy, the cross prompts/wiring,
entrypoint validation, and the run_cross_harvest lifecycle — including that
the pair docs are ONE-SIDED: nothing is ever written into the target bundle
(discoverability rides the reindex-derived XREF signal instead).
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

import harvest.okf_guard as okf_guard
import harvest.runner as runner
from harvest.clients import _session_policy
from harvest.fsutil import remove_tree
from harvest.glue_source import GlueAthenaSource
from harvest.metadata_export import METADATA_DIR, export_metadata, export_target_metadata
from harvest.okf_guard import OKFGuardMiddleware
from tests.fakes import f1_like_glue


# --------------------------------------------------------------------------- #
# Guard: writable_prefix confines every write/edit to the pair subtree
# --------------------------------------------------------------------------- #


class _AllowEngine:
    def guard_write_file(self, content, existing):
        return types.SimpleNamespace(allow=True, new_content=None, message=None)

    def guard_edit_file(self, old, new, existing):
        return types.SimpleNamespace(allow=True, new_content=None, message=None)


def _tm_standin(monkeypatch):
    monkeypatch.setattr(
        okf_guard,
        "ToolMessage",
        lambda content, tool_call_id, status="success": {
            "content": content,
            "id": tool_call_id,
            "status": status,
        },
    )


def _cross_mw():
    return OKFGuardMiddleware(
        _AllowEngine(),
        read_current=lambda _p: None,
        writable_prefix="external/crm/customers/",
    )


def _request(name="write_file", **args):
    args.setdefault("file_path", "tables/races.md")
    return types.SimpleNamespace(tool_call={"name": name, "args": args, "id": "c-1"})


def test_cross_guard_allows_writes_inside_pair_subtree():
    mw = _cross_mw()
    for path in (
        "external/crm/customers/overview.md",
        "/external/crm/customers/joins/orders__customers.md",
        "external/crm/customers/metrics/clv.md",
    ):
        req = _request(file_path=path, content="x")
        assert mw.wrap_tool_call(req, lambda r: "WROTE") == "WROTE", path


def test_cross_guard_refuses_writes_outside_pair_subtree(monkeypatch):
    _tm_standin(monkeypatch)
    mw = _cross_mw()
    called = {"n": 0}

    def handler(r):
        called["n"] += 1
        return "SHOULD NOT RUN"

    for path in (
        "tables/races.md",
        "references/joins/a__b.md",
        "datasets/orders.md",
        "external/other/pair/overview.md",  # a DIFFERENT pair's subtree
        "notes.txt",  # non-markdown too: nothing outside the subtree
        "external/crm/customers/../../../tables/races.md",  # normalized escape
    ):
        req = _request(file_path=path, content="x")
        result = mw.wrap_tool_call(req, handler)
        assert isinstance(result, dict), f"{path} should be refused"
        assert result["status"] == "error"
        assert "writable subtree" in result["content"]
    assert called["n"] == 0


def test_cross_guard_refuses_edit_outside_subtree(monkeypatch):
    _tm_standin(monkeypatch)
    mw = _cross_mw()
    req = _request(name="edit_file", old_string="a", new_string="b")
    result = mw.wrap_tool_call(req, lambda r: "SHOULD NOT RUN")
    assert isinstance(result, dict) and result["status"] == "error"


def test_cross_guard_metadata_still_read_only(monkeypatch):
    # .metadata/ refusal fires first — even for paths under .metadata/external/.
    _tm_standin(monkeypatch)
    mw = _cross_mw()
    req = _request(file_path=".metadata/external/crm/customers/columns.tsv", content="x")
    result = mw.wrap_tool_call(req, lambda r: "SHOULD NOT RUN")
    assert isinstance(result, dict)
    assert "read-only" in result["content"]


def test_default_guard_has_no_prefix_confinement():
    # Every non-cross mode: writable_prefix is None and writes land anywhere.
    mw = OKFGuardMiddleware(_AllowEngine(), read_current=lambda _p: None)
    req = _request(file_path="tables/races.md", content="x")
    assert mw.wrap_tool_call(req, lambda r: "WROTE") == "WROTE"


# --------------------------------------------------------------------------- #
# fsutil: remove_tree + copy_markdown_tree (the Y-side mirror)
# --------------------------------------------------------------------------- #


def test_remove_tree_removes_dir_and_reports_missing(tmp_path):
    d = tmp_path / "external" / "crm" / "customers"
    (d / "joins").mkdir(parents=True)
    (d / "joins" / "a.md").write_text("x", encoding="utf-8")
    assert remove_tree(d) is True
    assert not d.exists()
    assert remove_tree(d) is False


# --------------------------------------------------------------------------- #
# metadata_export: the target snapshot under .metadata/external/<d>/<ds>/
# --------------------------------------------------------------------------- #


def _source(database="na_mi_formula_1_curated"):
    return GlueAthenaSource(
        database=database,
        glue=f1_like_glue(),
        athena=None,
        region="us-east-1",
        account_id="123456789012",
    )


def test_export_target_metadata_layout_and_docs(tmp_path):
    # The target's published bundle: two docs plus generated/reserved files.
    target_root = tmp_path / "crm" / "customers"
    (target_root / "tables").mkdir(parents=True)
    (target_root / ".harvest").mkdir()
    (target_root / "tables" / "races.md").write_text("doc", encoding="utf-8")
    (target_root / "index.md").write_text("generated", encoding="utf-8")
    (target_root / ".harvest" / "state.json").write_text("{}", encoding="utf-8")
    # The target's OWN cross docs (about a third dataset) must NOT be copied —
    # they are another run's conclusions, not the target's own verified facts.
    (target_root / "external" / "ops" / "logs").mkdir(parents=True)
    (target_root / "external" / "ops" / "logs" / "overview.md").write_text(
        "third-party pair doc", encoding="utf-8"
    )
    # An individually unreadable doc (invalid UTF-8) is tolerated, not fatal.
    (target_root / "tables" / "binaryish.md").write_bytes(b"\xff\xfe broken")

    dataset_root = tmp_path / "sales" / "orders"
    dataset_root.mkdir(parents=True)
    export_metadata(_source(), dataset_root)  # wipes .metadata/ fresh first
    summary = export_target_metadata(
        _source(),
        dataset_root,
        target_data_domain="crm",
        target_dataset="customers",
        target_bundle_root=target_root,
    )

    ext = dataset_root / METADATA_DIR / "external" / "crm" / "customers"
    assert (ext / "index.md").is_file()
    assert (ext / "database.md").is_file()
    assert (ext / "columns.tsv").is_file()
    assert (ext / "tables" / "races.md").is_file()
    # Published docs copied verbatim; reserved/dot files + external/ excluded.
    assert (ext / "docs" / "tables" / "races.md").read_text(encoding="utf-8") == "doc"
    assert not (ext / "docs" / "index.md").exists()
    assert not (ext / "docs" / ".harvest").exists()
    assert not (ext / "docs" / "external").exists()
    # The undecodable doc arrived with replacement chars rather than failing.
    assert (ext / "docs" / "tables" / "binaryish.md").is_file()
    assert summary["docs_copied"] == 2
    # And the run's OWN snapshot is intact alongside it.
    assert (dataset_root / METADATA_DIR / "columns.tsv").is_file()


def test_export_target_manifest_teaches_cross_grep(tmp_path):
    dataset_root = tmp_path / "d"
    dataset_root.mkdir()
    export_target_metadata(
        _source(),
        dataset_root,
        target_data_domain="crm",
        target_dataset="customers",
        target_bundle_root=tmp_path / "missing",  # tolerate an empty tree
    )
    manifest = (
        dataset_root / METADATA_DIR / "external" / "crm" / "customers" / "index.md"
    ).read_text(encoding="utf-8")
    assert "columns.tsv" in manifest
    assert "run_sql" in manifest
    # Qualified-name guidance is the load-bearing verification rule.
    assert '"<db>"."<table>"' in manifest


# --------------------------------------------------------------------------- #
# clients._session_policy: the widened (pair-pinned) Glue scope
# --------------------------------------------------------------------------- #


def test_session_policy_pins_both_databases_for_cross():
    policy = _session_policy(
        region="us-east-1",
        account_id="123456789012",
        database="orders",
        workgroup="primary",
        results_bucket_arn=None,
        extra_databases=["customers"],
    )
    glue_stmt = next(s for s in policy["Statement"] if s["Sid"] == "GlueThisDb")
    res = glue_stmt["Resource"]
    for db in ("orders", "customers"):
        assert f"arn:aws:glue:us-east-1:123456789012:database/{db}" in res
        assert f"arn:aws:glue:us-east-1:123456789012:table/{db}/*" in res
    # Never catalog-wide table access.
    assert not any(r.endswith(":table/*") for r in res)


def test_session_policy_single_database_unchanged_without_extras():
    policy = _session_policy(
        region="us-east-1",
        account_id="123456789012",
        database="orders",
        workgroup="primary",
        results_bucket_arn=None,
    )
    glue_stmt = next(s for s in policy["Statement"] if s["Sid"] == "GlueThisDb")
    dbs = [r for r in glue_stmt["Resource"] if ":database/" in r]
    assert dbs == ["arn:aws:glue:us-east-1:123456789012:database/orders"]


# --------------------------------------------------------------------------- #
# prompts + agent wiring
# --------------------------------------------------------------------------- #


def test_cross_supervisor_prompt_carries_the_mode_rules():
    from harvest.prompts import build_cross_supervisor_prompt

    p = build_cross_supervisor_prompt()
    low = p.lower()
    assert "external/<target_domain>/<target_dataset>/" in p
    assert "Cross-Dataset Reference" in p
    assert "cross_dataset" in p
    # The METHODOLOGY lives in the skill (prompts carry runtime facts only) —
    # the prompt must route the agent to it.
    assert "references/cross-dataset.md" in p
    # One-sided docs read from both sides: symmetric prose, and the target side
    # is served by the discovery signal.
    assert "live ONLY in this bundle" in p
    assert "cross-reference signal" in low
    # Links go to BOTH sides: home file-relative (stitches the link graph) and
    # the counterpart via the bundle-escaping address form (may dangle).
    assert "../../../../tables/<t>.md" in p
    assert "../../../../../../<target_domain>/<target_dataset>/tables/<t>.md" in p
    assert "dangle" in low
    # Verification is qualified SQL, candidates verified BEFORE authoring.
    assert '"<db>"."<table>"' in p or '`"db"."table"`' in p
    assert "cross-author" in p
    # The review pass must not use whole-bundle clustering in this scoped mode.
    assert "cluster_concepts" in p


def test_cross_author_authors_from_the_brief_without_reverifying():
    # Cost tuning: verification happens exactly twice per relationship — the
    # supervisor's measurements and the adversarial review pass. The author
    # must NOT run a third pass; it writes from a complete brief, and an
    # incomplete brief bounces back instead of being re-measured or invented.
    from harvest.prompts import build_cross_author_prompt

    p = build_cross_author_prompt()
    norm = " ".join(p.split())
    assert "EXACTLY ONE" in p
    assert "Cross-Dataset Reference" in p
    assert "references/cross-dataset.md" in p
    assert "do NOT re-run it" in norm
    assert "Author FROM the brief" in norm
    assert "write NOTHING and return what is missing" in norm
    # Reading is scoped to the involved table sheets, not the whole wikis.
    assert "Read ONLY what the doc needs" in norm


def test_cross_supervisor_owns_verification_and_small_runs():
    # The brief contract + the two-pass verification rule + the no-fan-out rule
    # for one or two relationships.
    from harvest.prompts import build_cross_supervisor_prompt

    norm = " ".join(build_cross_supervisor_prompt().split())
    assert "MORE THAN TWO verified relationships" in norm
    assert "author the docs yourself" in norm
    assert "Verification happens exactly TWICE" in norm
    assert "Cross-authors do NOT re-verify" in norm
    assert "this IS the independent verification" in norm


def test_cross_prompts_gate_on_business_convergence_before_sql():
    # The tuning that keeps Formula 1 × california_schools from happening:
    # understanding both wikis comes FIRST, SQL only verifies named hypotheses,
    # and "no relationship" is an explicitly valid outcome that authors nothing.
    from harvest.prompts import build_cross_run_prompt, build_cross_supervisor_prompt

    p = build_cross_supervisor_prompt()
    norm = " ".join(p.split())
    assert "UNDERSTAND FIRST" in norm
    assert "no SQL yet" in norm
    assert "BUSINESS convergences" in norm
    assert "consumer question" in norm
    assert "STOP: author NOTHING" in norm
    assert "Unrelated is a valid, common outcome" in norm
    assert "does not go fishing" in norm

    run = build_cross_run_prompt(
        data_domain="sales",
        dataset="orders",
        database="orders_db",
        target_data_domain="crm",
        target_dataset="customers",
        target_database="customers_db",
        tables=["orders"],
        target_tables=["customers"],
    )
    run_norm = " ".join(run.split())
    assert "UNDERSTAND FIRST" in run_norm
    assert "author NOTHING" in run_norm


def test_skill_carries_cross_methodology_and_is_registered():
    # The methodology itself lives in the vendored skill (not the prompts):
    # discovery lenses, the verification bar, mirrored-doc conventions, and the
    # refuted-candidate rule — and SKILL.md registers/points to the reference.
    from pathlib import Path

    from harvest import agent as ag

    skill_root = Path(ag.__file__).resolve().parents[2] / "skills" / "okf-authoring"
    ref = (skill_root / "references" / "cross-dataset.md").read_text(encoding="utf-8")
    low = " ".join(ref.lower().split())
    for needle in (
        "symmetric",
        "cardinality",
        "overlap",
        "orphan",
        "conformed dimension",
        "refuted candidate",
        "overview.md",
        '"db"."table"',
        # Understand-first phasing + the plausibility gate (business meaning
        # before mechanics; unrelated pairs author nothing).
        "understand both datasets first (no sql yet)",
        "plausibility gate",
        "unrelated is a valid, common outcome",
        "consumer question",
        "vocabulary overlap, not knowledge",
        "never to fish",
        # Links go to BOTH sides: home form + bundle-escaping counterpart
        # address (tolerated dangling).
        "link both sides",
        "bundle-escaping relative link",
        "dangle",
    ):
        assert needle in low, f"cross-dataset.md missing: {needle}"
    skill_md = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    assert "references/cross-dataset.md" in skill_md


def test_cross_run_prompt_names_both_sides_and_pair_dir():
    from harvest.prompts import build_cross_run_prompt

    p = build_cross_run_prompt(
        data_domain="sales",
        dataset="orders",
        database="orders_db",
        target_data_domain="crm",
        target_dataset="customers",
        target_database="customers_db",
        tables=["orders"],
        target_tables=["customers"],
    )
    assert "external/crm/customers" in p
    assert ".metadata/external/crm/customers" in p
    assert '"orders_db"' in p and '"customers_db"' in p
    assert "cross_dataset:" in p
    assert "source: {data_domain: sales, dataset: orders}" in p
    assert "target: {data_domain: crm, dataset: customers}" in p


def test_cross_agent_wiring_in_source():
    # Assert on source (deepagents absent offline), like the other wiring tests.
    import inspect

    from harvest import agent as ag

    src = inspect.getsource(ag.build_harvest_agent)
    assert '"name": "cross-author"' in src
    # The cross build's reviewer carries the GUARD (deepagents hands every
    # sub-agent write tools; in cross mode the reviewer reads a copy of another
    # dataset's wiki — an injection surface — so the pair-subtree confinement
    # must hold on its write path too) AND the cross-specific reviewer prompt
    # (the standard table-doc checklist makes cross reviewers re-do discovery).
    assert "build_cross_reviewer_prompt(" in src
    assert '"middleware": [guard, tool_errors],' in src
    assert "subagents = [cross_author, cross_reviewer]" in src
    assert "writable_prefix" in src
    # The prefix is built through the segment-VALIDATING helper, not f-strings.
    assert "external_pair_prefix(" in src


def test_cross_reviewer_prompt_verifies_claims_not_rediscovery():
    # The time sink this pins: reviewers over a handful of pair docs must not
    # re-do discovery with dozens of slow cross-database queries. Cross review
    # verifies what the docs CLAIM, with query economy framed as reasonableness
    # (the doc's own SQL usually suffices; aggregates over probe strings) —
    # deliberately NOT a numeric cap.
    from harvest.prompts import build_cross_reviewer_prompt

    p = build_cross_reviewer_prompt()
    norm = " ".join(p.split())
    assert "READ-ONLY" in p
    assert "do NOT redo it" in norm
    assert "no trawling `columns.tsv`" in norm
    assert "make each one count" in norm
    assert "usually all the verification a doc needs" in norm
    assert "single aggregate that confirms several claims" in norm
    # Conventions checked without extra queries.
    assert "cross_dataset" in p
    assert "bundle-escaping address form" in norm
    assert "no issues found" in p


# --------------------------------------------------------------------------- #
# entrypoint validation
# --------------------------------------------------------------------------- #


def test_entrypoint_validates_cross_target():
    from harvest.entrypoint import _validate

    base = {"data_domain": "sales", "dataset": "orders", "mode": "cross"}
    assert _validate(base) is not None  # no target at all
    assert (
        _validate({**base, "target": {"data_domain": "crm"}}) is not None
    )  # incomplete
    assert (
        _validate(
            {**base, "target": {"data_domain": "sales", "dataset": "orders"}}
        )
        is not None
    )  # self-target
    # Target components become destructive paths + the '#'-delimited XREF key —
    # invalid segments are rejected at the payload boundary.
    for bad in ("crm/evil", "..", "cust#omers", "a b"):
        assert (
            _validate({**base, "target": {"data_domain": "crm", "dataset": bad}})
            is not None
        ), bad
    assert (
        _validate(
            {**base, "target": {"data_domain": "crm", "dataset": "customers"}}
        )
        is None
    )


# --------------------------------------------------------------------------- #
# run_cross_harvest lifecycle (offline: agent/finalize/status patched)
# --------------------------------------------------------------------------- #


class _Src:
    def __init__(self, database="orders_db", tables=("orders",)):
        self.database = database
        self._tables = list(tables)

    def table_names(self):
        return list(self._tables)


class _SilentAgent:
    """An agent whose crawl authors nothing."""

    def stream(self, *_a, **_k):
        return iter(())


class _AuthoringAgent:
    """An agent that writes one pair doc when streamed (simulating authoring)."""

    def __init__(self, pair_dir: Path):
        self._pair_dir = pair_dir

    def stream(self, *_a, **_k):
        (self._pair_dir / "joins").mkdir(parents=True, exist_ok=True)
        (self._pair_dir / "overview.md").write_text("o", encoding="utf-8")
        (self._pair_dir / "joins" / "orders__customers.md").write_text(
            "j", encoding="utf-8"
        )
        return iter(())


def _patch_cross(monkeypatch, agent, transitions):
    class _Built:
        pass

    _Built.agent = agent
    monkeypatch.setattr(runner, "build_harvest_agent", lambda *a, **k: _Built())
    monkeypatch.setattr(runner, "_table_versions", lambda *_a, **_k: {})
    monkeypatch.setattr(runner, "export_metadata", lambda *a, **k: {})
    monkeypatch.setattr(
        runner,
        "export_target_metadata",
        lambda *a, **k: {"table_count": 1, "docs_copied": 1},
    )
    monkeypatch.setattr(runner, "build_registry_client", lambda: ("fake", "tbl"))

    def fake_report(registry, *, data_domain, dataset, status, detail=None, **_k):
        transitions.append((f"{data_domain}/{dataset}", status, detail))

    monkeypatch.setattr(runner, "report_status", fake_report)


def _mark_target_ready(target_root: Path) -> None:
    """Seed the target's commit marker (the runner re-checks it at run time)."""
    (target_root / ".harvest").mkdir(parents=True, exist_ok=True)
    (target_root / ".harvest" / "state.json").write_text(
        '{"status": "complete"}', encoding="utf-8"
    )


def _run(tmp_path, monkeypatch, agent_cls=_SilentAgent):
    transitions: list[tuple] = []
    root = tmp_path / "sales" / "orders"
    target_root = tmp_path / "crm" / "customers"
    _mark_target_ready(target_root)
    pair_dir = root / "external" / "crm" / "customers"
    agent = agent_cls(pair_dir) if agent_cls is _AuthoringAgent else agent_cls()
    _patch_cross(monkeypatch, agent, transitions)
    runner.run_cross_harvest(
        source=_Src("orders_db", ("orders",)),
        dataset_root=root,
        data_domain="sales",
        dataset="orders",
        target_source=_Src("customers_db", ("customers",)),
        target_root=target_root,
        target_data_domain="crm",
        target_dataset="customers",
    )
    return transitions, root, target_root


def test_cross_run_reports_running_then_complete(tmp_path, monkeypatch):
    transitions, root, _ = _run(tmp_path, monkeypatch)
    assert [(t[0], t[1]) for t in transitions] == [
        ("sales/orders", "running"),
        ("sales/orders", "complete"),
    ]
    # Zero docs authored = no genuine convergence — a valid outcome, said plainly.
    assert "no genuine convergence found" in transitions[-1][2]
    # This side still finalized: commit marker written with cross provenance.
    state = (root / ".harvest" / "state.json").read_text(encoding="utf-8")
    assert '"cross_target": "crm/customers"' in state


def test_cross_run_is_one_sided_never_touches_the_target(tmp_path, monkeypatch):
    # The design decision this pins: pair docs have ONE home (this bundle).
    # Nothing — no docs, no marker changes, no status rows — is written
    # target-side; discoverability rides the reindex-derived XREF signal.
    transitions, root, target_root = _run(tmp_path, monkeypatch, _AuthoringAgent)
    # The target tree holds exactly what the test seeded: its own marker.
    assert [p for p in target_root.rglob("*") if p.is_file()] == [
        target_root / ".harvest" / "state.json"
    ]
    assert (target_root / ".harvest" / "state.json").read_text(
        encoding="utf-8"
    ) == '{"status": "complete"}'
    assert all(t[0] == "sales/orders" for t in transitions)
    # The authored docs live in THIS bundle's pair subtree.
    pair_dir = root / "external" / "crm" / "customers"
    assert (pair_dir / "overview.md").is_file()
    assert (pair_dir / "joins" / "orders__customers.md").is_file()
    src_final = transitions[-1]
    assert src_final[1] == "complete"
    assert "2 doc(s)" in src_final[2]


def test_cross_run_clears_prior_pair_output(tmp_path, monkeypatch):
    # A doc from a previous cross run of the SAME pair must not linger — but
    # OTHER pairs' subtrees are untouched.
    root = tmp_path / "sales" / "orders"
    stale = root / "external" / "crm" / "customers" / "joins" / "stale.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale", encoding="utf-8")
    other_pair = root / "external" / "ops" / "logs" / "overview.md"
    other_pair.parent.mkdir(parents=True)
    other_pair.write_text("other pair", encoding="utf-8")
    target_root = tmp_path / "crm" / "customers"
    _mark_target_ready(target_root)
    transitions: list[tuple] = []
    _patch_cross(monkeypatch, _SilentAgent(), transitions)
    runner.run_cross_harvest(
        source=_Src(),
        dataset_root=root,
        data_domain="sales",
        dataset="orders",
        target_source=_Src("customers_db"),
        target_root=target_root,
        target_data_domain="crm",
        target_dataset="customers",
    )
    assert not stale.exists()
    assert other_pair.is_file()
    assert transitions[-1][1] == "complete"


def test_cross_run_fails_before_any_destruction_when_snapshot_fails(
    tmp_path, monkeypatch
):
    # ORDERING IS LOAD-BEARING: the required target snapshot runs BEFORE the
    # in-progress marker flip and the pair wipe — a snapshot failure must
    # report `failed` while leaving the bundle READY and the prior pair docs
    # intact (the old order destroyed them and self-locked cross mode).
    root = tmp_path / "sales" / "orders"
    prior = root / "external" / "crm" / "customers" / "joins" / "prior.md"
    prior.parent.mkdir(parents=True)
    prior.write_text("prior pair doc", encoding="utf-8")
    (root / ".harvest").mkdir()
    (root / ".harvest" / "state.json").write_text(
        '{"status": "complete"}', encoding="utf-8"
    )
    target_root = tmp_path / "crm" / "customers"
    _mark_target_ready(target_root)
    transitions: list[tuple] = []
    _patch_cross(monkeypatch, _SilentAgent(), transitions)
    monkeypatch.setattr(
        runner,
        "export_target_metadata",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("target read failed")),
    )
    with pytest.raises(RuntimeError, match="target read failed"):
        runner.run_cross_harvest(
            source=_Src(),
            dataset_root=root,
            data_domain="sales",
            dataset="orders",
            target_source=_Src("customers_db"),
            target_root=target_root,
            target_data_domain="crm",
            target_dataset="customers",
        )
    assert transitions[-1][1] == "failed"
    assert "target read failed" in transitions[-1][2]
    # Nothing destructive happened: pair docs intact, bundle still READY.
    assert prior.read_text(encoding="utf-8") == "prior pair doc"
    assert '"status": "complete"' in (root / ".harvest" / "state.json").read_text(
        encoding="utf-8"
    )


def test_cross_run_aborts_when_target_not_ready(tmp_path, monkeypatch):
    # The trigger-time readiness check only covers trigger time: a full harvest
    # of the target may have started since. The runtime re-checks the target's
    # commit marker and fails loudly — again BEFORE any destructive step.
    root = tmp_path / "sales" / "orders"
    prior = root / "external" / "crm" / "customers" / "overview.md"
    prior.parent.mkdir(parents=True)
    prior.write_text("prior", encoding="utf-8")
    target_root = tmp_path / "crm" / "customers"
    (target_root / ".harvest").mkdir(parents=True)
    (target_root / ".harvest" / "state.json").write_text(
        '{"status": "in_progress"}', encoding="utf-8"
    )
    transitions: list[tuple] = []
    _patch_cross(monkeypatch, _SilentAgent(), transitions)
    with pytest.raises(RuntimeError, match="not published"):
        runner.run_cross_harvest(
            source=_Src(),
            dataset_root=root,
            data_domain="sales",
            dataset="orders",
            target_source=_Src("customers_db"),
            target_root=target_root,
            target_data_domain="crm",
            target_dataset="customers",
        )
    assert transitions[-1][1] == "failed"
    assert prior.is_file()
