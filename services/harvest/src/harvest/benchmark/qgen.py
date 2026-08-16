"""Synthetic question-bank generation (``mode="generate_questions"``).

Hosted on the harvest runtime like the other Benchmark Studio modes — the
container owns the model factory, the live source, and this package — and,
like them, it is NOT a harvest: no lease, no mount, nothing written to the
bundle. The outcome is a ``QBANK#`` row plus an off-mount artifact
(``benchmark/<d>/<ds>/qbank/<id>.json``) the human reviews in the UI and then
downloads or applies as the dataset's ``questions.csv``.

The load-bearing boundary: the author agents see ONLY the dataset's ground
truth — the ``.metadata/`` catalog snapshot (schema, profiles, relationship
evidence) and the ``.context/`` uploaded source docs — plus live read-only
SQL. Never the authored wiki (physically absent from their tree, see
:func:`~harvest.benchmark.s3_snapshot.materialize_ground_truth`): the wiki is
the system under test, and questions phrased in its own vocabulary would
measure parroting, not coverage.

Pipeline (the arithmetic is deterministic, the creativity is agentic):

1. ``okf_core.qbank.allocate_slots`` turns the config into an explicit
   worklist of (dimension, tier, check) slots.
2. One author agent per dimension batch, fanned out under a semaphore. Every
   question is delivered through the ``submit_question`` TOOL (structured by
   construction), which validates AT SUBMIT TIME — shape, cross-batch dedup,
   the business-language leakage lint, and for Accuracy questions a live
   execution of the gold through the same runner/caps the grader will use, so
   an applied bank can never produce DISCARDED questions. Failures return as
   corrective tool results the author fixes in its own loop. A slot the
   author cannot fill must be explicitly forfeited with a reason — and the
   :class:`AuthorQuotaMiddleware` refuses to let the agent finish while slots
   are silently unresolved (bounded nudges, then the backfill owns them).
3. Unresolved slots get ONE backfill round (a fresh author over all selected
   dimensions). Anything still unfilled is DROPPED — recorded with its reason
   in the artifact, never hidden (requesting 60 and silently delivering 57
   would be worse than saying so).

Failures are LOUD, mirroring ``studio.py``: a run that can't materialize its
ground truth or build its source FAILS the row with the error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any

from okf_core import benchmark_report as br
from okf_core import qbank
from okf_core.benchmark_questions import CHECK_BEHAVIOR, CHECK_SQL
from okf_core.lint import _mask_sql

from harvest.benchmark.report_store import update_report_row
from harvest.benchmark.s3_snapshot import default_bucket, materialize_ground_truth
from harvest.status import build_registry_client

log = logging.getLogger("harvest.benchmark.qgen")

# One author's ReAct budget. An author owns at most a dimension's slice of the
# bank (~9 slots at count=100 over 12 dimensions); each question costs a few
# exploration turns plus a validating submit, so this bounds a confused author
# without starving a diligent one.
_AUTHOR_RECURSION_LIMIT = 150

# Author fan-out cap (concurrent model conversations). Lower than the solver
# fan-out default: authors are long-lived explorers, not one-question solvers.
_DEFAULT_MAX_CONCURRENCY = 4

# A backfill author's worklist cap. Round-1 authors own one dimension's slice
# (~3-9 slots); the backfill inherits EVERYTHING unresolved, and a weak first
# round on count=100 can leave 30+ slots — far past what one agent resolves
# inside _AUTHOR_RECURSION_LIMIT (each question costs several exploration
# turns plus a validating submit). Chunking keeps the per-author step
# arithmetic identical to round 1 instead of mass-dropping the tail.
_BACKFILL_BATCH_SLOTS = 8

_NUDGE_PREFIX = "[quota] "


def _max_concurrency() -> int:
    try:
        return max(1, int(os.environ.get("OKF_QGEN_MAX_CONCURRENCY", "")))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_CONCURRENCY


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _s3_client():
    import boto3

    region = os.environ.get("AWS_REGION", "us-east-1")
    return boto3.client("s3", region_name=region)


# --------------------------------------------------------------------------- #
# payload + row lifecycle
# --------------------------------------------------------------------------- #


def validate_qgen_payload(payload: dict) -> str | None:
    """Payload sanity for ``mode=generate_questions`` (the Control API
    validated deeper; this is the runtime's defense in depth)."""
    for key in ("data_domain", "dataset", "qbank_id"):
        if not payload.get(key):
            return f"generate_questions mode requires '{key}'"
    if not qbank.is_valid_qbank_id(payload["qbank_id"]):
        return f"invalid qbank_id: {payload['qbank_id']!r}"
    try:
        qbank.validate_config(payload.get("config"))
    except qbank.QbankConfigError as e:
        return str(e)
    return None


def run_generate_questions(payload: dict, session_id: str | None = None) -> None:
    """Execute one generation end-to-end (thread entry point).

    Moves the QBANK# row ``queued → running → complete``/``failed`` — the
    exact lifecycle (and row-update machinery) benchmark reports use, with the
    QBANK sort key.
    """
    data_domain = payload["data_domain"]
    dataset = payload["dataset"]
    qbank_id = payload["qbank_id"]
    registry = build_registry_client()

    def row(attrs: dict[str, Any]) -> None:
        # EVERY row write — the initial `running` flip, progress ticks, and
        # the terminal statuses — is guarded on the row NOT being CANCELLED
        # (and, inside update_report_row, on the row still existing). The
        # initial write is the one that closes the resurrection hole: a
        # generation cancelled while still QUEUED (microVM not booted yet)
        # must not be flipped back to `running` by the runtime's cold start.
        # Blocked writes are dropped, never retried — that drop IS the cancel
        # contract: partial work does not survive.
        update_report_row(
            registry,
            data_domain=data_domain,
            dataset=dataset,
            report_id=qbank_id,
            attrs=attrs,
            sk=qbank.qbank_sk(qbank_id),
            unless_status=br.STATUS_CANCELLED,
        )

    terminal_row = row  # same guard; kept as a name for the terminal writes

    try:
        row(
            {
                "status": br.STATUS_RUNNING,
                "runtime_session_id": session_id or "",
                "started_at": _now_iso(),
            }
        )
        # If the conditional flip above was BLOCKED (cancelled while queued)
        # or the row is gone (deleted while queued), do no work at all — the
        # operator already said this bank must not exist. Fail-open reads
        # (transient registry fault → "running") proceed; the pre-persist
        # check and the conditional terminal write still cover them.
        status = _row_status(registry, data_domain, dataset, qbank_id)
        if status is None or status == br.STATUS_CANCELLED:
            log.info(
                "Question bank %s was %s before work started; not generating.",
                qbank_id,
                "deleted" if status is None else "cancelled",
            )
            return
        artifact = _execute(payload, qbank_id, row)
        # CANCELLED (or deleted) mid-run: discard the partial bank instead of
        # persisting it — the operator asked for the work NOT to survive, and
        # a gold-carrying artifact behind a cancelled row would be an orphan
        # the UI can't reach (get/apply only serve COMPLETE rows).
        status = _row_status(registry, data_domain, dataset, qbank_id)
        if status is None or status == br.STATUS_CANCELLED:
            log.info(
                "Question bank %s was %s mid-run; discarding the partial bank.",
                qbank_id,
                "deleted" if status is None else "cancelled",
            )
            return
        bucket = default_bucket()
        _s3_client().put_object(
            Bucket=bucket,
            Key=qbank.qbank_key(data_domain, dataset, qbank_id),
            Body=json.dumps(artifact).encode("utf-8"),
            ContentType="application/json",
        )
        counts = artifact.get("counts") or {}
        terminal_row(
            {
                "status": br.STATUS_COMPLETE,
                "completed_at": _now_iso(),
                "phase": "done",
                "question_count": int(counts.get("delivered") or 0),
                "dropped_count": len(artifact.get("dropped") or []),
                **{
                    f"count_{check}": int(n)
                    for check, n in ((counts.get("check") or {}).items())
                },
                "total_tokens": int(
                    ((artifact.get("telemetry") or {}).get("tokens") or {}).get(
                        "total_tokens"
                    )
                    or 0
                ),
            }
        )
        log.info(
            "Question bank %s complete (%s/%s): %s/%s delivered.",
            qbank_id, data_domain, dataset,
            counts.get("delivered"), counts.get("requested"),
        )
    except Exception as e:  # noqa: BLE001 - loud failure: the row carries the error
        terminal_row(
            {
                "status": br.STATUS_FAILED,
                "detail": f"{type(e).__name__}: {e}"[:1024],
                "completed_at": _now_iso(),
            }
        )
        raise


def _row_status(
    registry: tuple[Any, str] | None, data_domain: str, dataset: str, qbank_id: str
) -> str | None:
    """The QBANK row's current status, or None when the row is gone.

    Fail-open to ``running`` on a read error: the persist that follows must
    not be blocked by a transient registry fault (the conditional terminal
    write and the cancel path's purge still cover the cancelled case).
    """
    if registry is None:
        return br.STATUS_RUNNING
    client, table = registry
    try:
        item = client.get_item(
            TableName=table,
            Key={
                "pk": {"S": f"HARVEST#{data_domain}#{dataset}"},
                "sk": {"S": qbank.qbank_sk(qbank_id)},
            },
        ).get("Item")
    except Exception:  # noqa: BLE001 - a read fault must not drop a finished bank
        return br.STATUS_RUNNING
    if not item:
        return None
    return str((item.get("status") or {}).get("S") or "") or br.STATUS_RUNNING


# --------------------------------------------------------------------------- #
# the in-run question store + submit tools
# --------------------------------------------------------------------------- #


def _normalize_question(text: str) -> str:
    """The dedup key: casefolded, punctuation-blind, whitespace-collapsed."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).strip()


def _physical_identifiers(columns: dict[str, dict[str, str]]) -> list[str]:
    """The identifiers the leakage lint scans question text for.

    Deliberately partial: snake_case names (``race_id``) and dotted
    ``table.column`` references are unmistakably schema-speak, while bare
    English column names (``year``, ``name``) are legitimate business words no
    lint can ban. The goal is questions phrased like a business user, not a
    DBA — that is what makes the NL-resolution dimension measure anything.
    """
    idents: set[str] = set()
    for table, cols in (columns or {}).items():
        if "_" in table:
            idents.add(table.lower())
        for col in cols:
            if "_" in col:
                idents.add(col.lower())
            idents.add(f"{table}.{col}".lower())
    return sorted(idents, key=len, reverse=True)


def _leaked_identifier(question: str, idents: list[str]) -> str | None:
    q = question.lower()
    for ident in idents:
        if re.search(rf"(?<![a-z0-9_]){re.escape(ident)}(?![a-z0-9_])", q):
            return ident
    return None


_SQL_SHAPE_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


class QuestionStore:
    """One author batch's slots + submissions + forfeits (thread-safe: sync
    tools run in executor threads under the async fan-out, and the dedup set
    is shared ACROSS batches). ``on_change`` (optional) fires after every
    successful fill/forfeit — the live-progress hook."""

    def __init__(
        self,
        slots: dict[int, dict[str, str]],
        *,
        seen: set[str],
        seen_lock: threading.Lock,
        on_change=None,
    ):
        self.slots = slots  # slot number -> {"dimension","tier","check"}
        self.filled: dict[int, dict[str, Any]] = {}
        self.forfeited: dict[int, str] = {}
        self._seen = seen
        self._seen_lock = seen_lock
        self._lock = threading.Lock()
        self._on_change = on_change

    def changed(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:  # noqa: BLE001 - progress is observability only
                pass

    def unresolved(self) -> list[int]:
        return [
            n for n in sorted(self.slots) if n not in self.filled and n not in self.forfeited
        ]

    def claim_question(self, normalized: str) -> bool:
        """Atomically claim a normalized question text across ALL batches."""
        with self._seen_lock:
            if normalized in self._seen:
                return False
            self._seen.add(normalized)
            return True

    def release_question(self, normalized: str) -> None:
        with self._seen_lock:
            self._seen.discard(normalized)


def make_author_tools(
    store: QuestionStore,
    *,
    idents: list[str],
    execute_sql,
) -> list[Any]:
    """The author's ``submit_question`` / ``forfeit_slot`` delivery tools.

    Validation runs AT SUBMIT TIME and failures come back as corrective tool
    results (the cheapest fix point — the author still has its context and its
    exploration). ``execute_sql(sql) -> (rows, error)`` runs a gold through the
    grader-shaped runner.
    """
    from langchain_core.tools import tool

    @tool
    def submit_question(
        slot: int,
        question: str,
        gold_sql: str = "",
        expected_behavior: str = "",
    ) -> str:
        """Deliver ONE finished question for a slot you own. `slot` is the slot
        number from your worklist. Accuracy slots need `gold_sql` (it will be
        executed to verify it); behavior slots need `expected_behavior`. A
        rejection message tells you exactly what to fix — fix it and resubmit
        the SAME slot."""
        with store._lock:
            spec = store.slots.get(int(slot))
            if spec is None:
                return (
                    f"rejected: slot {slot} is not on your worklist — your slots "
                    f"are {sorted(store.slots)}"
                )
            if int(slot) in store.filled:
                return f"rejected: slot {slot} is already filled"
            # A standing forfeit is NOT popped here: it clears only when this
            # submission is ACCEPTED (the commit below). Popping early erased
            # the forfeit — and its concrete reason — on every rejected
            # resubmission, and a forfeit landing during the unlocked
            # validation window could leave the slot filled AND forfeited.

        question = (question or "").strip()
        if len(question) < 12:
            return "rejected: the question text is empty or too short to be a real question"
        check = spec["check"]
        gold_sql = (gold_sql or "").strip()
        expected_behavior = (expected_behavior or "").strip()
        if check == CHECK_SQL:
            if not gold_sql:
                return (
                    f"rejected: slot {slot} is an ACCURACY slot — it needs `gold_sql` "
                    "(the SQL whose result set IS the correct answer)"
                )
            if expected_behavior:
                return (
                    "rejected: accuracy slots carry gold_sql only — put behavior "
                    "expectations on a behavior slot"
                )
            # The one-statement check reads the MASK (literals/comments
            # blanked): a semicolon inside a string literal — `code = 'A;B'`
            # — is data, not a statement separator, and rejecting it made
            # perfectly valid golds permanently unsubmittable.
            if not _SQL_SHAPE_RE.match(gold_sql) or ";" in _mask_sql(
                gold_sql
            ).rstrip().rstrip(";"):
                return (
                    "rejected: gold_sql must be ONE SELECT/WITH statement "
                    "(no DDL/DML, no multiple statements)"
                )
        else:
            if not expected_behavior:
                return (
                    f"rejected: slot {slot} is a BEHAVIOR slot — it needs "
                    "`expected_behavior` (free-form prose: what a good answer DOES)"
                )
            if gold_sql:
                return (
                    "rejected: behavior slots carry expected_behavior only — a "
                    "gold_sql belongs on an accuracy slot"
                )

        # Business-language lint: questions must read like a user, not a DBA.
        # Meta/introspection is exempt — asking what tables exist IS the point.
        if spec["dimension"] != "meta_introspection":
            leaked = _leaked_identifier(question, idents)
            if leaked:
                return (
                    f"rejected: the question names the physical identifier "
                    f"`{leaked}` — rephrase it in business language (the way the "
                    ".context/ docs talk about this data); the SQL is where "
                    "physical names belong"
                )

        normalized = _normalize_question(question)
        if not store.claim_question(normalized):
            return (
                "rejected: an equivalent question was already submitted (possibly "
                "by another dimension's author) — ask something genuinely different"
            )

        validation: dict[str, Any] = {}
        if check == CHECK_SQL:
            rows, error = execute_sql(gold_sql)
            if error:
                store.release_question(normalized)
                return (
                    f"rejected: gold_sql did not execute — {error}. Fix the SQL "
                    "(check .metadata/columns.tsv for exact names/types) and "
                    "resubmit the same slot"
                )
            if not rows:
                store.release_question(normalized)
                return (
                    "rejected: gold_sql returned 0 rows — an empty gold grades "
                    "everything as equal-to-empty. Check the filter values against "
                    "the profile sheets (a typo?) or ask about data that exists"
                )
            validation = {"executed": True, "row_count": len(rows)}

        with store._lock:
            # Re-check under the lock: the model can emit PARALLEL tool calls,
            # and two submits for one slot both passed the early check.
            if int(slot) in store.filled:
                store.release_question(normalized)
                return f"rejected: slot {slot} is already filled"
            # ACCEPTED: a fill beats any forfeit, including one that landed
            # during the validation window — cleared atomically with the fill
            # so the slot can never be both.
            store.forfeited.pop(int(slot), None)
            store.filled[int(slot)] = {
                "question": question,
                "check": check,
                "gold_sql": gold_sql,
                "expected_behavior": expected_behavior,
                "tier": spec["tier"],
                "dimension": spec["dimension"],
                "validation": validation,
            }
            remaining = store.unresolved()
        store.changed()
        return (
            f"accepted: slot {slot} filled"
            + (f" (gold executed, {validation.get('row_count')} rows)" if validation else "")
            + (
                f". Remaining slots: {remaining}"
                if remaining
                else ". All your slots are resolved — finish with a one-line summary."
            )
        )

    @tool
    def forfeit_slot(slot: int, reason: str) -> str:
        """Give up ONE slot you genuinely cannot fill (e.g. the schema has no
        temporal columns for a projection question). Requires a concrete
        reason; forfeited slots are reassigned to another dimension."""
        reason = (reason or "").strip()
        with store._lock:
            if int(slot) not in store.slots:
                return f"rejected: slot {slot} is not on your worklist"
            if int(slot) in store.filled:
                return f"rejected: slot {slot} is already filled — nothing to forfeit"
            if len(reason) < 10:
                return (
                    "rejected: a forfeit needs a concrete reason (what makes this "
                    "slot unfillable for this dataset?)"
                )
            store.forfeited[int(slot)] = reason
            remaining = store.unresolved()
        store.changed()
        return f"slot {slot} forfeited." + (
            f" Remaining slots: {remaining}" if remaining else " All slots resolved."
        )

    return [submit_question, forfeit_slot]


# --------------------------------------------------------------------------- #
# the quota gate
# --------------------------------------------------------------------------- #

try:  # langchain is only present in the runtime image
    from harvest.benchmark.react import AgentMiddleware, hook_config

    _BASE = AgentMiddleware
except Exception:  # pragma: no cover - import-time fallback where absent
    _BASE = object

    def hook_config(*_a: Any, **_k: Any):  # type: ignore[no-redef]
        def _decorate(fn):
            return fn

        return _decorate


class AuthorQuotaMiddleware(_BASE):  # type: ignore[misc, valid-type]
    """Refuse to let an author finish while slots are silently unresolved.

    :class:`~harvest.benchmark.react.SubmitToolNudgeMiddleware` checks "was the
    tool ever called"; this checks the STORE — every slot must be filled or
    explicitly forfeited before the agent may end. On a tool-call-free final
    message with unresolved slots it injects a nudge NAMING them and jumps back
    to the model, at most ``max_nudges`` times (recovered from the conversation
    via a stable prefix — one agent instance is one conversation here, but the
    stateless recovery keeps the pattern uniform). After the last nudge the run
    ends and the unresolved slots flow to the backfill round — the quota is
    enforced by the PIPELINE, the middleware just makes silence impossible.
    """

    def __init__(self, store: QuestionStore, *, max_nudges: int = 2):
        super().__init__()
        self.store = store
        self.max_nudges = max(0, int(max_nudges))

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime):  # type: ignore[override]
        from harvest.benchmark.extract import message_text
        from harvest.benchmark.react import _is_final_ai_turn
        from langchain.messages import HumanMessage

        messages = (state or {}).get("messages") or []
        last = messages[-1] if messages else None
        if not _is_final_ai_turn(last):
            return None  # still working — never interfere mid-flight
        unresolved = self.store.unresolved()
        if not unresolved:
            return None  # quota met (filled or explicitly forfeited)
        nudges = sum(
            1
            for m in messages
            if getattr(m, "type", "") == "human"
            and message_text(getattr(m, "content", "")).startswith(_NUDGE_PREFIX)
        )
        if nudges >= self.max_nudges:
            log.warning(
                "Author ended with %d unresolved slot(s) after %d nudge(s); "
                "handing them to the backfill.",
                len(unresolved),
                nudges,
            )
            return None
        nudge = (
            f"{_NUDGE_PREFIX}You still have unresolved slot(s): {unresolved}. "
            "Every slot must be either FILLED via submit_question or explicitly "
            "FORFEITED via forfeit_slot with a concrete reason — finishing "
            "without resolving them is not an option. Continue now."
        )
        return {"jump_to": "model", "messages": [HumanMessage(content=nudge)]}


# --------------------------------------------------------------------------- #
# prompts
# --------------------------------------------------------------------------- #

AUTHOR_SYSTEM_PROMPT = """\
You are authoring benchmark questions for a data wiki's evaluation suite. You
see the dataset's GROUND TRUTH only: the `.metadata/` catalog snapshot
(`columns.tsv`, per-table docs, `profile/<table>.md` value profiles,
`relationships/` join/grain evidence) and `.context/` (the customer's uploaded
source documents — business vocabulary, formulas, policies). You do NOT see
the wiki being evaluated — that is deliberate. You also have live read-only
SQL tools against the real data ({dialect} dialect).

