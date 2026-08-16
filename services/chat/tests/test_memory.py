"""Long-term chat memory: record parsing, TTL, scoping, observation, and the
event write — everything the runtime owns (extraction/consolidation belong to
the managed service and are configured in Terraform, not tested here).

The bedrock-agentcore data plane is an injected fake throughout, per the
repo's offline-test contract.
"""

from __future__ import annotations

import json
from datetime import date

from chat.memory import (
    ANNOTATION_PREFIX,
    INJECT_MAX,
    MEMORY_MARKER,
    ChatMemory,
    TurnObservation,
    is_expired,
    make_chat_memory,
    parse_record,
)


def _record(text, record_id="mem-1"):
    return {"memoryRecordId": record_id, "content": {"text": text}}


# --- header parsing -----------------------------------------------------------


def test_parse_record_full_header():
    p = parse_record(
        _record(
            "[type:binding] [dataset:motorsport/f1] [expires:2026-09-30]\n"
            "『group performance』 resolves via computation group_monthly_performance, "
            "latest full month vs the month before."
        )
    )
    assert p["type"] == "binding"
    assert p["dataset"] == "motorsport/f1"
    assert p["expires"] == "2026-09-30"
    assert p["text"].startswith("『group performance』")
    assert "[type:" not in p["text"]


def test_parse_record_dashes_mean_unset():
    p = parse_record(_record("[type:stated] [dataset:-] [expires:-]\nPrefers tables."))
    assert p == {
        "id": "mem-1",
        "type": "stated",
        "dataset": "",
        "expires": "",
        "text": "Prefers tables.",
        # Header-sourced type: a server-side type filter can NOT match this.
        "type_from_metadata": False,
    }


def test_parse_record_without_header_degrades_to_generic_stated():
    # Extraction models drift — a headerless record must stay usable, not error.
    p = parse_record(_record("Answers should be short."))
    assert p["type"] == "stated" and p["dataset"] == "" and p["expires"] == ""
    assert p["text"] == "Answers should be short."


# --- TTL ------------------------------------------------------------------------


def test_is_expired_past_future_and_absent():
    today = date(2026, 10, 1)
    assert is_expired({"expires": "2026-09-30"}, today=today) is True
    assert is_expired({"expires": "2026-10-01"}, today=today) is False  # inclusive
    assert is_expired({"expires": ""}, today=today) is False


def test_is_expired_tolerates_a_mangled_date():
    # A bad header must not silently destroy the memory — it loses its window.
    assert is_expired({"expires": "next month"}) is False


# --- turn observation ------------------------------------------------------------


def _tool_start(name, args, call_id="c1"):
    return {"type": "tool", "id": call_id, "tool_name": name, "tool_start": True, "content": args}


def _tool_result(name, call_id="c1", error=False):
    return {"type": "tool", "id": call_id, "tool_name": name, "tool_start": False, "content": "ok", "error": error}


def test_observation_collects_datasets_and_governed_success():
    obs = TurnObservation()
    obs.observe(_tool_start("read_page", {"data_domain": "sports", "dataset": "f1", "concept_id": "tables/races"}, "a"))
    obs.observe(_tool_result("read_page", "a"))
    obs.observe(
        _tool_start(
            "run_computation",
            {"data_domain": "sports", "dataset": "f1", "name": "laps_by_season", "season": 2026},
            "b",
        )
    )
    obs.observe(_tool_result("run_computation", "b"))
    note = obs.annotation()
    assert note.startswith(ANNOTATION_PREFIX)
    assert '"sports/f1"' in note
    assert "resolved-by: run_computation slug=laps_by_season dataset=sports/f1" in note
    assert '"season": 2026' in note


def test_observation_drops_errored_governed_calls_and_ignores_other_chunks():
    obs = TurnObservation()
    obs.observe({"type": "text", "content": "hello"})
    obs.observe(_tool_start("run_computation", {"name": "x"}, "b"))
    obs.observe(_tool_result("run_computation", "b", error=True))
    assert obs.governed == []
    assert obs.annotation() == ""  # nothing observed → no annotation block


