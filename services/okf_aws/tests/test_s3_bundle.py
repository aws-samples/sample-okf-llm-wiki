import json

from okf_aws.s3_bundle import (
    bundle_prefix,
    is_bundle_ready,
    parse_bundle_key,
    state_marker_key,
)


def test_parse_table_key():
    loc = parse_bundle_key("okf/sales/orders/tables/races.md")
    assert loc is not None
    assert loc.data_domain == "sales"
    assert loc.dataset == "orders"
    assert loc.concept_id == "tables/races"
    assert loc.table == "races"
    assert loc.vector_key == "sales/orders/tables/races"


def test_parse_dataset_key():
    loc = parse_bundle_key("okf/sales/orders/datasets/orders.md")
    assert loc.concept_id == "datasets/orders"
    assert loc.table is None


def test_parse_reference_key():
    loc = parse_bundle_key("okf/sales/orders/references/joins/a__b.md")
    assert loc.concept_id == "references/joins/a__b"
    assert loc.table is None


def test_ignores_index_and_log():
    assert parse_bundle_key("okf/sales/orders/index.md") is None
    assert parse_bundle_key("okf/sales/orders/tables/index.md") is None
    assert parse_bundle_key("okf/sales/orders/log.md") is None


def test_ignores_dot_dirs():
    assert parse_bundle_key("okf/sales/orders/.context/spec.md") is None
    assert parse_bundle_key("okf/sales/orders/.harvest/state.json") is None


def test_ignores_non_bundle_and_non_md():
    assert parse_bundle_key("other/sales/orders/tables/races.md") is None
    assert parse_bundle_key("okf/sales/orders/tables/races.txt") is None
    assert parse_bundle_key("okf/sales/orders.md") is None  # too shallow


def test_ignores_scratch_dir_leaks_but_not_scratch_named_stems():
    # deepagents scratch leaks are runtime state, never concepts — the S3 side
    # must apply the same rule as okf_core's local walkers (link graph,
    # graph_json), or the precomputed and live-computed graphs diverge.
    assert parse_bundle_key("okf/d/ds/large_tool_results/leak.md") is None
    assert parse_bundle_key("okf/d/ds/conversation_history/00.md") is None
    # PARENTS only: a table legitimately named after a scratch dir survives.
    loc = parse_bundle_key("okf/d/ds/tables/conversation_history.md")
    assert loc is not None and loc.table == "conversation_history"


def test_ignores_empty_segments():
    # A `//` in the key would make the concept id an absolute path when
    # joined under a bundle root (temp-dir escape in build_graph_json).
    assert parse_bundle_key("okf/d/ds//x.md") is None
    assert parse_bundle_key("okf/d/ds/tables//x.md") is None


def test_concept_rules_match_the_local_walker(tmp_path):
    # The parity contract behind the precomputed /graph artifact: the S3 side
    # (parse_bundle_key) and the local walk (collect_bundle_files) must accept
    # exactly the same set of relative paths, or the artifact and the live
    # fallback would render different graphs for the same bundle.
    from okf_core.graph_json import collect_bundle_files

    rels = [
        "tables/orders.md",              # concept
        "datasets/orders.md",            # concept
        "external/d/ds/joins/x.md",      # concept
        "tables/conversation_history.md",  # concept (scratch name as STEM)
        "index.md",                      # reserved
        "tables/index.md",               # reserved
        "log.md",                        # reserved
        ".harvest/notes.md",             # dot dir
        ".metadata/tables/orders.md",    # dot dir
        "large_tool_results/leak.md",    # scratch dir
        "conversation_history/00.md",    # scratch dir
        "tables/.hidden.md",             # dot stem
    ]
    for rel in rels:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("---\ntitle: t\ntype: T\n---\nbody", encoding="utf-8")

    local = set(collect_bundle_files(tmp_path))
    s3_side = {
        loc.concept_id
        for rel in rels
        if (loc := parse_bundle_key(f"okf/d/ds/{rel}")) is not None
    }
    assert local == s3_side == {
        "tables/orders",
        "datasets/orders",
        "external/d/ds/joins/x",
        "tables/conversation_history",
    }


def test_key_helpers():
    assert bundle_prefix("sales", "orders") == "okf/sales/orders/"
    assert state_marker_key("sales", "orders") == "okf/sales/orders/.harvest/state.json"


class FakeS3:
    def __init__(self, objects):
        self._objects = objects

    def get_object(self, Bucket, Key):
        if Key not in self._objects:
            raise KeyError(Key)
        return {"Body": _Body(self._objects[Key])}


class _Body:
    def __init__(self, data):
        self._data = data.encode() if isinstance(data, str) else data

    def read(self):
        return self._data


def test_is_bundle_ready_true():
    s3 = FakeS3({"okf/s/o/.harvest/state.json": json.dumps({"status": "complete"})})
    assert is_bundle_ready(s3, "b", "s", "o") is True


def test_is_bundle_ready_false_in_progress():
    s3 = FakeS3({"okf/s/o/.harvest/state.json": json.dumps({"status": "in_progress"})})
    assert is_bundle_ready(s3, "b", "s", "o") is False


def test_is_bundle_ready_false_missing():
    assert is_bundle_ready(FakeS3({}), "b", "s", "o") is False
