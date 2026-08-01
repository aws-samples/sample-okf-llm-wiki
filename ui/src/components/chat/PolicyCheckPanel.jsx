// The policy check for ONE completed turn: what the agent actually did, checked
// against the dataset's documented rules by an automated reasoner. It reads as an
// audit trail, not a verdict on the answer — the check runs entirely outside the
// conversation (after the fact, on request) and its findings never reach the
// model, so nothing here can change what the agent said.
//
// Deliberately quiet: a failed or skipped check says NOTHING about the answer, so
// every non-report state is a neutral muted box (never an Alert — that reads as
// "the answer is wrong") and the toggle that opens this panel carries no badge.
//
// It shares the chat page's ONE side-panel slot with DocPeek (ChatPanel owns the
// slot), which is why clicking a rule's source page swaps the panel to the doc
// reader. Re-opening the check then re-reads it from the runtime, which persists
// one report per (thread, turn) and answers idempotently — no re-analysis.

import { FileTextIcon, ShieldCheckIcon, XIcon } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"

import { PanelShell } from "@/components/chat/PanelShell"
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { Spinner } from "@/components/ui/spinner"
import { policyCheckAPI } from "@/lib/chatApi"
import { cn } from "@/lib/utils"

// Display gate only — the runtime's OKF_CHAT_POLICY_CHECK_ENABLED is the real
// boundary. Default ON; set VITE_CHAT_POLICY_CHECK=false to hide the toggle.
export const POLICY_CHECK_ENABLED =
  String(import.meta.env.VITE_CHAT_POLICY_CHECK ?? "true") !== "false"

// Per-dataset verdict tones. Muted throughout (a violation is a prompt to go read
// the doc, not an error state); the destructive token and emerald are the app's
// semantic colors — never raw red-*/green-*.
const VERDICT_CHIP = {
  violation: { text: "Violation", variant: "destructive", cls: "" },
  consistent: {
    text: "Consistent",
    variant: "outline",
    cls: "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
  },
  not_checkable: { text: "Not checkable", variant: "secondary", cls: "" },
  no_policy: { text: "No policy", variant: "secondary", cls: "" },
  not_enrolled: { text: "Not enrolled", variant: "secondary", cls: "" },
  stale: { text: "Rebuild pending", variant: "secondary", cls: "" },
  building: { text: "Building", variant: "secondary", cls: "" },
}

// Finding type (the reasoner's UPPER_SNAKE finding name) → how it renders.
const FINDING_CLASS = {
  INVALID: "violation",
  IMPOSSIBLE: "violation",
  SATISFIABLE: "consistent",
  VALID: "consistent",
  NO_TRANSLATIONS: "not_checkable",
  TOO_COMPLEX: "not_checkable",
  TRANSLATION_AMBIGUOUS: "not_checkable",
}

// A verdict that renders as prose instead of findings: the dataset was touched but
// no usable policy existed, so nothing was checked against it.
const UNCHECKED_VERDICTS = new Set([
  "no_policy",
  "not_enrolled",
  "stale",
  "building",
])

// The report addresses a dataset as ONE "<domain>/<dataset>" string (the pinned
// report_json contract); the doc reader needs the halves separately. A split
// {data_domain, dataset} pair is accepted too, so either shape renders.
function datasetRef(entry) {
  const raw = String(entry?.dataset || "")
  const slash = raw.indexOf("/")
  const dataDomain = entry?.data_domain || (slash > 0 ? raw.slice(0, slash) : "")
  const dataset = entry?.data_domain
    ? raw
    : slash > 0
      ? raw.slice(slash + 1)
      : raw
  return {
    dataDomain,
    dataset,
    label: dataDomain ? `${dataDomain}/${dataset}` : dataset,
  }
}

// A rule's source is a bundle-relative page path; DocPeek addresses docs by
// concept id, which is the same path MINUS the ".md" suffix (okf_core.paths).
function conceptIdFromPage(page) {
  return String(page || "").replace(/\.md$/, "")
}

// Every built rule ends with its wiki source path in parentheses (that suffix is
// how the grounding map resolves rule_source_page in the first place). The page
// renders as its own chip below, so drop it from the quote.
function ruleQuote(text, page) {
  const rule = String(text || "").trim()
  if (!page) return rule
  const suffix = `(${page})`
  return rule.endsWith(suffix)
    ? rule.slice(0, -suffix.length).trimEnd()
    : rule
}

// The neutral register for every "nothing was checked" outcome — a muted box, so
// it can never be misread as a finding against the answer.
function Note({ children }) {
  return (
    <div className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
      {children}
    </div>
  )
}

function Running({ children }) {
  return (
    <div className="flex items-center gap-2 text-sm text-muted-foreground">
      <Spinner className="size-4 shrink-0" />
      {children}
    </div>
  )
}

