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
  {
    model: "global.anthropic.claude-opus-5",
    label: "Claude Opus 5",
    efforts: ["low", "medium", "high", "xhigh", "max"],
    default_effort: "xhigh",
  },
  {
    model: "global.anthropic.claude-sonnet-5",
    label: "Claude Sonnet 5",
    efforts: ["low", "medium", "high", "xhigh", "max"],
    default_effort: "xhigh",
  },
  {
    model: "openai.gpt-5.6-terra",
    label: "GPT-5.6 Terra",
    efforts: ["low", "medium", "high", "xhigh", "max"],
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

// -- picker grouping ---------------------------------------------------------
// The pickers render the catalog grouped by provider family, most capable
// first within each family: tier (Opus > Sonnet > Haiku; Sol > Terra > Luna),
// then newer version. Ranking is a heuristic over the model id — an id with an
// unknown tier or version sorts last in its family instead of breaking.
const FAMILY_ORDER = ["Anthropic", "OpenAI", "Other"]
const TIER_RANK = { opus: 0, sol: 0, sonnet: 1, terra: 1, haiku: 2, luna: 2 }

function familyOf(model) {
  if (model.startsWith("openai.") || model.startsWith("gpt-")) return "OpenAI"
  if (model.includes("anthropic") || model.includes("claude")) return "Anthropic"
  return "Other"
}

function tierRankOf(model) {
  for (const [tier, rank] of Object.entries(TIER_RANK)) {
    if (model.includes(tier)) return rank
  }
  return 9
}

// "claude-opus-4-8" -> 4.8, "claude-sonnet-5" -> 5, "gpt-5.6-terra" -> 5.6.
function versionOf(model) {
  const m = model.match(/(?:opus|sonnet|haiku|gpt)[-.]?(\d+)(?:[.-](\d+))?/)
  if (!m) return 0
  return parseInt(m[1], 10) + (m[2] ? parseInt(m[2], 10) / 10 : 0)
}

// [{family, models}] — families in FAMILY_ORDER (empty ones dropped), models
// most-capable-first. Derived once from the static MODEL_CATALOG.
export const GROUPED_MODEL_CATALOG = FAMILY_ORDER.map((family) => ({
  family,
  models: MODEL_CATALOG.filter((e) => familyOf(e.model) === family).sort(
    (a, b) =>
      tierRankOf(a.model) - tierRankOf(b.model) ||
      versionOf(b.model) - versionOf(a.model) ||
      a.label.localeCompare(b.label)
  ),
})).filter((g) => g.models.length)

// -- persisted user preference ---------------------------------------------
// The picker selection is a user preference: persisted to localStorage so it
// survives a page refresh (mirrors theme-provider's pattern). We VALIDATE on
// load against the current catalog — a saved model/effort that's no longer
// offered (catalog changed between deploys) falls back to the default rather
// than sending a value the Control API would 400.
//
// The preference carries THREE configs: the harvester's {model, effort}, the
// sub-agents' {subagentModel, subagentEffort} ("" = same as harvester), and
// the adversarial reviewer's {reviewerModel, reviewerEffort} ("" = same as
// sub-agents — a different family here improves review coverage). Older saved
// shapes load fine and get the "" defaults.
const PREF_KEY = "okf.harvest.modelPref"

// The catalog default: first model at its default effort; sub-agents same as
// the harvester.
export function defaultPreference() {
  const model = MODEL_CATALOG[0]?.model ?? ""
  return {
    model,
    effort: defaultEffortFor(model),
    subagentModel: "",
    subagentEffort: "",
    reviewerModel: "",
    reviewerEffort: "",
  }
}

// Load the saved preference, each pair validated against the catalog; else default.
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
  // The sub-agent pair falls back to "same as harvester" ("") when unset or no
  // longer offered — never to a value the Control API would 400.
  let subagentModel = ""
  let subagentEffort = ""
  if (saved.subagentModel && entryFor(saved.subagentModel)) {
    subagentModel = saved.subagentModel
    subagentEffort = effortsFor(subagentModel).includes(saved.subagentEffort)
      ? saved.subagentEffort
      : defaultEffortFor(subagentModel)
  }
  // Same validation for the reviewer's own pair ("" = same as sub-agents).
  let reviewerModel = ""
  let reviewerEffort = ""
  if (saved.reviewerModel && entryFor(saved.reviewerModel)) {
    reviewerModel = saved.reviewerModel
    reviewerEffort = effortsFor(reviewerModel).includes(saved.reviewerEffort)
      ? saved.reviewerEffort
      : defaultEffortFor(reviewerModel)
  }
  return {
    model: saved.model,
    effort,
    subagentModel,
    subagentEffort,
    reviewerModel,
    reviewerEffort,
  }
}

// Persist the current selection (best-effort; storage may be unavailable).
export function savePreference(
  model,
  effort,
  subagentModel = "",
  subagentEffort = "",
  reviewerModel = "",
  reviewerEffort = ""
) {
  try {
    localStorage.setItem(
      PREF_KEY,
      JSON.stringify({
        model,
        effort,
        subagentModel,
        subagentEffort,
        reviewerModel,
        reviewerEffort,
      })
    )
  } catch {
    // private mode / storage full — the in-memory selection still works
  }
}
