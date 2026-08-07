"""Finalize a harvest: regenerate index.md files and write the commit marker.

Bundle publish is NOT atomic on S3, so a consumer could catch a half-written
bundle. We write ``.harvest/state.json`` LAST as a commit marker: consumers (and
the reindex worker's readiness checks) treat a bundle as ready only when this
marker is present and its ``status`` is ``complete``.

The policy build does NOT live here: the harvest is DONE at the commit marker
(the runner flips the status row terminal right after this returns), and the
policy document authors as its own follow-on step in the runner — serialized
against new bundle-writing work by ITS OWN lock, the mapping row's
``ar_build_status = building`` flip (see ``okf_aws.ar_policy.build_lock_active``
and CONVENTIONS.md).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from harvest.fsutil import mkdirs, remove_tree, write_text
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
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Regenerate indexes, then write the commit marker. Returns the state doc.

    ``extra`` merges additional provenance keys into the marker — a cross-
    dataset run writes ``cross_target`` (the counterpart pair it documented)
    so the fresh ``complete`` marker records what its ``external/`` docs are
    about (see CONVENTIONS.md).
    """
    root = Path(dataset_root)

    # 1) Regenerate index.md files (progressive disclosure). The writer/remover
    # are fsutil's, not raw pathlib: these land on the S3 Files (NFS) mount,
    # where an in-place rewrite of an existing index can come back EACCES —
    # which used to fail the whole harvest HERE, after every doc was authored
    # (seen live on `references/enums/index.md`). fsutil heals that by
    # unlinking first, and retries transient ESTALE/EIO.
    regenerate_indexes(
        root,
        synthesize=synthesize,
        write_file=lambda path, text: write_text(path, text),
        remove_file=remove_tree,
    )

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
        **(extra or {}),
    }
    state_dir = root / _STATE_DIR
    mkdirs(state_dir)
    write_text(
        state_dir / _STATE_FILE,
        json.dumps(state, indent=2, sort_keys=True) + "\n",
    )

    # 3) Drop the recorded context-extractor digests — AFTER the commit
    # marker, so a failure anywhere earlier keeps them for debugging. They
    # exist for run_review's context-fidelity phase, which is over by now;
    # the next full harvest would wipe them at start anyway (that wipe stays
    # — it is what protects a run whose predecessor crashed past this point).
    # Best-effort: the harvest is COMMITTED at this point, and a stubborn NFS
    # delete error must not flip a finished multi-hour run to failed (same
    # rationale as the index-write healing above); the start-of-run wipe is
    # the correctness backstop.
    try:
        remove_tree(state_dir / "context")
    except OSError:
        logging.getLogger(__name__).warning(
            "Could not remove %s after commit; the next full harvest's "
            "start-of-run wipe will clear it",
            state_dir / "context",
            exc_info=True,
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
