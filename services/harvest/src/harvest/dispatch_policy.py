"""Which sub-agent types the MODEL may dispatch — a tiny, dependency-free policy.

Two enforcement points import this (so it must import nothing heavy):

* ``okf_guard.SubagentDispatchGuard`` — the supervisor's static ``task`` tool
  path (agent middleware);
* the ``harvest.subagent_io`` shim — the QuickJS ``task()`` path, which never
  reaches agent middleware.

``run_review``'s own dispatches call the task tool object directly and bypass
both — that is the point: workflow-only types exist FOR the workflow. A
fix-author the model dispatched itself would run with no cluster bound and its
write guard would fail closed; the refusal turns that wasted run into a
one-line instructive error.
"""

from __future__ import annotations

WORKFLOW_ONLY_SUBAGENTS = frozenset({"fix-author", "context-reviewer"})

WORKFLOW_ONLY_DISPATCH_MSG = (
    "Refused: `{sub}` is dispatched by the `run_review` tool only — never "
    "directly. For a small ad-hoc correction, apply the edit yourself with "
    "`edit_file`."
)
