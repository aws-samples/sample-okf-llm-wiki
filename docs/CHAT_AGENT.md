# Chat Agent — an in-UI assistant over the wiki (AG-UI + AgentCore)

**Status:** design (not yet built) · **Owner:** TBD · **Date:** 2026-07-14

This doc is the design contract for a new component: an **interactive chat agent**
that lives in a side panel of the SPA and answers questions about the wiki
content. It is written to be read alongside `docs/ARCHITECTURE.md` (where it
becomes the 8th component), `docs/CONVENTIONS.md` (which gains the new item
shapes, env vars, and the AG-UI run contract), and `docs/API_REFERENCE.md` (the
third-party API shapes it depends on). Shapes marked **CONTRACT** are load-bearing
across services and must land in CONVENTIONS.md before code.

---

## 1. What & why

Today the wiki exposes knowledge to **external** agents over MCP
(`consumption_mcp/`, agent↔tools) and to **humans** as static markdown in the SPA
(`ui/`). There is no way for a human to *ask a question* and have an agent read
the bundle and answer. This component adds that: a **chat side-panel** where a
user talks to an agent that uses the existing consumption tools to read the
wiki, streams its answer + reasoning back, and remembers the conversation.

MCP and AG-UI are **complementary, not competing**: MCP is the agent↔tools
protocol (what `consumption_mcp` speaks); **AG-UI** (Agent-User Interaction) is
the agent↔user protocol — a typed SSE event stream a frontend renders
incrementally. The chat agent *emits* AG-UI to the browser and *consumes* the
consumption tools internally.

### Requirements (from the requester)

1. Inline agent in a **side panel** in `ui/`, chatting over wiki content.
2. Agent has access to the **consumption MCP tools**.
3. A **separate AgentCore runtime** (not harvest, not consumption-MCP).
4. Use **AG-UI** to minimize UI code.
5. **Memory** via a **DynamoDB checkpointer**.
6. **Streaming** answers, and **display the reasoning**.
7. Reuse **native shadcn** chat components where possible; fall back to
   hand-built (the Sparky pattern) for the rest.
8. A **prepare / keep-warm** call.
9. **History is per-user**.
10. Default scope = **whole wiki**; typing **`@` picks a dataset** to narrow the agent.

### Non-goals (v1)

- No write path to bundles (the agent is read-only over the wiki; annotations
  remain the existing feedback mechanism).
- No human-in-the-loop interrupts / tool approval (AG-UI supports it; deferred).
- No cross-user or shared conversations.

---

## 2. Verified technology decisions

Every choice below was checked against primary sources (mid-2026). Pinned
versions are the ones verified to interoperate.

| Concern | Decision | Rationale |
|---|---|---|
| Agent framework | **LangGraph** | "Checkpointer" is LangGraph's term; same family as harvest's deepagents; proven on AgentCore. |
| AG-UI emission (server) | **`ag-ui-langgraph`** adapter (PyPI `ag-ui-langgraph` 0.0.42) — `LangGraphAgent(name, graph)` | First-party; converts `graph.astream_events(version="v2")` into the full AG-UI stream (text + tool + **reasoning**). No hand-translation. |
| Server shape | **Raw FastAPI** app mounting the adapter — **not** the SDK `AGUIApp` | We need full CORS control for a browser-direct call (see §5); a raw app also matches how `consumption_mcp` and `harvest` structure their entrypoints. |
| AG-UI protocol pkg | `ag-ui-protocol` **0.1.19** | Supplies `RunAgentInput` + the event enum. |
| Checkpointer | `langgraph-checkpoint-aws` **`DynamoDBSaver`** | Requirement #5. Note: this package's *headline* backends are Bedrock Session Mgmt / AgentCore Memory / Valkey; `DynamoDBSaver` is a valid-but-secondary saver — **confirm its exact table schema before provisioning** (§7, open item O1). |
| Frontend | **assistant-ui** (`@assistant-ui/react` 0.14.26) + **`@assistant-ui/react-ag-ui`** 0.0.44 | The only genuinely shadcn-native option: `npx shadcn add @assistant-ui/thread` copies Radix-based components into our tree that inherit our Tailwind v4 tokens. `useAgUiRuntime({ agent })` renders streaming + reasoning + tool-calls out of the box. |
| AG-UI client SDK | `@ag-ui/client` **0.0.57** (`HttpAgent`) | Transitively used by assistant-ui; carries our custom URL + auth headers. |
| Model config | Reuse `okf_core/harvest_models.py`; **extract** the harvest provider factory into a shared module | Opus↔GPT split + per-provider reasoning config already solved (§6). |

**Dropped (hallucinated) API:** the verifier found `useAgUiRuntime` has **no**
`autoCancelPendingToolCalls` option — real options are `agent, logger,
showThinking, onError, onCancel, adapters`. Do not use it.