# --- recall ------------------------------------------------------------------------


class FakeAgentCore:
    def __init__(self, records=None, fail=False):
        self.records = records or []
        self.fail = fail
        self.deleted: list[str] = []
        self.events: list[dict] = []

    def retrieve_memory_records(self, **kwargs):
        if self.fail:
            raise RuntimeError("boom")
        self.retrieve_kwargs = kwargs
        self.retrieve_calls = getattr(self, "retrieve_calls", [])
        self.retrieve_calls.append(kwargs)
        return {"memoryRecordSummaries": self.records}

    def delete_memory_record(self, **kwargs):
        self.deleted.append(kwargs["memoryRecordId"])
        return {}

    def create_event(self, **kwargs):
        if self.fail:
            raise RuntimeError("boom")
        self.events.append(kwargs)
        return {"event": {"eventId": "ev-1"}}


def _memory(records=None, fail=False, ddb=None):
    return (
        ChatMemory(
            FakeAgentCore(records, fail),
            memory_id="mem-abc",
            ddb=ddb,
            threads_table="okf-chat",
        ),
        None,
    )[0]


def test_recall_filters_expired_and_lazy_deletes():
    mem = _memory(
        [
            _record("[type:stated] [dataset:-] [expires:1999-01-01]\nOld fact.", "mem-old"),
            _record("[type:stated] [dataset:-] [expires:-]\nPrefers tables.", "mem-keep"),
        ]
    )
    kept = mem.recall(user_sub="u1", query="how are things")
    assert [r["id"] for r in kept] == ["mem-keep"]
    assert mem._client.deleted == ["mem-old"]


def test_recall_pinned_scope_keeps_generic_plus_pin_only():
    mem = _memory(
        [
            _record("[type:binding] [dataset:sports/f1] [expires:-]\nF1 binding.", "m1"),
            _record("[type:binding] [dataset:retail/eu] [expires:-]\nOther dataset.", "m2"),
            _record("[type:stated] [dataset:-] [expires:-]\nGeneric pref.", "m3"),
        ]
    )
    kept = mem.recall(
        user_sub="u1",
        query="group performance",
        dataset_scope={"data_domain": "sports", "dataset": "f1"},
    )
    assert [r["id"] for r in kept] == ["m1", "m3"]


def test_recall_unpinned_keeps_all_datasets():
    mem = _memory(
        [
            _record("[type:binding] [dataset:sports/f1] [expires:-]\nF1 binding.", "m1"),
            _record("[type:binding] [dataset:retail/eu] [expires:-]\nEU binding.", "m2"),
        ]
    )
    kept = mem.recall(user_sub="u1", query="group performance")
    assert [r["id"] for r in kept] == ["m1", "m2"]


def test_recall_caps_at_inject_max_and_namespaces_by_user():
    mem = _memory(
        [_record(f"[type:stated] [dataset:-] [expires:-]\nP{i}.", f"m{i}") for i in range(20)]
    )
    kept = mem.recall(user_sub="u1", query="q")
    assert len(kept) == INJECT_MAX
    assert mem._client.retrieve_kwargs["namespace"] == "wiki/u1"


def test_recall_failure_degrades_to_no_memories():
    mem = _memory(fail=True)
    assert mem.recall(user_sub="u1", query="q") == []


# --- injection --------------------------------------------------------------------


def test_injection_message_carries_marker_and_validation_framing():
    mem = _memory()
    msg = mem.injection_message(
        [
            {"type": "binding", "dataset": "sports/f1", "expires": "", "text": "Binding.", "id": "m1"},
            {"type": "stated", "dataset": "", "expires": "2026-12-31", "text": "Pref.", "id": "m2"},
        ]
    )
    assert msg.additional_kwargs[MEMORY_MARKER] == "recall"
    body = msg.content
    assert "<system-reminder>" in body and "</system-reminder>" in body
    assert "VERIFIED" in body  # validation-at-use is spelled out to the model
    assert "(binding [dataset sports/f1]) Binding." in body
    assert "(preference [valid until 2026-12-31]) Pref." in body


