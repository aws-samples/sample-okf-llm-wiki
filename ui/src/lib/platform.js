// Platform-aware keyboard-shortcut helpers: the web app shows macOS symbols
// (⇧⌘O) on Apple platforms and "Ctrl+Shift+O" everywhere else, and matches
// the chord on the corresponding modifier (metaKey vs ctrlKey).

export const IS_MAC =
  typeof navigator !== "undefined" &&
  /Mac|iPhone|iPad|iPod/i.test(navigator.platform || navigator.userAgent || "")

// ⇧⌘O / Ctrl+Shift+O — Claude's new-chat binding, kept for familiarity.
export const NEW_CHAT_SHORTCUT_LABEL = IS_MAC ? "⇧⌘O" : "Ctrl+Shift+O"

export function isNewChatShortcut(e) {
  const mod = IS_MAC ? e.metaKey : e.ctrlKey
  // e.code is layout-independent (the physical O key). The e.key fallback is
  // used ONLY when the event carries no code (synthetic events, some IMEs) —
  // OR-ing the two would make the chord fire on TWO different keys on
  // non-QWERTY layouts and hijack unrelated shortcuts (e.g. Dvorak's ⇧⌘R
  // sits on the physical KeyO).
  const isO = e.code ? e.code === "KeyO" : (e.key || "").toLowerCase() === "o"
  return mod && e.shiftKey && !e.altKey && isO
}
