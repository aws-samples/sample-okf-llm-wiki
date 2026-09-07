// A tiny copy-to-clipboard button — click to copy `text`, shows a check for ~1.5s.
// No dependency beyond lucide + the Button primitive; used by the chat code viewer.

import { CheckIcon, CopyIcon } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

export function CopyButton({
  text,
  className,
  label = "Copy",
  showLabel = false,
  variant = "ghost",
  size,
}) {
  const [copied, setCopied] = useState(false)
  const timerRef = useRef(null)
  const buttonLabel = copied ? "Copied" : label

  useEffect(() => () => clearTimeout(timerRef.current), [])

  const onCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text || "")
      setCopied(true)
      clearTimeout(timerRef.current)
      timerRef.current = setTimeout(() => setCopied(false), 1500)
    } catch {
      // clipboard blocked (permissions / insecure context) — no-op, no crash
    }
  }, [text])

  return (
    <Button
      type="button"
      variant={variant}
      size={size || (showLabel ? "sm" : "icon")}
      onClick={onCopy}
      aria-label={buttonLabel}
      title={buttonLabel}
      className={cn(
        !showLabel && "size-7 text-muted-foreground hover:text-foreground",
        className
      )}
    >
      {copied ? (
        <CheckIcon data-icon="inline-start" className="text-primary" />
      ) : (
        <CopyIcon data-icon="inline-start" />
      )}
      {showLabel ? buttonLabel : null}
    </Button>
  )
}
