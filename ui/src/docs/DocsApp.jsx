import {
  ArrowLeftIcon,
  ArrowRightIcon,
  ArrowUpIcon,
  MenuIcon,
  MoonIcon,
  SearchIcon,
  SunIcon,
  XIcon,
} from "lucide-react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import { WikiCubeIcon } from "@/components/WikiCubeIcon"
import { useTheme } from "@/components/theme-provider"
import { Button } from "@/components/ui/button"
import { CopyButton } from "@/components/ui/copy-button"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
} from "@/components/ui/sidebar"
import { TooltipProvider } from "@/components/ui/tooltip"
import { DOC_BY_ROUTE, DOCS } from "@/docs/content"
import {
  DocsMarkdown,
  headingSlug,
  normalizeDocsMarkdown,
} from "@/docs/DocsMarkdown"
import { DOC_PAGES, DOC_SECTIONS } from "@/docs/navigation"
import { cn } from "@/lib/utils"

const BASE_URL = import.meta.env.BASE_URL.endsWith("/")
  ? import.meta.env.BASE_URL
  : `${import.meta.env.BASE_URL}/`

function routeFromLocation() {
  let pathname = decodeURI(window.location.pathname)
  if (BASE_URL !== "/" && pathname.startsWith(BASE_URL)) {
    pathname = pathname.slice(BASE_URL.length)
  }
  return pathname.replace(/^\/+|\/+$/g, "")
}

function hrefFor(route = "", hash = "") {
  return `${BASE_URL}${route ? `${route}/` : ""}${hash}`
}

function extractHeadings(markdown) {
  const headings = []
  let inFence = false

  for (const line of normalizeDocsMarkdown(markdown).split("\n")) {
    if (/^```/.test(line.trim())) {
      inFence = !inFence
      continue
    }
    if (inFence) continue

    const match = line.match(/^(#{2,3})\s+(.+?)\s*#*\s*$/)
    if (!match) continue
    const text = match[2].replace(/[*_`[\]]/g, "").trim()
    headings.push({
      depth: match[1].length,
      id: headingSlug(text),
      text,
    })
  }

  return headings
}

function searchableText(markdown) {
  return markdown
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/!\[[^\]]*]\([^)]*\)/g, " ")
    .replace(/\[([^\]]+)]\([^)]*\)/g, "$1")
    .replace(/[#>*_`|:[\]()-]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase()
}

const SEARCH_INDEX = DOCS.map((page) => ({
  ...page,
  haystack:
    `${page.title} ${page.section} ${searchableText(page.markdown)}`.toLowerCase(),
}))

function searchDocs(query) {
  const terms = query.toLowerCase().trim().split(/\s+/).filter(Boolean)
  if (terms.length === 0) return []

  return SEARCH_INDEX.filter((page) =>
    terms.every((term) => page.haystack.includes(term))
  )
    .map((page) => {
      const title = page.title.toLowerCase()
      const score = terms.reduce(
        (total, term) =>
          total +
          (title === term ? 8 : 0) +
          (title.startsWith(term) ? 4 : 0) +
          (title.includes(term) ? 2 : 0),
        0
      )
      return { page, score }
    })
    .sort((left, right) => right.score - left.score)
    .slice(0, 8)
    .map(({ page }) => page)
}

function Brand({ sidebar = false }) {
  return (
    <a
      href={hrefFor("")}
      className={cn(
        "flex min-w-0 items-center gap-2 rounded-md outline-none focus-visible:ring-2",
        sidebar
          ? "text-sidebar-foreground focus-visible:ring-sidebar-ring"
          : "text-foreground focus-visible:ring-ring"
      )}
    >
      <WikiCubeIcon
        className={cn(
          "size-6 shrink-0",
          sidebar ? "text-sidebar-primary" : "text-primary"
        )}
      />
      <span className="truncate text-sm font-semibold">Data Wiki</span>
      <span
        className={cn(
          "text-xs font-medium",
          sidebar ? "text-sidebar-foreground/65" : "text-muted-foreground"
        )}
      >
        Docs
      </span>
    </a>
  )
}

function DocsTopBar({
  onSearchSelect,
  onOpenMobileNavigation,
  onOpenMobileSearch,
}) {
  return (
    <header className="docs-topbar flex h-12 shrink-0 items-center gap-2 px-3">
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        className="md:hidden"
        aria-label="Open documentation navigation"
        onClick={onOpenMobileNavigation}
      >
        <MenuIcon data-icon="inline-start" />
      </Button>
      <div className="min-w-0 md:hidden">
        <Brand />
      </div>
      <div className="ml-auto flex items-center gap-1.5">
        <ThemeButton />
        <DesktopSearch onSelect={onSearchSelect} />
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          className="sm:hidden"
          aria-label="Search documentation"
          onClick={onOpenMobileSearch}
        >
          <SearchIcon data-icon="inline-start" />
        </Button>
      </div>
    </header>
  )
}

function DocsNavLink({ page, active, onNavigate }) {
  return (
    <SidebarMenuItem>
      <SidebarMenuButton asChild isActive={active} tooltip={page.title}>
        <a
          href={hrefFor(page.route)}
          onClick={(event) => onNavigate(event, page.route)}
        >
          <span>{page.title}</span>
        </a>
      </SidebarMenuButton>
    </SidebarMenuItem>
  )
}

function DocsNavigation({ page, onNavigate }) {
  return DOC_SECTIONS.map((section, sectionIndex) => (
    <SidebarGroup
      key={section.label || `root-${sectionIndex}`}
      className={cn(!section.label && "pt-3")}
    >
      {section.label ? (
        <SidebarGroupLabel>{section.label}</SidebarGroupLabel>
      ) : null}
      <SidebarGroupContent>
        <SidebarMenu>
          {section.pages.map((candidate) => (
            <DocsNavLink
              key={candidate.route}
              page={candidate}
              active={candidate.route === page.route}
              onNavigate={onNavigate}
            />
          ))}
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  ))
}

function SearchResults({ query, onSelect, className }) {
  const results = useMemo(() => searchDocs(query), [query])

  if (!query.trim()) return null
  return (
    <div
      className={cn(
        "overflow-hidden rounded-lg border border-edge bg-popover p-1 text-popover-foreground shadow-lg",
        className
      )}
    >
      {results.length ? (
        results.map((page) => (
          <Button
            type="button"
            key={page.route}
            variant="ghost"
            className="h-auto w-full justify-start px-2.5 py-2 text-left whitespace-normal"
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => onSelect(page.route)}
          >
            <span className="flex min-w-0 flex-col items-start">
              <span className="text-sm font-medium">{page.title}</span>
              {page.section ? (
                <span className="text-xs text-muted-foreground">
                  {page.section}
                </span>
              ) : null}
            </span>
          </Button>
        ))
      ) : (
        <p className="px-2.5 py-3 text-sm text-muted-foreground">
          No matching documentation.
        </p>
      )}
    </div>
  )
}

