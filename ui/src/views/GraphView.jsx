import { memo, useCallback, useEffect, useMemo, useState } from "react"
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlowProvider,
  useReactFlow,
} from "reactflow"
import "reactflow/dist/style.css"
import {
  BookMarkedIcon,
  BoxIcon,
  DatabaseIcon,
  ExternalLinkIcon,
  FocusIcon,
  NetworkIcon,
  Table2Icon,
  XIcon,
} from "lucide-react"

import { cn } from "@/lib/utils"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuLabel,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from "@/components/ui/context-menu"
import { Skeleton } from "@/components/ui/skeleton"

// Arrowhead marker. Color is set explicitly because RF11 markers don't inherit
// the edge stroke; --muted-foreground stays legible on light and dark.
const ARROW = {
  type: MarkerType.ArrowClosed,
  width: 16,
  height: 16,
  color: "var(--muted-foreground)",
}

// Map a concept's frontmatter `type` to an icon + accent color. Types are
// free-form, so we match on keywords and fall back to a generic concept style.
function nodeKind(type) {
  const t = (type || "").toLowerCase()
  if (t.includes("table")) return { icon: Table2Icon, accent: "var(--chart-1)" }
  if (t.includes("dataset") || t.includes("database"))
    return { icon: DatabaseIcon, accent: "var(--chart-2)" }
  if (t.includes("reference") || t.includes("join") || t.includes("metric"))
    return { icon: BookMarkedIcon, accent: "var(--chart-3)" }
  return { icon: BoxIcon, accent: "var(--chart-4)" }
}

function toRfNode(node) {
  return {
    id: node.id,
    type: "concept",
    data: { title: node.title || node.id, conceptId: node.id, type: node.type },
    position: { x: 0, y: 0 },
  }
}

// Full-graph layout: lay concept nodes out on a circle (the graph JSON carries
// no positions). Radius scales with node count so the wide cards don't overlap.
function circleLayout(rawNodes) {
  const nodes = rawNodes.map(toRfNode)
  const n = nodes.length
  const radius = Math.max(300, n * 72)
  return nodes.map((node, i) => {
    const angle = (2 * Math.PI * i) / Math.max(1, n)
    return {
      ...node,
      position: {
        x: radius + radius * Math.cos(angle),
        y: radius + radius * Math.sin(angle),
      },
    }
  })
}

// Focus layout: the focused node dead-center, its neighbors evenly on a ring.
function radialLayout(rawNodes, focusId) {
  const nodes = rawNodes.map(toRfNode)
  const ring = nodes.filter((n) => n.id !== focusId)
  const radius = Math.max(280, ring.length * 72)
  return nodes.map((n) => {
    if (n.id === focusId) {
      return { ...n, position: { x: radius, y: radius }, selected: true }
    }
    const i = ring.findIndex((r) => r.id === n.id)
    const angle = (2 * Math.PI * i) / Math.max(1, ring.length)
    return {
      ...n,
      position: {
        x: radius + radius * Math.cos(angle),
        y: radius + radius * Math.sin(angle),
      },
      selected: false,
    }
  })
}

// Collapse the directed edge list into one edge per unordered pair, tracking
// which directions exist. A pair linked BOTH ways gets arrowheads at both ends
// (markerStart + markerEnd); a one-way link gets a single arrowhead.
function collapseEdges(rawEdges) {
  const pairs = new Map()
  for (const e of rawEdges || []) {
    const key =
      e.source < e.target ? `${e.source} ${e.target}` : `${e.target} ${e.source}`
    let p = pairs.get(key)
    if (!p) {
      const [a, b] =
        e.source < e.target ? [e.source, e.target] : [e.target, e.source]
      p = { source: a, target: b, aToB: false, bToA: false }
      pairs.set(key, p)
    }
    if (e.source === p.source) p.aToB = true
    else p.bToA = true
  }
  return [...pairs.values()].map((p) => ({
    id: `e-${p.source} ${p.target}`,
    source: p.source,
    target: p.target,
    // markerEnd points at the target; markerStart adds a reverse arrow when the
    // pair is also linked the other way (both directions).
    markerEnd: p.aToB ? ARROW : undefined,
    markerStart: p.bToA ? ARROW : undefined,
  }))
}

// A modern concept node: accent bar + type icon, title, and the concept id.
const ConceptNode = memo(function ConceptNode({ data, selected }) {
  const { icon: Icon, accent } = nodeKind(data.type)
  return (
    <div
      className={cn(
        "group flex w-56 items-center gap-2.5 rounded-xl border bg-card px-3 py-2.5 text-left shadow-sm ring-1 ring-foreground/5 transition-shadow",
        "hover:shadow-md",
        selected && "ring-2 ring-primary"
      )}
      style={{ borderLeftColor: accent, borderLeftWidth: 3 }}
    >
      <Handle
        type="target"
        position={Position.Left}
        className="!size-1.5 !border-0 !bg-border"
      />
      <span
        className="flex size-7 shrink-0 items-center justify-center rounded-lg"
        style={{
          backgroundColor: `color-mix(in oklch, ${accent} 18%, transparent)`,
          color: accent,
        }}
      >
        <Icon className="size-4" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium text-foreground">
          {data.title}
        </div>
        <div className="truncate font-mono text-[11px] text-muted-foreground">
          {data.conceptId}
        </div>
      </div>
      <Handle
        type="source"
        position={Position.Right}
        className="!size-1.5 !border-0 !bg-border"
      />
    </div>
  )
})

