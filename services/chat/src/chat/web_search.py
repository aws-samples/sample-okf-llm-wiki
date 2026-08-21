"""Public web search for the chat agent, via the AgentCore Gateway connector.

This is the only chat tool that reads something OUTSIDE the organization: it
answers "is this number actually bad?" questions by putting a wiki-grounded
figure next to public context (competitor results, tariffs, weather, a policy
change). The wiki stays the source of truth for what the data MEANS; the web is
only ever external context around it.

**Why a gateway and not an API.** Web Search on Bedrock AgentCore is exposed
exclusively as a built-in *connector target* on an AgentCore **Gateway**, which
speaks MCP over streamable HTTP — there is no direct `bedrock-agentcore:Search`
data-plane call. So this module is a small MCP client:

    initialize  -> Mcp-Session-Id  ->  tools/call {name, arguments}

signed with SigV4 (the gateway's inbound authorizer is ``AWS_IAM``, service
``bedrock-agentcore``) using the runtime's own execution-role credentials. We
hand-roll the JSON-RPC rather than pull in the async ``mcp`` SDK: the whole
protocol surface we need is two POSTs, and LangChain tools here are SYNCHRONOUS
(an async client would need an event-loop bridge inside a tool called from the
running graph loop). botocore — already a dependency via boto3 — supplies both
the signer and the HTTP session, so this adds no new package.

**Region.** The connector is offered in ``us-east-1``, ``eu-west-1``, and
``ap-northeast-1``; ``var.web_search_region`` places the gateway in one of them
(default us-east-1), which may still differ from the deployment's own region
(see infra/compute/web_search.tf). The query never leaves AWS — the gateway
serves it internally — but it can leave the deployment's region, which is the
trade-off for the capability.

**Server-side filters — deploy-gated.** Connector version 1.2.0 added an
optional ``filters`` object to ``WebSearch``: ``domainFilter.include/exclude``
(bare domains, subdomains match, ≤100 per list) and ``publishedDateFilter``
``from``/``to`` (inclusive dates). We surface them as the ``published_after``/
``published_before``/``include_domains``/``exclude_domains`` tool args. An
earlier revision shipped the date args as CLIENT-side post-filters and they
were removed: filtering a relevance-ranked top-N after the fact can only
subtract results, never surface period-relevant pages the ranking missed. The
connector's filters constrain the search itself, so that objection no longer
applies — but the agent still anchors the period in the QUERY TEXT too ("EU
steel tariffs Q2 2026" — the tool description carries today's date so it can),
because the date filter matches when a page was PUBLISHED, not the period it is
about. The args exist only when ``OKF_WEB_SEARCH_FILTERS_ENABLED`` is set —
Terraform sets it in the same apply that pins the gateway target to connector
>= 1.2.0 — because a pre-1.2.0 target does not REJECT an unknown ``filters``
argument, it silently IGNORES it (verified live): advertising the args against
an unpinned target would hand the model constraints that quietly don't apply.

Deploy-gated by ``OKF_WEB_SEARCH_ENABLED`` + a gateway URL: with either missing
the tool is simply not offered (the role carries no InvokeGateway grant anyway).
There is no per-run opt-in — unlike ``run_sql`` this touches no source data, and
the agent needs it available at the moment a question turns outward.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from typing import Any, Callable, NamedTuple

log = logging.getLogger("chat.web_search")

# The connector's own limits (docs: Web Search Tool input schema). We enforce
# them client-side so a too-long query comes back as actionable tool feedback
# instead of a gateway 400 the model can't interpret.
QUERY_MAX_CHARS = 200
RESULTS_MAX = 25
DOMAIN_FILTER_MAX = 100  # per list (include/exclude), connector 1.2.0

# The MCP protocol revision the gateway examples use.
_MCP_PROTOCOL_VERSION = "2025-06-18"

# AgentCore Gateway prefixes every tool with its target name, joined by THREE
# underscores (docs: "Understand how AgentCore Gateway tools are named"), e.g.
# `okf-web-search___WebSearch`. Terraform passes the composed name in env; when
# it's absent we discover it from tools/list rather than guessing a prefix.
_WEB_SEARCH_TOOL_SUFFIX = "websearch"


class HttpResponse(NamedTuple):
    """The minimal HTTP response shape this client needs (fakeable in tests)."""

    status: int
    headers: dict
    text: str


# transport(body, headers) -> HttpResponse
Transport = Callable[[str, dict], HttpResponse]


class WebSearchError(RuntimeError):
    """A gateway/protocol failure — surfaced to the model as tool feedback."""


def _header(headers: Any, name: str) -> str | None:
    """Case-insensitive header lookup that works on dicts and botocore headers."""
    if not headers:
        return None
    getter = getattr(headers, "get", None)
    if getter is not None:
        direct = getter(name)
        if direct:
            return direct
    lowered = name.lower()
    try:
        items = headers.items()
    except AttributeError:  # pragma: no cover - defensive
        return None
    for key, value in items:
        if str(key).lower() == lowered:
            return value
    return None


def _parse_jsonrpc_body(resp: HttpResponse) -> dict:
    """Decode a JSON-RPC response body, whether it came back as JSON or SSE.

    Streamable-HTTP MCP servers may answer a POST with either ``application/json``
    or an ``text/event-stream`` carrying the same JSON in ``data:`` lines, and the
    gateway picks based on the request's Accept header. We accept both so a
    server-side change of mind can't break the tool.
    """
    body = resp.text or ""
    ctype = (_header(resp.headers, "Content-Type") or "").lower()
    if "text/event-stream" in ctype or body.lstrip().startswith("event:"):
        for line in body.splitlines():
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk:
                continue
            try:
                parsed = json.loads(chunk)
            except ValueError:
                continue
            # Skip server-initiated notifications; we want the reply to our call.
            if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
                return parsed
        raise WebSearchError("gateway returned an event stream with no JSON-RPC result")
    try:
        return json.loads(body)
    except ValueError as e:
        raise WebSearchError(f"gateway returned a non-JSON body: {body[:200]!r}") from e


def _sigv4_transport(url: str, region: str, timeout_s: float) -> Transport:
    """Build the live transport: SigV4-signed POSTs over botocore's HTTP session.

    Credentials are resolved (and frozen) per request so the container's rotating
    role credentials keep working across a long-lived runtime session.
    """
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.httpsession import URLLib3Session

    boto_session = boto3.Session()
    http = URLLib3Session(timeout=timeout_s)

    def send(body: str, headers: dict) -> HttpResponse:
        creds = boto_session.get_credentials()
        if creds is None:  # pragma: no cover - misconfigured container
            raise WebSearchError("no AWS credentials available to sign the gateway call")
        request = AWSRequest(method="POST", url=url, data=body, headers=dict(headers))
        SigV4Auth(creds.get_frozen_credentials(), "bedrock-agentcore", region).add_auth(
            request
        )
        resp = http.send(request.prepare())
        text = resp.text if isinstance(resp.text, str) else (resp.content or b"").decode(
            "utf-8", "replace"
        )
        return HttpResponse(status=resp.status_code, headers=resp.headers, text=text)

    return send


def normalize_gateway_url(url: str) -> str:
    """Return the gateway's MCP endpoint (Terraform's ``gateway_url`` may omit /mcp)."""
    trimmed = (url or "").strip().rstrip("/")
    if not trimmed:
        return ""
    return trimmed if trimmed.endswith("/mcp") else f"{trimmed}/mcp"


_MONTHS = {
    name: number
    for number, name in enumerate(
        "january february march april may june july august "
        "september october november december".split(),
        start=1,
    )
}


def parse_published_date(value: Any) -> datetime.date | None:
    """Best-effort ``publishedDate`` -> date (the connector's format is unpinned).

    Results carry dates like ``2024-10-07`` and full timestamps, but also prose
    ("05:00PM, Sunday, October 06 2024, PDT" — observed live); anything we can't
    read counts as UNDATED rather than silently mapping to today.
    """
    if not isinstance(value, str):
        return None
    iso = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", value)
    if iso:
        parts = (int(iso[1]), int(iso[2]), int(iso[3]))
    else:
        prose = re.search(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", value)
        if not prose or prose[1].lower() not in _MONTHS:
            return None
        parts = (int(prose[3]), _MONTHS[prose[1].lower()], int(prose[2]))
    try:
        return datetime.date(*parts)
    except ValueError:
        return None


def _validate_filter_date(value: str, arg: str) -> str:
    """A model-supplied date bound -> validated ``YYYY-MM-DD`` (or ValueError)."""
    cleaned = (value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
        raise ValueError(f"{arg} must be a YYYY-MM-DD date (got {cleaned!r})")
    try:
        datetime.date.fromisoformat(cleaned)
    except ValueError as e:
        raise ValueError(f"{arg} is not a real date: {cleaned}") from e
    return cleaned


def _validate_domains(values: Any, arg: str) -> list[str]:
    """Model-supplied domain list -> cleaned bare domains (or ValueError)."""
    if values is None:
        return []
    if isinstance(values, str):  # a lone domain instead of a one-item list
        values = [values]
    cleaned = [str(v).strip() for v in values if str(v).strip()]
    if len(cleaned) > DOMAIN_FILTER_MAX:
        raise ValueError(
            f"{arg} accepts at most {DOMAIN_FILTER_MAX} domains (got {len(cleaned)})"
        )
    for domain in cleaned:
        if "://" in domain or "/" in domain or " " in domain:
            raise ValueError(
                f'{arg} entries must be bare domains like "example.com" '
                f"(got {domain!r})"
            )
    return cleaned


class WebSearchGateway:
    """MCP client for the gateway's ``WebSearch`` connector tool.

    The MCP session id is cached on the instance (one initialize per process
    rather than per search) and re-established transparently when the gateway
    expires it. ``transport`` is injectable so tests exercise the protocol and
    the filtering without AWS.
    """

    def __init__(
        self,
        *,
        gateway_url: str,
        region: str = "us-east-1",
        tool_name: str = "",
        default_max_results: int = 10,
        filters_enabled: bool = False,
        timeout_s: float = 20.0,
        transport: Transport | None = None,
        today: Callable[[], datetime.date] | None = None,
    ) -> None:
        self.url = normalize_gateway_url(gateway_url)
        if not self.url:
            raise ValueError("gateway_url is required")
        self.region = region
        self.default_max_results = max(1, min(RESULTS_MAX, default_max_results))
        # True only when the deployment's gateway target is pinned to connector
        # >= 1.2.0 (a pre-1.2.0 target silently IGNORES `filters` — see module
        # docstring). Gates both the tool's argument surface and search().
        self.filters_enabled = filters_enabled
        self._tool_name = (tool_name or "").strip()
        self._transport = transport or _sigv4_transport(self.url, region, timeout_s)
        self._today = today or (lambda: datetime.datetime.now(datetime.timezone.utc).date())
        self._session_id: str | None = None
        self._next_id = 0

    # --- MCP plumbing --------------------------------------------------------

    def _post(self, payload: dict, *, session_id: str | None) -> HttpResponse:
        headers = {
            "Content-Type": "application/json",
            # The MCP streamable-HTTP transport requires the client to accept
            # both; the gateway may answer either way (see _parse_jsonrpc_body).
            "Accept": "application/json, text/event-stream",
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        return self._transport(json.dumps(payload), headers)

    def _rpc(self, method: str, params: dict, *, retry_session: bool = True) -> dict:
        """One JSON-RPC call, initializing (or re-initializing) the session as needed."""
        if self._session_id is None:
            self._initialize()
        self._next_id += 1
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": f"okf-{self._next_id}",
                "method": method,
                "params": params,
            },
            session_id=self._session_id,
        )
        # A dropped/expired session shows up as a 4xx on an otherwise valid call;
        # re-handshake once before treating it as a real failure.
        if resp.status in (400, 404) and retry_session:
            log.info("web_search session rejected (%s) - re-initializing", resp.status)
            self._session_id = None
            return self._rpc(method, params, retry_session=False)
        if resp.status >= 300:
            raise WebSearchError(
                f"gateway {method} failed with HTTP {resp.status}: {(resp.text or '')[:300]}"
            )
        body = _parse_jsonrpc_body(resp)
        if isinstance(body.get("error"), dict):
            err = body["error"]
            raise WebSearchError(
                f"gateway {method} error {err.get('code')}: {err.get('message')}"
            )
        result = body.get("result")
        if not isinstance(result, dict):
            raise WebSearchError(f"gateway {method} returned no result object")
        return result

    def _initialize(self) -> None:
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": "okf-init",
                "method": "initialize",
                "params": {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "okf-chat", "version": "1.0.0"},
                },
            },
            session_id=None,
        )
        if resp.status >= 300:
            raise WebSearchError(
                f"gateway initialize failed with HTTP {resp.status}: {(resp.text or '')[:300]}"
            )
        _parse_jsonrpc_body(resp)  # surfaces a JSON-RPC error as WebSearchError
        session_id = _header(resp.headers, "Mcp-Session-Id")
        # A stateless gateway may not vend a session id at all; carry on without
        # one rather than failing (the id is only echoed back when present).
        self._session_id = session_id or ""

    def _resolve_tool_name(self) -> str:
        """The gateway-side tool name, from config or discovered via tools/list."""
        if self._tool_name:
            return self._tool_name
        result = self._rpc("tools/list", {})
        names = [
            t.get("name")
            for t in (result.get("tools") or [])
            if isinstance(t, dict) and t.get("name")
        ]
        for name in names:
            # `<target>___WebSearch` — match on the suffix so the target name is
            # free to change without a code change.
            if str(name).lower().replace("_", "").endswith(_WEB_SEARCH_TOOL_SUFFIX):
                self._tool_name = str(name)
                return self._tool_name
        raise WebSearchError(
            "no WebSearch tool on the gateway (tools/list returned: "
            f"{', '.join(str(n) for n in names) or 'nothing'})"
        )

    # --- the search itself ---------------------------------------------------

    def search(
        self,
        query: str,
        *,
        max_results: int | None = None,
        published_after: str = "",
        published_before: str = "",
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run one web search; return normalized results.

        Raises ``ValueError`` for bad arguments (the tool turns those into
        actionable feedback) and ``WebSearchError`` for gateway failures.
        """
        cleaned = (query or "").strip()
        if not cleaned:
            raise ValueError("query must not be empty")
        if len(cleaned) > QUERY_MAX_CHARS:
            raise ValueError(
                f"query must be {QUERY_MAX_CHARS} characters or fewer "
                f"(got {len(cleaned)}) — search for the key terms, not a sentence"
            )
        wanted = max_results or self.default_max_results
        if wanted < 1:
            raise ValueError("max_results must be at least 1")
        wanted = min(RESULTS_MAX, wanted)

        # Request-level filters (connector 1.2.0 wire shape). Validated FIRST so
        # bad values come back as tool feedback before any network round trip.
        filters: dict[str, Any] = {}
        include = _validate_domains(include_domains, "include_domains")
        exclude = _validate_domains(exclude_domains, "exclude_domains")
        domain_filter = {
            key: val for key, val in (("include", include), ("exclude", exclude)) if val
        }
        if domain_filter:
            filters["domainFilter"] = domain_filter
        date_filter = {}
        if (published_after or "").strip():
            date_filter["from"] = _validate_filter_date(published_after, "published_after")
        if (published_before or "").strip():
            date_filter["to"] = _validate_filter_date(published_before, "published_before")
        if "from" in date_filter and "to" in date_filter and date_filter["from"] > date_filter["to"]:
            raise ValueError("published_after must not be later than published_before")
        if date_filter:
            filters["publishedDateFilter"] = date_filter
        if filters and not self.filters_enabled:
            # Never send filters a pre-1.2.0 target would silently ignore —
            # a constraint the model believes in but that wasn't applied is
            # worse than an error it can react to.
            raise ValueError(
                "date/domain filters are not available on this deployment "
                "(its web-search target predates connector 1.2.0) — retry "
                "without them and steer with the query text instead"
            )

        arguments: dict[str, Any] = {"query": cleaned, "maxResults": wanted}
        if filters:
            arguments["filters"] = filters
        result = self._rpc(
            "tools/call",
            {"name": self._resolve_tool_name(), "arguments": arguments},
        )
        if result.get("isError"):
            raise WebSearchError(f"web search failed: {_result_text(result)[:300]}")

        results = [
            {
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                # Normalized to ISO (or None) so the UI and the model read one
                # shape whatever format the connector used.
                "published_date": (
                    published.isoformat()
                    if (published := parse_published_date(item.get("publishedDate")))
                    else None
                ),
                "text": item.get("text") or "",
            }
            for item in _extract_results(result)
        ]
        payload: dict[str, Any] = {
            "query": cleaned,
            "as_of": self._today().isoformat(),
            "result_count": len(results),
            "results": results,
        }
        if filters:
            # Echo what actually constrained the search (arg-shaped, not wire-
            # shaped) so an empty result reads as "maybe my window/domains were
            # too tight", not "the web has nothing".
            applied: dict[str, Any] = {}
            if include:
                applied["include_domains"] = include
            if exclude:
                applied["exclude_domains"] = exclude
            if "from" in date_filter:
                applied["published_after"] = date_filter["from"]
            if "to" in date_filter:
                applied["published_before"] = date_filter["to"]
            payload["filters"] = applied
        return payload


def _result_text(result: dict) -> str:
    """Concatenate the text parts of an MCP tool result's content blocks."""
    parts = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _extract_results(result: dict) -> list[dict]:
    """Pull the result list out of an MCP tool result.

    The connector answers with the search payload JSON-encoded inside a text
    content block (``{"id":…,"results":[…]}``) and, per the MCP spec, may ALSO
    provide it pre-parsed as ``structuredContent``. Prefer the structured form
    when present, else decode the text.
    """
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("results"), list):
        return [r for r in structured["results"] if isinstance(r, dict)]
    text = _result_text(result).strip()
    if not text:
        return []
    try:
        decoded = json.loads(text)
    except ValueError as e:
        raise WebSearchError(f"could not decode search results: {text[:200]!r}") from e
    if isinstance(decoded, dict) and isinstance(decoded.get("results"), list):
        return [r for r in decoded["results"] if isinstance(r, dict)]
    if isinstance(decoded, list):
        return [r for r in decoded if isinstance(r, dict)]
    return []


