"""OKF document model: parse / serialize / validate markdown-with-frontmatter.

Ported verbatim in spirit from the reference agent's ``bundle/document.py`` so
that bundles this system produces are byte-for-byte compatible with the existing
``na_mi_formula_1_curated`` golden bundle. OKF requires only a small set of
frontmatter keys; everything else is free-form.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

# Only these four keys are structurally required for a concept to be
# meaningfully consumed (OKF SPEC §4.1). ``type`` alone is required by the spec,
# but the reference producer always emits all four and downstream index
# generation depends on title/description, so we enforce the fuller set.
REQUIRED_FRONTMATTER_KEYS = ("type", "title", "description", "timestamp")

_FRONTMATTER_DELIM = "---"


class OKFDocumentError(ValueError):
    """Raised when a document cannot be parsed or fails validation."""


@dataclass
class OKFDocument:
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @classmethod
    def parse(cls, text: str) -> "OKFDocument":
        lines = text.splitlines()
        if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
            # No frontmatter block: whole file is the body.
            return cls(frontmatter={}, body=text)

        end_idx = None
        for i in range(1, len(lines)):
            if lines[i].strip() == _FRONTMATTER_DELIM:
                end_idx = i
                break
        if end_idx is None:
            raise OKFDocumentError("Unterminated YAML frontmatter block")

        fm_text = "\n".join(lines[1:end_idx])
        try:
            fm = yaml.safe_load(fm_text) or {}
        except yaml.YAMLError as e:
            raise OKFDocumentError(f"Invalid YAML in frontmatter: {e}") from e
        if not isinstance(fm, dict):
            raise OKFDocumentError("Frontmatter must be a YAML mapping")

        body = "\n".join(lines[end_idx + 1 :])
        if body.startswith("\n"):
            body = body[1:]
        return cls(frontmatter=fm, body=body)

    def serialize(self) -> str:
        fm_text = yaml.safe_dump(
            self.frontmatter, sort_keys=False, allow_unicode=True
        ).rstrip()
        body = self.body if self.body.endswith("\n") else self.body + "\n"
        return f"{_FRONTMATTER_DELIM}\n{fm_text}\n{_FRONTMATTER_DELIM}\n\n{body}"

    def validate(self) -> None:
        missing = [k for k in REQUIRED_FRONTMATTER_KEYS if not self.frontmatter.get(k)]
        if missing:
            raise OKFDocumentError(
                f"Missing required frontmatter keys: {', '.join(missing)}"
            )
