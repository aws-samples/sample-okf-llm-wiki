"""Per-invocation credential down-scoping (threats #9/#60).

Exercises the pure session-policy builder and the ambient-fallback / scoped-assume
branching in build_source, without touching AWS: build_scoped_session is stubbed
so we assert WHICH policy is minted and that clients come from the scoped session.
"""

from __future__ import annotations

import json

import pytest

from harvest import clients

REGION = "us-east-1"
ACCOUNT = "123456789012"
DB = "na_mi_formula_1_curated"
WG = "okf-harvest"


def _policy(**over):
    kwargs = dict(
        region=REGION,
        account_id=ACCOUNT,
        database=DB,
        workgroup=WG,
        results_bucket_arn="arn:aws:s3:::okf-athena-results-123456789012",
    )
    kwargs.update(over)
    return clients._session_policy(**kwargs)


def _sids(policy):
    return {s["Sid"]: s for s in policy["Statement"]}


def test_session_policy_pins_glue_db_and_tables():
    sids = _sids(_policy())
    res = sids["GlueThisDb"]["Resource"]
    assert f"arn:aws:glue:{REGION}:{ACCOUNT}:database/{DB}" in res
    assert f"arn:aws:glue:{REGION}:{ACCOUNT}:table/{DB}/*" in res
    assert f"arn:aws:glue:{REGION}:{ACCOUNT}:catalog" in res
    # No OTHER database is reachable via the pinned table ARN.
    assert not any("table/other_db/" in r for r in res)


def test_session_policy_getdatabases_is_catalog_only():
    # The plural LIST call cannot be pinned to one db; it targets catalog only.
    sids = _sids(_policy())
    assert sids["GlueListDbs"]["Resource"] == [
        f"arn:aws:glue:{REGION}:{ACCOUNT}:catalog"
    ]
    assert sids["GlueListDbs"]["Action"] == ["glue:GetDatabases"]


def test_session_policy_athena_scoped_to_workgroup():
    sids = _sids(_policy())
    assert sids["AthenaThisWorkgroup"]["Resource"] == [
        f"arn:aws:athena:{REGION}:{ACCOUNT}:workgroup/{WG}"
    ]


def test_session_policy_table_data_read_stays_broad():
    # Glue tables point at arbitrary buckets -> object read cannot be dataset-scoped.
    sids = _sids(_policy())
    assert sids["TableDataRead"]["Resource"] == ["*"]
    assert sids["TableDataRead"]["Action"] == [
        "s3:GetObject",
        "s3:ListBucket",
        "s3:GetBucketLocation",
    ]


def test_session_policy_omits_results_write_when_no_bucket():
    sids = _sids(_policy(results_bucket_arn=None))
    assert "AthenaResultsWrite" not in sids


def test_session_policy_under_inline_limit():
    # Inline session policies are capped at 2048 chars.
    assert len(json.dumps(_policy())) < 2048


def test_session_policy_lakeformation_off_by_default():
    # No lakeformation:GetDataAccess unless explicitly enabled.
    sids = _sids(_policy())
    assert "LakeFormationDataAccess" not in sids


def test_session_policy_lakeformation_when_enabled():
    sids = _sids(_policy(enable_lakeformation=True))
    assert sids["LakeFormationDataAccess"]["Action"] == ["lakeformation:GetDataAccess"]
    # Still under the inline cap with the extra statement.
    assert len(json.dumps(_policy(enable_lakeformation=True))) < 2048


