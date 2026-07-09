# Quick Start Guide: Using the Console

The Data Wiki console is where you turn a Glue database into a knowledge bundle: register a dataset, optionally enrich it with your own docs, run a harvest, and review the result. This guide walks the full workflow.

Sign in to the console (the CloudFront URL from `./scripts/deploy.sh summary`) with your Cognito credentials to get started.

---

## 1. Register a Dataset

A **domain mapping** links a Glue database to a Data Wiki data domain and dataset — the `data domain → dataset → table` hierarchy bundles are organized around.

1. Go to **Domains** in the sidebar.
2. Click **New mapping**.
3. Pick a **Glue database** and enter a **data domain** (e.g. `sales`).
4. Click **Save mapping**.

The dataset name is taken from the Glue database name automatically — the harvest runtime queries Glue by that name, so they must match. The new mapping becomes selectable in the dataset picker at the top of the other views.

> **Warning:** Deleting a mapping permanently removes everything it owns — the registry entry, the harvested bundle in S3, its vectors, and its harvest history. The underlying Glue data is not touched.

## 2. Add Context Docs (Optional)

The Glue schema says *what* columns exist, not what they *mean*. Upload your own source docs so the harvest agent grounds the bundle in your organization's knowledge — this is the biggest lever on bundle quality.

1. Select the dataset in the top picker.
2. Go to **Context Docs** and click **Upload**.
3. Add PDF, Word (`.docx`), PowerPoint (`.pptx`), Excel (`.xlsx`), XML, Markdown, text, or CSV — data dictionaries, DDL, join notes, business rules, and metric definitions are ideal.

The harvest agent reads text formats directly and extracts the binary ones (PDF/Word/PowerPoint/Excel) in a sandbox, so you can upload your source docs in their original format — no need to convert to text first.

Context docs persist across incremental harvests, so upload them **before** the first harvest. They aren't served to agents directly; only the harvested markdown is.

## 3. Run a Harvest

A **harvest** is the induction process: an AI agent (on Bedrock AgentCore) reads the Glue catalog, samples data via Athena, reads your context docs, and authors the markdown bundle — validating each claim against live data.

1. Select the dataset and go to **Harvest**.
2. Click **Start full harvest**.
3. Watch the status; the view polls every ~4 seconds.

| Status     | Meaning                                                    |
| ---------- | ---------------------------------------------------------- |
| `queued`   | Accepted, about to start.                                  |
| `running`  | The agent is crawling, authoring, and reviewing.           |
| `complete` | Finished and the bundle was published.                     |
| `failed`   | Stopped with an error — see the **Detail** field.          |

**Bundle: ready** means consumers can safely read it; publishing is atomic, so you never serve a half-written bundle. A run can take minutes to a couple of hours depending on catalog size, and only one harvest per dataset runs at a time (a lease prevents conflicts).

You don't trigger **incremental** harvests manually — Data Wiki re-harvests the changed table automatically when it detects a Glue table change, and the semantic index stays in sync on every write.

> **Tip:** If a harvest fails or behaves oddly, the full agent trajectory is traced into the CloudWatch GenAI Observability console. See the [Observability section of the Architecture doc](../docs/ARCHITECTURE.md#observability).

## 4. Browse the Bundle

Once a harvest is `complete`, review what the agent produced — the same content agents consume over MCP.

- **Browse** — navigate the concept tree (dataset overview, tables, joins, metrics, known issues) and read rendered markdown. Cross-references between concepts are clickable, and the open concept is reflected in the URL for deep-linking.
- **Graph** — visualize the bundle as a network of concepts and their cross-references. Use it to spot orphaned tables or confirm the join structure matches your mental model.

When reviewing, check that each table's grain is right, joins are real (and none obvious are missing), and known issues capture sentinel values and gotchas. To fix a thin or wrong concept, upload a clarifying context doc and re-harvest rather than editing by hand.

---

## Next Steps

- **Give agents access to your bundles** → [Connect an Agent (MCP)](./connect-an-agent.md)
- **Understand the system** → [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md)
