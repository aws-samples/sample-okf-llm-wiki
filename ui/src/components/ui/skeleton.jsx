import { cn } from "@/lib/utils"

function Skeleton({
  className,
  ...props
}) {
  return (
    <div
      data-slot="skeleton"
      // A foreground TINT, not bg-muted: a tint of --foreground reads on any
      // surface in BOTH themes, while bg-muted's visibility depends on where
      // the (often-retuned) page neutrals sit — it fully vanished on the old
      // soft-gray page. Same fix as the tabs track and the prose code pills.
      className={cn(
        "animate-pulse rounded-md bg-foreground/[0.08] dark:bg-muted",
        className
      )}
      {...props} />
  );
}

export { Skeleton }
