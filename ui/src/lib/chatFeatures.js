// Optional chat capabilities the user can toggle per conversation from the
// composer's "+" menu (Sparky-style: enable canvas/browser/etc).
//
// Two kinds of entry:
// - flat features (SQL) — one menu row, one chip, one id.
// - the POLICY field — a submenu with three mutually-exclusive options
//   (Computational / Behavioural / Strict = both), each its own feature id
//   (`policy:*`). It REQUIRES the SQL feature: the menu disables it while SQL
//   is off, and removing the SQL chip also removes the policy chip. The server
//   enforces the same dependency independently (normalize_features drops
//   orphaned policy:* values), so this is UX gating, not a security boundary.
//
// A feature is only OFFERED when it's deploy-enabled (a VITE flag baked from the
// Terraform output). The server re-checks both the deploy flag AND the per-run
// opt-in.

import { DatabaseIcon, ShieldCheckIcon } from "lucide-react"

// Vite inlines import.meta.env.* at build time. "true" (string) when the compute
// stack was deployed with var.enable_chat_sql = true.
const SQL_ENABLED =
  String(import.meta.env.VITE_CHAT_SQL_ENABLED || "") === "true"

// Display gate for everything policy: the composer's Policy field AND the
// Reasoning page. The runtime's OKF_CHAT_POLICY_CHECK_ENABLED is the real
// boundary. Default ON; set VITE_CHAT_POLICY_CHECK=false to hide both.
export const POLICY_CHECK_ENABLED =
  String(import.meta.env.VITE_CHAT_POLICY_CHECK ?? "true") !== "false"

// The full catalog of flat features, each with how it presents in the "+" menu
// and as an enabled chip. `available` gates whether it's offered at all.
export const CHAT_FEATURES = [
  {
    id: "sql",
    label: "SQL",
    // Shown in the "+" menu row.
    menuLabel: "Query with SQL",
    // The backend is picked per conversation: Athena over the catalog by
    // default; the @-mentioned dataset's Redshift when it's Redshift-backed.
    description: "Run read-only SQL against the live source data",
    icon: DatabaseIcon,
    available: SQL_ENABLED,
  },
]

// The Policy field's side options — mutually exclusive (picking one replaces
// the current one). Each lands as its own chip next to the SQL chip.
export const POLICY_PREFIX = "policy:"
export const POLICY_AVAILABLE = SQL_ENABLED && POLICY_CHECK_ENABLED
export const POLICY_OPTIONS = [
  {
    id: "policy:computational",
    label: "Computational",
    description: "Judge each analytical SQL query against the dataset's policies",
    icon: ShieldCheckIcon,
    available: POLICY_AVAILABLE,
  },
  {
    id: "policy:behavioural",
    label: "Behavioural",
    description: "Judge the steps the agent takes against the dataset's policies",
    icon: ShieldCheckIcon,
    available: POLICY_AVAILABLE,
  },
  {
    id: "policy:strict",
    label: "Strict",
    description: "Both checks — queries and conduct",
    icon: ShieldCheckIcon,
    available: POLICY_AVAILABLE,
  },
]

export function isPolicyId(id) {
  return typeof id === "string" && id.startsWith(POLICY_PREFIX)
}

// The flat features actually offered in this deployment (available === true).
export const AVAILABLE_FEATURES = CHAT_FEATURES.filter((f) => f.available)

// Any feature offered at all? (Hides the "+" button entirely when none are.)
export const HAS_FEATURES = AVAILABLE_FEATURES.length > 0

const BY_ID = new Map(
  [...CHAT_FEATURES, ...POLICY_OPTIONS].map((f) => [f.id, f])
)

export function featureById(id) {
  return BY_ID.get(id) || null
}

// -- persisted feature preference (the enabled set for the next new chat) -----
// Mirrors chatModels.js's effort-pref persistence, so a user who always wants SQL
// gets it on new chats without re-toggling. Only known+available ids survive, the
// policy→SQL dependency is re-enforced, and at most ONE policy:* is kept (the
// last one — the options are mutually exclusive).
const PREF_KEY = "okf.chat.featuresPref"

export function sanitizeFeatures(ids) {
  const known = new Set(
    [...AVAILABLE_FEATURES, ...POLICY_OPTIONS.filter((o) => o.available)].map(
      (f) => f.id
    )
  )
  const seen = new Set()
  let out = []
  for (const id of ids || []) {
    if (known.has(id) && !seen.has(id)) {
      seen.add(id)
      out.push(id)
    }
  }
  // Policy requires SQL; and only one policy option can be active.
  if (!out.includes("sql")) out = out.filter((id) => !isPolicyId(id))
  const policies = out.filter(isPolicyId)
  if (policies.length > 1) {
    const keep = policies[policies.length - 1]
    out = out.filter((id) => !isPolicyId(id) || id === keep)
  }
  return out
}

export function loadFeatures() {
  try {
    const raw = localStorage.getItem(PREF_KEY)
    if (raw) return sanitizeFeatures(JSON.parse(raw))
  } catch {
    // private mode / bad JSON — fall through to none enabled
  }
  return []
}

export function saveFeatures(ids) {
  try {
    localStorage.setItem(PREF_KEY, JSON.stringify(sanitizeFeatures(ids)))
  } catch {
    // private mode / storage full — the in-memory selection still works
  }
}
