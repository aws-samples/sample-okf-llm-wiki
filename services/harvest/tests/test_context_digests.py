"""harvest.context_digests — the extractor-digest recorder feeding the
review's context-fidelity phase."""

from __future__ import annotations

import pytest

from harvest import context_digests as cd


@pytest.fixture(autouse=True)
def _reset_recorder():
    # Module-global root: leak-proof the tests against each other and against
    # any other test that builds an agent (which calls configure()).
    yield
    cd.configure(None)


def test_record_filters_by_type_numbers_files_and_keeps_brief(tmp_path):
    cd.configure(tmp_path)
    cd.record("table-author", "brief", "not a digest — wrong type")
    cd.record("context-extractor", "Extract facts from dict.md", "## tables/races\n- fact one")
    cd.record("context-extractor", None, "second digest")
    cd.record("context-extractor", "b", "   ")  # empty result: nothing reviewable

    paths = cd.digest_paths(tmp_path)
    assert paths == [
        ".harvest/context/digest-01.md",
        ".harvest/context/digest-02.md",
    ]
    first = (tmp_path / paths[0]).read_text(encoding="utf-8")
    # The dispatch brief travels with the digest (the fidelity reviewer sees
    # what the extractor was ASKED to cover), and the digest text is verbatim.
    assert "Extract facts from dict.md" in first
    assert "- fact one" in first
    second = (tmp_path / paths[1]).read_text(encoding="utf-8")
    assert "second digest" in second and "Dispatch brief" not in second


def test_record_without_configure_is_a_noop(tmp_path):
    cd.configure(None)
    cd.record("context-extractor", "b", "digest")
    assert cd.digest_paths(tmp_path) == []


def test_digest_paths_empty_when_dir_missing(tmp_path):
    assert cd.digest_paths(tmp_path) == []
