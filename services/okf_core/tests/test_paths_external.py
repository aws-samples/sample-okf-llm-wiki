"""The external/ (cross-dataset) path helpers shared by control_api and harvest."""

from __future__ import annotations

import pytest

from okf_core.paths import (
    EXTERNAL_DIR,
    external_pair_prefix,
    is_external_concept_id,
)


def test_external_pair_prefix_shape():
    assert EXTERNAL_DIR == "external"
    assert external_pair_prefix("crm", "customers") == "external/crm/customers/"


def test_external_pair_prefix_validates_segments():
    with pytest.raises(ValueError):
        external_pair_prefix("crm/evil", "customers")
    with pytest.raises(ValueError):
        external_pair_prefix("crm", "../escape")


def test_is_external_concept_id():
    assert is_external_concept_id("external/crm/customers/joins/a__b")
    assert is_external_concept_id("external/crm/customers/overview")
    assert not is_external_concept_id("tables/races")
    assert not is_external_concept_id("references/joins/a__b")
    # Only the top-level external/ dir counts — not a nested name.
    assert not is_external_concept_id("references/external/other")
    assert not is_external_concept_id("externalish/x")