# The tool description the model sees. It carries TODAY'S DATE because the
# connector ranks by relevance — the query text (and, when available, the date
# bounds) is the time lever, and the model can't write "Q2 2026" for "last
# quarter" without knowing "now". The system prompt is deliberately static (a
# cacheable prefix), so this is the one place a per-run date belongs. The args
# paragraph has two variants, picked by engine.filters_enabled — a deployment
# whose target predates connector 1.2.0 must not be told about args that its
# gateway would silently ignore.
_WEB_SEARCH_DESC_HEAD = (
    "Search the public web and return ranked results (title, url, publication "
    "date, and a relevant text snippet). Today is {today}.\n"
    "Use this for context that lives OUTSIDE the wiki and outside the data: to "
    "judge whether a number is actually unusual (industry trend, a competitor's "
    "reported quarter), to look for a plausible external cause of a movement "
    "(tariffs, regulation, a supply shock, weather, an outage, a holiday shift), "
    "or to check a fact published after your training cutoff. NEVER use it for "
    "what a table, column, join, or metric MEANS — that is what the wiki is for, "
    "and web guesses about this organization's schema are wrong by default.\n"
    "Args: `query` is a short keyword phrase, {max_chars} characters or fewer (not "
    "a sentence). `max_results` is 1-{results_max} (default {default}). "
)
_WEB_SEARCH_DESC_NO_FILTERS = (
    "Results are ranked by RELEVANCE, not date, and there is no date filter — "
    "when the question concerns a specific period, put the period in the query "
    'itself ("EU steel tariffs Q2 2026") and check each result\'s publication '
    "date before treating it as evidence for that period.\n"
)
_WEB_SEARCH_DESC_FILTERS = (
    "Results are ranked by RELEVANCE — when the question concerns a specific "
    'period, put the period in the query itself ("EU steel tariffs Q2 2026") '
    "and bound it with `published_after`/`published_before` (YYYY-MM-DD, both "
    "inclusive): they constrain the search server-side to pages PUBLISHED in "
    "that window, which is not always the period a page is ABOUT, so still "
    "check each result's date and content. `include_domains` restricts results "
    "to the listed sites and `exclude_domains` drops them (bare domains like "
    '"reuters.com"; subdomains match; up to {domains_max} per list) — reach for '
    "`include_domains` when only particular sources can settle the claim (a "
    "regulator, a standards body, the company's own site). Zero results usually "
    "means over-constrained filters: retry with fewer before concluding the web "
    "has nothing.\n"
)
_WEB_SEARCH_DESC_TAIL = (
    "Every result you rely on must be attributed with its URL inside a citation "
    'tag — <c src="https://…"></c> — the same tag wiki docs use; put every '
    "source for one claim in ONE tag, comma-separated. Treat snippets as claims by "
    "their source, not as facts: say who reported what, and never present a web "
    "figure as if it came from this organization's data."
)


