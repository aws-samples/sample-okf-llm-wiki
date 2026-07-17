// The action bar under a completed AI response: copy the answer, and give
// like/dislike feedback. Rendered only for finished turns that produced text
// (no bar while streaming or on a tool-only/empty turn).
//
// Feedback is LOCAL toggle state (mutually exclusive, click again to clear) — the
// chat has no feedback sink yet, so this is a UI affordance; wiring it to a
// backend later just means lifting `feedback` up + a POST. Copy uses the same
// CopyButton primitive as the code viewer.

import { ThumbsDownIcon, ThumbsUpIcon } from "lucide-react"
import { memo, useCallback, useState } from "react"

import { Button } from "@/components/ui/button"
import { ButtonGroup } from "@/components/ui/button-group"
import { CopyButton } from "@/components/ui/copy-button"
import { cn } from "@/lib/utils"

export const ResponseActions = memo(function ResponseActions({ text }) {
  const [feedback, setFeedback] = useState(null) // "up" | "down" | null
  const copy = (text || "").trim()

  const vote = useCallback(
    (v) => setFeedback((cur) => (cur === v ? null : v)),
    []
  )

  return (
    <div className="mt-1 flex items-center gap-1">
      <CopyButton text={copy} label="Copy response" />
      <ButtonGroup className="ml-0.5">
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label="Good response"
          aria-pressed={feedback === "up"}
          onClick={() => vote("up")}
          className={cn(
            "size-7 rounded-md text-muted-foreground hover:text-foreground",
            feedback === "up" && "text-primary hover:text-primary"
          )}
        >
          <ThumbsUpIcon
            className={cn("size-3.5", feedback === "up" && "fill-current")}
          />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label="Bad response"
          aria-pressed={feedback === "down"}
          onClick={() => vote("down")}
          className={cn(
            "size-7 rounded-md text-muted-foreground hover:text-foreground",
            feedback === "down" && "text-destructive hover:text-destructive"
          )}
        >
          <ThumbsDownIcon
            className={cn("size-3.5", feedback === "down" && "fill-current")}
          />
        </Button>
      </ButtonGroup>
    </div>
  )
})
