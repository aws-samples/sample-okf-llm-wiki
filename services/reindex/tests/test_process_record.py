"""Unit tests for the pure process_record helper.

moto backs S3 + DynamoDB; s3vectors + bedrock-runtime are faked. No live AWS.
"""

from __future__ import annotations

import pytest

from reindex.handler import process_record
from fakes import (
    BUNDLE_BUCKET,
    FRESHNESS_TABLE,
    VECTOR_BUCKET,
    VECTOR_INDEX,
    FakeBedrock,
    FakeS3Vectors,
    put_object,
    s3_event_record,
)


def _process(record, aws, s3vectors, bedrock):
    return process_record(
        record,
        s3=aws["s3"],
        s3vectors=s3vectors,
        bedrock_runtime=bedrock,
        ddb=aws["ddb"],
        bundle_bucket=BUNDLE_BUCKET,
        vector_bucket=VECTOR_BUCKET,
        vector_index=VECTOR_INDEX,
        freshness_table=FRESHNESS_TABLE,
    )


def _seq_row(aws, vkey):
    tbl = aws["ddb"].Table(FRESHNESS_TABLE)
    return tbl.get_item(Key={"pk": f"VEC#{vkey}", "sk": "SEQ"}).get("Item")


# --- create / update ---------------------------------------------------------


def test_created_embeds_and_puts_vector(aws):
    key = "okf/na_mi/formula_1/tables/races.md"
    put_object(aws["s3"], key)
    s3v, br = FakeS3Vectors(), FakeBedrock()

    status = _process(s3_event_record(key), aws, s3v, br)

    assert status == "upserted"
    assert len(br.calls) == 1  # embedded exactly once
    assert len(s3v.put) == 1
    item = s3v.put[0]["vectors"][0]
    assert item["key"] == "na_mi/formula_1/tables/races"
    assert len(item["data"]["float32"]) == 512
    # merged filterable + non-filterable metadata
    md = item["metadata"]
    assert md["data_domain"] == "na_mi"
    assert md["dataset"] == "formula_1"
    assert md["table"] == "races"
    assert md["type"] == "Glue Table"
    assert md["title"] == "Races"
    assert md["s3_key"] == key
    assert "tags" in md
    # dedup row was written
    row = _seq_row(aws, "na_mi/formula_1/tables/races")
    assert row["last_sequencer"] == "00000000000000AAAA"


def test_update_overwrites_by_key_with_newer_sequencer(aws):
    key = "okf/na_mi/formula_1/tables/races.md"
    put_object(aws["s3"], key)
    s3v, br = FakeS3Vectors(), FakeBedrock()

    _process(s3_event_record(key, sequencer="000000000000000001"), aws, s3v, br)
    # a genuine update: same key, higher sequencer
    status = _process(
        s3_event_record(key, sequencer="000000000000000002"), aws, s3v, br
    )

    assert status == "upserted"
    assert len(s3v.put) == 2  # both accepted; PutVectors overwrites by key
    assert _seq_row(aws, "na_mi/formula_1/tables/races")["last_sequencer"] == (
        "000000000000000002"
    )


# --- delete ------------------------------------------------------------------


def test_deleted_removes_vector(aws):
    key = "okf/na_mi/formula_1/tables/races.md"
    s3v, br = FakeS3Vectors(), FakeBedrock()

    status = _process(s3_event_record(key, detail_type="Object Deleted"), aws, s3v, br)

    assert status == "deleted"
    assert s3v.deleted[0]["keys"] == ["na_mi/formula_1/tables/races"]
    assert s3v.put == []  # no embed on delete
    assert br.calls == []


# --- non-concept keys are ignored --------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "okf/na_mi/formula_1/index.md",
        "okf/na_mi/formula_1/log.md",
        "okf/na_mi/formula_1/.context/source.md",
        "okf/na_mi/formula_1/.harvest/state.json",
        "okf/na_mi/formula_1/tables/races.txt",  # not .md
        "something/else/entirely.md",  # wrong prefix
    ],
)
def test_non_concept_keys_skipped(aws, key):
    s3v, br = FakeS3Vectors(), FakeBedrock()
    status = _process(s3_event_record(key), aws, s3v, br)
    assert status == "skipped"
    assert s3v.put == [] and s3v.deleted == []
    assert br.calls == []


# --- sequencer dedup / ordering ----------------------------------------------


def test_older_sequencer_ignored(aws):
    key = "okf/na_mi/formula_1/tables/races.md"
    put_object(aws["s3"], key)
    s3v, br = FakeS3Vectors(), FakeBedrock()

    _process(s3_event_record(key, sequencer="0000000000000000FF"), aws, s3v, br)
    # replay of an OLDER event (lower sequencer) must be ignored
    status = _process(
        s3_event_record(key, sequencer="0000000000000000AA"), aws, s3v, br
    )

    assert status == "skipped"
    assert len(s3v.put) == 1  # second event did nothing
    assert _seq_row(aws, "na_mi/formula_1/tables/races")["last_sequencer"] == (
        "0000000000000000FF"
    )


