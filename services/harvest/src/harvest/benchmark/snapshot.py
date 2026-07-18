"""Bundle-only snapshot — the physical basis of the solver's bundle-blindness.

The benchmark solver simulates the real consumer, whose only knowledge source is
the wiki. If it could read the raw Glue schema (``.metadata/``), the uploaded
source docs (``.context/``), or anything gold, it would answer from the raw data
and the score would measure schema access, not wiki quality. A read-denylist
can't guarantee this — ``grep``/``glob`` scan the tree and can surface denied
content in their results without naming the denied dir (the authoring agent
already ``grep``s ``.metadata/``). So instead we give the solver a filesystem that
*physically lacks* everything it may not see.

:func:`snapshot_bundle` copies ONLY the authored bundle — the top-level entries
that do NOT start with ``.`` (``datasets/``, ``tables/``, ``references/``,
``index.md``, …) — into a fresh temp dir. Dot-prefixed inputs (``.metadata/``,
``.context/``, ``.benchmark/``, ``.harvest/``) are excluded by the same rule
``clean_authored_output`` uses to decide what is authored output. The solver's
``FilesystemBackend`` is then rooted at the snapshot, so it cannot reach anything
outside it however its read tools are called.

Copying (KBs of markdown) also pins the solver to a consistent view for the round
while the supervisor may still be editing the live tree.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def _is_authored(name: str) -> bool:
    """Authored output = a top-level entry whose name does NOT start with '.'.

    Mirrors ``fsutil.clean_authored_output``'s rule, so the snapshot contains
    exactly what a full harvest would (re)author and nothing dot-prefixed.
    """
    return not name.startswith(".")


def snapshot_bundle(dataset_root: str | Path, dest_root: str | Path) -> Path:
    """Copy the authored bundle from ``dataset_root`` into ``dest_root``.

    Only non-dot top-level entries are copied (files and directories, recursively).
    ``dest_root`` is created if absent and must be a fresh/empty dir the caller
    owns (typically a ``tempfile.mkdtemp()``). Returns the ``dest_root`` path.

    Symlinks are NOT followed into their targets (``copytree`` copies the link's
    contents by default here we materialize files) — the bundle is plain markdown,
    so there are no symlinks to worry about; a stray one is copied as a regular
    file to avoid escaping the snapshot.
    """
    src = Path(dataset_root)
    dest = Path(dest_root)
    dest.mkdir(parents=True, exist_ok=True)

    if not src.exists():
        return dest

    for entry in sorted(src.iterdir()):
        if not _is_authored(entry.name):
            continue
        target = dest / entry.name
        if entry.is_dir():
            # symlinks=False materializes real files; ignore any nested dotfiles
            # too (defense in depth — a dot-dir nested under an authored dir).
            shutil.copytree(
                entry,
                target,
                symlinks=False,
                ignore=shutil.ignore_patterns(".*"),
            )
        elif entry.is_file():
            shutil.copy2(entry, target)
    return dest
