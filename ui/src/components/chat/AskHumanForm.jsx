// The clarifying-questions form the composer expands into when the agent calls
// ask_human. It's a natural vertical extension of the chat input: the questions
// render in place of the textarea, one at a time, and Submit resumes the agent.
//
// Question kinds (from the server-normalized payload):
//   - single: radio group; the user picks one option.
//   - multi:  checkboxes; the user picks any number.
//   - text:   a textarea for free prose.
// For single/multi an "Other" free-text option is ALWAYS offered (payload
// allow_other) so the user is never boxed into the model's options.
//
// One question is shown at a time with Back/Next; the last question's Next becomes
// Submit. Answers are collected as { id, answer } where answer is a string
// (single/text) or string[] (multi), then handed to onSubmit(answers).

import { useCallback, useMemo, useState } from "react"
import { ArrowLeftIcon, ArrowRightIcon, CheckIcon, XIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

// Native radio/checkbox inputs (the kit has no radio-group/checkbox primitive);
// accent-primary tints them to the app palette without extra deps.
const _CTRL = "size-3.5 shrink-0 accent-[var(--primary)]"

// A sentinel used internally to mark the "Other (free text)" choice for single/
// multi questions; the actual answer sent is the typed text, never this token.
const OTHER = "__other__"

// A blank per-question answer draft, shaped by kind.
function blankAnswer(q) {
  if (q.kind === "multi") return { choices: [], other: "", otherOn: false }
  if (q.kind === "text") return { text: "" }
  return { choice: "", other: "", otherOn: false } // single
}

// Is this question's current draft answerable (so we can gate Next/Submit)?
function isAnswered(q, a) {
  if (!a) return false
  if (q.kind === "text") return a.text.trim().length > 0
  if (q.kind === "multi") {
    const picked = a.choices.length > 0
    const otherOk = !a.otherOn || a.other.trim().length > 0
    return picked && otherOk
  }
  // single
  if (a.choice === OTHER) return a.other.trim().length > 0
  return a.choice.length > 0
}

// Fold a draft into the { id, answer, interrupt_id? } the server expects (answer
// string|string[]). interrupt_id (tagged onto each question by the server) routes
// the answer to its owning interrupt when the model raised more than one.
function toAnswer(q, a) {
  const base = q.interrupt_id != null ? { id: q.id, interrupt_id: q.interrupt_id } : { id: q.id }
  if (q.kind === "text") return { ...base, answer: a.text.trim() }
  if (q.kind === "multi") {
    const out = a.choices.filter((c) => c !== OTHER)
    if (a.otherOn && a.other.trim()) out.push(a.other.trim())
    return { ...base, answer: out }
  }
  const answer = a.choice === OTHER ? a.other.trim() : a.choice
  return { ...base, answer }
}

export function AskHumanForm({ questions, onSubmit, onCancel, disabled = false }) {
  const qs = Array.isArray(questions) ? questions : []
  const [idx, setIdx] = useState(0)
  // One draft per question id.
  const [drafts, setDrafts] = useState(() => {
    const d = {}
    for (const q of qs) d[q.id] = blankAnswer(q)
    return d
  })

  const q = qs[idx]
  const a = q ? drafts[q.id] : null
  const setDraft = useCallback(
    (patch) => setDrafts((d) => ({ ...d, [q.id]: { ...d[q.id], ...patch } })),
    [q]
  )

  const answered = q ? isAnswered(q, a) : false
  const isLast = idx === qs.length - 1
  const allAnswered = useMemo(
    () => qs.every((qq) => isAnswered(qq, drafts[qq.id])),
    [qs, drafts]
  )

  const submit = useCallback(() => {
    if (!allAnswered || disabled) return
    onSubmit(qs.map((qq) => toAnswer(qq, drafts[qq.id])))
  }, [qs, drafts, allAnswered, disabled, onSubmit])

  if (!q) return null

  return (
    <div className="flex flex-col gap-3">
      {/* Header: progress + a dismiss (cancel) affordance. */}
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-muted-foreground">
          The assistant needs a bit more info
          {qs.length > 1 ? ` · ${idx + 1} of ${qs.length}` : ""}
        </span>
        {onCancel ? (
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            onClick={onCancel}
            aria-label="Dismiss questions"
            className="text-muted-foreground"
          >
            <XIcon className="size-3.5" />
          </Button>
        ) : null}
      </div>

      {/* The current question. */}
      <div className="flex flex-col gap-2">
        <p className="text-sm font-medium">{q.prompt}</p>

        {q.kind === "text" ? (
          <textarea
            autoFocus
            rows={2}
            value={a.text}
            onChange={(e) => setDraft({ text: e.target.value })}
            placeholder="Type your answer…"
            disabled={disabled}
            className={cn(
              "okf-thin-scroll max-h-40 min-h-10 w-full resize-none rounded-lg border bg-background px-3 py-2 text-sm outline-none",
              "placeholder:text-muted-foreground focus-visible:ring-[3px] focus-visible:ring-ring/40"
            )}
          />
        ) : q.kind === "multi" ? (
          <MultiChoice q={q} draft={a} setDraft={setDraft} disabled={disabled} />
        ) : (
          <SingleChoice q={q} draft={a} setDraft={setDraft} disabled={disabled} />
        )}
      </div>

      {/* Nav: Back / Next / Submit. */}
      <div className="flex items-center justify-between gap-2">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => setIdx((i) => Math.max(0, i - 1))}
          disabled={idx === 0 || disabled}
        >
          <ArrowLeftIcon className="size-3.5" data-icon="inline-start" />
          Back
        </Button>
        {isLast ? (
          <Button type="button" size="sm" onClick={submit} disabled={!allAnswered || disabled}>
            <CheckIcon className="size-3.5" data-icon="inline-start" />
            Submit answers
          </Button>
        ) : (
          <Button
            type="button"
            size="sm"
            onClick={() => setIdx((i) => Math.min(qs.length - 1, i + 1))}
            disabled={!answered || disabled}
          >
            Next
            <ArrowRightIcon className="size-3.5" data-icon="inline-end" />
          </Button>
        )}
      </div>
    </div>
  )
}

