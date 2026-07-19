"""The ``ask_human`` tool — the agent's way to ask the user clarifying questions.

Like ``render_chart``, ``ask_human`` does no server work of its own: the tool is
just a **declaration** of the question set the model wants answered. The actual
pause-and-wait is owned by :class:`chat.ask_human_middleware.AskHumanMiddleware`,
whose ``awrap_tool_call`` intercepts the call and raises a LangGraph
``interrupt(...)`` — so the graph checkpoints and stops, the UI renders a QA form,
and a later ``answer_human`` request resumes the run with ``Command(resume=…)``.
The tool body never blocks; it exists so the model has a schema to call and the
middleware has something to intercept (mirrors the verified chart-interrupt
pattern). Keep this module dependency-light (only ``langchain_core``) so it imports
in the unit venv.

Question kinds:

* ``single`` — pick exactly one of ``options``. The UI ALWAYS adds a free-text
  "Other" choice, so the user is never boxed in.
* ``multi`` — pick any number of ``options`` (also with the free-text "Other").
* ``text`` — free prose; no ``options`` (and no "Other" — it's already free text).

The tool takes ONE arg, ``questions``: a list of these. The model batches all the
clarifications it needs into a single call so the user answers them in one pass.
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


ASK_HUMAN_DESC = """Ask the user one or more clarifying questions and WAIT for their answers before continuing.

Use this ONLY when you genuinely cannot proceed well without input the user alone can give — the question is ambiguous, under-specified, or hinges on a preference/decision the wiki can't settle (e.g. "which of these two 'revenue' metrics do you mean?", "should I include cancelled orders?"). Do NOT use it for things the wiki answers — read the docs first. Do NOT interrogate: ask the fewest questions that unblock you, batched into ONE call, and only when the answer materially changes what you'd do.

The user is shown a short form, answers each question, and submits; their answers come back to you as the tool result, then you continue.

`questions` is a list; each item is:
  { "id": "grain",                         // short stable id you'll see back with the answer
    "prompt": "Which time grain do you want?",
    "kind": "single",                       // "single" | "multi" | "text"
    "options": ["Daily", "Weekly", "Monthly"] }   // required for single/multi; omit for text

Kinds:
- "single" — the user picks exactly one option.
- "multi"  — the user picks any number of options.
- "text"   — free-form prose answer; do NOT provide options.

For "single" and "multi" the user can ALWAYS also type their own answer instead of picking (an "Other" choice is added automatically) — so list the likely options, you don't have to be exhaustive. Keep prompts short and concrete; keep options mutually distinct.

Args:
  questions: the list of clarifying questions to ask, as described above.
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


def normalize_answers(answers: Any, questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize the user's submitted answers (from ``Command(resume=…)``) for the model.

    Accepts a list of ``{id, answer}`` (answer: str for single/text, list[str] for
    multi) OR a mapping ``{id: answer}``. Returns a list aligned to ``questions``,
    each ``{id, prompt, answer}`` — a stable, model-legible record folded into the
    tool result. Missing answers become ``"(no answer)"`` rather than dropping the
    question, so the model always sees one entry per asked question. Tolerant by
    design: this is a resume boundary, not a place to fail a live conversation.
    """
    by_id: dict[str, Any] = {}
    if isinstance(answers, dict):
        by_id = {str(k): v for k, v in answers.items()}
    elif isinstance(answers, (list, tuple)):
        for a in answers:
            if isinstance(a, dict) and a.get("id") is not None:
                by_id[str(a["id"])] = a.get("answer")

    result: list[dict[str, Any]] = []
    for q in questions:
        raw = by_id.get(q["id"])
        if isinstance(raw, (list, tuple)):
            answer = ", ".join(str(x).strip() for x in raw if str(x).strip())
        elif raw is None:
            answer = ""
        else:
            answer = str(raw).strip()
        result.append(
            {"id": q["id"], "prompt": q["prompt"], "answer": answer or "(no answer)"}
        )
    return result


def make_ask_human_tool() -> Any:
    """Wrap ``ask_human`` as a LangChain StructuredTool for the chat agent.

    The body is intentionally inert — the middleware short-circuits the call before
    it runs (raising the interrupt). It only returns a benign string in the
    (unexpected) event the middleware isn't attached, so a misconfiguration
    degrades to "no clarification happened" rather than crashing.
    """
    from langchain_core.tools import StructuredTool

    def ask_human(questions: list) -> str:
        # Never reached when AskHumanMiddleware is attached (it intercepts first).
        return json.dumps(
            {"status": "noop", "note": "ask_human requires the interrupt middleware"}
        )

    return StructuredTool.from_function(
        func=ask_human,
        name="ask_human",
        description=ASK_HUMAN_DESC,
    )