def test_memory_marker_is_pinned_in_steering_injected_keys():
    from chat.steering import _INJECTED_MARKER_KEYS

    assert MEMORY_MARKER in _INJECTED_MARKER_KEYS


# --- per-user switch --------------------------------------------------------------


class FakeDdb:
    def __init__(self, item=None, fail=False):
        self.item = item
        self.fail = fail
        self.updates: list[dict] = []

    def get_item(self, **kwargs):
        if self.fail:
            raise RuntimeError("boom")
        self.key = kwargs["Key"]
        return {"Item": self.item} if self.item is not None else {}

    def update_item(self, **kwargs):
        if self.fail:
            raise RuntimeError("boom")
        self.updates.append(kwargs)
        return {}


def test_user_enabled_defaults_on_when_row_missing_or_unreadable():
    assert _memory(ddb=FakeDdb()).user_enabled("u1") is True
    assert _memory(ddb=FakeDdb(fail=True)).user_enabled("u1") is True
    assert _memory(ddb=None).user_enabled("u1") is True


def test_user_enabled_reads_the_settings_row():
    ddb = FakeDdb({"memory_enabled": {"BOOL": False}})
    mem = _memory(ddb=ddb)
    assert mem.user_enabled("u1") is False
    assert ddb.key == {"pk": {"S": "CHAT#u1"}, "sk": {"S": "SETTINGS#memory"}}


# --- the turn event write -----------------------------------------------------------


def test_write_turn_payload_shape_and_annotation():
    mem = _memory()
    mem.write_turn(
        user_sub="u1",
        session_id="u1:t1",
        user_text="how did the group perform?",
        answer_text="a" * 10_000,
        observation={
            "datasets": [],
            "governed": [
                {"tool": "run_computation", "slug": "x", "params": {}, "dataset": ""}
            ],
        },
    )
    (event,) = mem._client.events
    assert event["memoryId"] == "mem-abc"
    # ids are SANITIZED to CreateEvent's [a-zA-Z0-9][a-zA-Z0-9-_]* pattern —
    # the internal thread id's colon failed every write live.
    assert event["actorId"] == "u1" and event["sessionId"] == "u1_t1"
    roles = [p["conversational"]["role"] for p in event["payload"]]
    assert roles == ["USER", "ASSISTANT", "ASSISTANT"]
    assert len(event["payload"][1]["conversational"]["content"]["text"]) == 4000
    assert event["payload"][2]["conversational"]["content"]["text"].startswith(ANNOTATION_PREFIX)


def test_write_turn_failure_is_swallowed():
    mem = _memory(fail=True)
    mem.write_turn(user_sub="u1", session_id="s", user_text="q", answer_text="a")


# --- factory gating ------------------------------------------------------------------


class _Cfg:
    memory_id = "mem-abc"
    threads_table = "okf-chat"


def test_make_chat_memory_requires_id_and_client():
    assert make_chat_memory(_Cfg(), {"agentcore_memory": FakeAgentCore()}) is not None
    assert make_chat_memory(_Cfg(), {}) is None

    class _Off:
        memory_id = ""
        threads_table = "okf-chat"

    assert make_chat_memory(_Off(), {"agentcore_memory": FakeAgentCore()}) is None


# --- real record metadata (awscc metadata_schema) -----------------------------------


def test_parse_record_prefers_real_metadata_over_header():
    p = parse_record(
        {
            "memoryRecordId": "mem-9",
            "content": {"text": "Prefers charts."},
            "metadata": {
                "type": {"stringValue": "stated"},
                "dataset": {"stringValue": "sports/f1"},
                "expires_at": {"stringValue": "2026-12-31"},
            },
        }
    )
    assert p["type"] == "stated"
    assert p["dataset"] == "sports/f1"
    assert p["expires"] == "2026-12-31"
    assert p["text"] == "Prefers charts."


