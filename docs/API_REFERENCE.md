# External API reference

Notes on the third-party API shapes this code is written against, gathered from
the LangChain and AWS docs (pinned 2026-07-01). These are the details that are
easy to get wrong or that changed recently, kept here so the code and the docs
don't drift. Sources are at the end of each section.

---

## 1. deepagents (LangChain Deep Agents)

- Install `deepagents` (stable 0.6.x), Python ≥ 3.11. Extras: `deepagents[aws]`
  for Bedrock, `deepagents[quickjs]` for dynamic subagents.
- `from deepagents import create_deep_agent, CompiledSubAgent`
- Signature (everything after `tools` is keyword-only):
  ```python
  create_deep_agent(
      model: str | BaseChatModel | None = None,
      tools=None,
      *,
      system_prompt: str | SystemMessage | None = None,   # not "instructions"
      middleware: Sequence[AgentMiddleware] = (),
      subagents=None,
      backend: BackendProtocol | None = None,
      **model_kwargs,   # e.g. model_provider="bedrock_converse", temperature=...
  )
  ```
  Returns a compiled LangGraph graph; call `.invoke({"messages": [...]})`,
  `.ainvoke(...)`, or `.stream(...)`.
- **Bedrock model.** Build a `ChatBedrockConverse` and pass it as
  `create_deep_agent(model=chat_model, ...)`. It comes from the standalone
  `langchain-aws` package (`pip install langchain-aws`), not the `langchain[aws]`
  extra. Building the model explicitly lets adaptive-thinking config ride on it
  via `additional_model_request_fields` (see §7 of `OKF_DESIGN.md`).
- **Backends** (`from deepagents.backends import ...`): `StateBackend` (default,
  ephemeral in graph state), `FilesystemBackend(root_dir=<abs>,
  virtual_mode=True)` (real files — `virtual_mode=True` is required, the default
  `False` gives no path confinement), and `CompositeBackend(default=...,
  routes={"/prefix/": ...})`. The recommended setup is
  `CompositeBackend(default=StateBackend(), routes={"/workspace/":
  FilesystemBackend(root_dir=..., virtual_mode=True)})`, so the agent's internal
  files (`/large_tool_results/`, `/conversation_history/`) stay ephemeral and
  only `/workspace/` hits disk.
- **Built-in tools:** `ls`, `read_file`, `write_file`, `edit_file`, `glob`,
  `grep`, `task`, `write_todos`. `write_file(file_path, content)`;
  `edit_file(file_path, old_string, new_string)` is an exact-string replace
  (Claude Code semantics); `read_file` supports pagination.
- **Subagent dict** (`SubAgent`): `name` (required), `description` (required),
  `system_prompt` (required — never inherited), `tools` (optional — when set it
  *replaces* the inherited tools), `model` (optional — inherits the parent),
  `middleware` (optional — not inherited; appended to the subagent's default
  stack). Exposed to the parent through the `task` tool.
- **Dynamic fan-out:** add `CodeInterpreterMiddleware` from `langchain_quickjs`
  (needs `deepagents[quickjs]`); with subagents configured, agent code can call a
  `task()` global to fan out. Beta.
- **Middleware:** `from langchain.agents.middleware import AgentMiddleware`.
  ```python
  def wrap_tool_call(self, request, handler):
      name = request.tool_call["name"]      # dict indexing, not request.name
      args = request.tool_call["args"]
      # short-circuit (the tool never runs): return a ToolMessage/Command without handler
      return ToolMessage(content="refused", tool_call_id=request.tool_call["id"])
      # or override args then run: return handler(request.override(...))
      # or run normally: return handler(request)
  ```
  `ToolMessage` is from `langchain.messages`, `Command` from `langgraph.types`.
  Custom `middleware=` is appended to the main stack. The guard has to be
  attached to each subagent's `middleware` list too, since subagent middleware
  doesn't inherit.

## 2. Bedrock AgentCore Runtime