Your worklist (in the user message) assigns you numbered SLOTS, each with a
dimension, a complexity tier, and a check type. Deliver every slot through the
`submit_question` tool — prose does not count. A slot you genuinely cannot
fill for THIS dataset must be forfeited via `forfeit_slot` with a concrete
reason. You may not finish while any slot is neither filled nor forfeited.

Method (per slot):
1. Ground it: read `.metadata/columns.tsv` first, then the profile sheets and
   relationship evidence for the tables involved; read `.context/` docs for
   the business language and any defined KPIs.
2. Phrase the question the way a BUSINESS USER would ask it — never use
   physical table/column identifiers (the lint rejects snake_case names and
   `table.column` references; meta/introspection slots are exempt). Use the
   vocabulary from `.context/` where it exists.
3. ACCURACY (sql) slots: write `gold_sql` — ONE SELECT/WITH statement in
   {dialect} whose result set IS the correct answer. Verify it yourself with
   `run_sql` BEFORE submitting; the submit tool executes it again and rejects
   golds that fail or return 0 rows. Prefer questions with stable answers
   (avoid "current month" phrasing).
4. BEHAVIOR slots: write `expected_behavior` — free-form prose stating what a
   good answer DOES (states a fact, refuses, says something is not tracked,
   asks which reading is meant, cites an assumption). Verify the premise
   first: an "is not tracked" expectation requires confirming no such column
   exists; a join-trap expectation requires the relationship evidence to show
   the fan-out.
