// The little square that identifies a source: a web page's favicon, or a glyph
// for a wiki doc.
//
// Favicons come from the source origin (see lib/sources.faviconUrl) and plenty of
// sites don't serve one, so a failure is NORMAL, not an error: on error (or while
// loading) we show a MONOGRAM tile — the host's first letter over a hue derived
// from the host string, so the same site always gets the same colour and a list of
// sources stays visually distinguishable even when every icon 404s.

import { DatabaseIcon, FileTextIcon, Link2Icon } from "lucide-react"
import { useEffect, useState } from "react"

import { faviconUrl, hostOf } from "@/lib/sources"
import { cn } from "@/lib/utils"

// Stable hue per host: a cheap string hash, spread over the colour wheel. Kept at
// low chroma / mid lightness so the tiles read as UI chrome, not decoration.
function hueOf(str) {
  let h = 0
  for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) % 360
  return h
}

function Monogram({ host, size }) {
  const letter = (host || "?").charAt(0).toUpperCase()
  const hue = hueOf(host || "?")
  return (
    <span
      aria-hidden="true"
      className="inline-flex shrink-0 items-center justify-center rounded-[3px] font-semibold"
      style={{
        width: size,
        height: size,
        fontSize: Math.max(7, Math.round(size * 0.6)),
        lineHeight: 1,
        color: `oklch(0.45 0.06 ${hue})`,
        backgroundColor: `oklch(0.9 0.03 ${hue})`,
      }}
    >
      {letter}
    </span>
  )
}

// The kind glyph for a wiki-doc source, keyed off the concept id's top-level dir.
const DOC_ICONS = {
  tables: DatabaseIcon,
  datasets: DatabaseIcon,
  references: FileTextIcon,
}

export function DocIcon({ conceptId, size = 14, className }) {
  const Glyph = DOC_ICONS[String(conceptId || "").split("/")[0]] || FileTextIcon
  return (
    <Glyph
      className={cn("shrink-0 text-muted-foreground", className)}
      style={{ width: size, height: size }}
      aria-hidden="true"
    />
  )
}

export function SourceIcon({ url, size = 14, className }) {
  const src = faviconUrl(url)
  const host = hostOf(url)
  const [failed, setFailed] = useState(!src)

  // A new url means a new icon attempt — reset the failure latch, or every source
  // after the first failure in a reused component instance would show a monogram.
  useEffect(() => setFailed(!src), [src])

  if (failed) {
    return host ? (
      <Monogram host={host} size={size} />
    ) : (
      <Link2Icon
        className={cn("shrink-0 text-muted-foreground", className)}
        style={{ width: size, height: size }}
        aria-hidden="true"
      />
    )
  }
  return (
    <img
      src={src}
      alt=""
      aria-hidden="true"
      width={size}
      height={size}
      loading="lazy"
      decoding="async"
      // No referrer: fetching a site's icon shouldn't tell it which page of ours
      // the user is reading.
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
      className={cn("shrink-0 rounded-[3px] object-contain", className)}
      style={{ width: size, height: size }}
    />
  )
}
