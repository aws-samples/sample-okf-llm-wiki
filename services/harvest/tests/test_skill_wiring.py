"""The vendored okf-authoring skill is present and discoverable by the agent.

deepagents loads skills natively via create_deep_agent(skills=["/skills/"]);
_skill_root() locates the top-level skills dir we ship in the image. These tests
don't need deepagents/AWS — they exercise the packaging + locator only.
"""

from pathlib import Path

from harvest import agent as ag


def test_skill_is_vendored():
    # skills/okf-authoring/SKILL.md ships next to the harvest package.
    pkg_skills = Path(ag.__file__).resolve().parents[2] / "skills"
    skill_md = pkg_skills / "okf-authoring" / "SKILL.md"
    assert skill_md.is_file(), "vendored okf-authoring SKILL.md missing from image"
    # The Athena/Glue source adapter (the load-bearing dialect reference) ships too.
    assert (
        pkg_skills / "okf-authoring" / "references" / "sources" / "athena-glue.md"
    ).is_file()
    assert (pkg_skills / "okf-authoring" / "references" / "templates.md").is_file()


def test_skill_md_has_deepagents_frontmatter():
    # deepagents requires name + description frontmatter to surface the skill.
    skill_md = (
        Path(ag.__file__).resolve().parents[2] / "skills" / "okf-authoring" / "SKILL.md"
    )
    head = skill_md.read_text(encoding="utf-8")[:400]
    assert head.startswith("---")
    assert "name: okf-authoring" in head
    assert "description:" in head


def test_skill_root_locates_default():
    root = ag._skill_root()
    assert root is not None
    assert (root / "okf-authoring" / "SKILL.md").is_file()


def test_skill_root_env_override(tmp_path, monkeypatch):
    # A valid OKF_SKILLS_DIR (containing okf-authoring/SKILL.md) is honored.
    skill_dir = tmp_path / "okf-authoring"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: okf-authoring\ndescription: x\n---\n"
    )
    monkeypatch.setenv("OKF_SKILLS_DIR", str(tmp_path))
    assert ag._skill_root() == tmp_path.resolve()


def test_skill_root_ignores_bad_env(tmp_path, monkeypatch):
    # A bogus override (no SKILL.md) falls back to the packaged skill.
    monkeypatch.setenv("OKF_SKILLS_DIR", str(tmp_path / "nope"))
    root = ag._skill_root()
    assert root is not None
    assert (root / "okf-authoring" / "SKILL.md").is_file()
