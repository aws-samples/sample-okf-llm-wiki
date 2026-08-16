"""In-memory fakes for the clients the Control API doesn't get from moto.

moto covers S3 + DynamoDB well; Glue's ``get_databases`` and the
``bedrock-agentcore`` data-plane are simpler to fake here so tests stay fast and
assert on the exact call shapes (payload bytes, session id).
"""

from __future__ import annotations

import json
from typing import Any


class _GlueEntityNotFound(Exception):
    """Mimic botocore's ClientError shape for Glue's EntityNotFoundException."""

    def __init__(self, database: str):
        super().__init__(f"Database {database} not found.")
        self.response = {"Error": {"Code": "EntityNotFoundException"}}


class FakeGlue:
    """Glue client returning canned databases, optionally across two pages."""

    def __init__(self, databases: list[dict[str, Any]], page_size: int | None = None):
        self._databases = databases
        self._page_size = page_size

    def get_databases(self, **kwargs) -> dict:
        if self._page_size is None:
            return {"DatabaseList": list(self._databases)}
        # Simulate NextToken pagination in fixed-size pages.
        start = int(kwargs.get("NextToken", "0"))
        page = self._databases[start : start + self._page_size]
        resp: dict[str, Any] = {"DatabaseList": page}
        nxt = start + self._page_size
        if nxt < len(self._databases):
            resp["NextToken"] = str(nxt)
        return resp

    def get_tables(self, **kwargs) -> dict:
        """Empty table list for a known database; EntityNotFound otherwise.

        Mirrors the real Glue call the harvest runtime makes, so the Control
        API's existence check can be exercised. The name check is all the
        boundary validation needs (table contents are irrelevant here).
        """
        name = kwargs.get("DatabaseName")
        if not any(db.get("Name") == name for db in self._databases):
            raise _GlueEntityNotFound(name)
        return {"TableList": []}


class FakeRedshift:
    """redshift control-plane fake: describe_clusters (Marker pagination)."""

    def __init__(self, clusters: list[dict[str, Any]] | None = None):
        self._clusters = clusters or []

    def describe_clusters(self, **kwargs) -> dict:
        return {"Clusters": list(self._clusters)}


class FakeRedshiftServerless:
    """redshift-serverless control-plane fake: list_workgroups (nextToken)."""

    def __init__(self, workgroups: list[dict[str, Any]] | None = None):
        self._workgroups = workgroups or []

    def list_workgroups(self, **kwargs) -> dict:
        return {"workgroups": list(self._workgroups)}


