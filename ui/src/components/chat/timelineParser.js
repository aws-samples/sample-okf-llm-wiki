// Timeline parser — ported from Sparky's timelineParser.js. Turns a think
// block's contentSegments (text + tool) into ordered TimelineStep objects for
// UnifiedThinkingBlock. Trimmed to the wiki chat's segment vocabulary (no web /
// browser segments).

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
  return merged
}
