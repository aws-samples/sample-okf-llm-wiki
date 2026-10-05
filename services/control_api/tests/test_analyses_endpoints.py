"""The analyses serving surface: the chat side panel's read (server-parsed
document), the Analysis page's cross-dataset lists (documents with
publication counts, published reports), and the owner-only human lifecycle
(full-document edit under an optimistic lock, delete purging publications).

Rows are seeded directly (the chat runtime's analysis tools own the agent
writes — see services/chat/tests/test_analyses.py).
"""

from __future__ import annotations

import pytest

from control_api import handlers
from control_api.handlers import ApiError

from tests.conftest import ANALYSES

DOMAIN, DATASET = "sales", "orders"
SUB, OTHER_SUB = "user-1", "user-2"


ANALYSIS_DOC = """---
title: Churn cohort deep-dive
description: Quantifies churn by signup cohort.
questions:
  - id: time_window
    prompt: Which period should the analysis cover?
    kind: single
    options: ["Last quarter", "Last 12 months"]
---
## Purpose
Find where churn concentrates.

## Steps
1. Run the `churn_by_cohort` computation with `@window` = {time_window}.

## Finalize
A report with the churn KPI per cohort.
"""


def _seed_analysis(ddb, *, name="churn-cohorts", sub=SUB, body=ANALYSIS_DOC):
    from okf_core.analyses import analysis_pk

    ddb.put_item(
        TableName=ANALYSES,
        Item={
            "pk": {"S": analysis_pk(DOMAIN, DATASET)},
            "sk": {"S": name},
            "name": {"S": name},
            "data_domain": {"S": DOMAIN},
            "dataset": {"S": DATASET},
            "title": {"S": "Churn cohort deep-dive"},
            "description": {"S": "Quantifies churn by signup cohort."},
            "questions": {"N": "1"},
            "owner_sub": {"S": sub},
            "created_at": {"S": "2026-08-17T09:00:00+00:00"},
            "updated_at": {"S": "2026-08-17T10:00:00+00:00"},
            "version": {"N": "3"},
            "body": {"S": body},
        },
    )


def test_get_analysis_parses_document_serverside(cfg):
    _seed_analysis(cfg.ddb)
    out = handlers.get_analysis(
        cfg.ddb,
        analyses_table=ANALYSES,
        user_sub=SUB,
        data_domain=DOMAIN,
        dataset=DATASET,
        name="churn-cohorts",
    )
    assert out["name"] == "churn-cohorts"
    assert out["title"] == "Churn cohort deep-dive"
    assert out["version"] == 3
    assert out["owned_by_you"] is True
    # frontmatter parsed off; questions come back as the authored list
    (q,) = out["questions"]
    assert q["id"] == "time_window" and q["kind"] == "single"
    assert out["body"].startswith("## Purpose")
    assert out["document"] == ANALYSIS_DOC  # raw doc rides along


def test_get_analysis_ownership_is_callers_view(cfg):
    _seed_analysis(cfg.ddb, sub=OTHER_SUB)
    out = handlers.get_analysis(
        cfg.ddb,
        analyses_table=ANALYSES,
        user_sub=SUB,
        data_domain=DOMAIN,
        dataset=DATASET,
        name="churn-cohorts",
    )
    assert out["owned_by_you"] is False


def test_get_analysis_missing_is_404(cfg):
    with pytest.raises(ApiError) as e:
        handlers.get_analysis(
            cfg.ddb,
            analyses_table=ANALYSES,
            user_sub=SUB,
            data_domain=DOMAIN,
            dataset=DATASET,
            name="ghost",
        )
    assert e.value.status == 404


def test_get_analysis_tolerates_unparseable_document(cfg):
    # Display path: a doc that predates a format change must still render —
    # questions empty, the raw text as the body.
    _seed_analysis(cfg.ddb, body="just steps, no frontmatter")
    out = handlers.get_analysis(
        cfg.ddb,
        analyses_table=ANALYSES,
        user_sub=SUB,
        data_domain=DOMAIN,
        dataset=DATASET,
        name="churn-cohorts",
    )
    assert out["questions"] == []
    assert out["body"] == "just steps, no frontmatter"


def _seed_publication(
    ddb, *, dd=DOMAIN, ds=DATASET, name="churn-cohorts", report_id, title, at, sub=SUB
):
    from okf_core.analyses import analysis_pk, publication_sk

    ddb.put_item(
        TableName=ANALYSES,
        Item={
            "pk": {"S": analysis_pk(dd, ds)},
            "sk": {"S": publication_sk(name, report_id)},
            "name": {"S": name},
            "data_domain": {"S": dd},
            "dataset": {"S": ds},
            "report_id": {"S": report_id},
            "title": {"S": title},
            "analysis_version": {"N": "2"},
            "published_by": {"S": sub},
            "published_at": {"S": at},
        },
    )


