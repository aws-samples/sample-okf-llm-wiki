// Direct transport to the chat AgentCore runtime — Sparky's approach, no AG-UI.
//
// The browser POSTs straight to the runtime's data-plane /invocations URL with a
// Cognito bearer (the scope-based okf-chat/invoke authorizer) and the AgentCore
// session-id header. We own the whole wire contract: a `type`-discriminated
// `input` envelope in, and either a raw streaming `Response` (for `send`, to be
// read by chatStream.js) or a JSON envelope (for history read/delete).
//
// AgentCore does NOT map the session-id header to the LangGraph thread_id, so the
// session id IS the client thread id (the server namespaces it per-user before
// touching the checkpoint). We make the session id equal the conversation id and
// pad it to the 33-char AgentCore floor.

const AWS_REGION = import.meta.env.VITE_AWS_REGION || ""
const CHAT_RUNTIME_ARN = import.meta.env.VITE_CHAT_RUNTIME_ARN || ""

// The chat runtime's data-plane /invocations URL, or "" if unconfigured (local
// dev without a deployed chat runtime) so the panel can render a disabled state.
export const CHAT_URL =
  AWS_REGION && CHAT_RUNTIME_ARN
    ? `https://bedrock-agentcore.${AWS_REGION}.amazonaws.com/runtimes/${encodeURIComponent(
        CHAT_RUNTIME_ARN
      )}/invocations?qualifier=DEFAULT`
    : ""

export const CHAT_CONFIGURED = Boolean(CHAT_URL)

// AgentCore requires the runtime-session-id header to be 33-256 chars. Our
// conversation ids are UUIDs (36 chars) which already satisfy that, but pad
// defensively so a shorter id (e.g. a test) still meets the floor.
export function sessionIdForThread(threadId) {
  const id = String(threadId || "")
  return id.length >= 33 ? id : (id + "-".repeat(33)).slice(0, 33)
}

// A fresh conversation id. crypto.randomUUID yields a 36-char id (satisfies the
// session-id floor with no padding).
export function newThreadId() {
  return crypto.randomUUID()
}

// POST one `input` envelope to the runtime, scoped to a conversation's session id.
// `getToken` is a function so the CURRENT access token is read per request (a
// conversation can outlive one token; react-oidc renews silently).
async function post(threadId, getToken, input, { signal } = {}) {
  if (!CHAT_URL) throw new Error("chat runtime is not configured")
  const token = getToken?.()
  return fetch(CHAT_URL, {
    method: "POST",
    signal,
    headers: {
      "Content-Type": "application/json",
      "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": sessionIdForThread(threadId),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ input }),
  })
}

// Send a chat message. Returns the raw streaming Response (SSE) for chatStream.js.
export async function sendMessageAPI({
  threadId,
  getToken,
  prompt,
  model,
  effort,
  features = null,
  datasetScope = null,
  signal,
}) {
  const input = { type: "send", prompt }
  if (model) input.model_id = model
  if (effort) input.effort = effort
  // Per-run opted-in optional tools (composer "+" menu). The runtime re-validates
  // against the deploy flag, so sending an unavailable feature is simply ignored.
  if (Array.isArray(features) && features.length > 0) input.features = features
  if (datasetScope) input.dataset_scope = datasetScope
  const res = await post(threadId, getToken, input, { signal })
  if (!res.ok) {
    const text = await res.text().catch(() => "")
    throw new Error(`chat request failed (${res.status}): ${text || "unknown error"}`)
  }
  return res
}

// Fetch a conversation's persisted history as { history: [chatTurns] }.
export async function fetchHistoryAPI({ threadId, getToken }) {
  const res = await post(threadId, getToken, { type: "get_session_history" })
  if (!res.ok) throw new Error(`failed to load history: ${res.status}`)
  const data = await res.json()
  return { history: Array.isArray(data?.history) ? data.history : [] }
}

// Delete a conversation's checkpoints on the runtime (the per-user thread index
// row is deleted separately via the Control API).
export async function deleteHistoryAPI({ threadId, getToken }) {
  const res = await post(threadId, getToken, { type: "delete_history" })
  if (!res.ok) throw new Error(`failed to delete history: ${res.status}`)
  return res.json()
}

// Re-subscribe to an IN-FLIGHT turn (the run keeps going server-side after a
// disconnect). Returns the raw streaming Response (SSE) like sendMessageAPI — the
// server replays buffered chunks then streams live; if nothing is running it emits
// a `no_active_stream` marker so the caller can fall back to history.
export async function resumeAPI({ threadId, getToken, signal }) {
  const res = await post(threadId, getToken, { type: "resume" }, { signal })
  if (!res.ok) throw new Error(`resume failed (${res.status})`)
  return res
}

// Explicitly STOP an in-flight turn on the runtime (the only thing that cancels a
// run — a dropped connection no longer does). Triggers the server-side checkpoint
// repair. JSON envelope: { type:"stop", stopped:bool }.
export async function stopAPI({ threadId, getToken }) {
  try {
    const res = await post(threadId, getToken, { type: "stop" })
    return res.ok ? res.json() : { stopped: false }
  } catch {
    return { stopped: false } // best-effort; the local stop still settles the UI
  }
}

// Optional keep-warm: reset the runtime's idle timer with no LLM call. Fire and
// forget; never affects the visible conversation (server emits only `end`).
export async function prepareAPI({ threadId, getToken }) {
  try {
    await post(threadId, getToken, { type: "prepare" })
  } catch {
    // keep-warm is best-effort — a failure just means a cold start next turn
  }
}
