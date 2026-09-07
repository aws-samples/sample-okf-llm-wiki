# How Data Wiki works

Data Wiki turns source metadata and evidence into a linked knowledge bundle.

## Authoring flow

1. An operator registers a dataset.
2. The harvest runtime snapshots source metadata.
3. The runtime reads optional context documents.
4. Author agents inspect tables and sample data.
5. Reviewer agents can check important claims during a full harvest.
6. The runtime writes authored pages directly to the live bundle.
7. Lint and review findings are reported.
8. The runtime writes a completion marker when the run reaches complete.

Bundle pages use Markdown with YAML frontmatter. Links connect tables, metrics, joins, references, and known issues.

Review and lint findings do not block publication. A failed or cancelled full harvest can leave partial content in the live bundle.

The indexing worker reacts to each Markdown object change. Search can update while a harvest is still running.

## Evidence sources

The authoring process can use:

- Catalog metadata
- Table and column descriptions
- Data samples
- SQL evidence queries
- Uploaded context documents
- Existing bundle pages during update work

Catalog text is treated as source material. It is not treated as an instruction for the agent.

Glue evidence queries use Athena. Redshift queries use the database user from the mapping secret.

Configure that Redshift user with read-only, least-privilege database permissions. IAM does not make submitted Redshift SQL read-only.

## Consumption flow

People use the console to browse pages, view the graph, and chat with the wiki.

External agents use MCP tools. They can list datasets, read pages, search content, and follow backlinks.

## Trust model

The bundle is useful context, not an authority by itself. Important claims should include evidence and receive human review.

Human verification remains important for business definitions, production queries, and high-impact decisions.

## Next step

Review the [architecture](architecture.md).