def test_equal_sequencer_ignored(aws):
    key = "okf/na_mi/formula_1/tables/races.md"
    put_object(aws["s3"], key)
    s3v, br = FakeS3Vectors(), FakeBedrock()

    seq = "0000000000000000FF"
    _process(s3_event_record(key, sequencer=seq), aws, s3v, br)
    status = _process(s3_event_record(key, sequencer=seq), aws, s3v, br)

    assert status == "skipped"
    assert len(s3v.put) == 1


def test_newer_sequencer_accepted(aws):
    key = "okf/na_mi/formula_1/tables/races.md"
    put_object(aws["s3"], key)
    s3v, br = FakeS3Vectors(), FakeBedrock()

    _process(s3_event_record(key, sequencer="0000000000000000AA"), aws, s3v, br)
    status = _process(
        s3_event_record(key, sequencer="0000000000000000FF"), aws, s3v, br
    )

    assert status == "upserted"
    assert len(s3v.put) == 2


def test_delete_does_not_block_later_recreate(aws):
    """A delete claims the sequencer; a later create with a higher one still runs."""
    key = "okf/na_mi/formula_1/tables/races.md"
    put_object(aws["s3"], key)
    s3v, br = FakeS3Vectors(), FakeBedrock()

    _process(
        s3_event_record(
            key, detail_type="Object Deleted", sequencer="0000000000000000A0"
        ),
        aws,
        s3v,
        br,
    )
    status = _process(
        s3_event_record(key, sequencer="0000000000000000B0"), aws, s3v, br
    )
    assert status == "upserted"
    assert len(s3v.deleted) == 1
    assert len(s3v.put) == 1


# --- malformed records raise (so the handler reports batchItemFailure) -------


def test_malformed_body_raises(aws):
    s3v, br = FakeS3Vectors(), FakeBedrock()
    with pytest.raises(Exception):
        _process({"messageId": "m1", "body": "not-json{{{"}, aws, s3v, br)


def test_unexpected_detail_type_raises(aws):
    key = "okf/na_mi/formula_1/tables/races.md"
    s3v, br = FakeS3Vectors(), FakeBedrock()
    with pytest.raises(Exception):
        _process(
            s3_event_record(key, detail_type="Object Restore Completed"), aws, s3v, br
        )


# --- failure does not advance the dedup marker (retry re-processes) ----------


def test_failed_embed_does_not_advance_sequencer_so_retry_succeeds(aws):
    """A transient embed failure must NOT commit the sequencer.

    Regression for the "commit-before-work" bug: advancing last_sequencer before
    the embed/PutVectors meant an SQS redelivery saw the advanced marker, treated
    the record as a duplicate, and silently dropped the vector forever. Now the
    marker advances only on success, so the retry embeds and writes the vector.
    """
    key = "okf/na_mi/formula_1/tables/races.md"
    put_object(aws["s3"], key)
    s3v = FakeS3Vectors()
    br = FakeBedrock(fail_times=1)  # first embed throttles, second succeeds

    # First delivery: embed raises -> the whole record fails.
    with pytest.raises(Exception):
        _process(s3_event_record(key), aws, s3v, br)
    assert s3v.put == []  # nothing written
    assert _seq_row(aws, "na_mi/formula_1/tables/races") is None  # marker NOT advanced

    # SQS redelivers the SAME message (same sequencer): now it succeeds.
    status = _process(s3_event_record(key), aws, s3v, br)
    assert status == "upserted"
    assert len(s3v.put) == 1
    assert _seq_row(aws, "na_mi/formula_1/tables/races")["last_sequencer"] == (
        "00000000000000AAAA"
    )


def test_missing_object_surfaces_and_leaves_marker_unadvanced(aws):
    """An S3 GET failure must also leave the marker unadvanced for a clean retry."""
    key = "okf/na_mi/formula_1/tables/ghost.md"  # never put_object'd
    s3v, br = FakeS3Vectors(), FakeBedrock()
    with pytest.raises(Exception):
        _process(s3_event_record(key), aws, s3v, br)
    assert _seq_row(aws, "na_mi/formula_1/tables/ghost") is None
    assert s3v.put == []


def test_missing_object_key_raises(aws):
    import json

    bad = {
        "messageId": "m1",
        "body": json.dumps(
            {"detail-type": "Object Created", "detail": {"bucket": {"name": "b"}}}
        ),
    }
    s3v, br = FakeS3Vectors(), FakeBedrock()
    with pytest.raises(Exception):
        _process(bad, aws, s3v, br)


def test_missing_s3_object_surfaces_as_failure(aws):
    """Concept key accepted by dedup but object absent from S3 -> raises (retried)."""
    key = "okf/na_mi/formula_1/tables/ghost.md"  # never put_object'd
    s3v, br = FakeS3Vectors(), FakeBedrock()
    with pytest.raises(Exception):
        _process(s3_event_record(key), aws, s3v, br)