// One finding. A violation shows the claim beside the rule it contradicts, quoted
// from the wiki with a link to its page — the point is to send the reader to the
// documentation, not to argue with them. A consistent finding is only interesting
// as the counterfactual the reasoner ruled out, so it collapses.
function Finding({ finding, dataDomain, dataset, onOpenDoc }) {
  const kind = FINDING_CLASS[finding?.type] || "not_checkable"
  const claim = finding?.claim || ""

  if (kind === "violation") {
    const quote = ruleQuote(finding.rule_text, finding.rule_source_page)
    return (
      <div className="min-w-0 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm">
        <p className="min-w-0 break-words">{claim || "—"}</p>
        {quote ? (
          <blockquote className="mt-2 border-l-2 border-border pl-2 text-xs text-muted-foreground italic break-words">
            {quote}
          </blockquote>
        ) : null}
        {finding.rule_source_page ? (
          <button
            type="button"
            onClick={() =>
              onOpenDoc?.({
                dataDomain,
                dataset,
                conceptId: conceptIdFromPage(finding.rule_source_page),
              })
            }
            disabled={!onOpenDoc || !dataDomain || !dataset}
            className="mt-1.5 inline-flex min-w-0 items-center gap-1 font-mono text-[11px] text-primary hover:underline disabled:pointer-events-none disabled:text-muted-foreground"
          >
            <FileTextIcon className="size-3 shrink-0" />
            <span className="truncate">{finding.rule_source_page}</span>
          </button>
        ) : null}
      </div>
    )
  }

  if (kind === "consistent") {
    if (!finding.scenario) return null
    // The scenario the reasoner had to rule out to call this consistent — the
    // honest framing of "consistent", and the only part worth reading.
    return (
      <Accordion
        type="single"
        collapsible
        className="min-w-0 rounded-lg bg-muted/60 px-2"
      >
        <AccordionItem value="scenario">
          <AccordionTrigger className="py-2 text-xs font-medium">
            What would make this wrong
          </AccordionTrigger>
          <AccordionContent className="text-xs break-words text-muted-foreground">
            {finding.scenario}
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    )
  }

  // Not checkable: the reasoner couldn't state this in the policy's terms, so it
  // is neither supported nor contradicted.
  return (
    <p className="min-w-0 text-xs break-words text-muted-foreground">
      {claim
        ? `Couldn’t be expressed in the policy’s terms: ${claim}`
        : "This turn couldn’t be expressed in the policy’s terms."}
    </p>
  )
}

function DatasetSection({ entry, onOpenDoc }) {
  const { dataDomain, dataset, label } = datasetRef(entry)
  const verdict = entry?.verdict || "not_checkable"
  const chip = VERDICT_CHIP[verdict] || VERDICT_CHIP.not_checkable
  const findings = Array.isArray(entry?.findings) ? entry.findings : []

  return (
    <div className="flex min-w-0 flex-col gap-2">
      <Badge variant={chip.variant} className={cn("shrink-0", chip.cls)}>
        <span className="font-mono">{label || "—"}</span> · {chip.text}
      </Badge>
      {UNCHECKED_VERDICTS.has(verdict) ? (
        // The chip already names the dataset, so these say what HAPPENED to its
        // policy — a stale policy is never used to render a verdict.
        <Note>
          {verdict === "stale"
            ? "The wiki changed since this policy was built — rebuild pending."
            : verdict === "building"
              ? "This policy is still being built."
              : verdict === "not_enrolled"
                ? `${label} is not enrolled in reasoning — enroll it on the Reasoning page to check turns against its rules.`
                : `No policy built yet for ${label}.`}
        </Note>
      ) : findings.length ? (
        findings.map((f, i) => (
          <Finding
            key={i}
            finding={f}
            dataDomain={dataDomain}
            dataset={dataset}
            onOpenDoc={onOpenDoc}
          />
        ))
      ) : (
        <p className="text-xs text-muted-foreground">
          Nothing in this turn contradicted the documented rules.
        </p>
      )}
    </div>
  )
}

function Report({ report, onOpenDoc }) {
  const datasets = Array.isArray(report?.datasets) ? report.datasets : []
  // The footnote names what was ACTUALLY checked — a dataset whose policy was
  // missing or stale contributed nothing, so claiming its rules would overstate
  // the check's scope.
  const checked = datasets
    .filter((d) => !UNCHECKED_VERDICTS.has(d?.verdict))
    .map((d) => datasetRef(d).label)
    .filter(Boolean)

  return (
    <>
      {/* The misread-intent tripwire — first, above any verdict: a question the
          rewrite resolved wrongly was checked against the wrong thing, and the
          reader can only see that if they read it. */}
      <div className="min-w-0">
        <div className="text-[10px] font-medium tracking-wide text-muted-foreground uppercase">
          Checked against{report?.rewritten ? " · rewritten" : ""}
        </div>
        <p className="mt-0.5 text-sm break-words">
          {report?.standalone_question || "—"}
        </p>
      </div>

      <Separator className="my-3" />

      {datasets.length ? (
        <div className="flex min-w-0 flex-col gap-4">
          {datasets.map((d, i) => (
            <DatasetSection key={i} entry={d} onOpenDoc={onOpenDoc} />
          ))}
        </div>
      ) : (
        <Note>No policy built yet for this turn’s dataset.</Note>
      )}

      {report?.transcript ? (
        // Findings are only as good as the premises they were given, so the
        // premises are readable — verbatim, not summarized.
        <Accordion
          type="single"
          collapsible
          className="mt-3 rounded-lg bg-muted/60 px-2"
        >
          <AccordionItem value="transcript">
            <AccordionTrigger className="py-2 text-xs font-medium">
              What the agent did
            </AccordionTrigger>
            <AccordionContent className="text-xs break-words whitespace-pre-wrap text-muted-foreground">
              {report.transcript}
            </AccordionContent>
          </AccordionItem>
        </Accordion>
      ) : null}

      <p className="mt-3 text-[11px] text-muted-foreground/60">
        Checks query mechanics and answer obligations against the documented rules
        of {checked.length ? checked.join(", ") : "the datasets this turn touched"}.
        Not a general fact-checker.
      </p>
    </>
  )
}

