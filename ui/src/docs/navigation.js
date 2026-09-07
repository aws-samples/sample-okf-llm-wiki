export const DOC_SECTIONS = [
  {
    pages: [
      {
        title: "Data Wiki",
        route: "",
        source: "docs/guide/index.md",
      },
    ],
  },
  {
    label: "Getting started",
    pages: [
      {
        title: "Overview",
        route: "getting-started",
        source: "docs/guide/getting-started/index.md",
      },
      {
        title: "Prerequisites",
        route: "getting-started/prerequisites",
        source: "docs/guide/getting-started/prerequisites.md",
      },
      {
        title: "Deploy Data Wiki",
        route: "getting-started/deploy",
        source: "docs/guide/getting-started/deploy.md",
      },
      {
        title: "Run your first harvest",
        route: "getting-started/first-harvest",
        source: "docs/guide/getting-started/first-harvest.md",
      },
    ],
  },
  {
    label: "Core concepts",
    pages: [
      {
        title: "How Data Wiki works",
        route: "concepts/how-it-works",
        source: "docs/guide/concepts/how-it-works.md",
      },
      {
        title: "Architecture",
        route: "concepts/architecture",
        source: "docs/guide/concepts/architecture.md",
      },
      {
        title: "Bundles and freshness",
        route: "concepts/bundles-and-freshness",
        source: "docs/guide/concepts/bundles-and-freshness.md",
      },
    ],
  },
  {
    label: "User guide",
    pages: [
      {
        title: "Manage domains and datasets",
        route: "user-guide/domains-and-datasets",
        source: "docs/guide/user-guide/domains-and-datasets.md",
      },
      {
        title: "Add context documents",
        route: "user-guide/context-documents",
        source: "docs/guide/user-guide/context-documents.md",
      },
      {
        title: "Run and review harvests",
        route: "user-guide/harvests",
        source: "docs/guide/user-guide/harvests.md",
      },
      {
        title: "Browse and chat",
        route: "user-guide/browse-and-chat",
        source: "docs/guide/user-guide/browse-and-chat.md",
      },
      {
        title: "Connect an agent",
        route: "user-guide/connect-an-agent",
        source: "docs/guide/user-guide/connect-an-agent.md",
      },
      {
        title: "MCP tools",
        route: "user-guide/mcp-tools",
        source: "docs/guide/user-guide/mcp-tools.md",
      },
      {
        title: "Benchmark a wiki",
        route: "user-guide/benchmark",
        source: "docs/guide/user-guide/benchmark.md",
      },
    ],
  },
  {
    label: "Configuration",
    pages: [
      {
        title: "Configure data sources",
        route: "configuration/data-sources",
        source: "docs/guide/configuration/data-sources.md",
      },
      {
        title: "Use AWS Lake Formation",
        route: "configuration/lake-formation",
        source: "docs/guide/configuration/lake-formation.md",
      },
      {
        title: "Configure deployment options",
        route: "configuration/deployment",
        source: "docs/guide/configuration/deployment.md",
      },
    ],
  },
  {
    pages: [
      {
        title: "Troubleshooting",
        route: "troubleshooting",
        source: "docs/guide/troubleshooting.md",
      },
    ],
  },
]

export const DOC_PAGES = DOC_SECTIONS.flatMap((section) =>
  section.pages.map((page) => ({
    ...page,
    section: section.label || "",
  }))
)

export const DOC_ROUTES = DOC_PAGES.map((page) => page.route).filter(Boolean)
