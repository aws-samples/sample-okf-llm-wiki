"""OKF incremental path.

Reacts to Glue Data Catalog table-change events (delivered via EventBridge ->
SQS) and, when a real schema change is confirmed, stages a column diff and
invokes the harvest AgentCore runtime scoped to the changed table. Also provides
a nightly reconcile pass that catches missed events by comparing stored vs.
current Glue table versions.
"""

from incremental.diff import compute_column_diff
from incremental.handler import lambda_handler, process_event

# NOTE: do NOT `from incremental.reconcile import reconcile` here — binding the
# name `reconcile` at package level shadows the `incremental.reconcile`
# SUBMODULE, which breaks the Lambda handler path
# `incremental.reconcile.reconcile_handler` (it would resolve to the function,
# not the module). Import the reconcile helpers from the submodule directly.

__all__ = [
    "compute_column_diff",
    "lambda_handler",
    "process_event",
]
