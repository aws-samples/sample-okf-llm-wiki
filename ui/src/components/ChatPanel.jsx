// The chat page — now just the transcript + the (optional) history drawer. The
// chat CONTROLS (model/effort select, new-chat, history toggle) live in the
// sidebar as sub-items under "Chat" (see App.jsx ChatNav); their shared state is
// the `ctrl` controller (useChatController), owned by the app shell so the
// sidebar and this page drive the same conversation.
//
// ChatPanel still owns the per-conversation session store (useChatSession) via
// the inner Conversation, keyed by threadId so a new-chat/model-switch/resume
// remounts with clean state.

import { useCallback, useEffect, useRef, useState } from "react"

import { ChatHistory } from "@/components/ChatHistory"
import { ChatThread } from "@/components/ChatThread"
import { DocPeek } from "@/components/chat/DocPeek"
import { PanelShell } from "@/components/chat/PanelShell"
import { CHAT_CONFIGURED } from "@/lib/chatApi"
import { useChatSession } from "@/hooks/useChatSession"
import { cn } from "@/lib/utils"

// The inner surface: mounts the session store. Keyed by conversation upstream.
function Conversation({
  conv,
  getToken,
  onStarted,
  onTurnComplete,
  historyResume,
  efforts,
  onEffortChange,
  onFeaturesChange,
  datasets,
  onScopeChange,
  onOpenDoc,
}) {
  const {
    chatTurns,
    isStreaming,
    error,
    loadingHistory,
    pendingAsk,
    send,
    answerHuman,
    stop,
    resume,
    prepare,
    loadHistory,
  } = useChatSession({
    threadId: conv.threadId,
    getToken,
    model: conv.model,
    effort: conv.effort,
    features: conv.features,
    datasetScope: conv.datasetScope,
  })

  // On mount for a RESUMED conversation: pull persisted history, THEN try to
  // re-attach to an in-flight turn (the run keeps going server-side after a
  // disconnect — Sparky-style resume). resume() replays what we missed + streams
  // live; if nothing is in flight it self-cleans (no ghost turn), leaving just the
  // loaded history. Order matters: history first so the live turn appends after it.
  useEffect(() => {
    if (!historyResume) return
    let cancelled = false
    ;(async () => {
      await loadHistory()
      if (!cancelled) resume()
    })()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Tell the controller once the first turn lands (locks the model, binds the URL).
  useEffect(() => {
    if (chatTurns.length > 0) onStarted()
  }, [chatTurns.length, onStarted])

  // Refresh the sidebar history list on each turn-COMPLETE (isStreaming true→false)
  // — by then the runtime has committed the thread's index row, so a NEW chat shows
  // up (and a later turn's title/scope update is reflected) without a page reload.
  const wasStreamingRef = useRef(false)
  useEffect(() => {
    if (wasStreamingRef.current && !isStreaming && chatTurns.length > 0) {
      onTurnComplete?.()
    }
    wasStreamingRef.current = isStreaming
  }, [isStreaming, chatTurns.length, onTurnComplete])

  return (
    <ChatThread
      chatTurns={chatTurns}
      isStreaming={isStreaming}
      error={error}
      loadingHistory={loadingHistory}
      emptyGreeting="Ask the Data Wiki"
      emptyHint="Questions about any dataset — tables, columns, joins, metrics, known issues."
      onSend={send}
      onAnswer={answerHuman}
      pendingAsk={pendingAsk}
      onStop={stop}
      onPrepare={prepare}
      effort={conv.effort}
      efforts={efforts}
      onEffortChange={onEffortChange}
      features={conv.features}
      onFeaturesChange={onFeaturesChange}
      datasets={datasets}
      datasetScope={conv.datasetScope}
      onScopeChange={onScopeChange}
      onOpenDoc={onOpenDoc}
    />
  )
}

// Doc-peek width bounds (px). Default 36rem; min keeps the doc readable, max is
// re-clamped to the viewport at drag time so the chat column always survives.
const PEEK_WIDTH_KEY = "okf.chat.docPeekWidth"
const PEEK_MIN = 320
const PEEK_DEFAULT = 576

function loadPeekWidth() {
  try {
    const saved = Number(localStorage.getItem(PEEK_WIDTH_KEY))
    if (saved >= PEEK_MIN && saved <= 1200) return saved
  } catch {
    // private mode / storage disabled — fall through to the default
  }
  return PEEK_DEFAULT
}

export function ChatPanel({ api, auth, ctrl, datasets = [] }) {
  const {
    conv,
    resumed,
    efforts,
    historyOpen,
    setHistoryOpen,
    historyReloadKey,
    onStarted,
    onTurnComplete,
    onEffortChange,
    onFeaturesChange,
    onScopeChange,
    resumeThread,
    onThreadDeleted,
  } = ctrl

  const getToken = () => auth?.user?.access_token

  // Doc peek: the citation-opened doc reader. Open flag and target are separate
  // so closing collapses the clip while the content stays mounted (no blank
  // panel mid-animation), and reopening the same doc is instant. A conversation
  // switch clears both — another thread's doc is stale context here.
  const [peekOpen, setPeekOpen] = useState(false)
  const [peekTarget, setPeekTarget] = useState(null)
  const openDoc = useCallback((target) => {
    setPeekTarget(target)
    setPeekOpen(true)
  }, [])
  useEffect(() => {
    setPeekOpen(false)
    setPeekTarget(null)
  }, [conv.threadId])

  // Resizable width: dragged from the panel's left-edge handle, persisted as a
  // preference. While a drag is live the clip's width TRANSITION is disabled —
  // otherwise every pointermove eases over 300ms and the panel rubber-bands.
  const [peekWidth, setPeekWidth] = useState(loadPeekWidth)
  const [peekDragging, setPeekDragging] = useState(false)
  const peekWidthRef = useRef(peekWidth)
  useEffect(() => {
    peekWidthRef.current = peekWidth
  }, [peekWidth])
  const startPeekResize = useCallback((e) => {
    e.preventDefault()
    const startX = e.clientX
    const startW = peekWidthRef.current
    // Clamp against the CURRENT viewport so the chat column keeps ~half the
    // screen even on small windows.
    const max = Math.max(
      PEEK_MIN,
      Math.min(880, Math.round(window.innerWidth * 0.55))
    )
    setPeekDragging(true)
    const prevSelect = document.body.style.userSelect
    document.body.style.userSelect = "none" // no text selection mid-drag
    const move = (ev) => {
      const next = Math.min(max, Math.max(PEEK_MIN, startW + (startX - ev.clientX)))
      peekWidthRef.current = next
      setPeekWidth(next)
    }
    const up = () => {
      document.body.style.userSelect = prevSelect
      setPeekDragging(false)
      try {
        localStorage.setItem(PEEK_WIDTH_KEY, String(peekWidthRef.current))
      } catch {
        // private mode / storage full — the in-memory width still applies
      }
      window.removeEventListener("pointermove", move)
      window.removeEventListener("pointerup", up)
    }
    window.addEventListener("pointermove", move)
    window.addEventListener("pointerup", up)
  }, [])

  if (!CHAT_CONFIGURED) {
    return (
      <div className="m-auto max-w-xs p-6 text-center text-sm text-muted-foreground">
        The chat agent isn&apos;t configured for this environment
        (VITE_CHAT_RUNTIME_ARN is unset).
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 w-full">
      {/* key=threadId remounts on new-chat / model switch / resume. */}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <Conversation
          key={conv.threadId}
          conv={conv}
          getToken={getToken}
          onStarted={onStarted}
          onTurnComplete={onTurnComplete}
          historyResume={resumed}
          efforts={efforts}
          onEffortChange={onEffortChange}
          onFeaturesChange={onFeaturesChange}
          datasets={datasets}
          onScopeChange={onScopeChange}
          onOpenDoc={openDoc}
        />
      </div>

      {/* Doc peek — opened by clicking a wiki citation. Same width-animated
          clip pattern as the history drawer below, so it PUSHES the chat
          (transcript + doc readable side by side) instead of overlaying it.
          Width is user-resizable (the panel's left-edge handle) — inline style
          rather than a class since it's a live number. */}
      <div
        className={cn(
          "h-full shrink-0 overflow-hidden",
          !peekDragging &&
            "transition-[width] duration-300 ease-in-out motion-reduce:transition-none"
        )}
        style={{ width: peekOpen ? peekWidth : 0 }}
        aria-hidden={!peekOpen}
      >
        <div
          className={cn("h-full", !peekOpen && "invisible")}
          style={{ width: peekWidth }}
        >
          {peekTarget ? (
            <DocPeek
              api={api}
              target={peekTarget}
              onTarget={setPeekTarget}
              onClose={() => setPeekOpen(false)}
              onResizeStart={startPeekResize}
              resizing={peekDragging}
            />
          ) : null}
        </div>
      </div>

      {/* The history drawer stays MOUNTED and slides open/closed by animating an
          outer clip's width (0 → 19rem). The inner panel keeps its fixed w-76 so
          its content never reflows mid-transition — it's just revealed/hidden as
          the clip grows/shrinks, and the chat area reflows smoothly alongside.
          Same PanelShell chrome as the doc peek (narrower, not resizable).
          aria-hidden + invisible when closed keeps it out of the tab order. */}
      <div
        className={cn(
          "h-full shrink-0 overflow-hidden transition-[width] duration-300 ease-in-out motion-reduce:transition-none",
          historyOpen ? "w-76" : "w-0"
        )}
        aria-hidden={!historyOpen}
      >
        <div className={cn("h-full w-76", !historyOpen && "invisible")}>
          <PanelShell>
            <ChatHistory
              api={api}
              activeThreadId={conv.threadId}
              reloadKey={historyReloadKey}
              onResume={resumeThread}
              onDeleted={onThreadDeleted}
              onClose={() => setHistoryOpen(false)}
            />
          </PanelShell>
        </div>
      </div>
    </div>
  )
}
