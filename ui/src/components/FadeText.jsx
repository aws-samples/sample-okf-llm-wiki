// One line of text that fades out at its end when it does not fit, instead of an
// ellipsis. The fade is applied only while the text overflows: on a short,
// content-sized label it would dim the last letters.

import { useLayoutEffect, useRef, useState } from "react"

import { cn } from "@/lib/utils"

export function FadeText({ children, className, title }) {
  const ref = useRef(null)
  const [overflowing, setOverflowing] = useState(false)

  // The observer reports once when it starts, then on every resize.
  useLayoutEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === "undefined") return undefined
    const observer = new ResizeObserver(() =>
      setOverflowing(el.scrollWidth > el.clientWidth + 1)
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [children])

  return (
    <span
      ref={ref}
      title={overflowing ? title : undefined}
      className={cn(
        "min-w-0 overflow-hidden whitespace-nowrap",
        overflowing && "okf-fade-end",
        className
      )}
    >
      {children}
    </span>
  )
}
