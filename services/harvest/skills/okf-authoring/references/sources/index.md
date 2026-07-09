# Source adapters

The OKF authoring workflow in `SKILL.md` is **source-generic** — state and verify
the grain, disambiguate near-synonyms, summarize wide tables, cite real sources.
But the *mechanics* differ per backend: how you harvest schema, what `type` /
`resource` / `dialect` to write, the type vocabulary, how identifiers are quoted,
and which traps a table doc must capture. An **adapter** is the per-backend sheet
of those specifics. Pass 0 step 5 tells you to load the matching one before you
author.

## Pick your adapter

| Your source looks like… | Adapter | Engine / dialect |
|---|---|---|
| S3 files (Parquet/ORC/JSON/CSV) + Glue Data Catalog metadata, queried by Athena | [athena-glue](athena-glue.md) | Athena engine v3 (Trino) DML + Hive DDL |
| `*.redshift.amazonaws.com:5439` or a Serverless workgroup; `SVV_*`/`PG_TABLE_DEF`, DISTKEY/SORTKEY, SUPER | [redshift](redshift.md) | Amazon Redshift SQL (Postgres-derived) |

No adapter for your source? Fall back to the generic guidance in `SKILL.md`, pin
the dialect as best you can, and **add an adapter** (below) if the source is one
you'll author against again.

## What every adapter contains (the fixed structure)

Each adapter is plain markdown (no frontmatter — these are skill reference docs,
not bundle concepts) with these sections, in order:

1. **When you're on this source** — the tell-tale signs.
2. **Frontmatter conventions** — the `type` values, the `resource` URI/ARN form,
   and the exact `dialect:` string to pin.
3. **Schema extraction** — concrete commands, both an out-of-band API/CLI method
   and an in-SQL catalog method, plus how to read partition/distribution layout
   and a *cheap* row count (grain verification needs counts — don't full-scan a
   billed warehouse to get them).
4. **Type vocabulary** — the canonical column type names to write in `# Schema`.
5. **Identifier quoting** — how identifiers and string literals are quoted, with a
   one-line `SELECT`.
6. **Idioms** — nested/semi-structured access, sampling, cost control — anything
   that changes how `# Common query patterns` are written.
7. **Gotchas** — the source-specific traps worth a `# Gotchas` note in table docs.
8. **Citations** — the authoritative vendor docs.
9. **Verify before you rely on it** — specifics the research could not confirm
   verbatim, listed so you re-check them rather than trust them blindly.

## Adding a new source adapter

1. Copy the section structure above (clone an existing adapter as a skeleton).
2. **Verify every specific against authoritative vendor docs** — exact system-table
   names, CLI flag spellings, type names, ARN/URI forms. These are precisely the
   facts that are easy to get subtly wrong, and a wrong adapter violates the
   skill's core "never invent" rule. Cite the pages you actually consulted; list
   anything you could not confirm under "Verify before you rely on it."
3. Add a row to the **Pick your adapter** table above.
4. Add the file to the `references/sources/` bullet in `SKILL.md` if you want it
   discoverable from the skill's file list.
