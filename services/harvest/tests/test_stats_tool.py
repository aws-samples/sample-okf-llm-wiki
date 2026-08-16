"""The supervisor's no-arg `get_stats` inventory tool: shape + wiring.

The counting itself is okf_core.stats' (tested there); here we test the tool
contract (no args, never raises) and that it reaches every SUPERVISOR mode
but no sub-agent spec.
"""

from pathlib import Path

from harvest.stats_tool import make_stats_tool


def _write(root: Path, rel: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")


def test_get_stats_is_no_arg_and_shows_zeros(tmp_path):
    _write(tmp_path, "datasets/f1.md")
    _write(tmp_path, "tables/races.md")
    _write(tmp_path, ".metadata/tables/races.md")
    result = make_stats_tool(tmp_path).invoke({})
    assert result["tables"] == 1 and result["snapshot_tables"] == 1
    assert result["references"]["named_sets"] == 0  # absence is visible


def test_get_stats_never_raises(tmp_path, monkeypatch):
    import harvest.stats_tool as st

    def _boom(root):
        raise RuntimeError("mount gone")

    monkeypatch.setattr(st, "bundle_stats", _boom)
    result = st.make_stats_tool(tmp_path).invoke({})
    assert "get_stats crashed" in result["note"]


def test_stats_tool_is_wired_to_every_supervisor_not_subagents():
    import inspect

    from harvest import agent as ag

    src = inspect.getsource(ag.build_harvest_agent)
    # Every supervisor mode: appended BEFORE the full_harvest-only gate.
    assert "main_tools.append(make_stats_tool(dataset_root))" in src
    idx_stats = src.index("main_tools.append(make_stats_tool(dataset_root))")
    idx_full = src.index("if full_harvest:")
    assert idx_stats < idx_full
    # Sub-agent specs keep all_tools (bundle-wide inventory in their hands
    # invites cross-cluster coverage findings that are the supervisor's job).
    assert src.count('"tools": all_tools') >= 4


def test_supervisor_prompt_prescribes_get_stats():
    from harvest import prompts

    p = prompts.SUPERVISOR_PROMPT
    # Two hooks: the post-fan-out completion check (tables vs snapshot_tables)
    # and the cross-cutting-references step (zeros made visible, no nagging).
    assert p.count("get_stats") >= 2
    assert "snapshot_tables" in p
    assert "a zero is a fact" in p.lower() or "a zero row" in p.lower()
