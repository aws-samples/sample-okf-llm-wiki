// The chat page's floating side-panel chrome — shared by the doc-peek reader
// and the history drawer so both read as the SAME surface: an inset, rounded
// card that PUSHES the chat (each is mounted in one of ChatPanel's
// width-animated clips, so open/close is a smooth slide).
//
// Optionally resizable: pass `onResizeStart` and a drawer-style grip renders in
// a left GUTTER beside the card (not on its border). The drag itself — width
// state, clamping, persistence — is owned by ChatPanel; `resizing` mirrors its
// drag state so the grip holds its cyan (primary) tint even when the pointer
// outruns the handle mid-drag (where CSS :active would flicker).

import { cn } from "@/lib/utils"

export function PanelShell({ onResizeStart, resizing = false, children }) {
  return (
    <div
      className={cn(
        "relative flex h-full flex-col py-3 pr-3",
        onResizeStart && "pl-2.5"
      )}
    >
      {onResizeStart ? (
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize panel"
          onPointerDown={onResizeStart}
          className="group absolute inset-y-3 left-0 z-10 flex w-2.5 cursor-col-resize touch-none items-center justify-center select-none"
        >
          <div
            className={cn(
              "h-10 w-[3px] rounded-full transition-colors duration-150",
              resizing ? "bg-primary" : "bg-border group-hover:bg-primary/60"
            )}
          />
        </div>
      ) : null}
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border bg-card shadow-sm">
        {children}
      </div>
    </div>
  )
}
