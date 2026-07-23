// Optional chat capabilities the user can toggle per conversation from the
// composer's "+" menu (Sparky-style: enable canvas/browser/etc). Today there's
// one — read-only SQL over the catalog — but the shape is a list so adding more
// later is just another entry.
//
// A feature is only OFFERED when it's deploy-enabled (a VITE flag baked from the
// Terraform output). The server re-checks both the deploy flag AND the per-run
// opt-in, so this is UX gating, not a security boundary.

import { DatabaseIcon } from "lucide-react"

// Vite inlines import.meta.env.* at build time. "true" (string) when the compute
// stack was deployed with var.enable_chat_sql = true.
const SQL_ENABLED =
  String(import.meta.env.VITE_CHAT_SQL_ENABLED || "") === "true"

// The full catalog of known features, each with how it presents in the "+" menu
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

// The features actually offered in this deployment (available === true).
export const AVAILABLE_FEATURES = CHAT_FEATURES.filter((f) => f.available)

// Any feature offered at all? (Hides the "+" button entirely when none are.)
export const HAS_FEATURES = AVAILABLE_FEATURES.length > 0

const BY_ID = new Map(CHAT_FEATURES.map((f) => [f.id, f]))

export function featureById(id) {
  return BY_ID.get(id) || null
}

// -- persisted feature preference (the enabled set for the next new chat) -----
// Mirrors chatModels.js's effort-pref persistence, so a user who always wants SQL
// gets it on new chats without re-toggling. Only known+available ids survive.
const PREF_KEY = "okf.chat.featuresPref"

function sanitize(ids) {
  const avail = new Set(AVAILABLE_FEATURES.map((f) => f.id))
  const seen = new Set()
  const out = []
  for (const id of ids || []) {
    if (avail.has(id) && !seen.has(id)) {
      seen.add(id)
      out.push(id)
    }
  }
  return out
}

export function loadFeatures() {
  try {
    const raw = localStorage.getItem(PREF_KEY)
    if (raw) return sanitize(JSON.parse(raw))
  } catch {
    // private mode / bad JSON — fall through to none enabled
  }
  return []
}

export function saveFeatures(ids) {
  try {
    localStorage.setItem(PREF_KEY, JSON.stringify(sanitize(ids)))
  } catch {
    // private mode / storage full — the in-memory selection still works
  }
}