5. Tier discipline: easy = {tier_easy}; medium = {tier_medium}; hard =
   {tier_hard}.

Quality bar: a question must be answerable (or correctly refusable) from the
data alone, unambiguous unless ambiguity IS the point of its dimension, and
genuinely different from your other submissions. Rejections tell you exactly
what to fix — fix and resubmit the same slot.

When every slot is resolved, reply with a one-line summary."""


def _author_prompt(dialect: str, *, gpt: bool) -> str:
    """The author system prompt, with the model-family runtime addendum.

    ``harvest.prompts._with_gpt`` appends exactly one of the two family blocks
    (derived from the provider prompting guides in ``prompts/``): the GPT
    addendum's ``<persistence>`` targets the exact failure mode the quota gate
    exists for (handing back at uncertainty — fewer nudges, fewer backfills),
    and the Claude addendum's ``<no_narration>`` keeps a long multi-question
    authoring conversation from paying for narration on every turn.
    """
    from harvest.prompts import _with_gpt

    return _with_gpt(
        AUTHOR_SYSTEM_PROMPT.format(
            dialect=dialect,
            tier_easy=qbank.TIER_BRIEFS[qbank.TIER_EASY],
            tier_medium=qbank.TIER_BRIEFS[qbank.TIER_MEDIUM],
            tier_hard=qbank.TIER_BRIEFS[qbank.TIER_HARD],
        ),
        gpt,
    )


def _worklist_message(
    data_domain: str, dataset: str, batch: dict[int, dict[str, str]], *, note: str = ""
) -> str:
    lines = [
        f"Dataset: {data_domain}/{dataset}.",
        note or "",
        "Your slots:",
    ]
    for n in sorted(batch):
        spec = batch[n]
        dim = qbank.dimension(spec["dimension"])
        lines.append(
            f"- slot {n}: [{spec['check']}] [{spec['tier']}] {dim.title} — {dim.brief}"
        )
    return "\n".join(line for line in lines if line)


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #


def _chunk_slots(
    unresolved: dict[int, dict[str, str]]
) -> list[dict[int, dict[str, str]]]:
    """The backfill worklist split into round-1-sized author batches
    (``_BACKFILL_BATCH_SLOTS``), slot order preserved."""
    items = sorted(unresolved.items())
    return [
        dict(items[i : i + _BACKFILL_BATCH_SLOTS])
        for i in range(0, len(items), _BACKFILL_BATCH_SLOTS)
    ]


def _execute(payload: dict, qbank_id: str, row) -> dict[str, Any]:
    """The run body: config → slots → ground-truth tree → author fan-out →
    backfill → artifact doc."""
    import shutil

    from harvest.agent import _build_model, _is_openai_model, resolve_model_config
    from harvest.clients import build_source

    data_domain = payload["data_domain"]
    dataset = payload["dataset"]
    config = qbank.validate_config(payload.get("config"))
    slots = qbank.allocate_slots(config)

    tree_dir = tempfile.mkdtemp(prefix=f"okf-qgen-{qbank_id}-")
    try:
        materialize_ground_truth(
            _s3_client(),
            bucket=default_bucket(),
            data_domain=data_domain,
            dataset=dataset,
            dest_dir=tree_dir,
        )
        source = build_source(dataset, source=payload.get("source"))
        model_cfg = resolve_model_config(payload.get("model"), payload.get("effort"))
        model = _build_model(
            model_cfg["model"], model_cfg["effort"], model_cfg["max_tokens"]
        )
        # The family addendum tracks the model that READS the prompt (see
        # _author_prompt) — resolved once, threaded through both rounds.
        gpt = _is_openai_model(model_cfg["model"])

        # The leakage lint's identifier list, from the same columns.tsv the
        # authors read (text parse — no okf_core.semantic dependency).
        idents = _physical_identifiers(
            _parse_columns_tsv(os.path.join(tree_dir, ".metadata", "columns.tsv"))
        )

        # The grader's OWN cap readers (studio.py) — one implementation, so
        # generation-time gold validation can never drift from the caps
        # grading enforces (drift here would resurrect DISCARDED questions,
        # the exact failure this feature promises away).
        from harvest.benchmark.studio import grader_max_rows, grader_timeout_s

        timeout_s = grader_timeout_s()
        max_rows = grader_max_rows()

        def execute_sql(sql: str) -> tuple[list, str]:
            """(rows, error) through the grader-shaped runner (positional rows,
            grading caps) — the validation must use the same path grading will."""
            try:
                _header, rows = source.run_query(
                    sql, timeout_s=timeout_s, max_rows=max_rows, positional=True
                )
                return rows, ""
            except Exception as e:  # noqa: BLE001 - engine text is the correction
                return [], f"{type(e).__name__}: {e}"

        numbered = {i + 1: slot for i, slot in enumerate(slots)}
        seen: set[str] = set()
        seen_lock = threading.Lock()
        usage_totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        progress = _Progress(row, total=len(numbered))

        # -- round 1: one author per dimension ---------------------------------
        batches: dict[str, dict[int, dict[str, str]]] = {}
        for n, spec in numbered.items():
            batches.setdefault(spec["dimension"], {})[n] = spec

        jobs: list[tuple[str, QuestionStore]] = []
        for _dim, batch in sorted(batches.items()):
            store = QuestionStore(
                batch, seen=seen, seen_lock=seen_lock, on_change=progress.tick
            )
            progress.watch(store)
            jobs.append((_worklist_message(data_domain, dataset, batch), store))
        progress.tick(force=True)

        run_authors_kwargs = dict(
            model=model,
            tree_dir=tree_dir,
            source=source,
            idents=idents,
            execute_sql=execute_sql,
            usage_totals=usage_totals,
            gpt=gpt,
        )

        async def _rounds() -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
            # BOTH rounds live on ONE event loop: the model client is shared,
            # and a GPT-family client pools httpx connections bound to the
            # loop they were opened on — a second asyncio.run() handed the
            # backfill a client full of connections to a CLOSED loop, and its
            # first request died with "Event loop is closed" disguised as
            # author inability.
            stores = await _run_authors(jobs, **run_authors_kwargs)

            filled: dict[int, dict[str, Any]] = {}
            unresolved: dict[int, dict[str, str]] = {}
            forfeit_reasons: dict[int, str] = {}
            for store in stores:
                filled.update(store.filled)
                forfeit_reasons.update(store.forfeited)
                for n in store.unresolved():
                    unresolved[n] = store.slots[n]
            for n, reason in forfeit_reasons.items():
                unresolved.setdefault(n, numbered[n])

            # -- round 2: backfill authors over everything unresolved ----------
            # Slots keep their dimension/tier/check: the mix is what the user
            # configured, and silently reshaping it would misreport what the
            # bank tests. CHUNKED to the same worklist size a round-1 author
            # gets — one giant backfill author would burn its recursion budget
            # after ~15 slots and mass-drop the tail. A slot no backfill author
            # fills either is DROPPED with its reason — a short bank that says
            # so beats a full bank that lies.
            dropped: list[dict[str, Any]] = []
            if unresolved:
                progress.phase = "backfill"
                note = (
                    "This is the BACKFILL round: earlier authors could not fill "
                    "these slots (forfeits and quota misses). You may pick ANY angle "
                    "within each slot's dimension/tier/check. Forfeit only what is "
                    "truly impossible for this dataset."
                )
                backfill_stores: list[QuestionStore] = []
                backfill_jobs: list[tuple[str, QuestionStore]] = []
                for chunk in _chunk_slots(unresolved):
                    bstore = QuestionStore(
                        chunk, seen=seen, seen_lock=seen_lock, on_change=progress.tick
                    )
                    progress.watch(bstore)
                    backfill_stores.append(bstore)
                    backfill_jobs.append(
                        (
                            _worklist_message(data_domain, dataset, chunk, note=note),
                            bstore,
                        )
                    )
                progress.tick(force=True)
                await _run_authors(backfill_jobs, **run_authors_kwargs)
                backfill_filled: dict[int, dict[str, Any]] = {}
                backfill_forfeits: dict[int, str] = {}
                for bstore in backfill_stores:
                    backfill_filled.update(bstore.filled)
                    backfill_forfeits.update(bstore.forfeited)
                filled.update(backfill_filled)
                for n, spec in sorted(unresolved.items()):
                    if n in backfill_filled:
                        continue
                    reason = (
                        backfill_forfeits.get(n)
                        or forfeit_reasons.get(n)
                        or "the author could not produce a valid question for this slot"
                    )
                    dropped.append({"slot": n, **spec, "reason": reason})
            return filled, dropped

        filled, dropped = asyncio.run(_rounds())

        questions = [filled[n] for n in sorted(filled)]
        counts = {
            "requested": len(numbered),
            "delivered": len(questions),
            **qbank.summarize(questions),
        }
        return {
            "qbank_id": qbank_id,
            "data_domain": data_domain,
            "dataset": dataset,
            "completed_at": _now_iso(),
            "config": {
                **config,
                qbank.FIELD_MODEL: model_cfg["model"],
                qbank.FIELD_EFFORT: model_cfg["effort"],
            },
            "questions": questions,
            "dropped": dropped,
            "counts": counts,
            "telemetry": {"tokens": dict(usage_totals)},
        }
    finally:
        shutil.rmtree(tree_dir, ignore_errors=True)


async def _run_authors(
    jobs: list[tuple[str, QuestionStore]],
    *,
    model,
    tree_dir: str,
    source,
    idents: list[str],
    execute_sql,
    usage_totals: dict[str, int],
    gpt: bool = False,
) -> list[QuestionStore]:
    """Fan the author batches out under the concurrency cap; return the stores.

    A single author crashing (model fault, recursion limit) must not sink the
    whole bank: its exception is logged, its partial store ships, and its
    unfilled slots flow to the backfill like any other quota miss.
    """
    from harvest.benchmark.react import is_recursion_limit, make_react_agent
    from harvest.benchmark.solver import fold_usage, make_readonly_file_tools
    from harvest.source_tools import make_source_tools

    semaphore = asyncio.Semaphore(_max_concurrency())
    dialect = source.prompt_profile.dialect

    async def run_one(user_msg: str, store: QuestionStore) -> None:
        agent = make_react_agent(
            model,
            [
                *make_readonly_file_tools(tree_dir, scope="dataset"),
                *make_source_tools(source),
                *make_author_tools(store, idents=idents, execute_sql=execute_sql),
            ],
            _author_prompt(dialect, gpt=gpt),
            extra_middleware=[AuthorQuotaMiddleware(store)],
        )
        async with semaphore:
            messages: list = []
            try:
                async for state in agent.astream(
                    {"messages": [("user", user_msg)]},
                    config={"recursion_limit": _AUTHOR_RECURSION_LIMIT},
                    stream_mode="values",
                ):
                    if isinstance(state, dict) and state.get("messages"):
                        messages = state["messages"]
            except Exception as e:  # noqa: BLE001 - partial store beats a dead bank
                if is_recursion_limit(e):
                    log.warning(
                        "Author hit its step budget with %d unresolved slot(s); "
                        "the backfill owns them.",
                        len(store.unresolved()),
                    )
                else:
                    log.warning("Author batch failed (continuing)", exc_info=True)
            usage = fold_usage(messages)
            for k in usage_totals:
                usage_totals[k] += int(usage.get(k) or 0)

    await asyncio.gather(*(run_one(msg, store) for msg, store in jobs))
    return [store for _msg, store in jobs]


class _Progress:
    """Throttled row progress for the polling UI: resolved slots / total.

    Wired into every store's ``on_change``, so the row moves per question, not
    per author batch. Throttled to one write per 2s (the same cadence
    ``RowProgress`` uses); ``force=True`` for phase transitions.
    """

    def __init__(self, row, *, total: int, min_interval_s: float = 2.0):
        self._row = row
        self.total = total
        self.phase = "authoring"
        self._last: float | None = None
        self._min_interval = min_interval_s
        self._lock = threading.Lock()
        self._stores: list[QuestionStore] = []

    def watch(self, store: QuestionStore) -> None:
        self._stores.append(store)

    def tick(self, force: bool = False) -> None:
        with self._lock:
            now = time.monotonic()
            if not force and self._last is not None and now - self._last < self._min_interval:
                return
            self._last = now
            # Each slot counts ONCE, judged by the LATEST store that owns it:
            # a backfill store re-issues round-1 forfeits/misses, so summing
            # every store double-counted those slots (once as the old forfeit,
            # once as the new fill) and pinned the bar at 100% for the whole
            # backfill phase. Later watch() registrations win.
            owner: dict[int, QuestionStore] = {}
            for s in self._stores:
                for n in s.slots:
                    owner[n] = s
            resolved = sum(
                1 for n, s in owner.items() if n in s.filled or n in s.forfeited
            )
        self._row(
            {
                "phase": self.phase,
                "progress_current": min(resolved, self.total),
                "progress_total": self.total,
            }
        )


def _parse_columns_tsv(path: str) -> dict[str, dict[str, str]]:
    """``{table: {column: type}}`` from the snapshot's columns.tsv.

    A thin adapter over ``okf_core.lint.read_columns_tsv`` — the ONE
    header-driven parser (lint's snapshot checks read the same file through
    it, so the two can never disagree on the format). Best-effort: the
    leakage lint degrades to dotted-reference-only when the file is
    unreadable or the header is unrecognizable.
    """
    from pathlib import Path

    from okf_core.lint import read_columns_tsv

    return read_columns_tsv(Path(path)) or {}
