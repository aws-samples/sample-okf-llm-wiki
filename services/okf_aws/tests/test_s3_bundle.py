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
