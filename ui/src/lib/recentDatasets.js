// Per-user MRU of recently opened datasets (the sidebar/quick-nav "Recent
// datasets" group). localStorage-backed like the chat effort preference
// (chatModels.js) — this is device-local UX memory, not server state, so it
// needs no backend. Keyed by the Cognito `sub` so two users sharing a browser
// don't see each other's history; capped at 5, most recent first.

const KEY_PREFIX = "okf.recentDatasets.v1"
export const MAX_RECENT_DATASETS = 5

const keyFor = (sub) => `${KEY_PREFIX}:${sub || "anon"}`

export function loadRecentDatasets(sub) {
  try {
    const raw = localStorage.getItem(keyFor(sub))
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr)
      ? arr.filter((k) => typeof k === "string").slice(0, MAX_RECENT_DATASETS)
      : []
  } catch {
    // private mode / storage disabled — behave as "no history"
    return []
  }
}

// Move `key` to the front (deduped), persist, and return the new list.
export function pushRecentDataset(sub, key) {
  const next = [
    key,
    ...loadRecentDatasets(sub).filter((k) => k !== key),
  ].slice(0, MAX_RECENT_DATASETS)
  try {
    localStorage.setItem(keyFor(sub), JSON.stringify(next))
  } catch {
    // storage full / denied — the in-memory list still works this session
  }
  return next
}
