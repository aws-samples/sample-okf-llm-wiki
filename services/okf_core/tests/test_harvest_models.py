"""The harvest model/effort catalog: parsing + validation.

Pure module (no AWS). Covers the trust boundary the Control API relies on: only
(model, effort) pairs offered by the catalog are accepted; everything else raises
ModelCatalogError (-> 400).
"""

import json

import pytest

from okf_core import harvest_models as hm


def test_default_catalog_offers_the_full_model_set():
    models = [e["model"] for e in hm.DEFAULT_CATALOG]
    assert "global.anthropic.claude-opus-4-8" in models
    assert "openai.gpt-5.6-sol" in models
    assert "global.anthropic.claude-opus-5" in models
    assert "global.anthropic.claude-sonnet-5" in models
    assert "openai.gpt-5.6-terra" in models
    # Opus 4.8 stays FIRST — the first entry is the UI picker's default; the
    # others are additional offerings, not a new default.
    assert models[0] == "global.anthropic.claude-opus-4-8"
    # Every entry offers the full effort ladder with the xhigh default.
    for entry in hm.DEFAULT_CATALOG:
        assert entry["efforts"] == ["low", "medium", "high", "xhigh", "max"]
        assert entry["default_effort"] == "xhigh"


def test_opus_5_offers_full_effort_ladder():
    cat = hm.DEFAULT_CATALOG
    assert hm.allowed_efforts(cat, "global.anthropic.claude-opus-5") == (
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )
    assert hm.default_effort_for(cat, "global.anthropic.claude-opus-5") == "xhigh"
    assert hm.validate_model_effort(cat, "global.anthropic.claude-opus-5", None) == (
        "global.anthropic.claude-opus-5",
        "xhigh",
    )


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
    assert "xhigh" in hm.allowed_efforts(cat, "openai.gpt-5.6-sol")
    # GPT-5.6 added "max" as a native level, so it IS offered now (same as Claude).
    assert "max" in hm.allowed_efforts(cat, "openai.gpt-5.6-sol")
    assert "max" in hm.allowed_efforts(cat, "global.anthropic.claude-opus-4-8")
    assert hm.allowed_efforts(cat, "nope") == ()
    assert hm.default_effort_for(cat, "openai.gpt-5.6-sol") == "xhigh"
    assert hm.default_effort_for(cat, "unknown") == hm.DEFAULT_EFFORT


def test_validate_model_effort_ok():
    cat = hm.DEFAULT_CATALOG
    assert hm.validate_model_effort(cat, "openai.gpt-5.6-sol", "high") == (
        "openai.gpt-5.6-sol",
        "high",
    )


def test_validate_model_effort_defaults_when_effort_omitted():
    cat = hm.DEFAULT_CATALOG
    assert hm.validate_model_effort(cat, "openai.gpt-5.6-sol", None) == (
        "openai.gpt-5.6-sol",
        "xhigh",
    )


def test_validate_model_effort_missing_model_raises():
    with pytest.raises(hm.ModelCatalogError):
        hm.validate_model_effort(hm.DEFAULT_CATALOG, None, "high")


def test_validate_model_effort_unknown_model_raises():
    with pytest.raises(hm.ModelCatalogError):
        hm.validate_model_effort(hm.DEFAULT_CATALOG, "anthropic.made-up", "high")


def test_validate_model_effort_effort_not_offered_raises():
    # An effort the model doesn't list (here a bogus level) -> reject. Guards the
    # trust boundary: only catalog-offered (model, effort) pairs reach Bedrock.
    with pytest.raises(hm.ModelCatalogError):
        hm.validate_model_effort(hm.DEFAULT_CATALOG, "openai.gpt-5.6-sol", "ultra")
