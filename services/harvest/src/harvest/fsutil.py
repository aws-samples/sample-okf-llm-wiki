"""NFS-resilient filesystem helpers for the S3 Files mount.

The bundle lives on an S3 Files (NFSv4.2) mount with close-to-open consistency.
Two things make raw ``pathlib`` calls flaky there:

* **ESTALE (Errno 116, "Stale file handle")** — an NFS handle the client cached
  can go stale (e.g. a directory just created in another op, or churn on the
  mount). The cure is simply to retry: the retry forces a fresh NFS lookup.
* Brief consistency windows right after creating a directory.

These wrappers retry the small set of transient NFS errnos with backoff, so a
one-off ESTALE doesn't abort the whole harvest. They're used for the state/dir
operations the runner performs directly; the agent's own file writes go through
the deepagents FilesystemBackend.
"""

from __future__ import annotations

import errno
import logging
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Transient NFS errors worth retrying (stale handle, and remote I/O).
_RETRYABLE = {errno.ESTALE, errno.EIO}  # 116, 5
_ATTEMPTS = 5
_BASE_SLEEP = 0.3

# DELETE paths retry a WIDER set. shutil.rmtree classifies each entry with a
# stat, and when a transient NFS hiccup makes that stat raise, CPython falls
# back to "treat it as a file" and unlinks — unlinking a directory then
# surfaces as EACCES/EPERM, masking the transient root cause (seen live: a
# full-harvest wipe dying on `references/recipes` while a concurrent harvest
# churned the mount). A fresh rmtree re-scandirs and classifies correctly, so
# EACCES/EPERM are retryable HERE — and only here: a genuine denial still
# fails after the bounded attempts, and write_text keeps its own deliberate
# PermissionError handling (unlink + rewrite), which widening _RETRYABLE
# would have short-circuited.
_RM_RETRYABLE = _RETRYABLE | {errno.EACCES, errno.EPERM}  # + 13, 1


def _retry(fn, *, what: str, retryable: set[int] = _RETRYABLE):
    last: OSError | None = None
    for attempt in range(_ATTEMPTS):
        try:
            return fn()
        except OSError as e:
            if e.errno not in retryable or attempt == _ATTEMPTS - 1:
                raise
            last = e
            time.sleep(_BASE_SLEEP * (2**attempt))
    if last:  # pragma: no cover - loop always returns or raises above
        raise last


def mkdirs(path: str | Path) -> Path:
    """``mkdir(parents=True, exist_ok=True)`` that tolerates transient ESTALE.

    Creates each ancestor individually and retries per level, so a stale handle
    on a just-created parent doesn't fail the whole chain.
    """
    p = Path(path)
    # Build the list of ancestors to create, shallowest first.
    parts: list[Path] = []
    cur = p
    while True:
        parts.append(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    for d in reversed(parts):
        if str(d) in ("", "/"):
            continue
        _retry(
            lambda d=d: d.mkdir(exist_ok=True) if not d.exists() else None,
            what=f"mkdir {d}",
        )
    return p


def write_text(path: str | Path, text: str) -> None:
    """``write_text`` that tolerates transient ESTALE and ensures the parent dir.

    Also heals EACCES on files whose S3 object was written OUTSIDE the mount:
    S3 Files maps object metadata to POSIX permissions, and an object PUT by
    another principal (the Control API's repromote rewrites
    ``.harvest/state.json``) carries none of the mount's file-mode metadata, so
    the mount presents it READ-ONLY — open-for-write raises PermissionError
    even though the runtime's S3 role may write the key. The enclosing dir was
    mount-created (writable), so unlink the read-only presentation and rewrite;
    the fresh file gets normal mount metadata again.
    """
    p = Path(path)
    mkdirs(p.parent)
    try:
        _retry(lambda: p.write_text(text, encoding="utf-8"), what=f"write {p}")
    except PermissionError:
        # The unlink itself gets the delete-path retry set: on this mount a
        # stat/lease blip can surface as EACCES here too, and losing the heal
        # would fail the caller for a transient reason.
        _retry(
            lambda: p.unlink(missing_ok=True),
            what=f"unlink {p}",
            retryable=_RM_RETRYABLE,
        )
        _retry(lambda: p.write_text(text, encoding="utf-8"), what=f"rewrite {p}")


def remove_tree(path: str | Path) -> bool:
    """Remove a directory subtree (or file), tolerating transient NFS errors.

    Returns True when something was removed, False when ``path`` didn't exist.
    Used by the cross-dataset mode to clear a pair's prior ``external/<d>/<ds>/``
    output before re-authoring (the scoped analogue of clean_authored_output).
    """
    p = Path(path)
    if not p.exists():
        return False

    def _rm() -> None:
        if not p.exists():
            return  # a prior (partially successful) attempt finished the job
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink(missing_ok=True)

    _retry(_rm, what=f"rm {p}", retryable=_RM_RETRYABLE)
    return True


def clean_authored_output(dataset_root: str | Path) -> list[str]:
    """Delete a full harvest's PRIOR authored output for a clean rebuild.

    "Full harvest" means start from scratch: remove everything the agent
    previously authored — ``datasets/``, ``tables/``, ``references/``, the
    generated ``index.md``/``log.md`` files, and any leaked scratch — so a table
    dropped from Glue since last time doesn't linger as a stale doc (and, via the
    S3 Files write-through → ObjectRemoved event → reindex ``DeleteVectors``, its
    vector is pruned too).

    UNCONDITIONAL by design — there is deliberately no keep-list. A full
    harvest is the destructive mode: even human-VERIFIED Attested Computations
    go (they are frozen against agents in the in-place modes only — see
    ``harvest.verification.frozen_computation_paths``), so a re-authored
    computation returns to the human's verify queue rather than inheriting a
    stamp that attested different content. Requesting a full re-harvest IS
    the decision to rebuild the wiki from source.

    PRESERVED (never deleted): dot-prefixed top-level entries — ``.context/``
    (user-uploaded source docs; these are INPUTS, not our output) and
    ``.harvest/`` (the commit marker the caller has just refreshed to
    ``in_progress``). The rule is simply: delete every top-level entry whose name
    does not start with ``.``.

    Returns the sorted names removed (for logging). Missing root = nothing to do.
    NFS-resilient: each removal retries the transient set PLUS EACCES/EPERM
    (see ``_RM_RETRYABLE`` — rmtree reports a flaky stat as a permission error
    on the directory), and is idempotent so a partially completed attempt
    doesn't fail its own retry.
    """
    root = Path(dataset_root)
    if not root.exists():
        return []

    removed: list[str] = []
    for child in sorted(_retry(lambda: list(root.iterdir()), what=f"iterdir {root}")):
        if child.name.startswith("."):
            continue  # preserve .context/ (user input) + .harvest/ (state)

        def _rm(target: Path = child) -> None:
            if not target.exists():
                return  # a prior (partially successful) attempt finished the job
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)

        _retry(_rm, what=f"rm {child}", retryable=_RM_RETRYABLE)
        removed.append(child.name)
    return removed