def test_recall_pinned_issues_generic_plus_pin_filtered_calls():
    mem = _memory(
        [_record("[type:stated] [dataset:-] [expires:-]\nGeneric pref.", "m3")]
    )
    mem.recall(
        user_sub="u1",
        query="q",
        dataset_scope={"data_domain": "sports", "dataset": "f1"},
    )
    calls = mem._client.retrieve_calls
    assert len(calls) == 2
    ops = [c["searchCriteria"]["metadataFilters"][0]["operator"] for c in calls]
    assert ops == ["NOT_EXISTS", "EQUALS_TO"]
    eq = calls[1]["searchCriteria"]["metadataFilters"][0]
    assert eq["right"]["metadataValue"]["stringValue"] == "sports/f1"


def test_recall_unpinned_issues_one_unfiltered_call():
    mem = _memory([_record("[type:stated] [dataset:-] [expires:-]\nP.", "m1")])
    mem.recall(user_sub="u1", query="q")
    calls = mem._client.retrieve_calls
    assert len(calls) == 1
    assert "metadataFilters" not in calls[0]["searchCriteria"]


def test_write_turn_interleaves_clarification_qa():
    mem = _memory()
    mem.write_turn(
        user_sub="u1",
        session_id="s",
        user_text="how did the group perform?",
        answer_text="answer",
        clarifications=[
            {"prompt": "Which group?", "answer": "EMEA, always"},
            {"prompt": "Which period?", "answer": "last month"},
        ],
    )
    (event,) = mem._client.events
    texts = [p["conversational"]["content"]["text"] for p in event["payload"]]
    roles = [p["conversational"]["role"] for p in event["payload"]]
    assert roles == ["USER", "ASSISTANT", "USER", "ASSISTANT", "USER", "ASSISTANT"]
    assert texts[1] == "(clarifying question) Which group?"
    assert texts[2] == "EMEA, always"
    assert texts[-1] == "answer"


# --- factory regression: memory clients must never reach the tools splat ------------


def test_factory_pops_memory_clients_before_consumption_splat():
    """make_agent_factory passes **clients to build_consumption_tools, which has
    a STRICT signature — the memory-only keys (agentcore_memory, dynamodb) must
    be popped first. Missing this crashed the deployed runtime at boot with
    "unexpected keyword argument 'agentcore_memory'"; the old test stubbed
    build_consumption_tools with a **kw lambda, which is why it never caught
    it — this one stubs with the strict signature."""
    import chat.server as server
    import chat.tools as chat_tools
    import chat.config as chat_config_mod
    import chat.graph as chat_graph_mod
    from chat.config import ChatConfig
    from consumption_mcp.tools import ConsumptionConfig

    from .fakes import FakeConsumptionTools

    cfg = ChatConfig(
        bundle_bucket="b", vector_bucket="v", vector_index="i", registry_table="r",
        checkpoint_table="cp", threads_table="th", catalog=[], sql_enabled=False,
        memory_id="mem-abc",
    )
    cons_cfg = ConsumptionConfig(
        bundle_bucket="b", vector_bucket="v", vector_index="i", registry_table="r"
    )

    def strict_bct(*, config, athena=None, redshift_data=None, s3=None,
                   s3vectors=None, bedrock_runtime=None, ddb=None):
        return FakeConsumptionTools()

    orig_bct = chat_tools.build_consumption_tools
    orig_graph = chat_graph_mod.build_graph
    orig_model = chat_config_mod.build_chat_model
    try:
        chat_tools.build_consumption_tools = strict_bct
        chat_graph_mod.build_graph = lambda *a, **k: object()
        chat_config_mod.build_chat_model = lambda *a, **k: object()
        clients = {
            "s3": None, "s3vectors": None, "bedrock_runtime": None, "ddb": None,
            # The two memory-only clients build_deps adds when memory_id is set:
            "agentcore_memory": object(), "dynamodb": object(),
        }
        build_agent = server.make_agent_factory(cfg, cons_cfg, clients)
        build_agent("us.anthropic.claude-opus-4-8", "high", None, object(), features=set())
    finally:
        chat_tools.build_consumption_tools = orig_bct
        chat_graph_mod.build_graph = orig_graph
        chat_config_mod.build_chat_model = orig_model


