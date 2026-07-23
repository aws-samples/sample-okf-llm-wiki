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

    def invoke_agent_runtime(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return {"statusCode": 200}

    def stop_runtime_session(self, **kwargs) -> dict:
        self.stop_calls.append(kwargs)
        return {"statusCode": 200, "runtimeSessionId": kwargs.get("runtimeSessionId")}

    # convenience for assertions
    def last_payload(self) -> dict[str, Any]:
        return json.loads(self.calls[-1]["payload"].decode())


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
