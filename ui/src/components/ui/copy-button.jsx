// A tiny copy-to-clipboard button — click to copy `text`, shows a check for ~1.5s.
// No dependency beyond lucide + the Button primitive; used by the chat code viewer.

import { CheckIcon, CopyIcon } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

export function CopyButton({ text, className, label = "Copy" }) {
  const [copied, setCopied] = useState(false)
  const timerRef = useRef(null)

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
      variant="ghost"
      size="icon"
      onClick={onCopy}
      aria-label={copied ? "Copied" : label}
      title={copied ? "Copied" : label}
      className={cn("size-7 text-muted-foreground hover:text-foreground", className)}
    >
      {copied ? (
        <CheckIcon className="size-3.5 text-emerald-500" />
      ) : (
        <CopyIcon className="size-3.5" />
      )}
    </Button>
  )
}