def test_write_turn_sanitizes_ids_to_the_event_pattern():
    from chat.memory import _safe_id

    assert _safe_id("sub-123:thread.9") == "sub-123_thread_9"
    assert _safe_id("_leading") == "m_leading"
    assert _safe_id("") == "m"


def test_parse_record_unwraps_builtin_json_output_shape():
    # The managed pipeline's fixed output schema (found live): content is
    # {"context", "preference", "categories"} JSON; structure lives in record
    # METADATA. The parser surfaces the human sentence.
    p = parse_record(
        {
            "memoryRecordId": "mem-j",
            "content": {
                "text": '{"context":"ctx.","preference":"Wants tables and short answers","categories":["presentation"]}'
            },
            "metadata": {"type": {"stringValue": "stated"}},
        }
    )
    assert p["text"] == "Wants tables and short answers"
    assert p["type"] == "stated" and p["dataset"] == "" and p["expires"] == ""


def test_parse_record_reads_content_embedded_metadata_as_fallback():
    # Observed live: the extractor sometimes duplicates metadata INSIDE the
    # content JSON. When real record metadata is absent, use it; real record
    # metadata still wins when both exist.
    p = parse_record(
        {
            "memoryRecordId": "mem-k",
            "content": {
                "text": '{"preference":"Mph for today","metadata":{"type":"stated","expires_at":"2026-08-16"}}'
            },
        }
    )
    assert p["text"] == "Mph for today"
    assert p["expires"] == "2026-08-16"

    p2 = parse_record(
        {
            "memoryRecordId": "mem-l",
            "content": {
                "text": '{"preference":"Mph","metadata":{"expires_at":"2026-08-16"}}'
            },
            "metadata": {"expires_at": {"stringValue": "2026-09-30"}},
        }
    )
    assert p2["expires"] == "2026-09-30"  # real record metadata wins


# --- personal context: once-per-session path -----------------------------------------


def test_recall_excludes_personal_records():
    mem = _memory(
        [
            {"memoryRecordId": "mp", "content": {"text": "User name is Edvin"},
             "metadata": {"type": {"stringValue": "personal"}}},
            _record("[type:stated] [dataset:-] [expires:-]\nPrefers tables.", "ms"),
        ]
    )
    kept = mem.recall(user_sub="u1", query="q")
    assert [r["id"] for r in kept] == ["ms"]


def test_recall_personal_uses_type_filter_and_parses():
    mem = _memory(
        [
            {"memoryRecordId": "mp", "content": {"text": "User name is Edvin"},
             "metadata": {"type": {"stringValue": "personal"}}},
        ]
    )
    kept = mem.recall_personal(user_sub="u1")
    assert [r["id"] for r in kept] == ["mp"] and kept[0]["type"] == "personal"
    f = mem._client.retrieve_kwargs["searchCriteria"]["metadataFilters"][0]
    assert f["operator"] == "EQUALS_TO"
    assert f["right"]["metadataValue"]["stringValue"] == "personal"


def test_injection_labels_personal_records():
    mem = _memory()
    msg = mem.injection_message(
        [{"type": "personal", "dataset": "", "expires": "", "text": "Name: Edvin", "id": "mp"}]
    )
    assert "(about the user) Name: Edvin" in msg.content


# --- review fixes: breadth, interleave, fallback-personal, caps, snapshots -----------


class FakePagedAgentCore(FakeAgentCore):
    """One response list per retrieve call — recall's pinned path issues TWO
    calls whose results must be distinguishable (the shared-list base fake
    dedupes the second call into nothing)."""

    def __init__(self, responses):
        super().__init__(records=None)
        self.responses = list(responses)

    def retrieve_memory_records(self, **kwargs):
        self.retrieve_kwargs = kwargs
        self.retrieve_calls = getattr(self, "retrieve_calls", [])
        self.retrieve_calls.append(kwargs)
        recs = self.responses.pop(0) if self.responses else []
        return {"memoryRecordSummaries": recs}


