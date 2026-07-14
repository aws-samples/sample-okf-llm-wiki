import * as React from "react"

import { cn } from "@/lib/utils"

// Join adjacent buttons into one control: flatten the inner corners (keep the
// outer 2xl radius), and overlap borders by 1px so a shared seam reads as a
// single divider. Children are Buttons (or a DropdownMenuTrigger rendering a
// Button via asChild), so the direct-child selectors land on the button element.
function ButtonGroup({ className, ...props }) {
  return (
    <div
      role="group"
      data-slot="button-group"
      className={cn(
        "flex items-center",
        "[&>*]:rounded-none [&>*:first-child]:rounded-l-2xl [&>*:last-child]:rounded-r-2xl",
        "[&>*:not(:first-child)]:-ml-px",
        // Raise the hovered/focused/open button so its full border shows over
        // the neighbour's overlapping edge.
        "[&>*]:relative [&>*:hover]:z-10 [&>*:focus-visible]:z-10 [&>*[aria-expanded=true]]:z-10",
        className
      )}
      {...props}
    />
  )
}

export { ButtonGroup }
