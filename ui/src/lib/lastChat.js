// The last chat conversation this user had open (device-local UX memory,
// localStorage-backed like the effort preference (chatModels.js) and recent
// datasets (recentDatasets.js) — not server state, so no backend). It lets
// "return to Chat" reopen the conversation you were last in, and survives a
// page reload even when the reload lands on a non-chat section (the URL only
// carries the thread while you're actually ON #/chat/<threadId>). Keyed by the
// Cognito `sub` so two users sharing a browser don't inherit each other's chat.

const KEY_PREFIX = "okf.chat.lastThreadId.v1"

const keyFor = (sub) => `${KEY_PREFIX}:${sub || "anon"}`

export function loadLastThread(sub) {
  try {
    const raw = localStorage.getItem(keyFor(sub))
    return raw && typeof raw === "string" ? raw : null
  } catch {
    // private mode / storage disabled — behave as "no last chat"
    return null
  }
}

export function saveLastThread(sub, threadId) {
  try {
    if (threadId) localStorage.setItem(keyFor(sub), threadId)
  } catch {
    // storage full / denied — the in-memory conversation still works this session
  }
}

export function clearLastThread(sub) {
  try {
    localStorage.removeItem(keyFor(sub))
  } catch {
    // nothing to do — a missing key already reads as "no last chat"
  }
}
