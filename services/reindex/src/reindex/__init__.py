"""reindex — SQS-triggered Lambda that keeps the S3 Vectors index in sync with
the bundle bucket.

S3 EventBridge emits ``.md`` Object Created / Object Deleted events to the
default bus; an EventBridge rule forwards them to an SQS queue that triggers this
worker. Each event is deduped/ordered on ``object.sequencer`` (via the
``okf-freshness`` DynamoDB table) and then applied: create/update re-embeds and
PutVectors, delete removes the vector. The heavy lifting (Titan embed, S3 Vectors
shapes, bundle-key parsing, embed text + metadata) lives in okf_core / okf_aws so
this module only owns the event plumbing.
"""

from reindex.handler import lambda_handler, process_record

__all__ = ["lambda_handler", "process_record"]
