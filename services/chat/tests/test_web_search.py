"""The web_search tool: MCP handshake, result normalization, error convention.

Fully offline — the transport (the one thing that would talk to AWS) is injected,
so these tests exercise the JSON-RPC we send, the two response encodings the
gateway may answer with, result normalization, the connector-1.2.0 request-level
filters (their wire shape, validation, and the deploy gate: a pre-1.2.0 target
silently IGNORES `filters`, so an ungated deployment must never offer or send
them), and the "a tool error is feedback, not a crash" convention.
"""

from __future__ import annotations

import datetime
import json

import pytest

from chat.web_search import (
    DOMAIN_FILTER_MAX,
    QUERY_MAX_CHARS,
    HttpResponse,
    WebSearchError,
    WebSearchGateway,
    build_web_search_engine,
    make_web_search_tool,
    normalize_gateway_url,
    parse_published_date,
)

GATEWAY = "https://okf-web-abcdefghij.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"


def _search_payload(results):
    """The connector's reply: the search JSON inside a text content block."""
    return {
        "content": [{"type": "text", "text": json.dumps({"id": "abc", "results": results})}]
    }


class FakeTransport:
    """Records every request and answers from a per-method script."""

    def __init__(self, *, tool_result=None, tools_list=None, init_status=200, call_status=200):
        self.requests = []  # [(payload, headers)]
        self.tool_result = tool_result if tool_result is not None else _search_payload([])
        self.tools_list = tools_list
        self.init_status = init_status
        self.call_status = call_status
        self.init_count = 0

    def __call__(self, body, headers):
        payload = json.loads(body)
        self.requests.append((payload, headers))
        method = payload.get("method")
        if method == "initialize":
            self.init_count += 1
            return HttpResponse(
                status=self.init_status,
                headers={"Content-Type": "application/json", "Mcp-Session-Id": "sess-1"},
                text=json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": {}}),
            )
        if method == "tools/list":
            tools = self.tools_list if self.tools_list is not None else []
            return self._ok(payload["id"], {"tools": tools})
        if method == "tools/call":
            if self.call_status >= 300:
                return HttpResponse(
                    status=self.call_status, headers={}, text="denied"
                )
            return self._ok(payload["id"], self.tool_result)
        raise AssertionError(f"unexpected method {method}")

    def _ok(self, req_id, result):
        return HttpResponse(
            status=200,
            headers={"Content-Type": "application/json"},
            text=json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}),
        )

    def calls(self, method):
        return [p for p, _ in self.requests if p.get("method") == method]


def _engine(transport, **kw):
    kw.setdefault("tool_name", "okf-web-search___WebSearch")
    kw.setdefault("today", lambda: datetime.date(2026, 7, 27))
    return WebSearchGateway(gateway_url=GATEWAY, transport=transport, **kw)


# --- URL + date helpers -------------------------------------------------------


def test_normalize_gateway_url_appends_mcp_path_once():
    base = "https://gw-1.gateway.bedrock-agentcore.us-east-1.amazonaws.com"
    assert normalize_gateway_url(base) == f"{base}/mcp"
    assert normalize_gateway_url(f"{base}/mcp") == f"{base}/mcp"
    assert normalize_gateway_url(f"{base}/mcp/") == f"{base}/mcp"
    assert normalize_gateway_url("") == ""


def test_parse_published_date_reads_dates_and_timestamps():
    assert parse_published_date("2024-10-07") == datetime.date(2024, 10, 7)
    assert parse_published_date("2024-10-07T11:22:33Z") == datetime.date(2024, 10, 7)
    # The connector's prose timestamp variant, verbatim as observed live.
    assert parse_published_date("05:00PM, Sunday, October 06 2024, PDT") == datetime.date(
        2024, 10, 6
    )
    assert parse_published_date("March 3, 2026") == datetime.date(2026, 3, 3)
    # Unreadable -> UNDATED (never silently "today"), incl. impossible dates in
    # either format.
    assert parse_published_date("last Tuesday") is None
    assert parse_published_date("2024-13-45") is None
    assert parse_published_date("Smarch 5, 2024") is None
    assert parse_published_date("February 30 2024") is None
    assert parse_published_date(None) is None


# --- the MCP conversation -----------------------------------------------------


