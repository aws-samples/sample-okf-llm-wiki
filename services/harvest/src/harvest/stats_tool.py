"""The supervisor's bundle-inventory tool.

One no-argument tool, ``get_stats``: counts the bundle's concept docs by
type (``okf_core.stats``) so the supervisor never answers "how many table
docs landed?" or "do I have any enum references?" by globbing directories
and counting listing lines in its head — a whole-bundle listing costs
thousands of prompt tokens per look and models miscount long lists; this
returns the same answer in a dozen fields, exactly.

Counts only, NO judgment — a zero row (``named_sets: 0``) is a fact made
visible, not a request to create one. Whether zero is right for this dataset
is the model's call. This is deliberately not a lint warning: lint gates
validity; stats state inventory.

SUPERVISORS ONLY — not in the sub-agent specs. Authors and reviewers work
one doc/cluster at a time; bundle-wide inventory in their hands invites
cross-cluster coverage findings that are the supervisor's job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from okf_core.stats import bundle_stats


def make_stats_tool(dataset_root: str | Path) -> Any:
    from langchain_core.tools import tool

    root = Path(dataset_root)

    @tool
    def get_stats() -> dict[str, Any]:
        """Deterministic bundle inventory: how many concept docs exist, by
        type. Takes NO arguments — everything is derived from the bundle on
        disk. Use this INSTEAD of glob/ls whenever you only need counts
        (progress checks, coverage sanity, the final report) — it is exact
        and costs no listing tokens.

        Returns `total_docs`, `datasets`, `tables`, `snapshot_tables` (from
        `.metadata/` — compare with `tables` for coverage; null when no
        snapshot exists), `references` (per subtype: joins, enums, metrics,
        named_sets, glossary, recipes, known_issues, usage_guardrails —
        ZEROS INCLUDED), `external`, and `other_docs`. Generated files
        (index.md, log.md) and internal dirs (.metadata/.context/.harvest)
        are excluded.

        A zero is a FACT, not a warning: `named_sets: 0` on a dataset with
        no meaningful entity subsets is correct and needs no action; on one
        whose context docs define cohorts, it is a gap YOU should close.
        """
        try:
            return bundle_stats(root)
        except Exception as e:  # noqa: BLE001 — inventory must never abort the run
            return {"note": f"get_stats crashed: {type(e).__name__}: {str(e)[:300]}"}

    return get_stats