def test_list_analysis_reports_spans_datasets_publications_only(cfg):
    # Noise that must NOT surface: the analysis DOC rows.
    _seed_analysis(cfg.ddb)
    _seed_publication(
        cfg.ddb,
        report_id="rep~sales~orders~20260810T090000Z~aaaa1111",
        title="July run",
        at="2026-08-10T09:05:00+00:00",
        sub=OTHER_SUB,
    )
    _seed_publication(
        cfg.ddb,
        dd="motorsport",
        ds="f1",
        name="f1-season-review",
        report_id="rep~motorsport~f1~20260817T120000Z~bbbb2222",
        title="1988 season review",
        at="2026-08-17T12:05:00+00:00",
    )
    out = handlers.list_analysis_reports(
        cfg.ddb, analyses_table=ANALYSES, user_sub=SUB
    )
    first, second = out["reports"]  # newest first, exactly the two publications
    assert first["report_id"] == "rep~motorsport~f1~20260817T120000Z~bbbb2222"
    assert first["analysis"] == "f1-season-review"
    assert first["data_domain"] == "motorsport" and first["dataset"] == "f1"
    assert first["published_by_you"] is True
    assert second["title"] == "July run" and second["published_by_you"] is False
    assert second["analysis_version"] == 2


def test_list_analysis_reports_empty(cfg):
    _seed_analysis(cfg.ddb)  # a document alone yields nothing
    assert handlers.list_analysis_reports(
        cfg.ddb, analyses_table=ANALYSES, user_sub=SUB
    ) == {"reports": []}


# --- Analysis page lifecycle: list / manual edit / delete ------------------------


def test_list_analyses_spans_datasets_with_publication_counts(cfg):
    _seed_analysis(cfg.ddb)  # sales/orders, owned by SUB
    _seed_analysis_other = _seed_analysis  # readability alias
    # a second dataset's analysis owned by someone else
    from okf_core.analyses import analysis_pk

    cfg.ddb.put_item(
        TableName=ANALYSES,
        Item={
            "pk": {"S": analysis_pk("motorsport", "f1")},
            "sk": {"S": "f1-season-review"},
            "name": {"S": "f1-season-review"},
            "data_domain": {"S": "motorsport"},
            "dataset": {"S": "f1"},
            "title": {"S": "F1 season review"},
            "description": {"S": "Reviews one season end to end."},
            "questions": {"N": "2"},
            "owner_sub": {"S": OTHER_SUB},
            "updated_at": {"S": "2026-08-17T12:00:00+00:00"},
            "version": {"N": "1"},
            "body": {"S": "x"},
        },
    )
    _seed_publication(
        cfg.ddb,
        report_id="rep~sales~orders~20260810T090000Z~aaaa1111",
        title="July run",
        at="2026-08-10T09:05:00+00:00",
    )

    out = handlers.list_analyses(cfg.ddb, analyses_table=ANALYSES, user_sub=SUB)
    rows = {r["name"]: r for r in out["analyses"]}
    assert set(rows) == {"churn-cohorts", "f1-season-review"}
    assert rows["churn-cohorts"]["owned_by_you"] is True
    assert rows["churn-cohorts"]["published_reports"] == 1
    assert rows["f1-season-review"]["owned_by_you"] is False
    assert rows["f1-season-review"]["published_reports"] == 0
    assert rows["f1-season-review"]["questions"] == 2


def test_update_analysis_full_replace_owner_only(cfg):
    _seed_analysis(cfg.ddb)  # v3, owner SUB
    new_doc = ANALYSIS_DOC.replace(
        "title: Churn cohort deep-dive", "title: Churn cohorts"
    )
    out = handlers.update_analysis(
        cfg.ddb,
        analyses_table=ANALYSES,
        user_sub=SUB,
        data_domain=DOMAIN,
        dataset=DATASET,
        name="churn-cohorts",
        document=new_doc,
        expected_version=3,
    )
    assert out["saved"] == "churn-cohorts" and out["version"] == 4
    got = handlers.get_analysis(
        cfg.ddb, analyses_table=ANALYSES, user_sub=SUB,
        data_domain=DOMAIN, dataset=DATASET, name="churn-cohorts",
    )
    assert got["title"] == "Churn cohorts" and got["version"] == 4
    assert got["document"] == new_doc

    # non-owner refused
    with pytest.raises(ApiError) as e:
        handlers.update_analysis(
            cfg.ddb, analyses_table=ANALYSES, user_sub=OTHER_SUB,
            data_domain=DOMAIN, dataset=DATASET, name="churn-cohorts",
            document=new_doc, expected_version=4,
        )
    assert e.value.status == 403