- SDK: `bedrock-agentcore`. Container images must be ARM64.
- **HTTP agent** (harvest):
  ```python
  from bedrock_agentcore.runtime import BedrockAgentCoreApp
  app = BedrockAgentCoreApp()
  @app.entrypoint
  def invoke(payload, context=None): ...   # payload = deserialized body
  @app.ping
  def ping(): return "HealthyBusy" if busy else "Healthy"
  app.run()   # serves /invocations + /ping on 0.0.0.0:8080
  ```
  Long work goes on a background thread reporting `HealthyBusy`, so the session
  isn't idled out (8h cap). Don't advance `time_of_last_update` on every ping.
- **MCP server** (consumption): serve on `0.0.0.0:8000/mcp`, stateless
  streamable-HTTP.
  ```python
  mcp = FastMCP(host="0.0.0.0", stateless_http=True)
  mcp.run(transport="streamable-http")   # port 8000, path /mcp
  ```
  It has to accept the platform-injected `Mcp-Session-Id` header.
- **Deploy (control plane):** `boto3.client("bedrock-agentcore-control")
  .create_agent_runtime(agentRuntimeName, agentRuntimeArtifact={"containerConfiguration":
  {"containerUri": "<ecr>:<tag>"}}, networkConfiguration={"networkMode": "VPC",
  "networkModeConfig": {"subnets": [...], "securityGroups": [...]}}, roleArn,
  protocolConfiguration={"serverProtocol": "HTTP"|"MCP"}, authorizerConfiguration,
  filesystemConfigurations=[...])`.
- **Invoke (data plane):** `boto3.client("bedrock-agentcore")
  .invoke_agent_runtime(agentRuntimeArn=..., payload=<bytes>, runtimeSessionId=...,
  qualifier="DEFAULT")`. `runtimeSessionId` is the dataset id, giving session
  affinity.
- **Filesystem config:** `{"s3FilesAccessPoint": {"accessPointArn":
  "arn:aws:s3files:<region>:<acct>:file-system/<fs>/access-point/<ap>", "mountPath":
  "/mnt/data"}}`. Runtime-scoped, shared across all sessions, VPC required,
  mountPath under `/mnt`. The exec role needs
  `s3files:ClientMount/ClientWrite/GetAccessPoint`.
- **JWT auth:** `authorizerConfiguration={"customJWTAuthorizer": {"discoveryUrl":
  "<issuer>/.well-known/openid-configuration", "allowedClients": [<client_id>]}}`.
  The discoveryUrl has to end with `/.well-known/openid-configuration`.
- **Terraform (`hashicorp/aws ~> 6.0`):** `aws_bedrockagentcore_agent_runtime`
  with `agent_runtime_artifact { container_configuration { container_uri } }`,
  `network_configuration { network_mode network_mode_config { subnets
  security_groups } }`, `protocol_configuration { server_protocol = "HTTP"|"MCP" }`
  (a block, not a string), `authorizer_configuration { custom_jwt_authorizer {
  discovery_url allowed_clients } }`, `filesystem_configuration {
  s3_files_access_point { access_point_arn mount_path } }`, and
  `lifecycle_configuration { idle_runtime_session_timeout max_lifetime }`. The
  nested configs are HCL blocks; `environment_variables` and `tags` are maps.

## 3. S3 Vectors — `boto3.client("s3vectors")`

- `create_vector_bucket(vectorBucketName=...)`.
- `create_index(vectorBucketName, indexName, dataType="float32", dimension=512,
  distanceMetric="cosine", metadataConfiguration={"nonFilterableMetadataKeys":
  ["title","description","s3_key"]})`. The `dimension`, `distanceMetric`,
  `dataType`, and `metadataConfiguration` are immutable — changing one means
  replacing the index and re-embedding.
- `put_vectors(vectorBucketName, indexName, vectors=[{"key": <path>, "data":
  {"float32": [floats, len == dim]}, "metadata": {...}}])`, up to 500 per call.
  Note `data` is a tagged union `{"float32": [...]}`, not a flat `vector` key. An
  existing key is fully overwritten.
- `query_vectors(vectorBucketName, indexName, topK, queryVector={"float32":[...]},
  filter={...}, returnMetadata=True, returnDistance=True)` returns `{"vectors":
  [{"key","distance","metadata"}], "distanceMetric", "nextToken"}`. Page ≤ 100.
