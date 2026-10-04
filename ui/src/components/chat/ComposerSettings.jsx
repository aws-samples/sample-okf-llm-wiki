// The run settings in the row under the composer card. Left: the Guardrails
// selector and the SQL switch. Right: the model · effort control (Sentry's
// trigger + stepped slider), whose popover header names the model — hovering it
// opens a side flyout listing the models to switch to.

import {
  CheckIcon,
  ChevronRightIcon,
  ShieldCheckIcon,
} from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"

import { EffortSlider } from "@/components/chat/EffortSlider"
import { RollingText } from "@/components/RollingText"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { Switch } from "@/components/ui/switch"
import { GUARDRAIL_OPTIONS } from "@/lib/chatFeatures"
import { FAMILY_LABELS, modelLabel, shortModelLabel } from "@/lib/chatModels"
import { cn } from "@/lib/utils"

// The row's text-button look (Sentry's effort trigger): ghost, text-xs, the hover
// background tight to the text. Foreground-alpha hover — muted is the page fill.
const ROW_BUTTON =
  "h-auto gap-1 rounded-md px-1.5 py-0.5 text-xs font-normal hover:bg-foreground/5"

// --- Guardrails ---------------------------------------------------------------

// Disabled or one of the three check modes. Selectable whatever the SQL switch
// says: the runtime ignores the mode while SQL is off.
// `inactive`: a mode is picked but this run has no SQL tool, so the runtime
// ignores it (chat.sql.normalize_features) — say so instead of implying checks.
export function GuardrailsSetting({ value, onChange, inactive = false }) {
  const current =
    GUARDRAIL_OPTIONS.find((o) => o.id === value) ?? GUARDRAIL_OPTIONS[0]
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className={ROW_BUTTON}
          title={
            inactive
              ? "Guardrails only check SQL — turn SQL on for them to run"
              : "Guardrail checks on the agent's SQL"
          }
        >
          <ShieldCheckIcon className="size-3.5 text-muted-foreground" />
          <span className="text-muted-foreground">Guardrails</span>
          <span className={inactive ? "text-muted-foreground" : "text-foreground"}>
            {current.label}
          </span>
          {inactive ? (
            <span className="text-[11px] text-muted-foreground/80">
              Inactive Without SQL
            </span>
          ) : null}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" side="top" className="min-w-56">
        {GUARDRAIL_OPTIONS.map((o) => {
          const Icon = o.icon
          const selected = o.id === current.id
          return (
            <DropdownMenuItem
              key={o.id || "disabled"}
              onSelect={() => onChange?.(o.id)}
              className="py-1"
            >
              {Icon ? (
                <Icon className="size-3.5 text-muted-foreground" />
              ) : null}
              {/* Icon beside a text column so items-center centers it against
                  BOTH lines. */}
              <span className="flex flex-1 flex-col">
                <span>{o.label}</span>
                {o.description ? (
                  <span className="text-[11px] text-muted-foreground">
                    {o.description}
                  </span>
                ) : null}
              </span>
              <CheckIcon
                className={cn(
                  "size-3.5",
                  selected ? "opacity-100" : "opacity-0"
                )}
              />
            </DropdownMenuItem>
          )
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

// --- SQL ----------------------------------------------------------------------

export function SqlSwitch({ checked, onChange }) {
  return (
    <label
      className="flex cursor-pointer items-center gap-1.5 rounded-md px-1.5 py-0.5 text-xs text-muted-foreground hover:text-foreground"
      title="Let the agent run read-only SQL against the live source data"
    >
      <Switch
        size="sm"
        checked={checked}
        onCheckedChange={onChange}
        aria-label="SQL"
      />
      SQL
    </label>
  )
}

// --- Model · Effort -------------------------------------------------------------

// Display names for effort wire values. Label-only: never pass to onEffortChange.
const EFFORT_LABELS = { xhigh: "Extra" }
const effortLabel = (e) => EFFORT_LABELS[e] ?? e

// Hover-driven Popover timings (Sentry's ActiveAccess flyout): short enough to
// feel like a submenu, and the close delay lets the pointer cross the gap.
const OPEN_DELAY = 100
const CLOSE_DELAY = 200

// The side flyout listing the models, attached to the effort card. Grouped by
// family; a started conversation passes only its own family.
function ModelList({ groups, model, locked, onPick }) {
  return (
    <div className="flex flex-col gap-1">
      {groups.map((g) => (
        <div key={g.family} className="flex flex-col">
          <p className="px-2 pt-1 pb-0.5 text-[11px] font-medium text-muted-foreground">
            {g.label}
          </p>
          {g.models.map((m) => {
            const selected = m.model === model
            return (
              <button
                key={m.model}
                type="button"
                onClick={() => onPick(m.model)}
                aria-current={selected ? "true" : undefined}
                className={cn(
                  "flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm outline-none hover:bg-foreground/5 focus-visible:bg-foreground/5",
                  selected && "font-medium"
                )}
              >
                <span className="min-w-0 flex-1 truncate">{m.label}</span>
                <CheckIcon
                  className={cn(
                    "size-3.5 text-primary",
                    selected ? "opacity-100" : "opacity-0"
                  )}
                />
              </button>
            )
          })}
        </div>
      ))}
      {locked ? (
        <p className="border-t border-border px-2 pt-1.5 pb-0.5 text-[11px] text-muted-foreground">
          {FAMILY_LABELS[locked]} models only in this chat — start a new chat to
          switch provider.
        </p>
      ) : null}
    </div>
  )
}

export function ModelEffortSetting({
  model,
  modelGroups,
  lockedFamily,
  onModelChange,
  effort,
  efforts,
  onEffortChange,
}) {
  const [open, setOpen] = useState(false)
  // The model flyout: hover opens it, a click pins it (Sentry's ActiveAccess).
  const [flyOpen, setFlyOpen] = useState(false)
  const openedBy = useRef("click")
  const timer = useRef(null)
  const modelButtonRef = useRef(null)
  // Read by the hover timers: an already-open (e.g. click-pinned) flyout keeps
  // how it was opened.
  const flyOpenRef = useRef(false)
  useEffect(() => {
    flyOpenRef.current = flyOpen
  }, [flyOpen])

  const cancel = useCallback(() => {
    if (timer.current) clearTimeout(timer.current)
    timer.current = null
  }, [])
  const openSoon = useCallback(() => {
    cancel()
    if (flyOpenRef.current) return
    timer.current = setTimeout(() => {
      openedBy.current = "hover"
      setFlyOpen(true)
    }, OPEN_DELAY)
  }, [cancel])
  const closeSoon = useCallback(() => {
    cancel()
    timer.current = setTimeout(() => {
      if (openedBy.current === "hover") setFlyOpen(false)
    }, CLOSE_DELAY)
  }, [cancel])
  useEffect(() => cancel, [cancel])

  // Closing the card closes its flyout with it.
  const onOpenChange = useCallback(
    (next) => {
      setOpen(next)
      if (!next) {
        cancel()
        setFlyOpen(false)
      }
    },
    [cancel]
  )

  if (!efforts || efforts.length === 0) return null
  const idx = Math.max(0, efforts.indexOf(effort))
  const canSwitch =
    Boolean(onModelChange) &&
    (modelGroups || []).reduce((n, g) => n + g.models.length, 0) > 1

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className={ROW_BUTTON}
          title="Model and reasoning effort"
        >
          <span className="text-foreground">{shortModelLabel(model)}</span>
          <span aria-hidden="true" className="text-muted-foreground/60">
            ·
          </span>
          <span className="text-muted-foreground">Effort</span>
          <span className="text-foreground capitalize">
            {effortLabel(effort)}
          </span>
        </Button>
      </PopoverTrigger>
      {/* align="end" keeps the card inside the column. It opens on the slider,
          not the first tabbable (the model button), so the arrows adjust at once. */}
      <PopoverContent
        align="end"
        side="top"
        className="w-64 gap-0 p-2.5"
        onOpenAutoFocus={(event) => {
          const thumb = event.currentTarget?.querySelector?.('[role="slider"]')
          if (!thumb) return
          event.preventDefault()
          thumb.focus()
        }}
      >
        {/* The flyout anchors to the whole card body, so it opens beside the
            card rather than beside the model button. */}
        <Popover open={flyOpen && canSwitch} onOpenChange={setFlyOpen}>
          <PopoverAnchor asChild>
            <div>
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-baseline gap-1 text-sm font-medium">
                  <span className="text-muted-foreground">Effort</span>
                  <RollingText
                    text={effortLabel(effort)}
                    textClassName="text-foreground capitalize"
                  />
                </div>
                {canSwitch ? (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    aria-label={`Model: ${modelLabel(model)}. Switch model.`}
                    aria-expanded={flyOpen}
                    onPointerEnter={(e) => {
                      if (e.pointerType === "mouse") openSoon()
                    }}
                    onPointerLeave={(e) => {
                      if (e.pointerType === "mouse") closeSoon()
                    }}
                    ref={modelButtonRef}
                    // A click, Enter or tap pins a hover-opened flyout, and
                    // toggles a pinned one.
                    onClick={() => {
                      cancel()
                      const hoverOpen = flyOpen && openedBy.current === "hover"
                      openedBy.current = "click"
                      setFlyOpen(hoverOpen ? true : !flyOpen)
                    }}
                    className="h-auto gap-0.5 rounded-md px-1.5 py-0.5 text-xs font-normal text-muted-foreground hover:bg-foreground/5 hover:text-foreground"
                  >
                    {modelLabel(model)}
                    <ChevronRightIcon className="size-3" />
                  </Button>
                ) : (
                  <span className="px-1.5 text-xs text-muted-foreground">
                    {modelLabel(model)}
                  </span>
                )}
              </div>
              <div className="okf-effort-slider mt-1">
                {/* One stop per level; the dotted pill is CSS (index.css). */}
                <EffortSlider
                  index={idx}
                  count={efforts.length}
                  onIndexChange={(i) => onEffortChange?.(efforts[i] ?? effort)}
                  valueText={effortLabel(effort)}
                  aria-label="Reasoning Effort"
                />
                <div className="mt-1 flex items-center justify-between text-[11px] text-muted-foreground">
                  <span>Faster</span>
                  <span>Smarter</span>
                </div>
              </div>
            </div>
          </PopoverAnchor>
          {/* p-2.5 on the card puts the anchor 10px inside its edge; the extra
              sideOffset clears that plus a small gap. */}
          <PopoverContent
            side="right"
            align="end"
            sideOffset={16}
            className="w-56 gap-0 p-1.5"
            onPointerEnter={cancel}
            onPointerLeave={(e) => {
              if (e.pointerType === "mouse") closeSoon()
            }}
            // Hover must not steal focus from the slider.
            onOpenAutoFocus={(e) => {
              if (openedBy.current === "hover") e.preventDefault()
            }}
            // The model button toggles the flyout itself; its pointer-down must
            // not first dismiss it as an outside interaction.
            onInteractOutside={(e) => {
              if (modelButtonRef.current?.contains(e.target)) e.preventDefault()
            }}
          >
            <ModelList
              groups={modelGroups || []}
              model={model}
              locked={lockedFamily}
              onPick={(m) => {
                cancel()
                setFlyOpen(false)
                if (m !== model) onModelChange?.(m)
              }}
            />
          </PopoverContent>
        </Popover>
      </PopoverContent>
    </Popover>
  )
}
