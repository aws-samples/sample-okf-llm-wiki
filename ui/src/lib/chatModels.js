// The chat model/effort catalog, provided by Terraform via VITE_CHAT_MODEL_CATALOG
// — base64(JSON) of the same {model, label, efforts, default_effort} shape as the
// harvest picker's (see harvestModels.js; base64 so the value survives deploy.sh's
// `eval "export k=v"`). Decoded ONCE at module load; a malformed/absent value
// falls back to the built-in catalog so the picker renders in local dev too.
//
// A conversation's model is free until its first turn. After that the picker
// offers only the conversation's FAMILY (Anthropic vs GPT): checkpointed messages
// carry provider-specific reasoning blocks, so Claude↔Claude or GPT↔GPT can
// switch in place but crossing providers needs a new chat (the runtime refuses it
// with `model_family_locked`).

import { familyOf, groupModels, normalizeModelId } from "@/lib/harvestModels"

export const DEFAULT_EFFORT = "high"
export const DEFAULT_CHAT_MODEL = "global.anthropic.claude-opus-5-5"

const EFFORTS = ["low", "medium", "high", "xhigh", "max"]
const entry = (model, label) => ({
  model,
  label,
  efforts: EFFORTS,
  default_effort: DEFAULT_EFFORT,
})

// Mirrors var.chat_model_catalog's default (the harvest catalog's 8 models, at
// chat's "high" default effort).
const FALLBACK_CATALOG = [
  entry("global.anthropic.claude-opus-5-5", "Claude Opus 5.5"),
  entry("global.anthropic.claude-opus-4-8", "Claude Opus 4.8"),
  entry("global.anthropic.claude-sonnet-5-5", "Claude Sonnet 5.5"),
  entry("global.anthropic.claude-fable-5-1", "Claude Fable 5.1"),
  entry("global.openai.gpt-6-astra", "GPT-6 Astra"),
  entry("global.openai.gpt-6.1-sol", "GPT-6.1 Sol"),
  entry("global.openai.gpt-6-luna", "GPT-6 Luna"),
  entry("global.openai.gpt-5.6-terra", "GPT-5.6 Terra"),
]

function decodeCatalog(raw) {
  if (!raw) return FALLBACK_CATALOG
  try {
    const parsed = JSON.parse(atob(raw))
    if (Array.isArray(parsed) && parsed.length) {
      return parsed
        .filter((e) => e && typeof e.model === "string")
        .map((e) => ({
          ...e,
          model: normalizeModelId(e.model),
          label: e.label || e.model,
          efforts:
            Array.isArray(e.efforts) && e.efforts.length ? e.efforts : EFFORTS,
          default_effort: e.default_effort || DEFAULT_EFFORT,
        }))
    }
  } catch {
    // fall through — a broken env shouldn't blank the picker
  }
  return FALLBACK_CATALOG
}

export const CHAT_MODEL_CATALOG = decodeCatalog(
  import.meta.env.VITE_CHAT_MODEL_CATALOG
)

export function chatEntryFor(model) {
  const id = normalizeModelId(model)
  return CHAT_MODEL_CATALOG.find((e) => e.model === id)
}

export function isOffered(model) {
  return Boolean(model && chatEntryFor(model))
}

// The efforts a model offers (empty if unknown).
export function effortsFor(model) {
  return chatEntryFor(model)?.efforts ?? []
}

export function defaultEffortFor(model) {
  return chatEntryFor(model)?.default_effort ?? DEFAULT_EFFORT
}

// `effort` if `model` offers it, else that model's default.
export function clampEffort(model, effort) {
  return effortsFor(model).includes(effort) ? effort : defaultEffortFor(model)
}

export function modelLabel(model) {
  return chatEntryFor(model)?.label || model || ""
}

// The composer's compact label: "Opus 5.5", "GPT-6.1 Sol".
export function shortModelLabel(model) {
  return modelLabel(model).replace(/^Claude\s+/, "")
}

// -- families ------------------------------------------------------------------
// GPT ids ride the Bedrock Runtime Responses API; everything else is Converse
// (Anthropic). Same split the runtime enforces; harvest's familyOf owns the
// id rule.
export function modelFamily(model) {
  return familyOf(String(model || "")) === "OpenAI" ? "openai" : "anthropic"
}

export const FAMILY_LABELS = { anthropic: "Anthropic", openai: "OpenAI" }

// [{family, label, models}] — Anthropic first, most capable first within each
// (harvest's ranking). `family` limits the list to one family (a started chat).
export function groupedChatModels(family = null) {
  const byFamily = new Map()
  for (const group of groupModels(CHAT_MODEL_CATALOG)) {
    for (const m of group.models) {
      const f = modelFamily(m.model)
      if (family && f !== family) continue
      if (!byFamily.has(f)) byFamily.set(f, [])
      byFamily.get(f).push(m)
    }
  }
  return ["anthropic", "openai"]
    .filter((f) => byFamily.has(f))
    .map((f) => ({
      family: f,
      label: FAMILY_LABELS[f],
      models: byFamily.get(f),
    }))
}

// The default model, optionally within one family: DEFAULT_CHAT_MODEL when it
// qualifies, else that family's most capable offered model.
function defaultModelIn(family = null) {
  if (
    isOffered(DEFAULT_CHAT_MODEL) &&
    (!family || modelFamily(DEFAULT_CHAT_MODEL) === family)
  ) {
    return DEFAULT_CHAT_MODEL
  }
  const groups = groupedChatModels(family)
  return (
    groups[0]?.models[0]?.model ||
    CHAT_MODEL_CATALOG[0]?.model ||
    DEFAULT_CHAT_MODEL
  )
}

// A stored model (a thread row, the server's pinned model) as an offered one:
// itself when still offered, else the default of the SAME family — a resumed
// conversation must never be moved across providers by a catalog change.
export function resolveModel(model) {
  if (isOffered(model)) return normalizeModelId(model)
  return defaultModelIn(model ? modelFamily(model) : null)
}

// -- persisted preferences (defaults for the next new chat) ---------------------
const MODEL_PREF_KEY = "okf.chat.modelPref"
const EFFORT_PREF_KEY = "okf.chat.effortPref"

export function loadModel() {
  try {
    const saved = localStorage.getItem(MODEL_PREF_KEY)
    if (saved && isOffered(saved)) return normalizeModelId(saved)
  } catch {
    // private mode / storage disabled — fall through to the default
  }
  return defaultModelIn()
}

export function saveModel(model) {
  try {
    if (isOffered(model))
      localStorage.setItem(MODEL_PREF_KEY, normalizeModelId(model))
  } catch {
    // private mode / storage full — the in-memory selection still works
  }
}

// The saved effort when `model` offers it, else the model's default.
export function loadEffort(model) {
  try {
    const saved = localStorage.getItem(EFFORT_PREF_KEY)
    if (saved) return clampEffort(model, saved)
  } catch {
    // private mode / storage disabled — fall through to the default
  }
  return defaultEffortFor(model)
}

export function saveEffort(effort) {
  try {
    if (EFFORTS.includes(effort)) localStorage.setItem(EFFORT_PREF_KEY, effort)
  } catch {
    // private mode / storage full — the in-memory selection still works
  }
}
