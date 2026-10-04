// Per-conversation chat run settings, set from the row under the composer:
//
// - SQL — a switch. On adds the `sql` feature id (the agent gets read-only SQL
//   against the live source data). Offered only when the deployment enables it.
// - GUARDRAILS — a selector of four: Disabled, or one of three mutually
//   exclusive check modes (internal ids `policy:*` — the server contract):
//   Computational / Behavioural / Strict (= both). Always selectable, SQL on or
//   off: the checks judge SQL conduct, so the RUNTIME ignores the selection
//   while SQL is off (normalize_features). That is the boundary — the UI no
//   longer cascades SQL-off into dropping the guardrail choice.
//
// Both ride the run's `features` array, e.g. ["sql", "policy:strict"]; the server
// re-checks the deploy flags AND the per-run opt-in.

import { Layers2Icon, PiIcon, RouteIcon, ShieldOffIcon } from "lucide-react"

// Vite inlines import.meta.env.* at build time. "true" (string) when the compute
// stack was deployed with var.enable_chat_sql = true.
export const SQL_AVAILABLE =
  String(import.meta.env.VITE_CHAT_SQL_ENABLED || "") === "true"

// Display gate for everything policy: the composer's Guardrails selector AND the
// Reasoning page. The runtime's OKF_CHAT_POLICY_CHECK_ENABLED is the real
// boundary. Default ON; set VITE_CHAT_POLICY_CHECK=false to hide both.
export const POLICY_CHECK_ENABLED =
  String(import.meta.env.VITE_CHAT_POLICY_CHECK ?? "true") !== "false"

export const SQL_FEATURE = "sql"
export const POLICY_PREFIX = "policy:"

// The three check modes — mutually exclusive (picking one replaces the current).
export const POLICY_OPTIONS = [
  {
    id: "policy:computational",
    label: "Computational",
    description: "Judge each SQL query",
    icon: PiIcon,
  },
  {
    id: "policy:behavioural",
    label: "Behavioural",
    description: "Judge the agent's steps",
    icon: RouteIcon,
  },
  {
    id: "policy:strict",
    label: "Strict",
    description: "Both checks",
    icon: Layers2Icon,
  },
]

// The selector's options: Disabled (no policy id) first, then the modes.
export const GUARDRAIL_DISABLED = {
  id: null,
  label: "Disabled",
  description: "No guardrail checks",
  icon: ShieldOffIcon,
}
export const GUARDRAIL_OPTIONS = [GUARDRAIL_DISABLED, ...POLICY_OPTIONS]

export function isPolicyId(id) {
  return typeof id === "string" && id.startsWith(POLICY_PREFIX)
}

// The active guardrail mode's id, or null (Disabled).
export function policyOf(features) {
  return (features || []).find(isPolicyId) ?? null
}

// `features` with the guardrail mode set to `id` (null = Disabled).
export function withPolicy(features, id) {
  const rest = (features || []).filter((f) => !isPolicyId(f))
  return id ? [...rest, id] : rest
}

export function sqlOn(features) {
  return (features || []).includes(SQL_FEATURE)
}

// `features` with SQL switched on/off; the guardrail choice is kept either way.
export function withSql(features, on) {
  const rest = (features || []).filter((f) => f !== SQL_FEATURE)
  return on ? [SQL_FEATURE, ...rest] : rest
}

// -- persisted feature preference (the enabled set for the next new chat) -----
// Only known + offered ids survive, and at most ONE policy:* is kept (the last
// one — the modes are mutually exclusive).
const PREF_KEY = "okf.chat.featuresPref"

export function sanitizeFeatures(ids) {
  const known = new Set([
    ...(SQL_AVAILABLE ? [SQL_FEATURE] : []),
    ...(POLICY_CHECK_ENABLED ? POLICY_OPTIONS.map((o) => o.id) : []),
  ])
  const seen = new Set()
  let out = []
  for (const id of ids || []) {
    if (known.has(id) && !seen.has(id)) {
      seen.add(id)
      out.push(id)
    }
  }
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
