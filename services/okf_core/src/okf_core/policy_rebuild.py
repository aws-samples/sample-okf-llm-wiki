"""The ``policy_rebuild`` event — one spelling of the AR freshness accelerator.

An Automated Reasoning policy is a derived artifact of the bundle, so a wiki
mutation that no harvest finalize covered (a repromote/version restore, or a
drift a chat policy-check notices) leaves the policy describing a superseded
wiki. This event exists ONLY to shorten that dark window: it pokes the rebuild
authority (the incremental service) so the repair starts in build wall-clock
(minutes) instead of at the next nightly pass.

It carries no correctness weight. Correctness comes from two other mechanisms
that hold even if every event is lost:

* the **fingerprint gate** — a policy renders a verdict only when its stamped
  ``ar_source_hash`` still equals the freshly computed
  :func:`okf_core.ar_sources.compute_source_hash` of the live sources, so a
  policy built off an older wiki is unusable, not wrong;
* the **nightly reconcile** — it hash-verifies every AR-enabled dataset and
  rebuilds the mismatched ones.

That is why every publisher (the Control API's repromote path, the chat
runtime's lazy stale discovery) treats the ``PutEvents`` call as best-effort:
a failed publish degrades freshness, never truth.

**Duplicates are harmless by construction.** A build starts only via the
conditional flip of ``ar_build_status`` to ``building`` on the dataset's
registry row, so N events for one dataset collapse to one build — publishers
never need to deduplicate, and the consumer never needs an idempotency token.

Pure Python (no AWS deps): the two publishers, the consumer's detail-type
dispatch, and the Terraform rule pattern all describe the same event, and this
module is the single place those strings and that shape are written down.
"""

from __future__ import annotations

from typing import Any, Mapping

#: ``PutEvents`` ``Source``. A CUSTOM source: EventBridge rejects any source
#: beginning with ``aws.``, and the consumer's rule matches on source AND
#: detail-type so a future ``okf.*`` event can't be swallowed by it.
EVENT_SOURCE = "okf.policy"

#: ``PutEvents`` ``DetailType``. Rides the SAME EventBridge -> SQS -> handler
#: path the Glue change events use, which is why the consumer must dispatch on
#: the envelope's detail-type BEFORE unwrapping the detail: a policy-rebuild
#: detail has no ``databaseName``/``tableName`` and would otherwise be
#: misread as an unmapped Glue change (a silent no-op).
DETAIL_TYPE_POLICY_REBUILD = "policy_rebuild"

#: Detail field names — spelled once so the publishers and the consumer cannot
#: drift on them.
FIELD_DATA_DOMAIN = "data_domain"
FIELD_DATASET = "dataset"
#: Optional publisher breadcrumb (``"repromote"``, ``"policy_check"``) that
#: only ever reaches logs: the consumer runs the same rebuild pipeline whatever
#: the reason, so it is deliberately NOT part of :func:`parse_detail`'s result.
FIELD_REASON = "reason"


def build_detail(data_domain: str, dataset: str, *, reason: str = "") -> dict[str, Any]:
    """The ``Detail`` payload (a dict — the caller JSON-encodes it).

    ``data_domain``/``dataset`` identify the dataset whose policy is suspect;
    both must be non-empty (an event that names no dataset can only be dropped
    by the consumer, so it is rejected at the publisher instead). ``reason`` is
    an optional breadcrumb for the consumer's log and is omitted when empty, so
    the default payload is exactly the two identifying fields.
    """
    domain = (data_domain or "").strip()
    name = (dataset or "").strip()
    if not domain or not name:
        raise ValueError(
            "build_detail requires a non-empty data_domain and dataset, got "
            f"{data_domain!r}/{dataset!r}"
        )
    detail: dict[str, Any] = {FIELD_DATA_DOMAIN: domain, FIELD_DATASET: name}
    if (reason or "").strip():
        detail[FIELD_REASON] = reason.strip()
    return detail


def parse_detail(detail: Mapping[str, Any] | Any) -> tuple[str, str]:
    """``(data_domain, dataset)`` from a received detail, or ``ValueError``.

    ``detail`` is the already-decoded ``detail`` object of the EventBridge
    envelope. Unknown keys (a publisher's :data:`FIELD_REASON`, or fields a
    later version adds) are ignored — the event is a poke, and forward
    compatibility matters more than strictness about extras. What is NOT
    tolerated is a payload that fails to name a dataset: raising here keeps the
    consumer's "malformed event" branch (log + skip, never a queue retry) in
    one obvious place instead of letting blank identifiers reach S3 keys and
    registry keys built from them.
    """
    if not isinstance(detail, Mapping):
        raise ValueError(
            f"policy_rebuild detail must be a mapping, got {type(detail).__name__}"
        )
    values: list[str] = []
    for field in (FIELD_DATA_DOMAIN, FIELD_DATASET):
        value = detail.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"policy_rebuild detail is missing a usable {field!r}: {detail!r}"
            )
        values.append(value.strip())
    return values[0], values[1]