const NODE_TYPES = { concept: ConceptNode }

// The link graph for a harvested bundle. Its own page (was a tab under Browse)
// so it can use the full viewport height. `onOpenConcept` jumps to the Browse
// view focused on a concept's doc ("go to file").
export default function GraphView({ api, selection, onOpenConcept }) {
  const domain = selection?.data_domain
  const dataset = selection?.dataset
  const hasSelection = Boolean(domain && dataset)

  if (!hasSelection) {
    return (
      <Alert>
        <NetworkIcon />
        <AlertTitle>Select a dataset first</AlertTitle>
        <AlertDescription>
          Pick a dataset from the sidebar to view its concept link graph.
        </AlertDescription>
      </Alert>
    )
  }

  return (
    <ReactFlowProvider>
      <GraphPane
        api={api}
        domain={domain}
        dataset={dataset}
        onOpenConcept={onOpenConcept}
      />
    </ReactFlowProvider>
  )
}

function GraphPane({ api, domain, dataset, onOpenConcept }) {
  const [graph, setGraph] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  // The concept id the graph is focused on (shows only it + direct neighbors),
  // or null for the full graph.
  const [focusId, setFocusId] = useState(null)
  const { fitView } = useReactFlow()

  useEffect(() => {
    let alive = true
    setLoading(true)
    setError(null)
    setGraph(null)
    setFocusId(null)
    api
      .bundleGraph(domain, dataset)
      .then((g) => {
        if (alive) setGraph(g)
      })
      .catch((e) => {
        if (alive) setError(e.message || String(e))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [api, domain, dataset])

  // Adjacency (undirected) so "focus" can find every concept directly linked to
  // the focused node, regardless of link direction.
  const neighbors = useMemo(() => {
    const m = new Map()
    for (const e of graph?.edges || []) {
      if (!m.has(e.source)) m.set(e.source, new Set())
      if (!m.has(e.target)) m.set(e.target, new Set())
      m.get(e.source).add(e.target)
      m.get(e.target).add(e.source)
    }
    return m
  }, [graph])

  // The set of node ids visible under the current focus: the focused node plus
  // its direct neighbors. Null when not focused (everything visible).
  const focusSet = useMemo(() => {
    if (!focusId) return null
    const set = new Set([focusId])
    for (const n of neighbors.get(focusId) || []) set.add(n)
    return set
  }, [focusId, neighbors])

  // One edge per unordered concept pair (with per-direction arrowheads).
  const allEdges = useMemo(() => collapseEdges(graph?.edges), [graph])

  const edges = useMemo(() => {
    if (!focusId) return allEdges
    // In focus mode show ONLY the focused node's own relationships (not links
    // between its neighbors) so the view is the focused node in context.
    return allEdges.filter((e) => e.source === focusId || e.target === focusId)
  }, [allEdges, focusId])

  const nodes = useMemo(() => {
    if (!graph?.nodes) return []
    if (focusSet) {
      // Focused: focused node dead-center, neighbors on a ring around it.
      const subset = graph.nodes.filter((n) => focusSet.has(n.id))
      return radialLayout(subset, focusId)
    }
    // Full graph: circle layout (unchanged default view).
    return circleLayout(graph.nodes)
  }, [graph, focusSet, focusId])

  // Re-fit the viewport whenever the layout (focus or full) changes.
  useEffect(() => {
    if (loading || !graph) return
    const id = window.requestAnimationFrame(() =>
      fitView({ padding: 0.2, duration: 400 })
    )
    return () => window.cancelAnimationFrame(id)
  }, [focusId, loading, graph, fitView])

  if (error) {
    return (
      <Card className="min-h-0 flex-1 gap-0 py-0">
        <GraphHeader graph={null} focusId={null} onClearFocus={() => {}} />
        <CardContent className="p-4">
          <Alert variant="destructive">
            <AlertTitle>Failed to load graph</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        </CardContent>
      </Card>
    )
  }

  if (loading) {
    return (
      <Card className="min-h-0 flex-1 gap-0 py-0">
        <GraphHeader graph={null} focusId={null} onClearFocus={() => {}} />
        <CardContent className="min-h-0 flex-1 p-0">
          <Skeleton className="size-full" />
        </CardContent>
      </Card>
    )
  }

  if (!graph || (graph.nodes?.length || 0) === 0) {
    return (
      <Card className="min-h-0 flex-1 gap-0 py-0">
        <GraphHeader graph={graph} focusId={null} onClearFocus={() => {}} />
        <CardContent className="p-4">
          <Alert>
            <NetworkIcon />
            <AlertTitle>No graph yet</AlertTitle>
            <AlertDescription>
              Run a harvest to populate concepts and links for this dataset.
            </AlertDescription>
          </Alert>
        </CardContent>
      </Card>
    )
  }

  return (
    <Card className="min-h-0 flex-1 gap-0 py-0">
      <GraphHeader
        graph={graph}
        focusId={focusId}
        onClearFocus={() => setFocusId(null)}
      />
      <CardContent className="min-h-0 flex-1 p-0">
        <ConceptGraphCanvas
          nodes={nodes}
          edges={edges}
          focusId={focusId}
          onFocus={setFocusId}
          onClearFocus={() => setFocusId(null)}
          onOpenConcept={onOpenConcept}
        />
      </CardContent>
    </Card>
  )
}

function GraphHeader({ graph, focusId, onClearFocus }) {
  return (
    <CardHeader className="border-b px-4 py-3">
      <CardTitle className="flex items-center gap-2 text-sm">
        <NetworkIcon className="size-4" />
        Link graph
      </CardTitle>
      <CardDescription className="flex flex-wrap items-center gap-2">
        {graph ? (
          <>
            <Badge variant="outline">{graph.nodes?.length || 0} nodes</Badge>
            <Badge variant="outline">{graph.edges?.length || 0} edges</Badge>
            <span className="text-muted-foreground">
              · click a node to focus · right-click for actions
            </span>
          </>
        ) : (
          "Concepts and their resolved links."
        )}
      </CardDescription>
      {focusId ? (
        <div className="col-start-2 row-span-2 row-start-1 flex items-center gap-2 self-start justify-self-end">
          <Badge variant="secondary" className="max-w-[16rem] truncate font-mono">
            <FocusIcon data-icon="inline-start" />
            {focusId}
          </Badge>
          <Button variant="outline" size="sm" onClick={onClearFocus}>
            <XIcon data-icon="inline-start" />
            Clear focus
          </Button>
        </div>
      ) : null}
    </CardHeader>
  )
}

// The React Flow canvas. Each node is wrapped in a shadcn ContextMenu so a
// right-click offers "Open file" and "Focus" (and "Clear focus" when active).
function ConceptGraphCanvas({
  nodes,
  edges,
  focusId,
  onFocus,
  onClearFocus,
  onOpenConcept,
}) {
  // Which node's context menu is open (React Flow renders one canvas, so we
  // track the target node and render a single ContextMenu around the surface).
  const [menuNode, setMenuNode] = useState(null)

  // id -> node data, so the capture handler can resolve a title from the DOM id.
  const nodeMeta = useMemo(() => {
    const m = new Map()
    for (const n of nodes) m.set(n.id, n.data)
    return m
  }, [nodes])

  // Determine the right-clicked node in the CAPTURE phase — this runs before
  // Radix's ContextMenuTrigger opens the menu (its listener is on bubble), so
  // menuNode is set before the menu content renders.
  const onContextMenuCapture = useCallback(
    (event) => {
      const el = event.target?.closest?.(".react-flow__node")
      const id = el?.dataset?.id
      if (id) setMenuNode({ id, title: nodeMeta.get(id)?.title })
      else setMenuNode(null) // right-click on empty canvas
    },
    [nodeMeta]
  )

  const onNodeClick = useCallback((_event, node) => onFocus(node.id), [onFocus])

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <div
          className="okf-graph size-full"
          onContextMenuCapture={onContextMenuCapture}
        >
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={NODE_TYPES}
            fitView
            minZoom={0.1}
            nodesConnectable={false}
            nodesDraggable
            onNodeClick={onNodeClick}
            proOptions={{ hideAttribution: true }}
          >
            <Background variant={BackgroundVariant.Dots} gap={20} size={1} />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent className="w-52">
        {menuNode ? (
          <>
            <ContextMenuLabel className="truncate">
              {menuNode.title || menuNode.id}
            </ContextMenuLabel>
            <ContextMenuSeparator />
            <ContextMenuItem onSelect={() => onOpenConcept?.(menuNode.id)}>
              <ExternalLinkIcon data-icon="inline-start" />
              Open file
            </ContextMenuItem>
            <ContextMenuItem
              onSelect={() => onFocus(menuNode.id)}
              disabled={focusId === menuNode.id}
            >
              <FocusIcon data-icon="inline-start" />
              Focus on node
            </ContextMenuItem>
            {focusId ? (
              <>
                <ContextMenuSeparator />
                <ContextMenuItem onSelect={onClearFocus}>
                  <XIcon data-icon="inline-start" />
                  Clear focus
                </ContextMenuItem>
              </>
            ) : null}
          </>
        ) : (
          <ContextMenuItem disabled>Right-click a node</ContextMenuItem>
        )}
      </ContextMenuContent>
    </ContextMenu>
  )
}
