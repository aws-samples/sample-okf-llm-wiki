"""Link-graph ``{nodes, edges}`` JSON for the UI — ONE builder, two producers.

The harvest precomputes this at ``finalize_bundle`` time into
``.harvest/graph.json``, stamped with the SAME ``completed_at`` the commit
marker carries — so the Control API's ``GET /bundle/{d}/{ds}/graph`` treats
the artifact as fresh iff the two timestamps match, and every path that
mutates the bundle (full/scoped/annotation/cross harvests via
``finalize_bundle``, repromote in the Control API) refreshes it along with
the marker. When the artifact is missing or stale (mid-harvest, legacy
bundle, failed precompute), the endpoint rebuilds the same JSON on the fly
from the live docs. Both paths call :func:`build_graph_json`, so precomputed
and live graphs are identical by construction.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from okf_core.links import extract_links_with_headings
from okf_core.paths import is_reserved_rel_segments

# Bundle-relative path of the precomputed artifact (under the run-state dot
# dir, next to state.json — derived data, never a concept doc).
GRAPH_ARTIFACT_REL = ".harvest/graph.json"

_RESERVED_FILES = ("index.md", "log.md")


def build_graph_json(files: dict[str, str]) -> dict[str, Any]:
    """Build ``{nodes, edges}`` link-graph JSON for the UI from concept docs.

    ``files`` maps concept id (e.g. ``tables/races``) -> raw markdown text. We
    materialize the docs into a temp dir preserving structure, then reuse
    :func:`okf_core.links.extract_links_with_headings` (the exact resolver the
    harvest agent and viewer use) so link resolution is identical everywhere.
    Edges whose target is not itself a known concept are dropped.

    * nodes: ``{id, title, type}`` (title/type from YAML frontmatter, best effort)
    * edges: ``{source, target}`` for each resolved intra-bundle link
    """
    from okf_core.document import OKFDocument, OKFDocumentError

    node_ids = set(files.keys())
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Write each concept doc to <root>/<concept_id>.md, creating parent dirs.
        for concept_id, text in files.items():
            path = root / f"{concept_id}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

        for concept_id in sorted(files):
            text = files[concept_id]
            title = concept_id
            type_ = "Unknown"
            body = text
            try:
                doc = OKFDocument.parse(text)
                fm = doc.frontmatter or {}
                title = str(fm.get("title") or concept_id)
                type_ = str(fm.get("type") or "Unknown")
                body = doc.body or ""
            except (OKFDocumentError, Exception) as e:  # noqa: BLE001 - tolerate malformed docs
                del e  # keep title/type defaults; a bad doc still becomes a node
            nodes.append({"id": concept_id, "title": title, "type": type_})

            doc_dir = (root / f"{concept_id}.md").parent
            for link in extract_links_with_headings(body, doc_dir, root):
                if link.target in node_ids:
                    edges.append({"source": concept_id, "target": link.target})

    return {"nodes": nodes, "edges": edges}


def collect_bundle_files(root: str | Path) -> dict[str, str]:
    """Read a local bundle dir into the ``{concept_id: text}`` map
    :func:`build_graph_json` takes.

    Applies the same "what is a concept" rules as the S3 side
    (``okf_aws.parse_bundle_key``) and the link graph: ``.md`` only, skip
    ``index.md``/``log.md``, skip anything under a dot-prefixed or
    internal-scratch dir, skip dot-prefixed stems. The two producers must
    agree, or the precomputed graph and the live fallback would differ.

    A per-file read error PROPAGATES: this runs at finalize time on a bundle
    that is not moving, and its output gets stamped as the authoritative
    graph for the commit — silently skipping a doc would bake a permanently
    incomplete artifact that passes the freshness check. The caller's
    best-effort boundary (finalize's try/except) turns the failure into
    "no artifact", which the endpoint correctly answers by computing live.
    """
    root = Path(root)
    files: dict[str, str] = {}
    if not root.is_dir():
        return files
    for md_path in sorted(root.rglob("*.md")):
        if md_path.name in _RESERVED_FILES:
            continue
        rel = md_path.relative_to(root).with_suffix("")
        if is_reserved_rel_segments(rel.parts[:-1]) or rel.parts[-1].startswith("."):
            continue
        files["/".join(rel.parts)] = md_path.read_text(encoding="utf-8")
    return files