function SingleChoice({ q, draft, setDraft, disabled }) {
  const name = `ask-${q.id}`
  return (
    <div className="flex flex-col gap-1.5" role="radiogroup">
      {q.options.map((opt, i) => (
        <label
          key={i}
          className="flex cursor-pointer items-center gap-2 rounded-md px-1 py-1 text-sm hover:bg-muted/50"
        >
          <input
            type="radio"
            name={name}
            className={_CTRL}
            checked={draft.choice === opt}
            onChange={() => setDraft({ choice: opt, otherOn: false })}
            disabled={disabled}
          />
          <span>{opt}</span>
        </label>
      ))}
      {q.allow_other ? (
        <div className="flex flex-col gap-1.5">
          <label className="flex cursor-pointer items-center gap-2 rounded-md px-1 py-1 text-sm hover:bg-muted/50">
            <input
              type="radio"
              name={name}
              className={_CTRL}
              checked={draft.choice === OTHER}
              onChange={() => setDraft({ choice: OTHER, otherOn: true })}
              disabled={disabled}
            />
            <span>Other (type your own)</span>
          </label>
          {draft.choice === OTHER ? (
            <OtherInput
              value={draft.other}
              onChange={(v) => setDraft({ other: v })}
              disabled={disabled}
            />
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

function MultiChoice({ q, draft, setDraft, disabled }) {
  const toggle = (opt, on) => {
    const set = new Set(draft.choices)
    if (on) set.add(opt)
    else set.delete(opt)
    setDraft({ choices: [...set] })
  }
  return (
    <div className="flex flex-col gap-1.5">
      {q.options.map((opt, i) => (
        <label
          key={i}
          className="flex cursor-pointer items-center gap-2 rounded-md px-1 py-1 text-sm hover:bg-muted/50"
        >
          <input
            type="checkbox"
            className={_CTRL}
            checked={draft.choices.includes(opt)}
            onChange={(e) => toggle(opt, e.target.checked)}
            disabled={disabled}
          />
          <span>{opt}</span>
        </label>
      ))}
      {q.allow_other ? (
        <div className="flex flex-col gap-1.5">
          <label className="flex cursor-pointer items-center gap-2 rounded-md px-1 py-1 text-sm hover:bg-muted/50">
            <input
              type="checkbox"
              className={_CTRL}
              checked={draft.otherOn}
              onChange={(e) => setDraft({ otherOn: e.target.checked })}
              disabled={disabled}
            />
            <span>Other (type your own)</span>
          </label>
          {draft.otherOn ? (
            <OtherInput
              value={draft.other}
              onChange={(v) => setDraft({ other: v })}
              disabled={disabled}
            />
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

function OtherInput({ value, onChange, disabled }) {
  return (
    <input
      type="text"
      autoFocus
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder="Your answer…"
      disabled={disabled}
      className={cn(
        "ml-6 w-[calc(100%-1.5rem)] rounded-md border bg-background px-2.5 py-1.5 text-sm outline-none",
        "placeholder:text-muted-foreground focus-visible:ring-[3px] focus-visible:ring-ring/40"
      )}
    />
  )
}

export { OTHER as ASK_HUMAN_OTHER }
