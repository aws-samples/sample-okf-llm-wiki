// The composer — Sparky's ChatInput, ported to tailwind/shadcn. A rounded card
// with an auto-growing textarea and a toolbar row: an effort setting + optional
// left slot on the left, send/stop on the right. Enter sends (Shift+Enter =
// newline); while streaming the button becomes Stop.
//
// Owns only its own draft text; the parent handles send/stop. Reasoning effort is
// set HERE (Sparky-style, from the composer) rather than the sidebar — it's a
// per-conversation setting, locked once the conversation has started.

import {
  ArrowUpIcon,
  AtSignIcon,
  DatabaseIcon,
  PinIcon,
  PlusIcon,
  ShieldCheckIcon,
  SlidersHorizontalIcon,
  SquareIcon,
  XIcon,
} from "lucide-react"
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react"

import { AskHumanForm } from "@/components/chat/AskHumanForm"
import { RollingText } from "@/components/RollingText"
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
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { Slider } from "@/components/ui/slider"
import {
  AVAILABLE_FEATURES,
  featureById,
  isPolicyId,
  POLICY_AVAILABLE,
  POLICY_OPTIONS,
} from "@/lib/chatFeatures"
import { cn } from "@/lib/utils"

const MAX_HEIGHT = 200

// The dataset key shown in the scope chip / mention list ("domain/dataset").
function datasetKey(d) {
  return `${d.data_domain}/${d.dataset}`
}

// The `@`-mention dataset picker — a Popover(Command) anchored to the composer,
// opened when the user types "@" (see ChatInput). Picking a dataset sets the
// conversation's scope; the current scope shows as a removable chip.
function DatasetScopeChip({ scope, onRemove }) {
  return (
    <span className="group/chip inline-flex h-8 items-center gap-1 rounded-md bg-primary/10 pr-1.5 pl-2.5 text-xs font-medium text-primary">
      <AtSignIcon className="size-3 opacity-80" />
      {datasetKey(scope)}
      <button
        type="button"
        aria-label="Clear dataset scope"
        onClick={onRemove}
        // Zero-width until the CHIP is hovered (saves toolbar width), w-0 rather
        // than hidden so it stays tabbable — keyboard focus re-expands it via
        // group-focus-within.
        className="ml-0 flex h-4 w-0 items-center justify-center overflow-hidden rounded-sm text-primary/70 opacity-0 transition-all group-focus-within/chip:ml-0.5 group-focus-within/chip:w-4 group-focus-within/chip:opacity-100 group-hover/chip:ml-0.5 group-hover/chip:w-4 group-hover/chip:opacity-100 hover:bg-primary/15 hover:text-primary"
      >
        <XIcon className="size-3" />
      </button>
    </span>
  )
}

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

// The "+" menu + enabled-feature chips (Sparky's add-capability affordance). The
// "+" opens a menu of the deployment's optional tools; picking one adds a chip to
// the composer that shows an × on hover to remove it. Only rendered when the
// deployment offers any feature at all (AVAILABLE_FEATURES non-empty).
function FeatureChip({ feature, onRemove }) {
  const Icon = feature.icon
  return (
    <span className="group/chip inline-flex h-8 items-center gap-1 rounded-md bg-muted/60 pr-1.5 pl-2.5 text-xs font-medium text-foreground/80">
      {Icon ? <Icon className="size-3 text-muted-foreground" /> : null}
      {feature.label}
      <button
        type="button"
        aria-label={`Disable ${feature.label}`}
        onClick={onRemove}
        // Zero-width until the CHIP is hovered — same pattern as the scope chip's ×.
        className="ml-0 flex h-4 w-0 items-center justify-center overflow-hidden rounded-sm text-muted-foreground opacity-0 transition-all group-focus-within/chip:ml-0.5 group-focus-within/chip:w-4 group-focus-within/chip:opacity-100 group-hover/chip:ml-0.5 group-hover/chip:w-4 group-hover/chip:opacity-100 hover:bg-foreground/10 hover:text-foreground"
      >
        <XIcon className="size-3" />
      </button>
    </span>
  )
}

