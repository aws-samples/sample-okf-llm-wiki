// The APPLIED theme, read off <html>'s `dark` class and tracked LIVE with a
// MutationObserver (ChartFrame's pattern): useTheme() alone can't drive this —
// its value is often "system", which never changes when the applied class
// flips. Shared by the report surfaces (chat ReportPanel, the Analysis page)
// to re-theme an ALREADY-OPEN document when the user switches theme.

import { useEffect, useState } from "react"

function readResolvedTheme() {
  if (typeof document === "undefined") return "light"
  return document.documentElement.classList.contains("dark") ? "dark" : "light"
}

export function useResolvedTheme() {
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
