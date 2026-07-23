// The chat transcript — a scrolling list of turns + the composer, hand-rolled
// (no assistant-ui), replicating Sparky's layout + scroll behavior. Three states:
//
//   - LOADING (resumed history in flight): skeleton placeholders.
//   - EMPTY (new chat): a centered greeting with the composer in the MIDDLE of the
//     page (Sparky's welcome screen). On first send it flips to the list view.
//   - POPULATED: a new message scrolls its question to the TOP of the viewport and
//     the answer streams in below it (view holds still — we scroll once when the
//     turn arrives, NOT per token). The tail turn carries a ~full-viewport
//     min-height so a short answer still pins its question up top. Composer docks
//     at the bottom.

import { AlertCircleIcon, ArrowDownIcon } from "lucide-react"
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react"

import { ChatInput } from "@/components/chat/ChatInput"
import { ChatMessage } from "@/components/chat/ChatMessage"
import { WikiCubeIcon } from "@/components/WikiCubeIcon"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"

// Loading placeholder for a resumed conversation — a couple of turn-shaped
// skeletons (a right-aligned question pill + a left-aligned answer block).
function HistorySkeleton() {
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-4 py-6">
      {[0, 1].map((i) => (
        <div key={i} className="flex flex-col gap-3">
          <div className="flex justify-end">
            <Skeleton className="h-8 w-52 rounded-2xl" />
          </div>
          <div className="flex flex-col gap-2">
            <Skeleton className="h-4 w-40" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-11/12" />
            <Skeleton className="h-4 w-3/4" />
          </div>
        </div>
      ))}
    </div>
  )
}

const FOOTER_NOTE =
  "The agent reads the wiki to answer. It can be wrong — verify against the source docs."

