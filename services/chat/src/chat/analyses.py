"""Analysis templates: saved, human-owned procedures the chat agent executes.

An analysis is one markdown document with YAML frontmatter: ``title`` +
``description`` (the discovery surface ``list_analyses`` shows), ``questions``
— the prerequisite inputs, each a VERBATIM ``ask_human`` question object
(``{id, prompt, kind, options}``; explicit unique ids, deliberately NO
defaults — an analysis asks, the user answers) — and a body of numbered steps
the agent follows. Because the questions are byte-for-byte ``ask_human``
payloads, validation reuses :func:`chat.ask_human.normalize_questions`, the
chat runner forwards them in one ``ask_human`` call unchanged, and a future
headless trigger is just the same questions arriving pre-answered.

Rows live on the analyses table keyed by dataset (``okf_core.analyses`` holds
the item conventions): any user may list/read/execute; only the recorded
owner may edit — and deletion is deliberately NOT an agent tool (the human
does it on the Analysis page, where the Control API enforces the same
ownership). ``update_analysis`` is a verbatim exact-match text
replacement (the Edit-tool contract: unique ``old_string`` → ``new_string``),
re-validated whole and version-bumped under an optimistic lock — never a
blind full-body overwrite.

The methodology lives in two vendored skills served by ``read_skill``:
``analysis-authoring`` (document shape, question design, the computation-
first source ladder) and ``analysis-execution`` (resolve-then-ask, follow
the steps, finalize as a report).
"""

from __future__ import annotations

import functools
import inspect
import logging
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import StructuredTool

from okf_core import analyses as an

from chat.ask_human import AskHumanError, normalize_questions

log = logging.getLogger(__name__)


def parse_analysis(text: str) -> tuple[dict[str, Any], list[str]]:
    """Parse + validate one analysis document; returns ``(parsed, errors)``.

    The rules live in ONE place — :func:`okf_core.analyses.parse_analysis`
    (shared with the Control API's manual-edit path). On a clean parse the
    chat side additionally runs the raw questions through the REAL ask_human
    normalizer, so ``parsed["questions"]`` is byte-for-byte the interrupt
    payload the executor forwards — validation and runtime can never drift.
    """
    parsed, errors = an.parse_analysis(text)
    if not errors and parsed.get("questions"):
        try:
            parsed = {**parsed, "questions": normalize_questions(parsed["questions"])}
        except AskHumanError as e:  # pragma: no cover — okf_core mirrors these rules
            errors = [str(e)]
    return parsed, errors


