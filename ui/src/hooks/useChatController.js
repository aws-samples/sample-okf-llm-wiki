// Shared chat control state, lifted OUT of ChatPanel so the sidebar (rendered by
// the app shell, above ChatPanel) and the chat page drive the same conversation:
// new-chat, resume-from-history, the history drawer toggle, and the run settings
// (model, effort, features, dataset scope).
//
// The MODEL is free until the conversation starts. After that only its FAMILY
// (Anthropic vs GPT) can be picked — switching within a family keeps the thread
// (no remount), crossing providers needs a new chat (the checkpoint's reasoning
// blocks are provider-specific; the runtime refuses it too).

import { useCallback, useEffect, useState } from "react"

import { newThreadId } from "@/lib/chatApi"
import {
  loadFeatures,
  sanitizeFeatures,
  saveFeatures,
} from "@/lib/chatFeatures"
import {
  clampEffort,
  effortsFor,
  groupedChatModels,
  loadEffort,
  loadModel,
  modelFamily,
  resolveModel,
  saveEffort,
  saveModel,
} from "@/lib/chatModels"
import { clearLastThread, loadLastThread, saveLastThread } from "@/lib/lastChat"

function newConversation(threadId = newThreadId()) {
  const model = loadModel()
  return {
    threadId,
    model,
    effort: loadEffort(model),
    features: loadFeatures(),
    // The `@`-mention dataset scope ({ data_domain, dataset }) or null (whole
    // wiki). Advisory relevance context, resolved per-run.
    datasetScope: null,
  }
}

