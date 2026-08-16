"""Server-side long-term-memory wiring: the turn-start strip/reinject, the
resume-path pause-blob restore, and the governed-tool scope fold — the pieces
``_produce_run_chunks`` delegates to (each SYNC, driven via asyncio.to_thread
in the server so a memory brownout can't stall the event loop).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from langchain_core.messages import HumanMessage, RemoveMessage

from chat.memory import MEMORY_MARKER, ChatMemory
from chat.server import (
    _LOCATION_TAKING_TOOLS,
    _memory_persist_pending,
    _memory_resume_start,
    _memory_turn_start,
    _with_scope,
)

_CFG = {"configurable": {"thread_id": "u1:t1"}}
_CHAT_CONFIG = SimpleNamespace(threads_table="okf-chat")


class _StubGraph:
    def __init__(self, messages):
        self._messages = messages

    def get_state(self, cfg):
        return SimpleNamespace(values={"messages": self._messages})


class _StubDdb:
    """Low-level DDB fake: canned get_item item + recorded update_item calls."""

    def __init__(self, item=None):
        self.item = item
        self.updates: list[dict] = []

    def get_item(self, **kwargs):
        return {"Item": self.item} if self.item is not None else {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


class _StubMemory:
    injection_message = staticmethod(ChatMemory.injection_message)

    def __init__(self, enabled=True, personal=None, recalled=None, ddb=None):
        self._enabled = enabled
        self._personal = personal or []
        self._recalled = recalled or []
        self._ddb = ddb
        self.recall_calls: list[dict] = []

    def user_enabled(self, user_sub):
        return self._enabled

    def recall_personal(self, *, user_sub):
        return list(self._personal)

    def recall(self, *, user_sub, query, dataset_scope=None):
        self.recall_calls.append({"query": query, "scope": dataset_scope})
        return list(self._recalled)


def _rec(text, type="stated"):
    return {"id": "m1", "type": type, "dataset": "", "expires": "", "text": text}


def _marked(marker, msg_id):
    return HumanMessage(
        content="<system-reminder>old</system-reminder>",
        additional_kwargs={MEMORY_MARKER: marker},
        id=msg_id,
    )


# --- _memory_turn_start ---------------------------------------------------------


def test_turn_start_strips_previous_recall_injection_even_when_disabled():
    # Retraction contract: switching memory OFF (or deleting records) must
    # also silence context injected on EARLIER turns of this thread.
    graph = _StubGraph(
        [
            HumanMessage(content="hi", id="h1"),
            _marked("recall", "r1"),
            _marked("personal", "p1"),  # personal is never stripped
        ]
    )
    enabled, msgs = _memory_turn_start(
        _StubMemory(enabled=False), graph, _CFG, _CHAT_CONFIG, "u1", "t1", "q", None
    )
    assert enabled is False
    assert [type(m) for m in msgs] == [RemoveMessage]
    assert msgs[0].id == "r1"


def test_turn_start_first_turn_injects_personal_then_recall():
    memory = _StubMemory(personal=[_rec("Name: Edvin", "personal")], recalled=[_rec("Tables.")])
    enabled, msgs = _memory_turn_start(
        memory, _StubGraph([]), _CFG, _CHAT_CONFIG, "u1", "t1", "hello", None
    )
    assert enabled is True
    markers = [m.additional_kwargs[MEMORY_MARKER] for m in msgs]
    assert markers == ["personal", "recall"]


def test_turn_start_later_turns_replace_recall_and_skip_personal():
    memory = _StubMemory(personal=[_rec("Name: Edvin", "personal")], recalled=[_rec("Tables.")])
    graph = _StubGraph([HumanMessage(content="hi", id="h1"), _marked("recall", "r1")])
    enabled, msgs = _memory_turn_start(
        memory, graph, _CFG, _CHAT_CONFIG, "u1", "t1", "and last month?", None
    )
    assert enabled is True
    assert isinstance(msgs[0], RemoveMessage) and msgs[0].id == "r1"
    markers = [
        m.additional_kwargs[MEMORY_MARKER]
        for m in msgs
        if not isinstance(m, RemoveMessage)
    ]
    assert markers == ["recall"]  # personal only rides the FIRST turn
    assert memory.recall_calls[0]["query"] == "and last month?"


def test_turn_start_scope_reaches_recall():
    memory = _StubMemory(recalled=[])
    scope = {"data_domain": "sports", "dataset": "f1"}
    _memory_turn_start(
        memory, _StubGraph([]), _CFG, _CHAT_CONFIG, "u1", "t1", "q", scope
    )
    assert memory.recall_calls[0]["scope"] == scope


# --- resume: the pause blob -------------------------------------------------------


def test_resume_start_reads_pending_blob():
    blob = {"obs": {"datasets": ["sports/f1"], "governed": []}, "qa": [{"prompt": "P?", "answer": "A"}]}
    ddb = _StubDdb(item={"memory_pending": {"S": json.dumps(blob)}})
    enabled, pending = _memory_resume_start(
        _StubMemory(ddb=ddb), _CHAT_CONFIG, "u1", "t1"
    )
    assert enabled is True and pending == blob


def test_resume_start_disabled_short_circuits():
    enabled, pending = _memory_resume_start(
        _StubMemory(enabled=False, ddb=_StubDdb()), _CHAT_CONFIG, "u1", "t1"
    )
    assert enabled is False and pending is None


def test_persist_pending_writes_observation_and_qa():
    from chat.memory import TurnObservation

    ddb = _StubDdb()
    obs = TurnObservation()
    obs.observe(
        {
            "type": "tool",
            "id": "a",
            "tool_name": "run_computation",
            "tool_start": True,
            "content": {"data_domain": "sports", "dataset": "f1", "name": "laps"},
        }
    )
    obs.observe({"type": "tool", "id": "a", "tool_start": False, "content": "ok"})
    qa = [{"prompt": "Calendar or fiscal?", "answer": "calendar"}]
    _memory_persist_pending(
        _StubMemory(ddb=ddb), _CHAT_CONFIG, "u1", "t1", obs, qa
    )
    (update,) = ddb.updates
    assert update["Key"]["sk"] == {"S": "THREAD#t1"}
    blob = json.loads(update["ExpressionAttributeValues"][":mp"]["S"])
    assert blob["qa"] == qa
    assert blob["obs"]["datasets"] == ["sports/f1"]
    assert blob["obs"]["governed"][0]["slug"] == "laps"


# --- the governed-tool scope fold ---------------------------------------------------


def test_governed_tools_are_location_taking():
    # Without the fold, a pinned conversation's run_computation chunk carries
    # no data_domain/dataset (chat.tools strips them from the model schema),
    # TurnObservation records a datasetless binding, and the extracted memory
    # leaks into every other dataset's pinned chats.
    assert "run_computation" in _LOCATION_TAKING_TOOLS
    assert "query_metric" in _LOCATION_TAKING_TOOLS


def test_with_scope_folds_pin_into_run_computation_args():
    scope = {"data_domain": "sports", "dataset": "f1"}
    folded = _with_scope("run_computation", {"name": "laps"}, scope)
    assert folded == {"name": "laps", "data_domain": "sports", "dataset": "f1"}
    # Model-passed values are never overwritten.
    explicit = _with_scope(
        "run_computation", {"name": "laps", "dataset": "f2"}, scope
    )
    assert explicit["dataset"] == "f2"