def make_analysis_tools(
    table,
    *,
    user_sub: str,
    dataset_scope: dict[str, str] | None = None,
    s3=None,
    bundle_bucket: str = "",
    registry=None,
):
    """The analysis tools: list / read / create / update (+ publish).

    ``registry`` (the resource-style registry Table) lets create refuse an
    unregistered dataset; None skips that check.

    ``table`` is the resource-style analyses Table (rows keyed by dataset —
    see okf_core.analyses). ``user_sub`` is closed over from the validated
    JWT: it is recorded as the owner on create and enforced on update.
    Closure-based like the report tools; a pinned conversation's scope
    overrides the location args.

    ``publish_report`` binds a produced report to the analysis that produced
    it (a publication row — the run's durable artifact, surviving thread
    deletion); it needs ``s3`` + ``bundle_bucket`` to verify the report's
    artifacts exist, so it is bound only when both are supplied (the same
    deploy shape that binds the report tools themselves).
    """
    from botocore.exceptions import ClientError

    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _scope() -> tuple[str, str] | None:
        if dataset_scope:
            return dataset_scope["data_domain"], dataset_scope["dataset"]
        return None

    def _location(data_domain: str, dataset: str) -> tuple[str, str]:
        pinned = _scope()
        if pinned:
            return pinned
        dd, ds = (data_domain or "").strip(), (dataset or "").strip()
        if not (dd and ds):
            raise ValueError(
                "name the dataset — data_domain + dataset (see list_domains)"
            )
        if not (
            an.LOCATION_SEGMENT_RE.match(dd) and an.LOCATION_SEGMENT_RE.match(ds)
        ):
            raise ValueError(
                f"{dd!r}/{ds!r} is not a dataset location — use the exact "
                "data_domain and dataset list_domains shows"
            )
        return dd, ds

    def _require_slug(name: str) -> None:
        # Read paths too: a non-slug can't name a document row, and one like
        # 'x#pub#rep~…' would address a PUBLICATION row as if it were one.
        if not an.SLUG_RE.match(name):
            raise ValueError(
                f"no analysis {name!r} — names are slugs; list_analyses shows "
                "what exists"
            )

    def _get(dd: str, ds: str, name: str) -> dict | None:
        return table.get_item(
            Key={"pk": an.analysis_pk(dd, ds), "sk": name}
        ).get("Item")

    def _refuse_unless_owner(row: dict, verb: str) -> None:
        if row.get("owner_sub") != user_sub:
            raise ValueError(
                f"only the owner can {verb} an analysis — this one belongs to "
                "another user. Relay the requested change to them, or save an "
                "adapted copy under a new name with create_analysis"
            )

    def _list(data_domain: str = "", dataset: str = "") -> Any:
        from boto3.dynamodb.conditions import Key

        dd, ds = _location(data_domain, dataset)
        items: list[dict] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(an.analysis_pk(dd, ds)),
            # The listing never needs the ≤32KB document body — shipping it
            # per row only to discard it made every list pay the whole
            # bundle's weight. (#n: `name` is a DynamoDB reserved word.)
            "ProjectionExpression": "sk, #n, title, description, questions, "
            "owner_sub, version, updated_at",
            "ExpressionAttributeNames": {"#n": "name"},
        }
        while True:
            resp = table.query(**kwargs)
            items.extend(resp.get("Items", []))
            start = resp.get("LastEvaluatedKey")
            if not start:
                break
            kwargs["ExclusiveStartKey"] = start
        # Document rows only — publication rows share the partition (their sk
        # carries the #pub# separator) and are counted per analysis instead.
        pub_counts: dict[str, int] = {}
        for i in items:
            sk = str(i.get("sk") or "")
            if an.is_publication_sk(sk):
                owner = sk.split(an.PUBLICATION_SK_SEP, 1)[0]
                pub_counts[owner] = pub_counts.get(owner, 0) + 1
        rows = [
            {
                "name": i.get("name"),
                "title": i.get("title"),
                "description": i.get("description"),
                "questions": int(i.get("questions") or 0),
                "owned_by_you": i.get("owner_sub") == user_sub,
                "version": int(i.get("version") or 1),
                "published_reports": pub_counts.get(str(i.get("sk") or ""), 0),
                "updated_at": i.get("updated_at"),
            }
            for i in items
            if not an.is_publication_sk(str(i.get("sk") or ""))
        ]
        rows.sort(key=lambda r: str(r.get("name") or ""))
        return {"data_domain": dd, "dataset": ds, "analyses": rows}

    def _publications(dd: str, ds: str, name: str) -> list[dict]:
        from boto3.dynamodb.conditions import Key

        items: list[dict] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(an.analysis_pk(dd, ds))
            & Key("sk").begins_with(an.publication_sk_prefix(name))
        }
        while True:
            resp = table.query(**kwargs)
            items.extend(resp.get("Items", []))
            start = resp.get("LastEvaluatedKey")
            if not start:
                break
            kwargs["ExclusiveStartKey"] = start
        pubs = [
            {
                "report_id": i.get("report_id"),
                "title": i.get("title"),
                "analysis_version": int(i.get("analysis_version") or 1),
                "published_by_you": i.get("published_by") == user_sub,
                "published_at": i.get("published_at"),
            }
            for i in items
        ]
        pubs.sort(key=lambda p: str(p.get("published_at") or ""), reverse=True)
        return pubs

    def _read(name: str, data_domain: str = "", dataset: str = "") -> Any:
        dd, ds = _location(data_domain, dataset)
        name = (name or "").strip()
        _require_slug(name)
        row = _get(dd, ds, name)
        if not row:
            raise ValueError(
                f"no analysis {name!r} for {dd}/{ds} — list_analyses shows what exists"
            )
        return {
            "name": row.get("name"),
            "version": int(row.get("version") or 1),
            "owned_by_you": row.get("owner_sub") == user_sub,
            "updated_at": row.get("updated_at"),
            # Prior runs' artifacts — re-showable via present_report(report_id).
            "published_reports": _publications(dd, ds, name),
            # The FULL document (frontmatter + steps) — the procedure to follow.
            "document": row.get("body"),
        }

    def _create(
        name: str, body: str, data_domain: str = "", dataset: str = ""
    ) -> Any:
        dd, ds = _location(data_domain, dataset)
        name = (name or "").strip()
        if not an.SLUG_RE.match(name):
            raise ValueError(
                f"invalid name {name!r} — a slug: lowercase letters/digits/"
                "hyphen/underscore, max 64 chars (e.g. churn-cohort-deep-dive)"
            )
        # Only into a REGISTERED dataset: a typo'd location would store rows
        # no dataset deletion ever purges (skipped when no registry handle is
        # wired — the offline tests).
        if registry is not None and not registry.get_item(
            Key={"pk": f"DOMAIN#{dd}", "sk": f"DATASET#{ds}"}
        ).get("Item"):
            raise ValueError(
                f"{dd}/{ds} is not a registered dataset — list_domains shows "
                "the exact data_domain and dataset"
            )
        parsed, errors = parse_analysis(body or "")
        if errors:
            return {"error": "the document does not validate", "problems": errors}
        now = _now()
        try:
            table.put_item(
                Item={
                    "pk": an.analysis_pk(dd, ds),
                    "sk": name,
                    "name": name,
                    "data_domain": dd,
                    "dataset": ds,
                    "title": parsed["title"],
                    "description": parsed["description"],
                    "questions": len(parsed["questions"]),
                    "owner_sub": user_sub,
                    "created_at": now,
                    "updated_at": now,
                    "version": 1,
                    "body": body,
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            raise ValueError(
                f"an analysis named {name!r} already exists for {dd}/{ds} — "
                "read_analysis it; edit via update_analysis (owner-only) or "
                "pick a different name"
            ) from e
        return {
            "saved": name,
            "version": 1,
            "note": f"the analysis is saved for {dd}/{ds} under the user's "
            "ownership — anyone with access to this dataset can now run it",
        }

    def _update(
        name: str,
        old_string: str,
        new_string: str,
        data_domain: str = "",
        dataset: str = "",
    ) -> Any:
        dd, ds = _location(data_domain, dataset)
        name = (name or "").strip()
        _require_slug(name)
        row = _get(dd, ds, name)
        if not row:
            raise ValueError(
                f"no analysis {name!r} for {dd}/{ds} — list_analyses shows what exists"
            )
        _refuse_unless_owner(row, "edit")
        if not old_string:
            raise ValueError("old_string is empty — copy the exact text to replace")
        if old_string == new_string:
            raise ValueError("old_string and new_string are identical — nothing to change")
        body = str(row.get("body") or "")
        count = body.count(old_string)
        if count == 0:
            raise ValueError(
                "old_string not found in the stored document — read_analysis "
                "and copy the text VERBATIM (whitespace included)"
            )
        if count > 1:
            raise ValueError(
                f"old_string appears {count} times — include more surrounding "
                "context so it matches exactly once"
            )
        new_body = body.replace(old_string, new_string)
        parsed, errors = parse_analysis(new_body)
        if errors:
            return {
                "error": "the edit would leave an invalid document — nothing was saved",
                "problems": errors,
            }
        version = int(row.get("version") or 1)
        try:
            table.put_item(
                Item={
                    **row,
                    "title": parsed["title"],
                    "description": parsed["description"],
                    "questions": len(parsed["questions"]),
                    "updated_at": _now(),
                    "version": version + 1,
                    "body": new_body,
                },
                # Optimistic lock: the row must still be the one we edited,
                # and still the caller's own. (#v: defensive aliasing against
                # the reserved-word list.)
                ConditionExpression="#v = :v AND owner_sub = :o",
                ExpressionAttributeNames={"#v": "version"},
                ExpressionAttributeValues={":v": version, ":o": user_sub},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            raise ValueError(
                "the analysis changed while you were editing — read_analysis "
                "again and re-apply the edit"
            ) from e
        return {"saved": name, "version": version + 1}

    def _publish(
        report_id: str, analysis: str, data_domain: str = "", dataset: str = ""
    ) -> Any:
        import json

        from okf_core import reports as rp

        dd, ds = _location(data_domain, dataset)
        analysis = (analysis or "").strip()
        _require_slug(analysis)
        report_id = (report_id or "").strip()
        row = _get(dd, ds, analysis)
        if not row:
            raise ValueError(
                f"no analysis {analysis!r} for {dd}/{ds} — publish binds a "
                "report to an EXISTING analysis (list_analyses shows what exists)"
            )
        coords = rp.parse_report_id(report_id)
        if coords is None:
            raise ValueError(
                f"{report_id!r} is not a report id — use the exact report_id "
                "a create_report call returned"
            )
        if coords["domain"] != dd or coords["dataset"] != ds:
            raise ValueError(
                f"report {report_id} belongs to {coords['domain']}/"
                f"{coords['dataset']} — it cannot be published onto a "
                f"{dd}/{ds} analysis"
            )
        # The report's stored source is the existence check AND the title's
        # authority (blocks.json is self-describing; reports have no DB row).
        prefix = rp.report_s3_prefix(dd, ds, coords["stamp"], coords["suffix"])
        try:
            body = s3.get_object(
                Bucket=bundle_bucket, Key=rp.report_blocks_key(prefix)
            )["Body"].read()
            title = str(json.loads(body).get("title") or "Report")
        except Exception as e:  # noqa: BLE001 — missing/unreadable = not publishable
            raise ValueError(
                f"report {report_id} has no readable artifacts — publish only "
                "a report_id that create_report returned in THIS deployment"
            ) from e
        # One transaction: the Put is conditioned on the publication key being
        # new, AND a ConditionCheck asserts the analysis DOCUMENT still exists
        # at write time. The plain read-then-put raced the Analysis page's
        # delete (read sees the doc → owner deletes it and purges publications
        # → the put lands anyway), minting a publication row no surface could
        # ever remove short of dataset deletion.
        #
        # PLAIN values throughout: ``table.meta.client`` shares the RESOURCE's
        # event system, whose boto3 document transform auto-serializes
        # transact params — pre-serialized AttributeValues get double-wrapped
        # and the whole transaction dies.
        item = {
            "pk": an.analysis_pk(dd, ds),
            "sk": an.publication_sk(analysis, report_id),
            "name": analysis,
            "data_domain": dd,
            "dataset": ds,
            "report_id": report_id,
            "title": title,
            "analysis_version": int(row.get("version") or 1),
            "published_by": user_sub,
            "published_at": _now(),
        }
        try:
            table.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "ConditionCheck": {
                            "TableName": table.table_name,
                            "Key": {
                                "pk": an.analysis_pk(dd, ds),
                                "sk": analysis,
                            },
                            "ConditionExpression": "attribute_exists(pk)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": table.table_name,
                            "Item": item,
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            reasons = e.response.get("CancellationReasons") or []

            def _failed(i: int) -> bool:
                return (
                    len(reasons) > i
                    and (reasons[i] or {}).get("Code") == "ConditionalCheckFailed"
                )

            if _failed(0):
                raise ValueError(
                    f"analysis {analysis!r} was deleted while publishing — "
                    "nothing was recorded"
                ) from e
            if _failed(1):
                raise ValueError(
                    f"report {report_id} is already published to {analysis!r}"
                ) from e
            raise ValueError(
                f"could not publish {report_id} to {analysis!r} — the analysis "
                "changed underneath the publish; retry"
            ) from e
        return {
            "published": report_id,
            "analysis": analysis,
            "title": title,
            "note": "the report is now a durable artifact of this analysis — "
            "it stays reachable from the analysis even if this conversation "
            "is deleted",
        }

    def _guarded(fn, name: str):
        # Tool containment: an unexpected raise (a DDB throttle,
        # expired creds) must come back as a tool RESULT the model can react
        # to, not abort the run; a ValueError is deliberate feedback.
        @functools.wraps(fn)
        def wrapper(**kwargs: Any) -> Any:
            try:
                return fn(**kwargs)
            except ValueError as e:
                return f"Error: {e}"
            except Exception as e:  # noqa: BLE001 — feedback, not a crash
                log.warning("chat tool %s failed", name, exc_info=True)
                return f"Error: {name} failed: {type(e).__name__}: {e}"

        return wrapper

    unscoped = (
        ""
        if dataset_scope
        else " `data_domain`/`dataset` name the dataset (see list_domains)."
    )
    list_doc = inspect.cleandoc(
        """List the saved ANALYSES for a dataset — reusable, human-owned
        procedure documents (inputs to gather from the user + the exact steps
        to execute + how to finalize). Call this when the user asks to run an
        analysis, refers to one by name, or asks what analyses exist — and
        BEFORE authoring a new one (an existing analysis may already cover
        the request). Returns per analysis: name, title, description, how
        many prerequisite questions it asks, and whether the current user
        owns it (only the owner can edit; deleting an analysis is a manual
        act on the app's Analysis page — there is no delete tool: when the
        user asks you to delete one, point them there)."""
    )
    read_doc = inspect.cleandoc(
        """Read the full analysis document: frontmatter (title, description,
        the prerequisite `questions`) + the steps. To EXECUTE it, first read
        read_skill("analysis-execution") (once per conversation), then follow
        the document — it is the procedure, not inspiration. Also the way to
        show the user what an analysis does, and the required first step
        before editing one (update_analysis replaces text you copy from
        here verbatim)."""
    )
    create_doc = inspect.cleandoc(
        """Save a NEW analysis for this dataset. Read
        read_skill("analysis-authoring") FIRST (once per conversation) — it
        defines the document shape, question design, the Grounding citations
        (wiki concept docs + computations every step traces to), and the
        computation-first sourcing rules. `name` is a slug (e.g.
        churn-cohort-deep-dive); `body` is the COMPLETE markdown document:
        YAML frontmatter (title, description, questions — each a verbatim
        ask_human object {id, prompt, kind, options} with an explicit unique
        id and NO defaults) followed by the steps. Draft it WITH the user and
        only save what they agreed to; the save validates and refuses with
        named problems. The analysis is recorded under the current user's
        ownership."""
    )
    update_doc = inspect.cleandoc(
        """Edit an analysis the current user OWNS, as an exact-match text
        replacement: `old_string` must match the stored document VERBATIM
        (copy it from read_analysis, whitespace included) and match exactly
        once — include surrounding context to disambiguate; it is replaced
        with `new_string`. The edited document is re-validated whole (an
        edit that would break the frontmatter or questions is refused with
        the problems named) and the version bumps. For edits requested on
        someone else's analysis, relay to the owner or save an adapted copy
        under a new name."""
    )
    publish_doc = inspect.cleandoc(
        """Bind a finished report to the analysis that produced it — the
        run's DURABLE artifact: the report stays reachable from the analysis
        (read_analysis lists it, the analysis panel shows it) even after
        this conversation is deleted. Call it once after the
        create_report + present_report of an ANALYSIS EXECUTION, with the
        exact report_id create_report returned and the analysis name; not
        for ad-hoc reports the user asked for outside an analysis. Each run
        publishes its own report; publishing the same report twice is
        refused."""
    )

    # A PINNED conversation drops the location params from the model schema
    # entirely (the governed-tools convention, chat/tools.py): with the args
    # merely overridden, the model could still pass a WRONG dataset that the
    # pin silently ignores server-side while surviving into the streamed
    # chunk — mislabeling the UI step, poisoning memory's dataset
    # observation, and 404ing the AnalysisPeek affordance. The server's
    # _with_scope fold re-injects the true location for those consumers.
    if dataset_scope:

        def list_analyses() -> Any:
            return _list()

        def read_analysis(name: str) -> Any:
            return _read(name)

        def create_analysis(name: str, body: str) -> Any:
            return _create(name, body)

        def update_analysis(name: str, old_string: str, new_string: str) -> Any:
            return _update(name, old_string, new_string)

        def publish_report(report_id: str, analysis: str) -> Any:
            return _publish(report_id, analysis)

    else:
        list_analyses = _list
        read_analysis = _read
        create_analysis = _create
        update_analysis = _update
        publish_report = _publish

    tools = [
        StructuredTool.from_function(
            _guarded(list_analyses, "list_analyses"),
            name="list_analyses", description=list_doc + unscoped,
        ),
        StructuredTool.from_function(
            _guarded(read_analysis, "read_analysis"),
            name="read_analysis", description=read_doc + unscoped,
        ),
        StructuredTool.from_function(
            _guarded(create_analysis, "create_analysis"),
            name="create_analysis", description=create_doc + unscoped,
        ),
        StructuredTool.from_function(
            _guarded(update_analysis, "update_analysis"),
            name="update_analysis", description=update_doc + unscoped,
        ),
    ]
    if s3 is not None and bundle_bucket:
        tools.append(
            StructuredTool.from_function(
                _guarded(publish_report, "publish_report"),
                name="publish_report", description=publish_doc + unscoped,
            )
        )
    return tools
