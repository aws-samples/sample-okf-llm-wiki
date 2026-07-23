"""The harvest prompts are built for the run's SOURCE.

Regression: a Redshift harvest used to be told the FIXED type strings were
`Glue Table`/`Glue Database` (the prompt was hardcoded to Glue), so it authored
Redshift docs tagged `type: Glue Table`. Every prompt now fills its source-specific
facts from the source's SourcePromptProfile.
"""

from __future__ import annotations

import pytest

from harvest import prompts
from harvest.glue_source import GlueAthenaSource
from harvest.redshift_source import RedshiftSource

GLUE = GlueAthenaSource.prompt_profile
REDSHIFT = RedshiftSource.prompt_profile

# (builder, needs no extra args) — every per-source prompt builder.
_BUILDERS = [
    prompts.build_supervisor_prompt,
    prompts.build_reviewer_prompt,
    prompts.build_context_extractor_prompt,
    prompts.build_table_author_prompt,
    prompts.build_reference_author_prompt,
]


def _redshift(builder):
    # build_supervisor_prompt takes recursive_improvement first; the rest take the
    # profile positionally. Call each with the Redshift profile as a kwarg.
    return builder(profile=REDSHIFT)


@pytest.mark.parametrize("builder", _BUILDERS)
def test_no_unfilled_tokens_for_either_source(builder):
    for prof in (GLUE, REDSHIFT):
        text = builder(profile=prof)
        assert "⟪" not in text and "⟫" not in text


@pytest.mark.parametrize("builder", _BUILDERS)
def test_glue_prompts_say_glue(builder):
    text = builder(profile=GLUE)
    assert "athena-glue.md" in text
    assert "Athena/Trino" in text


@pytest.mark.parametrize("builder", _BUILDERS)
def test_redshift_prompts_say_redshift_not_glue(builder):
    text = builder(profile=REDSHIFT)
    assert "redshift.md" in text
    assert "amazon-redshift" in text
    # The Glue-only nouns must NOT leak into a Redshift prompt.
    assert "athena-glue.md" not in text
    assert "Glue Table" not in text
    assert "Glue Database" not in text


def test_table_author_states_correct_type_strings():
    glue = prompts.build_table_author_prompt(GLUE)
    assert "`Glue Database`" in glue and "`Glue Table`" in glue

    rs = prompts.build_table_author_prompt(REDSHIFT)
    assert "`Redshift Database`" in rs
    assert "`Redshift Table`" in rs
    assert "`Redshift External Table`" in rs


def test_default_profile_is_glue_backcompat():
    # No-arg builders (and the module constants) still produce the Glue prompt.
    assert prompts.build_table_author_prompt() == prompts.TABLE_AUTHOR_PROMPT
    assert "Glue Table" in prompts.TABLE_AUTHOR_PROMPT
    assert prompts.build_supervisor_prompt() == prompts.SUPERVISOR_PROMPT


def test_annotation_prompt_is_source_aware():
    rs = prompts.build_annotation_prompt(
        dataset="orders_analytics",
        annotations=[],
        results_rel=".harvest/annotation_results.json",
        profile=REDSHIFT,
    )
    assert "amazon-redshift" in rs
    assert "Glue Table" not in rs
