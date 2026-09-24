"""Guard against dependency confusion on the repo's own (unpublished) packages.

The sibling packages under services/ are installed from local source (Dockerfiles,
scripts/build_lambdas.sh, README) and are NOT published to PyPI. Declaring one by
bare name in any pyproject would make pip/uv resolve it from the public index,
where anyone can register the name ("okf-core" already belongs to an unrelated
project). No pyproject may list a sibling package in any dependency table.
"""

import re
import tomllib
from pathlib import Path

_SERVICES = Path(__file__).resolve().parents[1] / "services"
_PYPROJECTS = sorted(_SERVICES.glob("*/pyproject.toml"))
_SIBLINGS = {
    tomllib.loads(p.read_text())["project"]["name"].lower() for p in _PYPROJECTS
}


def _dist_name(requirement: str) -> str:
    return re.split(r"[\s\[<>=!~;@(]", requirement.strip(), maxsplit=1)[0].lower()


def test_sibling_set_is_populated():
    assert {"okf-core", "okf-aws", "okf-consumption-mcp"} <= _SIBLINGS


def test_no_pyproject_declares_a_sibling_package():
    offenders = []
    for path in _PYPROJECTS:
        project = tomllib.loads(path.read_text())["project"]
        reqs = list(project.get("dependencies", []))
        for extra in project.get("optional-dependencies", {}).values():
            reqs.extend(extra)
        offenders += [
            f"{path.relative_to(_SERVICES)}: {r}"
            for r in reqs
            if _dist_name(r).replace("_", "-") in _SIBLINGS
        ]
    assert not offenders, "unpublished sibling packages declared as deps:\n" + "\n".join(offenders)
