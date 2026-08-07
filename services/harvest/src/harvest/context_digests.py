"""Persist ``context-extractor`` dispatch outputs for the review's fidelity phase.

The context-extractors return their routed fact digests as plain text to the
supervisor, who slices them into the authoring briefs — nothing lands on disk,
so by review time the ONLY record of what the uploaded ``.context/`` docs
actually said lives in the supervisor's (compacting) transcript. The
``run_review`` context-fidelity phase needs the digests VERBATIM: it audits
whether the bundle fairly represents each extracted fact, and an audit against
a paraphrase would inherit the paraphrase's losses.

So this module records every extractor dispatch as it completes —
``.harvest/context/digest-NN.md``, dispatch brief + full returned digest —
from the two places a dispatch's result is observable:

* the ``subagent_io`` QuickJS shim (the fan-out path the prompts prescribe);
* ``steps.StepEmitter.on_tool_end`` (the static ``task`` path).

Both call :func:`record`, which no-ops for every other sub-agent type.
:func:`configure` pins the dataset root once per run (the capture sites don't
know it); recording is fail-soft everywhere — losing a digest degrades the
fidelity review, it must never break a dispatch. The runner wipes
``.harvest/context/`` at full-harvest start (like ``.harvest/review/``): a
digest from a PREVIOUS run describes context the current run re-extracts.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

#: The sub-agent type whose results are worth persisting.
EXTRACTOR_SUBAGENT = "context-extractor"

#: Root-relative home of the recorded digests (dot-dir: reader-invisible,
#: excluded from authoring/lint/reindex like the rest of ``.harvest/``).
CONTEXT_DIR = ".harvest/context"

_LOCK = threading.Lock()
_ROOT: Path | None = None


def configure(dataset_root: str | Path | None) -> None:
    """Pin (or clear, with None) the run's dataset root for :func:`record`.

    Called once from the agent builder; a process hosts one harvest run, so
    module state is the same lifetime the review loop already relies on.
    """
    global _ROOT
    with _LOCK:
        _ROOT = Path(dataset_root) if dataset_root is not None else None


def record(subagent_type: str | None, brief: str | None, digest: str | None) -> None:
    """Persist one completed dispatch — iff it was a context-extractor's.

    Never raises: the callers sit on dispatch hot paths (the QuickJS shim and
    the step feed) where an observability write must not cost the dispatch.
    """
    if subagent_type != EXTRACTOR_SUBAGENT:
        return
    text = (digest or "").strip()
    if not text:
        return  # an empty result records nothing reviewable
    try:
        with _LOCK:
            if _ROOT is None:
                return
            target = _ROOT / CONTEXT_DIR
            n = 1
            if target.is_dir():
                n = len(list(target.glob("digest-*.md"))) + 1
            path = target / f"digest-{n:02d}.md"
            body = [f"# Context extraction digest {n:02d}", ""]
            if brief and brief.strip():
                body += ["## Dispatch brief", "", brief.strip(), ""]
            body += ["## Digest", "", text, ""]
            # fsutil, not raw pathlib: these land on the S3 Files (NFS)
            # mount, where a one-off transient ESTALE/EIO would otherwise be
            # swallowed by the fail-soft contract below and silently shrink
            # the fidelity audit by one digest. write_text retries transients
            # and creates the parent dir.
            from harvest.fsutil import write_text

            write_text(path, "\n".join(body))
    except Exception:  # noqa: BLE001 — fail-soft by contract
        log.warning("Failed to record a context-extractor digest", exc_info=True)


def digest_paths(dataset_root: str | Path) -> list[str]:
    """Root-relative paths of the run's recorded digests, sorted (stable)."""
    target = Path(dataset_root) / CONTEXT_DIR
    if not target.is_dir():
        return []
    return sorted(
        f"{CONTEXT_DIR}/{p.name}" for p in target.glob("digest-*.md")
    )
