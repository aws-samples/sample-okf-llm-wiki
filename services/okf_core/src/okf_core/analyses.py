"""Analysis templates: DynamoDB item conventions shared by chat + Control API.

An ANALYSIS is a saved, human-owned procedure document (markdown + YAML
frontmatter) the chat agent executes on request: frontmatter carries the
title/description and the prerequisite ``questions`` (verbatim ``ask_human``
objects), the body carries the steps. Rows live on the analyses table, keyed
by DATASET (not by user): any user may list/read/run a dataset's analyses;
only the owner recorded on the row may edit or delete.

This module holds the key conventions, caps, and the document validation
(:func:`parse_analysis`) — shared by the chat tools (create/update) and the
Control API (the Analysis page's manual edit). The question rules here
mirror ``chat.ask_human.normalize_questions`` exactly (the chat side still
runs the real normalizer on top, so the executor's ask_human payload can
never drift from what validation admitted). The Control API also imports
the pk builders for the dataset-deletion purge and the page's list.

Item shape (all plain resource-API values):
    pk = "ANALYSIS#<data_domain>#<dataset>", sk = <name slug>
    name, data_domain, dataset, title, description, questions (int count),
    owner_sub, created_at, updated_at (ISO seconds), version (int, 1-based),
    body (the FULL markdown document, frontmatter included)
"""

from __future__ import annotations

import re
from typing import Any

ANALYSIS_PK_PREFIX = "ANALYSIS#"

# One analysis is atomic and lightweight — a procedure, not a wiki. The cap
# keeps rows far from DynamoDB's 400KB item limit with headroom for attrs.
MAX_BODY_CHARS = 32_000

# Analysis names are slugs (the computation-slug convention): lowercase,
# digits, hyphen/underscore, max 64 chars.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def analysis_pk(data_domain: str, dataset: str) -> str:
    return f"{ANALYSIS_PK_PREFIX}{data_domain}#{dataset}"


# --- publications -------------------------------------------------------------
# A PUBLICATION binds one produced report to the analysis that produced it —
# the run's durable artifact (a report has no database row and its only other
# pointer is the chat thread, which the user may delete). Publication rows
# share the analysis's partition with a structured sk: slugs cannot contain
# ``#`` (SLUG_RE), so ``<name>#pub#<report_id>`` can never collide with a
# document row, one ``begins_with`` lists one analysis's publications, and
# the dataset-deletion partition purge covers them for free.

PUBLICATION_SK_SEP = "#pub#"


def publication_sk(name: str, report_id: str) -> str:
    return f"{name}{PUBLICATION_SK_SEP}{report_id}"


def publication_sk_prefix(name: str) -> str:
    return f"{name}{PUBLICATION_SK_SEP}"


def is_publication_sk(sk: str) -> bool:
    return PUBLICATION_SK_SEP in (sk or "")


# --- document validation --------------------------------------------------------

# The exact key set a frontmatter question may carry — the ask_human arg shape.
# Anything else (a typo, or an attempted `default`) is refused by name: there
# are deliberately no defaults, and a misspelled key would silently vanish.
QUESTION_KEYS = {"id", "prompt", "kind", "options"}

# The ask_human question kinds (mirrors chat.ask_human — single/multi require
# options; text is free prose).
_CHOICE_KINDS = ("single", "multi")
_VALID_KINDS = ("single", "multi", "text")


