"""The harvest model/effort catalog: parsing + validation.

Pure module (no AWS). Covers the trust boundary the Control API relies on: only
(model, effort) pairs offered by the catalog are accepted; everything else raises
ModelCatalogError (-> 400).
"""

import json

import pytest

from okf_core import harvest_models as hm


def test_default_catalog_has_both_providers():
    models = [e["model"] for e in hm.DEFAULT_CATALOG]
    assert "global.anthropic.claude-opus-4-8" in models
    assert "openai.gpt-5.5" in models


def test_parse_catalog_empty_falls_back_to_default():
    assert hm.parse_catalog(None) is hm.DEFAULT_CATALOG
    assert hm.parse_catalog("") is hm.DEFAULT_CATALOG
    assert hm.parse_catalog("   ") is hm.DEFAULT_CATALOG


def test_parse_catalog_valid_json():
    raw = json.dumps(
        [{"model": "m1", "label": "M1", "efforts": ["low"], "default_effort": "low"}]
    )
    catalog = hm.parse_catalog(raw)
    assert catalog[0]["model"] == "m1"


def test_parse_catalog_invalid_json_raises():
    with pytest.raises(hm.ModelCatalogError):
        hm.parse_catalog("{not json")


def test_parse_catalog_non_list_raises():
    with pytest.raises(hm.ModelCatalogError):
        hm.parse_catalog(json.dumps({"model": "m1"}))
    with pytest.raises(hm.ModelCatalogError):
        hm.parse_catalog("[]")  # empty


def test_allowed_efforts_and_default():
    cat = hm.DEFAULT_CATALOG
    assert "xhigh" in hm.allowed_efforts(cat, "openai.gpt-5.5")
    # GPT omits "max" (it collapses onto xhigh), so it must NOT be offered.
    assert "max" not in hm.allowed_efforts(cat, "openai.gpt-5.5")
    assert "max" in hm.allowed_efforts(cat, "global.anthropic.claude-opus-4-8")
    assert hm.allowed_efforts(cat, "nope") == ()
    assert hm.default_effort_for(cat, "openai.gpt-5.5") == "xhigh"
    assert hm.default_effort_for(cat, "unknown") == hm.DEFAULT_EFFORT


def test_validate_model_effort_ok():
    cat = hm.DEFAULT_CATALOG
    assert hm.validate_model_effort(cat, "openai.gpt-5.5", "high") == (
        "openai.gpt-5.5",
        "high",
    )


def test_validate_model_effort_defaults_when_effort_omitted():
    cat = hm.DEFAULT_CATALOG
    assert hm.validate_model_effort(cat, "openai.gpt-5.5", None) == (
        "openai.gpt-5.5",
        "xhigh",
    )


def test_validate_model_effort_missing_model_raises():
    with pytest.raises(hm.ModelCatalogError):
        hm.validate_model_effort(hm.DEFAULT_CATALOG, None, "high")


def test_validate_model_effort_unknown_model_raises():
    with pytest.raises(hm.ModelCatalogError):
        hm.validate_model_effort(hm.DEFAULT_CATALOG, "anthropic.made-up", "high")


def test_validate_model_effort_effort_not_offered_raises():
    # "max" is valid for Claude but NOT offered for gpt-5.5 -> reject.
    with pytest.raises(hm.ModelCatalogError):
        hm.validate_model_effort(hm.DEFAULT_CATALOG, "openai.gpt-5.5", "max")
