import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useAuth } from "react-oidc-context"
import {
  BoxesIcon,
  CheckIcon,
  ChevronsUpDownIcon,
  DatabaseIcon,
  FileTextIcon,
  GaugeIcon,
  GlobeIcon,
  HistoryIcon,
  KeyRoundIcon,
  LogInIcon,
  LogOutIcon,
  MessageSquarePlusIcon,
  MessagesSquareIcon,
  MonitorIcon,
  MoonIcon,
  NetworkIcon,
  PanelLeftIcon,
  PlayIcon,
  SunIcon,
} from "lucide-react"

import { ChatPanel } from "@/components/ChatPanel"
import { WikiCubeIcon } from "@/components/WikiCubeIcon"
import { useChatController } from "@/hooks/useChatController"
import { makeApi } from "@/lib/api"
import { signInPreservingRoute } from "@/lib/auth"
import {
  loadRecentDatasets,
  pushRecentDataset,
} from "@/lib/recentDatasets"
import { useRouter } from "@/lib/route"
import { cn } from "@/lib/utils"
import { useTheme } from "@/components/theme-provider.jsx"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
  SidebarProvider,
  useSidebar,
} from "@/components/ui/sidebar"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Toaster } from "@/components/ui/sonner"

import DomainsView from "@/views/DomainsView.jsx"
import MappingsView from "@/views/MappingsView.jsx"
import ContextView from "@/views/ContextView.jsx"
import CredentialsView from "@/views/CredentialsView.jsx"
import HarvestView from "@/views/HarvestView.jsx"
import BenchmarkView from "@/views/BenchmarkView.jsx"
import BrowseView from "@/views/BrowseView.jsx"
import GraphView from "@/views/GraphView.jsx"

// The console sections, in sidebar order. `needsSelection` gates the
// dataset-scoped views so the breadcrumb can hint when nothing is picked.
const NAV = [
  { key: "domains", label: "Domains", icon: GlobeIcon, needsSelection: false },
  {
    key: "mappings",
    label: "Datasets",
    icon: DatabaseIcon,
    needsSelection: false,
  },
  {
    key: "context",
    label: "Context Docs",
    icon: FileTextIcon,
    needsSelection: true,
  },
  { key: "harvest", label: "Harvest", icon: PlayIcon, needsSelection: true },
  {
    key: "benchmark",
    label: "Benchmark",
    icon: GaugeIcon,
    needsSelection: true,
  },
  { key: "browse", label: "Browse", icon: BoxesIcon, needsSelection: true },
  { key: "graph", label: "Graph", icon: NetworkIcon, needsSelection: true },
  // Chat spans the whole wiki (the "@" picker narrows it), so it needs no
  // pre-selected dataset.
  {
    key: "chat",
    label: "Chat",
    icon: MessagesSquareIcon,
    needsSelection: false,
  },
  {
    key: "credentials",
    label: "Credentials",
    icon: KeyRoundIcon,
    needsSelection: false,
  },
]

