import { cn } from "@/lib/utils"

function Skeleton({
  className,
  ...props
}) {
  return (
    <div
      data-slot="skeleton"
      // A foreground TINT, not bg-muted: in light mode --muted (0.963) sits
      // barely below --background (0.976), so a muted skeleton on the page
      // surface is nearly invisible. The tint reads in both themes — the
      // same fix as the tabs track and the prose code pills.
      className={cn(
        "animate-pulse rounded-md bg-foreground/[0.08] dark:bg-muted",
        className
      )}
      {...props} />
  );
}

export { Skeleton }
