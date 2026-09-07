# Bundles and freshness

A bundle is a versioned set of Markdown concepts for one dataset.

## Bundle structure

A typical bundle contains:

```text
index.md
datasets/<dataset>.md
tables/<table>.md
references/usage_guardrails.md
references/joins/
references/metrics/
references/enums/
references/named_sets/
references/glossary/
references/known_issues/
references/recipes/
references/computations/
```

Cross-dataset harvests can also add `external/<domain>/<dataset>/`. A full harvest removes that directory with other authored output.

Each concept has YAML frontmatter. Type, title, description, and timestamp are required.

Tags, source details, and other fields are optional. Their presence depends on the concept type.

Links form a graph across the bundle. Backlinks help agents discover related concepts without scanning every page.

## Published versions

The bundle bucket uses S3 Versioning. A completion marker records a run that reached the complete state.

Snapshot reconstruction uses the marker as a version boundary. S3 Files write-back is not strictly ordered, so this boundary is best-effort.

The console can compare versions and restore earlier content. A restore creates a new head version.

## Glue freshness

AWS Glue changes can trigger a scoped incremental harvest. The worker confirms that the catalog change is meaningful.

A nightly reconciliation can find source events that were missed.

The vector index reacts to each Markdown object change. It can update before the harvest writes its completion marker.

## Redshift freshness

Amazon Redshift does not use the Glue event path. Refresh Redshift datasets with manually started full harvests.

Use an external scheduler when you need scheduled Redshift refreshes.

## Derived indexes

The semantic index and cross-dataset signals are derived from bundle object events.

The repository does not include a full bucket-scan rebuild job. Restore or rewrite live objects to create new indexing events.

Published Markdown remains the durable source of truth.