- `delete_vectors(vectorBucketName, indexName, keys=[...])`, up to 500.
- Filter operators: `$eq $ne $gt $gte $lt $lte $in $nin $exists $and $or` (no
  prefix, substring, or regex). Range operators are number-only. `$eq` against a
  list value matches any element.
- The 403 trap: `query_vectors` with a filter or `returnMetadata=True` also needs
  `s3vectors:GetVectors`, not just `s3vectors:QueryVectors`.
- Filterable metadata is ≤ 2 KB per vector (≤ 40 KB total); non-filterable keys
  are ≤ 10 per index.
- **Terraform (`hashicorp/aws ~> 6.0`):** `aws_s3vectors_vector_bucket` and
  `aws_s3vectors_index` (args `data_type`, `dimension`, `distance_metric`, and a
  `metadata_configuration { non_filterable_metadata_keys = [...] }` block — all
  force replacement, matching the API's immutability).

## 4. Titan V2 / Glue / Athena / EventBridge

- **Titan V2:** `boto3.client("bedrock-runtime").invoke_model(modelId=
  "amazon.titan-embed-text-v2:0", body=json.dumps({"inputText": t[:50000],
  "dimensions": 512, "normalize": True}))`. `dimensions` is only 1024, 512, or
  256. Read the result at `json.loads(resp["body"].read())["embedding"]`.
  Throttling raises `ThrottlingException`; retry with backoff.
- **Glue** (`boto3.client("glue")`): `get_databases` / `get_tables` page at 100,
  `get_partitions` at 1000; also `get_table_versions`.
  `Table.StorageDescriptor.Columns[]` is `{Name, Type (Hive string), Comment}`;
  `Table.PartitionKeys[]` has the same shape. Detect change via `Table.UpdateTime`
  and `VersionId` (monotonic), never `LastAccessTime`.
- **Athena** (`boto3.client("athena")`): `start_query_execution(QueryString,
  QueryExecutionContext={"Database": ...}, ResultConfiguration={"OutputLocation":
  "s3://..."} | WorkGroup=...)`, then poll
  `get_query_execution(...)["QueryExecution"]["Status"]["State"]` until a terminal
  state (`SUCCEEDED`, `FAILED`, `CANCELLED` — two Ls), then `get_query_results(...)`.
  Row 0 is the header; cells are at `Rows[].Data[].VarCharValue`; paginate with
  `NextToken`.
- **Glue change event:** `{"source": ["aws.glue"], "detail-type": ["Glue Data
  Catalog Table State Change"]}`; detail `{databaseName, tableName, typeOfChange,
  changedPartitions}`.
- **S3 events:** enable with `aws_s3_bucket_notification { eventbridge = true }`
  (all events flow to the default bus; filter in the rule). Event `{"source":
  ["aws.s3"], "detail-type": ["Object Created","Object Deleted"]}`; detail
  `{bucket.name, object.key, object.size, object.sequencer}`. Order and dedup on
  `object.sequencer` — a hex string, comparable lexicographically per key.

## 5. Terraform (hashicorp/aws ~> 6.0, native throughout)

- `aws_cognito_user_pool` exposes `endpoint` =
  `cognito-idp.<region>.amazonaws.com/<poolId>` (no scheme). Issuer is
  `https://${endpoint}`; discovery is `${issuer}/.well-known/openid-configuration`.
- `aws_cognito_user_pool_client`: `allowed_oauth_flows_user_pool_client=true`,
  `allowed_oauth_flows=["code"]`, `allowed_oauth_scopes`, `callback_urls`,
  `logout_urls`, `supported_identity_providers=["COGNITO"]`. A SPA client has no
  secret.
- API Gateway v2: `aws_apigatewayv2_api` (`protocol_type="HTTP"`) +
  `aws_apigatewayv2_authorizer` (`authorizer_type="JWT"`,
  `identity_sources=["$request.header.Authorization"]`, `jwt_configuration {
  audience=[client_id], issuer="https://${endpoint}" }`) +
  `aws_apigatewayv2_integration` (AWS_PROXY, `integration_uri=<lambda invoke_arn>`,
  `payload_format_version="2.0"`) + `aws_apigatewayv2_route` (`route_key`,
  `authorization_type="JWT"`, `authorizer_id`) + `aws_apigatewayv2_stage`
  (`name="$default"`, `auto_deploy=true`). `aws_lambda_permission` uses
  `source_arn = "${aws_apigatewayv2_api.x.execution_arn}/*/*"`.
- `aws_lambda_function` (package_type Image or Zip), `aws_iam_role` +
  `aws_iam_role_policy` (`managed_policy_arns` / `inline_policy` are deprecated),
  `aws_lambda_event_source_mapping` (SQS: `event_source_arn`, `batch_size`,
  `function_response_types=["ReportBatchItemFailures"]`, no `starting_position`).
- `aws_dynamodb_table` (PAY_PER_REQUEST; declare `attribute` blocks only for key
  attributes, or you get a perpetual diff).
- `aws_s3_bucket_notification { eventbridge = true }` (atomic — one per bucket) +
  `aws_cloudwatch_event_rule` (`event_pattern` via `jsonencode`) +
  `aws_cloudwatch_event_target` + `aws_sqs_queue` + `aws_sqs_queue_policy`
  (Principal `events.amazonaws.com`, must include `Version="2012-10-17"`).
- `aws_cloudfront_origin_access_control` (`signing_protocol="sigv4"`) +
  `aws_cloudfront_distribution` (OAC origin). The SPA fallback is a
  `custom_error_response` for both 403 and 404 → 200 `/index.html`, since OAC on
  S3 returns 403 for a missing object.
- EventBridge S3 key filter: an array of content filters under one field is OR,
  not AND — `object.key = [{prefix},{suffix}]` matches prefix OR suffix. For
  prefix-AND-suffix, use a single wildcard: `object = { key = [{ wildcard =
  "okf/*.md" }] }`.
- S3 Files (native): `aws_s3files_file_system` (`bucket` = bundle-bucket ARN,
  `role_arn`, `accept_bucket_warning`, optional `prefix`; the role is assumed by
  `elasticfilesystem.amazonaws.com`) + `aws_s3files_mount_target`
  (`file_system_id`, `subnet_id`, `security_groups`) + `aws_s3files_access_point`
  (`file_system_id`, `root_directory { path = "/okf" }`, `posix_user { uid gid }`;
  exports `arn`). Mount that `arn` in the runtime's `filesystem_configuration`.
- Backend: `terraform { backend "s3" { bucket, key, region, use_lockfile = true } }`
  (DynamoDB locking is deprecated). Cross-stack reads use
  `data "terraform_remote_state"` (root-level outputs only).
- Everything, including S3 Vectors, AgentCore, and S3 Files, is native in
  `hashicorp/aws ~> 6.0` (v6.45+); the awscc provider isn't needed.
- The registry docs render client-side (empty to WebFetch), so read them from
  `raw.githubusercontent.com/hashicorp/terraform-provider-aws/main/website/docs/r/<name>.html.markdown`.
  Newer services like `s3files` may not have published docs yet — read the
  resource's Go schema under `internal/service/<svc>/`.

## 6. React + Cognito OIDC (`react-oidc-context` on `oidc-client-ts`)

- `AuthProvider` config: `authority="https://cognito-idp.<region>.amazonaws.com/
  <poolId>"`, `client_id`, `redirect_uri`, `response_type="code"` (PKCE S256),
  `scope="openid email profile"`, `onSigninCallback` (strip `?code&state`, or
  silent renew breaks), `userStore: new WebStorageStateStore({ store:
  window.localStorage })` (to survive SPA navigations).
- `useAuth()` returns `{isLoading, isAuthenticated, user, error, signinRedirect,
  signoutRedirect, removeUser}`. Tokens live at `auth.user?.id_token`,
  `.access_token`, `.profile`.
- Send the ID token to an API Gateway JWT authorizer with `audience=<client_id>`
  — the ID token's `aud` equals the client id, while the access token has no
  `aud`, only `client_id` and scope.
- Cognito's `/logout` is on the hosted-UI domain
  (`https://<domain>.auth.<region>.amazoncognito.com/logout?client_id=..&logout_uri=..`),
  not the issuer host.
- Vite MPA: `build.rollupOptions.input = { name: resolve(...html), ... }`.
