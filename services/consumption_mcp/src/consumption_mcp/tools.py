"""Pure tool logic for the consumption MCP server.

This module holds ALL the behaviour of the MCP tools as plain functions that
take injected boto3 clients (s3, s3vectors, bedrock-runtime, dynamodb). It has
**no dependency on FastMCP / mcp** so it can be unit-tested with moto + small
fakes without the agent-framework installed. ``server.py`` imports these and
wraps them in ``@mcp.tool()`` decorators.

The read side of OKF: the agent navigates the bundle via progressive disclosure
(``list_domains`` -> ``list_directory`` -> ``read_page``), follows the link
graph (``get_backlinks``), and jumps semantically (``semantic_search``). All of
these read the *bundle bucket* (the source of truth) for text; only
``semantic_search`` touches S3 Vectors, and only to get candidate concept ids +
their title/description/s3_key metadata — the agent then ``read_page``s the ones
it wants. Keeping the bulk markdown in S3 (never in vector metadata) is the
frozen storage decision (docs/CONVENTIONS.md, OKF_DESIGN §"What we store").
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from okf_aws.embeddings import (
    build_hierarchy_filter,
    embed_text,
    query_vectors,
)
from okf_aws.s3_bundle import bundle_prefix
from okf_core.document import OKFDocument, OKFDocumentError
from okf_core.domain import DOMAIN_DOC_TYPE, is_domain_dataset
from okf_core.links import extract_links_with_headings
from okf_core.paths import parse_concept_id

_BUNDLE_PREFIX = "okf/"
_RESERVED_FILES = {"index.md", "log.md"}

# DoS bounds on client-supplied search inputs (threats #13, #21).
_GREP_PATTERN_MAX_LEN = 1000  # reject absurdly long regexes outright
_SEMANTIC_TOP_K_MAX = 20  # cap fan-out per semantic_search (Titan embed + query)
_GREP_MAX_RESULTS_CAP = 1000  # hard ceiling on returned grep matches

# Catastrophic-backtracking heuristic (threat #21): the standard ``re`` engine is
# exponential on nested quantifiers like ``(a+)+$`` / ``(a*)*`` / ``(a+)*``. We
# cannot run re2 here (native bindings unavailable on the runtime) and a
# signal/thread timeout does not actually interrupt a running ``re.search`` in the
# FastMCP worker, so we REJECT the known-dangerous shape at the input boundary
# before compiling. This is a HEURISTIC, not a linear-time guarantee — the proper
# fix is a linear-time engine (re2); tracked as follow-up. It matches a quantifier
# applied to a group whose body itself ends in a quantifier, e.g. ``(...+)+``.
_NESTED_QUANTIFIER = re.compile(r"\([^()]*[+*}]\s*\)\s*[+*]|\([^()]*[+*]\)\s*\{")


def _validate_grep_pattern(pattern: str) -> None:
    """Reject client regexes that are overlong or prone to catastrophic backtracking.

    Raises ``ValueError`` (surfaced to the caller as a tool error) rather than
    letting a pathological pattern hang the shared FastMCP runtime. See threat #21.
    """
    if len(pattern) > _GREP_PATTERN_MAX_LEN:
        raise ValueError(
            f"regex too long ({len(pattern)} chars; max {_GREP_PATTERN_MAX_LEN})"
        )
    if _NESTED_QUANTIFIER.search(pattern):
        raise ValueError(
            "regex rejected: nested quantifiers (e.g. '(a+)+') can cause "
            "catastrophic backtracking; simplify the pattern"
        )


@dataclass
class ConsumptionConfig:
    """Runtime configuration resolved from env vars (see docs/CONVENTIONS.md).

    Passed explicitly into :class:`ConsumptionTools` so tests supply their own
    values and nothing reads process env at call time.
    """

    bundle_bucket: str
    vector_bucket: str
    vector_index: str
    registry_table: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ConsumptionConfig":
        env = env if env is not None else dict(os.environ)
        return cls(
            bundle_bucket=env["OKF_BUNDLE_BUCKET"],
            vector_bucket=env["OKF_VECTOR_BUCKET"],
            vector_index=env["OKF_VECTOR_INDEX"],
            registry_table=env.get("OKF_REGISTRY_TABLE", "okf-registry"),
        )


def _concept_s3_key(data_domain: str, dataset: str, concept_id: str) -> str:
    """S3 object key for a concept: ``okf/<domain>/<dataset>/<concept_id>.md``."""
    return f"{bundle_prefix(data_domain, dataset)}{concept_id}.md"


def _glob_to_regex(pattern: str) -> str:
    """Translate a shell-style glob to a regex with ``/``-aware wildcards.

    Matches the convention the agent's ``Glob`` tool uses (and does NOT rely on
    ``fnmatch.translate``, whose output format is a CPython implementation detail
    that has changed across versions):

    - ``*``   -> one path segment's worth of chars (``[^/]*``); never crosses ``/``
    - ``**/`` -> zero or more directories, so ``**/x`` matches ``x`` AND ``a/b/x``
    - ``**``  -> anything, including ``/``
    - ``?``   -> a single non-``/`` char
    - ``[seq]`` / ``[!seq]`` -> a character class within a segment

    Everything else is matched literally.
    """
    i, n = 0, len(pattern)
    out: list[str] = []
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                i += 2
                if i < n and pattern[i] == "/":  # "**/" => zero or more dirs
                    i += 1
                    out.append("(?:[^/]+/)*")
                else:  # bare "**" => anything, crossing "/"
                    out.append(".*")
            else:  # single "*" => within one segment
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pattern[j] in "!]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:  # unterminated class => literal "["
                out.append(re.escape("["))
                i += 1
            else:
                inner = pattern[i + 1 : j]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                out.append("[" + inner + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _glob_match(concept_id: str, pattern: str) -> bool:
    """True iff ``concept_id`` matches the shell-style ``pattern`` (``/``-aware)."""
    return re.fullmatch(_glob_to_regex(pattern), concept_id) is not None


def _is_within_prefix(key: str, prefix: str) -> bool:
    """True iff ``key`` normalises to a path still under ``prefix``.

    Guards against path traversal (``../``) in the concept_id / path arguments:
    a client-supplied ``concept_id`` like ``../../secrets`` must not let us read
    outside the dataset's bundle prefix. We normalise with posixpath semantics
    (collapsing ``.``/``..``) and require the result to still start with the
    dataset prefix.
    """
    # os.path.normpath collapses ".." segments. Use posix-style throughout.
    normalized = os.path.normpath(key)
    # normpath may strip a trailing slash; compare against the prefix sans slash
    # for containment, but require the boundary to be a real path segment.
    prefix_clean = prefix.rstrip("/")
    return normalized == prefix_clean or normalized.startswith(prefix_clean + "/")


class ConsumptionTools:
    """MCP tool implementations over injected clients.

    All clients are injected (never constructed here) so tests can pass moto
    resources / fakes and there are no live AWS calls in the unit suite.
    """

    def __init__(
        self,
        *,
        s3,
        s3vectors,
        bedrock_runtime,
        ddb,
        config: ConsumptionConfig,
    ):
        self.s3 = s3
        self.s3vectors = s3vectors
        self.bedrock_runtime = bedrock_runtime
        self.ddb = ddb  # a DynamoDB resource Table object (boto3 resource style)
        self.config = config

    # -- list_domains ----------------------------------------------------

    def list_domains(self) -> list[dict[str, Any]]:
        """Registered ``(data_domain, dataset)`` pairs from ``okf-registry``.

        Domain mapping items are ``pk="DOMAIN#<data_domain>",
        sk="DATASET#<dataset>"`` (docs/CONVENTIONS.md). We query ``pk
        begins_with "DOMAIN#"``; the boto3 resource ``Table`` does not support a
        begins_with on the *partition* key, so we use a ``scan`` with a filter
        (fine at demo scale) — the registry is tiny.

        Filters out the ``_domain`` pseudo-dataset (the domain's concept doc) and
        enriches each result with the declared domain's description (if available).
        """
        from boto3.dynamodb.conditions import Attr

        mappings: list[dict[str, Any]] = []
        meta_by_domain: dict[str, dict[str, str]] = {}
        kwargs: dict[str, Any] = {
            "FilterExpression": Attr("pk").begins_with("DOMAIN#"),
        }
        while True:
            resp = self.ddb.scan(**kwargs)
            for item in resp.get("Items", []):
                sk = item.get("sk", "")
                if sk == "META":
                    domain = item.get("data_domain", "")
                    meta_by_domain[domain] = {
                        "description": item.get("description", ""),
                        "context": item.get("context", ""),
                    }
                elif sk.startswith("DATASET#"):
                    ds = item.get("dataset", "")
                    # Hide the _domain pseudo-dataset from the listing.
                    if is_domain_dataset(ds):
                        continue
                    mappings.append(
                        {
                            "data_domain": item.get("data_domain", ""),
                            "dataset": ds,
                        }
                    )
            token = resp.get("LastEvaluatedKey")
            if not token:
                break
            kwargs["ExclusiveStartKey"] = token

        # Enrich each mapping with domain-level description.
        for m in mappings:
            domain = m["data_domain"]
            meta = meta_by_domain.get(domain)
            if meta:
                m["domain_description"] = meta.get("description", "")

        return mappings

    # -- list_declared_domains ----------------------------------------------

    def list_declared_domains(self) -> list[dict[str, Any]]:
        """Return all declared domains (DOMAIN#/META rows) with description + context.

        Exposes the operator-declared domain catalog so an agent can discover
        which domains exist and what they cover before drilling into datasets.
        """
        from boto3.dynamodb.conditions import Attr

        out: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "FilterExpression": Attr("pk").begins_with("DOMAIN#")
            & Attr("sk").eq("META"),
        }
        while True:
            resp = self.ddb.scan(**kwargs)
            for item in resp.get("Items", []):
                out.append(
                    {
                        "data_domain": item.get("data_domain", ""),
                        "description": item.get("description", ""),
                        "context": item.get("context", ""),
                    }
                )
            token = resp.get("LastEvaluatedKey")
            if not token:
                break
            kwargs["ExclusiveStartKey"] = token
        out.sort(key=lambda x: x.get("data_domain", ""))
        return out

    # -- search_domains -----------------------------------------------------

    def search_domains(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Semantic search over declared domain descriptions/context.

        Wraps :meth:`semantic_search` with ``type="Domain"`` so the agent can
        discover which domain best matches a natural-language question before
        drilling into its datasets. Returns the same shape as ``semantic_search``.
        """
        return self.semantic_search(query, type=DOMAIN_DOC_TYPE, top_k=top_k)

    # -- list_directory --------------------------------------------------

    def list_directory(
        self, data_domain: str, dataset: str, path: str = ""
    ) -> dict[str, Any]:
        """Progressive disclosure: return the ``index.md`` at a subtree level.

        Reads ``okf/<domain>/<dataset>/<path>/index.md``. If that index is
        missing, falls back to listing the S3 "directory" — the immediate child
        common prefixes and ``.md`` objects at that level — so the agent can
        still navigate.
        """
        prefix = bundle_prefix(data_domain, dataset)
        # Build the directory prefix (path may be "" for the bundle root).
        sub = path.strip("/")
        dir_prefix = f"{prefix}{sub}/" if sub else prefix
        index_key = f"{dir_prefix}index.md"

        # Reject traversal outside the dataset bundle prefix.
        if not _is_within_prefix(index_key, prefix):
            raise ValueError(f"path escapes bundle prefix: {path!r}")

        try:
            obj = self.s3.get_object(Bucket=self.config.bundle_bucket, Key=index_key)
            text = obj["Body"].read().decode("utf-8")
            return {
                "data_domain": data_domain,
                "dataset": dataset,
                "path": sub,
                "index_key": index_key,
                "content": text,
            }
        except Exception:  # noqa: BLE001 - missing index => list the prefix
            entries = self._list_prefix(dir_prefix)
            return {
                "data_domain": data_domain,
                "dataset": dataset,
                "path": sub,
                "index_key": index_key,
                "content": None,
                "entries": entries,
            }

    def _list_prefix(self, dir_prefix: str) -> list[dict[str, str]]:
        """One level of the S3 "directory" at ``dir_prefix`` (delimiter=/).

        Returns child prefixes (as ``dir`` entries, concept-id relative) and
        ``.md`` objects (as ``page`` entries), skipping reserved and dot-prefixed
        entries so ``.harvest``/``.context``/``index.md``/``log.md`` never show.
        """
        entries: list[dict[str, str]] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self.config.bundle_bucket, Prefix=dir_prefix, Delimiter="/"
        ):
            for cp in page.get("CommonPrefixes", []):
                child = cp["Prefix"][len(dir_prefix) :].rstrip("/")
                if not child or child.startswith("."):
                    continue
                entries.append({"type": "dir", "name": child})
            for obj in page.get("Contents", []):
                name = obj["Key"][len(dir_prefix) :]
                if not name or "/" in name:
                    continue  # not an immediate child file
                if name.startswith(".") or name in _RESERVED_FILES:
                    continue
                if not name.endswith(".md"):
                    continue
                entries.append({"type": "page", "name": name[: -len(".md")]})
        entries.sort(key=lambda e: (e["type"], e["name"]))
        return entries

    # -- read_page -------------------------------------------------------

    def read_page(
        self,
        concept_id: str,
        data_domain: str,
        dataset: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Return a concept's markdown from S3, optionally paginated by lines.

        ``offset``/``limit`` are line-based (0-indexed offset) so an agent can
        page through a very large table doc without pulling it all into context
        at once. Validates the resolved key stays under the dataset bundle
        prefix (path-traversal guard).
        """
        if offset < 0:
            raise ValueError("offset must be >= 0")
        prefix = bundle_prefix(data_domain, dataset)
        # Validate the id up front: parse_concept_id rejects any segment that
        # is not a clean path segment (so ``..``, ``../x``, absolute-ish forms
        # all fail here before we build an S3 key).
        parse_concept_id(concept_id)
        key = _concept_s3_key(data_domain, dataset, concept_id)
        if not _is_within_prefix(key, prefix):
            raise ValueError(f"concept_id escapes bundle prefix: {concept_id!r}")

        obj = self.s3.get_object(Bucket=self.config.bundle_bucket, Key=key)
        text = obj["Body"].read().decode("utf-8")

        lines = text.splitlines()
        total_lines = len(lines)
        paginated = offset > 0 or limit is not None
        if paginated:
            end = len(lines) if limit is None else offset + max(limit, 0)
            selected = lines[offset:end]
            content = "\n".join(selected)
            returned = len(selected)
        else:
            content = text
            returned = total_lines

        return {
            "concept_id": concept_id,
            "data_domain": data_domain,
            "dataset": dataset,
            "s3_key": key,
            "content": content,
            "offset": offset,
            "limit": limit,
            "total_lines": total_lines,
            "returned_lines": returned,
        }

    # -- get_backlinks ---------------------------------------------------

    def get_backlinks(
        self, concept_id: str, data_domain: str, dataset: str
    ) -> list[dict[str, str]]:
        """Concepts in the dataset subtree that link *to* ``concept_id``.

        Reuses ``okf_core.links.extract_links_with_headings`` (the same link
        resolver the harvest agent uses) so consumption and authoring agree on
        what counts as a backlink. We download the dataset subtree's ``.md``
        files into a temp dir, then resolve each doc's links relative to the
        bundle root; any doc whose resolved links include ``concept_id`` is a
        backlink. The heading is the section in the *referencing* doc where the
        link sits, so the agent knows where the reference lives.
        """
        prefix = bundle_prefix(data_domain, dataset)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = self._download_subtree(prefix, root)
            out: list[dict[str, str]] = []
            for source_id, doc_path in sorted(docs.items()):
                body = doc_path.read_text(encoding="utf-8")
                # Resolve links against the frontmatter-stripped body so a
                # link inside the YAML block never counts; OKFDocument gives us
                # the body plus the title for the result.
                try:
                    doc = OKFDocument.parse(body)
                    scan_body = doc.body or ""
                    title = str((doc.frontmatter or {}).get("title") or source_id)
                except OKFDocumentError:
                    scan_body = body
                    title = source_id
                links = extract_links_with_headings(scan_body, doc_path.parent, root)
                for link in links:
                    if link.target == concept_id:
                        out.append(
                            {
                                "id": source_id,
                                "title": title,
                                "heading": link.heading,
                            }
                        )
                        break
        return out

    def _iter_concepts(self, prefix: str) -> Iterator[tuple[str, str]]:
        """Yield ``(concept_id, s3_key)`` for every visible concept under ``prefix``.

        "Visible" == the exact harvest-time subtree scope: a ``.md`` object whose
        relative path is neither a reserved file (index.md / log.md) nor contains
        any dot-prefixed segment (.harvest/.context). This is the single place the
        traversal + visibility rules live; ``_download_subtree``, ``glob`` and
        ``grep`` all build on it so they agree on exactly which pages exist.
        """
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.config.bundle_bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".md"):
                    continue
                rel = key[len(prefix) :]  # concept path incl. .md
                if not rel:
                    continue
                parts = rel.split("/")
                if parts[-1] in _RESERVED_FILES:
                    continue
                if any(seg.startswith(".") for seg in parts):
                    continue
                yield rel[: -len(".md")], key

    def _download_subtree(self, prefix: str, root: Path) -> dict[str, Path]:
        """Download every concept ``.md`` under ``prefix`` into ``root``.

        Returns ``{concept_id: local_path}``. Scope (reserved + dot-prefixed
        segments skipped) is defined once in :meth:`_iter_concepts` so the link
        graph matches the harvest-time subtree exactly.
        """
        docs: dict[str, Path] = {}
        for concept_id, key in self._iter_concepts(prefix):
            rel = key[len(prefix) :]
            body = self.s3.get_object(Bucket=self.config.bundle_bucket, Key=key)[
                "Body"
            ].read()
            local = root / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(body)
            docs[concept_id] = local
        return docs

    # -- glob ------------------------------------------------------------

    def glob(
        self, pattern: str, data_domain: str, dataset: str
    ) -> list[dict[str, str]]:
        """Concept ids in the dataset subtree whose path matches ``pattern``.

        The counterpart to ``list_directory`` for when the agent knows the shape
        of a name but not its location: match concept *paths* (the id, e.g.
        ``tables/races``) across the whole subtree with shell-style wildcards.
        ``*`` does not cross ``/`` and ``**`` matches across directories, mirroring
        the ``Glob`` tool the agent already knows:

        - ``tables/*``    -> direct children of tables/
        - ``**/*orders*`` -> any concept whose leaf name contains "orders"
        - ``*``           -> top-level concepts only

        Reserved (index.md/log.md) and dot-prefixed (.harvest/.context) paths are
        never matched — same scope as ``get_backlinks``. Results are sorted.
        """
        prefix = bundle_prefix(data_domain, dataset)
        # Normalise the pattern the same way concept ids are (strip wrapping
        # slashes) so a leading "/" or ".md" suffix in the pattern still matches.
        pat = pattern.strip("/")
        if pat.endswith(".md"):
            pat = pat[: -len(".md")]
        matches = [
            concept_id
            for concept_id, _key in self._iter_concepts(prefix)
            if _glob_match(concept_id, pat)
        ]
        matches.sort()
        return [
            {"concept_id": cid, "data_domain": data_domain, "dataset": dataset}
            for cid in matches
        ]

    # -- grep ------------------------------------------------------------

    def grep(
        self,
        pattern: str,
        data_domain: str,
        dataset: str,
        ignore_case: bool = True,
        max_results: int = 100,
    ) -> dict[str, Any]:
        """Regex search over concept *contents* — the keyword peer of semantic_search.

        Scans every visible page in the dataset subtree and returns each matching
        line with its concept id, 1-indexed line number, and the (stripped) line
        text — like ``Grep`` in content mode. ``pattern`` is a Python regex;
        ``ignore_case`` defaults on. Results are capped at ``max_results`` (sorted
        by concept id then line number) and the response flags whether the cap was
        hit so the agent can narrow the query. Use this for exact tokens (a column
        name, an enum value, a table name); use ``semantic_search`` for meaning.
        """
        if max_results <= 0:
            raise ValueError("max_results must be >= 1")
        # Bound the result set so a client can't request an unbounded scan dump.
        max_results = min(max_results, _GREP_MAX_RESULTS_CAP)
        # DoS guard (threat #21): reject overlong / catastrophic-backtracking
        # patterns BEFORE compiling, since re is exponential on nested quantifiers
        # and we can neither run re2 nor interrupt a hung re.search here.
        _validate_grep_pattern(pattern)
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            raise ValueError(f"invalid regex pattern: {exc}") from exc

        prefix = bundle_prefix(data_domain, dataset)
        results: list[dict[str, Any]] = []
        truncated = False
        for concept_id, key in sorted(self._iter_concepts(prefix)):
            obj = self.s3.get_object(Bucket=self.config.bundle_bucket, Key=key)
            text = obj["Body"].read().decode("utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    if len(results) >= max_results:
                        truncated = True
                        break
                    results.append(
                        {
                            "concept_id": concept_id,
                            "line_number": lineno,
                            "line": line.strip(),
                        }
                    )
            if truncated:
                break
        return {
            "data_domain": data_domain,
            "dataset": dataset,
            "pattern": pattern,
            "matches": results,
            "match_count": len(results),
            "truncated": truncated,
        }

    # -- semantic_search -------------------------------------------------

    def semantic_search(
        self,
        query: str,
        data_domain: str | None = None,
        dataset: str | None = None,
        table: str | None = None,
        type: str | None = None,  # noqa: A002 - matches the MCP tool param name
        tags: list[str] | None = None,
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """Embed ``query`` (Titan V2) and query S3 Vectors with a hierarchy filter.

        Returns candidate concepts ranked by cosine distance. Each result is the
        vector's key (which is the deterministic concept path = ``<domain>/
        <dataset>/<concept_id>``) plus the non-filterable metadata the reindex
        worker stored: ``title``, ``description``, ``s3_key``. The agent then
        ``read_page``s the ones it wants — we never stuff bulk markdown into the
        vector store.

        ``top_k`` is clamped server-side to ``[1, _SEMANTIC_TOP_K_MAX]`` so a
        client can't drive an oversized fan-out (each call is a Titan V2 embed +
        an S3 Vectors query — a cost/throttle DoS lever, threat #13).
        """
        top_k = max(1, min(int(top_k), _SEMANTIC_TOP_K_MAX))
        embedding = embed_text(self.bedrock_runtime, query)
        metadata_filter = build_hierarchy_filter(
            data_domain=data_domain,
            dataset=dataset,
            table=table,
            type_=type,
            tags=tags,
        )
        hits = query_vectors(
            self.s3vectors,
            vector_bucket=self.config.vector_bucket,
            index_name=self.config.vector_index,
            query_embedding=embedding,
            top_k=top_k,
            metadata_filter=metadata_filter,
        )
        results: list[dict[str, Any]] = []
        for hit in hits:
            md = hit.get("metadata", {}) or {}
            results.append(
                {
                    "concept_id": hit.get("key"),
                    "title": md.get("title", ""),
                    "description": md.get("description", ""),
                    "s3_key": md.get("s3_key", ""),
                    "distance": hit.get("distance"),
                }
            )
        return results
