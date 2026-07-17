// One conversation turn: the user's message bubble + the AI response, rendered
// from the turn's raw chunk array via buildMessageBlocks. A `think` block renders
// as the collapsible timeline; a `text` block as markdown. The ring avatar (left)
// signals thinking/done — so there's NO separate typing indicator.
//
// Perf: the component is memo'd and each rendered block is memo'd by its content,
// so during a stream only the LAST (changing) block of the CURRENT turn re-parses
// its markdown — completed turns and earlier blocks don't re-render. This is what
// keeps token rendering smooth instead of getting heavier as the answer grows.

import { memo, useMemo } from "react"

import { AgentAvatar } from "@/components/chat/AgentAvatar"
import { Markdown } from "@/components/chat/Markdown"
import { ResponseActions } from "@/components/chat/ResponseActions"
import { UnifiedThinkingBlock } from "@/components/chat/UnifiedThinkingBlock"
import { buildMessageBlocks } from "@/lib/buildMessageBlocks"

// A stable content signature for a block, so the memo can skip re-rendering when
// nothing about the block actually changed (buildMessageBlocks returns fresh
// objects each flush, so a reference check alone never skips).
function blockSig(block) {
  if (block.type === "text") return `t:${block.content?.length || 0}:${block.content}`
  // think: signature over each segment. Include the segment INDEX and the tool
  // ID — otherwise two calls to the SAME tool (e.g. read_page twice) with no
  // reasoning text between them produce an identical signature, the memo skips
  // the update, and the second tool card never renders (looks like tools
  // "overwriting" each other). Index + id + state makes each segment distinct.
  return (
    "k:" +
    (block.contentSegments || [])
      .map((s, i) =>
        s.type === "text"
          ? `${i}x${s.content?.length || 0}`
          : `${i}#${s.id || ""}:${s.toolName}:${s.isComplete ? 1 : 0}:${s.error ? 1 : 0}`
      )
      .join("|")
  )
}

// A single rendered block, memo'd by CONTENT signature so it only re-parses when
// its own content changes — not when a sibling block streams. During a stream
// only the last (growing) block re-parses; earlier blocks are skipped.
const Block = memo(
  function Block({ block, complete, datasetScope }) {
    if (block.type === "think") {
      return (
        <UnifiedThinkingBlock
          contentBlocks={block.contentSegments}
          isGroupComplete={complete}
        />
      )
    }
    return <Markdown datasetScope={datasetScope}>{block.content}</Markdown>
  },
  (prev, next) =>
    prev.complete === next.complete &&
    prev.datasetScope === next.datasetScope &&
    blockSig(prev.block) === blockSig(next.block)
)

function ChatMessageImpl({ turn, streaming, datasetScope }) {
  const aiEvents = turn.aiMessage || []
  const isEnd = aiEvents.length > 0 && aiEvents[aiEvents.length - 1]?.end === true
  const blocks = useMemo(
    () => buildMessageBlocks(aiEvents, isEnd),
    [aiEvents, isEnd]
  )

  // The answer's plain text (all text blocks joined) — what the copy button
  // copies, and the presence test for the action bar. Tool-only/empty turns have
  // none, so no bar shows for them.
  const answerText = useMemo(
    () =>
      blocks
        .filter((b) => b.type === "text")
        .map((b) => b.content)
        .join("\n\n")
        .trim(),
    [blocks]
  )

  // Show the action bar only once the turn is finished (not mid-stream) and it
  // actually produced an answer.
  const showActions = !streaming && isEnd && answerText.length > 0

  return (
    <div className="flex flex-col gap-5">
      {/* User message — right-aligned bubble. */}
      <div className="flex justify-end">
        <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl bg-primary px-3 py-2 text-sm text-primary-foreground">
          {turn.userMessage}
        </div>
      </div>

      {/* AI response — the twinkling dot avatar (glows while this turn streams,
          freezes when done) beside the stacked answer blocks. The avatar IS the
          loading state, so no typing dots. items-start pins the avatar to the TOP
          so it stays put as the answer grows (not vertically centered). pt-1
          lowers the content ~2px so its first line lines up with the avatar. */}
      <div className="flex items-start gap-2.5">
        <AgentAvatar active={streaming} className="mt-1 shrink-0" />
        <div className="flex min-w-0 flex-1 flex-col gap-1 pt-1">
          {/* No pre-output text hint while waiting — the twinkling avatar IS the
              loading state; content blocks render as they arrive. */}
          {blocks.map((block, i) => {
            const isLast = i === blocks.length - 1
            // A block is "complete" unless it's the last block of a still-
            // streaming turn (that one is the live, growing one).
            const complete = !(streaming && isLast)
            // Key by content so React reuses the memo'd block across flushes.
            const key =
              block.type === "think"
                ? `think-${i}`
                : `text-${i}`
            return (
              <Block
                key={key}
                block={block}
                complete={complete}
                datasetScope={datasetScope}
              />
            )
          })}
          {showActions ? <ResponseActions text={answerText} /> : null}
        </div>
      </div>
    </div>
  )
}

// memo: a completed turn (streaming=false, same turn object) never re-renders
// while a LATER turn streams — only the active turn's tree updates per flush.
export const ChatMessage = memo(ChatMessageImpl)