function DesktopSearch({ onSelect }) {
  const [query, setQuery] = useState("")
  const [focused, setFocused] = useState(false)

  return (
    <div className="relative hidden w-56 sm:block lg:w-64">
      <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
      <Input
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        onKeyDown={(event) => {
          if (event.key !== "Enter") return
          const [first] = searchDocs(query)
          if (first) onSelect(first.route)
        }}
        className="bg-card pl-8"
        placeholder="Search documentation"
        aria-label="Search documentation"
      />
      {focused ? (
        <SearchResults
          query={query}
          onSelect={(route) => {
            onSelect(route)
            setQuery("")
          }}
          className="absolute top-[calc(100%+0.4rem)] right-0 z-50 w-80"
        />
      ) : null}
    </div>
  )
}

function MobileSearch({ open, onOpenChange, onSelect }) {
  const [query, setQuery] = useState("")
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next)
        if (!next) setQuery("")
      }}
    >
      <DialogContent
        className="top-16 block max-h-[calc(100svh-5rem)] -translate-y-0 overflow-y-auto sm:max-w-lg"
        showCloseButton={false}
      >
        <DialogHeader className="sr-only">
          <DialogTitle>Search documentation</DialogTitle>
        </DialogHeader>
        <div className="relative">
          <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            autoFocus
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            className="pl-8"
            placeholder="Search documentation"
          />
        </div>
        <SearchResults
          query={query}
          className="mt-3 border-0 shadow-none"
          onSelect={(route) => {
            onSelect(route)
            onOpenChange(false)
          }}
        />
      </DialogContent>
    </Dialog>
  )
}

function MobileNavigation({ open, onOpenChange, page, onNavigate }) {
  const navigateAndClose = (event, route) => {
    const plainClick =
      event.button === 0 &&
      !event.metaKey &&
      !event.ctrlKey &&
      !event.shiftKey &&
      !event.altKey
    onNavigate(event, route)
    if (plainClick) onOpenChange(false)
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="left"
        showCloseButton={false}
        className="docs-sidebar w-[18rem] border-sidebar-border bg-sidebar p-0 text-sidebar-foreground"
      >
        <SheetHeader className="sr-only">
          <SheetTitle>Data Wiki documentation navigation</SheetTitle>
        </SheetHeader>
        <div className="flex h-12 shrink-0 items-center px-4">
          <Brand sidebar />
          <SheetClose asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              className="ml-auto text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
              aria-label="Close documentation navigation"
            >
              <XIcon data-icon="inline-start" />
            </Button>
          </SheetClose>
        </div>
        <div className="okf-thin-scroll min-h-0 flex-1 overflow-y-auto">
          <DocsNavigation page={page} onNavigate={navigateAndClose} />
        </div>
      </SheetContent>
    </Sheet>
  )
}

