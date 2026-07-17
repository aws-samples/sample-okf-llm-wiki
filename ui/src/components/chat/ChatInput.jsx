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
  PlusIcon,
  SlidersHorizontalIcon,
  SquareIcon,
  XIcon,
} from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"

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
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { Slider } from "@/components/ui/slider"
import { AVAILABLE_FEATURES, featureById } from "@/lib/chatFeatures"
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
    <span className="group/chip inline-flex h-6 items-center gap-1 rounded-full border border-primary/25 bg-primary/10 pr-1 pl-2 text-xs font-medium text-primary">
      <AtSignIcon className="size-3 opacity-80" />
      {datasetKey(scope)}
      <button
        type="button"
        aria-label="Clear dataset scope"
        onClick={onRemove}
        className="ml-0.5 flex size-4 items-center justify-center rounded-full text-primary/70 transition-colors hover:bg-primary/15 hover:text-primary"
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
    <span className="group/chip inline-flex h-6 items-center gap-1 rounded-full border bg-muted/60 pr-1 pl-2 text-xs font-medium text-foreground/80">
      {Icon ? <Icon className="size-3 text-muted-foreground" /> : null}
      {feature.label}
      <button
        type="button"
        aria-label={`Disable ${feature.label}`}
        onClick={onRemove}
        className="ml-0.5 flex size-4 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-foreground/10 hover:text-foreground"
      >
        <XIcon className="size-3" />
      </button>
    </span>
  )
}

function AddFeatureMenu({ enabled, onToggle }) {
  const [open, setOpen] = useState(false)
  const enabledSet = new Set(enabled)
  // Nothing left to add once every available feature is enabled — hide the "+".
  const remaining = AVAILABLE_FEATURES.filter((f) => !enabledSet.has(f.id))
  if (remaining.length === 0) return null

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-7 shrink-0 rounded-full text-muted-foreground hover:text-foreground"
          title="Add a capability"
          aria-label="Add a capability"
        >
          <PlusIcon className="size-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" side="top" className="w-60">
        <DropdownMenuLabel className="text-xs text-muted-foreground">
          Add a capability
        </DropdownMenuLabel>
        {remaining.map((f) => {
          const Icon = f.icon
          return (
            <DropdownMenuItem
              key={f.id}
              onSelect={() => onToggle(f.id)}
              className="flex-col items-start gap-0.5"
            >
              <span className="flex items-center gap-2">
                {Icon ? <Icon className="size-3.5 text-muted-foreground" /> : null}
                {f.menuLabel || f.label}
              </span>
              {f.description ? (
                <span className="pl-5.5 text-[11px] text-muted-foreground">
                  {f.description}
                </span>
              ) : null}
            </DropdownMenuItem>
          )
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

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
          className="h-7 gap-1 rounded-full px-2 text-xs text-muted-foreground capitalize hover:text-foreground"
          title="Reasoning effort"
        >
          <SlidersHorizontalIcon className="size-3.5" />
          {effort}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" side="top" className="w-60 rounded-xl p-2.5">
        {/* Header: "Effort <Level>" with the level highlighted in the accent. */}
        <div className="text-sm font-medium">
          Effort <span className="text-primary capitalize">{effort}</span>
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
  placeholder = "Ask about the wiki…",
  leftSlot = null,
  autoFocus = true,
  effort,
  efforts,
  onEffortChange,
  features = [],
  onFeaturesChange,
  datasets = [],
  datasetScope = null,
  onScopeChange,
}) {
  const [text, setText] = useState("")
  const ref = useRef(null)

  // `@`-mention picker: open + the query typed after the "@" (used to seed the
  // picker's filter). The trigger "@"'s index lets us strip the fragment on pick.
  const [mentionOpen, setMentionOpen] = useState(false)
  const [mentionQuery, setMentionQuery] = useState("")
  const mentionAtRef = useRef(-1) // index of the active "@" in the textarea value
  const canMention = Boolean(onScopeChange) && datasets.length > 0

  const enabledFeatures = Array.isArray(features) ? features : []
  const addFeature = useCallback(
    (id) => {
      if (!onFeaturesChange) return
      if (enabledFeatures.includes(id)) return
      onFeaturesChange([...enabledFeatures, id])
    },
    [enabledFeatures, onFeaturesChange]
  )
  const removeFeature = useCallback(
    (id) => onFeaturesChange?.(enabledFeatures.filter((f) => f !== id)),
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

  useEffect(() => {
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

  return (
    <div className="flex flex-col gap-2 rounded-3xl border bg-card px-4 py-3 shadow-sm">
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
        {onFeaturesChange ? (
          <AddFeatureMenu enabled={enabledFeatures} onToggle={addFeature} />
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
              className="size-8 rounded-full"
              onClick={() => onStop?.()}
              aria-label="Stop"
            >
              <SquareIcon className="size-3.5 fill-current" />
            </Button>
          ) : (
            <Button
              type="button"
              size="icon"
              className="size-8 rounded-full"
              onClick={send}
              disabled={!canSend}
              aria-label="Send"
            >
              <ArrowUpIcon className="size-4" />
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}