**Reasoning events:** target the **`REASONING_*`** family
(`REASONING_START`, `REASONING_MESSAGE_START` with `role:"reasoning"`,
`REASONING_MESSAGE_CONTENT{delta}`, `REASONING_MESSAGE_END`, `REASONING_END`).
The older `THINKING_*` names are **deprecated for removal in AG-UI 1.0** — never
build on them. The adapter's `resolve_reasoning_content()` auto-extracts
reasoning from **both** Bedrock Converse (`reasoning_content`) and OpenAI
Responses (`reasoning.summary[].text`), so enabling extended thinking on either
provider produces `REASONING_*` automatically.

---

## 3. Where it fits

```
                    ┌───────────────────────────────────────────┐
  Browser (ui/)     │  AgentCore: chat runtime  (NEW, protocol HTTP)
  ┌────────────┐    │  raw FastAPI + ag-ui-langgraph LangGraphAgent
  │ ChatPanel  │    │    ├─ LangGraph graph (ChatBedrockConverse | ChatOpenAI)
  │ assistant  │───▶│    ├─ tools = ConsumptionTools (in-process, §4)
  │ -ui +      │ AG │    └─ DynamoDBSaver checkpointer (thread_id, §8)
  │ HttpAgent  │◀───│  Cognito JWT authorizer (scope okf-chat/invoke)
  └────┬───────┘ SSE└──────────────┬───────────────┬────────────┘
       │                           │ read bundle   │ query
       │ REST (existing)           ▼               ▼
       │  GET /chat/threads   S3 bundle bucket   S3 Vectors + registry (DDB)
       ▼
  control_api (extended): per-user thread list / rename / delete
```

Two backend surfaces, mirroring Sparky's split of *streaming* vs *history*:

- **Chat runtime (new):** the live AG-UI stream. Writes checkpoints + a
  per-user thread-index row.
- **control_api (extended):** Cognito-authed REST to **list/rename/delete** a
  user's past conversations (the browser can't query DynamoDB directly). This is
  the one place we don't use AG-UI, because listing history is a request/response,
  not a run.

---

## 4. Backend service — `services/chat/`

New service, structured like `consumption_mcp/` (`pyproject.toml`,
`requirements.txt`, `Dockerfile`, `src/chat/…`, `tests/…`).

### 4.1 Tools: reuse `ConsumptionTools` in-process (no MCP hop)

`consumption_mcp` deliberately separates **pure tool logic** (`tools.py`,
`ConsumptionTools`) from the FastMCP wiring (`server.py`). The chat agent
**imports `ConsumptionTools` directly** and wraps each method as a LangChain
`@tool`. No MCP protocol, no machine-to-machine token, no network hop — the chat
runtime already holds the same IAM grants as the consumption role (bundle read,
Bedrock embed, S3 Vectors query, registry read).

> **Decision:** in-process reuse (option B). Alternative (option A) — have the
> chat agent call the real `consumption_mcp` runtime over MCP via
> `langchain-mcp-adapters` with an M2M `okf-mcp/invoke` token — is only worth it
> if you want the chat agent to cross the actual MCP trust boundary. For an
> in-house agent that shares the IAM posture, B is simpler and cheaper. The MCP
> runtime stays for *external* consumers.

Tools exposed to the agent (all delegating to `ConsumptionTools`): `list_domains`,
`list_declared_domains`, `search_domains`, `list_directory`, `read_page`,
`get_backlinks`, `glob`, `grep`, `semantic_search`.

### 4.2 Dataset scoping (`@`-mention)

Default: the agent may read the whole wiki (all datasets). When the user `@`-picks
a dataset, the SPA sends the scope in the AG-UI **`forwardedProps`** (arbitrary
dict, surfaced as `input.forwarded_props`), e.g.
`forwardedProps: { dataset_scope: { data_domain, dataset } }`. The graph reads it
and, when set, (a) injects it into the system prompt and (b) pre-binds
`data_domain`/`dataset` defaults on the tool wrappers so the agent stays in-scope.
Scope is *advisory context* per run — it is **not** a security boundary (the IAM
role can read any bundle); it steers relevance, mirroring how the harvest picker
passes per-run model choice.

### 4.3 Server entrypoint (raw FastAPI, Sparky-style CORS)

The browser calls the runtime **directly** (§5), so the app must own CORS on the
**streaming response**, not just via middleware — `CORSMiddleware` can be
bypassed by `StreamingResponse`. Replicate Sparky's three-layer approach:

```python
# services/chat/src/chat/server.py  (shape; imports deferred like consumption_mcp)
app = FastAPI(title="OKF Chat Agent")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"])

@app.options("/invocations")            # explicit preflight
async def _preflight(): return {"ok": True}

@app.get("/ping")                        # AgentCore container health probe
async def _ping(): return {"status": "Healthy"}

_agent = LangGraphAgent(name="okf-chat", graph=build_graph())   # ag_ui_langgraph

@app.post("/invocations")
async def invocations(request: Request):
    run_input = RunAgentInput.model_validate(await request.json())
    user_sub  = decode_jwt_unverified(request.headers["Authorization"])  # AgentCore already validated
    # thread ownership + per-user checkpoint namespacing — see §8
    async def gen():
        async for event in _agent.run(_scoped_input(run_input, user_sub)):
            yield encode_sse(event)     # ag-ui EventEncoder -> "data: {json}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "Connection": "keep-alive",
        "X-Accel-Buffering": "no",                    # defeat proxy buffering
        "Access-Control-Allow-Origin": "*",           # CORS ON THE STREAM itself
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    })
```

