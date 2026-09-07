# MCP tools

The Data Wiki consumption server exposes tools for discovery, reading, search, and published computations.

Call `read_me` before you explore a new server.

## Discovery tools

| Signature | Purpose |
| --- | --- |
| `read_me()` | Return the recommended tool workflow |
| `list_declared_domains()` | List declared data domains and their context |
| `search_domains(query, top_k=5)` | Find declared domains by meaning |
| `list_domains(domain=None, query=None, cursor=None, limit=100)` | Page through registered domain and dataset pairs |

Use `search_domains` to choose a domain. Then pass its name to `list_domains` to discover datasets.

`list_domains.limit` is clamped from 1 through 500. Pass `next_cursor` back unchanged to read the next page.

Arguments without defaults are required. `domain` and `query` are optional filters.

## Reading and search tools

| Signature | Purpose |
| --- | --- |
| `list_directory(data_domain, dataset, path="")` | Read a bundle index or list child concepts |
| `read_page(concept_id, data_domain, dataset, offset=0, limit=None)` | Read one concept with line pagination |
| `glob(pattern, data_domain, dataset)` | Match concept paths with shell patterns |
| `grep(pattern, data_domain, dataset, ignore_case=True, max_results=100)` | Search content with a regular expression |
| `semantic_search(query, data_domain=None, dataset=None, table=None, type=None, tags=None, top_k=10)` | Find concepts by meaning and metadata |
| `get_backlinks(concept_id, data_domain, dataset)` | Find concepts in the selected dataset that link to one concept |

Use `grep` for exact names, values, and identifiers. Use `semantic_search` for business meaning.

`grep.max_results` is capped at 1,000. `semantic_search.top_k` is capped at 20.

Read selected pages after a search. Search results are candidates, not complete answers.

Semantic search returns `concept_id` as `<domain>/<dataset>/<relative-id>`. Remove the domain and dataset prefix before calling `read_page`.

`read_page.offset` is a zero-based line offset. Compare `returned_lines` with `total_lines` before requesting another page.

The `type` filter requires an exact frontmatter value. The `tags` filter matches any supplied tag.

## Computation tools

| Signature | Purpose |
| --- | --- |
| `list_computations(data_domain, dataset)` | List published computations and verification state |
| `describe_computation(name, data_domain, dataset)` | Read one computation contract |
| `run_computation(name, data_domain, dataset, parameters=None)` | Validate values, render SQL, and attempt execution |

Execution requires the deployment option `enable_attested_computations`.

Redshift execution also requires `enable_redshift`.

A published computation can be `verified`, `unverified`, or `stale`. Verification does not currently gate execution.

Always inspect `verification`, `executed`, `note`, `executed_sql`, and `warnings`.

The tool can return rendered SQL with `executed=false` when execution is disabled or unavailable.

## Recommended flow

1. Call `read_me`.
2. Discover the domain and dataset.
3. Read the dataset overview.
4. List the relevant directory.
5. Search for exact or semantic matches.
6. Convert semantic result IDs to bundle-relative IDs.
7. Read the selected concepts.
8. Follow backlinks when relationships matter.

Keep `data_domain` and `dataset` with every bundle-relative concept address.
