// Attested Computations in the Browse view (docs/ATTESTED_COMPUTATIONS.md §6),
// mirroring the compiled-metric treatment: the doc renders a brand-tinted
// BANNER between the frontmatter header and the prose (primary alpha, not
// `muted` — muted matches the page background in this theme) carrying the
// parameter contracts, the merged verification state, and the Run button; the
// doc header's action row carries Verify (opening its own dialog), and the
// header's tag chips include the verification badge.
//
// Pieces (BrowseView composes them; the CONTRACT — parameters, sql, hash,
// verification, observed domains — is fetched once per selected doc by
// FilesPane via api.getComputation, so banner + both dialogs share one load):
//   VerificationBadge        — verified / unverified / stale chip
//   ComputationBanner        — the blue inline panel + Run button
//   RunComputationDialog     — typed parameter form -> receipt (rows, SQL)
//   VerifyComputationDialog  — the human attestation screen: prose + frozen
//                              SQL + contracts + hash; Verify signs the
//                              CURRENT hash, Unverify revokes (tombstone)

import { useCallback, useEffect, useMemo, useState } from "react"
import {
  BadgeCheckIcon,
  Check,
  Loader2,
  Play,
  ShieldOffIcon,
} from "lucide-react"

import { CodeView } from "@/components/chat/CodeView"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
} from "@/components/ui/popover"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { cn } from "@/lib/utils"

// Free-text input with advisory type-ahead from the profile's observed values
// (the contract's `observed` map). Suggestions narrow as you type; arbitrary
// text stays legal — domains are observations, not law.
function ValueInput({ value, onChange, domain, placeholder }) {
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
          className="h-8 flex-1 font-mono text-xs"
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

// One verification chip, shared by the doc header's tag row, the banner, and
// both dialogs.
export function VerificationBadge({ verification, verifiedBy }) {
  if (verification === "verified") {
    return (
      <Badge
        variant="outline"
        className="gap-1 border-emerald-500/40 text-emerald-600 dark:text-emerald-400"
        title={verifiedBy ? `verified by ${verifiedBy}` : "verified"}
      >
        <BadgeCheckIcon className="size-3" /> verified
      </Badge>
    )
  }
  if (verification === "stale") {
    return (
      <Badge
        variant="outline"
        className="gap-1 border-amber-500/40 text-amber-600 dark:text-amber-400"
        title="the doc changed after it was verified — re-review and re-verify"
      >
        <ShieldOffIcon className="size-3" /> stale
      </Badge>
    )
  }
  return (
    <Badge variant="outline" className="gap-1 text-muted-foreground">
      unverified
    </Badge>
  )
}

// One parameter's contract as a row of TYPED CHIPS (banner + Verify dialog):
// the name pill, a mono type badge, required-or-default, a range chip
// (`1950–2017`, not "min · max" prose), each enum value its own chip, and the
// example as quiet trailing text. Chips over a dot-joined sentence: each fact
// reads at a glance and the eye can scan one kind of fact down the column.
const _chip = "h-5 px-1.5 font-mono text-[10px] font-normal"

function ParamChips({ p }) {
  const hasMin = p.min !== undefined
  const hasMax = p.max !== undefined
  const range = hasMin && hasMax ? `${p.min}–${p.max}` : hasMin ? `≥ ${p.min}` : hasMax ? `≤ ${p.max}` : null
  const enumShown = (p.enum || []).slice(0, 6)
  const enumMore = (p.enum || []).length - enumShown.length
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <code
        className="w-fit rounded bg-primary/10 px-1.5 py-0.5 font-mono text-xs"
        title={p.column ? `filters ${p.column}` : undefined}
      >
        @{p.name}
      </code>
      <Badge variant="outline" className={_chip}>
        {p.type}
      </Badge>
      {p.required ? (
        <Badge variant="secondary" className={cn(_chip, "font-sans")}>
          required
        </Badge>
      ) : (
        <Badge
          variant="outline"
          className={cn(_chip, "text-muted-foreground")}
          title="optional — omitted, this value is used"
        >
          default {String(p.default)}
        </Badge>
      )}
      {range && (
        <Badge
          variant="outline"
          className={cn(_chip, "text-muted-foreground")}
          title="declared bounds — values outside are refused"
        >
          {range}
        </Badge>
      )}
      {enumShown.map((v) => (
        <Badge
          key={String(v)}
          variant="outline"
          className={cn(_chip, "text-muted-foreground")}
          title="declared value set — anything else is refused"
        >
          {String(v)}
        </Badge>
      ))}
      {enumMore > 0 && (
        <span className="text-[10px] text-muted-foreground">+{enumMore} more</span>
      )}
      <span className="text-xs text-muted-foreground">e.g. {String(p.example)}</span>
    </div>
  )
}