export function useChatController({
  urlThreadId,
  onThreadChange,
  active = true,
  userSub,
}) {
  // The thread to open on FIRST mount: an explicit #/chat/<id> deep link wins;
  // otherwise fall back to this user's last conversation (localStorage) so
  // returning to Chat after a reload — even one that landed on another section —
  // reopens where they left off. Computed once (initializer), so a later
  // localStorage change doesn't yank the live conversation. A reopened thread's
  // model comes back with its history (onModelRestore).
  const [initialThreadId] = useState(
    () => urlThreadId || loadLastThread(userSub)
  )
  const [conv, setConv] = useState(() =>
    newConversation(initialThreadId || undefined)
  )
  // Opened from a link/history/last-session (needs a load) vs freshly minted.
  const [resumed, setResumed] = useState(Boolean(initialThreadId))
  // Initiated (first turn sent, or resumed) — gates the URL binding and locks
  // the model's family.
  const [started, setStarted] = useState(Boolean(initialThreadId))
  const [historyOpen, setHistoryOpen] = useState(false)
  // Bumped to re-fetch the history list (after a turn writes/renames a row).
  const [historyReloadKey, setHistoryReloadKey] = useState(0)

  // Bind the URL (#/chat/<threadId>) ONLY once the conversation is initiated AND
  // chat is the active section. A fresh, untouched chat has no server session
  // yet, so it must not stamp a thread id into the URL. The `active` guard is
  // what makes chat→browse→chat work: when you leave chat, `urlThreadId` goes
  // null but the in-memory conversation persists — without the guard this effect
  // would fire onThreadChange and shove #/chat/<id> back into the URL, bouncing
  // you off the section you navigated to. While away, the thread lives in memory
  // (and localStorage); on return, active flips true and re-binds the URL.
  useEffect(() => {
    if (!started || !active) return
    if (onThreadChange && conv.threadId !== urlThreadId) {
      onThreadChange(conv.threadId)
    }
  }, [started, active, conv.threadId, urlThreadId, onThreadChange])

  // Persist the started conversation as this user's "last chat" so returning to
  // Chat (even after a full page reload that landed on another section, where
  // the URL carries no thread) reopens it. Only once started — an untouched new
  // chat has no server session worth remembering.
  useEffect(() => {
    if (started && conv.threadId) saveLastThread(userSub, conv.threadId)
  }, [started, conv.threadId, userSub])

  // First turn landed: lock the conversation as started (binds the URL, locks the
  // model's family). We do NOT refresh the history list here — at turn-OPEN the
  // server's index-row write (touch_thread, best-effort, async) may not have
  // committed yet, so a fetch now can miss the new row. The refresh happens on
  // turn COMPLETE (onTurnComplete), by which point the row is written.
  const onStarted = useCallback(() => {
    setStarted(true)
    // Also mark it resumable: once a turn has landed the conversation is
    // persisted server-side, so if the chat page later UNMOUNTS (navigating to
    // another section) and remounts on return, it should reload history rather
    // than show a blank transcript. resumed is only read by the mount effect
    // (empty deps), so flipping it now doesn't disturb the live, already-mounted
    // conversation — it only governs the next mount.
    setResumed(true)
  }, [])

  // A turn finished streaming: the index row is committed, so refresh the sidebar
  // list. This is what makes a NEW conversation appear in history without a full
  // page reload (and picks up an auto-generated/renamed title on later turns).
  const onTurnComplete = useCallback(() => {
    setHistoryReloadKey((k) => k + 1)
  }, [])

  const startNewChat = useCallback(() => {
    setConv(newConversation())
    setResumed(false)
    setStarted(false)
    // Forget the persisted last chat too — a fresh, unsent chat has no server
    // session, so a reload now should land on a blank chat, not reopen the one
    // we just left. The persist effect re-saves once this chat's first turn lands.
    clearLastThread(userSub)
    // Drop the previous chat's id from the URL — a fresh chat isn't bound until
    // its first turn (the started-gated effect re-binds then).
    onThreadChange?.(null)
  }, [onThreadChange, userSub])

  const resumeThread = useCallback((t) => {
    // The thread row's last-used model; a model the catalog dropped falls back
    // to the default of the SAME family.
    const model = resolveModel(t.model)
    setConv({
      threadId: t.thread_id,
      model,
      effort: clampEffort(model, t.effort || loadEffort(model)),
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

  // The family a started conversation is held to (null = any model).
  const lockedFamily = started ? modelFamily(conv.model) : null

  // Pick a model: any before the first turn, the conversation's family after.
  // Effort clamps to what the new model offers. Persisted as the next new chat's
  // default.
  const onModelChange = useCallback(
    (model) => {
      const next = resolveModel(model)
      if (lockedFamily && modelFamily(next) !== lockedFamily) return
      saveModel(next)
      setConv((c) => ({
        ...c,
        model: next,
        effort: clampEffort(next, c.effort),
      }))
    },
    [lockedFamily]
  )

  // The server's pinned model for a reopened thread (its history read) — the
  // source of truth for the family lock. Not persisted as a preference.
  const onModelRestore = useCallback((model) => {
    const next = resolveModel(model)
    setConv((c) =>
      c.model === next
        ? c
        : { ...c, model: next, effort: clampEffort(next, c.effort) }
    )
  }, [])

  // Effort is changeable at any time (resolved per-run by the runtime).
  const onEffortChange = useCallback((effort) => {
    saveEffort(effort)
    setConv((c) => ({ ...c, effort }))
  }, [])

  // SQL + Guardrails toggle at any time too — resolved per-run, and persisted as
  // the default for the next new chat.
  const onFeaturesChange = useCallback((features) => {
    const next = sanitizeFeatures(Array.isArray(features) ? features : [])
    saveFeatures(next)
    setConv((c) => ({ ...c, features: next }))
  }, [])

  // The `@`-mention dataset scope — changeable any time (advisory per-run context).
  // null clears it (whole wiki). Not persisted: scope is a per-chat intent, not a
  // global preference like effort.
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
    efforts: effortsFor(conv.model),
    modelGroups: groupedChatModels(lockedFamily),
    lockedFamily,
    onStarted,
    onTurnComplete,
    startNewChat,
    resumeThread,
    onThreadDeleted,
    onModelChange,
    onModelRestore,
    onEffortChange,
    onFeaturesChange,
    onScopeChange,
  }
}
