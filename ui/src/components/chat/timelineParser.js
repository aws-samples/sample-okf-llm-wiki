// Timeline parser — ported from Sparky's timelineParser.js. Turns a think
// block's contentSegments (text + tool) into ordered TimelineStep objects for
// UnifiedThinkingBlock. Trimmed to the wiki chat's segment vocabulary (no web /
// browser segments).

import { splitReasoningSections } from "@/lib/reasoningSections"

export function buildTimelineSteps(contentSegments) {
  if (!Array.isArray(contentSegments)) return []

  const steps = []
  for (const segment of contentSegments) {
    if (segment.type === "text" && segment.content) {
      steps.push({
        id: `think-${steps.length}`,
        type: "thinking",
        segment: {
          id: `segment-${steps.length}`,
          content: segment.content,
          type: "paragraph",
        },
      })
    } else if (segment.type === "tool") {
      steps.push({
        id: `tool-${steps.length}`,
        type: "tool",
        toolName: segment.toolName,
        toolContent: segment.content,
        toolInput: segment.input,
        isToolComplete: segment.isComplete,
        toolError: segment.error,
        // Optional collapse count (the canvas-jobs drawer folds consecutive
        // same-tool calls into one step) — renders as a ×N suffix.
        toolCount: segment.count,
      })
    } else if (segment.type === "steer" && segment.content) {
      // A harness steering note — its own timeline step (bulb marker).
      steps.push({
        id: `steer-${steps.length}`,
        type: "steer",
        content: segment.content,
      })
    } else if (segment.type === "policy" && segment.content) {
      // A query-time policy flag — its own timeline step (shield marker).
      steps.push({
        id: `policy-${steps.length}`,
        type: "policy",
        content: segment.content,
      })
    }
  }
  return steps
}

// Merge consecutive thinking steps into one (Sparky's mergeThinkingSteps), so a
// run of reasoning tokens renders as a single timeline step, not one per token.
export function mergeThinkingSteps(steps) {
  const merged = []
  let group = null
  for (const step of steps) {
    if (step.type === "thinking") {
      if (group) group.segments.push(step.segment)
      else group = { id: step.id, type: "thinking", segments: [step.segment] }
    } else {
      if (group) {
        merged.push(group)
        group = null
      }
      merged.push(step)
    }
  }
  if (group) merged.push(group)
  return merged.flatMap(sectionThinkingStep)
}

// A merged reasoning step whose text carries GPT-style summary parts
// ("**Title**" line + body) fans out into one step PER PART, the title riding
// as the step's label. Untitled reasoning (Claude) stays one step, untouched.
// Ids are positional (`<id>-s<k>`), so a streaming part keeps its React key
// as it grows and a new part only appends a step.
function sectionThinkingStep(step) {
  if (step.type !== "thinking") return [step]
  const text = step.segments.map((s) => s?.content || "").join("\n\n")
  const sections = splitReasoningSections(text)
  if (!sections.some((s) => s.title)) return [step]
  return sections.map((s, k) => ({
    id: `${step.id}-s${k}`,
    type: "thinking",
    title: s.title,
    segments: s.body ? [{ id: `${step.id}-s${k}-body`, content: s.body, type: "paragraph" }] : [],
  }))
}
