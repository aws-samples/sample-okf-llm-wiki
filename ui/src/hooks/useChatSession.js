// The chat session store — Sparky's ChatContext/useChatSessionFunctions, boiled
// down to ONE active conversation (the wiki chat has no multi-session tabs,
// threads, canvas, or attachments to juggle). Owns:
//   - chatTurns: [{ id, userMessage, aiMessage:[…raw chunks…] }]
//   - isStreaming, error
//   - send(prompt), stop(), loadHistory(), reset(threadId)
//
// A turn's `aiMessage` is the raw typed-chunk array the SSE reader appends to;
// buildMessageBlocks (in the renderer) turns it into blocks. We keep the raw
// events (not pre-built blocks) so the reasoning/tool timeline assembles exactly
// like Sparky's and a mid-stream re-render is cheap.

import { useCallback, useEffect, useRef, useState } from "react"

import {
  answerHumanAPI,
  deleteHistoryAPI,
  fetchHistoryAPI,
  prepareAPI,
  resumeAPI,
  sendMessageAPI,
  stopAPI,
} from "@/lib/chatApi"
import { consumeSSE } from "@/lib/chatStream"

function turnId() {
  return `turn_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`
}

export function useChatSession({
  threadId,
  getToken,
  model,
  effort,
  features,
  datasetScope,
}) {
  const [chatTurns, setChatTurns] = useState([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [error, setError] = useState(null)
  // A paused ask_human interrupt awaiting the user: the questions to render as a
  // QA form in the composer. null when the agent isn't asking. Cleared on submit
  // (answerHuman), stop, or conversation switch.
  const [pendingAsk, setPendingAsk] = useState(null)
  // True while a resumed conversation's history is being fetched (drives the
  // skeleton placeholders in the transcript).
  const [loadingHistory, setLoadingHistory] = useState(false)

  const abortRef = useRef(null)
  // A pending EXPLICIT stop (the stopAPI round-trip). The server's stop handler
  // awaits the run's task teardown before it responds, so once this promise
  // resolves the runtime-side run is fully gone and a new turn can start cleanly.
  // send() awaits it first so a fast stop→re-send doesn't race live_streams.start
  // into re-attaching to the dying run (which would drop the new prompt).
  const stopPromiseRef = useRef(null)
  // Latest args in a ref so the stable send() closure always reads current values.
  const cfgRef = useRef({ threadId, getToken, model, effort, features, datasetScope })
  cfgRef.current = { threadId, getToken, model, effort, features, datasetScope }

  // --- smooth text reveal (typewriter pump) ----------------------------------
  // The network delivers tokens in BURSTS (one packet ≈ dozens of tokens). We
  // queue incoming chunks and reveal characters at a STEADY, SMOOTHED rate every
  // frame — decoupled from arrival — so the text types out evenly instead of
  // lurching. The rate is a low-pass-filtered function of the backlog (so it
  // eases up/down instead of snapping between fast and slow — that snapping is
  // what caused the burst-then-pause feel), with a fractional accumulator so
  // sub-1-char/frame speeds work (e.g. 1 char every other frame). Non-text chunks
  // (think / tool / end / error) pass through in order, promptly.
  const queueRef = useRef([]) // chunks awaiting reveal (FIFO)
  const rafRef = useRef(null)
  const netDoneRef = useRef(false) // network stream finished (still may be revealing)
  const rateRef = useRef(0) // smoothed chars/frame
  const accRef = useRef(0) // fractional char accumulator

  // Tuning: calm typewriter. DRAIN_FRAMES = how many frames to nominally spread
  // the current backlog over (bigger = slower / more cushion = fewer pauses).
  // The rate is clamped so it never trickles to a stop mid-answer (MIN) nor
  // bursts (MAX), and eased toward its target by SMOOTH (smaller = smoother).
  const DRAIN_FRAMES = 60 // ~1s worth of cushion
  const REVEAL_MIN = 0.6 // ≈36 chars/s floor while content remains
  const REVEAL_MAX = 6 // ≈360 chars/s ceiling
  const SMOOTH = 0.08 // rate low-pass factor

  const pendingChars = () => {
    let n = 0
    for (const c of queueRef.current) {
      if (c.type === "text" && typeof c.content === "string") n += c.content.length
    }
    return n
  }

  const finishIfDrained = useCallback(() => {
    if (queueRef.current.length === 0 && netDoneRef.current) {
      rateRef.current = 0
      accRef.current = 0
      setIsStreaming(false)
    }
  }, [])

  const pump = useCallback(() => {
    rafRef.current = null
    const q = queueRef.current
    if (q.length === 0) {
      finishIfDrained()
      return
    }

    const pending = pendingChars()
    // Ease the reveal rate toward a target derived from the backlog. Only floor
    // to MIN while there's actually text pending, so it keeps trickling steadily
    // rather than stalling; near the very end, MIN drains the last few chars.
    const target = pending > 0 ? Math.min(REVEAL_MAX, pending / DRAIN_FRAMES) : 0
    rateRef.current += (target - rateRef.current) * SMOOTH
    let effRate = rateRef.current
    if (pending > 0 && effRate < REVEAL_MIN) effRate = REVEAL_MIN

    // Accumulate fractional chars; reveal the whole-number part this frame.
    accRef.current += effRate
    let budget = Math.floor(accRef.current)
    accRef.current -= budget

    const emit = []
    while (q.length > 0) {
      const head = q[0]
      if (head.type === "text" && typeof head.content === "string") {
        if (budget <= 0) break
        if (head.content.length <= budget) {
          emit.push(head)
          budget -= head.content.length
          q.shift()
        } else {
          // Reveal a slice; leave the remainder at the queue head for next frame.
          emit.push({ ...head, content: head.content.slice(0, budget) })
          q[0] = { ...head, content: head.content.slice(budget) }
          budget = 0
          break
        }
      } else {
        // Non-text (think / tool / end / error): pass through in order, free.
        emit.push(head)
        q.shift()
      }
    }

    if (emit.length > 0) {
      setChatTurns((turns) => {
        if (turns.length === 0) return turns
        const next = turns.slice()
        const last = next[next.length - 1]
        next[next.length - 1] = {
          ...last,
          aiMessage: [...last.aiMessage, ...emit],
        }
        return next
      })
    }

    // Keep pumping while anything remains to reveal; otherwise settle.
    if (queueRef.current.length > 0) {
      rafRef.current = requestAnimationFrame(pump)
    } else {
      finishIfDrained()
    }
  }, [finishIfDrained])

  const ensurePump = useCallback(() => {
    if (rafRef.current == null) rafRef.current = requestAnimationFrame(pump)
  }, [pump])

  const cancelPump = useCallback(() => {
    if (rafRef.current != null) {
      cancelAnimationFrame(rafRef.current)
      rafRef.current = null
    }
  }, [])

  // Drain the whole queue to state at once (manual stop / teardown) — no metering.
  const drainNow = useCallback(() => {
    cancelPump()
    const q = queueRef.current
    if (q.length === 0) return
    queueRef.current = []
    setChatTurns((turns) => {
      if (turns.length === 0) return turns
      const next = turns.slice()
      const last = next[next.length - 1]
      next[next.length - 1] = { ...last, aiMessage: [...last.aiMessage, ...q] }
      return next
    })
  }, [cancelPump])

  const appendChunk = useCallback(
    (chunk) => {
      queueRef.current.push(chunk)
      ensurePump()
    },
    [ensurePump]
  )

  // EXPLICIT stop: tell the runtime to cancel the in-flight run (the ONLY thing
  // that cancels now — a dropped connection keeps the run alive for resume). Then
  // settle the local UI. We do NOT abort the fetch first: we want to keep reading
  // so the server's synthetic "cancelled" tool chunk + cancelled end marker land
  // in the transcript; the server ending the stream closes the connection for us.
  const stop = useCallback(() => {
    const cfg = cfgRef.current
    // Keep the stop promise so a follow-up send() can await the runtime-side run
    // fully tearing down before starting a new turn (else start() re-attaches to
    // the dying run and the new prompt is dropped). Cleared once it resolves.
    const p = stopAPI({ threadId: cfg.threadId, getToken: cfg.getToken }).finally(
      () => {
        if (stopPromiseRef.current === p) stopPromiseRef.current = null
      }
    )
    stopPromiseRef.current = p
    netDoneRef.current = true
    drainNow() // reveal whatever's already queued immediately
    setIsStreaming(false)
  }, [drainNow])

  // Shared SSE consumption for both send and resume: pipe each chunk through the
  // typewriter queue; surface errors; always settle the pump when the network
  // ends. `controller` gates the read; on abort (conversation switch) we bail
  // quietly without closing the open turn (the run continues server-side).
  // `onNoActive` fires if the server reports no in-flight run (resume fallback).
  const consumeStream = useCallback(
    async (res, controller, onNoActive) => {
      try {
        await consumeSSE(
          res,
          (chunk) => {
            if (chunk.type === "error") {
              setError(chunk.message || "the agent hit an error")
              return
            }
            if (chunk.type === "ask_human") {
              // The agent paused to ask the user. Surface the questions for the
              // composer's QA form; the run has ended server-side (graph paused at
              // the checkpoint) and will resume on answerHuman(). Keep the whole
              // chunk (questions carry per-question interrupt_id for the resume map).
              setPendingAsk({ questions: chunk.questions || [] })
              return
            }
            if (chunk.type === "no_active_stream") {
              onNoActive?.()
              return
            }
            if (chunk.type === "user_message") {
              // resume leads with the in-flight turn's question — stamp it onto the
              // current (placeholder) turn so it renders as a normal turn.
              setChatTurns((turns) => {
                if (turns.length === 0) return turns
                const next = turns.slice()
                const last = next[next.length - 1]
                next[next.length - 1] = { ...last, userMessage: chunk.content }
                return next
              })
              return
            }
            appendChunk(chunk)
          },
          { signal: controller.signal }
        )
      } catch (err) {
        if (err.name !== "AbortError") {
          setError(err.message || "the chat stream failed")
          appendChunk({ end: true }) // don't hang the spinner on a hard error
        }
      } finally {
        if (abortRef.current === controller) abortRef.current = null
        netDoneRef.current = true
        ensurePump()
        finishIfDrained()
      }
    },
    [appendChunk, ensurePump, finishIfDrained]
  )

  const send = useCallback(
    async (prompt) => {
      const text = (prompt || "").trim()
      const cfg = cfgRef.current
      if (!text || isStreaming) return

      // If the user just hit Stop, wait for the runtime-side run to finish tearing
      // down before starting a new turn — otherwise the server's live-stream
      // registry still has the (dying) run for this thread and start() would
      // re-attach to it instead of running our new prompt (a silently dropped send).
      if (stopPromiseRef.current) {
        try {
          await stopPromiseRef.current
        } catch {
          // best-effort; the stop still settled the local UI
        }
      }

      setError(null)
      netDoneRef.current = false
      rateRef.current = 0
      accRef.current = 0
      // Open a new turn (user message + empty AI response) and mark streaming.
      setChatTurns((turns) => [
        ...turns,
        { id: turnId(), userMessage: text, aiMessage: [] },
      ])
      setIsStreaming(true)

      const controller = new AbortController()
      abortRef.current = controller
      try {
        const res = await sendMessageAPI({
          threadId: cfg.threadId,
          getToken: cfg.getToken,
          prompt: text,
          model: cfg.model,
          effort: cfg.effort,
          features: cfg.features,
          datasetScope: cfg.datasetScope,
          signal: controller.signal,
        })
        await consumeStream(res, controller)
      } catch (err) {
        if (err.name !== "AbortError") {
          setError(err.message || "failed to send message")
          appendChunk({ end: true })
          if (abortRef.current === controller) abortRef.current = null
          netDoneRef.current = true
          ensurePump()
          finishIfDrained()
        }
      }
    },
    [appendChunk, consumeStream, ensurePump, finishIfDrained, isStreaming]
  )

  // Submit the user's answers to a paused ask_human interrupt and resume the run.
  // Continues the SAME turn (no new user bubble): the assistant's answer picks up
  // where it paused. `answers` is [{ id, answer }] (answer: string | string[]).
  const answerHuman = useCallback(
    async (answers) => {
      const cfg = cfgRef.current
      if (isStreaming) return
      setPendingAsk(null)
      setError(null)
      netDoneRef.current = false
      rateRef.current = 0
      accRef.current = 0
      setIsStreaming(true)

      const controller = new AbortController()
      abortRef.current = controller
      try {
        const res = await answerHumanAPI({
          threadId: cfg.threadId,
          getToken: cfg.getToken,
          answers,
          // Same (model, effort, features, scope) as the conversation, so the server
          // rebuilds the SAME graph the interrupt paused on (model is pinned + its
          // checkpoint isn't portable across models).
          model: cfg.model,
          effort: cfg.effort,
          features: cfg.features,
          datasetScope: cfg.datasetScope,
          signal: controller.signal,
        })
        await consumeStream(res, controller)
      } catch (err) {
        if (err.name !== "AbortError") {
          setError(err.message || "failed to submit answers")
          appendChunk({ end: true })
          if (abortRef.current === controller) abortRef.current = null
          netDoneRef.current = true
          ensurePump()
          finishIfDrained()
        }
      }
    },
    [appendChunk, consumeStream, ensurePump, finishIfDrained, isStreaming]
  )

  // Re-attach to a turn that's still streaming server-side (returning to a thread
  // whose answer is in flight). LAZY: it does NOT open a turn or flip streaming up
  // front — a plain refresh of a thread that has NOTHING in flight must look
  // exactly like a normal load (no blank "auto-sent" turn, no phantom spinner).
  // It subscribes silently; only when the FIRST real chunk arrives do we
  // materialize the turn + streaming state. If the server reports no_active_stream,
  // nothing was ever created. Returns true only if a live stream was consumed.
  const resume = useCallback(async () => {
    const cfg = cfgRef.current
    if (isStreaming) return false
    const controller = new AbortController()
    let opened = false // did a real chunk arrive → we created the turn?

    // Materialize the in-flight turn on the first real chunk (not before).
    const ensureTurnOpen = () => {
      if (opened) return
      opened = true
      netDoneRef.current = false
      rateRef.current = 0
      accRef.current = 0
      abortRef.current = controller
      setError(null)
      setChatTurns((turns) => [
        ...turns,
        { id: turnId(), userMessage: "", aiMessage: [] },
      ])
      setIsStreaming(true)
    }

    try {
      const res = await resumeAPI({
        threadId: cfg.threadId,
        getToken: cfg.getToken,
        signal: controller.signal,
      })
      await consumeSSE(
        res,
        (chunk) => {
          if (chunk.type === "no_active_stream") return // nothing in flight — no-op
          if (chunk.type === "error") {
            if (opened) setError(chunk.message || "the agent hit an error")
            return
          }
          if (chunk.type === "ask_human") {
            ensureTurnOpen()
            setPendingAsk({ questions: chunk.questions || [] })
            return
          }
          if (chunk.end && !opened) return // the settling end for an inactive resume
          ensureTurnOpen()
          if (chunk.type === "user_message") {
            setChatTurns((turns) => {
              if (turns.length === 0) return turns
              const next = turns.slice()
              const last = next[next.length - 1]
              next[next.length - 1] = { ...last, userMessage: chunk.content }
              return next
            })
            return
          }
          appendChunk(chunk)
        },
        { signal: controller.signal }
      )
    } catch (err) {
      if (err.name !== "AbortError" && opened) {
        appendChunk({ end: true })
      }
    } finally {
      if (opened) {
        if (abortRef.current === controller) abortRef.current = null
        netDoneRef.current = true
        ensurePump()
        finishIfDrained()
      }
    }
    return opened
  }, [appendChunk, ensurePump, finishIfDrained, isStreaming])

  // Load a past conversation's history into chatTurns (on resume / deep link).
  const loadHistory = useCallback(async () => {
    const cfg = cfgRef.current
    setLoadingHistory(true)
    try {
      const { history, pendingAsk: pending } = await fetchHistoryAPI({
        threadId: cfg.threadId,
        getToken: cfg.getToken,
      })
      if (history.length > 0) setChatTurns(history)
      // If the conversation is paused at an ask_human interrupt (durable in the
      // checkpoint), restore the QA form so a page reload can still answer it.
      if (pending && Array.isArray(pending.questions) && pending.questions.length) {
        setPendingAsk(pending)
      }
    } catch {
      // A missing/unreadable history just means an empty conversation — fine.
    } finally {
      setLoadingHistory(false)
    }
  }, [])

  // Purge the runtime-side checkpoints for this conversation.
  const clearRuntimeHistory = useCallback(async () => {
    const cfg = cfgRef.current
    try {
      await deleteHistoryAPI({ threadId: cfg.threadId, getToken: cfg.getToken })
    } catch {
      // best-effort; the Control API also drops the index row
    }
  }, [])

  // Keep-warm: reset the runtime's idle timer with no LLM call. Safe now that
  // it's a SEPARATE POST (server emits only {end}) — it can't touch the visible
  // conversation the way the old AG-UI keep-warm did (that re-ran the bound agent
  // and duplicated the answer). Fire-and-forget; skipped while streaming.
  const prepare = useCallback(() => {
    if (abortRef.current) return // a turn is in flight — no need to warm
    const cfg = cfgRef.current
    prepareAPI({ threadId: cfg.threadId, getToken: cfg.getToken })
  }, [])

  // Reset local state when the conversation changes (new chat / resume / model
  // switch remounts with a new threadId). Abort any in-flight stream + drop the
  // reveal queue/pump first.
  useEffect(() => {
    abortRef.current?.abort()
    abortRef.current = null
    stopPromiseRef.current = null
    cancelPump()
    queueRef.current = []
    netDoneRef.current = false
    rateRef.current = 0
    accRef.current = 0
    setChatTurns([])
    setError(null)
    setIsStreaming(false)
    setLoadingHistory(false)
    setPendingAsk(null)
  }, [threadId, cancelPump])

  // Cancel any pending frame on unmount so the pump never fires post-teardown.
  useEffect(() => () => cancelPump(), [cancelPump])

  return {
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
    clearRuntimeHistory,
    setError,
  }
}
