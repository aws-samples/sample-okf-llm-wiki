# OKF v0.1 — condensed normative reference

This condenses the Open Knowledge Format v0.1 spec to the rules an author needs.
Quoted MUST/SHOULD/MAY follow RFC 2119 sense. For the full spec narrative, the
source of truth is the upstream `SPEC.md`.

## Bundle structure

A bundle is a directory tree of markdown files. The directory layout is
**independent of the domain** — producers organize concepts however makes sense.

```
bundle/
├── index.md            # optional, generated — directory listing (§ Index files)
├── log.md              # optional — chronological update history
├── <concept>.md        # a concept at the root
└── <subdir>/
    ├── index.md
    ├── <concept>.md
    └── <subdir>/...
```

A bundle MAY ship as a git repo (recommended — history, attribution, diffs), a
tarball/zip, or a subdirectory of a larger repo.

### Reserved filenames (at every level)

| Filename   | Purpose                                  |
|------------|------------------------------------------|
| `index.md` | Directory listing (progressive disclosure). |
| `log.md`   | Update history.                          |

`index.md` and `log.md` MUST NOT be used as concept documents. All other `.md`
files ARE concept documents.

## Concept documents

A concept is one UTF-8 markdown file with two parts:

1. A **YAML frontmatter block**, delimited by a line that is exactly `---` at the
   very start of the file, and a closing line that is exactly `---`.
2. A **markdown body** — everything after the closing delimiter.

### Frontmatter

```yaml
---
type: <Type name>                  # REQUIRED
title: <Optional display name>
description: <Optional one-line summary>
resource: <Optional canonical URI for the underlying asset>
tags: [<tag>, <tag>]               # optional
timestamp: <ISO 8601 datetime>     # optional last-modified time
# ... arbitrary producer-defined keys allowed
---
```

- **`type` is the only REQUIRED key.** Short string identifying the kind of
  concept; consumers route/filter/present on it. Not centrally registered.
  Consumers MUST tolerate unknown `type` values (treat as generic).
- Recommended, in priority order: `title`, `description` (one sentence, reused in
  `index.md`), `resource`, `tags`, `timestamp`.
- `resource` is absent for abstract concepts (ideas, not physical assets).
- Producers MAY include any additional keys. Consumers SHOULD preserve unknown
  keys on round-trip and MUST NOT reject docs with unrecognized fields.

> Note: the upstream reference *implementation* additionally requires
> `title`, `description`, and `timestamp` (its `write` tool refuses without
> them) and auto-fills `timestamp`. The *spec* requires only `type`. For
> interoperable, high-quality output, always include `type`, `title`,
> `description`, `timestamp`, and `resource` when applicable.

### Body

Standard markdown. Producers SHOULD favor structural markdown (headings, lists,
tables, fenced code) over freeform prose. No required sections. Conventional
headings:

| Heading       | Purpose                                              |
|---------------|------------------------------------------------------|
| `# Schema`    | Structured description of an asset's columns/fields. |
| `# Examples`  | Concrete usage examples, often fenced code.          |
| `# Citations` | External sources backing claims (numbered).          |

## Cross-linking

Concepts link to each other with standard markdown links. Two forms:

- **Bundle-relative (absolute)** — begins with `/`, resolved from bundle root:
  `[customers](/tables/customers.md)`. Spec calls this the recommended form for
  stability, **but it breaks GitHub rendering** — prefer file-relative in
  practice.
- **Relative** — `[neighbor](./other.md)`, `[parent ds](../datasets/sales.md)`.

A link asserts a *relationship*; its kind is conveyed by surrounding prose, not
the link. Consumers treat links as directed edges of an untyped relationship and
**MUST tolerate broken links** (they may be not-yet-written knowledge).

## Index files

An `index.md` MAY appear in any directory (including the root) to enumerate the
directory's contents for progressive disclosure. **No frontmatter** (exception:
the bundle-root `index.md` MAY carry `okf_version: "0.1"`). Body is one or more
sections grouping entries under headings:

```markdown
# Section / Group Heading

* [Title 1](relative-url-1) - short description of item 1
* [Title 2](relative-url-2) - short description of item 2

# Another Section

* [Subdirectory](subdir/) - short description of the subdirectory
```

Entries SHOULD carry the linked concept's `description`. Producers MAY generate
`index.md`; consumers MAY synthesize one on the fly.

## Log files (optional)

```markdown
# Directory Update Log

## 2026-05-22
* **Update**: Added Glue table reference for [Customer Metrics](/tables/customer-metrics.md).
* **Creation**: Established the [Freshness Playbook](/playbooks/freshness.md).

## 2026-05-15
* **Initialization**: Created foundational directory structure.
```

Date headings MUST be ISO 8601 `YYYY-MM-DD`, newest first. Entries are prose; the
leading bold word is a convention, not a requirement.

## Citations

Claims sourced from external material SHOULD be listed under a bottom `# Citations`
heading, numbered:

```markdown
# Citations

[1] [Source Title](https://example.com/...)
[2] [Another Source](https://example.com/...)
```

Citation links MAY be absolute URLs, bundle-relative paths, or paths into a
`references/` subdirectory that mirrors external material as OKF concepts.

## Conformance (§9)

A bundle is **conformant** with OKF v0.1 if:

1. Every non-reserved `.md` file contains a parseable YAML frontmatter block.
2. Every frontmatter block contains a non-empty `type` field.
3. Every reserved filename (`index.md`, `log.md`) follows its structure when present.

Consumers treat all other constraints as soft and MUST NOT reject a bundle for:
missing optional frontmatter fields, unknown `type` values, unknown extra
frontmatter keys, broken cross-links, or missing `index.md` files. This
permissive consumption is intentional — bundles grow, get refactored, and are
partly agent-generated.

## Versioning

This is OKF **0.1**. Minor bumps add backward-compatible features; major bumps
may break. A bundle MAY declare its target version via `okf_version: "0.1"` in
the bundle-root `index.md` frontmatter (the only place frontmatter is permitted
in an `index.md`). Consumers that don't understand a declared version SHOULD do
best-effort consumption rather than refuse.

## Non-goals (don't try to make OKF do these)

- It does not define a fixed taxonomy of concept types.
- It does not prescribe storage, serving, or query infrastructure.
- It does not replace domain schemas (Avro, Protobuf, OpenAPI, …) — it
  *references* them; it does not subsume them.