- Port **8080**, `protocol_configuration.server_protocol = "HTTP"` (the container
  emits AG-UI as its HTTP/SSE body; there is no distinct "AGUI" protocol needed
  when we own the FastAPI app).
- `RUN_STARTED` … `RUN_FINISHED` bracketing and `RUN_ERROR`-on-exception are
  handled by the adapter; the FastAPI layer must still emit a terminal error
  frame if `.run()` throws before the adapter starts.
- Dockerfile mirrors `consumption_mcp/Dockerfile` (ARM64, `okf_core`+`okf_aws`
  copied as siblings, ADOT `opentelemetry-instrument` entrypoint). Add
  `LANGSMITH_*` OTEL-only env like the harvest runtime, since this is LangChain.

---

## 5. Auth — browser-direct, no proxy

The browser calls the AgentCore data-plane URL **directly** with a Cognito JWT
bearer — the same pattern `consumption_mcp` already uses and the same URL shape
the SPA already builds in `ui/src/views/CredentialsView.jsx:58-63`:

```
https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<url-encoded-arn>/invocations?qualifier=DEFAULT
```

**No API Gateway, no Lambda signer, no SigV4 in the browser.** CORS on the
data-plane endpoint is handled by the app itself (§4.3, `allow_origins=["*"]`) —
resolved, not an open risk.

- **Inbound authorizer:** `custom_jwt_authorizer` with the same OIDC discovery
  URL, but a **new dedicated scope `okf-chat/invoke`** (requester's choice —
  independently revocable, separates "read tools" from "chat"). Requires a new
  `aws_cognito_resource_server "chat"` + adding its scope to the SPA app client's
  `allowed_oauth_scopes` (see §9).
- **Token:** the SPA sends `auth.user?.access_token` (scope-based authorizer, so
  the **access** token — not the id token that control_api's `aud`-based
  authorizer wants). The SPA must **request** the `okf-chat/invoke` scope in its
  OIDC `scope` string (`ui/src/lib/auth.js`).
- **User identity:** the container decodes the JWT **without re-verifying the
  signature** (AgentCore already validated it) to read `sub` → `user_sub`, used
  for checkpoint namespacing + thread ownership (§8). Same technique as Sparky.
- **`request_header_allowlist = ["Authorization"]`** on the runtime so the header
  reaches the container.

---

## 6. Model configuration