def test_update_analysis_validates_and_locks(cfg):
    _seed_analysis(cfg.ddb)  # v3
    # invalid document => 400 with the problems named, nothing saved
    with pytest.raises(ApiError) as e:
        handlers.update_analysis(
            cfg.ddb, analyses_table=ANALYSES, user_sub=SUB,
            data_domain=DOMAIN, dataset=DATASET, name="churn-cohorts",
            document="---\ntitle: T\n---\nsteps", expected_version=3,
        )
    assert e.value.status == 400 and "description" in str(e.value)
    # stale version => 409 (someone saved in between)
    with pytest.raises(ApiError) as e:
        handlers.update_analysis(
            cfg.ddb, analyses_table=ANALYSES, user_sub=SUB,
            data_domain=DOMAIN, dataset=DATASET, name="churn-cohorts",
            document=ANALYSIS_DOC, expected_version=2,
        )
    assert e.value.status == 409
    got = handlers.get_analysis(
        cfg.ddb, analyses_table=ANALYSES, user_sub=SUB,
        data_domain=DOMAIN, dataset=DATASET, name="churn-cohorts",
    )
    assert got["version"] == 3 and got["document"] == ANALYSIS_DOC


def test_delete_analysis_owner_only_and_purges_publications(cfg):
    _seed_analysis(cfg.ddb)
    _seed_publication(
        cfg.ddb,
        report_id="rep~sales~orders~20260810T090000Z~aaaa1111",
        title="July run",
        at="2026-08-10T09:05:00+00:00",
    )
    with pytest.raises(ApiError) as e:
        handlers.delete_analysis(
            cfg.ddb, analyses_table=ANALYSES, user_sub=OTHER_SUB,
            data_domain=DOMAIN, dataset=DATASET, name="churn-cohorts",
        )
    assert e.value.status == 403

    out = handlers.delete_analysis(
        cfg.ddb, analyses_table=ANALYSES, user_sub=SUB,
        data_domain=DOMAIN, dataset=DATASET, name="churn-cohorts",
    )
    assert out == {"deleted": "churn-cohorts", "purged_publications": 1}
    # the whole partition is empty — no zombie publication rows
    from okf_core.analyses import analysis_pk

    scan = cfg.ddb.query(
        TableName=ANALYSES,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": {"S": analysis_pk(DOMAIN, DATASET)}},
    )
    assert scan["Items"] == []

    with pytest.raises(ApiError) as e:
        handlers.delete_analysis(
            cfg.ddb, analyses_table=ANALYSES, user_sub=SUB,
            data_domain=DOMAIN, dataset=DATASET, name="churn-cohorts",
        )
    assert e.value.status == 404


def test_delete_analysis_sweeps_a_publication_that_raced_in(cfg):
    # A publish_report landing BETWEEN the first purge and the document delete
    # (its ConditionCheck passed — the document still existed) must not
    # survive as an orphan: the post-delete sweep removes it.
    _seed_analysis(cfg.ddb)
    real_delete = cfg.ddb.delete_item

    class _Racing:
        def __getattr__(self, attr):
            return getattr(cfg.ddb, attr)

        def delete_item(self, **kw):
            if kw["Key"]["sk"]["S"] == "churn-cohorts":
                _seed_publication(
                    cfg.ddb,
                    report_id="rep~sales~orders~20261004T101010Z~bbbb2222",
                    title="Raced in",
                    at="2026-10-04T10:10:10+00:00",
                )
            return real_delete(**kw)

    out = handlers.delete_analysis(
        _Racing(), analyses_table=ANALYSES, user_sub=SUB,
        data_domain=DOMAIN, dataset=DATASET, name="churn-cohorts",
    )
    assert out["purged_publications"] == 1
    assert cfg.ddb.scan(TableName=ANALYSES)["Items"] == []


def test_get_analysis_serves_normalized_questions(cfg):
    # The Run dialog renders these as-is: the ask_human shape, not raw YAML.
    _seed_analysis(
        cfg.ddb,
        body=ANALYSIS_DOC.replace(
            'options: ["Last quarter", "Last 12 months"]',
            'options: ["Last quarter", " ", "Last 12 months "]',
        ),
    )
    out = handlers.get_analysis(
        cfg.ddb, analyses_table=ANALYSES, user_sub=SUB,
        data_domain=DOMAIN, dataset=DATASET, name="churn-cohorts",
    )
    q = out["questions"][0]
    assert q["options"] == ["Last quarter", "Last 12 months"]
    assert q["allow_other"] is True
