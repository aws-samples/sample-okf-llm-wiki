# reindex worker

SQS-triggered Lambda that keeps the S3 Vectors index in sync with the bundle
bucket.

## Packaging: zip Lambda (NOT a container)

This service ships as a **zip Lambda** on the managed `python3.12` runtime — no
Dockerfile. It only needs boto3 (provided by the runtime) plus the pure-Python
shared libs, so a zip is smaller and cold-starts faster than an image.

- Handler: `reindex.handler.lambda_handler`
- Runtime: `python3.12`
- Build the deployment package by installing this package + its shared-lib deps
  into a staging dir and zipping it, e.g.:

  ```bash
  pip install \
      services/reindex \
      services/okf_core \
      services/okf_aws \
      -t build/reindex
  (cd build/reindex && zip -r ../reindex.zip .)
  ```

  `reindex.zip` then becomes the `aws_lambda_function` code artifact (Zip
  package type).

## Event wiring (see docs/API_REFERENCE.md §4 and Terraform)

```
S3 bundle bucket (eventbridge = true)
  -> default EventBridge bus
  -> aws_cloudwatch_event_rule  {source: ["aws.s3"],
                                 detail-type: ["Object Created","Object Deleted"]}
  -> SQS queue
  -> aws_lambda_event_source_mapping  (batch_size=N,
        function_response_types=["ReportBatchItemFailures"])
  -> reindex.handler.lambda_handler
```

Because the mapping declares `ReportBatchItemFailures`, the handler returns
`{"batchItemFailures": [...]}` and SQS redrives only the records that raised.

## Environment variables (docs/CONVENTIONS.md)

| Var | Meaning |
|---|---|
| `AWS_REGION` | region for all clients |
| `OKF_BUNDLE_BUCKET` | S3 bundle bucket to GET the changed `.md` from |
| `OKF_VECTOR_BUCKET` | S3 Vectors bucket |
| `OKF_VECTOR_INDEX` | S3 Vectors index name |
| `OKF_FRESHNESS_TABLE` | DynamoDB dedup table (default `okf-freshness`) |

## IAM (execution role)

`s3:GetObject` on the bundle bucket; `s3vectors:PutVectors`, `DeleteVectors`,
`GetIndex`, `CreateIndex` on the vector bucket/index; `dynamodb:PutItem` on the
freshness table; `sqs:ReceiveMessage`/`DeleteMessage`/`GetQueueAttributes` on the
queue; `bedrock:InvokeModel` on the Titan V2 model.
