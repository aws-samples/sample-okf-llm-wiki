// Hand-rolled SSE reader for the chat runtime — Sparky's streaming approach.
//
// The runtime streams `data: {json}\n\n` frames of typed chunks:
//   {type:"think",  content}                                    reasoning
//   {type:"tool", id, tool_name, tool_start:true,  content}     tool call start (args)
//   {type:"tool", id, tool_name, tool_start:false, content, error}  tool result
//   {type:"text",  content}                                     answer tokens
//   {type:"error", error_code, message}                         a failure
//   {end:true, token_stats, checkpoint_id}                      terminal marker
//
// We read the response body ourselves and hand each parsed chunk to `onChunk`.
// The consumer (useChatSession) appends chunks to the current turn's raw event
// list; buildMessageBlocks turns that list into render blocks. This is why we
// never had the assistant-ui rendering-slot problems — we own every layer.

// Parse a single SSE line into a chunk object, or null if not a data line.
export function parseSSELine(line) {
  if (!line.startsWith("data:")) return null
  const jsonStr = line.slice(line.indexOf(":") + 1).trim()
  if (!jsonStr) return null
  return JSON.parse(jsonStr)
}

// Consume an SSE Response, invoking onChunk(chunk) for every parsed frame.
// Resolves when the stream ends (an `{end:true}` chunk or the body closing).
// `signal` (from an AbortController) cancels the read loop for the stop button.
export async function consumeSSE(response, onChunk, { signal } = {}) {
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""

  if (signal) {
    signal.addEventListener(
      "abort",
      () => {
        reader.cancel().catch(() => {})
      },
      { once: true }
    )
  }

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      let nl
      while ((nl = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, nl)
        buffer = buffer.slice(nl + 1)
        if (!line.startsWith("data:")) continue
        let chunk
        try {
          chunk = parseSSELine(line)
        } catch {
          // Frames are single-line JSON (the server writes `data: ${json}\n\n`
          // with no embedded newlines), so a line terminated by "\n" ALWAYS holds
          // a complete frame. A parse failure here is therefore a genuinely
          // malformed frame, not a mid-read split — skip it and keep reading. (A
          // frame split across reads has no trailing newline yet, so it stays
          // buffered until the rest arrives and never reaches this branch.) This
          // avoids matching engine-specific JSON error text, which differs across
          // browsers and silently dropped split frames on Firefox/Safari.
          continue
        }
        if (!chunk) continue
        onChunk(chunk)
        if (chunk.end) return
      }
    }
    // Flush a trailing frame with no closing newline.
    const tail = buffer.trim()
    if (tail.startsWith("data:")) {
      try {
        const chunk = parseSSELine(tail)
        if (chunk) onChunk(chunk)
      } catch {
        // ignore a malformed trailing frame
      }
    }
  } finally {
    reader.releaseLock?.()
  }
}
