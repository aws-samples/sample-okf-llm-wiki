"""Wiki annotations — user feedback on concept docs, applied by a re-harvest.

Pure-Python primitives shared by the Control API (annotation CRUD + the
pre-flight orphan sweep) and the harvest runner (resolution write-back). No AWS
or agent deps — the boto3 calls live in the services; this module owns the
*invariants*: the DynamoDB key shape, the status/outcome vocabulary, the 7-day
TTL, and — the load-bearing part — how an annotation's quoted passage is
RE-ANCHORED against a (possibly re-authored) doc body.

## The item (``okf-annotations`` table, CONVENTIONS.md)

    pk = "ANNO#<data_domain>#<dataset>#<user_sub>"   # user isolation is STRUCTURAL
    sk = "<concept_id>#<annotation_id>"

The author's Cognito ``sub`` (immutable, opaque, ``#``-free) is baked into the
partition key, so a user's Query can only ever return their OWN annotations —
there is no cross-user read path to forget an ``if`` on. ``sub`` (not ``email``)
is used precisely because it never changes and can't inject a ``#`` delimiter.

## Anchoring: quote, not coordinates

We deliberately do NOT store a character offset or a line number as the source
of truth — a re-harvest rewrites the doc and any coordinate goes stale instantly.
Instead an annotation stores a **TextQuoteSelector**: the selected ``quote`` plus
the minimal ``prefix``/``suffix`` context the UI grew until the window was unique
in the doc at capture time (W3C Web Annotation model). Re-anchoring then means:
does this quote still occur in the current body? That yes/no is all the orphan
sweep needs, and it degrades gracefully across edits — matching on meaning-ish
text, not fragile positions. ``block_line`` is stored too, but only as a *hint*
the agent can jump to; it never enters the orphan decision.
"""

from __future__ import annotations

import re
from typing import Any

# --------------------------------------------------------------------------- #
# Status / outcome vocabulary
# --------------------------------------------------------------------------- #

# An annotation is OPEN until the user runs an annotation harvest. The Control
# API flips the batch to IN_REVIEW when it dispatches the run (so a second run
# can't re-process the same notes), and the run — or the pre-flight sweep —
# drives each to the terminal RESOLVED state. There is no separate "rejected"
# status: rejection is a resolution OUTCOME, so rejected notes still live in the
# 7-day history exactly like applied ones.
STATUS_OPEN = "open"
STATUS_IN_REVIEW = "in_review"
STATUS_RESOLVED = "resolved"

# Set alongside STATUS_RESOLVED. APPLIED: the agent judged the feedback factually
# grounded and edited the bundle. REJECTED: the agent assessed it and declined
# (not grounded, out of scope, duplicate — the comment says which). ORPHANED: the
# pre-flight sweep couldn't re-anchor the quote to the live doc, so there was
# nothing for the agent to act on — auto-resolved without an agent ever seeing it.
OUTCOME_APPLIED = "applied"
OUTCOME_REJECTED = "rejected"
OUTCOME_ORPHANED = "orphaned"

_TERMINAL_OUTCOMES = frozenset({OUTCOME_APPLIED, OUTCOME_REJECTED, OUTCOME_ORPHANED})

# Terminal annotations linger this long as history, then DynamoDB's TTL reaps
# them (``expires_at``, epoch seconds). OPEN annotations carry no ``expires_at``
# and never expire — the attribute is set ONLY at resolution.
HISTORY_TTL_SECONDS = 7 * 24 * 60 * 60

# The message stored on an auto-orphaned annotation. Non-blaming: the passage
# moved, so there's simply nothing to apply — not a judgement on the feedback.
ORPHAN_RESOLUTION_MESSAGE = (
    "Auto-resolved (orphaned). The passage this note referenced is no longer in "
    "the wiki — the page changed since you wrote it, so there was nothing to apply."
)


# --------------------------------------------------------------------------- #
# Key construction
# --------------------------------------------------------------------------- #


def annotation_pk(data_domain: str, dataset: str, user_sub: str) -> str:
    """Partition key that scopes annotations to one user + dataset.

    ``user_sub`` is the caller's immutable Cognito ``sub``. It must be non-empty
    (a missing subject would collapse everyone into one shared partition), so the
    caller is responsible for rejecting an unauthenticated request before here.
    """
    if not user_sub:
        raise ValueError("annotation_pk requires a non-empty user_sub")
    return f"ANNO#{data_domain}#{dataset}#{user_sub}"


def annotation_sk(concept_id: str, annotation_id: str) -> str:
    """Sort key: ``<concept_id>#<annotation_id>``.

    Concept ids never contain ``#`` (they're slash-delimited paths like
    ``tables/races``), so ``begins_with(sk, "<concept_id>#")`` cleanly selects
    one concept's annotations within a user's partition.
    """
    return f"{concept_id}#{annotation_id}"


def concept_sk_prefix(concept_id: str) -> str:
    """The ``begins_with`` prefix for one concept's annotations."""
    return f"{concept_id}#"


# --------------------------------------------------------------------------- #
# Quote re-anchoring (the orphan check)
# --------------------------------------------------------------------------- #

