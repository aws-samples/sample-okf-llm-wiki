// The composer (shape ported from Sentry). One row — "+", text, send — until the
// text needs a second line, then the text takes the full width over a button row.
// A dataset scope forces that two-line shape: its chip rides next to the "+" on
// the button row. The shape is measured independently of the current shape (see
// shapeFor) so it cannot oscillate. Enter sends (Shift+Enter = newline); while
// streaming the button becomes Stop.
//
// Under the card, the run settings: Guardrails + the SQL switch on the left, the
// model · effort control and the context gauge on the right. Owns only its own
// draft text; the parent handles send/stop and every setting.

import {
  AtSignIcon,
  CornerDownRightIcon,
  DatabaseIcon,
  MicroscopeIcon,
  PinIcon,
  PlusIcon,
  SquareIcon,
  XIcon,
} from "lucide-react"
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react"
import { toast } from "sonner"

import { AskHumanForm } from "@/components/chat/AskHumanForm"
import {
  GuardrailsSetting,
  ModelEffortSetting,
  SqlSwitch,
} from "@/components/chat/ComposerSettings"
import { Button } from "@/components/ui/button"
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Popover, PopoverAnchor, PopoverContent } from "@/components/ui/popover"
import {
  POLICY_CHECK_ENABLED,
  SQL_AVAILABLE,
  policyOf,
  sqlOn,
  withPolicy,
  withSql,
} from "@/lib/chatFeatures"
import { cn } from "@/lib/utils"

// One timing for textarea height (inline style) and card padding/radius (class):
// they sum into one movement.
const MOVE_MS = 150
const MOVE_EASE = "cubic-bezier(0.4, 0, 0.2, 1)"
const HEIGHT_TRANSITION = `height ${MOVE_MS}ms ${MOVE_EASE}`
const CARD_TRANSITION =
  "transition-[padding,border-radius] duration-150 ease-[cubic-bezier(0.4,0,0.2,1)] motion-reduce:transition-none"

// Characters measured for the shape decision: more cannot change "wider than one
// row?", and laying out a whole pasted block unwrapped per keystroke is costly.
const MEASURE_CHARS = 400

const prefersReducedMotion = () =>
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false

// The dataset key shown in the scope chip / mention list ("domain/dataset").
function datasetKey(d) {
  return `${d.data_domain}/${d.dataset}`
}

// The current scope as a removable chip, beside the "+" on the button row.
function DatasetScopeChip({ scope, onRemove }) {
  return (
    <span className="group/chip inline-flex h-7 items-center gap-1 rounded-md bg-primary/10 pr-1.5 pl-2.5 text-xs font-medium text-primary">
      <AtSignIcon className="size-3 opacity-80" />
      {datasetKey(scope)}
      <button
        type="button"
        aria-label="Clear Dataset Scope"
        onClick={onRemove}
        // Zero-width until the CHIP is hovered (saves row width), w-0 rather
        // than hidden so it stays tabbable — keyboard focus re-expands it via
        // group-focus-within.
        className="ml-0 flex h-4 w-0 items-center justify-center overflow-hidden rounded-sm text-primary/70 opacity-0 transition-all group-focus-within/chip:ml-0.5 group-focus-within/chip:w-4 group-focus-within/chip:opacity-100 group-hover/chip:ml-0.5 group-hover/chip:w-4 group-hover/chip:opacity-100 hover:bg-primary/15 hover:text-primary"
      >
        <XIcon className="size-3" />
      </button>
    </span>
  )
}

// The `@`-mention dataset picker — a Popover(Command) anchored to the text field,
// opened by typing "@" or from the "+" menu. Picking sets the scope.
function DatasetMentionList({ datasets, onPick, onBackspaceEmpty }) {
  return (
    <Command>
      <CommandInput
        placeholder="Scope to a dataset…"
        autoFocus
        // Backspace on an EMPTY search removes the "@" that opened the picker and
        // closes it (so the user doesn't have to reach for Escape).
        onKeyDown={(e) => {
          if (e.key === "Backspace" && e.currentTarget.value === "") {
            e.preventDefault()
            onBackspaceEmpty?.()
          }
        }}
      />
      <CommandList>
        <CommandEmpty>No datasets match.</CommandEmpty>
        <CommandGroup>
          {datasets.map((d) => {
            const key = datasetKey(d)
            return (
              <CommandItem key={key} value={key} onSelect={() => onPick(d)}>
                <DatabaseIcon className="size-3.5 text-muted-foreground" />
                {key}
              </CommandItem>
            )
          })}
        </CommandGroup>
      </CommandList>
    </Command>
  )
}

