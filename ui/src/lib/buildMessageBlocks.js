// Turn a turn's raw AI event array (the chunks the SSE reader appended) into
// ordered render blocks. Ported from Sparky's buildMessageBlocks, trimmed to the
// wiki chat's chunk vocabulary (text / think / tool). No canvas / browser /
// images / citations.
//
// Four block kinds:
//   { type:"think", contentSegments:[ {type:"text",content} | {type:"tool",...} ], isComplete }
//   { type:"text",  content, isComplete }
//   { type:"chart", id, code, title, isComplete }
//   { type:"report", id, toolName, title, reportId, pending, isComplete, error? }
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

// present_report is the DISPLAY step and the only lifted report tool —
// create_report stays an ORDINARY working step inside the thinking timeline
// (its refusals show there like any other tool error). The card is PINNED to
// the bottom of the AI turn: collected into a separate list and appended
// after every other block, so tool calls and text emitted later never push
// it up into the middle of the response.
const REPORT_TOOL = "present_report"

// create_report's args (the whole blocks_json) stream for a long time, so its
// generic timeline step would only appear at args-complete — the user would
// see nothing while the report composes. Its tool_pending opens the segment
// immediately; the args-complete start fills that SAME segment in place.
const CREATE_TOOL = "create_report"

// Steering notes (server "steer" chunks — chat.steering's course-correction
// reminders) are shown in the thinking timeline by default; set
// VITE_CHAT_SHOW_STEERING=false to hide them. Display-only: the model was
// steered server-side either way.
const SHOW_STEERING =
  String(import.meta.env.VITE_CHAT_SHOW_STEERING ?? "true") !== "false"

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