def test_recall_passes_explicit_max_results():
    # The API's DEFAULT page is 20 — topK=25 without maxResults silently
    # retrieved only 20 (records 21-25 never seen, expired ones never reaped).
    from chat.memory import TOP_K

    mem = _memory([_record("[type:stated] [dataset:-] [expires:-]\nP.", "m1")])
    mem.recall(user_sub="u1", query="q")
    assert mem._client.retrieve_kwargs["maxResults"] == TOP_K
    mem.recall_personal(user_sub="u1")
    assert mem._client.retrieve_kwargs["maxResults"] == INJECT_MAX


def test_recall_pinned_interleaves_so_generics_cannot_starve_pin_records():
    generics = [
        _record(f"[type:stated] [dataset:-] [expires:-]\nG{i}.", f"g{i}")
        for i in range(INJECT_MAX + 4)
    ]
    pins = [
        _record("[type:binding] [dataset:sports/f1] [expires:-]\nF1 binding.", "p1"),
        _record("[type:stated] [dataset:sports/f1] [expires:-]\nF1 pref.", "p2"),
    ]
    client = FakePagedAgentCore([generics, pins])
    mem = ChatMemory(client, memory_id="mem-abc")
    kept = mem.recall(
        user_sub="u1",
        query="q",
        dataset_scope={"data_domain": "sports", "dataset": "f1"},
    )
    ids = [r["id"] for r in kept]
    assert len(ids) == INJECT_MAX
    # Dataset records lead the interleave — a pile of generic preferences
    # must never push the pin's records (the most conversation-relevant
    # ones) out of the injection budget.
    assert ids[0] == "p1" and "p2" in ids


def test_recall_keeps_fallback_typed_personal_records():
    # A personal record WITHOUT real metadata can't match recall_personal's
    # server-side type filter — skipping it in per-turn recall too would make
    # it invisible to EVERY recall path. It degrades to per-turn injection.
    mem = _memory(
        [
            _record("[type:personal] [dataset:-] [expires:-]\nName: Edvin.", "fallback"),
            {"memoryRecordId": "real", "content": {"text": "Role: analyst"},
             "metadata": {"type": {"stringValue": "personal"}}},
        ]
    )
    kept = mem.recall(user_sub="u1", query="q")
    assert [r["id"] for r in kept] == ["fallback"]


def test_recall_personal_lazy_deletes_expired():
    mem = _memory(
        [
            {"memoryRecordId": "gone", "content": {"text": "Temp fact"},
             "metadata": {"type": {"stringValue": "personal"},
                          "expires_at": {"stringValue": "1999-01-01"}}},
        ]
    )
    assert mem.recall_personal(user_sub="u1") == []
    assert mem._client.deleted == ["gone"]


def test_recall_namespace_derives_from_sanitized_actor_id():
    # The strategy resolves {actorId} from CreateEvent's SANITIZED actorId —
    # recall against the raw sub would read a namespace nothing writes to.
    mem = _memory([])
    mem.recall(user_sub="sub:with:colons", query="q")
    assert mem._client.retrieve_kwargs["namespace"] == "wiki/sub_with_colons"


def test_injection_message_marker_value_is_the_lifecycle():
    mem = _memory()
    rec = [{"type": "personal", "dataset": "", "expires": "", "text": "N.", "id": "m"}]
    assert mem.injection_message(rec).additional_kwargs[MEMORY_MARKER] == "recall"
    assert (
        mem.injection_message(rec, marker="personal").additional_kwargs[MEMORY_MARKER]
        == "personal"
    )