// `canScope` adds a "Scope to a dataset" entry (an explicit, discoverable
// alternative to typing "@" — see ChatInput). Picking it fires `onScope`, which
// opens the same dataset picker the "@" mention does.
function AddFeatureMenu({ enabled, onToggle, onPickPolicy, canScope, onScope }) {
  const [open, setOpen] = useState(false)
  // Set when the "Scope to a dataset" item is chosen, so onCloseAutoFocus knows
  // to skip Radix's focus-restore to the "+" trigger — that restore lands
  // OUTSIDE the dataset popover onScope just opened and would dismiss it
  // instantly. onScope focuses the picker itself, so no focus is lost.
  const scopeSelectedRef = useRef(false)
  const enabledSet = new Set(enabled)
  const remaining = AVAILABLE_FEATURES.filter((f) => !enabledSet.has(f.id))
  // The Policy field is offered while no policy option is active. Its hard
  // dependency: it only ENABLES while the SQL feature is checked (the checks
  // judge SQL conduct — nothing to check without the tool). The chip cascade
  // (removing SQL removes Policy) lives in the feature sanitizer.
  const sqlOn = enabledSet.has("sql")
  const policyActive = enabled.some(isPolicyId)
  const offerPolicy = POLICY_AVAILABLE && !policyActive
  // Hide the "+" only when there's genuinely nothing to offer — no remaining
  // features, no policy field, AND no dataset to scope to.
  if (remaining.length === 0 && !offerPolicy && !canScope) return null

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-8 shrink-0 text-muted-foreground hover:text-foreground"
          title="Add a capability"
          aria-label="Add a capability"
        >
          <PlusIcon className="size-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        side="top"
        className="min-w-56"
        onCloseAutoFocus={(e) => {
          if (scopeSelectedRef.current) {
            scopeSelectedRef.current = false
            e.preventDefault()
          }
        }}
      >
        {canScope ? (
          <DropdownMenuItem
            onSelect={() => {
              scopeSelectedRef.current = true
              onScope()
            }}
          >
            <PinIcon className="size-3.5 text-muted-foreground" />
            Scope to a dataset
          </DropdownMenuItem>
        ) : null}
        {remaining.map((f) => {
          const Icon = f.icon
          return (
            <DropdownMenuItem
              key={f.id}
              onSelect={() => onToggle(f.id)}
            >
              {Icon ? <Icon className="size-3.5 text-muted-foreground" /> : null}
              {f.menuLabel || f.label}
            </DropdownMenuItem>
          )
        })}
        {offerPolicy ? (
          <DropdownMenuSub>
            {/* Disabled while SQL is off — the checks judge SQL conduct. */}
            <DropdownMenuSubTrigger disabled={!sqlOn}>
              <ShieldCheckIcon className="size-3.5 text-muted-foreground" />
              Guardrails
            </DropdownMenuSubTrigger>
            {/* Gap to the main card (sideOffset) + bottom edge aligned with
                the BOTTOM of the Guardrails row: Radix top-aligns sub content
                with its trigger and has no align="end" for subs, so shift it
                up by its own height minus the trigger row's 2rem (py-1.5 ×2 +
                one text-sm line). avoidCollisions must be OFF — the composer
                sits at the viewport bottom, so Radix pre-shifts the card up
                to fit and the translate would stack on that shift; with a
                deterministic start (top = trigger top) the math is exact.
                The `translate` property is separate from `transform`, so the
                open/close animation doesn't clobber the offset. */}
            <DropdownMenuSubContent
              sideOffset={8}
              avoidCollisions={false}
              className="translate-y-[calc(-100%+2rem)]"
            >
              {POLICY_OPTIONS.map((o) => {
                const Icon = o.icon
                // Icon beside a text column (not inside the label row) so the
                // item's default items-center centers it against BOTH lines.
                return (
                  <DropdownMenuItem
                    key={o.id}
                    onSelect={() => onPickPolicy(o.id)}
                    className="py-1"
                  >
                    {Icon ? (
                      <Icon className="size-3.5 text-muted-foreground" />
                    ) : null}
                    <span className="flex flex-col">
                      <span>{o.label}</span>
                      {o.description ? (
                        <span className="text-[11px] text-muted-foreground">
                          {o.description}
                        </span>
                      ) : null}
                    </span>
                  </DropdownMenuItem>
                )
              })}
            </DropdownMenuSubContent>
          </DropdownMenuSub>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

// Display names for reasoning-effort values — "xhigh" is the wire value the
// runtime expects but reads badly in the UI, so it shows as "Extra". Label-only:
// never feed these back into onEffortChange.
const EFFORT_LABELS = { xhigh: "Extra" }
const effortLabel = (e) => EFFORT_LABELS[e] ?? e

// The reasoning-effort control: a toolbar button showing the current effort,
// opening a popover with a Faster↔Smarter SLIDER (one stop per model level).
// Changeable at any time, INCLUDING on an existing conversation — effort is
// resolved per-run by the runtime and isn't pinned by the checkpoint (only the
// MODEL is, since Opus/GPT checkpoints aren't portable).
function EffortSetting({ effort, efforts, onChange }) {
  const [open, setOpen] = useState(false)
  if (!efforts || efforts.length === 0) return null

  const idx = Math.max(0, efforts.indexOf(effort))
  const last = efforts.length - 1
  // Filled fraction 0..1. The range paints the SAME full-track light→dark fade
  // (in CSS), scaled by 1/frac so its background image spans the whole track —
  // the range (only `frac` wide) then reveals just the 0→thumb slice of that one
  // gradient. So the fill still fades (never a flat block) and only shows up to
  // the thumb. Guard the divide-by-zero at frac=0.
  const frac = last > 0 ? idx / last : 1

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-8 gap-1 px-2.5 text-xs text-muted-foreground capitalize hover:text-foreground"
          title="Reasoning effort"
        >
          <SlidersHorizontalIcon className="size-3.5" />
          {effortLabel(effort)}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" side="top" className="w-60 p-2.5">
        {/* Header: "Effort <Level>" with the level highlighted in the accent;
            switching levels rolls the old value out below the new one. */}
        <div className="text-sm font-medium">
          Effort{" "}
          <RollingText
            text={effortLabel(effort)}
            textClassName="text-primary capitalize"
          />
        </div>
        {/* Compact: end labels + slider sit tight together, just under the header.
            --okf-effort-frac (0..1) scales the range's fade so the filled slice
            shows the true 0→thumb portion of the light→dark gradient. */}
        <div
          className="okf-effort-slider mt-1"
          style={{ "--okf-effort-frac": frac || 0.0001 }}
        >
          <div className="mb-0.5 flex items-center justify-between text-[11px] text-muted-foreground">
            <span>Faster</span>
            <span>Smarter</span>
          </div>
          {/* Stepped slider: one stop per level; the dotted track is a CSS overlay
              (repeating dots) behind the shadcn Slider's own thin track. */}
          <Slider
            min={0}
            max={last}
            step={1}
            value={[idx]}
            onValueChange={([v]) => onChange?.(efforts[v] ?? effort)}
            aria-label="Reasoning effort"
          />
        </div>
      </PopoverContent>
    </Popover>
  )
}

// Sparky's keep-warm timings (ChatInput.jsx): don't fire in the first 2s after
// mount, fire IMMEDIATELY on the first keystroke of an empty box, then debounce
// 500ms on subsequent typing, and ping every 300s while there's draft text.
const PREPARE_MOUNT_GRACE_MS = 2000
const PREPARE_DEBOUNCE_MS = 500
const PREPARE_INTERVAL_MS = 300000

export function ChatInput({
  onSend,
  onStop,
  onPrepare,
  isStreaming = false,
  disabled = false,
  placeholder = "Ask about the wiki. Use @ to pin questions to a particular dataset",
  leftSlot = null,
  autoFocus = true,
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
}) {
  const [text, setText] = useState("")
  const ref = useRef(null)

  // `@`-mention picker: open + the query typed after the "@" (used to seed the
  // picker's filter). The trigger "@"'s index lets us strip the fragment on pick.
  const [mentionOpen, setMentionOpen] = useState(false)
  const [mentionQuery, setMentionQuery] = useState("")
  const mentionAtRef = useRef(-1) // index of the active "@" in the textarea value
  const canMention = Boolean(onScopeChange) && datasets.length > 0
  // Visibility for the "+" menu's scope entry. Distinct from canMention: with
  // every feature chip enabled, "Scope to a dataset" is the "+"'s only
  // remaining offering, and gating it on the FETCHED list makes the button pop
  // in a beat after first paint. While the list is still loading, assume
  // scoping will be offered (a registered deployment has datasets) so the
  // button mounts with the composer; it hides only when the list is KNOWN
  // empty.
  const offerScope =
    Boolean(onScopeChange) && (datasetsLoading || datasets.length > 0)

  const enabledFeatures = Array.isArray(features) ? features : []
  const addFeature = useCallback(
    (id) => {
      if (!onFeaturesChange) return
      if (enabledFeatures.includes(id)) return
      onFeaturesChange([...enabledFeatures, id])
    },
    [enabledFeatures, onFeaturesChange]
  )
  // Picking a policy option replaces any current one (mutually exclusive).
  const pickPolicy = useCallback(
    (id) =>
      onFeaturesChange?.([...enabledFeatures.filter((f) => !isPolicyId(f)), id]),
    [enabledFeatures, onFeaturesChange]
  )
  // Removing SQL also drops the policy selection (the controller's sanitizer
  // enforces the same dependency; filtering here keeps the UI instant).
  const removeFeature = useCallback(
    (id) =>
      onFeaturesChange?.(
        enabledFeatures.filter(
          (f) => f !== id && !(id === "sql" && isPolicyId(f))
        )
      ),
    [enabledFeatures, onFeaturesChange]
  )

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
      syncMention(e.target.value, e.target.selectionStart ?? e.target.value.length)
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
        // Strip from the "@" through the current query length.
        const before = text.slice(0, at)
        const after = text.slice(at + 1 + mentionQuery.length)
        const next = (before + after).replace(/\s{2,}/g, " ")
        setText(next)
      }
      setMentionOpen(false)
      mentionAtRef.current = -1
      requestAnimationFrame(() => ref.current?.focus())
    },
    [onScopeChange, text, mentionQuery]
  )

  // Open the dataset picker from the "+" menu instead of an "@" keystroke. There
  // is no "@" fragment to strip on pick, so leave mentionAtRef at -1 (pickDataset
  // skips the strip when it's negative). Stamp the open time: the "+" dropdown's
  // teardown (focus restore + outside-pointer detection) fires just after this
  // and the picker popover reads it as an outside interaction — the onOpenChange
  // guard below ignores any dismiss within a short grace window so the picker
  // doesn't flicker shut. (Same pattern as App.jsx's CollapsedNavTrigger.)
  const scopeOpenedAt = useRef(0)
  const openScopePicker = useCallback(() => {
    if (!canMention) return
    mentionAtRef.current = -1
    setMentionQuery("")
    scopeOpenedAt.current = performance.now()
    setMentionOpen(true)
  }, [canMention])

  // Dismiss the picker WITHOUT choosing: strip the "@" (and any query typed after
  // it in the textarea) that triggered it, close, and refocus the composer. Fired
  // by Backspace on the empty picker search — so one keypress undoes the "@".
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

  const grow = useCallback(() => {
    const el = ref.current
    if (!el) return
    el.style.height = "auto"
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`
  }, [])

  // LAYOUT effect, deliberately: the composer must collapse back to one row
  // in the SAME frame the send clears the draft — ChatThread's new-turn pin
  // runs in the parent's layout effect and measures the transcript viewport,
  // so a post-paint resize (plain useEffect) leaves it measuring against the
  // still-tall composer and the pin falls short for multi-line messages. Child
  // layout effects run before the parent's, which is exactly the ordering the
  // pin relies on. (Also removes the one-frame flash of a tall empty composer.)
  useLayoutEffect(() => {
    grow()
  }, [text, grow])

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
    onSend(t)
    setText("")
  }, [text, disabled, isStreaming, onSend])

  const onKeyDown = useCallback(
    (e) => {
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
        if (isStreaming) onStop?.()
        else send()
      }
    },
    [isStreaming, onStop, send, mentionOpen]
  )

  const canSend = text.trim().length > 0 && !disabled && !isStreaming

  // When the agent has paused to ask clarifying questions, the composer becomes
  // the QA form (a natural vertical expansion of the input) — the textarea/toolbar
  // are hidden until the user submits, which resumes the agent.
  const asking = Boolean(pendingAsk && pendingAsk.questions?.length && onAnswer)

  // The composer card is borderless in LIGHT mode (the white card + shadow
  // already separates it from the gray page); dark mode keeps the border — it's
  // the only thing separating surfaces there. border-transparent (not border-0)
  // so the box size is identical in both modes.
  return (
    <div className="flex flex-col gap-2 rounded-2xl border border-transparent bg-card px-4 py-3 shadow-sm dark:border-border">
      {asking ? (
        <AskHumanForm
          questions={pendingAsk.questions}
          onSubmit={onAnswer}
          disabled={isStreaming}
        />
      ) : (
        <>
      {leftSlot ? (
        <div className="flex flex-wrap items-center gap-1.5">{leftSlot}</div>
      ) : null}

      {/* The textarea, wrapped in a Popover anchored to it so the @-mention
          dataset picker floats above the composer. The Command inside autofocuses
          + filters as the user keeps typing; picking sets the scope. */}
      <Popover
        open={mentionOpen && canMention}
        onOpenChange={(o) => {
          if (!o) {
            // Ignore the transient dismiss that fires right after opening from
            // the "+" menu: the dropdown's teardown (focus restore + outside-
            // pointer detection) reads as an outside interaction and would close
            // the picker within a few hundred ms. A short grace window after a
            // programmatic open swallows it; real dismisses arrive later.
            if (performance.now() - scopeOpenedAt.current < 500) return
            setMentionOpen(false)
            mentionAtRef.current = -1
          }
        }}
      >
        <PopoverAnchor asChild>
          <textarea
            ref={ref}
            rows={1}
            value={text}
            onChange={onTextChange}
            onKeyDown={onKeyDown}
            disabled={disabled}
            placeholder={isStreaming ? "Streaming response…" : placeholder}
            className={cn(
              "okf-thin-scroll max-h-48 min-h-6 w-full resize-none bg-transparent text-sm outline-none",
              "placeholder:text-muted-foreground"
            )}
            aria-label="Chat message input"
          />
        </PopoverAnchor>
        <PopoverContent align="start" side="top" className="w-72 p-0">
          {/* CommandInput autofocuses so the user types into the filter. */}
          <DatasetMentionList
            datasets={datasets}
            onPick={pickDataset}
            onBackspaceEmpty={dismissMention}
          />
        </PopoverContent>
      </Popover>

      <div className="flex items-center gap-1">
        {onFeaturesChange || offerScope ? (
          <AddFeatureMenu
            enabled={enabledFeatures}
            onToggle={addFeature}
            onPickPolicy={pickPolicy}
            canScope={offerScope}
            onScope={openScopePicker}
          />
        ) : null}
        <EffortSetting
          effort={effort}
          efforts={efforts}
          onChange={onEffortChange}
        />
        {/* Dataset scope chip — the active @-mention, removable. */}
        {datasetScope ? (
          <DatasetScopeChip
            scope={datasetScope}
            onRemove={() => onScopeChange?.(null)}
          />
        ) : null}
        {/* Enabled-feature chips (Sparky-style) — sit just after the controls;
            each shows an × on hover to disable. Only known+available ids render. */}
        {enabledFeatures.length > 0 ? (
          <div className="flex flex-wrap items-center gap-1">
            {enabledFeatures.map((id) => {
              const feature = featureById(id)
              if (!feature || !feature.available) return null
              return (
                <FeatureChip
                  key={id}
                  feature={feature}
                  onRemove={() => removeFeature(id)}
                />
              )
            })}
          </div>
        ) : null}
        <div className="ml-auto">
          {isStreaming ? (
            <Button
              type="button"
              size="icon"
              variant="outline"
              className="size-8"
              onClick={() => onStop?.()}
              aria-label="Stop"
            >
              <SquareIcon className="size-3.5 fill-current" />
            </Button>
          ) : (
            <Button
              type="button"
              size="icon"
              className="size-8"
              onClick={send}
              disabled={!canSend}
              aria-label="Send"
            >
              <ArrowUpIcon className="size-4" />
            </Button>
          )}
        </div>
      </div>
        </>
      )}
    </div>
  )
}
