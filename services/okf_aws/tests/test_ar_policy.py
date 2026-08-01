"""AR policy helpers: exact boto3 request shapes + the registry lease semantics.

Split by what each half needs. moto has NO bedrock Automated Reasoning or
guardrail support (its ``bedrock`` backend covers custom models only), so the
control plane and runtime are hand fakes that mirror the shapes introspected
from botocore 1.43.47 — the request assertions here ARE the contract with the
real API. S3 and DynamoDB run on moto, because the two properties that matter
there (conditional-write races, prefix walking) cannot be faked honestly: a hand
fake would accept every ConditionExpression.
"""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from okf_aws import ar_policy as ap
from okf_core.ar_sources import compute_source_hash

DOMAIN = "sport"
DATASET = "formula_1"
LABEL = f"{DOMAIN}/{DATASET}"
BUCKET = "okf-bundles"
TABLE = "okf-registry"
POLICY_ARN = "arn:aws:bedrock:us-east-1:1:automated-reasoning-policy/p1"
VERSION_ARN = f"{POLICY_ARN}:1"


class Boto3Error(Exception):
    """A botocore-shaped error: the code lives under ``response.Error.Code``."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeBedrockAR:
    """The ``bedrock`` control plane, recording every request verbatim.

    Models the LIVE-VERIFIED build semantics: an INGEST_CONTENT workflow's
    result is staged as the POLICY_DEFINITION asset (never applied to the
    draft by AWS), and the FIDELITY_REPORT / BUILD_LOG assets raise
    ``ResourceNotFoundException`` unless the test opts them in.
    """

    def __init__(
        self,
        *,
        policies: list[dict] | None = None,
        coverage: float = 0.9,
        accuracy: float = 0.95,
        create_error: str = "",
        fidelity: bool = False,
        build_log: dict | None = None,
        built_definition: dict | None = None,
        dead_version_arns: set[str] | None = None,
    ):
        self.policies = policies or []
        self.coverage = coverage
        self.accuracy = accuracy
        self.create_error = create_error
        self.fidelity = fidelity
        self.build_log = build_log
        self.dead_version_arns = set(dead_version_arns or ())
        self.applied: dict | None = None  # what update_...policy last pushed
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.gets: list[dict] = []
        self.lists: list[dict] = []
        self.started: list[dict] = []
        self.workflow_gets: list[dict] = []
        self.asset_gets: list[dict] = []
        self.versions: list[dict] = []
        self.exports: list[dict] = []
        self.guardrails_created: list[dict] = []
        self.guardrails_updated: list[dict] = []
        self.guardrail_versions: list[dict] = []
        self.rules = [
            {
                "id": "R1",
                "expression": (
                    "IF queryExecuted AND zeroRowsReturned THEN the answer must not "
                    "state figures (references/usage_guardrails.md)"
                ),
            },
            {"id": "R2", "expression": "IF dedup missing THEN caveatIncluded"},
        ]
        self.built_definition = built_definition or {
            "version": "1",
            "types": [],
            "variables": [
                {"name": "queryExecuted", "type": "BOOL", "description": "x" * 81}
            ],
            "rules": self.rules,
        }

    # -- policies
    def list_automated_reasoning_policies(self, **kw):
        self.lists.append(kw)
        return {"automatedReasoningPolicySummaries": self.policies}

    def get_automated_reasoning_policy(self, **kw):
        self.gets.append(kw)
        return {
            "policyArn": kw["policyArn"],
            "definitionHash": "hash-2",
            "version": "DRAFT",
        }

    def update_automated_reasoning_policy(self, **kw):
        self.updated.append(kw)
        self.applied = kw.get("policyDefinition")
        return {"policyArn": kw["policyArn"], "definitionHash": "hash-3"}

    def create_automated_reasoning_policy(self, **kw):
        self.created.append(kw)
        if self.create_error:
            raise Boto3Error(self.create_error)
        return {
            "policyArn": POLICY_ARN,
            "version": "DRAFT",
            "name": kw["name"],
            "definitionHash": "hash-1",
        }

    # -- build workflow
    def start_automated_reasoning_policy_build_workflow(self, **kw):
        self.started.append(kw)
        return {"policyArn": kw["policyArn"], "buildWorkflowId": "wf-1"}

    def get_automated_reasoning_policy_build_workflow(self, **kw):
        self.workflow_gets.append(kw)
        return {"status": "COMPLETED", "buildWorkflowType": "INGEST_CONTENT"}

    def get_automated_reasoning_policy_build_workflow_result_assets(self, **kw):
        self.asset_gets.append(kw)
        base = {"policyArn": kw["policyArn"], "buildWorkflowId": kw["buildWorkflowId"]}
        asset_type = kw.get("assetType")
        if asset_type == "POLICY_DEFINITION":
            return {
                **base,
                "buildWorkflowAssets": {"policyDefinition": self.built_definition},
            }
        if asset_type == "FIDELITY_REPORT":
            if not self.fidelity:
                raise Boto3Error("ResourceNotFoundException")
            return {
                **base,
                "buildWorkflowAssets": {
                    "fidelityReport": {
                        "coverageScore": self.coverage,
                        "accuracyScore": self.accuracy,
                        "ruleReports": {
                            "R2": {
                                "rule": "IF dedup missing THEN caveatIncluded "
                                "(references/known_issues/dupes.md)",
                                "accuracyScore": 0.8,
                            },
                            "R3": {"rule": "IF x THEN y", "accuracyScore": 0.5},
                        },
                        "variableReports": {},
                        "documentSources": [],
                    }
                },
            }
        if asset_type == "BUILD_LOG":
            if self.build_log is None:
                raise Boto3Error("ResourceNotFoundException")
            return {**base, "buildWorkflowAssets": {"buildLog": self.build_log}}
        raise Boto3Error("ValidationException")

    # -- versions
    def create_automated_reasoning_policy_version(self, **kw):
        self.versions.append(kw)
        return {
            "policyArn": VERSION_ARN,
            "version": "1",
            "definitionHash": kw["lastUpdatedDefinitionHash"],
        }

    def export_automated_reasoning_policy_version(self, **kw):
        self.exports.append(kw)
        if kw.get("policyArn") in self.dead_version_arns:
            raise Boto3Error("ResourceNotFoundException")
        return {
            "policyDefinition": self.applied
            or {"rules": self.rules, "variables": [], "types": []}
        }

    # -- guardrails
    def create_guardrail(self, **kw):
        self.guardrails_created.append(kw)
        return {
            "guardrailId": "gr-1",
            "guardrailArn": "arn:aws:bedrock:us-east-1:1:guardrail/gr-1",
            "version": "DRAFT",
        }

    def update_guardrail(self, **kw):
        self.guardrails_updated.append(kw)
        return {
            "guardrailId": kw["guardrailIdentifier"],
            "guardrailArn": "arn:aws:bedrock:us-east-1:1:guardrail/gr-1",
            "version": "DRAFT",
        }

    def create_guardrail_version(self, **kw):
        self.guardrail_versions.append(kw)
        return {"guardrailId": kw["guardrailIdentifier"], "version": "3"}


def _summary(arn: str, name: str, version: str) -> dict:
    return {"policyArn": arn, "name": name, "version": version, "policyId": "p1"}


# -- the pre-seeded schema -------------------------------------------------------


def test_policy_definition_types_and_variables():
    definition = ap.policy_definition()
    assert definition["rules"] == []
    type_ = definition["types"][0]
    assert type_["name"] == "OKFDisposition"
    assert [v["value"] for v in type_["values"]] == ["COMMIT", "ASK", "BLOCK", "REFUSE"]
    assert all(v["description"] for v in type_["values"])

    by_name = {v["name"]: v for v in definition["variables"]}
    assert len(by_name) == len(ap.CORE_VARIABLES) == 17
    # The custom enum is referenced BY TYPE NAME; primitives use the isolated
    # literals, live-verified as BOOL/INT/NUMBER (the lowercase spellings the
    # API-reference prose suggests are rejected by the real validator).
    assert by_name["disposition"]["type"] == "OKFDisposition"
    assert by_name["clarificationObtained"]["type"] == "BOOL"
    assert by_name["rowCount"]["type"] == "INT"
    # description is REQUIRED by the API and is the translation-quality lever.
    assert all(len(v["description"]) > 80 for v in definition["variables"])


def test_env_guardrail_profile_matches_the_region_family(monkeypatch):
    # The profile FAMILY must match the deployment region (live-verified:
    # CreateGuardrail in eu-west-1 with us.guardrail.v1:0 is a
    # ValidationException). Explicit env wins; else derive from AWS_REGION.
    monkeypatch.delenv("OKF_POLICY_GUARDRAIL_PROFILE", raising=False)
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    assert ap.env_guardrail_profile() == "eu.guardrail.v1:0"
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    assert ap.env_guardrail_profile() == "us.guardrail.v1:0"
    monkeypatch.setenv("OKF_POLICY_GUARDRAIL_PROFILE", "eu.guardrail.v1:0")
    assert ap.env_guardrail_profile() == "eu.guardrail.v1:0"


def test_variable_names_match_the_ar_namespace_pattern():
    import re

    names = [v["name"] for v in ap.CORE_VARIABLES] + [ap.OKF_DISPOSITION_TYPE["name"]]
    for name in names:
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name), name
        assert len(name) <= 64


def test_names_are_pattern_safe():
    # Neither the policy nor the guardrail name pattern admits "/".
    assert ap.policy_name(LABEL) == "okf-sport-formula_1"
    assert ap.guardrail_name(LABEL) == "okf-ar-sport-formula_1"
    assert len(ap.guardrail_name("x" * 200)) <= 50


def test_supported_regions_are_the_six_verified_ones():
    assert ap.AR_SUPPORTED_REGIONS == frozenset(
        {"us-east-1", "us-east-2", "us-west-2", "eu-central-1", "eu-west-1", "eu-west-3"}
    )
    assert ap.region_supported("us-east-1")
    assert not ap.region_supported("ap-southeast-2")


# -- the preprocessing prompt ----------------------------------------------------


def test_build_rules_prompt_fences_each_source_with_its_path():
    prompt = ap.build_rules_prompt(
        [
            ("references/usage_guardrails.md", b"Never sum snapshots."),
            ("references/enums/status.md", "OK, -1 = unknown"),
        ]
    )
    assert "### Source: references/usage_guardrails.md" in prompt
    assert "### Source: references/enums/status.md" in prompt
    assert "```markdown\nNever sum snapshots.\n```" in prompt
    # The two load-bearing rule notes must reach the model.
    assert "MUST also be conditioned on queryExecuted" in prompt
    assert "zeroRowsReturned is true THEN the answer must" in prompt
    assert prompt.startswith("You convert dataset documentation")


def test_parse_rules_response_strips_fence_and_preamble():
    assert ap.parse_rules_response(
        "Here are the rules:\n1. IF a THEN b\n2. IF c THEN d"
    ) == "1. IF a THEN b\n2. IF c THEN d"
    assert ap.parse_rules_response("```markdown\n1. IF a THEN b\n```") == "1. IF a THEN b"
    # No numbered line at all: keep the body rather than returning nothing.
    assert ap.parse_rules_response("  no rules found  ") == "no rules found"


# -- policy lifecycle ------------------------------------------------------------


def test_ensure_policy_creates_with_the_seeded_schema():
    br = FakeBedrockAR()
    arn, hash_ = ap.ensure_policy(br, name="okf-x", description="d")
    assert (arn, hash_) == (POLICY_ARN, "hash-1")
    req = br.created[0]
    assert req["name"] == "okf-x"
    assert req["description"] == "d"
    assert req["policyDefinition"]["rules"] == []
    assert req["policyDefinition"]["types"][0]["name"] == "OKFDisposition"
    assert len(req["policyDefinition"]["variables"]) == 17


def test_ensure_policy_resolves_the_draft_arn_of_an_existing_policy():
    # Versioned summaries share the name; only the DRAFT arn accepts a build.
    br = FakeBedrockAR(
        policies=[
            _summary("arn:other", "someone-else", "DRAFT"),
            _summary(f"{POLICY_ARN}:1", "okf-x", "1"),
            _summary(POLICY_ARN, "okf-x", "DRAFT"),
        ]
    )
    arn, hash_ = ap.ensure_policy(br, name="okf-x", description="d")
    assert arn == POLICY_ARN
    assert hash_ == "hash-2"
    assert br.created == []


def test_ensure_policy_raises_policy_cap_error_on_quota():
    br = FakeBedrockAR(create_error="ServiceQuotaExceededException")
    with pytest.raises(ap.PolicyCapError):
        ap.ensure_policy(br, name="okf-x", description="d")


def test_ensure_policy_reraises_other_errors():
    br = FakeBedrockAR(create_error="ValidationException")
    with pytest.raises(Boto3Error):
        ap.ensure_policy(br, name="okf-x", description="d")


def test_ensure_policy_resolves_the_winner_after_a_create_race():
    br = FakeBedrockAR(
        policies=[_summary(POLICY_ARN, "okf-x", "DRAFT")],
        create_error="ConflictException",
    )
    # The name lookup happens BEFORE create, so seed it only after the fact:
    # emulate the race by hiding the policy from the first lookup.
    calls = {"n": 0}
    all_policies = br.policies

    def _list(**kw):
        calls["n"] += 1
        found = [] if calls["n"] == 1 else all_policies
        return {"automatedReasoningPolicySummaries": found}

    br.list_automated_reasoning_policies = _list
    arn, hash_ = ap.ensure_policy(br, name="okf-x", description="d")
    assert arn == POLICY_ARN


def test_start_build_sends_one_txt_document_in_the_source_content_wrapper():
    br = FakeBedrockAR()
    workflow_id = ap.start_build(
        br, policy_arn=POLICY_ARN, rules_text="1. IF a THEN b"
    )
    assert workflow_id == "wf-1"
    req = br.started[0]
    assert req["policyArn"] == POLICY_ARN
    assert req["buildWorkflowType"] == "INGEST_CONTENT"
    docs = req["sourceContent"]["workflowContent"]["documents"]
    assert len(docs) == 1  # exactly one document, per the ingest contract
    assert docs[0]["document"] == b"1. IF a THEN b"
    assert docs[0]["documentContentType"] == "txt"  # there is no "md" member
    assert docs[0]["documentName"] == "ar_rules.md"
    assert "policyDefinition" not in req["sourceContent"]


def test_truncate_rules_cuts_at_a_rule_boundary(caplog):
    rules = "\n".join(f"{i}. IF a THEN b" for i in range(1, 5001))
    out = ap.truncate_rules(rules)
    assert len(out) <= ap.MAX_INGEST_CHARS + 1
    assert out.endswith("\n")
    # No half-rule survives: every line is a complete numbered rule.
    assert all(line.endswith("IF a THEN b") for line in out.strip().splitlines())
    assert "truncating" in caplog.text.lower()


def test_truncate_rules_passes_short_text_through():
    assert ap.truncate_rules("1. IF a THEN b") == "1. IF a THEN b"


def test_get_build_status():
    br = FakeBedrockAR()
    status = ap.get_build_status(br, policy_arn=POLICY_ARN, workflow_id="wf-1")
    assert status == "COMPLETED"
    assert br.workflow_gets[0] == {"policyArn": POLICY_ARN, "buildWorkflowId": "wf-1"}
    assert "COMPLETED" in ap.TERMINAL_WORKFLOW_STATUSES
    assert "BUILDING" not in ap.TERMINAL_WORKFLOW_STATUSES


def test_complete_build_applies_the_staged_definition_then_versions():
    br = FakeBedrockAR()
    stamp = ap.complete_build(
        br, policy_arn=POLICY_ARN, workflow_id="wf-1", dataset_label=LABEL
    )
    # The STAGED definition is read first and APPLIED to the draft — an
    # INGEST_CONTENT workflow never mutates the draft itself (live-verified);
    # versioning without the apply would freeze an empty policy.
    assert br.asset_gets[0]["assetType"] == "POLICY_DEFINITION"
    (applied,) = br.updated
    assert applied == {"policyArn": POLICY_ARN, "policyDefinition": br.built_definition}
    # the version carries the CURRENT definitionHash (optimistic concurrency)
    assert br.gets[0] == {"policyArn": POLICY_ARN}
    assert br.versions[0] == {
        "policyArn": POLICY_ARN,
        "lastUpdatedDefinitionHash": "hash-2",
    }
    # rule text is exported from the VERSIONED arn, not the draft
    assert br.exports[0] == {"policyArn": VERSION_ARN}
    # the guardrail points at the versioned policy and carries the profile
    gr = br.guardrails_created[0]
    assert gr["automatedReasoningPolicyConfig"] == {
        "policies": [VERSION_ARN],
        "confidenceThreshold": 1.0,
    }
    assert gr["crossRegionConfig"] == {"guardrailProfileIdentifier": "us.guardrail.v1:0"}
    assert gr["blockedInputMessaging"] and gr["blockedOutputsMessaging"]
    assert br.guardrail_versions[0] == {"guardrailIdentifier": "gr-1"}

    assert stamp["policy_version"] == "1"
    assert stamp["guardrail_id"] == "gr-1"
    assert stamp["guardrail_version"] == "3"
    # No fidelity report exists for an ingest build: UNMEASURED is not LOW —
    # the status stays ready with 0.0 scores, never degraded.
    assert stamp["build_status"] == "ready"
    assert stamp["fidelity_coverage"] == 0.0
    grounded = stamp["grounding"]["R1"]
    assert grounded["rule_source_page"] == "references/usage_guardrails.md"


def test_complete_build_refuses_a_rule_free_result():
    # A rule-free policy renders no verdicts; applying + versioning it would
    # swap a working policy for a useless one. The draft must stay untouched.
    br = FakeBedrockAR(
        built_definition={"version": "1", "types": [], "variables": [], "rules": []}
    )
    with pytest.raises(ValueError):
        ap.complete_build(
            br, policy_arn=POLICY_ARN, workflow_id="wf-1", dataset_label=LABEL
        )
    assert br.updated == [] and br.versions == []


def test_complete_build_updates_an_existing_guardrail_in_place():
    br = FakeBedrockAR()
    stamp = ap.complete_build(
        br,
        policy_arn=POLICY_ARN,
        workflow_id="wf-1",
        dataset_label=LABEL,
        guardrail_id="gr-9",
        guardrail_profile="eu.guardrail.v1:0",
    )
    assert br.guardrails_created == []
    upd = br.guardrails_updated[0]
    assert upd["guardrailIdentifier"] == "gr-9"
    # Update re-requires name + both messaging fields, or it is a 400.
    assert upd["name"] == ap.guardrail_name(LABEL)
    assert upd["blockedInputMessaging"] and upd["blockedOutputsMessaging"]
    assert upd["crossRegionConfig"]["guardrailProfileIdentifier"] == "eu.guardrail.v1:0"
    assert stamp["guardrail_id"] == "gr-9"


def test_ensure_guardrail_recreates_one_that_was_deleted_out_of_band():
    br = FakeBedrockAR()

    def _boom(**_kw):
        raise Boto3Error("ResourceNotFoundException")

    br.update_guardrail = _boom
    out = ap.ensure_guardrail(
        br, name="okf-ar-x", policy_version_arn=VERSION_ARN, guardrail_id="gone"
    )
    assert out["guardrail_id"] == "gr-1"
    assert br.guardrails_created  # fell back to create rather than wedging
    assert br.guardrail_versions[0] == {"guardrailIdentifier": "gr-1"}


def test_ensure_guardrail_reraises_other_update_errors():
    br = FakeBedrockAR()

    def _boom(**_kw):
        raise Boto3Error("ValidationException")

    br.update_guardrail = _boom
    with pytest.raises(Boto3Error):
        ap.ensure_guardrail(
            br, name="okf-ar-x", policy_version_arn=VERSION_ARN, guardrail_id="gr-9"
        )


@pytest.mark.parametrize(
    "coverage,accuracy,expected",
    [
        (0.9, 0.95, "ready"),
        (0.6, 0.8, "ready"),  # thresholds are inclusive lower bounds
        (0.59, 0.95, "degraded"),
        (0.9, 0.79, "degraded"),
    ],
)
def test_fidelity_gate(coverage, accuracy, expected):
    assert ap.build_status_for_fidelity(coverage, accuracy) == expected
    br = FakeBedrockAR(coverage=coverage, accuracy=accuracy, fidelity=True)
    stamp = ap.complete_build(
        br, policy_arn=POLICY_ARN, workflow_id="wf-1", dataset_label=LABEL
    )
    assert stamp["build_status"] == expected
    assert stamp["fidelity_coverage"] == coverage


# -- grounding map ---------------------------------------------------------------


def test_build_grounding_parses_the_source_page_suffix():
    grounding = ap.build_grounding(
        [
            {"id": "A", "expression": "IF x THEN y (references/metrics/rev.md)"},
            {"id": "B", "expression": "IF p THEN q"},
        ],
        {},
    )
    assert grounding["A"] == {
        "rule_text": "IF x THEN y (references/metrics/rev.md)",
        "rule_source_page": "references/metrics/rev.md",
    }
    # No parenthesized reference -> None, never a guessed page.
    assert grounding["B"]["rule_source_page"] is None


def test_build_grounding_falls_back_to_the_fidelity_rule_reports():
    grounding = ap.build_grounding(
        [{"id": "A", "expression": "IF x THEN y"}],
        {
            "A": {"rule": "page from the report (references/enums/st.md)"},
            "Z": {"rule": "IF z THEN w (references/recipes/asof.md)"},
        },
    )
    assert grounding["A"]["rule_text"] == "IF x THEN y"  # export is authoritative
    # ...but the report's source sentence may still supply the PAGE.
    assert grounding["A"]["rule_source_page"] == "references/enums/st.md"
    assert grounding["Z"]["rule_source_page"] == "references/recipes/asof.md"


def test_build_grounding_prefers_the_natural_language_restatement():
    # Live-built rules carry an SMT `expression` plus an `alternateExpression`
    # restatement; the restatement is what faces humans in the sidebar and the
    # finding renderer. Pages fall back to the BUILD_LOG attribution.
    grounding = ap.build_grounding(
        [
            {
                "id": "A",
                "expression": "(=> p q)",
                "alternateExpression": "if p is true, then q is true",
            }
        ],
        {},
        log_pages={"A": "references/usage_guardrails.md"},
    )
    assert grounding["A"] == {
        "rule_text": "if p is true, then q is true",
        "rule_source_page": "references/usage_guardrails.md",
    }


def test_build_log_pages_attributes_only_single_page_chunks():
    def entry(content, *rule_ids):
        return {
            "annotation": {"ingestContent": {"content": content}},
            "status": "APPLIED",
            "buildSteps": [
                {"context": {"mutation": {"addRule": {"rule": {"id": rid}}}}}
                for rid in rule_ids
            ],
        }

    log = {
        "entries": [
            entry("1. IF a THEN b. (references/usage_guardrails.md)", "R1", "R2"),
            # Two cited pages -> attribution would be a guess -> skipped.
            entry(
                "2. x (references/a.md)\n3. y (references/b.md)", "R3"
            ),
            # The workflow's own refinement annotations carry no page.
            {
                "annotation": {"addRule": {"expression": "(=> a b)"}},
                "status": "APPLIED",
                "buildSteps": [
                    {"context": {"mutation": {"addRule": {"rule": {"id": "R4"}}}}}
                ],
            },
        ]
    }
    assert ap.build_log_pages(log) == {
        "R1": "references/usage_guardrails.md",
        "R2": "references/usage_guardrails.md",
    }


@pytest.mark.parametrize(
    "text,expected",
    [
        ("IF a THEN b (references/enums/x.md)", "references/enums/x.md"),
        # a merged rule cites several; the trailing one is the appended source
        ("IF a (references/a.md) THEN b (references/b.md)", "references/b.md"),
        ("IF a THEN b (tables/races.md)", None),  # only reference pages qualify
        ("IF a THEN b", None),
        ("", None),
    ],
)
def test_parse_rule_source_page(text, expected):
    assert ap.parse_rule_source_page(text) == expected


# -- runtime findings ------------------------------------------------------------


def _resp(findings: list[dict], units: int = 1) -> dict:
    return {
        "action": "NONE",
        "assessments": [{"automatedReasoningPolicy": {"findings": findings}}],
        "usage": {"automatedReasoningPolicyUnits": units},
    }


def _translation(claim: str, confidence: float = 0.9) -> dict:
    return {
        "premises": [{"logic": "p", "naturalLanguage": "the user asked about 2019"}],
        "claims": [{"logic": "c", "naturalLanguage": claim}],
        "untranslatedPremises": [],
        "untranslatedClaims": [],
        "confidence": confidence,
    }


def test_parse_ar_findings_invalid_carries_contradicting_rules_and_scenario():
    resp = _resp(
        [
            {
                "invalid": {
                    "translation": _translation("total points were 413"),
                    "contradictingRules": [
                        {"identifier": "R1", "policyVersionArn": VERSION_ARN}
                    ],
                    "logicWarning": None,
                }
            }
        ]
    )
    (finding,) = ap.parse_ar_findings(resp)
    assert finding["type"] == "INVALID"
    assert finding["claim"] == "total points were 413"
    assert finding["rule_ids"] == ["R1"]
    assert finding["confidence"] == 0.9
    assert finding["scenario"] == []


def test_parse_ar_findings_satisfiable_prefers_the_false_scenario():
    resp = _resp(
        [
            {
                "satisfiable": {
                    "translation": _translation("revenue rose"),
                    "claimsTrueScenario": {
                        "statements": [{"logic": "t", "naturalLanguage": "dedup applied"}]
                    },
                    "claimsFalseScenario": {
                        "statements": [
                            {"logic": "f", "naturalLanguage": "dedup was not applied"}
                        ]
                    },
                }
            }
        ]
    )
    (finding,) = ap.parse_ar_findings(resp)
    assert finding["type"] == "SATISFIABLE"
    # the FALSE scenario is what explains how the claim could fail
    assert finding["scenario"] == ["dedup was not applied"]


def test_parse_ar_findings_valid_uses_supporting_rules_and_true_scenario():
    resp = _resp(
        [
            {
                "valid": {
                    "translation": _translation("caveat included"),
                    "claimsTrueScenario": {
                        "statements": [
                            {"logic": "t", "naturalLanguage": "caveat present"}
                        ]
                    },
                    "supportingRules": [
                        {"identifier": "R7", "policyVersionArn": VERSION_ARN}
                    ],
                }
            }
        ]
    )
    (finding,) = ap.parse_ar_findings(resp)
    assert finding["type"] == "VALID"
    assert finding["rule_ids"] == ["R7"]
    assert finding["scenario"] == ["caveat present"]


def test_parse_ar_findings_impossible():
    resp = _resp(
        [
            {
                "impossible": {
                    "translation": _translation("both A and not A"),
                    "contradictingRules": [
                        {"identifier": "R2", "policyVersionArn": VERSION_ARN},
                        {"identifier": "R3", "policyVersionArn": VERSION_ARN},
                    ],
                }
            }
        ]
    )
    (finding,) = ap.parse_ar_findings(resp)
    assert finding["type"] == "IMPOSSIBLE"
    assert finding["rule_ids"] == ["R2", "R3"]


def test_parse_ar_findings_translation_ambiguous_has_no_top_level_translation():
    resp = _resp(
        [
            {
                "translationAmbiguous": {
                    "options": [
                        {"translations": [_translation("revenue means booked", 0.4)]},
                        {"translations": [_translation("revenue means billed", 0.4)]},
                    ],
                    "differenceScenarios": [
                        {"statements": [{"logic": "d", "naturalLanguage": "differs"}]}
                    ],
                }
            }
        ]
    )
    (finding,) = ap.parse_ar_findings(resp)
    assert finding["type"] == "TRANSLATION_AMBIGUOUS"
    assert finding["claim"] == "revenue means booked"
    assert finding["confidence"] == 0.4
    assert finding["rule_ids"] == []


@pytest.mark.parametrize(
    "member,expected",
    [("tooComplex", "TOO_COMPLEX"), ("noTranslations", "NO_TRANSLATIONS")],
)
def test_parse_ar_findings_keeps_the_empty_struct_members(member, expected):
    # {"tooComplex": {}} is FALSY — a truthiness check would silently drop the
    # exact outcomes the sidebar reports as "not checkable".
    (finding,) = ap.parse_ar_findings(_resp([{member: {}}]))
    assert finding["type"] == expected
    assert finding["claim"] == ""
    assert finding["rule_ids"] == []
    assert finding["scenario"] == []
    assert finding["confidence"] is None


def test_parse_ar_findings_across_multiple_assessments_and_empty_responses():
    resp = {
        "assessments": [
            {"automatedReasoningPolicy": {"findings": [{"tooComplex": {}}]}},
            {"contentPolicy": {}},  # a non-AR assessment block is skipped
            {"automatedReasoningPolicy": {"findings": [{"noTranslations": {}}]}},
        ]
    }
    assert [f["type"] for f in ap.parse_ar_findings(resp)] == [
        "TOO_COMPLEX",
        "NO_TRANSLATIONS",
    ]
    assert ap.parse_ar_findings({}) == []
    assert ap.parse_ar_findings({"assessments": []}) == []


def test_ar_ran_reads_the_billing_counter():
    # A guardrail with no reachable policy returns a well-formed, empty
    # assessment — indistinguishable from "consistent" without this counter.
    assert ap.ar_ran(_resp([], units=2)) is True
    assert ap.ar_ran(_resp([], units=0)) is False
    assert ap.ar_ran({"usage": {}}) is False
    assert ap.ar_ran({}) is False


# -- source gathering (moto S3) --------------------------------------------------


@pytest.fixture()
def s3_bundle():
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        yield s3


def _artifact_kwargs(s3) -> dict:
    """The keyword set every off-mount artifact reader/writer takes."""
    return {"s3": s3, "bucket": BUCKET, "data_domain": DOMAIN, "dataset": DATASET}


def _put(s3, rel: str, body: bytes) -> None:
    s3.put_object(Bucket=BUCKET, Key=f"okf/{DOMAIN}/{DATASET}/{rel}", Body=body)


def test_gather_sources_selects_and_sorts_the_source_set(s3_bundle):
    _put(s3_bundle, "references/usage_guardrails.md", b"guardrails")
    _put(s3_bundle, "references/enums/status.md", b"enums")
    _put(s3_bundle, "references/known_issues/dupes.md", b"issues")
    # not policy material
    _put(s3_bundle, "tables/races.md", b"table")
    _put(s3_bundle, "references/joins.md", b"joins")
    _put(s3_bundle, ".harvest/state.json", b"{}")
    # a cross-dataset reference is dataset-relative under external/ -> excluded,
    # which is what makes a cross-mode harvest leave the fingerprint alone.
    _put(s3_bundle, "external/other/ds/references/usage_guardrails.md", b"x")
    # another dataset entirely
    s3_bundle.put_object(
        Bucket=BUCKET, Key=f"okf/{DOMAIN}/other/references/usage_guardrails.md", Body=b"y"
    )

    pairs = ap.gather_sources(s3_bundle, BUCKET, DOMAIN, DATASET)
    assert [k for k, _ in pairs] == [
        "references/enums/status.md",
        "references/known_issues/dupes.md",
        "references/usage_guardrails.md",
    ]
    assert pairs[2][1] == b"guardrails"


def test_source_hash_matches_the_pure_fingerprint(s3_bundle):
    _put(s3_bundle, "references/usage_guardrails.md", b"guardrails")
    _put(s3_bundle, "references/metrics/rev.md", b"metric")
    expected = compute_source_hash(
        [
            ("references/usage_guardrails.md", b"guardrails"),
            ("references/metrics/rev.md", b"metric"),
        ]
    )
    assert ap.source_hash(s3_bundle, BUCKET, DOMAIN, DATASET) == expected


def test_source_hash_is_none_when_there_are_no_sources(s3_bundle):
    _put(s3_bundle, "tables/races.md", b"table")
    assert ap.gather_sources(s3_bundle, BUCKET, DOMAIN, DATASET) == []
    assert ap.source_hash(s3_bundle, BUCKET, DOMAIN, DATASET) is None
    assert ap.hash_sources([]) is None


def test_gather_sources_follows_the_continuation_token():
    prefix = f"okf/{DOMAIN}/{DATASET}/"

    class PagedS3:
        def __init__(self):
            self.tokens: list[str | None] = []

        def list_objects_v2(self, **kw):
            self.tokens.append(kw.get("ContinuationToken"))
            if not kw.get("ContinuationToken"):
                return {
                    "Contents": [{"Key": f"{prefix}references/enums/a.md"}],
                    "IsTruncated": True,
                    "NextContinuationToken": "t1",
                }
            return {
                "Contents": [{"Key": f"{prefix}references/enums/b.md"}],
                "IsTruncated": False,
            }

        def get_object(self, **kw):
            return {"Body": _Body(kw["Key"].encode())}

    class _Body:
        def __init__(self, raw):
            self._raw = raw

        def read(self):
            return self._raw

    s3 = PagedS3()
    pairs = ap.gather_sources(s3, BUCKET, DOMAIN, DATASET)
    assert [k for k, _ in pairs] == ["references/enums/a.md", "references/enums/b.md"]
    assert s3.tokens == [None, "t1"]


# -- off-mount artifacts ---------------------------------------------------------


def test_policy_artifacts_live_beside_the_bundle_not_inside_it():
    assert ap.policy_prefix(DOMAIN, DATASET) == "policy/sport/formula_1/"
    assert ap.ar_rules_key(DOMAIN, DATASET) == "policy/sport/formula_1/ar_rules.md"
    assert ap.grounding_key(DOMAIN, DATASET) == "policy/sport/formula_1/grounding.json"
    # Outside okf/ -> outside the mount AND outside the reindex key filter.
    assert not ap.policy_prefix(DOMAIN, DATASET).startswith("okf/")


def test_rules_and_grounding_round_trip(s3_bundle):
    ap.put_ar_rules(**_artifact_kwargs(s3_bundle), rules_text="1. IF a THEN b")
    assert ap.read_ar_rules(**_artifact_kwargs(s3_bundle)) == "1. IF a THEN b"
    obj = s3_bundle.get_object(Bucket=BUCKET, Key=ap.ar_rules_key(DOMAIN, DATASET))
    assert obj["ContentType"] == "text/markdown"

    grounding = {
        "R1": {"rule_text": "IF a THEN b", "rule_source_page": "references/x.md"}
    }
    ap.put_grounding(**_artifact_kwargs(s3_bundle), grounding=grounding)
    assert ap.read_grounding(**_artifact_kwargs(s3_bundle)) == grounding
    stored = s3_bundle.get_object(Bucket=BUCKET, Key=ap.grounding_key(DOMAIN, DATASET))
    assert json.loads(stored["Body"].read()) == grounding


def test_missing_artifacts_degrade_instead_of_raising(s3_bundle):
    # A finding without resolvable rule text still renders; a missing artifact
    # must never fail a check.
    assert ap.read_grounding(**_artifact_kwargs(s3_bundle)) == {}
    assert ap.read_ar_rules(**_artifact_kwargs(s3_bundle)) is None


# -- registry stamps (moto DynamoDB — conditional writes are the point) ----------


@pytest.fixture()
def registry():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.put_item(
            TableName=TABLE,
            Item={
                **ap.registry_key(DOMAIN, DATASET),
                "data_domain": {"S": DOMAIN},
                "dataset": {"S": DATASET},
            },
        )
        yield ddb


def _row(ddb) -> dict:
    key = ap.registry_key(DOMAIN, DATASET)
    return ddb.get_item(TableName=TABLE, Key=key).get("Item", {})


@pytest.mark.parametrize(
    "status,stored,live,usable",
    [
        ("ready", "h", "h", True),
        ("degraded", "h", "h", True),
        ("ready", "h", "h2", False),  # the wiki moved -> no verdict
        ("stale", "h", "h", False),
        ("building", "h", "h", False),
        ("failed", "h", "h", False),
        ("unsupported_region", "h", "h", False),
        ("ready", None, "h", False),  # never built -> nothing to compare
        ("ready", "h", None, False),  # sources vanished -> no live fingerprint
    ],
)
def test_policy_usable_requires_both_halves_of_the_gate(status, stored, live, usable):
    assert (
        ap.policy_usable(build_status=status, stored_hash=stored, live_hash=live)
        is usable
    )


def test_registry_key_is_the_dataset_mapping_row():
    assert ap.registry_key(DOMAIN, DATASET) == {
        "pk": {"S": "DOMAIN#sport"},
        "sk": {"S": "DATASET#formula_1"},
    }


def test_flip_building_serializes_builds(registry):
    assert (
        ap.try_flip_building(
            registry, TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="h1"
        )
        is True
    )
    row = _row(registry)
    assert row["ar_build_status"]["S"] == "building"
    assert row["ar_pending_source_hash"]["S"] == "h1"
    # The second trigger (reconcile, a duplicate policy_rebuild event, a second
    # finalize) loses the condition and does NOT start a build.
    assert (
        ap.try_flip_building(
            registry, TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="h2"
        )
        is False
    )
    assert _row(registry)["ar_pending_source_hash"]["S"] == "h1"


@pytest.mark.parametrize(
    "status", ["ready", "degraded", "failed", "stale", "unsupported_region"]
)
def test_flip_building_reclaims_a_non_building_row(registry, status):
    registry.update_item(
        TableName=TABLE,
        Key=ap.registry_key(DOMAIN, DATASET),
        UpdateExpression="SET ar_build_status = :s",
        ExpressionAttributeValues={":s": {"S": status}},
    )
    assert (
        ap.try_flip_building(
            registry, TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="h"
        )
        is True
    )


def test_flip_building_refuses_a_deleted_dataset(registry):
    registry.delete_item(TableName=TABLE, Key=ap.registry_key(DOMAIN, DATASET))
    assert (
        ap.try_flip_building(
            registry, TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="h"
        )
        is False
    )
    # No phantom row resurrected by the upsert-by-default UpdateItem.
    assert _row(registry) == {}


def test_stamp_build_started_attaches_the_workflow_id(registry):
    ap.stamp_build_started(
        registry, TABLE, data_domain=DOMAIN, dataset=DATASET, workflow_id="wf-7"
    )
    assert _row(registry)["ar_build_workflow_id"]["S"] == "wf-7"


def test_stamp_build_complete_carries_the_pending_hash_verbatim(registry):
    ap.try_flip_building(
        registry, TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="gathered"
    )
    br = FakeBedrockAR(fidelity=True)
    stamp = ap.complete_build(
        br, policy_arn=POLICY_ARN, workflow_id="wf-1", dataset_label=LABEL
    )
    status = ap.stamp_build_complete(
        registry,
        TABLE,
        data_domain=DOMAIN,
        dataset=DATASET,
        stamp=stamp,
        pending_hash="gathered",
        bundle_version="v-1",
    )
    row = _row(registry)
    assert status == "ready"
    assert row["ar_build_status"]["S"] == "ready"
    # The stored hash describes what was INGESTED, so a wiki that moved mid-build
    # yields a stale-on-arrival policy rather than a mislabelled current one.
    assert row["ar_source_hash"]["S"] == "gathered"
    assert row["ar_policy_arn"]["S"] == POLICY_ARN
    assert row["ar_policy_version"]["S"] == "1"
    assert row["ar_guardrail_id"]["S"] == "gr-1"
    assert row["ar_guardrail_version"]["S"] == "3"
    assert row["ar_bundle_version"]["S"] == "v-1"
    assert float(row["ar_fidelity_coverage"]["N"]) == 0.9
    assert float(row["ar_fidelity_accuracy"]["N"]) == 0.95
    assert row["ar_built_at"]["S"]


def test_stamp_build_complete_writes_degraded_below_the_fidelity_gate(registry):
    br = FakeBedrockAR(coverage=0.2, accuracy=0.99, fidelity=True)
    stamp = ap.complete_build(
        br, policy_arn=POLICY_ARN, workflow_id="wf-1", dataset_label=LABEL
    )
    assert (
        ap.stamp_build_complete(
            registry,
            TABLE,
            data_domain=DOMAIN,
            dataset=DATASET,
            stamp=stamp,
            pending_hash="h",
        )
        == "degraded"
    )
    assert _row(registry)["ar_build_status"]["S"] in ap.USABLE_BUILD_STATUSES


def test_stamp_build_failed(registry):
    assert (
        ap.stamp_build_failed(
            registry, TABLE, data_domain=DOMAIN, dataset=DATASET, reason="policy_cap"
        )
        == "failed"
    )
    row = _row(registry)
    assert row["ar_build_status"]["S"] == "failed"
    assert row["ar_build_detail"]["S"] == "policy_cap"


def test_stamp_build_failed_unsupported_region_is_its_own_status(registry):
    assert (
        ap.stamp_build_failed(
            registry,
            TABLE,
            data_domain=DOMAIN,
            dataset=DATASET,
            reason=ap.REASON_UNSUPPORTED_REGION,
        )
        == "unsupported_region"
    )
    assert _row(registry)["ar_build_status"]["S"] == "unsupported_region"


@pytest.mark.parametrize("status", ["ready", "degraded", "stale"])
def test_flag_stale_applies_to_a_built_policy(registry, status):
    registry.update_item(
        TableName=TABLE,
        Key=ap.registry_key(DOMAIN, DATASET),
        UpdateExpression="SET ar_build_status = :s",
        ExpressionAttributeValues={":s": {"S": status}},
    )
    assert ap.flag_stale(registry, TABLE, data_domain=DOMAIN, dataset=DATASET) is True
    assert _row(registry)["ar_build_status"]["S"] == "stale"


@pytest.mark.parametrize("status", ["building", "failed", "unsupported_region"])
def test_flag_stale_never_clobbers_a_non_built_status(registry, status):
    registry.update_item(
        TableName=TABLE,
        Key=ap.registry_key(DOMAIN, DATASET),
        UpdateExpression="SET ar_build_status = :s",
        ExpressionAttributeValues={":s": {"S": status}},
    )
    assert ap.flag_stale(registry, TABLE, data_domain=DOMAIN, dataset=DATASET) is False
    assert _row(registry)["ar_build_status"]["S"] == status


def test_flag_stale_on_a_dataset_with_no_policy_is_a_no_op(registry):
    assert ap.flag_stale(registry, TABLE, data_domain=DOMAIN, dataset=DATASET) is False
    assert "ar_build_status" not in _row(registry)


# --- enrollment ---------------------------------------------------------------


def test_enrollment_round_trip(registry):
    # Absent attr / absent row / non-BOOL are all NOT enrolled.
    assert ap.is_enrolled(_row(registry)) is False
    assert ap.is_enrolled(None) is False
    assert ap.is_enrolled({ap.ATTR_ENROLLED: {"S": "true"}}) is False
    ap.set_enrolled(registry, TABLE, data_domain=DOMAIN, dataset=DATASET)
    assert ap.is_enrolled(_row(registry)) is True


def test_set_enrolled_never_creates_a_phantom_row(registry):
    with pytest.raises(Exception):
        ap.set_enrolled(registry, TABLE, data_domain="ghost", dataset="none")


def test_clear_ar_attrs_removes_every_ar_attribute(registry):
    # Stamp a full lifecycle's worth of attrs, then unenroll: the row must be
    # indistinguishable from never-enrolled (delete semantics, no zombie state).
    ap.set_enrolled(registry, TABLE, data_domain=DOMAIN, dataset=DATASET)
    ap.try_flip_building(
        registry, TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="h"
    )
    ap.stamp_build_started(
        registry, TABLE, data_domain=DOMAIN, dataset=DATASET, workflow_id="wf"
    )
    ap.stamp_build_failed(
        registry, TABLE, data_domain=DOMAIN, dataset=DATASET, reason="boom"
    )
    ap.clear_ar_attrs(registry, TABLE, data_domain=DOMAIN, dataset=DATASET)
    row = _row(registry)
    assert not any(a in row for a in ap.AR_ROW_ATTRS)
    assert row["pk"]["S"].startswith("DOMAIN#")  # the mapping itself survives


def test_stamped_attrs_are_all_in_the_unenroll_set(registry):
    # Every attribute any stamp writes must be listed in AR_ROW_ATTRS, or an
    # unenroll would leave it behind as a zombie. Exercise every stamp, then
    # diff the row against the declared set.
    ap.set_enrolled(registry, TABLE, data_domain=DOMAIN, dataset=DATASET)
    ap.try_flip_building(
        registry, TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="h"
    )
    ap.stamp_build_started(
        registry, TABLE, data_domain=DOMAIN, dataset=DATASET, workflow_id="wf"
    )
    ap.stamp_build_complete(
        registry,
        TABLE,
        data_domain=DOMAIN,
        dataset=DATASET,
        stamp={
            "policy_arn": "arn:p",
            "policy_version": "1",
            "guardrail_id": "g1",
            "guardrail_version": "2",
            "fidelity_coverage": 0.9,
            "fidelity_accuracy": 0.9,
            "build_status": "ready",
        },
        pending_hash="h",
        bundle_version="v1",
    )
    ap.stamp_build_failed(
        registry, TABLE, data_domain=DOMAIN, dataset=DATASET, reason="boom"
    )
    ar_attrs_on_row = {a for a in _row(registry) if a.startswith("ar_")}
    assert ar_attrs_on_row <= set(ap.AR_ROW_ATTRS)


def test_registry_helpers_reraise_non_condition_errors():
    class Boom:
        def update_item(self, **_kw):
            raise Boto3Error("ProvisionedThroughputExceededException")

    with pytest.raises(Boto3Error):
        ap.try_flip_building(
            Boom(), TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="h"
        )
    with pytest.raises(Boto3Error):
        ap.flag_stale(Boom(), TABLE, data_domain=DOMAIN, dataset=DATASET)


# --- author state + snapshots -------------------------------------------------


_SOURCES = [
    ("references/usage_guardrails.md", b"never sum booked and billed"),
    ("references/enums/status.md", b"-1 means unknown"),
]


def test_sources_manifest_matches_the_fingerprint():
    manifest = ap.build_sources_manifest(_SOURCES)
    assert manifest["fingerprint"] == compute_source_hash(_SOURCES)
    assert set(manifest["files"]) == {rel for rel, _ in _SOURCES}


def test_persist_author_state_round_trips(s3_bundle):
    ap.persist_author_state(
        s3_bundle, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        sources=_SOURCES, rules_text="1. IF x THEN y",
    )
    assert (
        ap.read_ar_rules(**_artifact_kwargs(s3_bundle))
        == "1. IF x THEN y"
    )
    manifest = ap.read_sources_manifest(**_artifact_kwargs(s3_bundle))
    assert manifest["fingerprint"] == compute_source_hash(_SOURCES)
    copy = ap.read_source_copy(
        s3_bundle, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        rel_path="references/enums/status.md",
    )
    assert copy == b"-1 means unknown"
    assert (
        ap.read_source_copy(
            s3_bundle, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
            rel_path="references/ghost.md",
        )
        is None
    )


def _stamp(status="ready"):
    return {
        "policy_arn": POLICY_ARN,
        "policy_version": "1",
        "guardrail_id": "g1",
        "guardrail_version": "2",
        "fidelity_coverage": 0.9,
        "fidelity_accuracy": 0.95,
        "build_status": status,
        "grounding": {"RULEIDABCDEF": {"rule_text": "IF x THEN y",
                                        "rule_source_page": "references/usage_guardrails.md"}},
        "policy_definition": {"rules": [{"id": "RULEIDABCDEF", "expression": "(=> x y)"}]},
    }


def test_snapshot_round_trip(s3_bundle):
    snap = ap.make_snapshot(_stamp(), fingerprint="f1", rules_text="1. IF x THEN y")
    ap.write_snapshot(
        s3_bundle, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET, snapshot=snap
    )
    back = ap.read_snapshot(
        s3_bundle, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET, fingerprint="f1"
    )
    assert back["policy_definition"]["rules"][0]["id"] == "RULEIDABCDEF"
    assert back["rules_text"] == "1. IF x THEN y"
    assert back["build_status"] == "ready" and back["fidelity_accuracy"] == 0.95


def test_snapshot_misses(s3_bundle):
    assert (
        ap.read_snapshot(
            s3_bundle, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
            fingerprint="never-built",
        )
        is None
    )
    assert (
        ap.read_snapshot(
            s3_bundle, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
            fingerprint="",
        )
        is None
    )
    # A snapshot without a definition must read as a MISS — restoring an empty
    # definition would blank the live policy.
    s3_bundle.put_object(
        Bucket=BUCKET,
        Key=ap.snapshot_key(DOMAIN, DATASET, "hollow"),
        Body=b'{"fingerprint": "hollow", "policy_definition": {}}',
    )
    assert (
        ap.read_snapshot(
            s3_bundle, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
            fingerprint="hollow",
        )
        is None
    )


def test_vocabulary_round_trip_and_definition_extraction(s3_bundle):
    definition = {
        "variables": [
            {"name": "lastRowSelected", "type": "BOOL", "description": "picks last"},
            {"name": "", "type": "BOOL", "description": "nameless: skipped"},
        ],
        "rules": [],
    }
    vocab = ap.definition_vocabulary(definition)
    assert vocab == [
        {"name": "lastRowSelected", "type": "BOOL", "description": "picks last"}
    ]
    ap.put_vocabulary(
        s3_bundle, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        vocabulary=vocab,
    )
    assert (
        ap.read_vocabulary(
            s3_bundle, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
        )
        == vocab
    )
    # Absent artifact degrades to [] — the pre-pass then runs core-only.
    assert (
        ap.read_vocabulary(
            s3_bundle, bucket=BUCKET, data_domain=DOMAIN, dataset="never-built"
        )
        == []
    )


def test_finish_completed_build_writes_the_derived_artifacts(s3_bundle, registry):
    # The one shared completion path: grounding + vocabulary + snapshot + stamp.
    br = FakeBedrockAR()
    ap.try_flip_building(
        registry, TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="era-h"
    )
    status = ap.finish_completed_build(
        br, registry, s3_bundle,
        table=TABLE, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        policy_arn=POLICY_ARN, workflow_id="wf-1", pending_hash="era-h",
    )
    assert status == "ready"
    vocab = ap.read_vocabulary(
        s3_bundle, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    )
    assert [v["name"] for v in vocab] == ["queryExecuted"]  # from built_definition
    snap = ap.read_snapshot(
        s3_bundle, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        fingerprint="era-h",
    )
    assert snap is not None and snap["policy_version_arn"] == VERSION_ARN


def test_parse_ar_findings_names_the_ambiguity_difference():
    # A translationAmbiguous finding has an EMPTY top-level translation; the
    # substance is in `options`. The synthesized claim must name exactly the
    # variables the candidate readings disagree on — actionable feedback
    # instead of a bare "couldn't be expressed".
    def option(*logic_names):
        return {
            "translations": [
                {
                    "premises": [],
                    "claims": [
                        {"logic": n, "naturalLanguage": f"{n} is true"}
                        for n in logic_names
                    ],
                }
            ]
        }

    response = {
        "assessments": [
            {
                "automatedReasoningPolicy": {
                    "findings": [
                        {
                            "translationAmbiguous": {
                                "options": [
                                    option("dedupApplied"),
                                    option("dedupApplied", "lastRowSelected"),
                                ]
                            }
                        }
                    ]
                }
            }
        ]
    }
    (finding,) = ap.parse_ar_findings(response)
    assert finding["type"] == "TRANSLATION_AMBIGUOUS"
    assert "lastRowSelected" in finding["claim"]
    assert "dedupApplied" not in finding["claim"]  # agreement is not ambiguity


def test_parse_ar_findings_names_a_premise_claim_placement_ambiguity():
    # Live-observed second ambiguity class: both readings bind the SAME
    # variables but split them differently across premises and claims (one
    # reading treats the narrative facts as given, the other as asserted).
    # The trivial `true` claims and `(not x)` wrappers must not pollute the
    # summary.
    response = {
        "assessments": [
            {
                "automatedReasoningPolicy": {
                    "findings": [
                        {
                            "translationAmbiguous": {
                                "options": [
                                    {
                                        "translations": [
                                            {
                                                "premises": [
                                                    {"logic": "dedupApplied"},
                                                    {"logic": "(not snapshotSummedOverTime)"},
                                                ],
                                                "claims": [{"logic": "true"}],
                                            }
                                        ]
                                    },
                                    {
                                        "translations": [
                                            {
                                                "premises": [],
                                                "claims": [
                                                    {"logic": "dedupApplied"},
                                                    {"logic": "(not snapshotSummedOverTime)"},
                                                ],
                                            }
                                        ]
                                    },
                                ]
                            }
                        }
                    ]
                }
            }
        ]
    }
    (finding,) = ap.parse_ar_findings(response)
    assert "premises" in finding["claim"] and "claims" in finding["claim"]
    assert "true" not in finding["claim"].split()  # noise filtered


def test_write_snapshot_requires_a_fingerprint(s3_bundle):
    with pytest.raises(ValueError):
        ap.write_snapshot(
            s3_bundle, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
            snapshot={"policy_definition": {"rules": [1]}},
        )


def test_complete_build_returns_the_exported_definition():
    fake = FakeBedrockAR(policies=[_summary(POLICY_ARN, "okf-sport-formula_1", "DRAFT")])
    stamp = ap.complete_build(
        fake, policy_arn=POLICY_ARN, workflow_id="wf-1", dataset_label=LABEL
    )
    assert stamp["policy_definition"]["rules"] == fake.rules


def test_restore_snapshot_repoints_to_the_recorded_version(s3_bundle, registry):
    # VERSION-FIRST: the snapshot records the immutable policy version frozen
    # at build time; when it still exists, restore is ONE guardrail repoint —
    # no draft mutation, no new version burned from the quota.
    fake = FakeBedrockAR(policies=[_summary(POLICY_ARN, "okf-sport-formula_1", "DRAFT")])
    stamp_in = {**_stamp(status="degraded"), "policy_version_arn": VERSION_ARN}
    snap = ap.make_snapshot(
        stamp_in, fingerprint="era-hash", rules_text="1. IF x THEN y"
    )
    status = ap.restore_snapshot(
        fake, registry, s3_bundle,
        table=TABLE, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        snapshot=snap, guardrail_id="g1",
    )
    assert status == "degraded"
    # The version's existence was probed via export; nothing was pushed or
    # re-versioned — the draft is untouched.
    assert fake.exports[0] == {"policyArn": VERSION_ARN}
    assert fake.updated == [] and fake.versions == []
    (gr,) = fake.guardrails_updated
    assert gr["automatedReasoningPolicyConfig"]["policies"] == [VERSION_ARN]
    row = _row(registry)
    assert row["ar_source_hash"]["S"] == "era-hash"
    assert row["ar_policy_version"]["S"] == "1"
    assert row["ar_build_status"]["S"] == "degraded"
    assert ap.read_ar_rules(**_artifact_kwargs(s3_bundle)) == "1. IF x THEN y"


def test_restore_snapshot_falls_back_when_the_version_is_dead(s3_bundle, registry):
    # Unenroll deleted the policy and its versions; re-enroll made a fresh
    # policy. The recorded version ARN still matches the (re-created, same
    # name -> found by ensure_policy) policy id here, but the version itself
    # is GONE — the fallback pushes the snapshot's definition instead.
    fake = FakeBedrockAR(
        policies=[_summary(POLICY_ARN, "okf-sport-formula_1", "DRAFT")],
        dead_version_arns={VERSION_ARN},
    )
    stamp_in = {**_stamp(), "policy_version_arn": VERSION_ARN}
    snap = ap.make_snapshot(stamp_in, fingerprint="era-hash", rules_text="1. r")
    status = ap.restore_snapshot(
        fake, registry, s3_bundle,
        table=TABLE, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        snapshot=snap, guardrail_id="g1",
    )
    assert status == "ready"
    (update,) = fake.updated
    assert update["policyDefinition"] == snap["policy_definition"]
    assert fake.versions  # a NEW version was frozen for the pushed definition


def test_restore_snapshot_ignores_a_version_from_another_policy_generation(
    s3_bundle, registry
):
    # A recorded version ARN whose policy id differs from the CURRENT policy
    # can never be repointed to — the prefix check routes straight to the
    # definition push without even probing.
    fake = FakeBedrockAR(policies=[_summary(POLICY_ARN, "okf-sport-formula_1", "DRAFT")])
    foreign = "arn:aws:bedrock:us-east-1:1:automated-reasoning-policy/dead99:7"
    stamp_in = {**_stamp(), "policy_version_arn": foreign}
    snap = ap.make_snapshot(stamp_in, fingerprint="era-hash", rules_text="1. r")
    ap.restore_snapshot(
        fake, registry, s3_bundle,
        table=TABLE, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        snapshot=snap, guardrail_id="g1",
    )
    assert fake.exports == []  # no probe of a foreign-generation version
    (update,) = fake.updated
    assert update["policyDefinition"] == snap["policy_definition"]


def test_restore_snapshot_pushes_the_exact_definition_back(s3_bundle, registry):
    # A degraded-era snapshot WITHOUT a recorded version (or with a dead one):
    # status and fidelity must restore VERBATIM (never re-measured), the stamp
    # must carry the ERA fingerprint, and the live grounding + rules artifacts
    # must be rewritten from the snapshot.
    fake = FakeBedrockAR(policies=[_summary(POLICY_ARN, "okf-sport-formula_1", "DRAFT")])
    snap = ap.make_snapshot(
        _stamp(status="degraded"), fingerprint="era-hash", rules_text="1. IF x THEN y"
    )
    status = ap.restore_snapshot(
        fake, registry, s3_bundle,
        table=TABLE, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        snapshot=snap, guardrail_id="g1",
    )
    assert status == "degraded"
    (update,) = fake.updated
    assert update["policyDefinition"] == snap["policy_definition"]
    # Version freeze uses the hash read AFTER the update, not a stale one.
    (versioned,) = fake.versions
    assert versioned["lastUpdatedDefinitionHash"] == "hash-2"
    # Existing guardrail updated in place (no create).
    assert fake.guardrails_updated and not fake.guardrails_created
    row = _row(registry)
    assert row["ar_source_hash"]["S"] == "era-hash"
    assert row["ar_build_status"]["S"] == "degraded"
    assert row["ar_fidelity_accuracy"]["N"] == "0.95"
    assert (
        ap.read_ar_rules(**_artifact_kwargs(s3_bundle))
        == "1. IF x THEN y"
    )
    grounding = ap.read_grounding(**_artifact_kwargs(s3_bundle))
    assert "RULEIDABCDEF" in grounding
