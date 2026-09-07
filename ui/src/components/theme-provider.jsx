/* eslint-disable react-refresh/only-export-components */
import * as React from "react"

// Minimal theme provider (plain JS). Applies light/dark to <html> and persists
// the choice; defaults to the system preference.
const COLOR_SCHEME_QUERY = "(prefers-color-scheme: dark)"
const THEME_VALUES = ["dark", "light", "system"]
const ThemeProviderContext = React.createContext(undefined)

function isTheme(value) {
  return value !== null && THEME_VALUES.includes(value)
}

function getSystemTheme() {
  return window.matchMedia(COLOR_SCHEME_QUERY).matches ? "dark" : "light"
}

export function ThemeProvider({
  children,
  defaultTheme = "system",
  storageKey = "theme",
  ...props
}) {
  const [theme, setThemeState] = React.useState(() => {
    const storedTheme = localStorage.getItem(storageKey)
    return isTheme(storedTheme) ? storedTheme : defaultTheme
  })
  const [systemTheme, setSystemTheme] = React.useState(getSystemTheme)
  const resolvedTheme = theme === "system" ? systemTheme : theme

  const setTheme = React.useCallback(
    (next) => {
      if (!isTheme(next)) return
      localStorage.setItem(storageKey, next)
      setThemeState(next)
    },
    [storageKey]
  )

  const applyTheme = React.useCallback((resolved) => {
    const root = document.documentElement
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
  }, [])

  React.useEffect(() => {
    applyTheme(resolvedTheme)
  }, [resolvedTheme, applyTheme])

  React.useEffect(() => {
    const mediaQuery = window.matchMedia(COLOR_SCHEME_QUERY)
    const onChange = (event) => {
      setSystemTheme(event.matches ? "dark" : "light")
    }
    mediaQuery.addEventListener("change", onChange)
    return () => mediaQuery.removeEventListener("change", onChange)
  }, [])

  React.useEffect(() => {
    const onStorage = (event) => {
      if (event.storageArea !== localStorage || event.key !== storageKey) return
      setThemeState(isTheme(event.newValue) ? event.newValue : defaultTheme)
    }
    window.addEventListener("storage", onStorage)
    return () => window.removeEventListener("storage", onStorage)
  }, [defaultTheme, storageKey])

  const value = React.useMemo(
    () => ({ theme, resolvedTheme, setTheme }),
    [theme, resolvedTheme, setTheme]
  )

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