Requirement: pick model (Opus 4.8 / GPT-5.6 Sol) + effort like harvest does, but
**model is pinned per conversation** — switching model **starts a new thread**,
because Opus and GPT checkpoints are **not portable** (provider-specific thinking
signatures + tool/reasoning content formats; resuming across providers makes the
new provider's API reject the stored history).

### 6.1 Reuse + extract

- **Catalog/validation:** reuse `okf_core/harvest_models.py` as-is (it is pure and
  provider-agnostic despite the name): the `{model, label, efforts, default_effort}`
  schema, `EFFORT_LEVELS`, and `validate_model_effort`.
- **Provider factory:** **extract** `_build_model` / `_is_openai_model` /
  `_build_bedrock_converse` / `_build_mantle_openai` / `_mantle_token_provider` /
  `_thinking_fields` / `_gpt_effort` out of `services/harvest/src/harvest/agent.py`
  into a shared module (proposed: `okf_aws` or a new `okf_core/model_factory.py`)
  so harvest and chat build identical model clients. Harvest keeps its
  `resolve_model_config` (harvest-specific env names); chat gets its own
  `OKF_CHAT_*` resolver over the same builders.

### 6.2 Separate catalog

New `var.chat_model_catalog` (requester's choice), independent of
`var.harvest_model_catalog` — chat may offer a lighter/faster model set than the
heavyweight authoring agent. Fans out the **same three ways** harvest's does
(§9): base64 JSON → `ui_env` (`VITE_CHAT_MODEL_CATALOG`), raw JSON → the validator,
default → the runtime env (`OKF_CHAT_MODEL`).

### 6.3 Where validation lives (trust boundary)

Harvest validates `(model, effort)` in the Control API before it reaches
`bedrock:InvokeModel`. The chat path is **browser-direct with no proxy**, so the
**runtime itself** validates the pinned `(model, effort)` against
`OKF_CHAT_MODEL_CATALOG` at session creation, before the first `InvokeModel`.
Same principle (validate server-side, never trust the client), enforced one hop
later. The IAM role grants Mantle only if a catalog entry is `openai.*` — reuse
harvest's `local.*_mantle_enabled` pattern (`infra/compute/agentcore_iam.tf:14`).

### 6.4 Pinning

The model/effort travel in `forwardedProps` on the **first** run of a thread. The
graph stamps them into the checkpoint state (like harvest stamps model/effort onto
its status row at the `running` transition). On subsequent runs of the same thread
the pinned model is read from state and the client-sent value is ignored. The UI
locks the model picker once a thread has messages and offers "new chat to change
model" (§10).

---

## 7. DynamoDB — checkpointer + per-user thread index  **(CONTRACT)**

Two concerns, two stores:

### 7.1 Checkpointer table (`okf-chat-checkpoints`)

Managed by `DynamoDBSaver`. **Schema resolved from the installed
`langgraph-checkpoint-aws` source + empirically validated against moto** (was
open item O1):

- **`PK`** — String, **HASH** key. (attribute names are UPPERCASE)
- **`SK`** — String, **RANGE** key.
- **TTL attribute: `ttl`** (lowercase, epoch seconds) — written only when the
  saver is constructed with `ttl_seconds=...`. Enable DynamoDB TTL on `ttl`.
- **No GSI.** Checkpoints and writes share the one table, distinguished by PK
  prefix (`CHECKPOINT_<thread_id>` vs `WRITES_<thread_id>#<ns>#<ckpt_id>`),
  queried with `begins_with(SK, …)`.
- Optional large-payload S3 offload via `s3_offload_config` (defer for v1).

`PAY_PER_REQUEST`, same CMK/PITR posture as the other tables. `put`/`get`/`list`/
`delete_thread` verified working against this schema with moto (so offline tests
drive the real saver).

### 7.2 Per-user thread index

`DynamoDBSaver`'s table is keyed by `thread_id`, so it can't answer "list user
X's conversations." A small index table does. Following the **annotations**
precedent (`infra/durable/dynamodb.tf:154`), use **structural per-user isolation**
in the partition key and a **dedicated table** (blast-radius isolation from the
durable registry):

- **Table:** `okf-chat` (new).
- **`pk = "CHAT#<user_sub>"`**, **`sk = "THREAD#<thread_id>"`**.
- Attributes: `title`, `model`, `effort`, `dataset_scope` (nullable),
  `created_at`, `updated_at`, optional `expires_at` (TTL).
- Isolation is structural: a user's `Query` on `pk = CHAT#<their sub>` can only
  ever return their own threads — no cross-user read path exists, exactly like
  annotations.

The **chat runtime** writes here (create-on-first-run, touch `updated_at`/`title`
per turn). **control_api** reads/deletes here for the UI list.

### 7.3 Per-user checkpoint isolation (critical)

The browser sends `threadId` in `RunAgentInput`, so a malicious user could send
**someone else's** `threadId` and resume their conversation. The checkpointer
keyed on `thread_id` alone gives **no** user isolation. Fix — **namespace the
checkpoint key with the user's sub** so it is structurally impossible to touch
another user's state:

> effective checkpoint `thread_id` = `f"{user_sub}:{client_thread_id}"`

Implement by rewriting `input.thread_id` (or the graph config) inside
`server.py`'s `_scoped_input` before handing to `LangGraphAgent.run` (a thin
`LangGraphAgent` subclass overriding config construction is the clean seam). The
AG-UI `RUN_STARTED`/`RUN_FINISHED` echo can keep the client's original id; only
the checkpoint key is namespaced. This matches the annotations philosophy —
isolation in the key, not in a check that can be forgotten. (A belt-and-suspenders
ownership lookup against `okf-chat` is optional once the key is namespaced.)

---

## 8. Sessions, threads, streaming, reasoning

**The load-bearing wiring fact:** AgentCore's
`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header is **NOT** auto-mapped to
LangGraph `thread_id`. The adapter keys the checkpointer on
`RunAgentInput.threadId` → `config.configurable.thread_id`. So:

- The **browser must send a stable `threadId` per conversation** on every turn.
  Simplest: `threadId == runtimeSessionId` (one value drives both AG-UI thread and
  AgentCore microVM affinity). Get this wrong (fresh UUID per turn) and every turn
  starts a new thread → history lost.
- `runtimeSessionId` must be **≥ 33 chars** (AgentCore requirement) — pad/derive
  accordingly on the client.

**Streaming + reasoning** come free from the adapter (`astream_events` v2 →
`TEXT_MESSAGE_*`, `TOOL_CALL_*`, `REASONING_*`). On the client, assistant-ui's
`showThinking` (default **true**) renders `REASONING_*` deltas in a separate,
collapsible pane keyed by `message_id`, and tool calls as cards — no custom
reducer. Enable extended thinking on the model (Converse `thinking.type=adaptive`
via the shared `_thinking_fields`; GPT `reasoning_effort`) to produce reasoning.

---

## 9. Keep-warm / prepare

**Important nuance (verified):** AG-UI has **no native `prepare`** concept, and
**`GET /ping` does NOT keep a session warm** — `/ping` is AgentCore's container
health probe; only a real invocation to the *same* `runtimeSessionId` resets the
idle timer. Options, in preference order:

1. **Rely on the durable checkpointer (default).** After the idle stop
   (configurable, default 15 min) a new turn cold-starts a fresh microVM and the
   `DynamoDBSaver` **rehydrates** the thread from `thread_id`. A cold start costs
   *latency*, not *history*. Set `lifecycle_configuration.idle_runtime_session_timeout`
   generously (harvest uses 3600s; chat can use ~900–1800s).
2. **Optional active keep-warm** (only if cold-start latency is unacceptable): the
   requested "prepare call" is a **real minimal invoke** to the same
   `runtimeSessionId`+`threadId`, gated by a `forwardedProps` flag
   (`{ prepare: true }`) that the graph **short-circuits** (returns immediately,
   no LLM call, emits `RUN_STARTED`→`RUN_FINISHED`). Fire it debounced on first
   keystroke and every ~300s while there is draft text (Sparky's cadence),
   comfortably under the idle window. **Not** `GET /ping`.

Recommend (1) always; add (2) behind a flag if latency demands it.

---

## 10. Frontend — `ui/`

JavaScript SPA, Vite multi-entry, shadcn/ui + Tailwind v4, `react-oidc-context`.

- **Install:** `npx shadcn@latest add @assistant-ui/thread @assistant-ui/thread-list`
  (+ base `avatar collapsible` etc.) → components land in `src/components/ui/` (or
  `src/components/assistant-ui/`) using **our existing tokens** (`components.json`
  style `radix-rhea`, baseColor `mist`) — zero restyle. npm: `@assistant-ui/react`,
  `@assistant-ui/react-ag-ui`, `@ag-ui/client`, `@assistant-ui/react-markdown`.
- **Placement:** a `ChatPanel` inside `SidebarInset` in `ui/src/App.jsx:513`, as a
  right-hand flex column (persistent) — it gets the active dataset (`route.selectionKey`)
  for free. Style precedent: the right-hand `Sheet` in
  `ui/src/views/BrowseView.jsx:311-336`.
- **Transport (auth + session headers):** build an `HttpAgent` with a **custom
  `fetch`** so the token is re-read per request (verified cleanest refresh hook):

  ```js
  const agent = new HttpAgent({
    url: `https://bedrock-agentcore.${region}.amazonaws.com/runtimes/${encodeURIComponent(arn)}/invocations?qualifier=DEFAULT`,
    fetch: (u, init) => fetch(u, { ...init, headers: {
      ...init.headers,
      Authorization: `Bearer ${auth.user?.access_token}`,
      "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": sessionId,   // == threadId, >=33 chars
    }}),
  });
  const runtime = useAgUiRuntime({ agent });   // showThinking defaults true
  // <AssistantRuntimeProvider runtime={runtime}><Thread /></AssistantRuntimeProvider>
  ```
- **`@`-mention dataset picker:** reuse `DatasetPicker` (`ui/src/App.jsx:128-193`)
  — a cmdk `Command`-in-`Popover` over the same `datasets` list (`api.listDomains()`).
  Selected dataset → `forwardedProps.dataset_scope`.
- **Model picker:** shown on the empty-composer / **new-chat** state; **locked once
  the thread has messages**. Changing model creates a **new `threadId`** (§6). Read
  options from `VITE_CHAT_MODEL_CATALOG` via a `chatModels.js` cloned from
  `ui/src/lib/harvestModels.js`.
- **History:** a `ThreadList` fed by new control_api routes (§11). Per-user by
  construction (server filters on JWT `sub`).
- **Env:** add `VITE_CHAT_RUNTIME_ARN`, `VITE_CHAT_SCOPE` (`okf-chat/invoke`),
  `VITE_CHAT_MODEL_CATALOG` to `ui_env` (§9 infra). No new HTML entry needed.
- Markdown/scroll: assistant-ui brings its own; our `.okf-prose` styles remain
  available if we swap renderers.

---

## 11. control_api additions (per-user history) — BUILT

Cognito-authed routes (id-token `aud` authorizer, like the rest of control_api),
querying the `okf-chat` index table filtered by the caller's `sub`:

- `GET /chat/threads` → the user's conversations (title, model, effort, scope,
  timestamps), newest-updated first, soft-deleted rows skipped.
- **`PUT`** `/chat/threads/{thread_id}` → rename. **PUT, not PATCH** — the API
  Gateway CORS `allow_methods` + the handler `CORS_HEADERS` enumerate
  GET/PUT/POST/DELETE/OPTIONS, so PATCH would fail preflight.
- `DELETE /chat/threads/{thread_id}` → delete the index row **and purge the
  conversation's LangGraph checkpoint items**. The purge is done directly on
  DynamoDB (Query the exact `CHECKPOINT_<sub:thread>` PK + Scan the
  `WRITES_<sub:thread>#…` prefix, BatchDelete) rather than via
  `DynamoDBSaver.delete_thread`, so **control_api stays free of the langgraph
  dependency**. It targets the sub-namespaced key (`<sub>:<thread_id>`) and is
  best-effort (an orphaned checkpoint is TTL-reaped anyway).

The item shape + keys live in `okf_core/chat_threads.py` (shared with the chat
runtime's index writer — single source of truth, like `okf_core/annotations.py`).
control_api's role gains `dynamodb:Query/Scan/DeleteItem/UpdateItem` on both
`okf-chat` and `okf-chat-checkpoints`; env `OKF_CHAT_THREADS_TABLE` +
`OKF_CHAT_CHECKPOINT_TABLE`.

The chat **runtime** writes the index row on each turn (best-effort
`touch_thread`: `SET updated_at/model/effort/scope`, `if_not_exists` on
`created_at`/`title` so a UI rename survives) — a failed index write never breaks
the chat run.

---

## 12. Infra changes

**`infra/durable/`**
- `dynamodb.tf`: `aws_dynamodb_table "chat_checkpoints"` (schema per O1) +
  `aws_dynamodb_table "chat"` (pk/sk, TTL on `expires_at`), same CMK/PITR posture.
- `cognito.tf`: `aws_cognito_resource_server "chat"` (identifier `okf-chat`, scope
  `invoke`); add `aws_cognito_resource_server.chat.scope_identifiers[0]` to the web
  client's `allowed_oauth_scopes` (`cognito.tf:57`).
- `outputs.tf`: export the chat scope + table ARNs to the compute stack.

**`infra/compute/`**
- `agentcore_runtimes.tf`: `aws_bedrockagentcore_agent_runtime "chat"`, gated on a
  new `var.chat_image_uri`, `protocol_configuration.server_protocol = "HTTP"`,
  `network_configuration.network_mode = "PUBLIC"`, `custom_jwt_authorizer` with
  `allowed_scopes = [chat_scope]`, `request_header_allowlist = ["Authorization"]`,
  `lifecycle_configuration` (idle ~900–1800s), `OKF_CHAT_*` env.
- `agentcore_iam.tf`: `aws_iam_role "chat"` = baseline + consumption grants
  (bundle read, Bedrock embed, S3 Vectors query, registry read) + `bedrock:InvokeModel*`
  + conditional Mantle (reuse the `*_mantle_enabled` local over `chat_model_catalog`)
  + DDB read/write on both chat tables.
- `variables.tf`: `chat_image_uri`, `chat_model_catalog`, `chat_model`,
  `chat_effort`, `chat_mantle_region`, `chat_max_tokens`, idle timeout.
- `outputs.tf`: add `chat_runtime_arn`; add `VITE_CHAT_RUNTIME_ARN`,
  `VITE_CHAT_SCOPE`, `VITE_CHAT_MODEL_CATALOG` to `ui_env`.
- `control_api.tf`: pass `OKF_CHAT_THREADS_TABLE`.

**`scripts/deploy.sh`:** `images` stage builds/pushes the ARM64 `chat` container
alongside harvest + consumption.

---

## 13. CONVENTIONS.md additions

- **`okf-chat` item shapes** (§7.2) under "DynamoDB tables".
- **`okf-chat-checkpoints`** note (DynamoDBSaver-owned; schema per package).
- **AG-UI run contract:** the `/invocations` request (`RunAgentInput`:
  `threadId`, `runId`, `messages`, `tools`, `context`, `state`, `forwardedProps`),
  the `text/event-stream` response, the required `RUN_STARTED…RUN_FINISHED`/`RUN_ERROR`
  bracketing, and our `forwardedProps` keys (`dataset_scope`, `model`, `effort`,
  `prepare`). Note `threadId == runtimeSessionId` and the ≥33-char rule.
- **`OKF_CHAT_*` env vars** in the env table:
  `OKF_CHAT_MODEL`, `OKF_CHAT_EFFORT`, `OKF_CHAT_MAX_TOKENS`,
  `OKF_CHAT_MANTLE_REGION`, `OKF_CHAT_MODEL_CATALOG` (raw JSON),
  `OKF_CHAT_CHECKPOINT_TABLE`, `OKF_CHAT_THREADS_TABLE`.
- **New scope** `okf-chat/invoke` in "HTTP and auth".

---

## 14. Testing (offline-first, like the rest of the repo)

- **`services/chat/tests/`** (from its own dir, `pythonpath=["src"]`): tool-wrapper
  delegation to a fake `ConsumptionTools`; `forwardedProps` scope injection;
  model/effort **validation** + **pinning** (client override ignored after turn 1);
  per-user checkpoint **namespacing** (`user_sub` prefix); server CORS headers on
  the streaming response; `RUN_ERROR` on tool exception. Mock the LLM (no Bedrock).
- **Shared model factory:** move harvest's existing factory tests with it; assert
  `openai.*`→Mantle and `anthropic.*`→Converse still hold for both callers.
- **control_api:** `/chat/threads` filters by `sub` (moto DynamoDB), can't read
  another user's rows.
- **UI:** `npm run build` + `lint`; a light test that the `HttpAgent` custom-fetch
  attaches both headers.
- Wire into `scripts/run_tests.sh`. Bedrock, S3 Vectors, live AG-UI streaming, and
  AgentCore hosting need a real account (as with harvest/consumption).

---

## 14b. Optional read-only SQL tool (`run_sql`) — BUILT

An OPT-IN capability that lets the chat agent run **read-only** SQL over the Glue
catalog via Athena — the one chat tool that touches *source data* (every other
tool reads the authored bundle). Off by default; gated **twice**, both
server-side, so a client string can never enable it alone:

1. **Deploy-time** — Terraform `var.enable_chat_sql` (default `false`). When on,
   the chat runtime's IAM role gains **catalog-wide read-only** Glue/Athena +
   Athena results-bucket write (no source-data write), and `OKF_CHAT_SQL_ENABLED`
   is set. When off, the role has no Glue/Athena grants, so the tool would 403 —
   and the runtime doesn't even offer it.
2. **Per-conversation** — the browser opts in via `features: ["sql"]` on the
   `send` envelope (the composer's "+" menu → SQL chip). The runtime adds the tool
   only when **both** the deploy flag AND the per-run opt-in are present
   (`server.make_agent_factory`).

**Read-only** is enforced two ways (defense in depth): the IAM role carries no
write grants, AND `chat/sql.py:is_read_only` rejects anything but a single
`SELECT`/`WITH`/`SHOW`/`DESCRIBE`/`EXPLAIN` (comments stripped first, so a
smuggled second statement fails) — a non-read query gets a clean error instead of
an opaque permission failure.

**Catalog-wide, unlike harvest.** Harvest pins each invocation to one database via
a scoped STS session (`harvest_data` role). Chat SQL is *not* pinned — the model
writes fully-qualified `"db"."table"` references and may query any database. An
`@`-mention `dataset_scope` is used only as the DEFAULT database for unqualified
names + advisory prompt context, **not** a security boundary. (Chosen for UX — SQL
works without first picking a dataset. Tighten by leaving the deploy flag off.)

Results are capped at `OKF_CHAT_SQL_MAX_ROWS` (default 200) with a `truncated`
flag, to bound a turn's token cost. New env: `OKF_CHAT_SQL_ENABLED`,
`OKF_CHAT_SQL_MAX_ROWS`, and the harvest-shared `OKF_ATHENA_WORKGROUP` /
`OKF_ATHENA_OUTPUT` (only meaningful when SQL is on). New UI env:
`VITE_CHAT_SQL_ENABLED` (gates the composer's "+" → SQL affordance).

## 14c. Inline charts (`render_chart`) — BUILT

The agent can show a **chart inline** in its answer when a visual carries the point
better than prose (comparisons, trends, parts-of-a-whole, distributions). Unlike
every other tool, `render_chart` does **no server work** and is **always on** (no
deploy flag, no per-run opt-in) — there's nothing to gate because the runtime never
executes anything: the model writes chart **"script code"** and the *browser*
renders it.

> **Deliberate deviation — no interrupt round-trip.** An earlier design sketch had
> a LangChain `interrupt` round-trip (a `before_tool`/`awrap_tool_call` middleware
> pauses the graph on a `render_chart` call, the UI renders and returns ok/error via
> `Command(resume=…)`, and the model learns whether the visual succeeded). This was
> **intentionally dropped** for a simpler ack-only design: the chart model is
> accurate enough that the added latency + middleware/streaming machinery isn't
> worth it, and a render failure is surfaced to the *user* as a contained inline
> error rather than fed back to the model. The interrupt mechanism was verified to
> work on the installed langgraph/langchain (spikes during development), so it can
> be added later if a use case needs the model to react to render outcomes; the tool
> would move from returning an immediate ack to raising an interrupt. Recorded here
> so the absence is understood as a choice, not an oversight.

**Flow.** The model calls `render_chart(code, title)` where `code` is JavaScript
that calls a helper the UI provides — `renderChart(el, spec)` — with a small
declarative `spec` (`type`, `labels`, `series`, optional `title`/`stacked`/axis
labels). The tool (`chat/charts.py`) validates the code is non-empty and returns an
**ack** (`{"status":"rendered", …}`) — NOT a render result. The ack tells the model
the visual was handed off so it keeps writing its answer; we do **not** round-trip
the render outcome back to the model (no human-in-the-loop interrupt — the chart
model is accurate enough that the latency + machinery isn't worth it). The full
authoring contract (the `renderChart` API, the spec shape, the palette rules) lives
in the **tool description**, not the system prompt, so `SYSTEM_PROMPT` stays a
static, brace-free, cacheable prefix; the prompt's short `<charts>` block only
covers *when* to chart and the "real numbers only / inherit the app palette"
guardrails.

**Rendering + confinement (UI).** A `render_chart` tool call rides the normal typed
tool-chunk path, but `buildMessageBlocks` **lifts it out of the tool/think timeline
into its own inline `chart` block** (in call order; the ack result is dropped).
`ChatMessage` renders that block with `ChartFrame`, which runs the model's `code`
inside a **sandboxed `<iframe>`** — `sandbox="allow-scripts"` with **no**
`allow-same-origin`, so the frame is a unique opaque origin: model JS can't reach
the app DOM, cookies, or the Cognito token in the parent. The frame carries its own
strict **CSP** (`default-src 'none'; connect-src 'none'; …`) that denies all
network — it only draws to a canvas and reports its height/status back via
`postMessage`. This is the security AND crash boundary; three layers guard it: the
sandbox + the frame's own CSP, a status→contained-error-card path, and a React
error boundary around the whole component (so even a failure to *build* the frame
can't crash the chat).

**CSP trade-off (`srcdoc` + `'unsafe-inline'`).** The frame is loaded via the
`srcdoc` attribute (like Sparky's canvas). A `srcdoc` (local-scheme) iframe
**inherits the embedding page's CSP** and cannot carry its own *looser* policy — so
under a strict app CSP its inline scripts (Chart.js + the render code) are blocked
(`"Content-Security-Policy: blocked an inline script … about:srcdoc"`). Loading the
same document from a `blob:`/`data:` URL does **not** help: those local-scheme
documents inherit the initiator's CSP too. The only ways to run inline scripts in
the frame are therefore (a) allow `'unsafe-inline'` in the app's `script-src`, or
(b) serve the frame from a **separate origin** whose own CSP header is permissive.
We take (a) — `infra/compute/ui.tf` sets `script-src 'self' 'unsafe-inline'` — for
simplicity, accepting that it weakens the app-wide inline-script protection; the
comment there documents the (b) alternative for a stricter deployment. The chart
frame stays confined regardless of this choice: the sandbox (opaque origin, no
`allow-same-origin`) + the frame's own `<meta>` CSP (`default-src 'none';
connect-src 'none'`) keep it away from the app DOM, the network, and the Cognito
token — `'unsafe-inline'` only lets *inline `<script>`* run, it does not re-grant
same-origin access or network.

**Charting library + theme.** Chart.js is **vendored** (`ui/src/vendor/chart.umd.min.js`,
v4.5.1, MIT) and **inlined** into the iframe `srcdoc` via a `?raw` import — the
sandboxed frame can't fetch it, and Chart.js's `exports` map blocks a deep
`?raw` import straight from `node_modules` (hence the local vendored copy). The
in-frame `renderChart` helper (`ui/src/lib/chartIframe.js`) maps the spec onto
Chart.js and applies the **app palette + theme**: because the opaque-origin frame
can't read the parent's computed styles, `resolveChartPalette` resolves the app's
`--chart-1…5` + a few UI tokens (oklch) to concrete `rgb` triples in the parent
(via a 1×1 canvas painter) and injects them, and the frame rebuilds on a light/dark
switch. The model is told **not** to hard-code colors, so charts stay on-brand in
both themes. Supported types: `bar`, `line`, `area`, `pie`, `doughnut`, `radar`,
`scatter`. No new env, no infra change, no new runtime dependency (server-side the
tool is pure `langchain_core`).

## 15. Open items / risks

- **O1 — `DynamoDBSaver` table schema. RESOLVED** (§7.1): `PK`(HASH,S)+`SK`(RANGE,S),
  TTL attr `ttl`, no GSI, single table for checkpoints+writes. Verified against
  the installed package source and moto.
- **O2 — checkpoint deletion. RESOLVED:** `DynamoDBSaver.delete_thread(thread_id)`
  (+ `adelete_thread`, `prune`, `copy_thread`) exist, so `DELETE /chat/threads`
  purges state, not just the index row.
- **O3 — thread-id namespacing seam.** Verify the cleanest way to inject
  `user_sub:` into `config.configurable.thread_id` under `ag-ui-langgraph`
  (subclass `LangGraphAgent` vs. rewrite `input.thread_id`), and that
  `RUN_STARTED/FINISHED` still echo the client's original id so assistant-ui
  reconciles correctly.
- **O4 — pre-1.0 churn.** `@ag-ui/client` 0.0.57, `ag-ui-langgraph` 0.0.42,
  `@assistant-ui/react-ag-ui` 0.0.44 are all 0.0.x; pin exact versions and expect
  API drift (some interrupt APIs already deprecated).
- **O5 — first streaming client in the SPA.** No existing SSE/`fetch`-stream code
  in `ui/`; assistant-ui owns it, but this is new operational surface (timeouts,
  aborts on panel close).

---

## 16. Suggested phasing

1. **Shared model factory** — extract from harvest, keep harvest green (pure refactor + tests).
2. **`services/chat/` backend** — graph + `ConsumptionTools` wrappers + `DynamoDBSaver` + raw-FastAPI/AG-UI server; offline tests. Confirm O1/O3 here.
3. **Infra** — durable tables + Cognito scope; compute runtime + IAM + env; deploy.sh image stage. `terraform validate` both stacks.
4. **control_api** — `/chat/threads` list/rename/delete.
5. **UI** — assistant-ui `ChatPanel` in `SidebarInset`, transport + headers, `@`-picker, model picker/pinning, history, keep-warm. `npm run build`.
6. **Docs** — fold §13 into CONVENTIONS.md; add the component to ARCHITECTURE.md; record the third-party shapes in API_REFERENCE.md.
```