def test_search_initializes_then_calls_the_tool_with_the_session_id():
    t = FakeTransport(
        tool_result=_search_payload(
            [
                {
                    "title": "Sector sales fell 8% in Q2",
                    "url": "https://example.com/q2",
                    "publishedDate": "2026-07-02",
                    "text": "Industry-wide sales declined...",
                }
            ]
        )
    )
    out = _engine(t).search("q2 sector sales decline", max_results=5)

    methods = [p["method"] for p, _ in t.requests]
    assert methods == ["initialize", "tools/call"]
    # The initialize request carries no session id; the call echoes the vended one.
    assert "Mcp-Session-Id" not in t.requests[0][1]
    assert t.requests[1][1]["Mcp-Session-Id"] == "sess-1"
    # MCP streamable HTTP requires the client to accept both encodings.
    assert t.requests[1][1]["Accept"] == "application/json, text/event-stream"
    call = t.calls("tools/call")[0]["params"]
    assert call["name"] == "okf-web-search___WebSearch"
    assert call["arguments"] == {"query": "q2 sector sales decline", "maxResults": 5}

    assert out["result_count"] == 1
    assert out["as_of"] == "2026-07-27"
    assert out["results"][0] == {
        "title": "Sector sales fell 8% in Q2",
        "url": "https://example.com/q2",
        "published_date": "2026-07-02",
        "text": "Industry-wide sales declined...",
    }
    # An unfiltered search stays flat — no `filters` key on the wire (a pre-1.2.0
    # gateway must see the exact old shape) and none echoed in the payload.
    assert set(out) == {"query", "as_of", "result_count", "results"}


def test_session_is_reused_across_searches():
    t = FakeTransport()
    engine = _engine(t)
    engine.search("one")
    engine.search("two")
    assert t.init_count == 1
    assert [p["method"] for p, _ in t.requests].count("tools/call") == 2


def test_expired_session_is_re_established_once():
    # A gateway that has forgotten the session answers 404, then succeeds after a
    # fresh handshake — the search must survive that without the model seeing it.
    t = FakeTransport()
    engine = _engine(t)
    engine.search("warm up")  # establishes sess-1

    calls = {"n": 0}
    inner = t.__call__

    def flaky(body, headers):
        payload = json.loads(body)
        if payload.get("method") == "tools/call":
            calls["n"] += 1
            if calls["n"] == 1:
                t.requests.append((payload, headers))
                return HttpResponse(status=404, headers={}, text="no session")
        return inner(body, headers)

    engine._transport = flaky
    out = engine.search("again")
    assert out["result_count"] == 0
    assert t.init_count == 2  # re-handshaked


def test_sse_encoded_reply_is_parsed():
    class SseTransport(FakeTransport):
        def _ok(self, req_id, result):
            body = "event: message\ndata: " + json.dumps(
                {"jsonrpc": "2.0", "id": req_id, "result": result}
            ) + "\n\n"
            return HttpResponse(
                status=200, headers={"Content-Type": "text/event-stream"}, text=body
            )

    t = SseTransport(
        tool_result=_search_payload(
            [{"title": "T", "url": "https://e/1", "publishedDate": "2026-01-01", "text": "x"}]
        )
    )
    out = _engine(t).search("anything")
    assert out["results"][0]["url"] == "https://e/1"


def test_structured_content_is_preferred_over_the_text_block():
    t = FakeTransport(
        tool_result={
            "content": [{"type": "text", "text": "not json"}],
            "structuredContent": {
                "results": [{"title": "S", "url": "https://e/s", "text": "y"}]
            },
        }
    )
    out = _engine(t).search("anything")
    assert out["results"][0]["title"] == "S"
    assert out["results"][0]["published_date"] is None


def test_tool_name_is_discovered_when_not_configured():
    t = FakeTransport(
        tools_list=[
            {"name": "something___else"},
            {"name": "okf-web-search___WebSearch"},
        ]
    )
    engine = _engine(t, tool_name="")
    engine.search("q")
    assert t.calls("tools/call")[0]["params"]["name"] == "okf-web-search___WebSearch"
    # Discovery is cached: a second search doesn't re-list.
    engine.search("q2")
    assert len(t.calls("tools/list")) == 1


def test_missing_web_search_tool_raises():
    t = FakeTransport(tools_list=[{"name": "other___tool"}])
    with pytest.raises(WebSearchError, match="no WebSearch tool"):
        _engine(t, tool_name="").search("q")


def test_gateway_http_error_raises_web_search_error():
    t = FakeTransport(call_status=403)
    with pytest.raises(WebSearchError, match="HTTP 403"):
        _engine(t).search("q")


def test_is_error_result_raises():
    t = FakeTransport(
        tool_result={"isError": True, "content": [{"type": "text", "text": "throttled"}]}
    )
    with pytest.raises(WebSearchError, match="throttled"):
        _engine(t).search("q")


