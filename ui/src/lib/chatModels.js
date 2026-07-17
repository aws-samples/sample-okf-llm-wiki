// The chat model is FIXED to Claude Opus 4.8 — no model choice in the UI.
// (GPT-5.6 on Bedrock Mantle doesn't return reasoning summaries and behaved
// inconsistently, so the wiki chat pins Opus.) Only the reasoning EFFORT is
// user-selectable. This module keeps the small surface the rest of the chat UI
// imports (the single model + its efforts + the persisted effort preference).

export const DEFAULT_EFFORT = "high"

// The one and only chat model.
export const CHAT_MODEL = "global.anthropic.claude-opus-4-8"
export const CHAT_MODEL_LABEL = "Claude Opus 4.8"
export const CHAT_EFFORTS = ["low", "medium", "high", "xhigh", "max"]

export function effortsFor(_model) {
  return CHAT_EFFORTS
}

// -- persisted effort preference (the default effort for the next new chat) ----
const PREF_KEY = "okf.chat.effortPref"

export function loadEffort() {
  try {
    const saved = localStorage.getItem(PREF_KEY)
    if (saved && CHAT_EFFORTS.includes(saved)) return saved
  } catch {
    // private mode / storage disabled — fall through to the default
  }
  return DEFAULT_EFFORT
}

export function saveEffort(effort) {
  try {
    if (CHAT_EFFORTS.includes(effort)) localStorage.setItem(PREF_KEY, effort)
  } catch {
    // private mode / storage full — the in-memory selection still works
  }
}
