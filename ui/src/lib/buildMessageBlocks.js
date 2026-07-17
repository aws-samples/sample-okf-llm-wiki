// Turn a turn's raw AI event array (the chunks the SSE reader appended) into
// ordered render blocks. Ported from Sparky's buildMessageBlocks, trimmed to the
// wiki chat's chunk vocabulary (text / think / tool). No canvas / browser /
// images / citations.
//
// Two block kinds:
//   { type:"think", contentSegments:[ {type:"text",content} | {type:"tool",...} ], isComplete }
//   { type:"text",  content, isComplete }
//
// Reasoning tokens and tool calls collapse into a single "think" timeline block
// (the collapsible ThinkingBlock renders it); an assistant text run breaks the
// think block and starts a text block. This grouping is what makes reasoning +
// tools read as one "working…" section above the answer, exactly like Sparky.

// Pass 1: fold all tool events (start + result, possibly out of order) into a map
// keyed by tool id, so a block always has the latest known state for each tool.
function collectTools(events) {
  const toolsById = new Map()
  for (const ev of events) {
    if (ev.type !== "tool" || !ev.id) continue
    if (!toolsById.has(ev.id)) {
      toolsById.set(ev.id, {
        id: ev.id,
        toolName: ev.tool_name,
        input: null,
        content: null,
        isComplete: false,
        error: false,
      })
    }
    const t = toolsById.get(ev.id)
    if (ev.tool_start) {
      t.toolName = ev.tool_name || t.toolName
      t.input = ev.content
    } else {
      t.toolName = ev.tool_name || t.toolName
      t.content = ev.content
      t.isComplete = true
      t.error = Boolean(ev.error)
    }
  }
  return toolsById
}

// Pass 2: walk the events in order, assembling blocks. Tools attach to the
// current think block as segments (in call order); text runs break it.
export function buildMessageBlocks(events, isEnd) {
  if (!events || events.length === 0) return []
  const toolsById = collectTools(events)

  const blocks = []
  let think = null
  let text = null
  const toolSeen = new Set()

  const openThink = () => {
    if (think) return think
    if (text) {
      text.isComplete = true
      text = null
    }
    think = { type: "think", contentSegments: [], isComplete: false }
    blocks.push(think)
    return think
  }

  const addToolSegment = (toolId) => {
    if (toolSeen.has(toolId)) return
    const t = toolsById.get(toolId)
    if (!t) return
    const tb = openThink()
    tb.contentSegments.push({ type: "tool", ...t })
    toolSeen.add(toolId)
  }

  for (const ev of events) {
    if (ev.end) continue

    if (ev.type === "tool" && ev.id && ev.tool_start) {
      addToolSegment(ev.id)
      continue
    }
    if (ev.type === "tool") continue // results are folded via toolsById

    if (ev.type === "think" && ev.content != null) {
      const tb = openThink()
      const last = tb.contentSegments[tb.contentSegments.length - 1]
      if (last?.type === "text") last.content += ev.content
      else tb.contentSegments.push({ type: "text", content: ev.content })
      continue
    }

    if (ev.type === "text" && ev.content != null) {
      if (think) {
        think.isComplete = true
        think = null
      }
      if (text) text.content += ev.content
      else {
        text = { type: "text", content: ev.content, isComplete: false }
        blocks.push(text)
      }
    }
  }

  // Refresh each attached tool segment with its final folded state (a result may
  // have arrived after the start was placed) and mark everything complete at end.
  for (const b of blocks) {
    if (b.type !== "think") continue
    b.contentSegments = b.contentSegments.map((seg) => {
      if (seg.type !== "tool") return seg
      const t = toolsById.get(seg.id)
      return t ? { type: "tool", ...t } : seg
    })
  }

  if (isEnd) {
    for (const b of blocks) {
      b.isComplete = true
      if (b.type === "think") {
        b.contentSegments.forEach((s) => {
          if (s.type === "tool") s.isComplete = true
        })
      }
    }
  }

  return blocks
}