# --- max_results ---------------------------------------------------------------


def test_default_max_results_is_ten():
    t = FakeTransport()
    _engine(t).search("q")
    assert t.calls("tools/call")[0]["params"]["arguments"]["maxResults"] == 10


def test_search_asks_for_exactly_what_was_requested():
    t = FakeTransport()
    _engine(t).search("q", max_results=4)
    assert t.calls("tools/call")[0]["params"]["arguments"]["maxResults"] == 4


def test_max_results_is_clamped_to_the_connector_ceiling():
    t = FakeTransport()
    _engine(t).search("q", max_results=99)
    assert t.calls("tools/call")[0]["params"]["arguments"]["maxResults"] == 25


def test_bad_arguments_raise_value_error_before_any_request():
    t = FakeTransport()
    engine = _engine(t)
    with pytest.raises(ValueError, match="must not be empty"):
        engine.search("   ")
    with pytest.raises(ValueError, match="characters or fewer"):
        engine.search("x" * (QUERY_MAX_CHARS + 1))
    with pytest.raises(ValueError, match="at least 1"):
        engine.search("q", max_results=-3)
    assert t.requests == []


# --- request-level filters (connector 1.2.0) -----------------------------------


def test_filters_are_sent_in_the_connector_wire_shape_and_echoed():
    t = FakeTransport()
    out = _engine(t, filters_enabled=True).search(
        "steel tariffs",
        published_after="2026-04-01",
        published_before="2026-06-30",
        include_domains=["europa.eu", "reuters.com"],
        exclude_domains=["example.com"],
    )
    args = t.calls("tools/call")[0]["params"]["arguments"]
    assert args["filters"] == {
        "domainFilter": {
            "include": ["europa.eu", "reuters.com"],
            "exclude": ["example.com"],
        },
        "publishedDateFilter": {"from": "2026-04-01", "to": "2026-06-30"},
    }
    # The payload echoes what constrained the search, in ARG shape (so an empty
    # result reads as "maybe over-constrained", not "the web has nothing").
    assert out["filters"] == {
        "include_domains": ["europa.eu", "reuters.com"],
        "exclude_domains": ["example.com"],
        "published_after": "2026-04-01",
        "published_before": "2026-06-30",
    }


def test_partial_filters_send_only_what_was_given():
    t = FakeTransport()
    _engine(t, filters_enabled=True).search("q", published_after="2026-01-01")
    args = t.calls("tools/call")[0]["params"]["arguments"]
    assert args["filters"] == {"publishedDateFilter": {"from": "2026-01-01"}}

    t2 = FakeTransport()
    _engine(t2, filters_enabled=True).search("q", include_domains=["sec.gov"])
    args2 = t2.calls("tools/call")[0]["params"]["arguments"]
    assert args2["filters"] == {"domainFilter": {"include": ["sec.gov"]}}


def test_a_lone_domain_string_is_accepted_as_a_one_item_list():
    t = FakeTransport()
    _engine(t, filters_enabled=True).search("q", include_domains="sec.gov")
    args = t.calls("tools/call")[0]["params"]["arguments"]
    assert args["filters"] == {"domainFilter": {"include": ["sec.gov"]}}


def test_bad_filters_raise_value_error_before_any_request():
    t = FakeTransport()
    engine = _engine(t, filters_enabled=True)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        engine.search("q", published_after="last quarter")
    with pytest.raises(ValueError, match="not a real date"):
        engine.search("q", published_before="2026-13-45")
    with pytest.raises(ValueError, match="must not be later"):
        engine.search("q", published_after="2026-06-01", published_before="2026-01-01")
    with pytest.raises(ValueError, match="bare domains"):
        engine.search("q", include_domains=["https://example.com/page"])
    with pytest.raises(ValueError, match=f"at most {DOMAIN_FILTER_MAX}"):
        engine.search("q", exclude_domains=[f"d{i}.com" for i in range(DOMAIN_FILTER_MAX + 1)])
    assert t.requests == []


def test_filters_are_refused_when_the_deployment_is_not_gated_on():
    # A pre-1.2.0 target silently IGNORES `filters` (verified live), so sending
    # them ungated would give the model constraints that quietly don't apply —
    # refuse with actionable feedback instead, before any request.
    t = FakeTransport()
    with pytest.raises(ValueError, match="predates connector 1.2.0"):
        _engine(t).search("q", published_after="2026-01-01")
    assert t.requests == []


# --- the LangChain tool wrapper ----------------------------------------------