// The "+" menu: scope the conversation to a dataset (an explicit, discoverable
// alternative to typing "@"), and run a saved analysis (the questionnaire
// dialog — ChatThread owns it). Either entry is omitted when its handler is.
function AddMenu({ onScope, onRunAnalysis }) {
  const [open, setOpen] = useState(false)
  // Set when "Scope To A Dataset" is chosen, so onCloseAutoFocus skips Radix's
  // focus-restore to the "+" trigger — that restore lands OUTSIDE the dataset
  // popover onScope just opened and would dismiss it instantly. onScope focuses
  // the picker itself, so no focus is lost.
  const scopeSelectedRef = useRef(false)
  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-8 shrink-0 text-muted-foreground hover:bg-foreground/5 hover:text-foreground"
          title="Add"
          aria-label="Add"
        >
          <PlusIcon className="size-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        side="top"
        className="min-w-52"
        onCloseAutoFocus={(e) => {
          if (scopeSelectedRef.current) {
            scopeSelectedRef.current = false
            e.preventDefault()
          }
        }}
      >
        {onScope ? (
          <DropdownMenuItem
            onSelect={() => {
              scopeSelectedRef.current = true
              onScope()
            }}
          >
            <PinIcon className="size-3.5 text-muted-foreground" />
            Scope To A Dataset
          </DropdownMenuItem>
        ) : null}
        {onRunAnalysis ? (
          <DropdownMenuItem
            onSelect={() => {
              // Same handoff guard as the scope item: skip the menu's
              // focus-restore to the "+" trigger — it would land on top of
              // the analysis dialog this opens and fight its focus trap.
              scopeSelectedRef.current = true
              onRunAnalysis()
            }}
          >
            <MicroscopeIcon className="size-3.5 text-muted-foreground" />
            Run An Analysis
          </DropdownMenuItem>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

// Sparky's keep-warm timings: don't fire in the first 2s after mount, fire
// IMMEDIATELY on the first keystroke of an empty box, then debounce 500ms on
// subsequent typing, and ping every 300s while there's draft text.
const PREPARE_MOUNT_GRACE_MS = 2000
const PREPARE_DEBOUNCE_MS = 500
const PREPARE_INTERVAL_MS = 300000

export function ChatInput({
  onSend,
  onStop,
  onPrepare,
  isStreaming = false,
  disabled = false,
  placeholder = "Ask about the wiki — type @ to scope a dataset",
  autoFocus = true,
  model,
  modelGroups,
  lockedFamily = null,
  onModelChange,
  effort,
  efforts,
  onEffortChange,
  features = [],
  onFeaturesChange,
  datasets = [],
  datasetsLoading = false,
  datasetScope = null,
  onScopeChange,
  pendingAsk = null,
  onAnswer,
  // The ContextGauge, at the right end of the settings row.
  contextSlot = null,
  // True while the conversation compacts: the runtime refuses turns then, so
  // Enter keeps the text and explains instead of clearing the box.
  compacting = false,
  // Opens the analysis-run dialog (the "+" menu's "Run An Analysis" item);
  // null hides the item (no API, streaming, or a pending question).
  onRunAnalysis = null,
}) {
  const [text, setText] = useState("")
  const ref = useRef(null)
  const [multiline, setMultiline] = useState(false)
  const mirrorRef = useRef(null) // measures the text's natural single-line width
  const plusRef = useRef(null)
  const sendRef = useRef(null)
  // Set by the setRowEl callback ref (the row unmounts during an ask).
  const rowRef = useRef(null)
  const rowRoRef = useRef(null)
  const lastWidthRef = useRef(0)
  // The settled height; the live style.height can hold an intermediate value
  // measured at the wrong width during a shape flip.
  const settledHeightRef = useRef("")
  // The draft for the resize observer, so it is not re-created per keystroke.
  const textRef = useRef("")
  // Set by send(): the collapse after a send must land in the SAME frame (no
  // height animation) — ChatThread's new-turn pin measures the transcript
  // viewport in its layout effect, and a still-animating tall composer would
  // leave it measuring short.
  const instantRef = useRef(false)

  // When the agent has paused to ask clarifying questions, the composer becomes
  // the QA form (a natural vertical expansion of the input) — the textarea and
  // its row unmount until the user submits, which resumes the agent.
  const asking = Boolean(pendingAsk && pendingAsk.questions?.length && onAnswer)
  // A scope chip forces the two-line shape (text over the button row).
  const twoLine = multiline || Boolean(datasetScope)

  // `@`-mention picker: open + the query typed after the "@" (used to seed the
  // picker's filter). The trigger "@"'s index lets us strip the fragment on pick.
  const [mentionOpen, setMentionOpen] = useState(false)
  const [mentionQuery, setMentionQuery] = useState("")
  const mentionAtRef = useRef(-1) // index of the active "@" in the textarea value
  const canMention = Boolean(onScopeChange) && datasets.length > 0
  // The "+" mounts with the composer while the dataset list is still loading (a
  // registered deployment has datasets) and hides only when the list is KNOWN
  // empty — gating on the fetched list would pop it in a beat after first paint.
  const offerScope =
    Boolean(onScopeChange) && (datasetsLoading || datasets.length > 0)

  // Detect an active `@mention` at the caret: an "@" at the start or after
  // whitespace, followed by [\w/.-]* up to the caret. Opens the dataset picker and
  // tracks the "@" index + the typed query. Any other edit closes it.
  const syncMention = useCallback(
    (value, caret) => {
      if (!canMention) return
      const upToCaret = value.slice(0, caret)
      const m = /(^|\s)@([\w/.-]*)$/.exec(upToCaret)
      if (m) {
        mentionAtRef.current = caret - m[2].length - 1 // index of the "@"
        setMentionQuery(m[2])
        setMentionOpen(true)
      } else if (mentionOpen) {
        setMentionOpen(false)
        mentionAtRef.current = -1
      }
    },
    [canMention, mentionOpen]
  )

  const onTextChange = useCallback(
    (e) => {
      setText(e.target.value)
      syncMention(
        e.target.value,
        e.target.selectionStart ?? e.target.value.length
      )
    },
    [syncMention]
  )

  // Pick a dataset from the mention popover: set the scope and remove the "@query"
  // fragment from the draft (the chip now represents it), then refocus the box.
  const pickDataset = useCallback(
    (d) => {
      onScopeChange?.({ data_domain: d.data_domain, dataset: d.dataset })
      const at = mentionAtRef.current
      if (at >= 0) {
        const before = text.slice(0, at)
        const after = text.slice(at + 1 + mentionQuery.length)
        setText((before + after).replace(/\s{2,}/g, " "))
      }
      setMentionOpen(false)
      mentionAtRef.current = -1
      requestAnimationFrame(() => ref.current?.focus())
    },
    [onScopeChange, text, mentionQuery]
  )

  // Open the dataset picker from the "+" menu instead of an "@" keystroke. There
  // is no "@" fragment to strip on pick, so leave mentionAtRef at -1. Stamp the
  // open time: the "+" dropdown's teardown (focus restore + outside-pointer
  // detection) fires just after this and the picker popover reads it as an
  // outside interaction — the onOpenChange guard below ignores any dismiss within
  // a short grace window so the picker doesn't flicker shut.
  const scopeOpenedAt = useRef(0)
  const openScopePicker = useCallback(() => {
    if (!canMention) return
    mentionAtRef.current = -1
    setMentionQuery("")
    scopeOpenedAt.current = performance.now()
    setMentionOpen(true)
  }, [canMention])

  // Dismiss the picker WITHOUT choosing: strip the "@" (and any query typed after
  // it) that triggered it, close, and refocus the composer.
  const dismissMention = useCallback(() => {
    const at = mentionAtRef.current
    if (at >= 0) {
      const before = text.slice(0, at)
      const after = text.slice(at + 1 + mentionQuery.length)
      setText(before + after)
    }
    setMentionOpen(false)
    mentionAtRef.current = -1
    requestAnimationFrame(() => ref.current?.focus())
  }, [text, mentionQuery])

  // Whether the text needs the multiline shape, without reading the live textarea
  // (the two shapes give different widths, so that would oscillate): an
  // off-screen mirror's single-line width vs the narrow row's. Returns, does not
  // apply.
  const shapeFor = useCallback((value) => {
    const row = rowRef.current
    const mirror = mirrorRef.current
    if (!row || !mirror) return false
    // A newline decides it before the mirror is touched.
    if (value.includes("\n")) return true
    if (!value) return false
    if (value.length > MEASURE_CHARS) return true
    mirror.textContent = value
    // Button widths and the gap are read from the DOM (present in both shapes).
    const gap = parseFloat(getComputedStyle(row).columnGap) || 0
    const reserve =
      (plusRef.current?.offsetWidth || 0) +
      (sendRef.current?.offsetWidth || 0) +
      gap * 2
    // Clamped at 0 so an empty composer in a very narrow column stays one row.
    const available = Math.max(row.clientWidth - reserve, 0)
    return mirror.offsetWidth > available
  }, [])

  const grow = useCallback(() => {
    const el = ref.current
    if (!el) return
    // The cap is the CSS class's (max-h-48).
    const cap = parseFloat(getComputedStyle(el).maxHeight) || Infinity
    // FLIP: measure at height:auto with the transition off, restore the settled
    // height, flush, then animate to the new one.
    const previous = settledHeightRef.current
    el.style.transition = "none"
    el.style.height = "auto"
    const next = `${Math.min(el.scrollHeight, cap)}px`
    settledHeightRef.current = next
    const instant = instantRef.current || prefersReducedMotion()
    instantRef.current = false
    if (!previous || previous === next || instant) {
      el.style.height = next
      if (!instant) el.style.transition = HEIGHT_TRANSITION
      return
    }
    el.style.height = previous
    void el.offsetHeight // forces the restored height to take effect first
    el.style.transition = HEIGHT_TRANSITION
    el.style.height = next
  }, [])

  // LAYOUT effect so the height is right before paint. The shape is applied first
  // and growth deferred to the next pass (same frame), because `grow` measures at
  // the width the shape decides. `asking` is a dependency because the textarea
  // remounts after an ask; `datasetScope` because the chip moves the "+" slot's
  // width and forces/releases the two-line shape.
  useLayoutEffect(() => {
    textRef.current = text
    if (asking) return
    const next = shapeFor(text)
    if (next !== multiline) {
      setMultiline(next)
      // After a send the box is empty — one line in either shape — so collapse
      // NOW: ChatThread's new-turn pin measures in this same commit, before
      // the shape's re-render lands.
      if (!instantRef.current) return
    }
    grow()
  }, [text, multiline, asking, datasetScope, shapeFor, grow])

  // Re-decide the shape when the row's width changes. A callback ref because the
  // row unmounts with every ask.
  const setRowEl = useCallback(
    (el) => {
      rowRef.current = el
      rowRoRef.current?.disconnect()
      rowRoRef.current = null
      lastWidthRef.current = 0
      if (!el || typeof ResizeObserver === "undefined") return
      rowRoRef.current = new ResizeObserver(() => {
        // Width only: height changes every wrap and transition frame, and
        // reacting would force layouts and "ResizeObserver loop" errors.
        const width = el.clientWidth
        if (width === lastWidthRef.current) return
        lastWidthRef.current = width
        setMultiline(shapeFor(textRef.current))
      })
      rowRoRef.current.observe(el)
    },
    [shapeFor]
  )

  useEffect(() => () => rowRoRef.current?.disconnect(), [])

  useEffect(() => {
    if (autoFocus && ref.current && !isStreaming) ref.current.focus()
  }, [autoFocus, isStreaming])

  // --- keep-warm: prepare() as the user types (Sparky's debounce) ------------
  const firstMountRef = useRef(true)
  const prevTextRef = useRef("")
  const debounceRef = useRef(null)

  // Ignore keystrokes for the first 2s after mount (avoids a prepare on a
  // conversation the user just opened but isn't typing into yet).
  useEffect(() => {
    firstMountRef.current = true
    const t = setTimeout(() => {
      firstMountRef.current = false
    }, PREPARE_MOUNT_GRACE_MS)
    return () => clearTimeout(t)
  }, [])

  useEffect(() => {
    if (!onPrepare || firstMountRef.current) {
      prevTextRef.current = text
      return
    }
    const cur = text.trim()
    const prev = prevTextRef.current.trim()
    if (debounceRef.current) clearTimeout(debounceRef.current)
    if (cur) {
      if (!prev) {
        onPrepare() // first keystroke of an empty box → warm now
      } else {
        debounceRef.current = setTimeout(onPrepare, PREPARE_DEBOUNCE_MS)
      }
    }
    prevTextRef.current = text
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current)
    }
  }, [text, onPrepare])

  // Periodic ping while there's draft text, so a long compose keeps it warm.
  useEffect(() => {
    if (!onPrepare) return
    const id = setInterval(() => {
      if (text.trim()) onPrepare()
    }, PREPARE_INTERVAL_MS)
    return () => clearInterval(id)
  }, [text, onPrepare])

  const send = useCallback(() => {
    const t = text.trim()
    if (!t || disabled || isStreaming) return
    // The runtime refuses a new turn while compacting; keep the text.
    if (compacting) {
      toast.message("This Conversation Is Being Compacted", {
        description: "It takes a few seconds — send again once it finishes.",
      })
      return
    }
    instantRef.current = true
    onSend(t)
    setText("")
  }, [text, disabled, isStreaming, compacting, onSend])

  const onKeyDown = useCallback(
    (e) => {
      // An IME composition owns Enter (isComposing, or keyCode 229 on legacy
      // engines).
      if (e.nativeEvent?.isComposing || e.keyCode === 229) return
      // While the @-mention picker is open, let it own the keys (arrows/Enter to
      // choose, Escape to dismiss) instead of sending the message.
      if (mentionOpen) {
        if (e.key === "Escape") {
          e.preventDefault()
          setMentionOpen(false)
          mentionAtRef.current = -1
        }
        // Enter/arrows are handled by the Command via its own focus; don't send.
        if (e.key === "Enter") e.preventDefault()
        return
      }
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault()
        // Enter on an empty box mid-run stops; with text it waits for the run.
        if (isStreaming) {
          if (!text.trim()) onStop?.()
        } else {
          send()
        }
      }
    },
    [isStreaming, onStop, send, text, mentionOpen]
  )

  const canSend = text.trim().length > 0 && !disabled && !isStreaming

  const policy = policyOf(features)
  const sql = sqlOn(features)

  // Bordered in both modes: on the near-white page the fill + shadow alone don't
  // separate it, so --edge carries the boundary. In DARK mode the fill is one step
  // lighter than --card (mixed from the tokens), which lifts it off the transcript.
  return (
    <div className="flex flex-col gap-1.5">
      <div
        className={cn(
          "relative border border-edge bg-card px-2 shadow-sm dark:bg-[color-mix(in_oklch,var(--muted)_40%,var(--card))]",
          // See MOVE_MS.
          CARD_TRANSITION,
          // Slimmer and squarer at rest; the radius opens up with the box.
          twoLine || asking ? "rounded-2xl py-2.5" : "rounded-xl py-1.5"
        )}
      >
        {asking ? (
          <div className="px-2">
            <AskHumanForm
              questions={pendingAsk.questions}
              onSubmit={onAnswer}
              disabled={isStreaming}
            />
          </div>
        ) : (
          // One row in both shapes, same DOM nodes: two-line is the field taking
          // `basis-full order-first`, wrapping the buttons below. Moving nodes
          // between containers would remount them (losing the caret), so only
          // classes change.
          <div
            ref={setRowEl}
            // No row gap: wrapped, the buttons' own padding is the space under
            // the text.
            className="flex flex-wrap items-center gap-x-2 gap-y-0"
          >
            {/* The shape mirror (see shapeFor); inside the row to inherit its font. */}
            <span
              ref={mirrorRef}
              aria-hidden="true"
              className="pointer-events-none invisible absolute -z-10 text-sm whitespace-pre"
            />

            <div ref={plusRef} className="flex shrink-0 items-center gap-1.5">
              {offerScope || onRunAnalysis ? (
                <AddMenu
                  onScope={offerScope ? openScopePicker : null}
                  onRunAnalysis={onRunAnalysis}
                />
              ) : null}
              {datasetScope ? (
                <DatasetScopeChip
                  scope={datasetScope}
                  onRemove={() => onScopeChange?.(null)}
                />
              ) : null}
            </div>

            {/* The field, wrapped in a Popover anchored to it so the @-mention
                dataset picker floats above the composer. */}
            <Popover
              open={mentionOpen && canMention}
              onOpenChange={(o) => {
                if (!o) {
                  // Ignore the transient dismiss that fires right after opening
                  // from the "+" menu (its teardown reads as an outside
                  // interaction); real dismisses arrive later.
                  if (performance.now() - scopeOpenedAt.current < 500) return
                  setMentionOpen(false)
                  mentionAtRef.current = -1
                }
              }}
            >
              <PopoverAnchor asChild>
                <div
                  className={cn(
                    "relative flex items-start",
                    twoLine ? "order-first basis-full px-1" : "min-w-0 flex-1",
                    // A lone field (no "+") still wants a little inset.
                    !twoLine && !offerScope && !onRunAnalysis && "pl-1"
                  )}
                >
                  <textarea
                    ref={ref}
                    rows={1}
                    value={text}
                    onChange={onTextChange}
                    onKeyDown={onKeyDown}
                    disabled={disabled}
                    placeholder={
                      isStreaming ? "Streaming response…" : placeholder
                    }
                    className={cn(
                      "okf-thin-scroll max-h-48 min-h-6 min-w-0 flex-1 resize-none bg-transparent text-sm outline-none",
                      // leading-6 matches min-h-6 so the text centres against the
                      // buttons (the default line-height sits it 2px high).
                      "leading-6 placeholder:text-muted-foreground"
                    )}
                    aria-label="Chat message input"
                  />
                </div>
              </PopoverAnchor>
              <PopoverContent
                align="start"
                side="top"
                className="w-72 gap-0 p-0"
              >
                <DatasetMentionList
                  datasets={datasets}
                  onPick={pickDataset}
                  onBackspaceEmpty={dismissMention}
                />
              </PopoverContent>
            </Popover>

            <div ref={sendRef} className="ml-auto flex shrink-0 items-center">
              {/* Ghost, neutral foreground; disabled send stays visibly muted. */}
              {isStreaming ? (
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  className="size-8 text-foreground hover:bg-foreground/5 hover:text-foreground"
                  onClick={() => onStop?.()}
                  aria-label="Stop"
                >
                  <SquareIcon className="size-4" />
                </Button>
              ) : (
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  className="size-8 text-foreground hover:bg-foreground/5 hover:text-foreground disabled:text-muted-foreground"
                  onClick={send}
                  disabled={!canSend}
                  aria-label="Send"
                >
                  <CornerDownRightIcon className="size-4" />
                </Button>
              )}
            </div>
          </div>
        )}
      </div>

      {/* The row under the card: run settings. It stays during an ask so the
          composer reads as opened into a question, not replaced. */}
      <div className="flex items-center justify-between gap-3 px-1">
        <div className="flex min-w-0 items-center gap-1">
          {POLICY_CHECK_ENABLED && onFeaturesChange ? (
            <GuardrailsSetting
              value={policy}
              onChange={(id) => onFeaturesChange(withPolicy(features, id))}
              inactive={Boolean(policy) && !(SQL_AVAILABLE && sql)}
            />
          ) : null}
          {SQL_AVAILABLE && onFeaturesChange ? (
            <SqlSwitch
              checked={sql}
              onChange={(on) => onFeaturesChange(withSql(features, on))}
            />
          ) : null}
        </div>
        <div className="flex items-center gap-0.5">
          <ModelEffortSetting
            model={model}
            modelGroups={modelGroups}
            lockedFamily={lockedFamily}
            onModelChange={onModelChange}
            effort={effort}
            efforts={efforts}
            onEffortChange={onEffortChange}
          />
          {contextSlot}
        </div>
      </div>
    </div>
  )
}
