"""Regenerate ``index.md`` files for a bundle subtree.

Ported from the reference producer. Each directory gets an ``index.md`` grouping
its children by ``type`` for progressive disclosure. Directory descriptions are
synthesized by an optional callable; the default is a deterministic fallback so
this runs offline (the harvest agent can pass a Bedrock-backed synthesizer).
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Callable

from okf_core.document import OKFDocument

_INDEX_FILE = "index.md"


def _load_doc(path: Path) -> OKFDocument | None:
    try:
        return OKFDocument.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _build_index_text(entries: list[tuple[str, str, str, str]]) -> str:
    # entries: (type, title, relative_link, description)
    grouped: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for typ, title, link, desc in entries:
        grouped[typ or "Other"].append((title, link, desc))

    sections: list[str] = []
    for typ in sorted(grouped):
        lines = [f"# {typ}", ""]
        for title, link, desc in sorted(grouped[typ], key=lambda e: e[0].lower()):
            suffix = f" - {desc}" if desc else ""
            lines.append(f"* [{title}]({link}){suffix}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"


# Directory names that are never OKF concept dirs and must not get an index.md
# or contribute entries: dot-prefixed reserved dirs (.harvest, .context) and
# deepagents' internal scratch dirs (defense-in-depth in case any leak to disk).
_INTERNAL_DIRS = {"large_tool_results", "conversation_history"}


def _is_ignored_rel(bundle_root: Path, path: Path) -> bool:
    """True if any path segment below the bundle root is reserved/internal."""
    try:
        rel = path.relative_to(bundle_root)
    except ValueError:
        return False
    return any(seg.startswith(".") or seg in _INTERNAL_DIRS for seg in rel.parts)


def _directories_to_index(bundle_root: Path) -> list[Path]:
    dirs: set[Path] = set()
    for md in bundle_root.rglob("*.md"):
        if _is_ignored_rel(bundle_root, md.parent):
            continue
        cur = md.parent
        while cur != bundle_root.parent:
            dirs.add(cur)
            if cur == bundle_root:
                break
            cur = cur.parent
    return sorted(dirs)


def _fallback_synth(rel_path: str, children: list[tuple[str, str]]) -> str:
    titles = ", ".join(t for t, _ in children if t) or "no titled entries"
    return f"Contains {len(children)} entries: {titles}."


def regenerate_indexes(
    bundle_root: Path,
    *,
    synthesize: Callable[[str, list[tuple[str, str]]], str] | None = None,
) -> list[Path]:
    """(Re)write every directory's ``index.md``. Returns the paths written.

    ``synthesize(rel_path, [(title, desc), ...]) -> str`` produces the one-line
    directory description; when omitted a deterministic listing is used.
    """
    bundle_root = Path(bundle_root)
    synth = synthesize or _fallback_synth
    written: list[Path] = []
    if not bundle_root.exists():
        return written

    # Deepest directories first, so a parent can reuse a child's description.
    directories = sorted(
        _directories_to_index(bundle_root),
        key=lambda p: (-len(p.relative_to(bundle_root).parts), str(p)),
    )

    dir_descriptions: dict[Path, str] = {}

    for directory in directories:
        entries: list[tuple[str, str, str, str]] = []
        for child in sorted(directory.iterdir()):
            if child.name == _INDEX_FILE:
                continue
            if child.is_file() and child.suffix == ".md":
                doc = _load_doc(child)
                if doc is None:
                    continue
                fm = doc.frontmatter
                title = str(fm.get("title") or child.stem)
                desc = str(fm.get("description") or "")
                typ = str(fm.get("type") or "")
                entries.append((typ, title, child.name, desc))
            elif child.is_dir():
                # Skip reserved (.harvest/.context) and internal scratch dirs so
                # they never appear as bundle entries.
                if _is_ignored_rel(bundle_root, child):
                    continue
                desc = dir_descriptions.get(child, "")
                entries.append(
                    ("Subdirectories", child.name, f"{child.name}/{_INDEX_FILE}", desc)
                )

        if not entries:
            continue

        index_path = directory / _INDEX_FILE
        index_path.write_text(_build_index_text(entries), encoding="utf-8")
        written.append(index_path)

        if directory == bundle_root:
            continue

        pairs = [(title, desc) for _, title, _, desc in entries]
        if len(pairs) == 1 and pairs[0][1]:
            dir_descriptions[directory] = pairs[0][1]
        else:
            rel = str(directory.relative_to(bundle_root))
            dir_descriptions[directory] = synth(rel, pairs)

    return written