// The inline computation banner: rendered by ConceptDoc between the header
// and the prose (same placement + tint as the compiled-metric banner).
export function ComputationBanner({ contract, onRun }) {
  const params = contract?.parameters || []
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-primary/25 bg-primary/5 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge className="border-transparent bg-primary/15 text-primary hover:bg-primary/15">
          Attested Computation
        </Badge>
        <VerificationBadge
          verification={contract?.verification}
          verifiedBy={contract?.verified_by}
        />
        {onRun && (
          <Button
            variant="outline"
            size="sm"
            className="ml-auto h-6 gap-1 border-primary/30 px-2 text-xs text-primary hover:text-primary"
            onClick={onRun}
          >
            <Play className="size-3" />
            Run
          </Button>
        )}
      </div>
      <dl className="grid grid-cols-[auto_1fr] items-baseline gap-x-4 gap-y-1 text-sm">
        <dt className="text-muted-foreground">Runtime</dt>
        <dd className="font-mono text-xs">{contract?.runtime}</dd>
        {params.length > 0 && (
          <>
            <dt className="text-muted-foreground">Parameters</dt>
            <dd className="flex flex-col gap-1.5">
              {params.map((p) => (
                <ParamChips key={p.name} p={p} />
              ))}
            </dd>
          </>
        )}
        {contract?.verification === "verified" && contract?.verified && (
          <>
            <dt className="text-muted-foreground">Verified</dt>
            <dd className="text-xs text-muted-foreground">
              by {contract.verified_by || "unknown"} on{" "}
              {String(contract.verified).slice(0, 10)}
            </dd>
          </>
        )}
      </dl>
    </div>
  )
}

