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
import shutil
import time
from pathlib import Path

# Transient NFS errors worth retrying (stale handle, and remote I/O).
_RETRYABLE = {errno.ESTALE, errno.EIO}  # 116, 5
_ATTEMPTS = 5
_BASE_SLEEP = 0.3


def _retry(fn, *, what: str):
    last: OSError | None = None
    for attempt in range(_ATTEMPTS):
        try:
            return fn()
        except OSError as e:
            if e.errno not in _RETRYABLE or attempt == _ATTEMPTS - 1:
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
    """``write_text`` that tolerates transient ESTALE and ensures the parent dir."""
    p = Path(path)
    mkdirs(p.parent)
    _retry(lambda: p.write_text(text, encoding="utf-8"), what=f"write {p}")


def clean_authored_output(dataset_root: str | Path) -> list[str]:
    """Delete a full harvest's PRIOR authored output for a clean rebuild.

    "Full harvest" means start from scratch: remove everything the agent
    previously authored — ``datasets/``, ``tables/``, ``references/``, the
    generated ``index.md``/``log.md`` files, and any leaked scratch — so a table
    dropped from Glue since last time doesn't linger as a stale doc (and, via the
    S3 Files write-through → ObjectRemoved event → reindex ``DeleteVectors``, its
    vector is pruned too).

    PRESERVED (never deleted): dot-prefixed top-level entries — ``.context/``
    (user-uploaded source docs; these are INPUTS, not our output) and
    ``.harvest/`` (the commit marker the caller has just refreshed to
    ``in_progress``). The rule is simply: delete every top-level entry whose name
    does not start with ``.``.

    Returns the sorted names removed (for logging). Missing root = nothing to do.
    NFS-resilient: each removal retries transient ESTALE/EIO.
    """
    root = Path(dataset_root)
    if not root.exists():
        return []

    removed: list[str] = []
    for child in sorted(_retry(lambda: list(root.iterdir()), what=f"iterdir {root}")):
        if child.name.startswith("."):
            continue  # preserve .context/ (user input) + .harvest/ (state)

        def _rm(target: Path = child) -> None:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()

        _retry(_rm, what=f"rm {child}")
        removed.append(child.name)
    return removed
