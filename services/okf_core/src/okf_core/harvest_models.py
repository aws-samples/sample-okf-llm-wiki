"""Harvest model + reasoning-effort catalog — the source of truth for the
per-harvest model picker.

A harvest can run on one of several models (an Anthropic Claude model on the
Bedrock Converse API, or an OpenAI GPT model on the Bedrock Mantle endpoint),
each at a chosen reasoning **effort**. Which model/effort a given harvest uses is
selected per-run in the UI, sent in the ``POST /harvest`` body, validated by the
Control API, and threaded into the harvest invocation payload (see
``docs/CONVENTIONS.md``). It is NO LONGER a fixed deploy-time env var — the env
vars (``OKF_HARVEST_MODEL``/``OKF_HARVEST_EFFORT``) remain only as the fallback
default when a request omits them.

This module is pure (no AWS, no agent deps): it owns the effort vocabulary, the
catalog shape, the default catalog, a JSON parser, and a validator — so the
Control API (validator), the UI (via a Terraform-provided JSON catalog), and the
harvest runtime all agree on exactly which ``(model, effort)`` pairs are legal.
Mirrors the design of :mod:`okf_core.sources`.

**Catalog shape** — a list of model entries::

    [
      {
        "model": "global.anthropic.claude-opus-4-8",
        "label": "Claude Opus 4.8",
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "default_effort": "xhigh"
      },
      {
        "model": "openai.gpt-5.5",
        "label": "GPT-5.5",
        "efforts": ["low", "medium", "high", "xhigh"],
        "default_effort": "xhigh"
      }
    ]

Terraform is the authority on the deployed catalog (``var.harvest_model_catalog``
→ ``OKF_HARVEST_MODEL_CATALOG`` env for the Control API + ``VITE_HARVEST_MODEL_
CATALOG`` for the UI). :data:`DEFAULT_CATALOG` is the built-in fallback used by
tests and by any consumer that isn't handed a catalog.
"""

from __future__ import annotations

import json
from typing import Any

# -- effort vocabulary -------------------------------------------------------

#: The single effort vocabulary shared end-to-end. These are the strings the
#: harvest agent already understands: on the Converse (Claude) path they ride
#: verbatim into ``output_config.effort``; on the GPT path they map onto OpenAI's
#: ``reasoning_effort`` scale (``max``/``xhigh`` -> ``xhigh``). Ordered
#: low -> high so a UI can render them in a sensible order.
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

#: The default effort when a harvest doesn't specify one — maximum reasoning, to
#: match the authoring agent's quality bias. Every catalog entry SHOULD list this
#: (or override with its own ``default_effort``).
DEFAULT_EFFORT = "xhigh"

# -- catalog entry keys ------------------------------------------------------

MODEL_KEY = "model"
LABEL_KEY = "label"
EFFORTS_KEY = "efforts"
DEFAULT_EFFORT_KEY = "default_effort"

#: The built-in catalog: one Anthropic model (Converse) + one OpenAI model
#: (Mantle). GPT collapses ``max`` onto ``xhigh`` (see ``harvest.agent._GPT_
#: EFFORT``), so the GPT entry omits ``max`` rather than offering a level that
#: silently degrades to another. Terraform's ``var.harvest_model_catalog``
#: overrides this in a real deployment.
DEFAULT_CATALOG: list[dict[str, Any]] = [
    {
        MODEL_KEY: "global.anthropic.claude-opus-4-8",
        LABEL_KEY: "Claude Opus 4.8",
        EFFORTS_KEY: ["low", "medium", "high", "xhigh", "max"],
        DEFAULT_EFFORT_KEY: "xhigh",
    },
    {
        MODEL_KEY: "openai.gpt-5.5",
        LABEL_KEY: "GPT-5.5",
        EFFORTS_KEY: ["low", "medium", "high", "xhigh"],
        DEFAULT_EFFORT_KEY: "xhigh",
    },
]


class ModelCatalogError(ValueError):
    """A model/effort selection is not offered by the catalog (or the catalog
    itself is malformed). Surfaced to the Control API caller as a 400."""


def parse_catalog(raw: str | None) -> list[dict[str, Any]]:
    """Parse a JSON catalog string (the Terraform-provided env), or fall back.

    ``raw`` is the ``OKF_HARVEST_MODEL_CATALOG`` env value — a JSON array of model
    entries. Empty/unset returns :data:`DEFAULT_CATALOG`. A present-but-invalid
    value raises :class:`ModelCatalogError` (a deploy misconfiguration we want to
    surface loudly, not silently mask with the default).
    """
    if not raw or not raw.strip():
        return DEFAULT_CATALOG
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ModelCatalogError(f"OKF_HARVEST_MODEL_CATALOG is not valid JSON: {exc}")
    if not isinstance(parsed, list) or not parsed:
        raise ModelCatalogError(
            "OKF_HARVEST_MODEL_CATALOG must be a non-empty JSON array of model entries"
        )
    return parsed


def _entry_for(catalog: list[dict[str, Any]], model: str) -> dict[str, Any] | None:
    for entry in catalog:
        if isinstance(entry, dict) and entry.get(MODEL_KEY) == model:
            return entry
    return None


def allowed_efforts(catalog: list[dict[str, Any]], model: str) -> tuple[str, ...]:
    """The efforts a given model offers in ``catalog`` (empty if model absent)."""
    entry = _entry_for(catalog, model)
    if entry is None:
        return ()
    efforts = entry.get(EFFORTS_KEY)
    return tuple(efforts) if isinstance(efforts, list) else ()


def default_effort_for(catalog: list[dict[str, Any]], model: str) -> str:
    """The model's ``default_effort``, or the global :data:`DEFAULT_EFFORT`."""
    entry = _entry_for(catalog, model)
    if entry and isinstance(entry.get(DEFAULT_EFFORT_KEY), str):
        return entry[DEFAULT_EFFORT_KEY]
    return DEFAULT_EFFORT


def validate_model_effort(
    catalog: list[dict[str, Any]],
    model: str | None,
    effort: str | None,
) -> tuple[str, str]:
    """Validate a ``(model, effort)`` request against ``catalog``; return the pair.

    - ``model`` MUST name an entry in the catalog.
    - ``effort`` MUST be one of that model's offered efforts; when ``effort`` is
      omitted (None/empty), the model's ``default_effort`` is used.

    Raises :class:`ModelCatalogError` (→ 400) on anything not offered, so an
    arbitrary client-supplied string can never reach ``bedrock:InvokeModel``.
    This is the trust boundary: the runtime deliberately does not allow-list
    effort, so validation lives HERE.
    """
    if not model:
        raise ModelCatalogError("missing required field: model")
    efforts = allowed_efforts(catalog, model)
    if not efforts:
        offered = ", ".join(e.get(MODEL_KEY, "?") for e in catalog)
        raise ModelCatalogError(
            f"unsupported model {model!r}; offered: {offered}"
        )
    chosen = effort or default_effort_for(catalog, model)
    if chosen not in efforts:
        raise ModelCatalogError(
            f"effort {chosen!r} not offered for model {model!r}; "
            f"allowed: {', '.join(efforts)}"
        )
    return model, chosen
