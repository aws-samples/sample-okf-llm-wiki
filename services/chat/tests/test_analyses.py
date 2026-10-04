"""Analyses: parse/validate + the list/read/create/update tools (+ publish).

DynamoDB is real (moto, the analyses table's pk/sk shape). The load-bearing
contracts: frontmatter questions are VERBATIM ask_human objects (strict ids,
no defaults), update is an exact-match Edit-style replacement re-validated
whole under an optimistic lock, and ownership (create's recorded sub) gates
update but never list/read/execute — deletion is deliberately NOT an agent
tool (the Analysis page owns it; see control_api's delete_analysis).
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from okf_core import analyses as an

from chat.analyses import make_analysis_tools, parse_analysis

TABLE = "okf-analyses"
SUB = "owner-1"
OTHER = "someone-else"
SCOPE = {"data_domain": "sales", "dataset": "orders"}

DOC = """---
title: Churn cohort deep-dive
description: Quantifies churn by signup cohort and isolates which segments drive it.
questions:
  - id: time_window
    prompt: Which period should the analysis cover?
    kind: single
    options: ["Last quarter", "Last 12 months"]
  - id: segment
    prompt: Restrict to a customer segment, or run across all?
    kind: text
---
## Purpose
Find where churn concentrates.

## Steps
1. Run the `churn_by_cohort` computation with `@window` = {time_window}.

