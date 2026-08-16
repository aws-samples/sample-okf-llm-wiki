"""Opus 5 prompt hygiene (per the Opus 5 prompting guide).

Opus 5 verifies its own work unprompted, delegates to sub-agents more readily,
and writes longer documents than prior models. The prompts therefore must:
calibrate written-deliverable length explicitly, prescribe delegation exactly
(no invented sub-agents, no verification beyond the one review pass), scope
each reviewer to its cluster, and never carry redundant "double-check"-style
instructions that compound with the model's own self-checking.
"""

from harvest import prompts

# The roles that WRITE bundle docs get the authoring contract (_AUTHORING_TMPL);
# the read-only roles must never see it — a write contract in a prompt whose own
# body says "READ-ONLY, you do NOT write files" is a standing contradiction.
WRITER_PROMPTS = (
    prompts.SUPERVISOR_PROMPT,
    prompts.TABLE_AUTHOR_PROMPT,
    prompts.REFERENCE_AUTHOR_PROMPT,
    prompts.ANNOTATION_PROMPT,
    prompts.build_cross_supervisor_prompt(),
    prompts.build_cross_author_prompt(),
)
READONLY_PROMPTS = (
    prompts.REVIEWER_PROMPT,
    prompts.CONTEXT_EXTRACTOR_PROMPT,
    prompts.build_cross_reviewer_prompt(),
)
ALL_PROMPTS = WRITER_PROMPTS + READONLY_PROMPTS


def test_writer_prompts_right_size_written_docs():
    # Written deliverables run long on Opus 5 — the authoring fragment carries
    # explicit length calibration (substance, then stop; no filler/boilerplate),
    # so every agent that writes docs inherits it.
    for p in WRITER_PROMPTS:
        assert "Right-size every doc" in p
        assert "boilerplate" in p


def test_readonly_prompts_never_carry_the_write_contract():
    for p in READONLY_PROMPTS:
        assert "READ-ONLY, you do NOT write" in p
        assert "## Authoring (write path + guard)" not in p
        assert "Right-size every doc" not in p
        assert "augment, don't" not in p


def test_supervisor_prescribes_delegation_exactly():
    # Opus 5 delegates readily; every supervisor (standard AND cross-mode) must
    # be told which dispatches the workflow prescribes and that nothing beyond
    # them is warranted — including extra verification rounds (it verifies its
    # own work without being told).
    for p in (prompts.SUPERVISOR_PROMPT, prompts.build_cross_supervisor_prompt()):
        assert "Delegation discipline" in p
        assert "finish yourself in a couple of" in p
        assert "NO further verification" in p


def test_review_pass_is_bounded_to_a_single_pass():
    for p in (prompts.SUPERVISOR_PROMPT, prompts.build_cross_supervisor_prompt()):
        # Stated once per prompt: step 7 forbids the re-review, and the
        # delegation-discipline paragraph owns the no-extra-verification bound.
        assert "re-review docs after" in p
        assert "add NO further verification" in p


def test_supervisor_final_summary_is_length_calibrated():
    assert "Keep your final summary short" in prompts.SUPERVISOR_PROMPT
    assert "Keep your final summary short" in prompts.build_cross_supervisor_prompt()


def test_reviewer_scope_is_its_cluster_only():
    # Opus 5 expands task scope; a reviewer wandering the bundle duplicates the
    # other reviewers' work. Reading a linked out-of-cluster doc for the
    # cross-doc check stays allowed.
    p = prompts.REVIEWER_PROMPT
    assert "EXACTLY the cluster" in p
    assert "every other doc has its own" in p


def test_no_redundant_self_check_instructions():
    # "Double-check your answer" style instructions compound with Opus 5's own
    # self-verification and add cost without improving results.
    for p in ALL_PROMPTS:
        low = p.lower()
        assert "double-check" not in low and "double check" not in low
        assert "check your work" not in low
        assert "verify your answer" not in low