# Markdown syntax the RENDERED text drops but the raw source keeps. We strip
# these before comparing so a quote captured from the DOM ("status 9") still
# matches source that wrote it as "**status** `9`". Intentionally conservative:
# we remove emphasis/code/heading/list/quote markers and link/image *syntax*
# (keeping the visible link text), not arbitrary punctuation.
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")  # [text](url) / ![alt](url) -> text
# A fenced code block's DELIMITER line (```` ```sql ````, ``~~~``, or a bare
# closing ```` ``` ````) renders to NOTHING — only the code BETWEEN the fences is
# visible. So the whole line must go, including any info string (the ``sql``
# language tag), which is in the source but never in a DOM selection. Must run
# BEFORE marker-stripping, or ```` ```sql ```` first collapses to the word ``sql``.
_CODE_FENCE_LINE = re.compile(r"(?m)^\s*(?:`{3,}|~{3,})[^\n]*$")
_MD_STRIP_CHARS = re.compile(r"[*_`~#>]+")  # emphasis, code, heading, quote, strike
_LIST_MARKER = re.compile(r"(?m)^\s*(?:[-+*]|\d+\.)\s+")  # leading bullets/numbers
# A GFM table delimiter row (|---|:--:|…) carries no content — the rendered table
# drops it entirely, so we must too or it leaks stray dashes into the fold.
_TABLE_DELIM_ROW = re.compile(r"(?m)^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
_TABLE_PIPE = re.compile(r"\|")  # cell separator
_WS = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Fold source markdown + rendered text onto one comparable form.

    Unwrap links to their visible text, drop code-fence delimiter lines and the
    GFM table delimiter row, strip inline markdown markers (emphasis/code/etc.),
    turn table cell pipes into spaces, flatten list bullets, collapse whitespace,
    and casefold. The result is what both a DOM selection and the raw ``.md``
    source reduce to — including a selection spanning several table cells (browser
    joins with whitespace, source with ``|``) or a fenced SQL block (the ```` ```sql ````
    info string is in the source but not the render) — so a substring test is
    tolerant of reflow and formatting churn without fuzzy scoring (which could
    wrongly KEEP a stale note).

    Note the ORDER: fence lines are dropped BEFORE markers (else ```` ```sql ````
    collapses to ``sql``); inline markers then collapse to nothing (so mid-word
    markup like ``un`bel`ievable`` folds to ``unbelievable``, matching the DOM);
    pipes become SPACES last (so adjacent cells don't fuse into one token).
    """
    text = _MD_LINK.sub(r"\1", text)
    text = _CODE_FENCE_LINE.sub(" ", text)  # fence line (+ lang tag) -> nothing
    text = _TABLE_DELIM_ROW.sub(" ", text)
    text = _LIST_MARKER.sub("", text)
    text = _MD_STRIP_CHARS.sub("", text)  # markers -> nothing (fuse, matches DOM)
    text = _TABLE_PIPE.sub(" ", text)  # cell separator -> space (don't fuse cells)
    text = _WS.sub(" ", text)
    return text.strip().casefold()


def find_quote(body: str, quote: str) -> bool:
    """Does ``quote`` still occur in ``body`` (a TextQuoteSelector re-anchor)?

    This is the orphan predicate, and it answers ONE question — *does the passage
    still exist?* — which is all the pre-flight sweep needs. It deliberately does
    NOT try to pin *which* occurrence a repeated quote refers to: identical quotes
    make a passage more likely to still exist, never ambiguous for a yes/no, and
    disambiguating the right occurrence is the agent's job at apply time (where the
    stored prefix/suffix + reasoning beat a substring rule). Both sides are folded
    through :func:`normalize_text` first, so an empty/whitespace quote — the only
    guaranteed-orphan input — returns False; anything present returns True.
    """
    nquote = normalize_text(quote)
    if not nquote:
        return False
    return nquote in normalize_text(body)


def annotatable_text(doc_text: str) -> str:
    """The ANNOTATABLE surface of a concept doc: the markdown body only.

    Only the rendered body (``.okf-prose``) is selectable for annotations — the
    frontmatter header (type/tags/title/description) is deliberately NOT (see the
    UI's captureSelection). So re-anchoring must run against the body alone, with
    the YAML frontmatter stripped: matching the raw ``.md`` would let a quote match
    a ``key:`` value it can't actually be selected from, and — for a header
    selection that shouldn't exist anyway — would depend on YAML layout. Parsing
    failures fall back to the raw text so a malformed doc still gets a best-effort
    match.
    """
    from okf_core.document import OKFDocument, OKFDocumentError

    try:
        return OKFDocument.parse(doc_text).body or ""
    except (OKFDocumentError, Exception):  # noqa: BLE001 - tolerate any parse issue
        return doc_text


def is_orphaned(doc_text: str | None, quote: str) -> bool:
    """True when the annotation cannot be re-anchored to the doc.

    ``doc_text`` is the RAW ``.md`` file (frontmatter + body). A missing doc
    (``None`` — the whole concept was dropped from the bundle) orphans every
    annotation on it; otherwise match ``quote`` against the annotatable BODY
    (:func:`annotatable_text`), since that's the only surface a selection covers.
    """
    if doc_text is None:
        return True
    return not find_quote(annotatable_text(doc_text), quote)


# --------------------------------------------------------------------------- #
# Item (de)serialization — plain dicts, no boto3
# --------------------------------------------------------------------------- #


def is_terminal_outcome(outcome: str | None) -> bool:
    return outcome in _TERMINAL_OUTCOMES
