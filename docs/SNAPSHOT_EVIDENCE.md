# Snapshot evidence: column profiles & relationship sheets

Two deterministic passes run at harvest start — after the `.metadata/` catalog
snapshot, **before the agent exists** — and pre-answer the questions that
otherwise cost agent tool-loops: *what's in each column?* and *which tables
relate, on what keys, at what grain?* Both are plain Python inside
`export_metadata` (no LLM, no tools, no middleware); their outputs are
read-only files under `.metadata/` that every agent explores with its
built-in `read_file`/`grep`. The economics: each agent tool call replays the
agent's whole context as input tokens, both sides of a join used to probe the
same relationship independently, and reviewers re-verified it a third time —
a `for` loop over the same SQL costs none of that.

```
run_*_harvest (runner.py)
 └─ export_metadata (metadata_export.py)
      ├─ catalog snapshot: tables/*.md, columns.tsv, database.md, index.md
      ├─ write_profiles (profile.py)            → .metadata/profile/
      └─ write_relationship_evidence
         (relationships.py, consumes profiles)  → .metadata/relationships/
 └─ build_harvest_agent(...)                    ← agents read the files above
```

Both passes are **best-effort by contract**: a failed query is a manifest
row, a crashed pass is a log line — never a failed snapshot. Anything not
covered degrades to exactly the old behavior (the agent probes it live).
Each pass emits one live-feed status line (`Column profiles: …`,
`Relationship evidence: …`; an explicit "no candidates found" line when the
relationship pass ran but nominated nothing).

## Column profiles (`profile.py`)

One sheet per table — `profile/<table>.md` — answering the null/enum/range
probes authors would otherwise run:

- **Pass 1** (one scan): row count, and per column null share, approximate
  distinct count, min/max.
- **Pass 2** (bounded): top-K values for the most enum-like columns only
  (≤ `enum_max_distinct` ≈ 50 distinct, ≤ 15 columns, K = 20).

Cost posture:

- Tables over ~1 GiB (or with **no size hint** — treated as large; Iceberg
  tables, whose Glue Parameters never carry Hive stats, are first sized
  *exactly* from their `$files` metadata sum via the source's
  `iceberg_data_bytes` capability) are
  profiled from a `TABLESAMPLE BERNOULLI` row sample sized toward ~256 MiB.
  Sampled sheets are stamped **INDICATIVE** everywhere — a sampled value list
  is never proof of a closed enum. (BERNOULLI is fine here: per-row
  statistics survive row sampling; it does *not* reduce Athena's billed
  bytes, only aggregation work.)
- A wall-clock budget (default 30 min) bounds the whole pass; tables past it
  are `skipped-budget` in `profile/manifest.tsv`. The per-query ceiling
  (`OKF_HARVEST_PROFILE_QUERY_TIMEOUT_S`, default 60 s — reliably tables to
  ~50 GB at Athena's dependable scan rates) is the knob that decides how big
  a table can be profiled to COMPLETION: a cancelled query bills its partial
  scan and yields neither a sheet nor the size measurement, so raise it per
  deployment (~300 s buys the 100–500 GB band) when such tables are worth
  profiling. Every query's timeout is clamped to the budget remaining, so no
  setting lets one query overrun the pass.
- **The pass-1 scan doubles as a size measurement.** Athena reports each
  execution's `data_scanned_bytes`; because BERNOULLI reads full bytes,
  even a *sampled* profile measures the real footprint of the profilable
  columns. That number — plus per-column `{distinct, null_pct}` — persists
  as sibling keys on the table's `domains.json` entry (cache-carried under
  the same fingerprint policy) and feeds the relationship pass: observed
  bytes are a sizing rung, and the column stats rank sketch columns.
- **Fingerprint cache**: each sheet is keyed on the table's catalog identity
  (update time + version + column set + size/row hints). Incremental runs
  re-profile only the changed table, cross runs only mismatches; a **full
  harvest always re-profiles** (it is the explicit "re-read the data"
  action).

## Relationship evidence (`relationships.py`)

Join and grain verdicts under `relationships/`, probed by the **same SQL
cores** the live `validate_join`/`check_grain` tools call (`probes.py` — one
implementation, so precomputed and live numbers are bit-identical).

```
relationships/
├── manifest.tsv                    # kind, subject, fingerprint, status, at
├── joins/<a>__<b>--<key>.md        # match rates, cardinality, orphan samples
│   (…--<colL>--<colR>.md when the two sides spell the key differently)
└── grain/<table>.md                # key-uniqueness verdicts
```