## Finalize
A report with the churn KPI per cohort.
"""


@pytest.fixture
def table():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)


def _tools(table, *, sub=SUB, scope=None):
    return {
        t.name: t
        for t in make_analysis_tools(table, user_sub=sub, dataset_scope=scope)
    }


def _create(tools, name="churn-cohort-deep-dive", body=DOC, **loc):
    loc = loc or {"data_domain": "sales", "dataset": "orders"}
    return tools["create_analysis"].invoke({"name": name, "body": body, **loc})


# --- parse_analysis -----------------------------------------------------------


def test_parse_valid_doc():
    parsed, errors = parse_analysis(DOC)
    assert errors == []
    assert parsed["title"] == "Churn cohort deep-dive"
    assert "segments drive it" in parsed["description"]
    assert parsed["body"].startswith("## Purpose")
    # questions come back NORMALIZED — the exact ask_human interrupt shape
    assert [q["id"] for q in parsed["questions"]] == ["time_window", "segment"]
    q0 = parsed["questions"][0]
    assert q0["kind"] == "single" and q0["options"] == ["Last quarter", "Last 12 months"]
    assert q0["allow_other"] is True


def test_parse_questions_are_verbatim_ask_human_payload():
    # THE contract: what the executor forwards to ask_human is exactly what
    # normalize_questions makes of the frontmatter list — no analysis-side
    # reshaping that could drift from the interrupt schema.
    import yaml

    from chat.ask_human import normalize_questions

    raw = yaml.safe_load(DOC.split("---")[1])["questions"]
    parsed, _ = parse_analysis(DOC)
    assert parsed["questions"] == normalize_questions(raw)


def test_parse_questions_optional():
    doc = "---\ntitle: T\ndescription: D\n---\n## Steps\n1. x\n"
    parsed, errors = parse_analysis(doc)
    assert errors == [] and parsed["questions"] == []


@pytest.mark.parametrize(
    "doc,needle",
    [
        ("no frontmatter at all", "missing YAML frontmatter"),
        ("---\ndescription: D\n---\nbody", "title"),
        ("---\ntitle: T\n---\nbody", "description"),
        ("---\ntitle: T\ndescription: D\n---\n   \n", "body is empty"),
        ("---\ntitle: T\ndescription: D\nquestions: not-a-list\n---\nb", "YAML list"),
        (
            "---\ntitle: T\ndescription: D\nquestions:\n  - prompt: p?\n---\nb",
            "explicit `id`",
        ),
        (
            "---\ntitle: T\ndescription: D\nquestions:\n"
            "  - {id: a, prompt: p, kind: text}\n"
            "  - {id: a, prompt: q, kind: text}\n---\nb",
            "duplicate question id",
        ),
        (
            # the design line: NO defaults — the key is refused by name
            "---\ntitle: T\ndescription: D\nquestions:\n"
            "  - {id: a, prompt: p, kind: text, default: all}\n---\nb",
            "unsupported key",
        ),
        (
            # ask_human's own validation still applies (single needs options)
            "---\ntitle: T\ndescription: D\nquestions:\n"
            "  - {id: a, prompt: p, kind: single}\n---\nb",
            "options",
        ),
        (
            # YAML 1.1 coerces bare yes/no to booleans — the two answer
            # surfaces would stringify them differently ('True' vs 'true'),
            # so non-string options are refused at validation time.
            "---\ntitle: T\ndescription: D\nquestions:\n"
            "  - {id: a, prompt: p, kind: single, options: [yes, no]}\n---\nb",
            "must all be strings",
        ),
    ],
)
def test_parse_rejects(doc, needle):
    _, errors = parse_analysis(doc)
    assert errors and any(needle in e for e in errors)


def test_parse_rejects_oversized_document():
    _, errors = parse_analysis("x" * (an.MAX_BODY_CHARS + 1))
    assert errors and "cap" in errors[0]


# --- create + list + read ------------------------------------------------------


def test_create_list_read_round_trip(table):
    tools = _tools(table)
    out = _create(tools)
    assert out["saved"] == "churn-cohort-deep-dive" and out["version"] == 1

    listed = tools["list_analyses"].invoke({"data_domain": "sales", "dataset": "orders"})
    (row,) = listed["analyses"]
    assert row["name"] == "churn-cohort-deep-dive"
    assert row["title"] == "Churn cohort deep-dive"
    assert row["questions"] == 2
    assert row["owned_by_you"] is True and row["version"] == 1

    got = tools["read_analysis"].invoke(
        {"name": "churn-cohort-deep-dive", "data_domain": "sales", "dataset": "orders"}
    )
    assert got["document"] == DOC  # verbatim — it is the procedure
    assert got["version"] == 1 and got["owned_by_you"] is True


def test_read_shows_ownership_from_the_callers_side(table):
    _create(_tools(table))
    got = _tools(table, sub=OTHER)["read_analysis"].invoke(
        {"name": "churn-cohort-deep-dive", "data_domain": "sales", "dataset": "orders"}
    )
    assert got["owned_by_you"] is False


def test_create_duplicate_refused(table):
    tools = _tools(table)
    _create(tools)
    out = _create(tools, body=DOC.replace("deep-dive", "again"))
    assert "already exists" in out
    # the original survived untouched
    got = tools["read_analysis"].invoke(
        {"name": "churn-cohort-deep-dive", "data_domain": "sales", "dataset": "orders"}
    )
    assert got["document"] == DOC


@pytest.mark.parametrize("bad", ["", "Has Spaces", "UPPER", "-leading", "x" * 65])
def test_create_rejects_bad_slug(table, bad):
    out = _create(_tools(table), name=bad)
    assert "invalid name" in out


def test_create_invalid_doc_names_problems(table):
    out = _create(_tools(table), body="---\ntitle: T\n---\nsteps")
    assert out["error"].startswith("the document does not validate")
    assert any("description" in p for p in out["problems"])


def test_list_and_read_missing(table):
    tools = _tools(table)
    listed = tools["list_analyses"].invoke({"data_domain": "sales", "dataset": "orders"})
    assert listed["analyses"] == []
    out = tools["read_analysis"].invoke(
        {"name": "ghost", "data_domain": "sales", "dataset": "orders"}
    )
    assert "no analysis 'ghost'" in out


def test_datasets_are_isolated(table):
    tools = _tools(table)
    _create(tools)
    other = tools["list_analyses"].invoke({"data_domain": "sales", "dataset": "returns"})
    assert other["analyses"] == []


# --- update: Edit-style verbatim replacement ------------------------------------


def test_update_replaces_and_bumps_version(table):
    tools = _tools(table)
    _create(tools)
    out = tools["update_analysis"].invoke(
        {
            "name": "churn-cohort-deep-dive",
            "old_string": "A report with the churn KPI per cohort.",
            "new_string": "A report with the churn KPI per cohort and a trend chart.",
            "data_domain": "sales",
            "dataset": "orders",
        }
    )
    assert out == {"saved": "churn-cohort-deep-dive", "version": 2}
    got = tools["read_analysis"].invoke(
        {"name": "churn-cohort-deep-dive", "data_domain": "sales", "dataset": "orders"}
    )
    assert "and a trend chart" in got["document"]
    assert got["version"] == 2


def test_update_rederives_listing_fields_from_edited_frontmatter(table):
    tools = _tools(table)
    _create(tools)
    tools["update_analysis"].invoke(
        {
            "name": "churn-cohort-deep-dive",
            "old_string": "title: Churn cohort deep-dive",
            "new_string": "title: Churn cohorts",
            "data_domain": "sales",
            "dataset": "orders",
        }
    )
    (row,) = tools["list_analyses"].invoke(
        {"data_domain": "sales", "dataset": "orders"}
    )["analyses"]
    assert row["title"] == "Churn cohorts"


@pytest.mark.parametrize(
    "old,new,needle",
    [
        ("not in the doc", "x", "not found"),
        ("cohort", "cohorts", "times"),  # appears more than once
        ("## Steps", "## Steps", "identical"),
        ("", "x", "empty"),
    ],
)
def test_update_edit_contract_errors(table, old, new, needle):
    tools = _tools(table)
    _create(tools)
    out = tools["update_analysis"].invoke(
        {
            "name": "churn-cohort-deep-dive",
            "old_string": old,
            "new_string": new,
            "data_domain": "sales",
            "dataset": "orders",
        }
    )
    assert needle in out


def test_update_that_breaks_validation_saves_nothing(table):
    tools = _tools(table)
    _create(tools)
    out = tools["update_analysis"].invoke(
        {
            "name": "churn-cohort-deep-dive",
            "old_string": "description: Quantifies churn by signup cohort and isolates which segments drive it.",
            "new_string": "description:",
            "data_domain": "sales",
            "dataset": "orders",
        }
    )
    assert "invalid document" in out["error"]
    got = tools["read_analysis"].invoke(
        {"name": "churn-cohort-deep-dive", "data_domain": "sales", "dataset": "orders"}
    )
    assert got["document"] == DOC and got["version"] == 1


def test_update_by_non_owner_refused(table):
    _create(_tools(table))
    out = _tools(table, sub=OTHER)["update_analysis"].invoke(
        {
            "name": "churn-cohort-deep-dive",
            "old_string": "## Purpose",
            "new_string": "## Goal",
            "data_domain": "sales",
            "dataset": "orders",
        }
    )
    assert "only the owner" in out
    got = _tools(table)["read_analysis"].invoke(
        {"name": "churn-cohort-deep-dive", "data_domain": "sales", "dataset": "orders"}
    )
    assert got["document"] == DOC


# --- delete ---------------------------------------------------------------------
def test_pinned_scope_drops_location_args_from_schema(table):
    # The governed-tools convention: a pinned conversation removes the
    # location params from the model schema entirely. Merely overriding them
    # let the model pass a WRONG dataset that the pin silently ignored
    # server-side while surviving into the streamed args — mislabeling the
    # UI step, poisoning memory's dataset observation, and 404ing the
    # AnalysisPeek affordance.
    tools = _tools(table, scope=SCOPE)
    for tname in (
        "list_analyses",
        "read_analysis",
        "create_analysis",
        "update_analysis",
    ):
        assert "data_domain" not in tools[tname].args
        assert "dataset" not in tools[tname].args
    out = tools["create_analysis"].invoke(
        {"name": "churn-cohort-deep-dive", "body": DOC}
    )
    assert out["saved"] == "churn-cohort-deep-dive"
    # landed under the PIN
    listed = tools["list_analyses"].invoke({})
    assert listed["data_domain"] == "sales" and listed["dataset"] == "orders"
    assert len(listed["analyses"]) == 1
    got = tools["read_analysis"].invoke({"name": "churn-cohort-deep-dive"})
    assert got["document"] == DOC


def test_unscoped_schema_keeps_location_args(table):
    tools = _tools(table)
    assert "data_domain" in tools["create_analysis"].args
    assert "dataset" in tools["list_analyses"].args


def test_unscoped_without_location_is_an_error(table):
    out = _tools(table)["list_analyses"].invoke({})
    assert "name the dataset" in out


# --- wiring ----------------------------------------------------------------------


def test_analysis_tools_wire_into_the_agent_toolset():
    """Bound whenever the analyses table handle + a verified subject exist."""
    import chat.server as server
    import chat.config as chat_config_mod
    import chat.graph as chat_graph_mod
    import chat.tools as chat_tools
    from chat.config import ChatConfig
    from consumption_mcp.tools import ConsumptionConfig

    from .fakes import FakeConsumptionTools

    captured = {}

    def fake_build_graph(model, tools, checkpointer, *, system_prompt=None, middleware=None):
        captured["names"] = [t.name for t in tools]
        return object()

    cfg = ChatConfig(
        bundle_bucket="b",
        vector_bucket="v",
        vector_index="i",
        registry_table="r",
        checkpoint_table="cp",
        threads_table="th",
        catalog=[],
        sql_enabled=False,
    )
    cons_cfg = ConsumptionConfig(
        bundle_bucket="b", vector_bucket="v", vector_index="i", registry_table="r"
    )
    orig = (chat_graph_mod.build_graph, chat_config_mod.build_chat_model, chat_tools.build_consumption_tools)
    try:
        chat_graph_mod.build_graph = fake_build_graph
        chat_config_mod.build_chat_model = lambda *a, **k: object()
        chat_tools.build_consumption_tools = lambda **kw: FakeConsumptionTools()
        build_agent = server.make_agent_factory(
            cfg,
            cons_cfg,
            {
                "s3": object(),
                "s3vectors": None,
                "bedrock_runtime": None,
                "ddb": None,
                "analyses": object(),
            },
        )
        build_agent(
            "global.anthropic.claude-opus-5-5",
            "high",
            None,
            object(),
            features=set(),
            user_sub=SUB,
        )
    finally:
        chat_graph_mod.build_graph, chat_config_mod.build_chat_model, chat_tools.build_consumption_tools = orig

    for name in (
        "list_analyses",
        "read_analysis",
        "create_analysis",
        "update_analysis",
        # bound because the clients carry s3 + the cfg a bundle bucket (the
        # same deploy shape that binds the report tools publish binds to)
        "publish_report",
    ):
        assert name in captured["names"]
        # a pinned conversation must fold its scope into these tools' streamed
        # args (labels + memory's dataset observation)
        assert name in server._LOCATION_TAKING_TOOLS
    # deletion is deliberately NOT an agent capability — the human does it on
    # the Analysis page (Control API, owner-gated there)
    assert "delete_analysis" not in captured["names"]


def test_analysis_skills_ship_and_tools_point_at_them():
    from chat.skills import CATALOG

    names = {s["name"] for s in CATALOG}
    assert {"analysis-authoring", "analysis-execution"} <= names
    table = object()
    tools = {
        t.name: t for t in make_analysis_tools(table, user_sub=SUB, dataset_scope=None)
    }
    assert 'read_skill("analysis-authoring")' in tools["create_analysis"].description
    assert 'read_skill("analysis-execution")' in tools["read_analysis"].description


# --- publish_report: the run's durable artifact ----------------------------------

BUCKET = "okf-bundles"
STAMP, SUFFIX = "20260817T120000Z", "ab12cd34"


def _seed_report(*, dd="sales", ds="orders", title="Churn Q3"):
    """A stored report (blocks.json only — the publish check's target)."""
    import json

    from okf_core import reports as rp

    s3 = boto3.client("s3", region_name="us-east-1")
    try:
        s3.create_bucket(Bucket=BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    report_id = rp.make_report_id(dd, ds, STAMP, SUFFIX)
    prefix = rp.report_s3_prefix(dd, ds, STAMP, SUFFIX)
    s3.put_object(
        Bucket=BUCKET,
        Key=rp.report_blocks_key(prefix),
        Body=json.dumps({"title": title, "blocks": []}).encode(),
    )
    return s3, report_id


def _ptools(table, *, sub=SUB, scope=None, s3=None):
    return {
        t.name: t
        for t in make_analysis_tools(
            table,
            user_sub=sub,
            dataset_scope=scope,
            s3=s3,
            bundle_bucket=BUCKET if s3 is not None else "",
        )
    }


def test_publish_binds_report_and_surfaces_everywhere(table):
    s3, report_id = _seed_report()
    tools = _ptools(table, s3=s3)
    _create(tools)
    out = tools["publish_report"].invoke(
        {
            "report_id": report_id,
            "analysis": "churn-cohort-deep-dive",
            "data_domain": "sales",
            "dataset": "orders",
        }
    )
    assert out["published"] == report_id
    assert out["title"] == "Churn Q3"  # blocks.json is the title's authority
    assert "deleted" in out["note"]  # the permanence promise, stated to the model

    got = tools["read_analysis"].invoke(
        {"name": "churn-cohort-deep-dive", "data_domain": "sales", "dataset": "orders"}
    )
    (pub,) = got["published_reports"]
    assert pub["report_id"] == report_id
    assert pub["title"] == "Churn Q3"
    assert pub["analysis_version"] == 1  # the version that produced the run
    assert pub["published_by_you"] is True

    # list: the publication is a COUNT on the doc row, never a row of its own
    listed = tools["list_analyses"].invoke({"data_domain": "sales", "dataset": "orders"})
    (row,) = listed["analyses"]
    assert row["published_reports"] == 1


def test_publish_refuses_wrong_dataset_report(table):
    s3, report_id = _seed_report(dd="marketing", ds="campaigns")
    tools = _ptools(table, s3=s3)
    _create(tools)
    out = tools["publish_report"].invoke(
        {
            "report_id": report_id,
            "analysis": "churn-cohort-deep-dive",
            "data_domain": "sales",
            "dataset": "orders",
        }
    )
    assert "belongs to marketing/campaigns" in out


@pytest.mark.parametrize(
    "report_id,needle",
    [
        ("not-a-report-id", "not a report id"),
        # well-formed id whose artifacts were never stored
        ("rep~sales~orders~20260817T120000Z~ffffffff", "no readable artifacts"),
    ],
)
def test_publish_refuses_bad_or_missing_report(table, report_id, needle):
    s3, _ = _seed_report()
    tools = _ptools(table, s3=s3)
    _create(tools)
    out = tools["publish_report"].invoke(
        {
            "report_id": report_id,
            "analysis": "churn-cohort-deep-dive",
            "data_domain": "sales",
            "dataset": "orders",
        }
    )
    assert needle in out


def test_publish_requires_existing_analysis_and_refuses_duplicates(table):
    s3, report_id = _seed_report()
    tools = _ptools(table, s3=s3)
    args = {
        "report_id": report_id,
        "analysis": "churn-cohort-deep-dive",
        "data_domain": "sales",
        "dataset": "orders",
    }
    out = tools["publish_report"].invoke(dict(args))
    assert "no analysis" in out
    _create(tools)
    assert tools["publish_report"].invoke(dict(args))["published"] == report_id
    again = tools["publish_report"].invoke(dict(args))
    assert "already published" in again

def test_publish_tool_absent_without_report_storage(table):
    # No s3/bucket (a deploy without the report tools) => no publish tool,
    # but the quartet stays intact (no delete tool by design — the human
    # deletes on the Analysis page).
    tools = _tools(table)
    assert "publish_report" not in tools
    assert set(tools) == {
        "list_analyses",
        "read_analysis",
        "create_analysis",
        "update_analysis",
    }


def test_publish_refuses_when_analysis_deleted_between_read_and_write(table):
    # The transactional guard: the ConditionCheck on the DOCUMENT row must
    # refuse a publish whose analysis vanished after the ownership read —
    # the plain read-then-put minted an orphan publication row no surface
    # could ever remove.
    s3, report_id = _seed_report()
    tools = _ptools(table, s3=s3)
    _create(tools)

    class RacingTable:
        """Delegates to the real Table, but deletes the analysis document
        right before the publish transaction commits."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        @property
        def meta(self):
            outer = self

            class _Meta:
                @property
                def client(self):
                    inner_client = outer._inner.meta.client

                    class _Client:
                        def transact_write_items(self, **kwargs):
                            outer._inner.delete_item(
                                Key={
                                    "pk": "ANALYSIS#sales#orders",
                                    "sk": "churn-cohort-deep-dive",
                                }
                            )
                            return inner_client.transact_write_items(**kwargs)

                        def __getattr__(self, name):
                            return getattr(inner_client, name)

                    return _Client()

            return _Meta()

    racing = {
        t.name: t
        for t in make_analysis_tools(
            RacingTable(table), user_sub=SUB, dataset_scope=None,
            s3=s3, bundle_bucket=BUCKET,
        )
    }
    out = racing["publish_report"].invoke(
        {
            "report_id": report_id,
            "analysis": "churn-cohort-deep-dive",
            "data_domain": "sales",
            "dataset": "orders",
        }
    )
    assert "deleted while publishing" in out or "changed underneath" in out
    # nothing was recorded — the partition holds no publication row
    from boto3.dynamodb.conditions import Key

    resp = table.query(
        KeyConditionExpression=Key("pk").eq(an.analysis_pk("sales", "orders"))
    )
    assert resp["Items"] == []