def test_write_turn_caps_every_piece_not_just_the_answer():
    from chat.memory import ANSWER_EXCERPT_CHARS

    mem = _memory()
    mem.write_turn(
        user_sub="u1",
        session_id="s1",
        user_text="U" * (ANSWER_EXCERPT_CHARS + 500),
        answer_text="A" * (ANSWER_EXCERPT_CHARS + 500),
        clarifications=[
            {
                "prompt": "P" * (ANSWER_EXCERPT_CHARS + 500),
                "answer": "R" * (ANSWER_EXCERPT_CHARS + 500),
            }
        ],
    )
    payload = mem._client.events[0]["payload"]
    texts = [p["conversational"]["content"]["text"] for p in payload]
    prefix = len("(clarifying question) ")
    assert all(len(t) <= ANSWER_EXCERPT_CHARS + prefix for t in texts)
    assert texts[0] == "U" * ANSWER_EXCERPT_CHARS


# --- citation-first dataset resolution (level 1: cited, level 2: touched) -----------


def _event_annotation(mem):
    """The annotation message of the last written event ('' when absent)."""
    texts = [
        p["conversational"]["content"]["text"]
        for p in mem._client.events[-1]["payload"]
    ]
    return texts[-1] if texts[-1].startswith(ANNOTATION_PREFIX) else ""


def test_cited_datasets_parses_wiki_addresses_only():
    from chat.memory import _cited_datasets

    text = (
        'A <c src="bird/formula_1/tables/results,bird/formula_1/references/joins/x"></c> '
        'B <c src="https://example.com/article"></c> '
        'C <c src="tables/races"></c> '  # bare concept id: no dataset to take
        'D <c src="retail/eu/tables/orders"></c>'
    )
    assert _cited_datasets(text) == ["bird/formula_1", "retail/eu"]


def test_write_turn_valid_citation_replaces_touched_list():
    # Level 1 is EITHER/OR with level 2: attribution beats exploration — the
    # extractor must not be offered the two datasets the turn merely grepped.
    mem = _memory()
    mem.write_turn(
        user_sub="u1",
        session_id="s",
        user_text="most successful driver?",
        answer_text=(
            "Schumacher leads on titles "
            '<c src="bird/formula_1/references/metrics/end_of_season_standings"></c>.'
        ),
        observation={
            "datasets": ["bird/formula_1", "retail/eu", "hr/people"],
            "governed": [],
        },
    )
    note = _event_annotation(mem)
    assert 'datasets-cited: ["bird/formula_1"]' in note
    assert "datasets-touched" not in note and "retail/eu" not in note


def test_write_turn_unbacked_citation_falls_back_to_touched():
    # A citation the harness never observed is a model claim — it must not
    # be laundered into the trusted block; level 2 takes over wholesale.
    mem = _memory()
    mem.write_turn(
        user_sub="u1",
        session_id="s",
        user_text="q",
        answer_text='X <c src="made/up/tables/x"></c>.',
        observation={"datasets": ["retail/eu"], "governed": []},
    )
    note = _event_annotation(mem)
    assert 'datasets-touched: ["retail/eu"]' in note
    assert "datasets-cited" not in note and "made/up" not in note


def test_write_turn_pin_corroborates_citation_on_a_no_tool_turn():
    mem = _memory()
    mem.write_turn(
        user_sub="u1",
        session_id="s",
        user_text="and hamilton?",
        answer_text='Second on titles <c src="bird/formula_1/tables/results"></c>.',
        observation={"datasets": [], "governed": []},
        pin="bird/formula_1",
    )
    assert 'datasets-cited: ["bird/formula_1"]' in _event_annotation(mem)


def test_write_turn_thread_ledger_corroborates_citation_across_turns():
    # THE motivating case: a follow-up answered from context (no tool calls)
    # still cites the docs it draws on; the thread's cumulative ledger of
    # observed datasets is what lets that citation count.
    ddb = FakeDdb(item={"memory_datasets": {"S": json.dumps(["bird/formula_1"])}})
    mem = _memory(ddb=ddb)
    mem.write_turn(
        user_sub="u1",
        session_id="s",
        thread_id="t1",
        user_text="and in the rain?",
        answer_text='Still Schumacher <c src="bird/formula_1/tables/results"></c>.',
        observation={"datasets": [], "governed": []},
    )
    note = _event_annotation(mem)
    assert 'datasets-cited: ["bird/formula_1"]' in note
    assert "datasets-touched" not in note