// The Run modal: one typed input per parameter (declared default pre-filled,
// enum as a select — a declared enum is CONTRACT: the executor refuses values
// outside it), executes on demand, renders the receipt (rows + the exact
// executed SQL + warnings + verification status).
export function RunComputationDialog({
  open,
  onOpenChange,
  api,
  domain,
  dataset,
  slug,
  contract,
}) {
  const [values, setValues] = useState({})
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState(null)
  const [runError, setRunError] = useState("")

  useEffect(() => {
    if (!open) return
    setResult(null)
    setRunError("")
    const init = {}
    for (const p of contract?.parameters || []) {
      if (p.default !== undefined) init[p.name] = String(p.default)
    }
    setValues(init)
  }, [open, contract])

  const run = useCallback(async () => {
    setRunning(true)
    setRunError("")
    setResult(null)
    try {
      // Only send what the human filled — omitted optionals take their
      // declared defaults server-side (the contract, not the form, is law).
      const params = {}
      for (const p of contract?.parameters || []) {
        const v = values[p.name]
        if (v !== undefined && String(v).trim() !== "") params[p.name] = v
      }
      setResult(await api.runComputation(domain, dataset, slug, params))
    } catch (e) {
      setRunError(String(e.message || e))
    } finally {
      setRunning(false)
    }
  }, [api, domain, dataset, slug, contract, values])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[85vh] flex-col overflow-hidden sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 font-mono text-base">
            {slug}
            <VerificationBadge
              verification={contract?.verification}
              verifiedBy={contract?.verified_by}
            />
          </DialogTitle>
          <DialogDescription>
            {contract?.description ||
              "Run this attested computation by filling its typed parameters."}
          </DialogDescription>
        </DialogHeader>

        <div className="okf-thin-scroll flex min-h-0 flex-col gap-4 overflow-y-auto pr-3">
          {(contract?.parameters || []).length > 0 && (
            <div className="flex flex-col gap-2">
              {(contract?.parameters || []).map((p) => (
                <div key={p.name} className="flex items-center gap-2">
                  <span
                    className="w-32 shrink-0 truncate font-mono text-xs"
                    title={p.column ? `filters ${p.column}` : undefined}
                  >
                    {p.name}
                    {p.required && <span className="text-destructive"> *</span>}
                  </span>
                  <span className="w-20 shrink-0 text-xs text-muted-foreground">
                    {p.type}
                  </span>
                  {p.enum ? (
                    <Select
                      value={values[p.name] ?? ""}
                      onValueChange={(v) =>
                        setValues((all) => ({ ...all, [p.name]: v }))
                      }
                    >
                      <SelectTrigger
                        size="sm"
                        className="flex-1 font-mono text-xs"
                      >
                        <SelectValue placeholder={String(p.example)} />
                      </SelectTrigger>
                      <SelectContent>
                        {p.enum.map((v) => (
                          <SelectItem
                            key={String(v)}
                            value={String(v)}
                            className="font-mono text-xs"
                          >
                            {String(v)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  ) : p.type === "boolean" ? (
                    <Select
                      value={values[p.name] ?? ""}
                      onValueChange={(v) =>
                        setValues((all) => ({ ...all, [p.name]: v }))
                      }
                    >
                      <SelectTrigger
                        size="sm"
                        className="flex-1 font-mono text-xs"
                      >
                        <SelectValue placeholder={String(p.example)} />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="true" className="font-mono text-xs">
                          true
                        </SelectItem>
                        <SelectItem value="false" className="font-mono text-xs">
                          false
                        </SelectItem>
                      </SelectContent>
                    </Select>
                  ) : (
                    <ValueInput
                      value={values[p.name] ?? ""}
                      domain={contract?.observed?.[p.name]}
                      placeholder={`e.g. ${p.example}`}
                      onChange={(v) =>
                        setValues((all) => ({ ...all, [p.name]: v }))
                      }
                    />
                  )}
                </div>
              ))}
            </div>
          )}

          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">
              runtime: {contract?.runtime}
            </span>
            <Button
              size="sm"
              className="ml-auto gap-1.5"
              onClick={run}
              disabled={running}
            >
              {running ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <Play className="size-3.5" />
              )}
              Run
            </Button>
          </div>

          {runError && (
            <p className="rounded-md border border-destructive/30 bg-destructive/5 p-2 text-xs whitespace-pre-wrap text-destructive">
              {runError}
            </p>
          )}

          {result && (
            <div className="flex flex-col gap-2">
              {result.executed ? (
                <>
                  <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <Check className="size-3.5 text-primary" />
                    {result.row_count} row{result.row_count === 1 ? "" : "s"}
                    {result.truncated && (
                      <Badge variant="outline">truncated</Badge>
                    )}
                    {result.stats?.data_scanned_bytes != null && (
                      <span>
                        {(result.stats.data_scanned_bytes / 1048576).toFixed(1)}{" "}
                        MB scanned
                      </span>
                    )}
                  </div>
                  <div className="max-h-64 overflow-auto rounded-md border">
                    <table className="w-full text-xs">
                      <thead className="sticky top-0 bg-background">
                        <tr>
                          {(result.columns || []).map((c) => (
                            <th
                              key={c}
                              className="border-b px-2 py-1.5 text-left font-mono font-medium"
                            >
                              {c}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {(result.rows || []).map((row, i) => (
                          <tr key={i} className="odd:bg-foreground/[0.02]">
                            {row.map((v, j) => (
                              <td key={j} className="px-2 py-1 font-mono">
                                {v == null ? (
                                  <span className="opacity-40">null</span>
                                ) : (
                                  v
                                )}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              ) : (
                <p className="text-xs text-muted-foreground">
                  {result.note ||
                    "Execution is not enabled — rendered SQL below."}
                </p>
              )}
              {result.executed && result.note && (
                <p className="text-xs text-amber-600 dark:text-amber-400">
                  {result.note}
                </p>
              )}
              {(result.warnings || []).map((w) => (
                <p key={w} className="text-xs text-amber-600 dark:text-amber-400">
                  ⚠ {w}
                </p>
              ))}
              <details className="text-xs">
                <summary className="cursor-pointer text-muted-foreground">
                  Executed SQL
                </summary>
                {/* The shared CodeView (language label + copy + highlight) —
                    the same chrome SQL gets everywhere else in the app. */}
                <div className="mt-1">
                  <CodeView code={result.executed_sql || ""} language="sql" />
                </div>
              </details>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}

// The Verify dialog: everything a human must read before attesting — the
// business description, the FROZEN statement, the parameter contracts, and
// the content hash the click will sign. Verify signs the CURRENT hash (a doc
// edited after this screen loaded simply reads as stale afterwards — the
// binding self-corrects); Unverify revokes (a tombstone server-side, so an
// already-folded doc stamp cannot resurrect). Identity comes from the JWT on
// the server — nothing user-supplied rides the request.
export function VerifyComputationDialog({
  open,
  onOpenChange,
  api,
  domain,
  dataset,
  slug,
  contract,
  onChanged = null,
}) {
  const [flipping, setFlipping] = useState(false)
  const [flipError, setFlipError] = useState("")

  useEffect(() => {
    if (open) setFlipError("")
  }, [open])

  const flip = useCallback(
    async (verb) => {
      setFlipping(true)
      setFlipError("")
      try {
        const out =
          verb === "verify"
            ? // Send the hash THIS dialog showed — the server 409s if the
              // doc changed since, instead of signing unseen content.
              await api.verifyComputation(domain, dataset, slug, contract?.sha256)
            : await api.unverifyComputation(domain, dataset, slug)
        onChanged?.(out)
        if (verb === "verify") onOpenChange(false)
      } catch (e) {
        setFlipError(String(e.message || e))
      } finally {
        setFlipping(false)
      }
    },
    [api, domain, dataset, slug, contract, onChanged, onOpenChange]
  )

  const verification = contract?.verification || "unverified"

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[85vh] flex-col overflow-hidden sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 font-mono text-base">
            {slug}
            <VerificationBadge
              verification={verification}
              verifiedBy={contract?.verified_by}
            />
          </DialogTitle>
          <DialogDescription>
            Verifying attests — in your name — that this exact statement and
            its parameter contracts encode the intended business logic. Read
            everything below first; any later edit to the doc visibly voids
            the stamp.
          </DialogDescription>
        </DialogHeader>

        <div className="okf-thin-scroll flex min-h-0 flex-col gap-3 overflow-y-auto pr-3">
          {contract?.description && (
            <p className="text-sm">{contract.description}</p>
          )}

          <div className="flex items-center gap-2">
            <span className="text-sm font-medium">The frozen statement</span>
            <span className="text-xs text-muted-foreground">
              runtime: {contract?.runtime}
            </span>
            <span
              className="ml-auto font-mono text-[10px] text-muted-foreground"
              title="content hash — what a verification signs"
            >
              sha256:{String(contract?.sha256 || "").slice(0, 12)}…
            </span>
          </div>
          {/* The shared CodeView — highlighted, copyable, capped scroll —
              so the statement a human signs reads exactly like SQL does
              everywhere else in the app. */}
          <CodeView code={contract?.sql || ""} language="sql" />

          {(contract?.parameters || []).length > 0 && (
            <div className="flex flex-col gap-1.5">
              <span className="text-sm font-medium">Parameter contracts</span>
              {(contract?.parameters || []).map((p) => (
                <ParamChips key={p.name} p={p} />
              ))}
            </div>
          )}

          {verification === "stale" && (
            <p className="text-xs text-amber-600 dark:text-amber-400">
              This doc changed after it was last verified — the old stamp no
              longer binds. Re-read the statement above, then re-verify to
              sign the new content (or unverify to clear it).
            </p>
          )}
          {verification === "verified" && contract?.verified && (
            <p className="text-xs text-muted-foreground">
              Verified by {contract.verified_by || "unknown"} on{" "}
              {String(contract.verified).slice(0, 10)}. Unverifying revokes
              that stamp for every consumer.
            </p>
          )}
          {flipError && <p className="text-xs text-destructive">{flipError}</p>}

          <div className="flex items-center gap-2">
            {verification !== "verified" && (
              <Button
                size="sm"
                className="gap-1.5"
                disabled={flipping}
                onClick={() => flip("verify")}
              >
                {flipping ? (
                  <Loader2 className="size-3.5 animate-spin" />
                ) : (
                  <BadgeCheckIcon className="size-3.5" />
                )}
                Verify
              </Button>
            )}
            {verification !== "unverified" && (
              <Button
                size="sm"
                variant="outline"
                className="gap-1.5 text-muted-foreground"
                disabled={flipping}
                onClick={() => flip("unverify")}
              >
                {flipping ? (
                  <Loader2 className="size-3.5 animate-spin" />
                ) : (
                  <ShieldOffIcon className="size-3.5" />
                )}
                Unverify
              </Button>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
