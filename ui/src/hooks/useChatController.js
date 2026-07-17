// Shared chat control state, lifted OUT of ChatPanel so the sidebar (rendered by
// the app shell, above ChatPanel) and the chat page drive the same conversation:
// new-chat, resume-from-history, the history drawer toggle, and the reasoning
// EFFORT. The model is FIXED to Opus 4.8 (no model choice), so there's no model
// state and effort can change freely on any conversation.

import { useCallback, useEffect, useState } from "react"

import { newThreadId } from "@/lib/chatApi"
import { loadFeatures, saveFeatures } from "@/lib/chatFeatures"
import {
  CHAT_EFFORTS,
  CHAT_MODEL,
  loadEffort,
  saveEffort,
} from "@/lib/chatModels"

function newConversation() {
  return {
    threadId: newThreadId(),
    model: CHAT_MODEL,
    effort: loadEffort(),
    features: loadFeatures(),
    // The `@`-mention dataset scope ({ data_domain, dataset }) or null (whole
    // wiki). Advisory relevance context, resolved per-run — not pinned like model.
    datasetScope: null,
  }
}

export function useChatController({ urlThreadId, onThreadChange }) {
  const [conv, setConv] = useState(() =>
    urlThreadId
      ? {
          threadId: urlThreadId,
          model: CHAT_MODEL,
          effort: loadEffort(),
          features: loadFeatures(),
          datasetScope: null,
        }
      : newConversation()
  )
  // Opened from a link/history (needs a load) vs freshly minted (starts empty).
  const [resumed, setResumed] = useState(Boolean(urlThreadId))
  // Initiated (first turn sent, or resumed) — gates the URL binding.
  const [started, setStarted] = useState(Boolean(urlThreadId))
  const [historyOpen, setHistoryOpen] = useState(false)
  // Bumped to re-fetch the history list (after a turn writes/renames a row).
  const [historyReloadKey, setHistoryReloadKey] = useState(0)

  // Bind the URL (#/chat/<threadId>) ONLY once the conversation is initiated. A
  // fresh, untouched chat has no server session yet, so it must not stamp a
  // thread id into the URL. Once `started`, keep it in sync (replace() upstream).
  useEffect(() => {
    if (!started) return
    if (onThreadChange && conv.threadId !== urlThreadId) {
      onThreadChange(conv.threadId)
    }
  }, [started, conv.threadId, urlThreadId, onThreadChange])

  // First turn landed: refresh the history list (the turn wrote the index row).
  const onStarted = useCallback(() => {
    setStarted((s) => {
      if (!s) setHistoryReloadKey((k) => k + 1)
      return true
    })
  }, [])

  const startNewChat = useCallback(() => {
    setConv(newConversation())
    setResumed(false)
    setStarted(false)
    // Drop the previous chat's id from the URL — a fresh chat isn't bound until
    // its first turn (the started-gated effect re-binds then).
    onThreadChange?.(null)
  }, [onThreadChange])

  const resumeThread = useCallback((t) => {
    setConv({
      threadId: t.thread_id,
      model: CHAT_MODEL,
      effort: t.effort || loadEffort(),
      // Features aren't pinned by the checkpoint (resolved per-run like effort),
      // so a resumed chat starts from the saved preference; the user can retoggle.
      features: loadFeatures(),
      // The history row carries the last-used scope — restore it so the resumed
      // chat stays pointed at the same dataset (still changeable via @).
      datasetScope: t.dataset_scope || null,
    })
    setResumed(true)
    setStarted(true)
    setHistoryOpen(false)
  }, [])

  const onThreadDeleted = useCallback(
    (deletedId) => {
      if (deletedId === conv.threadId) startNewChat()
    },
    [conv.threadId, startNewChat]
  )

  // Effort is changeable at any time (resolved per-run by the runtime; not pinned
  // by the checkpoint like the model would be).
  const onEffortChange = useCallback((effort) => {
    saveEffort(effort)
    setConv((c) => ({ ...c, effort }))
  }, [])

  // Optional features (e.g. SQL) toggle at any time too — resolved per-run, and
  // persisted as the default for the next new chat.
  const onFeaturesChange = useCallback((features) => {
    const next = Array.isArray(features) ? features : []
    saveFeatures(next)
    setConv((c) => ({ ...c, features: next }))
  }, [])

  // The `@`-mention dataset scope — changeable any time (advisory per-run context,
  // not pinned). null clears it (whole wiki). Not persisted: scope is a per-chat
  // intent, not a global preference like effort.
  const onScopeChange = useCallback((scope) => {
    setConv((c) => ({ ...c, datasetScope: scope || null }))
  }, [])

  return {
    conv,
    resumed,
    started,
    historyOpen,
    setHistoryOpen,
    historyReloadKey,
    efforts: CHAT_EFFORTS,
    onStarted,
    startNewChat,
    resumeThread,
    onThreadDeleted,
    onEffortChange,
    onFeaturesChange,
    onScopeChange,
  }
}