export function ChatThread({
  chatTurns,
  isStreaming,
  error,
  loadingHistory = false,
  emptyGreeting = "How can I help with the wiki?",
  emptyHint,
  onSend,
  onAnswer,
  pendingAsk = null,
  onStop,
  onPrepare,
  effort,
  efforts,
  onEffortChange,
  features,
  onFeaturesChange,
  datasets,
  datasetScope,
  onScopeChange,
  composerLeftSlot,
  disabled,
}) {
  const viewportRef = useRef(null)
  const lastTurnRef = useRef(null)
  const prevCountRef = useRef(0)
  const roRef = useRef(null)
  const [showButton, setShowButton] = useState(false)
  const [viewportH, setViewportH] = useState(0)
  // The tail turn's reserved height is FROZEN when a new turn arrives — it must
  // NOT track viewportH live, or the composer auto-growing (as you type multiple
  // lines) shrinks the viewport → recomputes this → jerks the transcript. Only
  // new turns update it.
  const [tailMinH, setTailMinH] = useState(0)

  const isEmpty = chatTurns.length === 0
  const showWelcome = isEmpty && !loadingHistory

  // CALLBACK REF: measure the viewport whenever the node attaches — not just on
  // first mount. The transcript mounts LATER than the component (a fresh chat
  // starts in the welcome branch, where this div doesn't exist), so a plain
  // mount-effect measured null and left viewportH=0 → the tail turn got no
  // min-height → no space + no scroll-to-top. Measuring on attach fixes that.
  const setViewportEl = useCallback((el) => {
    viewportRef.current = el
    if (roRef.current) {
      roRef.current.disconnect()
      roRef.current = null
    }
    if (!el) return
    const measure = () => setViewportH(el.clientHeight)
    measure()
    roRef.current = new ResizeObserver(measure)
    roRef.current.observe(el)
  }, [])

  useEffect(() => () => roRef.current?.disconnect(), [])

  const checkScroll = useCallback(() => {
    const el = viewportRef.current
    if (!el) return
    const gap = el.scrollHeight - el.scrollTop - el.clientHeight
    // Show the jump-to-bottom button EARLY — as soon as content starts slipping
    // behind the bottom fade, not after a big scroll. Small threshold so it's a
    // hint the moment you leave the bottom.
    setShowButton(gap > 24)
  }, [])

  const scrollToBottom = useCallback((smooth = true) => {
    const el = viewportRef.current
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" })
  }, [])

  // Bring the last turn's TOP near the top of the viewport (Sparky's new-message
  // behavior). Leave a gap (TOP_GAP) above it so the question lands just BELOW the
  // top scroll-fade (~40px) rather than dissolving into it or sitting flush.
  const TOP_GAP = 44
  const scrollLastTurnToTop = useCallback((smooth = true) => {
    const el = viewportRef.current
    const turn = lastTurnRef.current
    if (!el || !turn) return
    const delta =
      turn.getBoundingClientRect().top - el.getBoundingClientRect().top - TOP_GAP
    el.scrollTo({ top: el.scrollTop + delta, behavior: smooth ? "smooth" : "auto" })
  }, [])

  useLayoutEffect(() => {
    const count = chatTurns.length
    const prev = prevCountRef.current
    if (count === 0) {
      prevCountRef.current = 0
      return
    }
    // Bulk load (resumed history: 0 → many turns at once) → jump to the bottom to
    // show the latest, no pin.
    if (prev === 0 && count > 1) {
      scrollToBottom(false)
      prevCountRef.current = count
      return
    }
    // A single NEW turn was appended (a fresh send) → pin its question to the top
    // with the answer growing beneath (Sparky). Wait until the viewport height is
    // known so the tail min-height (the room to scroll into + the gap above the
    // composer) is in place; otherwise there's nothing to scroll against and the
    // pin no-ops. Once viewportH lands this effect re-runs and pins.
    if (count > prev) {
      if (!viewportH) return // don't advance prevCount; retry when height lands
      // Freeze the tail's reserved height at THIS moment (won't move as the
      // composer grows while typing) — enough for the question to pin near top.
      // Measure the viewport LIVE here, not via viewportH state: with a
      // MULTI-LINE draft the composer is tall at send time (transcript short),
      // and the ResizeObserver's post-send update hasn't landed yet — a reserve
      // frozen from that stale height is too small once the composer collapses
      // back to one row, and the pin falls short. The child ChatInput has
      // already reset by the time this parent layout effect runs (its resize is
      // a layout effect), so clientHeight is the settled, post-collapse height.
      const vh = viewportRef.current?.clientHeight || viewportH
      const h = Math.max(vh - 132, 0)
      setTailMinH(h)
      // Apply the reserve to the DOM NOW, not on the next render: scrollTo
      // clamps its target against scrollHeight at CALL time, and the state
      // update above hasn't reached the DOM yet — without the reserve in place
      // the pin falls short whenever the transcript is still shallow (the
      // "sometimes the question doesn't scroll up" flakiness). The re-render
      // then just confirms the same value.
      if (lastTurnRef.current) {
        lastTurnRef.current.style.minHeight = `${h}px`
      }
      scrollLastTurnToTop(true)
    }
    prevCountRef.current = count
  }, [chatTurns.length, viewportH, scrollToBottom, scrollLastTurnToTop])

  // --- EMPTY (new chat): greeting + composer centered in the page ------------
  if (showWelcome) {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col items-center justify-center gap-6 px-4">
          <div className="text-center">
            <h2 className="flex items-center justify-center gap-2.5 text-2xl font-semibold tracking-tight">
              <WikiCubeIcon className="size-12 shrink-0 text-primary" />
              {emptyGreeting}
            </h2>
            {emptyHint ? (
              <p className="mt-2 text-sm text-muted-foreground">{emptyHint}</p>
            ) : null}
          </div>
          <div className="w-full">
            <ChatInput
              onSend={onSend}
              onAnswer={onAnswer}
              pendingAsk={pendingAsk}
              onStop={onStop}
              onPrepare={onPrepare}
              isStreaming={isStreaming}
              disabled={disabled}
              leftSlot={composerLeftSlot}
              effort={effort}
              efforts={efforts}
              onEffortChange={onEffortChange}
              features={features}
              onFeaturesChange={onFeaturesChange}
              datasets={datasets}
              datasetScope={datasetScope}
              onScopeChange={onScopeChange}
            />
            <p className="mt-1.5 text-center text-[11px] text-muted-foreground/60">
              {FOOTER_NOTE}
            </p>
          </div>
        </div>
      </div>
    )
  }

  // --- LOADING / POPULATED: scroll list + docked composer --------------------
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* okf-chat-fade (index.css) dissolves BOTH edges so text fades out (not
          hard-cuts) as it scrolls under the composer (bottom, ~4rem) and off the
          top of the layout (~2.5rem). The inner pt-10/pb-24 buffer the first/last
          lines so they clear the mask at rest. */}
      <div
        ref={setViewportEl}
        onScroll={checkScroll}
        className="okf-thin-scroll okf-chat-fade min-h-0 flex-1 overflow-y-auto"
      >
        {loadingHistory ? (
          <HistorySkeleton />
        ) : (
          <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-4 pt-10 pb-24">
            {/* pb-24: gap below the LAST message so it clears the composer + the
                bottom scroll-fade at rest, instead of ending right at the edge. */}
            {chatTurns.map((turn, i) => {
              const isLast = i === chatTurns.length - 1
              return (
                <div
                  key={turn.id}
                  ref={isLast ? lastTurnRef : null}
                  // Tail turn reserves just enough height that its question can
                  // PIN near the top. Uses the FROZEN tailMinH (captured when the
                  // turn arrived), NOT live viewportH — so the composer growing as
                  // you type doesn't resize this and jerk the transcript.
                  style={
                    isLast && tailMinH ? { minHeight: `${tailMinH}px` } : undefined
                  }
                >
                  <ChatMessage
                    turn={turn}
                    streaming={isStreaming && isLast}
                    datasetScope={datasetScope}
                  />
                </div>
              )
            })}
            {error ? (
              <div className="flex items-center gap-2 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                <AlertCircleIcon className="size-4 shrink-0" />
                <span>{error}</span>
              </div>
            ) : null}
          </div>
        )}
      </div>

      {/* Composer, centered + capped to match the transcript column. */}
      <div className="relative mx-auto w-full max-w-3xl px-4 pb-4">
        {showButton ? (
          <Button
            type="button"
            variant="outline"
            size="icon"
            onClick={() => scrollToBottom(true)}
            className="absolute -top-9 left-1/2 size-7 -translate-x-1/2 rounded-full shadow"
            aria-label="Scroll to bottom"
          >
            <ArrowDownIcon className="size-3.5" />
          </Button>
        ) : null}

        <ChatInput
          onSend={onSend}
          onAnswer={onAnswer}
          pendingAsk={pendingAsk}
          onStop={onStop}
          onPrepare={onPrepare}
          isStreaming={isStreaming}
          disabled={disabled || loadingHistory}
          leftSlot={composerLeftSlot}
          effort={effort}
          efforts={efforts}
          onEffortChange={onEffortChange}
          features={features}
          onFeaturesChange={onFeaturesChange}
          datasets={datasets}
          datasetScope={datasetScope}
          onScopeChange={onScopeChange}
        />
        <p className="mt-1.5 text-center text-[11px] text-muted-foreground/60">
          {FOOTER_NOTE}
        </p>
      </div>
    </div>
  )
}