### Candidate nomination — three sources, zero queries for the first two

1. **Shared names** (normalized: case- and underscore-blind, so `driver_id`
   groups with `driverid`). Only *key-like* names qualify — `_id`/`_key`/
   `_nbr`/`_sk` suffixes, or `raceid`-style names whose prefix names a table.
   Partition keys count as columns everywhere in this pass (in lake-style
   warehouses the partition column often IS the join key, and the engine
   queries it like any other).
   Widely-shared keys pair each holder with the column's **home table** (the
   probable PK side: `raceid` → `races`) instead of exploding pairwise; a key
   shared by many tables with no home is refused with a note.
2. **Role-named FKs**: a column whose normalized name strictly *ends with*
   another table's self-naming key — `home_team_api_id` → `team.team_api_id`.
   Invisible to shared-name matching (the role column exists in one table).
3. **Value sketches** (name-blind; full harvests only). During the snapshot
   each key-ish/text column gets a KMV **bottom-k sketch**: the k = 256
   smallest hashes of its distinct values — simultaneously a uniform sample
   of the value set and a cardinality estimator. Athena computes all of a
   table's sketches in one columnar scan (`min(DISTINCT
   from_big_endian_64(xxhash64(cast(col as varchar))), k)` — both tokens are
   load-bearing: `min(x, n)` is an order statistic over input *rows*, so
   without DISTINCT repeated values fill the k slots and fact-side FK
   sketches collapse; and Trino's `xxhash64` returns VARBINARY, so without
   the decode every cell is unparseable and the nominator silently
   disables itself); Redshift falls back to one cheap per-column query
   (`FNV_HASH`). Sketches are computed by their **own full scan** of just
   those columns — never from the profile pass's row sample: per-*value*
   facts don't survive row sampling (a parent key appearing once survives a
   rate-p sample with probability ~p, so a sampled sketch would publish
   deflated containment that silently reads as "unrelated"). The scan is
   affordable because columnar engines bill roughly the sketched columns'
   bytes, not the table's; tables *measured* over the size gate are never
   sketched — they fall back to name/role nomination and the sampled probe —
   while tables whose size is UNKNOWN get bounded **last-resort attempts**:
   visited after every measured table, each capped by the sketch per-query
   timeout (`OKF_HARVEST_REL_QUERY_TIMEOUT_S`), abandoned after two
   consecutive failures (the catalog is telling us the unknowns are big). A
   timed-out sketch yields NOTHING — never wrong evidence — which is why a
   bounded attempt is safe where a sampled sketch is not; and a nomination
   whose probe the size gate still refuses persists as a no-verdict
   **NOMINATED sheet** (`joins/…`) carrying the containment estimate and
   routing the author to a live `validate_join` — without it a renamed-key
   lead on an unsizable table would vanish into a manifest skip row.
   Eligibility is **type-based** (int/text families only — never
   floats/timestamps), deliberately name-blind; the ≤ 12 sketched columns
   per table are *ranked* by names first (key-suffixed, then
   id/key/code-style hints) and, within each name tier, by the profile
   pass's per-column stats — high-distinct, low-null first (that's what
   keys look like), with columns whose OBSERVED distinct count sits at or
   under the enum-domain floor excluded outright (the comparison stage
   refuses their nominations anyway, so a slot spent on them buys nothing;
   safe even from a sampled profile — a genuine key's in-sample distinct
   tracks the sampled row count, orders of magnitude above the floor). The
   enum-domain floor is also checked against the sketch's own KMV
   cardinality estimate.
   Because non-full runs never re-sketch, full runs persist
   the nominations in `relationships/candidates.json`; incremental/cross
   runs revalidate and merge them so sketch-discovered sheets are reused or
   re-probed instead of vanishing with the `.metadata` wipe. All cross-table column pairs are then compared **in
   memory**: estimated value containment ≥ 0.5 nominates the pair. This is
   what catches renamed keys (`satscores.cds` = `schools.cdscode`).
   Guardrails, all learned from real schemas: the *contained* side must
   exceed the enum-domain size (a tiny domain — status codes 1–8 — is
   contained in everything); the contained side must NOT be its own table's
   PK (bare `id` or a self-naming key — FKs point AT PKs, so a PK contained
   elsewhere is the dense-surrogate coincidence that nominated 301 junk
   `id↔id` pairs on MusicBrainz); containment INTO a dense **int** PK
   additionally requires the contained column to NAME the containing table
   (`artist` → `artist.id`, `begin_area` → `area.id`, `raceid` → `races`) —
   a 1..N surrogate numerically contains every smaller int column (ordering
   columns, counts, unrelated FKs), so values alone prove nothing there,
   while hash/UUID PKs and non-PK containers like `cdscode` are sparse
   domains where values alone remain sufficient; and same-named holder pairs collapse
   toward the column's HOME table exactly like the name source (`tag` held
   by fifteen `*_tag` tables pairs each holder with `tag`, never pairwise —
   widely-shared same-named columns with no home are refused outright).

Nominations dedup across sources and share one PROBE cap (default 100).
The cap counts probed pairs only — cache reuses, TYPE MISMATCH sheets, and
size-skips are free — so no slot is ever spent without producing evidence,
and a re-run's budget goes entirely to new probes instead of being eaten by
cached sheets. When it binds, the budget is spread across tables
(least-covered endpoint first: every table earns its first sheet before a
hub earns its twelfth) with sketch nominations winning ties — a skipped
name pair is one `columns.tsv` grep away for an author; a skipped
renamed-key pair is unrecoverable live. Pairs beyond the budget are
`skipped-cap` without even being sized. **Precision over recall throughout**: a
missed candidate falls back to live probing; a junk candidate bills real
scans.

### The size gate — probe shape per pair

Name/role nomination is size-blind (pure catalog strings, zero queries);
sketch nomination is size-gated as described above. The gate's job in this
section is different: picking the **probe shape** for each nominated pair.
Per-side size resolves as: catalog byte hint → **profile-observed scan
bytes** (free — the profile pass-1 query already read the table's profilable
columns in full and its `data_scanned_bytes` is a COMPLETE-scan measurement,
never an early-exit lower bound; also the honest number for what a probe
would bill, unlike a listing that sums columns nobody reads) → **Iceberg
`$files` metadata sum** (Iceberg tables only — exact for the current
snapshot, a manifests-only scan through the engine's own permissions, so it
also answers for LF-governed/cross-account Iceberg tables; an S3 listing
would instead overcount every retained snapshot until VACUUM) → **S3
listing** of the table's location (`estimate_table_bytes`, LIST calls only,
early-exit at the gate; needed because DDL-registered tables carry no
`totalSize` — only crawlers/ETL write it) → assume large. Then:

| Pair shape | Action |
|---|---|
| both sides ≤ 10 GiB | full two-direction probe (4 aggregate queries) |
| exactly one side bigger, size known | **sampled probe**: big side `TABLESAMPLE SYSTEM` toward ~256 MiB vs the FULL small side |
| both big, or size unmeasurable | `skipped-size` |

The sampled probe reports **only the sampled→full direction** — a uniform
sample of the contained side probed against the full containing side is an
unbiased match-rate estimate, while the reverse collapses toward the sample
fraction on a perfect join. The sampled side's key uniqueness is unknowable
(a clean sample proves nothing) and is never surfaced; sheets carry an
INDICATIVE banner. SYSTEM (not BERNOULLI) because it skips whole splits and
actually cuts billed bytes; the cost is split-clustering bias, disclosed on
the sheet. Grain probes are never sampled. Two mechanical rules keep the
numbers honest: every sampled-side figure comes from ONE query (each
`TABLESAMPLE` reference draws an independent sample, so the matched count
rides a LEFT JOIN inside the same scan — splitting it would divide numbers
from different draws), and the sample percent is sized from a **complete**
S3 listing, never the gate measurement (which early-exits and is only a
lower bound — sizing off it would over-sample a huge table by orders of
magnitude); when even the complete listing can't tell, the pair skips.

### Verdicts (mechanical, thresholds on the best match rate)

- **HOLDS** (≥ 0.8) — with 1:1/1:N/N:1/M:N cardinality.
- **WEAK** (0.2–0.8) — investigate; partial overlap can be a real join with
  a story, or a trap.
- **SUSPECT** — trumps HOLDS when the *shared-enum signature* fires: M:N
  (neither side key-unique) with both value domains tiny and heavily
  repeated. High mutual containment of two code lists is not a key
  relationship; the sheet routes the author toward an enum/named-set doc.
- **REFUTED** (≤ 0.2) — "do NOT document this join"; a refuted sheet is
  itself valuable (it stops the model trusting a coincidental name).
- **TYPE MISMATCH** — same key, incomparable type families; no query runs
  (the `=` would error), but the sheet says a cast-join is likely.

Join sheets with a sub-100% match also carry an **orphan sample** (a few
actual unmatched rows) so the author can *interpret* the mismatch — the
left-vs-inner-join advice — without another query. Grain sheets state
UNIQUE / NOT unique with duplicate samples.

### Caching & budget

Every subject is fingerprinted (both tables' catalog fingerprints + the key)
in `manifest.tsv`; reuse follows the profile policy (incremental: re-probe
only pairs touching the changed table; cross: mismatches only; full: fresh).
Only `ok`/`ok-sampled` rows are cache-eligible — a failed probe is an
`error:` row, so a transient throttle is re-probed next run instead of being
served as evidence. A wall-clock budget (default 30 min) marks the tail
`skipped-budget` (checked before size resolution, so a dead budget never
bills S3 listings just to write skip rows). Two ordering rules keep a
flooded run useful: **grain probes run first** (one cheap aggregate per
table, high author value — a candidate-flooded join loop must never starve
them to zero again), and **the sketch scan is hard-capped at half the
budget**, so collection can't eat the time the probes exist to spend.

## How agents consume it

The runtime prompt and the table-author's steps enforce **sheets first,
probes second**: a sheet's verdict is this run's evidence — do not re-run
`validate_join`/`check_grain` on anything a sheet answers; probe live only
what no sheet covers (composite keys, transform-dependent joins,
context-doc claims) or where a sheet genuinely conflicts with another
source. Reviewers use the same sheets as ground truth, and the authoring
rule still applies on top: docs record *proportions + mechanism*, never the
sheets' raw row counts.

## Knobs

Authoritative table in CONVENTIONS.md; the ones that matter operationally:

| Env | Default | Meaning |
|---|---|---|
| `OKF_HARVEST_PROFILE_BUDGET_S` | 1800 | profile pass wall clock |
| `OKF_HARVEST_PROFILE_QUERY_TIMEOUT_S` | 60 | per-profile-query ceiling, clamped to the budget remaining; raise (~300) for 100+ GB tables |
| `OKF_HARVEST_REL_BUDGET_S` | 1800 | relationship pass wall clock |
| `OKF_HARVEST_REL_QUERY_TIMEOUT_S` | 60 | sketch per-query timeout; bounds each unknown-size last-resort attempt |
| `OKF_HARVEST_REL_MAX_PAIRS` | 100 | join-pair cap |
| `OKF_HARVEST_REL_MAX_TABLE_BYTES` | 10 GiB | full-probe size gate per side |
| `OKF_HARVEST_REL_SAMPLE_TARGET_BYTES` | 256 MiB | sampled-probe target |
| `OKF_HARVEST_REL_SKETCH_MIN_CONTAINMENT` | 0.5 | sketch nomination threshold |
| `OKF_HARVEST_REL_ENABLED` / `…_SKETCH_ENABLED` | on | kill switches |

## Known limitations

- Composite keys and transform-dependent joins (`lpad`, `TRIM`) are not
  nominated; sketches surface *reduced* containment for value-transforming
  keys, sometimes below threshold. Authors still own these live.
- The sketch comparison is blind to very lopsided pairs: when two key
  columns' distinct counts differ by more than ~k/8 (≈ 32× at k = 256), the
  smaller side keeps almost no survivors in the merged bottom-k and real
  containment reads as ~zero — a silent nomination miss, not an error. The
  pass logs a count of such pairs; raising `OKF_HARVEST_REL_SKETCH_K` moves
  the wall proportionally. It fails safe (name/role nomination or live
  probing still covers the pair).
- `estimate_table_bytes` can't list LF-governed / cross-account locations
  (no direct S3 perms — LF vends creds to Athena, not to us): such tables
  without catalog hints degrade to `skipped-size`. An empty root listing on
  a partitioned table is likewise unmeasurable, not zero (`ADD PARTITION`
  can point data outside the root). Two exemptions shrink this wall:
  **Iceberg tables** size via their `$files` metadata sum, and **any table
  whose profile completed** sizes via the observed scan bytes — both answer
  through the query engine's own permissions. The residue is a non-Iceberg
  LF/cross-account table whose profile also failed or was skipped; even
  there, sketch nomination still makes a bounded last-resort attempt.
- Redshift gets no sampled probes yet (no reference-level sampling clause)
  and per-column (not batched) sketches.