class FakeRedshiftData:
    """redshift-data fake: list_databases keyed by the connection target.

    ``databases_by_target`` maps a cluster id / workgroup name -> the DB names it
    returns. A call for an unknown target (or missing secret) mimics the Data API
    raising, which the handler maps to a clean 400.
    """

    def __init__(self, databases_by_target: dict[str, list[str]] | None = None):
        self._by_target = databases_by_target or {}
        self.calls: list[dict[str, Any]] = []

    def list_databases(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        target = kwargs.get("ClusterIdentifier") or kwargs.get("WorkgroupName")
        if target not in self._by_target:
            raise RuntimeError(f"cannot connect to {target!r}")
        return {"Databases": list(self._by_target[target])}


class FakeAgentCore:
    """bedrock-agentcore data-plane fake capturing every invoke_agent_runtime call."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.stop_calls: list[dict[str, Any]] = []
        # Optional synchronous ack the runtime would answer with (the harvest
        # entrypoint's {"status": "accepted"|"rejected", ...}). None mimics an
        # ack-less/streaming response — callers must tolerate both.
        self.ack: dict[str, Any] | None = None

    def invoke_agent_runtime(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        resp: dict[str, Any] = {"statusCode": 200}
        if self.ack is not None:
            import io

            resp["response"] = io.BytesIO(json.dumps(self.ack).encode())
        return resp

    def stop_runtime_session(self, **kwargs) -> dict:
        self.stop_calls.append(kwargs)
        return {"statusCode": 200, "runtimeSessionId": kwargs.get("runtimeSessionId")}

    # convenience for assertions
    def last_payload(self) -> dict[str, Any]:
        return json.loads(self.calls[-1]["payload"].decode())


class _MemoryRecordNotFound(Exception):
    """Mimic botocore's ClientError shape for a missing memory record."""

    def __init__(self, record_id: str):
        super().__init__(f"Memory record {record_id} not found.")
        self.response = {"Error": {"Code": "ResourceNotFoundException"}}


class FakeAgentCoreMemory:
    """bedrock-agentcore Memory data-plane fake (the Memory page's routes).

    Seeded with raw record dicts (``memoryRecordId``, ``content.text``,
    ``namespaces``). ``list_memory_records`` filters by the requested namespace
    and, when ``page_size`` is set, paginates in fixed-size pages via an
    integer-offset ``nextToken`` so the handlers' exhaustion loop is exercised.
    get/update/delete on an unknown id raise the botocore-shaped
    ResourceNotFoundException. Every call is recorded for asserts.
    """

    def __init__(
        self,
        records: list[dict[str, Any]] | None = None,
        page_size: int | None = None,
    ):
        self.records: dict[str, dict[str, Any]] = {
            r["memoryRecordId"]: dict(r) for r in (records or [])
        }
        self._page_size = page_size
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []

    def list_memory_records(self, **kwargs) -> dict:
        self.list_calls.append(kwargs)
        ns = kwargs.get("namespace")
        matched = [
            dict(r)
            for r in self.records.values()
            if ns is None or ns in (r.get("namespaces") or [])
        ]
        if self._page_size is None:
            return {"memoryRecordSummaries": matched}
        start = int(kwargs.get("nextToken", "0"))
        page = matched[start : start + self._page_size]
        resp: dict[str, Any] = {"memoryRecordSummaries": page}
        nxt = start + self._page_size
        if nxt < len(matched):
            resp["nextToken"] = str(nxt)
        return resp

    def get_memory_record(self, **kwargs) -> dict:
        self.get_calls.append(kwargs)
        record_id = kwargs.get("memoryRecordId")
        if record_id not in self.records:
            raise _MemoryRecordNotFound(record_id)
        return {"memoryRecord": dict(self.records[record_id])}

    def batch_update_memory_records(self, **kwargs) -> dict:
        self.update_calls.append(kwargs)
        successful = []
        failed = []
        for update in kwargs.get("records", []):
            record_id = update["memoryRecordId"]
            # Per the service model, a missing record is a per-record entry in
            # failedRecords inside a 200 response — NOT an exception. The
            # handler must check, or it fabricates a success.
            if record_id not in self.records:
                failed.append(
                    {
                        "memoryRecordId": record_id,
                        "errorMessage": f"no such record: {record_id}",
                    }
                )
                continue
            stored = self.records[record_id]
            if update.get("content"):
                stored["content"] = dict(update["content"])
            if update.get("namespaces"):
                stored["namespaces"] = list(update["namespaces"])
            # Pessimistic REPLACE semantics for metadata (the real behavior is
            # undocumented): an update that omits it strips the stored fields,
            # so a handler that forgets to re-supply metadata loses type/
            # dataset/expires here exactly as it might live.
            stored["metadata"] = (
                dict(update["metadata"]) if update.get("metadata") else {}
            )
            successful.append({"memoryRecordId": record_id, "status": "SUCCEEDED"})
        return {"successfulRecords": successful, "failedRecords": failed}

    def delete_memory_record(self, **kwargs) -> dict:
        self.delete_calls.append(kwargs)
        record_id = kwargs.get("memoryRecordId")
        if record_id not in self.records:
            raise _MemoryRecordNotFound(record_id)
        del self.records[record_id]
        return {"memoryRecordId": record_id}


class _CognitoResourceNotFound(Exception):
    """Mimic botocore's ClientError shape for Cognito's ResourceNotFoundException."""

    def __init__(self, client_id: str):
        super().__init__(f"App client {client_id} not found.")
        self.response = {"Error": {"Code": "ResourceNotFoundException"}}


class FakeCognito:
    """cognito-idp fake for M2M app-client create/delete.

    Mints deterministic client_id/secret per creation (index-based, since the
    workflow env forbids random/time), records every call, and mimics the
    ResourceNotFoundException on deleting an unknown client so the handler's
    idempotent-revoke path is exercisable.
    """

    def __init__(self):
        self.clients: dict[str, dict[str, Any]] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self._n = 0

    def create_user_pool_client(self, **kwargs) -> dict:
        self.create_calls.append(kwargs)
        self._n += 1
        client_id = f"m2mclient{self._n}"
        record = {
            "ClientId": client_id,
            "ClientSecret": f"secret-{self._n}",
            "ClientName": kwargs.get("ClientName"),
            "AllowedOAuthScopes": kwargs.get("AllowedOAuthScopes"),
            "AllowedOAuthFlows": kwargs.get("AllowedOAuthFlows"),
        }
        self.clients[client_id] = record
        return {"UserPoolClient": record}

    def delete_user_pool_client(self, **kwargs) -> dict:
        self.delete_calls.append(kwargs)
        client_id = kwargs.get("ClientId")
        if client_id not in self.clients:
            raise _CognitoResourceNotFound(client_id)
        del self.clients[client_id]
        return {}


class FakeLogs:
    """CloudWatch Logs fake for the harvest step-feed reader.

    Holds pre-seeded log events per group and applies a minimal substring
    ``filterPattern`` (quoted terms ANDed) like FilterLogEvents. Supports a single
    ``nextToken`` page split so pagination is exercised. Records calls for asserts.
    """

    def __init__(self, events_by_group: dict[str, list[dict[str, Any]]] | None = None):
        # {group_name: [{"message": str, "timestamp": int}, ...]}
        self._events = events_by_group or {}
        self.calls: list[dict[str, Any]] = []
        self.page_size: int | None = None  # None = single page

    def _matches(self, message: str, pattern: str | None) -> bool:
        if not pattern:
            return True
        # CloudWatch quoted-term pattern: extract "..." terms, all must be present.
        import re

        terms = re.findall(r'"([^"]*)"', pattern) or [pattern]
        return all(t in message for t in terms)

    def filter_log_events(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        group = kwargs.get("logGroupName")
        pattern = kwargs.get("filterPattern")
        # Honor startTime like the real API: only events at/after it are returned.
        start_time = kwargs.get("startTime")
        matched = [
            e
            for e in self._events.get(group, [])
            if self._matches(e.get("message", ""), pattern)
            and (start_time is None or e.get("timestamp", 0) >= start_time)
        ]
        if self.page_size is None:
            return {"events": matched}
        # Paginate: nextToken is the integer offset of the next page.
        start = int(kwargs.get("nextToken", "0"))
        page = matched[start : start + self.page_size]
        resp: dict[str, Any] = {"events": page}
        nxt = start + self.page_size
        if nxt < len(matched):
            resp["nextToken"] = str(nxt)
        return resp


class FakeEvents:
    """EventBridge fake capturing every ``put_events`` call verbatim.

    Hand-rolled rather than moto-backed because the assertions are about the
    exact wire shape (Source/DetailType/Detail) a consuming rule pattern must
    match, which a real bus would swallow. ``raises=True`` simulates an
    unreachable/denied publisher; ``fail_entries=True`` the nastier case of a
    200 response that rejected the entry anyway.
    """

    def __init__(self, raises: bool = False, fail_entries: bool = False):
        self.calls: list[dict[str, Any]] = []
        self._raises = raises
        self._fail_entries = fail_entries

    def put_events(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        if self._raises:
            raise RuntimeError("events unreachable")
        entries = kwargs.get("Entries", [])
        if self._fail_entries:
            return {
                "FailedEntryCount": len(entries),
                "Entries": [
                    {"ErrorCode": "InternalException", "ErrorMessage": "nope"}
                    for _ in entries
                ],
            }
        return {
            "FailedEntryCount": 0,
            "Entries": [{"EventId": f"evt-{i}"} for i, _ in enumerate(entries)],
        }

    # convenience for assertions (mirrors FakeAgentCore.last_payload)
    def last_detail(self) -> dict[str, Any]:
        return json.loads(self.calls[-1]["Entries"][0]["Detail"])
