// Turn a turn's raw AI event array (the chunks the SSE reader appended) into
// ordered render blocks. Ported from Sparky's buildMessageBlocks, trimmed to the
// wiki chat's chunk vocabulary (text / think / tool). No canvas / browser /
// images / citations.
//
// Three block kinds:
//   { type:"think", contentSegments:[ {type:"text",content} | {type:"tool",...} ], isComplete }
//   { type:"text",  content, isComplete }
//   { type:"chart", id, code, title, isComplete }
//
// Reasoning tokens and (most) tool calls collapse into a single "think" timeline
// block (the collapsible ThinkingBlock renders it); an assistant text run breaks
// the think block and starts a text block. This grouping is what makes reasoning +
// tools read as one "working…" section above the answer, exactly like Sparky.
//
// render_chart is special: it's a VISUAL, not a working step. Its tool START
// BREAKS the thinking block (exactly like a text run does) and the chart becomes
// its own block in sequence — it never appears as a step inside the timeline.
// The chart's code + title come from the tool CALL's args (fully assembled, from
// the updates stream); ChartFrame renders it in a sandboxed iframe with its own
// "generating…" reveal. Any reasoning/tools after the chart open a NEW think block.
const CHART_TOOL = "render_chart"

// Pass 1: fold all tool events (start + result, possibly out of order) into a map
// keyed by tool id, so a block always has the latest known state for each tool.
// render_chart IS included — its folded ack marks the chart block complete.
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
// current think block as segments (in call order); text runs and charts break it.
export function buildMessageBlocks(events, isEnd) {
  if (!events || events.length === 0) return []
  const toolsById = collectTools(events)

  const blocks = []
  let think = null
  let text = null
  const toolSeen = new Set()
  const chartSeen = new Set()

  // Close the open think block — used when a text run or a chart breaks the
  // working timeline.
  const closeThink = () => {
    if (think) {
      think.isComplete = true
      think = null
    }
  }

  const closeText = () => {
    if (text) {
      text.isComplete = true
      text = null
    }
  }

  const openThink = () => {
    if (think) return think
    closeText()
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

    // render_chart: the chart BREAKS the working timeline (and any open text run)
    // and renders as its own block, in order. The tool START carries the whole
    // code+title; the ack result (folded via toolsById) marks the block complete.
    // Dedup by tool id so a re-emitted start (history reload) doesn't double-render.
    if (ev.type === "tool" && ev.tool_name === CHART_TOOL) {
      if (ev.tool_start && ev.id && !chartSeen.has(ev.id)) {
        chartSeen.add(ev.id)
        closeThink()
        closeText()
        const args = ev.content && typeof ev.content === "object" ? ev.content : {}
        blocks.push({
          type: "chart",
          id: ev.id,
          code: typeof args.code === "string" ? args.code : "",
          title: typeof args.title === "string" ? args.title : "",
          isComplete: Boolean(toolsById.get(ev.id)?.isComplete),
        })
      }
      continue
    }

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
      // A text run ends the reasoning block, then starts (or extends) the answer.
      closeThink()
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
