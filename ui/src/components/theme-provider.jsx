/* eslint-disable react-refresh/only-export-components */
import * as React from "react"

// Minimal theme provider (plain JS). Applies light/dark to <html> and persists
// the choice; defaults to the system preference.
const ThemeProviderContext = React.createContext(undefined)

function getSystemTheme() {
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light"
}

export function ThemeProvider({
  children,
  defaultTheme = "system",
  storageKey = "theme",
  ...props
}) {
  const [theme, setThemeState] = React.useState(
    () => localStorage.getItem(storageKey) || defaultTheme
  )

  const setTheme = React.useCallback(
    (next) => {
      localStorage.setItem(storageKey, next)
      setThemeState(next)
    },
    [storageKey]
  )

  React.useEffect(() => {
    const root = document.documentElement
    const apply = (resolved) => {
      root.classList.remove("light", "dark")
      root.classList.add(resolved)
      // The pre-paint script in index.html/callback.html put an inline HEX
      // background + color-scheme on <html> (so the first frame paints in-theme
      // before the stylesheet exists). Once React owns the theme, swap the hex
      // for the live token so runtime toggles recolor it (a stale inline hex
      // shows in overscroll/rubber-band areas), and keep color-scheme in sync
      // for native scrollbars/controls.
      root.style.colorScheme = resolved
      root.style.backgroundColor = "var(--background)"
    }
    if (theme !== "system") {
      apply(theme)
      return
    }
    apply(getSystemTheme())
    // In "system", FOLLOW the OS/browser preference live — without this
    // listener a mid-session OS theme flip only lands on the next reload.
    const mq = window.matchMedia("(prefers-color-scheme: dark)")
    const onChange = (e) => apply(e.matches ? "dark" : "light")
    mq.addEventListener("change", onChange)
    return () => mq.removeEventListener("change", onChange)
  }, [theme])

  const value = React.useMemo(() => ({ theme, setTheme }), [theme, setTheme])

  return (
    <ThemeProviderContext.Provider {...props} value={value}>
      {children}
    </ThemeProviderContext.Provider>
  )
}

export const useTheme = () => {
  const context = React.useContext(ThemeProviderContext)
  if (context === undefined) {
    throw new Error("useTheme must be used within a ThemeProvider")
  }
  return context
}