// A report tool's ack is JSON — already an object when the stream decoded it,
// a raw string otherwise. Unparseable → null (the block stays on its args).
function parseReportAck(content) {
  if (content && typeof content === "object") return content
  if (typeof content === "string") {
    try {
      return JSON.parse(content)
    } catch {
      return null
    }
  }
  return null
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
  const reportSeen = new Set()

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

  // Pending chart blocks by tool id -> index into `blocks`, so the
  // args-complete start can fill the SAME block in place (no reflow, and the
  // placeholder keeps its slot). Set when a tool_pending for render_chart
  // arrives — the model has STARTED the call but is still generating its args
  // (the chart code), which for charts is the long part.
  const chartPendingAt = new Map()
  // Report cards live OUTSIDE the ordered block list (bottom-pinned): the
  // tool_pending opens the card in its pending state, the args-complete start
  // fills it in place, and the list is appended after all other blocks.
  const reportCards = []
  const reportPendingAt = new Map()
  // Timeline segments opened at tool_pending (create_report), by tool id —
  // the args-complete start fills the SAME segment instead of appending a
  // duplicate.
  const pendingSeg = new Map()

  for (const ev of events) {
    if (ev.end) continue

    // render_chart announced EARLY (first streamed fragment of the call): open
    // the chart block immediately, code-less — ChartFrame renders the
    // generating theater for it while the model writes the code. Non-chart
    // tool_pending events are ignored (the timeline waits for the reliable
    // args-complete start from the updates stream).
    if (ev.type === "tool_pending") {
      if (
        ev.tool_name === CHART_TOOL &&
        ev.id &&
        !chartSeen.has(ev.id) &&
        !chartPendingAt.has(ev.id)
      ) {
        closeThink()
        closeText()
        chartPendingAt.set(ev.id, blocks.length)
        blocks.push({
          type: "chart",
          id: ev.id,
          pending: true,
          code: "",
          title: "",
          isComplete: false,
        })
      }
      if (
        ev.tool_name === REPORT_TOOL &&
        ev.id &&
        !reportSeen.has(ev.id) &&
        !reportPendingAt.has(ev.id)
      ) {
        // No closeThink/closeText: the card is not inline — it renders at the
        // turn's bottom regardless of where the call fired.
        reportPendingAt.set(ev.id, reportCards.length)
        reportCards.push({
          type: "report",
          id: ev.id,
          toolName: ev.tool_name,
          title: "",
          reportId: "",
          pending: true,
          isComplete: false,
        })
      }
      if (
        ev.tool_name === CREATE_TOOL &&
        ev.id &&
        !toolSeen.has(ev.id) &&
        !pendingSeg.has(ev.id)
      ) {
        // Announce the step now, args-less — the label shimmers while the
        // model writes the blocks_json.
        const seg = {
          type: "tool",
          id: ev.id,
          toolName: CREATE_TOOL,
          input: undefined,
          content: undefined,
          isComplete: false,
        }
        openThink().contentSegments.push(seg)
        pendingSeg.set(ev.id, seg)
      }
      continue
    }

    // render_chart: the chart BREAKS the working timeline (and any open text run)
    // and renders as its own block, in order. The tool START carries the whole
    // code+title; the ack result (folded via toolsById) marks the block complete.
    // Dedup by tool id so a re-emitted start (history reload) doesn't double-render.
    if (ev.type === "tool" && ev.tool_name === CHART_TOOL) {
      if (ev.tool_start && ev.id && !chartSeen.has(ev.id)) {
        chartSeen.add(ev.id)
        const args = ev.content && typeof ev.content === "object" ? ev.content : {}
        const filled = {
          type: "chart",
          id: ev.id,
          code: typeof args.code === "string" ? args.code : "",
          title: typeof args.title === "string" ? args.title : "",
          isComplete: Boolean(toolsById.get(ev.id)?.isComplete),
        }
        const at = chartPendingAt.get(ev.id)
        if (at != null) {
          blocks[at] = filled // fill the pending block in place
          chartPendingAt.delete(ev.id)
        } else {
          closeThink()
          closeText()
          blocks.push(filled)
        }
      }
      continue
    }

    // present_report: the report card. Args carry report_id + title (the id
    // exists before the call — create_report returned it); the folded RESULT
    // ack can still override the title or flag an error (a bad id).
    if (ev.type === "tool" && ev.tool_name === REPORT_TOOL) {
      if (ev.tool_start && ev.id && !reportSeen.has(ev.id)) {
        reportSeen.add(ev.id)
        const args = ev.content && typeof ev.content === "object" ? ev.content : {}
        const t = toolsById.get(ev.id)
        const ack = t?.isComplete ? parseReportAck(t.content) : null
        const filled = {
          type: "report",
          id: ev.id,
          toolName: ev.tool_name || "",
          title:
            typeof ack?.title === "string" && ack.title
              ? ack.title
              : typeof args.title === "string"
                ? args.title
                : "",
          reportId:
            typeof ack?.report_id === "string" && ack.report_id
              ? ack.report_id
              : typeof args.report_id === "string"
                ? args.report_id
                : "",
          pending: false,
          isComplete: Boolean(t?.isComplete),
        }
        if (typeof ack?.error === "string" && ack.error) {
          filled.error = ack.error
        } else if (t?.error) {
          // Transport-level failure (ToolMessage status="error") — the content
          // is the raised message, not a JSON ack.
          filled.error =
            typeof t.content === "string" && t.content
              ? t.content
              : "report tool failed"
        }
        const at = reportPendingAt.get(ev.id)
        if (at != null) {
          reportCards[at] = filled // fill the pending card in place
          reportPendingAt.delete(ev.id)
        } else {
          reportCards.push(filled)
        }
      }
      continue
    }

    if (ev.type === "tool" && ev.id && ev.tool_start) {
      const seg = pendingSeg.get(ev.id)
      if (seg) {
        // The step was announced at tool_pending — fill that segment in place
        // (the folded entry carries the args and any already-arrived result).
        Object.assign(seg, toolsById.get(ev.id))
        pendingSeg.delete(ev.id)
        toolSeen.add(ev.id)
      } else {
        addToolSegment(ev.id)
      }
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

    // A steering note joins the working timeline as its own segment (rendered
    // with a bulb marker). It arrives whole — no accumulation.
    if (ev.type === "steer" && ev.content) {
      if (!SHOW_STEERING) continue
      const tb = openThink()
      tb.contentSegments.push({ type: "steer", content: ev.content })
      continue
    }

    // A query-time policy flag (split out of a run_sql result server-side):
    // its own segment with a shield marker. Never gated — unlike steering,
    // this is user-relevant substance, not harness meta.
    if (ev.type === "policy" && ev.content) {
      const tb = openThink()
      tb.contentSegments.push({ type: "policy", content: ev.content })
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

  // Bottom-pin the report cards: whatever else the turn emitted, the report
  // is the response's final element.
  const all = reportCards.length ? [...blocks, ...reportCards] : blocks

  if (isEnd) {
    for (const b of all) {
      b.isComplete = true
      if (b.type === "think") {
        b.contentSegments.forEach((s) => {
          if (s.type === "tool") s.isComplete = true
        })
      }
    }
  }

  return all
}