#: The canonical authored-folder skeleton (the okf-authoring skill's routing
#: table) — every directory a doc write is allowed to need. Pre-created by
#: IN-PLACE runs; see :func:`ensure_authored_dirs`.
AUTHORED_DIR_SKELETON = (
    "datasets",
    "tables",
    "references",
    "references/joins",
    "references/metrics",
    "references/enums",
    "references/named_sets",
    "references/glossary",
    "references/known_issues",
    "references/recipes",
    "references/computations",
)


def ensure_authored_dirs(
    dataset_root: str | Path, extra: tuple[str, ...] = ()
) -> list[str]:
    """Pre-create the canonical authored folders (IN-PLACE runs only).

    A full harvest wipes and re-authors, so every directory it needs is
    created by the run itself. An IN-PLACE run (incremental / annotation /
    cross) works inside a tree materialized from existing S3 prefixes, and
    creating a NEW directory there has failed live with EACCES: the deepagents
    backend's write path does ONE raw ``mkdir(parents=True)`` with none of
    this module's healing, and the whole write dies (seen when an annotation
    promoted a bundle's first Attested Computation and
    ``references/computations/`` didn't exist yet — file writes into existing
    dirs in the same run were fine). Creating the skeleton up front, with the
    wider EACCES/EPERM retry set the delete paths use (an attribute-cache
    blip on this mount can surface as a permission error), means no agent
    write should ever need a brand-new directory.

    Best-effort per directory: a persistent refusal is LOGGED and skipped —
    the run proceeds and the agent reports the blocked write exactly as
    before, but the log now names the real failure. Unused empty dirs are
    invisible downstream: they never materialize as S3 prefixes (no objects),
    and index generation skips empty subtrees.

    ``extra`` appends run-specific directories (e.g. a cross run's
    ``external/<domain>/<dataset>`` pair subtree).

    Returns the relative paths actually created (for logging).
    """
    root = Path(dataset_root)
    created: list[str] = []
    for rel in (*AUTHORED_DIR_SKELETON, *extra):
        d = root / rel
        try:
            if d.is_dir():
                continue
            _retry(
                lambda d=d: d.mkdir(parents=True, exist_ok=True),
                what=f"mkdir {d}",
                retryable=_RM_RETRYABLE,
            )
            created.append(rel)
        except OSError:
            log.warning(
                "could not pre-create %s — an agent write needing this "
                "directory will fail and be reported in-run",
                d,
                exc_info=True,
            )
    return created