function LoginScreen({ onSignIn }) {
  return (
    <div className="flex min-h-svh items-center justify-center p-6">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <WikiCubeIcon className="size-6 text-primary" />
            Data wiki
          </CardTitle>
          <CardDescription>
            Sign in to manage Data wiki domains, harvests, and bundles.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button className="w-full" onClick={onSignIn}>
            <LogInIcon data-icon="inline-start" />
            Sign in with Cognito
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}

// Dataset picker shown in the top-bar breadcrumb (in place of the dataset
// name), on views that need a selection. Populated from the registry mappings;
// the chosen (data_domain, dataset) is shared with Context / Harvest / Browse.
// Styled to sit inline in the breadcrumb: borderless/transparent, muted text,
// with a subtle hover — it reads as the breadcrumb's last segment, not a form.
function DatasetPicker({ datasets, selectionKey, onChange, loading }) {
  const [open, setOpen] = useState(false)

  // Registry still loading: hold the picker's spot with a skeleton instead of
  // flashing the empty-state text before the first response lands.
  if (loading) {
    return <Skeleton className="h-4 w-40" />
  }

  if (!datasets.length) {
    return <span className="text-sm text-muted-foreground">No datasets</span>
  }

  const select = (key) => {
    onChange(key)
    setOpen(false)
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          role="combobox"
          aria-expanded={open}
          // Match the old Select trigger's inline breadcrumb look: -ml-1.5
          // cancels the button's left padding so the TEXT lines up with the
          // content column's left edge, muted until hover, no focus ring box.
          className="-ml-1.5 h-7 -translate-y-px gap-1 px-1.5 font-normal text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:ring-0 [&_svg]:text-muted-foreground/70"
        >
          {selectionKey || "Select a dataset…"}
          <ChevronsUpDownIcon data-icon="inline-end" className="opacity-70" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-72 p-0" align="start">
        <Command>
          <CommandInput placeholder="Search datasets…" />
          <CommandList>
            <CommandEmpty>No datasets found.</CommandEmpty>
            <CommandGroup>
              {datasets.map((d) => {
                const key = `${d.data_domain}/${d.dataset}`
                return (
                  <CommandItem
                    key={key}
                    value={key}
                    // Use the closure key, not onSelect's arg — cmdk may
                    // normalise the passed value, which would corrupt the key.
                    onSelect={() => select(key)}
                  >
                    <CheckIcon
                      data-icon="inline-start"
                      className={cn(
                        selectionKey === key ? "opacity-100" : "opacity-0"
                      )}
                    />
                    {key}
                  </CommandItem>
                )
              })}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

// Cycles system -> light -> dark. Rendered as a SidebarMenuButton so it matches
// the nav items exactly (hover, collapsed icon + tooltip).
function ThemeToggle() {
  const { theme, setTheme } = useTheme()
  const next =
    theme === "system" ? "light" : theme === "light" ? "dark" : "system"
  const Icon =
    theme === "dark" ? MoonIcon : theme === "light" ? SunIcon : MonitorIcon
  const label =
    theme === "system"
      ? "System theme"
      : theme === "light"
        ? "Light theme"
        : "Dark theme"
  return (
    <SidebarMenuItem>
      <SidebarMenuButton tooltip={label} onClick={() => setTheme(next)}>
        <Icon />
        <span>{label}</span>
      </SidebarMenuButton>
    </SidebarMenuItem>
  )
}

// Shared collapse/expand toggle. Matches the SidebarMenuButton icon look
// (size-8, rounded-md, ghost) so it reads as the same kind of control wherever
// it appears. `className` lets each surface supply its own colors: the sidebar
// header uses light-on-dark sidebar tokens; the top bar uses the default ghost
// (foreground) colors so it stays legible on the light chrome.
function SidebarToggle({ label, className }) {
  const { toggleSidebar } = useSidebar()
  return (
    <Button
      variant="ghost"
      size="icon"
      aria-label={label}
      onClick={toggleSidebar}
      className={cn("rounded-md", className)}
    >
      <PanelLeftIcon />
    </Button>
  )
}

// Collapsed-sidebar trigger with QUICK NAV. At rest it shows the wiki cube (a
// compact brand affordance in the corner); hovering morphs it into the usual
// expand icon AND opens a popover with the section menu, so users can jump
// between views without expanding the sidebar. Clicking still expands the
// sidebar (unchanged). The popover opens/closes on hover with a short close
// DELAY — the pointer crosses a gap between button and panel, and an immediate
// close on mouseleave would flicker the panel shut before it can be reached.
function CollapsedNavTrigger({
  label,
  className,
  section,
  onNavigate,
  chatCtrl,
  recents,
  selectionKey,
  onSelectRecent,
}) {
  const { toggleSidebar } = useSidebar()
  const [open, setOpen] = useState(false)
  const closeTimer = useRef(null)
  // Ignore hover-opens right after mount: collapsing the sidebar mounts this
  // trigger, and the browser re-evaluates hover on that layout change — firing
  // a synthetic mouseenter with NO pointer movement, which flashed the popover
  // open for a moment on every collapse. Real hovers arrive later than this.
  const mountedAt = useRef(0)
  useEffect(() => {
    mountedAt.current = performance.now()
    return () => closeTimer.current && clearTimeout(closeTimer.current)
  }, [])
  const hoverOpen = useCallback(() => {
    if (performance.now() - mountedAt.current < 350) return
    if (closeTimer.current) clearTimeout(closeTimer.current)
    closeTimer.current = null
    setOpen(true)
  }, [])
  const hoverClose = useCallback(() => {
    if (closeTimer.current) clearTimeout(closeTimer.current)
    closeTimer.current = setTimeout(() => setOpen(false), 140)
  }, [])
  return (
    <Popover
      open={open}
      // Only honor CLOSE requests (outside click / escape); opening is
      // hover-driven, and the trigger click must keep meaning "expand sidebar",
      // not "toggle popover".
      onOpenChange={(v) => {
        if (!v) setOpen(false)
      }}
    >
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          aria-label={label}
          onClick={toggleSidebar}
          onMouseEnter={hoverOpen}
          onMouseLeave={hoverClose}
          // aria-expanded:bg-transparent: the ghost variant paints an
          // aria-expanded (popover open) background — reads as a stuck hover
          // on the logo. Hover tint still applies while actually hovered.
          className={cn(
            "group/collapsed rounded-md aria-expanded:bg-transparent",
            className
          )}
        >
          {/* size-8 = the SidebarBrand logo size, so the mark reads identically
              collapsed and expanded (the SVG's internal margins keep it clear
              of the button edges). */}
          <WikiCubeIcon className="size-8 text-primary group-hover/collapsed:hidden" />
          <PanelLeftIcon className="hidden size-5 group-hover/collapsed:block" />
        </Button>
      </PopoverTrigger>
      {/* Styled as a floating slice of the real sidebar: same surface tokens,
          same group label, and the REAL SidebarMenu/SidebarMenuButton
          components (their styling is self-contained), so items render
          pixel-identical to the expanded sidebar. */}
      <PopoverContent
        side="bottom"
        align="start"
        sideOffset={4}
        onMouseEnter={hoverOpen}
        onMouseLeave={hoverClose}
        // Radix dismisses popovers when focus moves OUTSIDE the content — and
        // navigating to chat auto-focuses the composer, which read as exactly
        // that. This popover is hover-scoped, not focus-scoped: ignore
        // focus-outside; hover-out / Escape / outside CLICKS still close it.
        onFocusOutside={(e) => e.preventDefault()}
        className="w-56 border-sidebar-border bg-sidebar p-2 text-sidebar-foreground"
      >
        <SidebarGroupLabel>Manage</SidebarGroupLabel>
        <SidebarMenu>
          {NAV.map((item) =>
            item.key === "chat" && chatCtrl ? (
              // The REAL ChatNav (submenu included). Clicks deliberately do
              // NOT close the popover — it lives while hovered (leave to close),
              // so users can hop sections / fire chat actions in sequence.
              <ChatNav
                key={item.key}
                item={item}
                active={section === "chat"}
                onNavigate={(k) => onNavigate?.(k)}
                ctrl={chatCtrl}
                tooltip={null}
              />
            ) : (
              <SidebarMenuItem key={item.key}>
                <SidebarMenuButton
                  isActive={section === item.key}
                  onClick={() => onNavigate?.(item.key)}
                >
                  <item.icon />
                  <span>{item.label}</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            )
          )}
        </SidebarMenu>
        <RecentDatasetsMenu
          recents={recents}
          selectionKey={selectionKey}
          onSelect={onSelectRecent}
        />
      </PopoverContent>
    </Popover>
  )
}

// Sidebar brand + collapse control. The logo (the dot-grid knowledge cube —
// the chat agent's avatar extruded to 3D, see WikiCubeIcon — + "Data wiki"
// wordmark) sits on the left and the collapse toggle is pinned to the right
// edge. Collapsing fully hides the sidebar (offcanvas), so re-expanding is
// handled by the persistent trigger in the top bar (see TopbarTrigger).
function SidebarBrand() {
  return (
    <div className="flex h-8 items-center gap-2 px-1.5 font-heading font-medium">
      <WikiCubeIcon className="size-8 shrink-0 text-primary" />
      <span className="truncate">Data wiki</span>
      <SidebarToggle
        label="Collapse sidebar"
        className="ml-auto text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
      />
    </div>
  )
}

// Persistent top-bar toggle. On desktop it appears only while the sidebar is
// collapsed (offcanvas fully hides the in-sidebar toggle), so it's the sole
// affordance to reopen. On mobile it's always shown (there's no desktop rail).
// It sits at the top-left, the same corner the panel scales out of, and reuses
// the same button style as the in-sidebar toggle (default ghost colors here).
// Chat has no top bar (it fills the full height), so when the sidebar is hidden
// its expand toggle would be unreachable — float a small one at the top-left of
// the chat area. Only rendered while the sidebar is collapsed / on mobile.
// The ONE collapsed-sidebar trigger, floated over the inset at the exact spot
// the in-flow header toggle used to occupy (header px-4/pt-4 put it at 16,16 =
// top-4 left-4). A SINGLE persistent instance for every section — chat
// included — so navigating to/from chat through the quick-nav popover no
// longer unmounts the trigger mid-click and takes the open popover with it
// (the old chat-specific floating toggle and the header's in-flow trigger were
// two different React elements; TopbarHeader keeps a same-size spacer so the
// breadcrumb can't slide under this overlay).
function CollapsedTriggerOverlay({
  section,
  onNavigate,
  chatCtrl,
  recents,
  selectionKey,
  onSelectRecent,
}) {
  const { state, isMobile } = useSidebar()
  if (!isMobile && state !== "collapsed") return null
  // The ghost variant's hover (bg-muted) is ~invisible on the top bar since
  // --muted ≈ --background in this theme; use a foreground tint so the hover
  // reads. dark:hover overrides the variant's dark:hover:bg-muted/50 too.
  return (
    <div className="absolute top-4 left-4 z-20">
      <CollapsedNavTrigger
        label="Expand sidebar"
        section={section}
        onNavigate={onNavigate}
        chatCtrl={chatCtrl}
        recents={recents}
        selectionKey={selectionKey}
        onSelectRecent={onSelectRecent}
        className="hover:bg-foreground/10 dark:hover:bg-foreground/10"
      />
    </div>
  )
}

// Top-bar breadcrumb. The active section is already highlighted in the sidebar
// while it's open, so the section label is redundant there — show it only when
// the sidebar is hidden (collapsed, or on mobile), where the breadcrumb is the
// sole section indicator. The dataset picker (on dataset-scoped views) always
// shows so the current dataset stays visible/switchable.
// Top bar. h-12 (48px) with pt-4 pushes the row's center to ~32px — the same
// line as the "Data wiki" brand row in the floating sidebar, so the two stay
// parallel. The collapse/expand toggle (shown only while the sidebar is hidden)
// sits at the left IN FLOW so the breadcrumb can never slide under it, even when
// the page is zoomed in and the content column runs full-width. A matching
// spacer on the right mirrors the toggle so the breadcrumb column stays centered
// on the same axis as the content below (max-w-6xl / max-w-7xl).
function TopbarHeader({
  centered,
  needsSelection,
  datasets,
  datasetsLoading,
  selectionKey,
  onSelectionChange,
}) {
  const { state, isMobile } = useSidebar()
  const showToggle = isMobile || state === "collapsed"
  // The strip carries EXACTLY one thing: the dataset picker. Views that take
  // no dataset get no header at all (the collapsed expand-toggle floats over
  // the content there, chat-style).
  if (!needsSelection) return null
  return (
    <header className="sticky top-0 z-10 flex h-12 shrink-0 items-center gap-1 bg-background/95 px-4 pt-4 supports-backdrop-filter:backdrop-blur">
      {/* Spacer where the toggle used to sit in flow — the trigger itself is
          now the persistent CollapsedTriggerOverlay floated at this exact spot,
          so the breadcrumb keeps its alignment without sliding under it. */}
      {showToggle ? <div aria-hidden="true" className="size-8 shrink-0" /> : null}
      {/* Match the content column below: centered views cap the breadcrumb at
          max-w-6xl, full-width views at max-w-7xl (px-1 lines it up with the
          cards' ring inset). mx-auto absorbs the free space so the column
          centers between the toggle and the mirror spacer. min-w-0 lets it
          shrink so a long dataset name truncates instead of overflowing. */}
      <div
        className={cn(
          "mx-auto flex w-full min-w-0 items-center px-1",
          centered ? "max-w-6xl" : "max-w-7xl"
        )}
      >
        <DatasetPicker
          datasets={datasets}
          selectionKey={selectionKey}
          onChange={onSelectionChange}
          loading={datasetsLoading}
        />
      </div>
      {/* Mirror the toggle's footprint (size-8) so the breadcrumb column stays
          centered on the true axis rather than shifting right by the toggle. */}
      {showToggle ? (
        <div className="size-8 shrink-0" aria-hidden="true" />
      ) : null}
    </header>
  )
}

// The Chat nav entry + its sub-controls. The Chat button behaves like any nav
// item; when chat is the ACTIVE section its controls (new chat, history) reveal
// as sub-items beneath it — driven by the shared chat controller so they operate
// the same conversation the chat page renders. The model is fixed (Opus 4.8);
// effort lives in the composer, so no model/effort controls here.
function ChatNav({ item, active, onNavigate, ctrl, tooltip = item.label }) {
  const { historyOpen, setHistoryOpen } = ctrl
  return (
    <SidebarMenuItem>
      <SidebarMenuButton
        isActive={active}
        tooltip={tooltip}
        onClick={() => onNavigate(item.key)}
      >
        <item.icon />
        <span>{item.label}</span>
      </SidebarMenuButton>

      {active ? (
        <SidebarMenuSub>
          <SidebarMenuSubItem>
            <SidebarMenuSubButton
              onClick={ctrl.startNewChat}
              className="cursor-pointer"
            >
              <MessageSquarePlusIcon />
              <span>New chat</span>
            </SidebarMenuSubButton>
          </SidebarMenuSubItem>
          <SidebarMenuSubItem>
            <SidebarMenuSubButton
              isActive={historyOpen}
              onClick={() => setHistoryOpen((v) => !v)}
              className="cursor-pointer"
            >
              <HistoryIcon />
              <span>History</span>
            </SidebarMenuSubButton>
          </SidebarMenuSubItem>
        </SidebarMenuSub>
      ) : null}
    </SidebarMenuItem>
  )
}

// The last few datasets this user opened (per-user MRU, see lib/
// recentDatasets.js) — rendered identically in the sidebar and the collapsed
// quick-nav popover, like recent chats in a chat app. Clicking one re-selects
// it; the caller decides which section to land on.
function RecentDatasetsMenu({ recents, selectionKey, onSelect }) {
  if (!recents?.length) return null
  return (
    <>
      <SidebarGroupLabel>Recent datasets</SidebarGroupLabel>
      <SidebarMenu>
        {recents.map((key) => (
          <SidebarMenuItem key={key}>
            <SidebarMenuButton
              isActive={selectionKey === key}
              onClick={() => onSelect(key)}
            >
              <HistoryIcon />
              <span>{key}</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        ))}
      </SidebarMenu>
    </>
  )
}

// Sections that live in the URL; anything else falls back to "domains".
const SECTION_KEYS = new Set(NAV.map((n) => n.key))

function Console({ auth, api }) {
  const [datasets, setDatasets] = useState([])
  // True only until the FIRST registry response lands — the picker shows a
  // skeleton then. Deliberately never reset: later reloads (onChanged after a
  // mapping edit) keep showing current data rather than flashing a skeleton.
  const [datasetsLoading, setDatasetsLoading] = useState(true)
  // The URL hash is the source of truth for section / dataset / open concept,
  // so browser back/forward navigate the app. See lib/route.js.
  const { route, push, replace } = useRouter()

  const section = SECTION_KEYS.has(route.section) ? route.section : "domains"
  const selectionKey = route.selectionKey
  // Browse's currently-open concept comes from the URL (Browse pushes updates).
  const routeConcept = section === "browse" ? route.concept : null

  const setSelectionKey = useCallback(
    (key) => push({ section, selectionKey: key, concept: null }),
    [push, section]
  )

  // Sidebar navigation: switch section, keeping the selected dataset. (buildHash
  // ignores selectionKey for the chat section — chat is not dataset-scoped.)
  const navigate = useCallback(
    (key) => push({ section: key, selectionKey, concept: null }),
    [push, selectionKey]
  )

  // Chat's conversation id lives in the URL (#/chat/<threadId>) so a chat is
  // linkable/resumable. replace() (not push()) so switching conversations within
  // chat doesn't spam the back-stack; ChatPanel drives this as the active
  // conversation changes, and reads route.threadId back to open a linked chat.
  const setChatThread = useCallback(
    (threadId) => replace({ section: "chat", threadId }),
    [replace]
  )

  // Shared chat control state (model/effort, new-chat, resume, history toggle),
  // lifted here so the sidebar sub-items (ChatNav) and the chat page (ChatPanel)
  // drive the SAME conversation. Reads/writes the #/chat/<threadId> URL.
  const chat = useChatController({
    urlThreadId: route.threadId,
    onThreadChange: setChatThread,
  })

  // Jump from the Graph view to the Browse view, opening a concept's doc.
  const openConceptInBrowse = useCallback(
    (conceptId) =>
      push({ section: "browse", selectionKey, concept: conceptId }),
    [push, selectionKey]
  )

  // Browse reports the concept it opened so the URL stays in sync (and Back
  // returns to the previously-open concept).
  const onBrowseConcept = useCallback(
    (conceptId) =>
      push({ section: "browse", selectionKey, concept: conceptId }),
    [push, selectionKey]
  )

  // Load the registry mappings for the sidebar dataset picker.
  const loadDatasets = useCallback(async () => {
    if (!api) return
    try {
      const doms = await api.listDomains()
      const list = Array.isArray(doms) ? doms : []
      setDatasets(list)
    } catch {
      // Non-fatal for the shell; the Domains view surfaces the error.
      setDatasets([])
    } finally {
      setDatasetsLoading(false)
    }
  }, [api])

  useEffect(() => {
    loadDatasets()
  }, [loadDatasets])

  // Normalize the URL once datasets load: default the section, and auto-select
  // the first dataset when the URL has none (or names an unknown one). Uses
  // replace() so it doesn't add a spurious history entry.
  useEffect(() => {
    if (!datasets.length) return
    // Chat is NOT dataset-scoped: its trailing URL segment is a conversation id,
    // not a dataset, so it never has a selectionKey. Skip normalization here — it
    // would rewrite #/chat/<threadId> to #/chat and strip the conversation id
    // (buildHash drops selectionKey/concept for chat). The chat controller owns
    // the threadId in the URL.
    if (route.section === "chat") return
    const known = datasets.some(
      (d) => `${d.data_domain}/${d.dataset}` === selectionKey
    )
    if (!SECTION_KEYS.has(route.section) || !known) {
      replace({
        section,
        selectionKey: known
          ? selectionKey
          : `${datasets[0].data_domain}/${datasets[0].dataset}`,
        // Preserve any deep-linked concept even when correcting the dataset —
        // Browse shows a graceful "not found" if it isn't in the new dataset.
        concept: routeConcept,
      })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [datasets, route.section, selectionKey])

  const selection = useMemo(() => {
    if (!selectionKey) return null
    const found = datasets.find(
      (d) => `${d.data_domain}/${d.dataset}` === selectionKey
    )
    if (found) return { data_domain: found.data_domain, dataset: found.dataset }
    const [data_domain, dataset] = selectionKey.split("/")
    return { data_domain, dataset }
  }, [selectionKey, datasets])

  const email = auth.user?.profile?.email
  const userSub = auth.user?.profile?.sub

  // Recent datasets (per-user MRU, localStorage). Recording keys off the URL's
  // selectionKey, so picker choices AND deep links both count as an "access".
  const [recentDatasets, setRecentDatasets] = useState(() =>
    loadRecentDatasets(userSub)
  )
  useEffect(() => {
    setRecentDatasets(loadRecentDatasets(userSub))
  }, [userSub])
  useEffect(() => {
    if (selectionKey) setRecentDatasets(pushRecentDataset(userSub, selectionKey))
  }, [selectionKey, userSub])
  // Opening a recent keeps a dataset-scoped section, else lands on Browse (the
  // natural reading view for "take me back to that dataset").
  const openRecentDataset = useCallback(
    (key) => {
      const scoped = NAV.find((n) => n.key === section)?.needsSelection
      push({
        section: scoped ? section : "browse",
        selectionKey: key,
        concept: null,
      })
    },
    [push, section]
  )
  // Show only datasets that still exist in the registry (stale/renamed entries
  // hide rather than 404). While the registry is still loading, show as-is.
  const visibleRecents = useMemo(() => {
    if (!datasets.length) return recentDatasets
    const known = new Set(datasets.map((d) => `${d.data_domain}/${d.dataset}`))
    return recentDatasets.filter((k) => known.has(k))
  }, [recentDatasets, datasets])

  const activeNav = NAV.find((n) => n.key === section) || NAV[0]
  // Browse/Graph fill the full inset width; the other views cap their cards at
  // max-w-6xl and center them. The breadcrumb sits in the same-width column so
  // it never runs wider than the content below it.
  const centered = section !== "browse" && section !== "graph"
  // Whether the top strip (dataset picker) renders: dataset-scoped sections,
  // except Browse, which hosts the picker inside its tree-pane header. Drives
  // both TopbarHeader and the content region's top padding below.
  const hasTopStrip = activeNav.needsSelection && section !== "browse"

  return (
    // Pin the shell to exactly the viewport height (the wrapper is min-h-svh by
    // default, which grows with content). h-svh + overflow-hidden gives the
    // flex/grid chain a definite height so Browse's cards fit the viewport and
    // scroll internally instead of expanding the page.
    <SidebarProvider className="h-svh overflow-hidden">
      <Sidebar collapsible="offcanvas" variant="floating">
        <SidebarHeader>
          <SidebarBrand />
        </SidebarHeader>
        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupLabel>Manage</SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {NAV.map((item) =>
                  item.key === "chat" ? (
                    <ChatNav
                      key={item.key}
                      item={item}
                      active={section === "chat"}
                      onNavigate={navigate}
                      ctrl={chat}
                    />
                  ) : (
                    <SidebarMenuItem key={item.key}>
                      <SidebarMenuButton
                        isActive={section === item.key}
                        tooltip={item.label}
                        onClick={() => navigate(item.key)}
                      >
                        <item.icon />
                        <span>{item.label}</span>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  )
                )}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
          {visibleRecents.length ? (
            <SidebarGroup>
              <SidebarGroupContent>
                <RecentDatasetsMenu
                  recents={visibleRecents}
                  selectionKey={selectionKey}
                  onSelect={openRecentDataset}
                />
              </SidebarGroupContent>
            </SidebarGroup>
          ) : null}
        </SidebarContent>
        <SidebarFooter>
          {email ? (
            <span className="truncate px-2 text-xs text-sidebar-foreground/70 group-data-[collapsible=icon]:hidden">
              {email}
            </span>
          ) : null}
          {/* Same SidebarMenuButton as the nav items, so hover/active/collapsed
              behavior is identical. */}
          <SidebarMenu>
            <ThemeToggle />
            <SidebarMenuItem>
              <SidebarMenuButton
                tooltip="Sign out"
                onClick={() => auth.signoutRedirect()}
              >
                <LogOutIcon />
                <span>Sign out</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarFooter>
      </Sidebar>

      {/* min-w-0: as a flex child of the sidebar wrapper, the inset defaults to
          min-width:auto and would grow to its widest content (e.g. a long
          unbroken line in Harvest's raw-status <pre>), pushing past the
          viewport. min-w-0 lets it shrink so inner overflow containers scroll. */}
      <SidebarInset className="relative min-w-0">
        <CollapsedTriggerOverlay
          section={section}
          onNavigate={navigate}
          chatCtrl={chat}
          recents={visibleRecents}
          selectionKey={selectionKey}
          onSelectRecent={openRecentDataset}
        />
        {/* Chat is a FULL-HEIGHT page: it fills the ENTIRE inset (no top bar), so
            the transcript + composer own the whole vertical layout — its controls
            live in the sidebar (ChatNav), not a header. The collapsed expand
            toggle is the shared CollapsedTriggerOverlay above. Every other
            section keeps the TopbarHeader (breadcrumb + dataset picker). */}
        {section === "chat" ? (
          <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
            <ChatPanel api={api} auth={auth} ctrl={chat} datasets={datasets} />
          </div>
        ) : (
          <>
            {/* The floating sidebar adds an 8px outer gap (p-2) above its header,
                so the brand sits at ~32px; TopbarHeader's pt-4 matches that. */}
            <TopbarHeader
              centered={centered}
              // Browse hosts the picker inside its tree-pane header instead,
              // so it gets no top strip at all — full height for the card.
              needsSelection={hasTopStrip}
              datasets={datasets}
              datasetsLoading={datasetsLoading}
              selectionKey={selectionKey}
              onSelectionChange={setSelectionKey}
            />
            {/* Content region fills the viewport below the header. Browse/Graph
                get the full width/height and manage their own internal scrolling;
                the other views stay centered (max-w-6xl) and scroll as a block.
                pb-2 (8px) lines the bottom edge up with the sidebar's p-2 gap —
                and stripless sections put their cards' TOP edge on the same 8px
                line as the sidebar: Browse's full-width wrapper has no vertical
                padding of its own so it gets pt-2 directly, while the centered
                views add p-1 ring room inside their scroll container, so pt-1
                outside + p-1 inside = the same 8px. Sections with the picker
                strip keep pt-4 as breathing room below it. */}
            <div
              className={cn(
                "flex min-h-0 flex-1 flex-col overflow-hidden px-4 pb-2",
                hasTopStrip ? "pt-4" : centered ? "pt-1" : "pt-2"
              )}
            >
              {section === "browse" ? (
                // Full-width views still cap + center (max-w-7xl) so they leave a
                // margin on both sides and line up under the top-bar toggle/picker.
                // px-1 gives the cards' ring/shadow room like the centered column.
                <div className="mx-auto flex min-h-0 w-full max-w-7xl flex-1 flex-col px-1">
                  <BrowseView
                    api={api}
                    selection={selection}
                    picker={
                      <DatasetPicker
                        datasets={datasets}
                        selectionKey={selectionKey}
                        onChange={setSelectionKey}
                        loading={datasetsLoading}
                      />
                    }
                    concept={routeConcept}
                    onConceptChange={onBrowseConcept}
                  />
                </div>
              ) : section === "graph" ? (
                <div className="mx-auto flex min-h-0 w-full max-w-7xl flex-1 flex-col px-1">
                  <GraphView
                    api={api}
                    selection={selection}
                    onOpenConcept={openConceptInBrowse}
                  />
                </div>
              ) : (
                // p-1 gives the cards' ring/shadow room so the vertical scroll
                // container doesn't clip them — including the first card's top edge,
                // which otherwise sits flush against the scroll container's top.
                <div className="mx-auto min-h-0 w-full max-w-6xl flex-1 overflow-y-auto p-1">
                  {section === "domains" && (
                    <DomainsView api={api} onChanged={loadDatasets} />
                  )}
                  {section === "mappings" && (
                    <MappingsView api={api} onChanged={loadDatasets} />
                  )}
                  {section === "context" && (
                    <ContextView api={api} selection={selection} />
                  )}
                  {section === "harvest" && (
                    <HarvestView api={api} selection={selection} />
                  )}
                  {section === "benchmark" && (
                    <BenchmarkView api={api} selection={selection} />
                  )}
                  {section === "credentials" && (
                    <CredentialsView api={api} email={email} />
                  )}
                </div>
              )}
            </div>
          </>
        )}
      </SidebarInset>
    </SidebarProvider>
  )
}

export function App() {
  const auth = useAuth()

  // One API client bound to the current ID token.
  const api = useMemo(
    () => makeApi(auth.user?.id_token),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [auth.user]
  )

  let body
  if (auth.isLoading) {
    body = (
      <div className="flex min-h-svh flex-col items-center justify-center gap-3">
        <Spinner />
        <p className="text-sm text-muted-foreground">Loading…</p>
      </div>
    )
  } else if (auth.error) {
    body = (
      <div className="flex min-h-svh items-center justify-center p-6">
        <Alert variant="destructive" className="max-w-md">
          <AlertTitle>Authentication error</AlertTitle>
          <AlertDescription>
            {auth.error.message}
            <Button
              variant="outline"
              size="sm"
              className="mt-3 w-fit"
              onClick={() => signInPreservingRoute(auth)}
            >
              <LogInIcon data-icon="inline-start" />
              Try again
            </Button>
          </AlertDescription>
        </Alert>
      </div>
    )
  } else if (!auth.isAuthenticated) {
    body = <LoginScreen onSignIn={() => signInPreservingRoute(auth)} />
  } else {
    body = <Console auth={auth} api={api} />
  }

  return (
    <>
      {body}
      <Toaster richColors position="bottom-right" />
    </>
  )
}

export default App
