"""Finalize a harvest: regenerate index.md files and write the commit marker.

Bundle publish is NOT atomic on S3, so a consumer could catch a half-written
bundle. We write ``.harvest/state.json`` LAST as a commit marker: consumers (and
the reindex worker's readiness checks) treat a bundle as ready only when this
marker is present and its ``status`` is ``complete``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harvest.fsutil import mkdirs, write_text
from okf_core.index_gen import regenerate_indexes

_STATE_DIR = ".harvest"
_STATE_FILE = "state.json"


def finalize_bundle(
    dataset_root: str | Path,
    *,
    data_domain: str,
    dataset: str,
    tables: list[str],
    timestamp: str,
    table_versions: dict[str, str] | None = None,
    synthesize=None,
) -> dict[str, Any]:
    """Regenerate indexes, then write the commit marker. Returns the state doc."""
    root = Path(dataset_root)

    # 1) Regenerate index.md files (progressive disclosure).
    regenerate_indexes(root, synthesize=synthesize)

    # 2) Write the commit marker LAST.
    state = {
        "status": "complete",
        "data_domain": data_domain,
        "dataset": dataset,
        "tables": sorted(tables),
        "completed_at": timestamp,
        # per-table Glue VersionId / UpdateTime seen at harvest time, used by the
        # incremental path to detect real changes.
        "table_versions": table_versions or {},
    }
    state_dir = root / _STATE_DIR
    mkdirs(state_dir)
    write_text(
        state_dir / _STATE_FILE,
        json.dumps(state, indent=2, sort_keys=True) + "\n",
    )
    return state


def mark_in_progress(
    dataset_root: str | Path, *, data_domain: str, dataset: str, timestamp: str
) -> None:
    """Write an in-progress marker at the START of a harvest.

    Overwrites any prior ``complete`` marker so consumers know the bundle is
    mid-write until :func:`finalize_bundle` restores ``complete``.
    """
    root = Path(dataset_root)
    state_dir = root / _STATE_DIR
    mkdirs(state_dir)
    write_text(
        state_dir / _STATE_FILE,
        json.dumps(
            {
                "status": "in_progress",
                "data_domain": data_domain,
                "dataset": dataset,
                "started_at": timestamp,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
