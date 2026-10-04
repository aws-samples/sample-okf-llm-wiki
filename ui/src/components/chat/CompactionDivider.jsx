// The line a compaction leaves in the transcript. Messages above it stay on
// screen, but the agent now carries only their summary. "Context Compacted" names
// what happened to the agent, not the transcript. One line per mark
// ({source, phase, pre_tokens, post_tokens}).

import { FoldVerticalIcon } from "lucide-react"

import { formatTokens } from "@/lib/utils"

// Hover text: a requested compaction versus the automatic one at the threshold.
function explain(mark) {
  const sizes =
    mark.pre_tokens && mark.post_tokens
      ? ` (${formatTokens(mark.pre_tokens)} → ${formatTokens(mark.post_tokens)} tokens)`
      : ""
  return mark.source === "auto"
    ? `Earlier messages were summarized automatically when the context window filled${sizes}. They are still shown here; the agent reads the summary.`
    : `Earlier messages were summarized on request${sizes}. They are still shown here; the agent reads the summary.`
}

export function CompactionDivider({ marks }) {
  if (!marks || marks.length === 0) return null
  return (
    <>
      {marks.map((mark, i) => (
        <div
          key={i}
          className="flex items-center gap-2 py-1 text-xs text-muted-foreground"
          title={explain(mark)}
        >
          <span className="h-px flex-1 bg-border" />
          <span className="inline-flex items-center gap-1.5">
            <FoldVerticalIcon className="size-3 shrink-0" />
            <span>
              Context Compacted
              {mark.source === "auto" ? " Automatically" : ""}
            </span>
          </span>
          <span className="h-px flex-1 bg-border" />
        </div>
      ))}
    </>
  )
}