def test_tool_shape_and_description_without_filters():
    # The ungated variant (target predates connector 1.2.0): the old two-arg
    # surface, and the description documents the ABSENT date filter as a
    # property, not an omission.
    tool = make_web_search_tool(_engine(FakeTransport()), today=datetime.date(2026, 7, 27))
    assert tool.name == "web_search"
    # The model needs today's date to write "Q2 2026" for "last quarter"; the
    # system prompt is static, which makes the description the place for it.
    assert "2026-07-27" in tool.description
    assert set(tool.args) == {"query", "max_results"}
    assert "no date filter" in tool.description
    assert "default 10" in tool.description
    assert "RELEVANCE, not date" in tool.description
    assert "put the period in the query itself" in tool.description
    assert "EU steel tariffs Q2 2026" in tool.description
    assert "publication date before" in tool.description
    # Never advertise args the gateway would silently ignore.
    assert "published_after" not in tool.description
    assert "include_domains" not in tool.description


def test_tool_shape_and_description_with_filters():
    tool = make_web_search_tool(
        _engine(FakeTransport(), filters_enabled=True), today=datetime.date(2026, 7, 27)
    )
    assert set(tool.args) == {
        "query",
        "max_results",
        "published_after",
        "published_before",
        "include_domains",
        "exclude_domains",
    }
    assert "2026-07-27" in tool.description
    # The query-text time anchor survives (the date filter matches when a page
    # was PUBLISHED, not the period it is about), now beside the bounds.
    assert "EU steel tariffs Q2 2026" in tool.description
    assert "`published_after`/`published_before`" in tool.description
    assert "YYYY-MM-DD" in tool.description
    assert "include_domains" in tool.description
    assert f"up to {DOMAIN_FILTER_MAX} per list" in tool.description
    assert "no date filter" not in tool.description


def test_tool_passes_filter_args_through_to_the_engine():
    t = FakeTransport()
    tool = make_web_search_tool(_engine(t, filters_enabled=True))
    tool.invoke(
        {
            "query": "q",
            "published_after": "2026-01-01",
            "include_domains": ["sec.gov"],
        }
    )
    args = t.calls("tools/call")[0]["params"]["arguments"]
    assert args["filters"] == {
        "domainFilter": {"include": ["sec.gov"]},
        "publishedDateFilter": {"from": "2026-01-01"},
    }

    # And a filter mistake is feedback, not a crash.
    out = tool.invoke({"query": "q", "published_after": "yesterday"})
    assert out.startswith("Error: ") and "YYYY-MM-DD" in out


def test_tool_returns_errors_as_results_never_raises():
    tool = make_web_search_tool(_engine(FakeTransport(call_status=500)))
    out = tool.invoke({"query": "q"})
    assert isinstance(out, str) and out.startswith("Error: web_search failed")

    bad_args = make_web_search_tool(_engine(FakeTransport())).invoke({"query": "  "})
    assert bad_args.startswith("Error: ") and "must not be empty" in bad_args


def test_tool_passes_a_zero_max_results_through_as_the_default():
    t = FakeTransport()
    tool = make_web_search_tool(_engine(t, default_max_results=6))
    tool.invoke({"query": "q", "max_results": 0})
    assert t.calls("tools/call")[0]["params"]["arguments"]["maxResults"] == 6


# --- deploy gating ------------------------------------------------------------


class _Cfg:
    def __init__(self, **kw):
        self.web_search_enabled = kw.get("enabled", True)
        self.web_search_gateway_url = kw.get("url", GATEWAY)
        self.web_search_region = "us-east-1"
        self.web_search_tool_name = ""
        self.web_search_max_results = 8
        self.web_search_filters_enabled = kw.get("filters", False)


def test_engine_is_none_unless_flag_and_url_are_both_present(monkeypatch):
    # Never construct the real SigV4 transport in a test.
    import chat.web_search as ws

    monkeypatch.setattr(ws, "_sigv4_transport", lambda *a, **k: FakeTransport())
    assert build_web_search_engine(_Cfg(enabled=False)) is None
    assert build_web_search_engine(_Cfg(url="  ")) is None
    assert build_web_search_engine(_Cfg()) is not None


def test_engine_reads_the_filters_gate_from_config(monkeypatch):
    import chat.web_search as ws

    monkeypatch.setattr(ws, "_sigv4_transport", lambda *a, **k: FakeTransport())
    assert build_web_search_engine(_Cfg()).filters_enabled is False
    assert build_web_search_engine(_Cfg(filters=True)).filters_enabled is True
