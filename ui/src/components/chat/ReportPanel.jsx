// The report reading surface — an INLINE side panel on the chat page (ChatPanel
// mounts it in a width-animated clip, like the doc peek) so it PUSHES the chat
// rather than overlaying it, resizable from the PanelShell grip. Hosts the
// composed HTML in a fully-inert iframe, plus a PDF download. The HTML is
// fetched anonymously from its presigned URL (auth lives in the signature,
// vended by the Cognito-authed GET /report/{id}) — and refetched on every open
// (each ReportCard click hands ChatPanel a FRESH target object, and the effect
// keys on that identity), since the presigns are minutes-lived and pdf_url
// must be fresh when clicked.
//
// sandbox="" on purpose: the report is static HTML (charts were rasterized to
// data-URI PNGs at create time) — no scripts, no same-origin, nothing to run.

import { DownloadIcon, XIcon } from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import { PanelShell } from "@/components/chat/PanelShell"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { cn } from "@/lib/utils"

// The applied theme, read off <html>'s `dark` class and tracked LIVE with a
// MutationObserver (ChartFrame's pattern): useTheme() alone can't drive this —
// its value is often "system", which never changes when the applied class
// flips. This is what re-themes an ALREADY-OPEN report when the user switches
// theme (the fix for "close and reopen to re-theme").
function readResolvedTheme() {
  if (typeof document === "undefined") return "light"
  return document.documentElement.classList.contains("dark") ? "dark" : "light"
}

function useResolvedTheme() {
  const [theme, setTheme] = useState(readResolvedTheme)
  useEffect(() => {
    if (typeof document === "undefined") return undefined
    const sync = () => setTheme(readResolvedTheme())
    sync() // catch a flip between initial state + effect attach
    const obs = new MutationObserver(sync)
    obs.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    })
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)")
    mq?.addEventListener?.("change", sync)
    return () => {
      obs.disconnect()
      mq?.removeEventListener?.("change", sync)
    }
  }, [])
  return theme
}

// The document region — the report ADOPTS the app theme: the fetched HTML's
// data-theme switch (set below) selects the composer's dark token set, whose
// page surface mirrors --card, so document and panel share one seamless
// surface in both themes. The iframe fills it edge to edge, no inner border
// or rounding. While the panel is being resized the iframe must not see the
// pointer — an iframe swallows pointermove, which froze shrink-drags dead the
// moment the cursor crossed into the document.
function ReportBody({ loading, error, html, title, resizing = false, className }) {
  return (
    <div className={cn("flex flex-col bg-card", className)}>
      {loading ? (
        <div className="m-auto flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner />
          Loading report…
        </div>
      ) : error ? (
        <p className="m-auto max-w-sm px-4 text-center text-sm text-destructive">
          {error}
        </p>
      ) : (
        <iframe
          sandbox=""
          srcDoc={html}
          title={title}
          className={cn("h-full w-full", resizing && "pointer-events-none")}
        />
      )}
    </div>
  )
}

export function ReportPanel({ api, target, onClose, onResizeStart, resizing = false }) {
  const [state, setState] = useState({
    loading: true,
    error: null,
    html: null,
    pdfUrl: "",
  })

  useEffect(() => {
    // Destructured HERE (not at component scope) so the dep is the target
    // object itself — its per-click identity is the "opened again" signal that
    // re-fetches the minutes-lived presigns even for the same reportId.
    const { reportId } = target || {}
    if (!api || !reportId) return undefined
    let cancelled = false
    setState({ loading: true, error: null, html: null, pdfUrl: "" })
    ;(async () => {
      try {
        const meta = await api.getReport(reportId)
        const res = await fetch(meta.html_url, { credentials: "omit" })
        if (!res.ok) {
          throw new Error(`could not fetch the report document (${res.status})`)
        }
        const html = await res.text()
        if (!cancelled) {
          setState({
            loading: false,
            error: null,
            html,
            pdfUrl: meta.pdf_url || "",
          })
        }
      } catch (e) {
        if (!cancelled) {
          setState({
            loading: false,
            error: e?.message || "failed to load the report",
            html: null,
            pdfUrl: "",
          })
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [api, target])

  const displayTitle = target?.title || "Report"

  // Blend with the app theme, LIVE: the composed HTML carries a dark token
  // set behind [data-theme="dark"] (okf_core.reports) — derive the themed
  // document from the RAW fetch + the observed theme, so a mid-view theme
  // switch re-themes the open report (srcDoc swap, no re-fetch). Downloaded/
  // PDF copies never set the attribute: they stay the light print document.
  const theme = useResolvedTheme()
  const themedHtml = useMemo(
    () =>
      state.html && theme === "dark"
        ? state.html.replace('<html lang="en">', '<html lang="en" data-theme="dark">')
        : state.html,
    [state.html, theme]
  )

  return (
    <PanelShell onResizeStart={onResizeStart} resizing={resizing}>
      <div className="flex items-center gap-2 p-3">
        <span
          className="min-w-0 flex-1 truncate text-sm font-medium text-foreground"
          title={displayTitle}
        >
          {displayTitle}
        </span>
        {state.pdfUrl ? (
          <Button variant="ghost" size="icon" className="size-7 shrink-0" asChild>
            <a
              href={state.pdfUrl}
              download
              aria-label="Download PDF"
              title="Download PDF"
            >
              <DownloadIcon className="size-4" />
            </a>
          </Button>
        ) : null}
        <Button
          variant="ghost"
          size="icon"
          className="size-7 shrink-0"
          onClick={onClose}
          aria-label="Close report panel"
        >
          <XIcon className="size-4" />
        </Button>
      </div>

      {/* Edge to edge below the header — the white paper region IS the content
          area. PanelShell's overflow-hidden card clips it to the rounded
          corners, so the shell stays consistent with the other panels while
          only the content region reads as paper. */}
      <ReportBody
        className="min-h-0 flex-1"
        resizing={resizing}
        loading={state.loading}
        error={state.error}
        html={themedHtml}
        title={displayTitle}
      />
    </PanelShell>
  )
}