def test_write_turn_merges_observed_datasets_into_the_ledger():
    ddb = FakeDdb(item={"memory_datasets": {"S": json.dumps(["old/one"])}})
    mem = _memory(ddb=ddb)
    mem.write_turn(
        user_sub="u1",
        session_id="s",
        thread_id="t1",
        user_text="q",
        answer_text="plain answer, no citations",
        observation={"datasets": ["bird/formula_1"], "governed": []},
    )
    (update,) = ddb.updates
    assert update["Key"]["sk"] == {"S": "THREAD#t1"}
    merged = json.loads(update["ExpressionAttributeValues"][":md"]["S"])
    assert merged == ["old/one", "bird/formula_1"]


def test_write_turn_carries_curated_question_in_the_annotation():
    # Extraction is async — an elliptical turn must be self-contained, so the
    # harness's context-resolved question rides the trusted block (the raw
    # wording stays the USER message: meanings extract from the user's words).
    mem = _memory()
    mem.write_turn(
        user_sub="u1",
        session_id="s",
        user_text="and last month?",
        answer_text="Down 3%.",
        observation={"datasets": [], "governed": []},
        curated_question="How did EMEA group revenue in July 2026 compare to June?",
    )
    note = _event_annotation(mem)
    assert note.startswith(ANNOTATION_PREFIX)
    assert (
        "curated-question: How did EMEA group revenue in July 2026 compare to June?"
        in note
    )
    # The raw user text is still the USER message, untouched.
    texts = [
        p["conversational"]["content"]["text"]
        for p in mem._client.events[-1]["payload"]
    ]
    assert texts[0] == "and last month?"


def test_write_turn_drops_curated_question_identical_to_raw():
    # _curated_now() falls back to the RAW question when the rewrite hasn't
    # landed — identical text adds nothing over the USER message, so no
    # annotation line (and with nothing else observed, no annotation at all).
    mem = _memory()
    mem.write_turn(
        user_sub="u1",
        session_id="s",
        user_text="how did the group perform?",
        answer_text="Fine.",
        observation={"datasets": [], "governed": []},
        curated_question="how did the group perform?",
    )
    assert _event_annotation(mem) == ""


def test_write_turn_resolved_by_rides_regardless_of_citation_level():
    # Bindings stay observation-only evidence: citations are docs-only and
    # must never suppress (or mint) a resolved-by line.
    mem = _memory()
    mem.write_turn(
        user_sub="u1",
        session_id="s",
        user_text="q",
        answer_text='Answer <c src="bird/formula_1/tables/results"></c>.',
        observation={
            "datasets": ["bird/formula_1"],
            "governed": [
                {
                    "tool": "run_computation",
                    "slug": "laps",
                    "params": {"season": 2026},
                    "dataset": "bird/formula_1",
                }
            ],
        },
    )
    note = _event_annotation(mem)
    assert 'datasets-cited: ["bird/formula_1"]' in note
    assert "resolved-by: run_computation slug=laps dataset=bird/formula_1" in note


def test_observation_snapshot_restore_round_trip():
    obs = TurnObservation()
    obs.observe(
        _tool_start(
            "run_computation",
            {"data_domain": "sports", "dataset": "f1", "name": "laps"},
            "a",
        )
    )
    obs.observe(_tool_result("run_computation", "a"))
    resumed = TurnObservation()
    resumed.restore(obs.snapshot())
    assert resumed.datasets == ["sports/f1"]
    assert resumed.annotation() == obs.annotation()
    resumed.restore(None)  # tolerant of a missing blob
    resumed.restore({"datasets": ["sports/f1"]})  # dedupes on replay
    assert resumed.datasets == ["sports/f1"]