def test_build_source_threads_lakeformation_env_into_policy(monkeypatch):
    # build_source reads OKF_ENABLE_LAKEFORMATION and passes it into the session
    # policy it mints, so LF-vended data access reaches the scoped credential.
    monkeypatch.setenv(
        "OKF_HARVEST_DATA_ROLE_ARN", f"arn:aws:iam::{ACCOUNT}:role/okf-harvest-data"
    )
    monkeypatch.setenv("OKF_ATHENA_WORKGROUP", WG)
    monkeypatch.setenv("OKF_ATHENA_OUTPUT", "s3://okf-athena-results-x/harvest/")
    monkeypatch.setenv("OKF_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("OKF_ENABLE_LAKEFORMATION", "true")

    captured = {}

    def _fake_build_scoped(
        *, role_arn, session_policy, region, session_name="okf-harvest"
    ):
        captured["policy"] = session_policy
        return _FakeSession()

    monkeypatch.setattr(clients, "build_scoped_session", _fake_build_scoped)
    clients.build_source(DB, region=REGION, account_id=ACCOUNT)
    sids = _sids(captured["policy"])
    assert "LakeFormationDataAccess" in sids


@pytest.mark.parametrize(
    "loc,expected",
    [
        ("s3://okf-athena-results-123/harvest/", "arn:aws:s3:::okf-athena-results-123"),
        ("s3://bucket-only", "arn:aws:s3:::bucket-only"),
        ("", None),
        (None, None),
        ("https://not-s3/x", None),
    ],
)
def test_results_bucket_arn(loc, expected):
    assert clients._results_bucket_arn(loc) == expected


# --- build_source branching -------------------------------------------------


class _FakeClient:
    def __init__(self, name):
        self.name = name


class _FakeSession:
    """Stand-in for the scoped boto3.Session; records that it was used."""

    def __init__(self):
        self.made = []

    def client(self, name, **kw):
        self.made.append(name)
        return _FakeClient(f"scoped:{name}")


def test_build_source_uses_scoped_session_when_data_role_set(monkeypatch):
    monkeypatch.setenv(
        "OKF_HARVEST_DATA_ROLE_ARN", f"arn:aws:iam::{ACCOUNT}:role/okf-harvest-data"
    )
    monkeypatch.setenv("OKF_ATHENA_WORKGROUP", WG)
    monkeypatch.setenv("OKF_ATHENA_OUTPUT", "s3://okf-athena-results-x/harvest/")
    monkeypatch.setenv("OKF_ACCOUNT_ID", ACCOUNT)

    captured = {}
    fake = _FakeSession()

    def _fake_build_scoped(
        *, role_arn, session_policy, region, session_name="okf-harvest"
    ):
        captured["role_arn"] = role_arn
        captured["policy"] = session_policy
        captured["region"] = region
        return fake

    monkeypatch.setattr(clients, "build_scoped_session", _fake_build_scoped)

    src = clients.build_source(DB, region=REGION, account_id=ACCOUNT)

    # Clients came from the SCOPED session, not ambient boto3.
    assert src.glue.name == "scoped:glue"
    assert src.athena.name == "scoped:athena"
    assert fake.made == ["glue", "athena"]
    # The minted policy pins THIS database.
    sids = _sids(captured["policy"])
    assert (
        f"arn:aws:glue:{REGION}:{ACCOUNT}:database/{DB}"
        in sids["GlueThisDb"]["Resource"]
    )
    assert captured["role_arn"].endswith(":role/okf-harvest-data")


def test_build_source_falls_back_to_ambient_when_no_data_role(monkeypatch):
    monkeypatch.delenv("OKF_HARVEST_DATA_ROLE_ARN", raising=False)
    monkeypatch.setenv("OKF_ACCOUNT_ID", ACCOUNT)

    made = []

    class _FakeBoto:
        def client(self, name, **kw):
            made.append(name)
            return _FakeClient(f"ambient:{name}")

    monkeypatch.setattr(
        clients, "build_scoped_session", lambda **k: pytest.fail("should not assume")
    )
    import sys
    import types

    fake_boto3 = types.SimpleNamespace(client=_FakeBoto().client)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    src = clients.build_source(DB, region=REGION, account_id=ACCOUNT)
    assert src.glue.name == "ambient:glue"
    assert src.athena.name == "ambient:athena"


def test_build_scoped_session_assumes_and_refreshes(monkeypatch):
    """The refresh closure is really wired: build_scoped_session assumes the data
    role with the pinned policy up front, and a forced refresh re-assumes it."""
    from datetime import datetime, timedelta, timezone

    calls = []

    class _FakeSts:
        def assume_role(self, **kw):
            calls.append(kw)
            # Fresh expiry each call so RefreshableCredentials accepts it.
            return {
                "Credentials": {
                    "AccessKeyId": f"AKIA{len(calls)}",
                    "SecretAccessKey": "secret",
                    "SessionToken": "token",
                    "Expiration": datetime.now(timezone.utc) + timedelta(hours=1),
                }
            }

    # Stub boto3.client so build_scoped_session's sts client is our fake; boto3.Session
    # is still needed at the end, so keep the real one.
    import boto3 as _real_boto3

    class _FakeBoto3:
        Session = _real_boto3.Session

        @staticmethod
        def client(name, **kw):
            assert name == "sts"
            return _FakeSts()

    import sys

    monkeypatch.setitem(sys.modules, "boto3", _FakeBoto3)

    policy = _policy()
    session = clients.build_scoped_session(
        role_arn=f"arn:aws:iam::{ACCOUNT}:role/okf-harvest-data",
        session_policy=policy,
        region=REGION,
        session_name="okf-harvest-f1",
    )

    # (a) Initial assume happened with the pinned policy + 1h duration.
    assert len(calls) == 1
    first = calls[0]
    assert first["RoleArn"].endswith(":role/okf-harvest-data")
    assert first["DurationSeconds"] == clients._ASSUME_DURATION_SECONDS == 3600
    assert first["RoleSessionName"] == "okf-harvest-f1"
    assert (
        json.loads(first["Policy"]) == policy
    )  # same pinned policy re-sent on refresh

    # (b) Force a refresh through botocore and confirm it re-assumes (same policy).
    # Expire the creds, then access them the way SigV4 signing does
    # (get_frozen_credentials) — RefreshableCredentials re-invokes the closure.
    creds = session.get_credentials()
    creds._expiry_time = datetime.now(timezone.utc) - timedelta(minutes=1)
    frozen = creds.get_frozen_credentials()
    assert len(calls) == 2
    assert calls[1]["Policy"] == calls[0]["Policy"]
    # The signer now sees the re-minted key from the second assume.
    assert frozen.access_key == "AKIA2"


def test_build_source_fails_open_to_ambient_on_assume_error(monkeypatch):
    # A broken trust/role must not brick every harvest: fall back to ambient creds.
    monkeypatch.setenv(
        "OKF_HARVEST_DATA_ROLE_ARN", f"arn:aws:iam::{ACCOUNT}:role/okf-harvest-data"
    )
    monkeypatch.setenv("OKF_ACCOUNT_ID", ACCOUNT)

    def _boom(**k):
        raise RuntimeError("AccessDenied: cannot assume")

    monkeypatch.setattr(clients, "build_scoped_session", _boom)

    made = []

    class _FakeBoto:
        def client(self, name, **kw):
            made.append(name)
            return _FakeClient(f"ambient:{name}")

    import sys
    import types

    monkeypatch.setitem(
        sys.modules, "boto3", types.SimpleNamespace(client=_FakeBoto().client)
    )

    src = clients.build_source(DB, region=REGION, account_id=ACCOUNT)
    assert src.glue.name == "ambient:glue"
    assert src.athena.name == "ambient:athena"


# --- source-descriptor dispatch (Phase D) -----------------------------------


def test_build_source_glue_reads_database_from_descriptor(monkeypatch):
    # The glue database comes from the descriptor, NOT the (possibly different)
    # dataset id — this is what lets a mapping name a db != dataset in future.
    monkeypatch.delenv("OKF_HARVEST_DATA_ROLE_ARN", raising=False)
    monkeypatch.setenv("OKF_ACCOUNT_ID", ACCOUNT)

    class _FakeBoto:
        def client(self, name, **kw):
            return _FakeClient(f"ambient:{name}")

    import sys
    import types

    monkeypatch.setitem(
        sys.modules, "boto3", types.SimpleNamespace(client=_FakeBoto().client)
    )
    src = clients.build_source(
        "dataset_alias",
        source={"type": "glue", "glue_database": "real_glue_db"},
        region=REGION,
        account_id=ACCOUNT,
    )
    assert src.name == "glue"
    assert src.database == "real_glue_db"


def test_build_source_defaults_to_glue_by_dataset_when_no_descriptor(monkeypatch):
    # Back-compat: an absent descriptor -> a glue source named by the dataset.
    monkeypatch.delenv("OKF_HARVEST_DATA_ROLE_ARN", raising=False)
    monkeypatch.setenv("OKF_ACCOUNT_ID", ACCOUNT)

    class _FakeBoto:
        def client(self, name, **kw):
            return _FakeClient(f"ambient:{name}")

    import sys
    import types

    monkeypatch.setitem(
        sys.modules, "boto3", types.SimpleNamespace(client=_FakeBoto().client)
    )
    src = clients.build_source(DB, region=REGION, account_id=ACCOUNT)
    assert src.name == "glue"
    assert src.database == DB


def test_build_source_redshift_branch_scoped(monkeypatch):
    monkeypatch.setenv(
        "OKF_HARVEST_DATA_ROLE_ARN", f"arn:aws:iam::{ACCOUNT}:role/okf-harvest-data"
    )
    monkeypatch.setenv("OKF_ACCOUNT_ID", ACCOUNT)

    captured = {}
    fake = _FakeSession()

    def _fake_build_scoped(
        *, role_arn, session_policy, region, session_name="okf-harvest"
    ):
        captured["policy"] = session_policy
        return fake

    monkeypatch.setattr(clients, "build_scoped_session", _fake_build_scoped)
    # Connection comes from the self-describing descriptor (no deploy-time env).
    src = clients.build_source(
        "dev",
        source={
            "type": "redshift",
            "redshift_database": "dev",
            "cluster_identifier": "f1-cluster",
            "secret_arn": "arn:aws:secretsmanager:us-east-1:1:secret:f1",
        },
        region=REGION,
        account_id=ACCOUNT,
    )
    assert src.name == "redshift"
    assert src.database == "dev"
    # The redshift-data client came from the scoped session.
    assert src.data.name == "scoped:redshift-data"
    assert fake.made == ["redshift-data"]
    # The policy grants the Data API + cluster auth, pinned to the cluster.
    sids = _sids(captured["policy"])
    assert "RedshiftDataApi" in sids
    assert sids["RedshiftClusterAuth"]["Action"] == ["redshift:GetClusterCredentials"]
    assert any("f1-cluster" in r for r in sids["RedshiftClusterAuth"]["Resource"])


def test_build_source_redshift_branch_ambient(monkeypatch):
    monkeypatch.delenv("OKF_HARVEST_DATA_ROLE_ARN", raising=False)
    monkeypatch.setenv("OKF_ACCOUNT_ID", ACCOUNT)

    made = []

    class _FakeBoto:
        def client(self, name, **kw):
            made.append(name)
            return _FakeClient(f"ambient:{name}")

    import sys
    import types

    monkeypatch.setitem(
        sys.modules, "boto3", types.SimpleNamespace(client=_FakeBoto().client)
    )
    src = clients.build_source(
        "dev",
        source={
            "type": "redshift",
            "redshift_database": "dev",
            "workgroup_name": "wg1",
            "secret_arn": "arn:aws:secretsmanager:us-east-1:1:secret:wg1",
        },
        region=REGION,
        account_id=ACCOUNT,
    )
    assert src.data.name == "ambient:redshift-data"
    assert src.workgroup_name == "wg1"
    assert made == ["redshift-data"]


def test_build_source_redshift_reads_connection_from_descriptor(monkeypatch):
    # A self-describing mapping's cluster/workgroup + secret come from the
    # DESCRIPTOR — so any cluster in the account is harvestable with no deploy env.
    monkeypatch.setenv(
        "OKF_HARVEST_DATA_ROLE_ARN", f"arn:aws:iam::{ACCOUNT}:role/okf-harvest-data"
    )
    monkeypatch.setenv("OKF_ACCOUNT_ID", ACCOUNT)

    captured = {}
    fake = _FakeSession()

    def _fake_build_scoped(
        *, role_arn, session_policy, region, session_name="okf-harvest"
    ):
        captured["policy"] = session_policy
        return fake

    monkeypatch.setattr(clients, "build_scoped_session", _fake_build_scoped)
    src = clients.build_source(
        "dev",
        source={
            "type": "redshift",
            "redshift_database": "dev",
            "workgroup_name": "mapping-wg",
            "secret_arn": "arn:aws:secretsmanager:us-east-1:1:secret:map",
        },
        region=REGION,
        account_id=ACCOUNT,
    )
    # The descriptor's workgroup + secret were used, NOT the env cluster.
    assert src.workgroup_name == "mapping-wg"
    assert src.cluster_identifier is None
    assert src.secret_arn.endswith(":secret:map")
    sids = _sids(captured["policy"])
    assert "RedshiftServerlessAuth" in sids
    assert sids["RedshiftSecretRead"]["Resource"] == [
        "arn:aws:secretsmanager:us-east-1:1:secret:map"
    ]


def test_build_source_redshift_bare_descriptor_has_no_target(monkeypatch):
    # A db-only descriptor (no cluster/workgroup) can't connect — there is no
    # deploy-time env fallback anymore, so RedshiftSource raises.
    monkeypatch.delenv("OKF_HARVEST_DATA_ROLE_ARN", raising=False)
    monkeypatch.setenv("OKF_ACCOUNT_ID", ACCOUNT)

    import sys
    import types

    monkeypatch.setitem(
        sys.modules,
        "boto3",
        types.SimpleNamespace(client=lambda name, **kw: _FakeClient(f"ambient:{name}")),
    )
    with pytest.raises(ValueError, match="cluster_identifier"):
        clients.build_source(
            "dev",
            source={"type": "redshift", "redshift_database": "dev"},
            region=REGION,
            account_id=ACCOUNT,
        )


def test_redshift_session_policy_serverless_and_secret():
    policy = clients._redshift_session_policy(
        region=REGION,
        account_id=ACCOUNT,
        cluster_identifier=None,
        workgroup_name="wg1",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:rs-xyz",
    )
    sids = _sids(policy)
    assert sids["RedshiftDataApi"]["Resource"] == ["*"]  # not ARN-scopable
    assert sids["RedshiftServerlessAuth"]["Action"] == [
        "redshift-serverless:GetCredentials"
    ]
    assert "RedshiftClusterAuth" not in sids  # serverless -> no cluster grant
    assert sids["RedshiftSecretRead"]["Resource"] == [
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:rs-xyz"
    ]
    # Inline session policies are capped at 2048 chars.
    assert len(json.dumps(policy)) < 2048
