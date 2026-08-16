// Drag-to-resize state for a right-hand side panel — the chat doc-peek
// behavior, shared so every panel resizes identically: drag the panel's
// left-edge grip, width clamped to [min, min(hardMax, 55% of viewport)], the
// preference persisted per storage key.
//
// Two details that look incidental but aren't:
//  - the max is re-clamped against the CURRENT viewport at drag START, so the
//    content column always survives on a small window (a width persisted on a
//    wide monitor can't strand the page on a laptop);
//  - `dragging` is exposed so the caller can DISABLE its width transition mid
//    drag — otherwise every pointermove eases over 300ms and the panel
//    rubber-bands behind the cursor.

import { useCallback, useEffect, useRef, useState } from "react"

export function usePanelWidth({
  storageKey,
  min = 320,
  defaultWidth = 576,
  hardMax = 880,
}) {
  const load = useCallback(() => {
    try {
      const saved = Number(localStorage.getItem(storageKey))
      if (saved >= min && saved <= 1200) return saved
    } catch {
      // private mode / storage disabled — fall through to the default
    }
    return defaultWidth
  }, [storageKey, min, defaultWidth])

  const [width, setWidth] = useState(load)
  const [dragging, setDragging] = useState(false)
  const widthRef = useRef(width)
  useEffect(() => {
    widthRef.current = width
  }, [width])

  // The active drag's cleanup, kept on a ref so BOTH end-of-drag paths and the
  // unmount effect below can run it. A drag can end without a pointerup —
  // pointercancel (touch scroll takeover, palm rejection, pen) or the
  // component unmounting mid-drag — and skipping cleanup used to leave the
  // move listener bound forever and text selection disabled app-wide.
  const cleanupRef = useRef(null)
  useEffect(() => () => cleanupRef.current?.(), [])

  const startResize = useCallback(
    (e) => {
      e.preventDefault()
      cleanupRef.current?.() // a second pointer mid-drag restarts cleanly
      const startX = e.clientX
      const startW = widthRef.current
      const max = Math.max(
        min,
        Math.min(hardMax, Math.round(window.innerWidth * 0.55))
      )
      setDragging(true)
      const prevSelect = document.body.style.userSelect
      document.body.style.userSelect = "none" // no text selection mid-drag
      const move = (ev) => {
        const next = Math.min(max, Math.max(min, startW + (startX - ev.clientX)))
        widthRef.current = next
        setWidth(next)
      }
      const finish = () => {
        cleanupRef.current = null
        document.body.style.userSelect = prevSelect
        setDragging(false)
        try {
          localStorage.setItem(storageKey, String(widthRef.current))
        } catch {
          // private mode / storage full — the in-memory width still applies
        }
        window.removeEventListener("pointermove", move)
        window.removeEventListener("pointerup", finish)
        window.removeEventListener("pointercancel", finish)
      }
      cleanupRef.current = finish
      window.addEventListener("pointermove", move)
      window.addEventListener("pointerup", finish)
      window.addEventListener("pointercancel", finish)
    },
    [storageKey, min, hardMax]
  )

  return { width, dragging, startResize }
}