function ThemeButton() {
  const { resolvedTheme, setTheme } = useTheme()
  const dark = resolvedTheme === "dark"

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-sm"
      aria-label={dark ? "Use light mode" : "Use dark mode"}
      title={dark ? "Use light mode" : "Use dark mode"}
      onClick={() => setTheme(dark ? "light" : "dark")}
    >
      {dark ? (
        <SunIcon data-icon="inline-start" />
      ) : (
        <MoonIcon data-icon="inline-start" />
      )}
    </Button>
  )
}

function PageFooter({ page, onNavigate }) {
  const index = DOC_PAGES.findIndex(
    (candidate) => candidate.route === page.route
  )
  const previous = DOC_PAGES[index - 1]
  const next = DOC_PAGES[index + 1]

  if (!previous && !next) return null
  return (
    <nav
      className="mt-12 flex flex-col gap-1 sm:flex-row sm:items-stretch sm:justify-between"
      aria-label="Documentation pages"
    >
      {previous ? (
        <a
          href={hrefFor(previous.route)}
          onClick={(event) => onNavigate(event, previous.route)}
          className="group flex min-w-0 items-center gap-3 rounded-lg px-3 py-2.5 text-left transition-colors outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring sm:max-w-[48%]"
        >
          <ArrowLeftIcon className="size-4 shrink-0 text-muted-foreground transition-transform group-hover:-translate-x-0.5" />
          <span className="min-w-0">
            <span className="block text-xs text-muted-foreground">
              Previous
            </span>
            <span className="block truncate text-sm font-medium text-foreground">
              {previous.title}
            </span>
          </span>
        </a>
      ) : null}
      {next ? (
        <a
          href={hrefFor(next.route)}
          onClick={(event) => onNavigate(event, next.route)}
          className="group ml-auto flex min-w-0 items-center justify-end gap-3 rounded-lg px-3 py-2.5 text-right transition-colors outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring sm:max-w-[48%]"
        >
          <span className="min-w-0">
            <span className="block text-xs text-muted-foreground">Next</span>
            <span className="block truncate text-sm font-medium text-foreground">
              {next.title}
            </span>
          </span>
          <ArrowRightIcon className="size-4 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5" />
        </a>
      ) : null}
    </nav>
  )
}

