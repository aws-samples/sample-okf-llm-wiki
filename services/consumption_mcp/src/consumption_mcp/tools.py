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

import base64
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
from okf_core.wiki_primer import MCP_PRIMER

_BUNDLE_PREFIX = "okf/"
_RESERVED_FILES = {"index.md", "log.md"}

# DoS bounds on client-supplied search inputs (threats #13, #21).
_GREP_PATTERN_MAX_LEN = 1000  # reject absurdly long regexes outright
_SEMANTIC_TOP_K_MAX = 20  # cap fan-out per semantic_search (Titan embed + query)

# get_bundle_diff output bounds (agent context is the scarce resource): at most
# this many per-file entries, each diff capped in lines. Both surface honest
# `truncated` / `diff_truncated` flags so the agent can narrow, mirroring grep.
_DIFF_MAX_FILES_CAP = 50
_DIFF_MAX_LINES_PER_FILE = 100
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

    # -- read_me ---------------------------------------------------------

    def read_me(self) -> str:
        """The wiki-usage primer (static; ``okf_core.wiki_primer.MCP_PRIMER``).

        Structure, trap locations, and the navigation moves that work — served
        as a tool so an agent's FIRST call orients it instead of it re-deriving
        the layout by trial and error (and never touching ``get_backlinks``).
        """
        return MCP_PRIMER

    # -- list_domains ----------------------------------------------------
    # (pagination + xref helpers for it sit at module level, below the class)

    def list_domains(
        self,
        domain: str | None = None,
        query: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Pages of the ``(data_domain, dataset)`` pairs you can read.

        Returns ``{"datasets": [...], "next_cursor": ...}`` — pass a non-null
        ``next_cursor`` back verbatim for the next page (~``limit`` each).
        ``domain`` narrows to one data domain; ``query`` is a case-insensitive
        substring over ``"<domain>/<dataset>"``. Prefer ``search_domains`` /
        ``semantic_search`` to FIND a dataset by meaning.

        Each entry carries its domain's description plus the cross-dataset
        signal: ``cross_references`` — datasets this one holds pair docs FOR
        (read them under this dataset's ``external/<d>/<ds>/``);
        ``cross_referenced_by`` — the counterpart holds pair docs about THIS
        one (read them under ``<that dataset>/external/<this>/…``).
        """
        # Implementation notes (deliberately not in the model-facing docstring):
        #
        # Mapping rows are pk="DOMAIN#<d>", sk="DATASET#<ds>" and carry the
        # by-entity GSI keys (entity="dataset", pair="<d>/<ds>" — CONVENTIONS.md
        # "Registry entity index"), so the catalog is a QUERY, never a Scan (a
        # Scan reads the whole table — harvest status + report rows included —
        # and grows with usage, not with datasets). A `domain` filter queries
        # the base-table partition directly. The index is consulted ONLY once
        # the backfill's readiness marker says every row is stamped
        # (okf_aws.registry_entity) — a partially-stamped registry would Query
        # back a partial catalog. No marker → the old full scan: unpaginated,
        # exactly the pre-index behavior.
        #
        # `limit` is a SOFT cap: whole result pages are consumed, so a reply
        # can run a few entries over. The `_domain` pseudo-dataset is hidden.
        #
        # Cross-dataset references: pair docs live in ONE bundle (the
        # initiating dataset's), so the counterpart's own bundle never reveals
        # them; reindex-derived XREF# rows (entity="xref") surface both
        # directions here, fetched in one small GSI query (xref rows are few).
        from boto3.dynamodb.conditions import Key

        from okf_aws import registry_entity

        limit = max(1, min(int(limit or 100), 500))
        needle = (query or "").strip().lower()
        start_key = _decode_cursor(cursor)

        mappings: list[dict[str, Any]] = []
        next_key: dict[str, Any] | None = None
        legacy = not registry_entity.entity_index_ready(self.ddb, resource=True)
        # Limit each DDB request too: mapping rows are tiny, so without it a
        # single 1MB result page holds THOUSANDS of rows and the soft cap
        # would never bite (the whole catalog would return on page one).
        page_kwargs: dict[str, Any] = {"Limit": limit}
        if start_key:
            page_kwargs["ExclusiveStartKey"] = start_key
        if not legacy:
            try:
                while True:
                    if domain:
                        resp = self.ddb.query(
                            KeyConditionExpression=Key("pk").eq(f"DOMAIN#{domain}")
                            & Key("sk").begins_with("DATASET#"),
                            **page_kwargs,
                        )
                    else:
                        resp = self.ddb.query(
                            IndexName=registry_entity.INDEX_NAME,
                            KeyConditionExpression=Key("entity").eq(
                                registry_entity.ENTITY_DATASET
                            ),
                            **page_kwargs,
                        )
                    for item in resp.get("Items", []):
                        ds = item.get("dataset", "")
                        dd = item.get("data_domain", "")
                        if is_domain_dataset(ds):
                            continue
                        if needle and needle not in f"{dd}/{ds}".lower():
                            continue
                        mappings.append({"data_domain": dd, "dataset": ds})
                    lek = resp.get("LastEvaluatedKey")
                    if not lek:
                        next_key = None
                        break
                    if len(mappings) >= limit:
                        next_key = lek
                        break
                    page_kwargs["ExclusiveStartKey"] = lek
            except Exception as e:  # noqa: BLE001 - triaged below, most re-raise
                # ONLY "the index does not exist" may fall back to the scan
                # (the marker was stamped before the terraform apply). Any
                # other failure — a throttle mid-pagination, a cursor replayed
                # against a different query shape — must SURFACE: a silent
                # scan here would hand the caller the whole catalog again with
                # next_cursor=null, duplicating the pages it already consumed.
                if not registry_entity.is_missing_index_error(e):
                    raise
                legacy = True
                mappings = []

        if legacy:
            mappings, meta_by_domain, references, referenced_by = (
                self._scan_domains_legacy()
            )
            if domain:
                mappings = [m for m in mappings if m["data_domain"] == domain]
            if needle:
                mappings = [
                    m
                    for m in mappings
                    if needle in f"{m['data_domain']}/{m['dataset']}".lower()
                ]
            next_key = None
        else:
            meta_by_domain = self._domain_meta()
            references, referenced_by = self._xref_maps()

        # Enrich each mapping with domain-level description + the cross signal.
        for m in mappings:
            dom = m["data_domain"]
            meta = meta_by_domain.get(dom)
            if meta:
                m["domain_description"] = meta.get("description", "")
            pair_key = (dom, m["dataset"])
            if pair_key in references:
                m["cross_references"] = sorted(references[pair_key])
            if pair_key in referenced_by:
                m["cross_referenced_by"] = sorted(referenced_by[pair_key])

        return {"datasets": mappings, "next_cursor": _encode_cursor(next_key)}

    def _scan_domains_legacy(self):
        """The pre-index full listing (one filtered scan): the fallback path."""
        from boto3.dynamodb.conditions import Attr

        mappings: list[dict[str, Any]] = []
        meta_by_domain: dict[str, dict[str, str]] = {}
        references: dict[tuple[str, str], set[str]] = {}
        referenced_by: dict[tuple[str, str], set[str]] = {}
        kwargs: dict[str, Any] = {
            "FilterExpression": Attr("pk").begins_with("DOMAIN#"),
        }
        while True:
            resp = self.ddb.scan(**kwargs)
            for item in resp.get("Items", []):
                sk = item.get("sk", "")
                if sk == "META":
                    dom = item.get("data_domain", "")
                    meta_by_domain[dom] = {
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
                elif sk.startswith("XREF#"):
                    _fold_xref(item, references, referenced_by)
            token = resp.get("LastEvaluatedKey")
            if not token:
                break
            kwargs["ExclusiveStartKey"] = token
        return mappings, meta_by_domain, references, referenced_by

    def _domain_meta(self) -> dict[str, dict[str, str]]:
        """Every declared domain's META blurb, in ONE indexed query.

        Domains are few (one small ``entity="domain"`` partition), so a single
        Query replaces the serial per-domain GetItems a page used to pay.
        Best-effort: a failed read just drops the blurbs.
        """
        from boto3.dynamodb.conditions import Key

        from okf_aws import registry_entity

        out: dict[str, dict[str, str]] = {}
        try:
            kwargs: dict[str, Any] = {
                "IndexName": registry_entity.INDEX_NAME,
                "KeyConditionExpression": Key("entity").eq(
                    registry_entity.ENTITY_DOMAIN
                ),
            }
            while True:
                resp = self.ddb.query(**kwargs)
                for item in resp.get("Items", []):
                    dom = item.get("data_domain", "")
                    if dom:
                        out[dom] = {
                            "description": item.get("description", ""),
                            "context": item.get("context", ""),
                        }
                token = resp.get("LastEvaluatedKey")
                if not token:
                    break
                kwargs["ExclusiveStartKey"] = token
        except Exception:  # noqa: BLE001 - enrichment only
            pass
        return out

    def _xref_maps(self):
        """All cross-reference rows via the by-entity GSI (xref rows are few).

        Best-effort: on a legacy deployment without the index the signal is
        simply absent from paged replies (the full-scan fallback still carries
        it).
        """
        from boto3.dynamodb.conditions import Key

        from okf_aws import registry_entity

        references: dict[tuple[str, str], set[str]] = {}
        referenced_by: dict[tuple[str, str], set[str]] = {}
        try:
            kwargs: dict[str, Any] = {
                "IndexName": registry_entity.INDEX_NAME,
                "KeyConditionExpression": Key("entity").eq(
                    registry_entity.ENTITY_XREF
                ),
            }
            while True:
                resp = self.ddb.query(**kwargs)
                for item in resp.get("Items", []):
                    _fold_xref(item, references, referenced_by)
                token = resp.get("LastEvaluatedKey")
                if not token:
                    break
                kwargs["ExclusiveStartKey"] = token
        except Exception:  # noqa: BLE001 - enrichment only
            pass
        return references, referenced_by

    # -- list_declared_domains ----------------------------------------------

    def list_declared_domains(self) -> list[dict[str, Any]]:
        """Every declared data domain with its description and context.

        The operator-declared domain catalog: use it to see which domains exist
        and what they cover before drilling into their datasets.
        """
        # Reads the DOMAIN#<domain> / sk="META" rows (docs/CONVENTIONS.md).
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

        Finds which domain best matches a natural-language question, before you
        drill into its datasets. Same result shape as ``semantic_search``.
        """
        # Just semantic_search pinned to type="Domain".
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
        """Return a concept's markdown, optionally paginated by lines.

        ``offset`` (0-indexed) and ``limit`` are LINE-based, so a very large table
        doc can be read a page at a time instead of all at once. The response's
        ``total_lines`` vs ``returned_lines`` tell you whether more remains: page
        again from ``offset + returned_lines``.
        """
        # The concept_id is validated and the resolved key checked to stay under
        # the dataset bundle prefix (path-traversal guard).
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

        The fastest route from a concept to everything that references it. Each
        result names the referencing page (``id`` + ``title``) AND the ``heading``
        — the section of that page where the link sits — so you know where the
        reference lives before reading it.
        """
        # Reuses okf_core.links.extract_links_with_headings (the same link resolver
        # the harvest agent uses) so consumption and authoring agree on what counts
        # as a backlink. We download the dataset subtree's .md files into a temp
        # dir, then resolve each doc's links relative to the bundle root; any doc
        # whose resolved links include concept_id is a backlink.
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
        never matched. Results are sorted.
        """
        # Visibility scope is _iter_concepts' — the same one get_backlinks uses.
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

        Two patterns are REJECTED with an error rather than run: one over 1000
        characters, and one that nests quantifiers (e.g. ``(a+)+``) — simplify it.
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
        """Find concepts by MEANING (embedding search), then ``read_page`` the hits.

        Returns candidates ranked by cosine distance: each is the concept path
        ``<domain>/<dataset>/<concept_id>`` plus its ``title``, ``description``,
        and ``s3_key`` — never the doc body, so read the ones you want.

        ``data_domain``/``dataset``/``table`` narrow the search to one part of the
        hierarchy. ``type`` is an EXACT match on the doc's frontmatter type — a
        value outside this vocabulary silently returns NOTHING, so omit it unless
        you mean it: ``Glue Table``, ``Glue Database``, ``Redshift Table``,
        ``Redshift External Table``, ``Redshift Database``, ``Reference``,
        ``Cross-Dataset Reference``, ``Playbook``, ``Domain``. ``tags`` matches a
        doc carrying ANY of the given tags. ``top_k`` is capped server-side at 20.
        """
        # The `type` vocabulary in the docstring is the set of frontmatter types
        # that actually reach the vector index: the runtime-pinned concept types
        # (okf_core.concept_types — Glue*/Redshift*/Cross-Dataset Reference), the
        # `Reference` docs the harvest prompts pin, `Domain` (okf_core.domain
        # DOMAIN_DOC_TYPE), and `Playbook` from the okf-authoring type vocabulary
        # (skills/okf-authoring/SKILL.md). Keep it in sync with those. NOT listed:
        # `Index` — index.md is a RESERVED file that reindex never embeds, so
        # filtering on it can only ever return nothing.
        #
        # top_k is clamped to [1, _SEMANTIC_TOP_K_MAX] so a client can't drive an
        # oversized fan-out (each call is a Titan V2 embed + an S3 Vectors query —
        # a cost/throttle DoS lever, threat #13).
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

    # -- get_bundle_diff -------------------------------------------------

    def get_bundle_diff(
        self,
        data_domain: str,
        dataset: str,
        from_version: str = "",
        to_version: str = "",
        max_files: int = 20,
    ) -> dict[str, Any]:
        """What changed between two published versions of this dataset's docs.

        A version is one completed harvest. Returns per-file unified diffs plus
        ``summary`` counts. BOTH selectors are optional — omitted, you get the last harvest (previous
        -> current). Use ids from the returned ``versions`` list (newest first), or
        ``to_version="live"`` for the current working files. Bounded:
        ``max_files`` entries, capped at 50, sets ``truncated``; each diff is
        capped at 100 lines and sets ``diff_truncated``.
        """
        # DELIBERATELY NOT exposed over the MCP server (no wrapper in
        # server.register_tools): version history is an operator concern, so only
        # the built-in chat agent gets this tool (chat/tools.py _TOOL_NAMES) —
        # external MCP agents see the published bundle only.
        from okf_aws import s3_versions

        max_files = max(1, min(int(max_files), _DIFF_MAX_FILES_CAP))
        result = s3_versions.bundle_diff(
            self.s3,
            bucket=self.config.bundle_bucket,
            data_domain=data_domain,
            dataset=dataset,
            from_version=from_version or "",
            to_version=to_version or "",
            max_files=max_files,
            max_lines_per_file=_DIFF_MAX_LINES_PER_FILE,
        )
        markers = s3_versions.list_complete_markers(
            self.s3,
            bucket=self.config.bundle_bucket,
            data_domain=data_domain,
            dataset=dataset,
            limit=5,
        )
        result["versions"] = [m.descriptor() for m in markers]
        return result


# -- list_domains plumbing (module level: pure, unit-testable) --------------------


def _encode_cursor(key: dict[str, Any] | None) -> str | None:
    """Opaque page cursor: base64(JSON(LastEvaluatedKey)). None = last page."""
    if not key:
        return None
    return base64.urlsafe_b64encode(json.dumps(key).encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    if not cursor:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except Exception as e:  # noqa: BLE001 - a garbled cursor is a caller error
        raise ValueError("invalid cursor — pass next_cursor back verbatim") from e


def _fold_xref(item: dict[str, Any], references: dict, referenced_by: dict) -> None:
    """Fold one XREF row into the two direction maps (malformed rows ignored)."""
    src = (
        item.get("source_data_domain", ""),
        item.get("source_dataset", ""),
    )
    tgt = (
        item.get("target_data_domain", ""),
        item.get("target_dataset", ""),
    )
    if not all(src) or not all(tgt):
        return  # malformed row — ignore rather than half-report
    references.setdefault(src, set()).add(f"{tgt[0]}/{tgt[1]}")
    referenced_by.setdefault(tgt, set()).add(f"{src[0]}/{src[1]}")