def parse_analysis(text: str) -> tuple[dict[str, Any], list[str]]:
    """Parse + validate one analysis document; returns ``(parsed, errors)``.

    ``parsed`` (meaningful only when ``errors`` is empty) carries ``title``,
    ``description``, ``questions`` (the RAW frontmatter list — the chat side
    normalizes it through the real ask_human validator) and ``body`` (below
    the frontmatter). Every problem is collected by name so a draft can be
    fixed in one pass — by the model or by a human in the page editor.
    """
    from okf_core.document import OKFDocument, OKFDocumentError

    errors: list[str] = []
    if len(text or "") > MAX_BODY_CHARS:
        return {}, [
            f"document is {len(text)} chars — the cap is {MAX_BODY_CHARS}. "
            "An analysis is ONE atomic procedure; split it or tighten the prose"
        ]
    try:
        doc = OKFDocument.parse(text or "")
    except OKFDocumentError as e:
        return {}, [str(e)]
    fm = doc.frontmatter
    if not fm:
        return {}, [
            "missing YAML frontmatter — the document must open with "
            "`---` carrying title, description and (optionally) questions"
        ]

    title = str(fm.get("title") or "").strip()
    description = str(fm.get("description") or "").strip()
    if not title:
        errors.append("frontmatter needs a `title` (the human name of the analysis)")
    if not description:
        errors.append(
            "frontmatter needs a `description` (one or two lines: what the "
            "analysis answers — it is how the analysis is discovered)"
        )
    if not doc.body.strip():
        errors.append("the body is empty — the steps below the frontmatter ARE the analysis")

    raw_questions = fm.get("questions")
    questions: list[dict[str, Any]] = []
    if raw_questions not in (None, []):
        q_errors = _check_questions(raw_questions)
        errors.extend(q_errors)
        if not q_errors:
            questions = list(raw_questions)

    parsed = {
        "title": title,
        "description": description,
        "questions": questions,
        "body": doc.body,
    }
    return parsed, errors


def _check_questions(raw: Any) -> list[str]:
    """Validate the frontmatter ``questions`` list.

    Mirrors ``chat.ask_human.normalize_questions`` (prompt required, kind in
    the enum, choice kinds need options) plus the analysis-specific
    strictness: ask_human tolerates missing ids (derives ``q1``…) because an
    ad-hoc call dies with its turn; analysis questions are REFERENCED BY ID
    from the steps (``{time_window}``) and keyed by a future headless
    trigger, so ids must be explicit, unique, and the key set exact.
    """
    if not isinstance(raw, list):
        return ["frontmatter `questions` must be a YAML list of question objects"]
    errors: list[str] = []
    seen: set[str] = set()
    for i, q in enumerate(raw):
        if not isinstance(q, dict):
            errors.append(f"question {i} must be a mapping (id/prompt/kind/options)")
            continue
        qid = str(q.get("id") or "").strip()
        if not qid:
            errors.append(f"question {i} needs an explicit `id` — the steps refer to answers by it")
        elif qid in seen:
            errors.append(f"duplicate question id {qid!r} — ids must be unique")
        else:
            seen.add(qid)
        unknown = sorted(set(q) - QUESTION_KEYS)
        if unknown:
            errors.append(
                f"question {i} carries unsupported key(s) {unknown} — only "
                "id/prompt/kind/options exist (there are no defaults: an "
                "analysis asks, the user answers)"
            )
        if not str(q.get("prompt") or "").strip():
            errors.append(f"question {i} is missing a prompt")
        kind = str(q.get("kind") or "single").strip().lower()
        if kind not in _VALID_KINDS:
            errors.append(
                f"question {i} has invalid kind {kind!r}; use one of {list(_VALID_KINDS)}"
            )
        elif kind in _CHOICE_KINDS:
            options = q.get("options")
            if not isinstance(options, list) or not [
                o for o in options if str(o).strip()
            ]:
                errors.append(
                    f"question {i} ({kind!r}) requires a non-empty options list"
                )
            elif any(not isinstance(o, str) for o in options):
                # YAML 1.1 coerces bare yes/no/true/on to booleans (and bare
                # numbers to ints) — the two answer surfaces then stringify
                # them differently (Python 'True' vs JS 'true'), so the
                # recorded answer never matches what the steps anticipated.
                errors.append(
                    f"question {i} options must all be strings — quote YAML "
                    'literals like yes/no/true/2024 ("yes", "no")'
                )
    return errors