export function DocsApp() {
  const [route, setRoute] = useState(routeFromLocation)
  const [hash, setHash] = useState(window.location.hash)
  const [mobileNavigationOpen, setMobileNavigationOpen] = useState(false)
  const [mobileSearchOpen, setMobileSearchOpen] = useState(false)
  const [showTop, setShowTop] = useState(false)
  const scrollRef = useRef(null)
  const page = DOC_BY_ROUTE.get(route) || DOC_BY_ROUTE.get("")
  const headings = useMemo(
    () => extractHeadings(page.markdown),
    [page.markdown]
  )
  const [activeHeading, setActiveHeading] = useState(headings[0]?.id || "")

  const navigate = useCallback(
    (event, nextRoute = null, nextHash = "") => {
      if (
        event &&
        (event.button !== 0 ||
          event.metaKey ||
          event.ctrlKey ||
          event.shiftKey ||
          event.altKey)
      ) {
        return
      }
      event?.preventDefault()
      const targetRoute = nextRoute === null ? route : nextRoute
      window.history.pushState({}, "", hrefFor(targetRoute, nextHash))
      setRoute(targetRoute)
      setHash(nextHash)
    },
    [route]
  )

  const selectSearchResult = useCallback(
    (nextRoute) => navigate(null, nextRoute),
    [navigate]
  )

  useEffect(() => {
    const onPopState = () => {
      setRoute(routeFromLocation())
      setHash(window.location.hash)
    }
    window.addEventListener("popstate", onPopState)
    return () => window.removeEventListener("popstate", onPopState)
  }, [])

  useEffect(() => {
    document.title = `${page.title} · Data Wiki Docs`
    const frame = requestAnimationFrame(() => {
      if (hash) {
        document
          .getElementById(hash.slice(1))
          ?.scrollIntoView({ block: "start" })
      } else {
        scrollRef.current?.scrollTo({ top: 0 })
      }
    })
    return () => cancelAnimationFrame(frame)
  }, [page, hash])

  useEffect(() => {
    const root = scrollRef.current
    if (!root || headings.length === 0) {
      setActiveHeading("")
      return undefined
    }

    const updateActiveHeading = () => {
      const rootTop = root.getBoundingClientRect().top
      let next = headings[0].id
      for (const heading of headings) {
        const element = document.getElementById(heading.id)
        if (!element) continue
        if (element.getBoundingClientRect().top - rootTop <= 96) {
          next = heading.id
        } else {
          break
        }
      }
      setActiveHeading((current) => (current === next ? current : next))
    }

    const frame = requestAnimationFrame(updateActiveHeading)
    root.addEventListener("scroll", updateActiveHeading, { passive: true })
    return () => {
      cancelAnimationFrame(frame)
      root.removeEventListener("scroll", updateActiveHeading)
    }
  }, [headings, page.route])

  return (
    <TooltipProvider>
      <SidebarProvider
        className="h-svh max-h-svh overflow-hidden"
        style={{ "--sidebar-width": "16.5rem" }}
      >
        <Sidebar
          collapsible="none"
          className="docs-sidebar hidden shrink-0 md:flex"
        >
          <SidebarContent className="okf-thin-scroll">
            <DocsNavigation page={page} onNavigate={navigate} />
          </SidebarContent>
        </Sidebar>

        <SidebarInset className="min-w-0 overflow-hidden">
          <DocsTopBar
            onSearchSelect={selectSearchResult}
            onOpenMobileNavigation={() => setMobileNavigationOpen(true)}
            onOpenMobileSearch={() => setMobileSearchOpen(true)}
          />

          <div className="flex min-h-0 flex-1">
            <main
              ref={scrollRef}
              className="okf-thin-scroll min-h-0 min-w-0 flex-1 overflow-y-auto"
              onScroll={(event) =>
                setShowTop(event.currentTarget.scrollTop > 500)
              }
            >
              <article className="mx-auto w-full max-w-3xl px-5 py-6 sm:px-6 sm:pt-4">
                <DocsMarkdown
                  markdown={page.markdown}
                  source={page.source}
                  makeHref={hrefFor}
                  navigate={navigate}
                />
                <PageFooter page={page} onNavigate={navigate} />
              </article>
            </main>

            <aside className="okf-thin-scroll hidden w-56 shrink-0 overflow-y-auto px-5 py-5 xl:block">
              {headings.length ? (
                <>
                  <p className="mb-3 text-xs font-medium text-muted-foreground">
                    On this page
                  </p>
                  <nav
                    className="docs-page-nav flex flex-col border-l border-border"
                    aria-label="On this page"
                  >
                    {headings.map((heading) => {
                      const active = activeHeading === heading.id
                      return (
                        <a
                          key={heading.id}
                          href={`#${heading.id}`}
                          aria-current={active ? "location" : undefined}
                          onClick={(event) =>
                            navigate(event, null, `#${heading.id}`)
                          }
                          className={cn(
                            "relative py-1.5 pr-1 pl-4 text-xs leading-5 text-muted-foreground transition-colors before:absolute before:inset-y-1 before:-left-px before:w-px before:rounded-full before:bg-transparent before:content-[''] hover:text-foreground",
                            heading.depth === 3 && "pl-6",
                            active &&
                              "font-medium text-primary before:w-0.5 before:bg-primary"
                          )}
                        >
                          {heading.text}
                        </a>
                      )
                    })}
                  </nav>
                </>
              ) : null}
              <CopyButton
                text={page.markdown}
                label="Copy page"
                showLabel
                variant="outline"
                className={cn(headings.length && "mt-5")}
              />
            </aside>
          </div>

          {showTop ? (
            <Button
              type="button"
              variant="secondary"
              size="sm"
              className="absolute right-4 bottom-4 z-20 shadow-md"
              onClick={() =>
                scrollRef.current?.scrollTo({ top: 0, behavior: "smooth" })
              }
            >
              <ArrowUpIcon data-icon="inline-start" />
              Back to top
            </Button>
          ) : null}
        </SidebarInset>

        <MobileNavigation
          open={mobileNavigationOpen}
          onOpenChange={setMobileNavigationOpen}
          page={page}
          onNavigate={navigate}
        />
        <MobileSearch
          open={mobileSearchOpen}
          onOpenChange={setMobileSearchOpen}
          onSelect={selectSearchResult}
        />
      </SidebarProvider>
    </TooltipProvider>
  )
}
