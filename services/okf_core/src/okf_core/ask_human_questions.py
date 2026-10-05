"""``ask_human`` question objects: the kinds, the error, and the normalizer.

Shared because an analysis's frontmatter questions ARE ``ask_human`` question
objects: the chat tool (``chat.ask_human``), the analysis validation
(``okf_core.analyses``) and the Control API's ``get_analysis`` (which serves
the Run dialog's questionnaire) must all agree on one normalized shape —
``{id, prompt, kind, options, allow_other}``. Pure Python, no dependencies.

Question kinds:

* ``single`` — pick exactly one of ``options`` (the UI adds a free-text "Other").
* ``multi`` — pick any number of ``options`` (also with "Other").
* ``text`` — free prose; no ``options`` and no "Other".
"""

from __future__ import annotations

import json
from typing import Any

# The recognized question kinds. ``single``/``multi`` require options and get a
# free-text "Other"; ``text`` is free prose with neither.
KIND_SINGLE = "single"
KIND_MULTI = "multi"
KIND_TEXT = "text"
_CHOICE_KINDS = (KIND_SINGLE, KIND_MULTI)
_VALID_KINDS = (KIND_SINGLE, KIND_MULTI, KIND_TEXT)


class AskHumanError(ValueError):
    """An ``ask_human`` call was malformed (bad shape / kind / missing options).

    The middleware turns this into a tool ERROR result (not an interrupt) so the
    model sees what it got wrong and can re-issue a valid call — never a crash.
    """


def normalize_questions(questions: Any) -> list[dict[str, Any]]:
    """Validate + normalize the model's ``questions`` arg into the interrupt payload.

    Returns a list of ``{id, prompt, kind, options, allow_other}`` dicts (the exact
    shape the UI renders). Raises :class:`AskHumanError` on anything unusable so the
    middleware can hand the model a corrective error instead of interrupting on a
    malformed form. ``allow_other`` is True for choice kinds (the UI's free-text
    5th option), False for ``text``.
    """
    if isinstance(questions, str):
        # A model that passed a JSON string instead of a list — tolerate it.
        try:
            questions = json.loads(questions)
        except (ValueError, TypeError) as exc:
            raise AskHumanError("questions must be a list, got an unparseable string") from exc
    if not isinstance(questions, (list, tuple)) or not questions:
        raise AskHumanError("questions must be a non-empty list")

    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            raise AskHumanError(f"question {i} must be an object")
        prompt = str(q.get("prompt") or "").strip()
        if not prompt:
            raise AskHumanError(f"question {i} is missing a prompt")
        kind = str(q.get("kind") or KIND_SINGLE).strip().lower()
        if kind not in _VALID_KINDS:
            raise AskHumanError(
                f"question {i} has invalid kind {kind!r}; use one of {list(_VALID_KINDS)}"
            )
        # A stable id: use the model's if given + unique, else derive one.
        qid = str(q.get("id") or "").strip() or f"q{i + 1}"
        if qid in seen_ids:
            qid = f"{qid}_{i + 1}"
        seen_ids.add(qid)

        options: list[str] = []
        if kind in _CHOICE_KINDS:
            raw = q.get("options")
            if not isinstance(raw, (list, tuple)) or not raw:
                raise AskHumanError(
                    f"question {i} ({kind!r}) requires a non-empty options list"
                )
            options = [str(o).strip() for o in raw if str(o).strip()]
            if not options:
                raise AskHumanError(f"question {i} ({kind!r}) has no usable options")

        out.append(
            {
                "id": qid,
                "prompt": prompt,
                "kind": kind,
                "options": options,
                # The UI always offers a free-text choice on single/multi so the
                # user is never limited to the model's options; text is free already.
                "allow_other": kind in _CHOICE_KINDS,
            }
        )
    return out
