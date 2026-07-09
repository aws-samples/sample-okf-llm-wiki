// Tiny hash-based router. Hash routing (not pathname) is deliberate: the app is
// an OAuth SPA served from CloudFront with an index/callback MPA split, so a
// hash keeps deep links working on refresh with no server rewrite and never
// collides with the `?code=&state=` OAuth params. The location becomes:
//
//   #/<section>[/<domain>/<dataset>[/<concept…>]]
//   e.g. #/browse/sport/formula_1/tables/races
//
// domain and dataset are always single segments (validated server-side); the
// concept id (which may contain "/") is everything after them.

import { useCallback, useEffect, useState } from "react"

export function parseHash() {
  const raw = window.location.hash.replace(/^#\/?/, "")
  const parts = raw
    .split("/")
    .filter(Boolean)
    .map((s) => {
      try {
        return decodeURIComponent(s)
      } catch {
        return s
      }
    })
  const [section, domain, dataset, ...conceptParts] = parts
  return {
    section: section || null,
    selectionKey: domain && dataset ? `${domain}/${dataset}` : null,
    concept: conceptParts.length ? conceptParts.join("/") : null,
  }
}

export function buildHash({ section, selectionKey, concept }) {
  const segs = []
  if (section) segs.push(section)
  if (selectionKey) segs.push(...selectionKey.split("/"))
  if (concept) segs.push(...concept.split("/"))
  return "#/" + segs.map(encodeURIComponent).join("/")
}

// Returns the parsed route plus `push` (adds a history entry — use for user
// navigation, so Back returns to the prior view) and `replace` (rewrites the
// current entry without a new one — use for normalization/defaults).
export function useRouter() {
  const [route, setRoute] = useState(parseHash)

  useEffect(() => {
    const onChange = () => setRoute(parseHash())
    // hashchange covers explicit hash sets; popstate covers back/forward.
    window.addEventListener("hashchange", onChange)
    window.addEventListener("popstate", onChange)
    return () => {
      window.removeEventListener("hashchange", onChange)
      window.removeEventListener("popstate", onChange)
    }
  }, [])

  const push = useCallback((next) => {
    const hash = buildHash(next)
    if (hash !== window.location.hash) {
      window.location.hash = hash // fires hashchange -> setRoute
    }
  }, [])

  const replace = useCallback((next) => {
    const hash = buildHash(next)
    const url = window.location.pathname + window.location.search + hash
    window.history.replaceState(null, "", url)
    // replaceState doesn't fire hashchange/popstate, so sync manually — but only
    // when the parsed route actually changed, to avoid a redundant re-render.
    const parsed = parseHash()
    setRoute((prev) =>
      prev.section === parsed.section &&
      prev.selectionKey === parsed.selectionKey &&
      prev.concept === parsed.concept
        ? prev
        : parsed
    )
  }, [])

  return { route, push, replace }
}
