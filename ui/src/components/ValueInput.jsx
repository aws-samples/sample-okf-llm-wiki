// Free-text input with advisory type-ahead from PROFILE evidence (a
// parameter's `observed` domain: the values the harvest's profiling pass saw
// in the bound column). Suggestions narrow as you type; arbitrary text stays
// legal — domains are observations, not law (the executor warns-and-runs on
// a miss). Shared by the computation Run modal (ComputationRunner) and the
// canvas dial bar (CanvasView) so both surfaces read identically.

import { useMemo, useState } from "react"

import { Input } from "@/components/ui/input"
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
} from "@/components/ui/popover"
import { cn } from "@/lib/utils"

export function ValueInput({
  value,
  onChange,
  domain,
  placeholder,
  className = "h-8 flex-1 font-mono text-xs",
}) {
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(-1)
  const query = String(value).trim().toLowerCase()
  const matches = useMemo(() => {
    const all = (domain?.values || []).map(String)
    const hit = all.filter((v) => v.toLowerCase().includes(query))
    return [
      ...hit.filter((v) => v.toLowerCase().startsWith(query)),
      ...hit.filter((v) => !v.toLowerCase().startsWith(query)),
    ]
  }, [domain, query])

  const pick = (v) => {
    onChange(v)
    setActive(-1)
    setOpen(false)
  }

  const onKeyDown = (e) => {
    if (!matches.length) return
    if (e.key === "ArrowDown") {
      e.preventDefault()
      setOpen(true)
      setActive((i) => Math.min(i + 1, matches.length - 1))
    } else if (e.key === "ArrowUp") {
      e.preventDefault()
      setActive((i) => Math.max(i - 1, -1))
    } else if (e.key === "Enter" && open && active >= 0) {
      e.preventDefault()
      pick(matches[active])
    } else if (e.key === "Escape" && open) {
      // Swallow it: close the suggestions, not the whole dialog.
      e.stopPropagation()
      setOpen(false)
    }
  }

  return (
    <Popover open={open && matches.length > 0} onOpenChange={setOpen}>
      <PopoverAnchor asChild>
        <Input
          className={className}
          placeholder={placeholder}
          value={value}
          onFocus={() => setOpen(true)}
          onBlur={() => setOpen(false)}
          onChange={(e) => {
            onChange(e.target.value)
            setOpen(true)
            setActive(-1)
          }}
          onKeyDown={onKeyDown}
        />
      </PopoverAnchor>
      <PopoverContent
        align="start"
        className="max-h-56 w-(--radix-popover-trigger-width) gap-0 overflow-y-auto rounded-md p-1"
        onOpenAutoFocus={(e) => e.preventDefault()}
        onMouseDown={(e) => e.preventDefault()}
      >
        {matches.map((v, i) => (
          <button
            type="button"
            key={v}
            ref={
              i === active
                ? (el) => el?.scrollIntoView({ block: "nearest" })
                : undefined
            }
            onClick={() => pick(v)}
            className={cn(
              "w-full shrink-0 rounded-sm px-2 py-1 text-left font-mono text-xs",
              i === active
                ? "bg-accent text-accent-foreground"
                : "hover:bg-accent/50"
            )}
          >
            {v}
          </button>
        ))}
        {!domain?.exhaustive && (
          <p className="shrink-0 px-2 pt-1 pb-0.5 text-[10px] text-muted-foreground">
            observed values — not exhaustive, free text is fine
          </p>
        )}
      </PopoverContent>
    </Popover>
  )
}