def make_web_search_tool(engine: WebSearchGateway, *, today: datetime.date | None = None) -> Any:
    """Wrap a :class:`WebSearchGateway` as the LangChain ``web_search`` tool.

    The argument surface follows ``engine.filters_enabled``: the filter args
    exist only when the deployment's target speaks connector >= 1.2.0.
    """
    from langchain_core.tools import StructuredTool

    stamp = (today or datetime.datetime.now(datetime.timezone.utc).date()).isoformat()
    args_variant = (
        _WEB_SEARCH_DESC_FILTERS if engine.filters_enabled else _WEB_SEARCH_DESC_NO_FILTERS
    )
    description = (_WEB_SEARCH_DESC_HEAD + args_variant + _WEB_SEARCH_DESC_TAIL).format(
        today=stamp,
        max_chars=QUERY_MAX_CHARS,
        results_max=RESULTS_MAX,
        default=engine.default_max_results,
        domains_max=DOMAIN_FILTER_MAX,
    )

    def run(query: str, max_results: int, **filter_args: Any) -> Any:
        # Same convention as chat.tools/chat.sql: a failure comes back as the
        # tool's RESULT so the model can react (rephrase the query, drop a
        # filter, or tell the user the web lookup failed) instead of aborting
        # the run.
        try:
            return engine.search(query, max_results=max_results or None, **filter_args)
        except ValueError as e:  # bad arguments — concise + actionable
            return f"Error: {e}"
        except Exception as e:  # noqa: BLE001 - a tool error is feedback, not a crash
            log.warning("web_search failed", exc_info=True)
            return f"Error: web_search failed: {type(e).__name__}: {e}"

    if engine.filters_enabled:

        def web_search(
            query: str,
            max_results: int = 0,
            published_after: str = "",
            published_before: str = "",
            include_domains: list[str] | None = None,
            exclude_domains: list[str] | None = None,
        ) -> Any:
            return run(
                query,
                max_results,
                published_after=published_after,
                published_before=published_before,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
            )

    else:

        def web_search(query: str, max_results: int = 0) -> Any:  # type: ignore[misc]
            return run(query, max_results)

    return StructuredTool.from_function(
        func=web_search, name="web_search", description=description
    )


def build_web_search_engine(chat_config: Any) -> WebSearchGateway | None:
    """Build the gateway client from config, or ``None`` when not deploy-enabled.

    Both the flag and a gateway URL are required: with either missing the chat
    role carries no ``bedrock-agentcore:InvokeGateway`` grant, so offering the
    tool would only produce 403s the model can't act on.
    """
    if not getattr(chat_config, "web_search_enabled", False):
        return None
    url = getattr(chat_config, "web_search_gateway_url", "") or ""
    if not url.strip():
        log.warning("OKF_WEB_SEARCH_ENABLED is set but no gateway URL — web_search off")
        return None
    try:
        return WebSearchGateway(
            gateway_url=url,
            region=getattr(chat_config, "web_search_region", "us-east-1"),
            tool_name=getattr(chat_config, "web_search_tool_name", "") or "",
            default_max_results=getattr(chat_config, "web_search_max_results", 10),
            filters_enabled=getattr(chat_config, "web_search_filters_enabled", False),
        )
    except Exception:  # noqa: BLE001 - a misconfigured gateway must not break chat
        log.warning("could not build the web search client — web_search off", exc_info=True)
        return None
