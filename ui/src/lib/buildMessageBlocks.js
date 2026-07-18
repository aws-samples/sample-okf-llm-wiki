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
// render_chart is special: it's both a step in the model's working AND a VISUAL.
// So it appears BOTH ways, without breaking the reasoning: it stays a tool step
// inside the think timeline (a label-only step — its ack result body is suppressed,
// see wikiTools.parseToolResult), and the actual chart renders as its own block
// FLUSHED DIRECTLY BELOW the reasoning block it belongs to (not wedged mid-timeline,
// which used to split "Reasoning" into two). The chart's code + title come from the
// tool CALL's args; ChartFrame renders it in a sandboxed iframe.
const CHART_TOOL = "render_chart"

// Pass 1: fold all tool events (start + result, possibly out of order) into a map
// keyed by tool id, so a block always has the latest known state for each tool.
// render_chart IS included — it renders as a (label-only) timeline step too.
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
  const chartSeen = new Set()
  // Charts discovered inside the CURRENT think block, waiting to be flushed as
  // their own blocks directly BELOW it (so they don't split the reasoning). Flushed
  // when the think block ends (text run, or end of stream).
  let pendingCharts = []

  const flushCharts = () => {
    for (const c of pendingCharts) blocks.push(c)
    pendingCharts = []
  }

  // Close the open think block (and flush its charts right after it), or the open
  // text block — used when a text run breaks the working timeline.
  const closeThink = () => {
    if (think) {
      think.isComplete = true
      think = null
      flushCharts()
    }
  }

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

  // A render_chart call is BOTH a timeline tool step (added via addToolSegment, so
  // the reasoning shows "Charted …") AND a chart block queued to render just below
  // this reasoning block. Dedup the block by tool id so a re-emitted start (history
  // reload) doesn't double-render.
  const queueChart = (ev) => {
    if (!ev.id || chartSeen.has(ev.id)) return
    chartSeen.add(ev.id)
    const args = ev.content && typeof ev.content === "object" ? ev.content : {}
    pendingCharts.push({
      type: "chart",
      id: ev.id,
      code: typeof args.code === "string" ? args.code : "",
      title: typeof args.title === "string" ? args.title : "",
      isComplete: false,
    })
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

    // render_chart: keep it as a tool step in the timeline (label only — its ack
    // result body is suppressed by wikiTools), AND queue the chart to render below
    // the reasoning block. The tool START carries the code+title.
    if (ev.type === "tool" && ev.tool_name === CHART_TOOL) {
      if (ev.tool_start) {
        addToolSegment(ev.id)
        queueChart(ev)
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
      // A text run ends the reasoning block; flush its charts below it, then start
      // (or extend) the answer text.
      closeThink()
      if (text) text.content += ev.content
      else {
        text = { type: "text", content: ev.content, isComplete: false }
        blocks.push(text)
      }
    }
  }

  // End of stream: flush any charts still pending under the final think block.
  flushCharts()

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