export function PolicyCheckPanel({
  threadId,
  getToken,
  turnKey,
  datasetScope = null,
  onOpenDoc,
  onClose,
  onResizeStart,
  resizing = false,
}) {
  // One entry per turn, so re-opening a turn this panel already checked is
  // instant (the runtime persists the report too, but a round-trip isn't free).
  const [byTurn, setByTurn] = useState({})
  // threadId/getToken are fresh closures every parent render (getToken reads the
  // CURRENT access token, which a long conversation outlives) — read them from a
  // ref so they can't retrigger the fetch effect.
  const authRef = useRef({ threadId, getToken })
  authRef.current = { threadId, getToken }

  // An automatic open never forces: the runtime returns the stored report for a
  // turn it already checked. Only an explicit "Try again" does, so a check the
  // runtime couldn't finish gets re-run rather than re-read.
  const run = useCallback((key, { force = false } = {}) => {
    const { threadId: tid, getToken: token } = authRef.current
    setByTurn((cur) => ({ ...cur, [key]: { status: "loading", data: null } }))
    policyCheckAPI({ threadId: tid, getToken: token, turnKey: key, force })
      .then((data) =>
        setByTurn((cur) => ({ ...cur, [key]: { status: "ok", data } }))
      )
      .catch((e) =>
        setByTurn((cur) => ({
          ...cur,
          [key]: { status: "error", error: e.message || String(e) },
        }))
      )
  }, [])

  // Fires once per turn, keyed on the TURN — not on the fetch status, which would
  // cancel its own in-flight request the moment it flipped to "loading".
  useEffect(() => {
    if (typeof turnKey !== "number") return
    if (byTurn[turnKey]) return
    run(turnKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turnKey, run])

  const entry = byTurn[turnKey]
  const data = entry?.status === "ok" ? entry.data : null
  const status = data?.status
  const loading = entry?.status === "loading"
  const scopeLabel = datasetScope
    ? `${datasetScope.data_domain}/${datasetScope.dataset}`
    : ""

  let body
  if (loading) {
    body = (
      <Running>
        {scopeLabel
          ? `Checking against ${scopeLabel}’s policy…`
          : "Checking this turn…"}
      </Running>
    )
  } else if (entry?.status === "error" || status === "unavailable") {
    // Never alarming: a check that couldn't run is a gap in OUR coverage.
    body = (
      <>
        <Note>
          Couldn’t check this turn
          {entry?.error || data?.error ? ` — ${entry?.error || data.error}` : ""}
          . A check that didn’t run says nothing about the answer.
        </Note>
        <Button
          variant="outline"
          size="sm"
          className="mt-2"
          onClick={() => run(turnKey, { force: true })}
        >
          Try again
        </Button>
      </>
    )
  } else if (status === "running") {
    // The runtime declines a turn that hasn't finished: its premises aren't
    // complete until the answer is. No spinner — nothing is running HERE.
    body = (
      <>
        <Note>This turn is still running — check it once it finishes.</Note>
        <Button
          variant="outline"
          size="sm"
          className="mt-2"
          onClick={() => run(turnKey)}
        >
          Check now
        </Button>
      </>
    )
  } else if (status === "not_eligible" || data?.eligible === false) {
    body = (
      <p className="text-sm text-muted-foreground">
        No data claims this turn — nothing to check.
      </p>
    )
  } else if (data) {
    body = <Report report={data} onOpenDoc={onOpenDoc} />
  } else {
    body = null
  }

  return (
    <PanelShell onResizeStart={onResizeStart} resizing={resizing}>
      <div className="flex items-start gap-2 border-b p-3">
        <ShieldCheckIcon className="mt-0.5 size-4 shrink-0 text-primary" />
        <div className="flex min-w-0 flex-1 flex-col">
          <span className="truncate text-sm font-medium">Policy check</span>
          <span
            className={cn(
              "truncate text-[11px] text-muted-foreground",
              loading && "text-shimmer"
            )}
          >
            turn {Number(turnKey) + 1} · advisory — never changes the answer
          </span>
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="size-7 shrink-0"
          onClick={onClose}
          aria-label="Close policy check panel"
        >
          <XIcon className="size-4" />
        </Button>
      </div>
      <div className="okf-thin-scroll min-h-0 min-w-0 flex-1 overflow-y-auto p-3">
        {body}
      </div>
    </PanelShell>
  )
}
