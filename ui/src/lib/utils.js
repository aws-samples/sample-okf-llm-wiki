import { clsx } from "clsx";
import { twMerge } from "tailwind-merge"

export function cn(...inputs) {
  return twMerge(clsx(inputs));
}

// Compact token counts (1.2K / 3.4M) for the chat's token readouts — the context
// gauge and the per-turn usage popover describe the same conversation, so they
// share one K/M threshold.
export function formatTokens(n) {
  if (!n) return "0"
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)}K`
  return String(n)
}
