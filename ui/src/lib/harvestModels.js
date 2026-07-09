// The harvest model/effort picker catalog, provided by Terraform via
// VITE_HARVEST_MODEL_CATALOG. It's base64(JSON) — base64 so the value survives
// deploy.sh's `eval "export k=v"` (raw JSON braces/quotes/spaces would be mangled
// by the shell). See infra/compute/outputs.tf. We decode + parse it ONCE at
// module load; a malformed/absent value falls back to a built-in default so the
// picker still renders in local dev without a deployed env.

// Effort levels, low -> high, matching okf_core.harvest_models.EFFORT_LEVELS.
export const DEFAULT_EFFORT = "xhigh"

const FALLBACK_CATALOG = [
  {
    model: "global.anthropic.claude-opus-4-8",
    label: "Claude Opus 4.8",
    efforts: ["low", "medium", "high", "xhigh", "max"],
    default_effort: "xhigh",
  },
  {
    model: "openai.gpt-5.5",
    label: "GPT-5.5",
    efforts: ["low", "medium", "high", "xhigh"],
    default_effort: "xhigh",
  },
]

function decodeCatalog(raw) {
  if (!raw) return FALLBACK_CATALOG
  try {
    const parsed = JSON.parse(atob(raw))
    if (Array.isArray(parsed) && parsed.length) return parsed
  } catch {
    // fall through to the default — a broken env shouldn't blank the picker
  }
  return FALLBACK_CATALOG
}

export const MODEL_CATALOG = decodeCatalog(
  import.meta.env.VITE_HARVEST_MODEL_CATALOG
)

// The catalog entry for a model id, or undefined.
export function entryFor(model) {
  return MODEL_CATALOG.find((e) => e.model === model)
}

// The efforts a model offers (empty array if unknown).
export function effortsFor(model) {
  return entryFor(model)?.efforts ?? []
}

// A model's default effort, else the global DEFAULT_EFFORT.
export function defaultEffortFor(model) {
  return entryFor(model)?.default_effort ?? DEFAULT_EFFORT
}

// -- persisted user preference ---------------------------------------------
// The picker selection is a user preference: persisted to localStorage so it
// survives a page refresh (mirrors theme-provider's pattern). We VALIDATE on
// load against the current catalog — a saved model/effort that's no longer
// offered (catalog changed between deploys) falls back to the default rather
// than sending a value the Control API would 400.
const PREF_KEY = "okf.harvest.modelPref"

// The catalog default: first model at its default effort.
export function defaultPreference() {
  const model = MODEL_CATALOG[0]?.model ?? ""
  return { model, effort: defaultEffortFor(model) }
}

// Load the saved {model, effort}, validated against the catalog; else default.
export function loadPreference() {
  const fallback = defaultPreference()
  let saved
  try {
    saved = JSON.parse(localStorage.getItem(PREF_KEY) || "null")
  } catch {
    return fallback
  }
  if (!saved || !entryFor(saved.model)) return fallback
  const effort = effortsFor(saved.model).includes(saved.effort)
    ? saved.effort
    : defaultEffortFor(saved.model)
  return { model: saved.model, effort }
}

// Persist the current selection (best-effort; storage may be unavailable).
export function savePreference(model, effort) {
  try {
    localStorage.setItem(PREF_KEY, JSON.stringify({ model, effort }))
  } catch {
    // private mode / storage full — the in-memory selection still works
  }
}
