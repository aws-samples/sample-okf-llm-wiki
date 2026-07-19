"""review_store: off-mount key shape, doc build, and the empty-session guard."""

from __future__ import annotations

import json

from harvest.benchmark.review_store import (
    build_review_document,
    make_review_persister,
    review_key,
    review_prefix,
)
from harvest.benchmark.tool import QuestionReview


def test_key_is_off_mount_and_shaped():
    assert review_prefix("sales", "orders") == "benchmark/sales/orders/reviews/"
    key = review_key("sales", "orders", "sess-1", 3)
    assert key == "benchmark/sales/orders/reviews/sess-1/3.json"
    # Load-bearing: off the okf/ mount so no LLM role can read the gold it carries.
    assert not key.startswith("okf/")


def test_build_document_counts_buckets_and_serializes_gold():
    review = [
        QuestionReview(q_id=0, bucket="passed", question="Q0", gold_sql="G0", predicted_sql="P0"),
        QuestionReview(q_id=1, bucket="genuine_error", question="Q1", gold_sql="G1",
                       predicted_sql="W1", note="docs miss X"),
    ]
    doc = build_review_document(2, review)
    assert doc["iteration"] == 2
    assert doc["counts"] == {"passed": 1, "genuine_error": 1}
    assert {q["q_id"] for q in doc["questions"]} == {0, 1}
    assert any(q["gold_sql"] == "G1" for q in doc["questions"])


def test_persister_writes_to_off_mount_key(monkeypatch):
    monkeypatch.setenv("OKF_BUNDLE_BUCKET", "okf-bundle")
    puts = []
    persist = make_review_persister(
        data_domain="sales", dataset="orders", session_id="sess-1",
        put_object=lambda b, k, body: puts.append((b, k, json.loads(body))),
    )
    persist(0, [QuestionReview(q_id=0, bucket="passed", question="Q", gold_sql="G")])
    assert len(puts) == 1
    bucket, key, doc = puts[0]
    assert bucket == "okf-bundle"
    assert key == "benchmark/sales/orders/reviews/sess-1/0.json"
    assert doc["counts"] == {"passed": 1}


def test_persister_is_noop_without_session_id(monkeypatch):
    # A blank session id would yield a double-slash key (reviews//0.json) whose empty
    # {session} route segment is unfetchable — so persistence is skipped entirely.
    monkeypatch.setenv("OKF_BUNDLE_BUCKET", "okf-bundle")
    puts = []
    persist = make_review_persister(
        data_domain="sales", dataset="orders", session_id="",
        put_object=lambda b, k, body: puts.append(k),
    )
    persist(0, [QuestionReview(q_id=0, bucket="passed", question="Q", gold_sql="G")])
    assert puts == []  # nothing written


def test_persister_is_noop_without_bucket(monkeypatch):
    monkeypatch.delenv("OKF_BUNDLE_BUCKET", raising=False)
    puts = []
    persist = make_review_persister(
        data_domain="sales", dataset="orders", session_id="sess-1",
        put_object=lambda b, k, body: puts.append(k),
    )
    persist(0, [QuestionReview(q_id=0, bucket="passed", question="Q", gold_sql="G")])
    assert puts == []
