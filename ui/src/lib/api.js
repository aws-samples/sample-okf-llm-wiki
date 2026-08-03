// Thin Control API client. Every call attaches the Cognito ID token as a bearer
// (the API Gateway JWT authorizer is configured with audience = the app client
// id, which matches the ID token's `aud` claim). Pass the token from useAuth().

const BASE = import.meta.env.VITE_API_BASE_URL || ""

async function request(token, method, path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: {
      "content-type": "application/json",
      ...(token ? { authorization: `Bearer ${token}` } : {}),
    },
    body: body != null ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    let detail = ""
    try {
      detail = (await res.json()).error || ""
    } catch {
      detail = await res.text().catch(() => "")
    }
    throw new Error(
      `${method} ${path} -> ${res.status}${detail ? `: ${detail}` : ""}`
    )
  }
  const ct = res.headers.get("content-type") || ""
  return ct.includes("application/json") ? res.json() : res.text()
}

// Fetch a large benchmark artifact from its presigned S3 URL. The auth lives
// in the URL's signature (vended inside a Cognito-authed response, minutes of
// validity) — no bearer header, and an explicitly anonymous request so no
// stray credentials ride along.
async function fetchPresignedJson(url, what) {
  const res = await fetch(url, { credentials: "omit" })
  if (!res.ok) {
    throw new Error(`could not fetch the ${what} document (${res.status})`)
  }
  return res.json()
}

// Build an API bound to one token. Components call useApi(token) once.
export function makeApi(token) {
  return {
    // Declared domains (first-class entities)
    listDeclaredDomains: () => request(token, "GET", "/domain-defs"),
    getDeclaredDomain: (domain) =>
      request(token, "GET", `/domain-defs/${encodeURIComponent(domain)}`),
    declareDomain: (domain, description, context) =>
      request(token, "PUT", `/domain-defs/${encodeURIComponent(domain)}`, {
        description,
        context,
      }),
    // PUT /domain-defs is an upsert: re-PUTting an existing domain edits its
    // description/context (created_at is preserved, updated_at is bumped, and
    // the domain concept doc is re-materialised). Same call as declare; named
    // separately so edit call sites read as edits.
    updateDomain: (domain, description, context) =>
      request(token, "PUT", `/domain-defs/${encodeURIComponent(domain)}`, {
        description,
        context,
      }),
    deleteDeclaredDomain: (domain) =>
      request(token, "DELETE", `/domain-defs/${encodeURIComponent(domain)}`),

    // Glue + domain mapping
    listGlueDatabases: () => request(token, "GET", "/glue/databases"),
    // Redshift source pickers: list clusters/workgroups, then databases within a
    // chosen target (needs the secret that authenticates to it).
    listRedshiftClusters: () => request(token, "GET", "/redshift/clusters"),
    listRedshiftDatabases: ({ kind, id, secretArn, database }) => {
      const key = kind === "cluster" ? "cluster" : "workgroup"
      const qs = new URLSearchParams({ [key]: id, secret_arn: secretArn })
      // Bootstrap DB ListDatabases connects to first (a cluster's DBName hint
      // from /redshift/clusters); the backend falls back to "dev".
      if (database) qs.set("database", database)
      return request(token, "GET", `/redshift/databases?${qs.toString()}`)
    },
    listDomains: () => request(token, "GET", "/domains"),
    // `source` is the first-class source descriptor ({type, ...config}) — e.g.
    // {type:"glue", glue_database} or {type:"redshift", redshift_database}. The
    // caller builds the right shape per source type; the backend validates it.
    setDomainMapping: (domain, dataset, source) =>
      request(token, "PUT", `/domains/${domain}/datasets/${dataset}`, {
        source,
      }),
    deleteDomainMapping: (domain, dataset) =>
      request(token, "DELETE", `/domains/${domain}/datasets/${dataset}`),

    // MCP machine credentials (Cognito M2M app clients)
    listCredentials: () => request(token, "GET", "/credentials"),
    createCredential: (name, createdBy) =>
      request(token, "POST", "/credentials", {
        name,
        ...(createdBy ? { created_by: createdBy } : {}),
      }),
    deleteCredential: (clientId) =>
      request(token, "DELETE", `/credentials/${encodeURIComponent(clientId)}`),

    // Context docs
    listContext: (domain, dataset) =>
      request(token, "GET", `/context/${domain}/${dataset}`),
    presignUpload: (domain, dataset, filename, contentType) =>
      request(token, "POST", `/context/${domain}/${dataset}/presign`, {
        filename,
        content_type: contentType,
      }),
    deleteContext: (domain, dataset, filename) =>
      request(token, "DELETE", `/context/${domain}/${dataset}/${filename}`),

    // Harvest. `model`/`effort` are the per-run picker selection for the
    // HARVESTER (supervisor); `subagentModel`/`subagentEffort` the separate
    // selection for its SUB-AGENTS (authors/reviewers/benchmark). All optional:
    // omitted model → the backend's deploy-time default; omitted subagent pair →
    // the sub-agents run on the harvester's config. The Control API validates
    // each pair against the model catalog and 400s an unknown model/effort.
    // The trailing options object carries mode-specific extras: `target`
    // ({ dataDomain, dataset }) is REQUIRED for mode "cross" — the counterpart
    // dataset the run documents relationships against (validated server-side:
    // registered, glue-backed, bundle ready).
    startHarvest: (
      dataDomain,
      dataset,
      mode = "full",
      model,
      effort,
      subagentModel,
      subagentEffort,
      reviewerModel,
      reviewerEffort,
      { target } = {}
    ) =>
      request(token, "POST", "/harvest", {
        data_domain: dataDomain,
        dataset,
        mode,
        ...(model ? { model } : {}),
        ...(effort ? { effort } : {}),
        ...(subagentModel ? { subagent_model: subagentModel } : {}),
        ...(subagentEffort ? { subagent_effort: subagentEffort } : {}),
        ...(reviewerModel ? { reviewer_model: reviewerModel } : {}),
        ...(reviewerEffort ? { reviewer_effort: reviewerEffort } : {}),
        ...(target
          ? {
              target_data_domain: target.dataDomain,
              target_dataset: target.dataset,
            }
          : {}),
      }),
    harvestStatus: (domain, dataset) =>
      request(token, "GET", `/harvest/${domain}/${dataset}`),
    // Cancel an in-flight harvest: stops the AgentCore runtime session and
    // frees the per-dataset lease (marks the status row `cancelled`). 409 if the
    // harvest already reached a terminal state; 404 if there's no harvest row.
    cancelHarvest: (domain, dataset) =>
      request(
        token,
        "POST",
        `/harvest/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}/cancel`
      ),
    // Live step feed for a running harvest. Two cursors, both echoed back and
    // fed to the next poll: `since` = highest seq seen (exact dedup); `sinceTs` =
    // highest CloudWatch event timestamp in ms (bounds the server-side scan
    // window so each poll is cheap). Both 0 on first load → the server backfills
    // the whole current run from its start. Returns {events, next, next_ts, done}.
    harvestEvents: (domain, dataset, since = 0, sinceTs = 0) =>
      request(
        token,
        "GET",
        `/harvest/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}/events?since=${since}&since_ts=${sinceTs}`
      ),

    // Bundle browsing
    listBundle: (domain, dataset) =>
      request(token, "GET", `/bundle/${domain}/${dataset}`),
    // `version` (optional) reads a specific S3 object version — the rich diff
    // view fetches both sides of a file this way (works under a delete marker,
    // so a removed file's old content stays readable).
    readBundleFile: (domain, dataset, key, version) =>
      request(
        token,
        "GET",
        `/bundle/${domain}/${dataset}/file?key=${encodeURIComponent(key)}` +
          (version ? `&version=${encodeURIComponent(version)}` : "")
      ),
    bundleGraph: (domain, dataset) =>
      request(token, "GET", `/bundle/${domain}/${dataset}/graph`),

    // Bundle version history (reconstructed server-side from S3 object
    // versions; a version = one completed harvest/repromote). Diff selectors
    // are marker version_ids from listBundleVersions; both optional (default:
    // previous -> current). `to` may be the sentinel "live" to inspect what an
    // interrupted harvest half-wrote.
    listBundleVersions: (domain, dataset) =>
      request(token, "GET", `/bundle/${domain}/${dataset}/versions`),
    bundleDiff: (domain, dataset, from, to) => {
      const qs = new URLSearchParams()
      if (from) qs.set("from", from)
      if (to) qs.set("to", to)
      const q = qs.toString()
      return request(
        token,
        "GET",
        `/bundle/${domain}/${dataset}/diff${q ? `?${q}` : ""}`
      )
    },
    // Make an older version the new head (append-only restore; 409 while a
    // harvest holds the dataset lease). Poll repromoteStatus until
    // state === "converged" — the vector index serves the promoted content
    // only then. stalled_lease/stalled + can_retry mean re-POST repromote.
    repromote: (domain, dataset, versionId) =>
      request(token, "POST", `/bundle/${domain}/${dataset}/repromote`, {
        version_id: versionId,
      }),
    repromoteStatus: (domain, dataset) =>
      request(token, "GET", `/bundle/${domain}/${dataset}/repromote`),

    // Guardrails (policy checks) — the Guardrails page (internal name
    // "reasoning"). Always-on per dataset: guardrails author automatically
    // when the wiki changes (harvest, increment, restore); sync queues a
    // manual (re)build — the first authoring for datasets predating the
    // feature, and the fail-safe when the wiki moved and the automatic
    // trigger was missed. (The GET /reasoning/{d}/{ds}/document endpoint
    // still exists server-side for raw policies.yaml access; the page no
    // longer embeds a viewer.)
    getReasoning: (domain, dataset) =>
      request(token, "GET", `/reasoning/${domain}/${dataset}`),
    triggerReasoningSync: (domain, dataset) =>
      request(token, "POST", `/reasoning/${domain}/${dataset}/sync`),

    // Annotations (user-scoped feedback on concept docs). All calls are scoped
    // server-side to the caller's Cognito sub — you only ever see/act on your
    // own. `concept` (a slash path like tables/races) rides in the query string
    // for list/delete since it can't be a path segment.
    listAnnotations: (domain, dataset, concept) =>
      request(
        token,
        "GET",
        `/annotations/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}` +
          (concept ? `?concept=${encodeURIComponent(concept)}` : "")
      ),
    // anchor = { quote, prefix, suffix, block_line } captured from the selection.
    createAnnotation: (domain, dataset, conceptId, note, anchor = {}) =>
      request(
        token,
        "POST",
        `/annotations/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}`,
        {
          concept_id: conceptId,
          note,
          quote: anchor.quote,
          ...(anchor.prefix ? { prefix: anchor.prefix } : {}),
          ...(anchor.suffix ? { suffix: anchor.suffix } : {}),
          ...(anchor.block_line != null
            ? { block_line: anchor.block_line }
            : {}),
        }
      ),
    deleteAnnotation: (domain, dataset, conceptId, annotationId) =>
      request(
        token,
        "DELETE",
        `/annotations/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}/` +
          `${encodeURIComponent(annotationId)}?concept=${encodeURIComponent(conceptId)}`
      ),
    // Run the caller's open annotations through an annotation-mode re-harvest.
    // The server takes the lease, sweeps orphans, and invokes if some live
    // annotations remain OR the dataset guidance is dirty (else returns
    // {status:"complete", skipped:true}). `scope` (optional, "dataset"|"cross")
    // narrows the run to the dataset's own docs vs its external/ cross-dataset
    // docs; a cross-scoped run also ignores dataset guidance. `annotationIds`
    // (optional list) narrows further to an explicit selection — the picker
    // modal's partial apply. Omitted = every in-scope note. `crossTarget`
    // ("<domain>/<dataset>", cross scope only) names the pair the run verifies
    // against, so its Glue DB is granted even on a general-notes-only run.
    // The model/effort triple mirrors startHarvest's — applying annotations is
    // a harvest like any other, so it honors the SAME picker selection a full
    // harvest would (was previously dropped, always running on the runtime's
    // deploy-time default regardless of what the user had picked).
    runAnnotationHarvest: (
      domain,
      dataset,
      scope,
      annotationIds,
      crossTarget,
      model,
      effort,
      subagentModel,
      subagentEffort,
      reviewerModel,
      reviewerEffort
    ) =>
      request(
        token,
        "POST",
        `/harvest/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}/annotations/run`,
        {
          ...(scope ? { scope } : {}),
          ...(annotationIds ? { annotation_ids: annotationIds } : {}),
          ...(crossTarget ? { cross_target: crossTarget } : {}),
          ...(model ? { model } : {}),
          ...(effort ? { effort } : {}),
          ...(subagentModel ? { subagent_model: subagentModel } : {}),
          ...(subagentEffort ? { subagent_effort: subagentEffort } : {}),
          ...(reviewerModel ? { reviewer_model: reviewerModel } : {}),
          ...(reviewerEffort ? { reviewer_effort: reviewerEffort } : {}),
        }
      ),

    // Dataset guidance: shared, persistent authoring instructions that steer every
    // harvest of the dataset. GET returns {guidance, guidance_updated_at,
    // guidance_applied_version, guidance_dirty}; PUT sets/clears it (bumps the
    // version → dirty until the next successful harvest applies it).
    getDatasetGuidance: (domain, dataset) =>
      request(
        token,
        "GET",
        `/guidance/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}`
      ),
    setDatasetGuidance: (domain, dataset, guidance) =>
      request(
        token,
        "PUT",
        `/guidance/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}`,
        { guidance }
      ),

    // Benchmark Studio. The question CSV (one gold column per check) uploads via
    // a presign that pins an OFF-MOUNT key (benchmark/<d>/<ds>/questions.csv, NOT
    // under okf/) so the gold is unreadable by every LLM role; a RUN is a
    // standalone evaluation (no harvest, no lease) that persists a REPORT — see
    // docs/CONVENTIONS.md and docs/BENCHMARK_GUIDE.md.
    presignBenchmarkUpload: (domain, dataset, contentType) =>
      request(
        token,
        "POST",
        `/benchmark/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}/presign`,
        { content_type: contentType }
      ),
    // Parse the uploaded CSV with the SAME parser the benchmark runtime uses and
    // report {uploaded, valid, count, check_counts, dropped, capped, error} — so
    // the UI shows exactly what each check would grade, and flags a bad format
    // at upload.
    inspectBenchmarkQuestions: (domain, dataset) =>
      request(
        token,
        "GET",
        `/benchmark/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}/questions`
      ),
    // Start a run: {checks, runs, solver_model?, solver_effort?, judge_model?,
    // judge_effort?, version_id?}. Validated server-side (models against the
    // harvest catalog; the CSV must carry participants for an enabled check).
    // Returns {report_id, status:"queued", ...} — the list poller takes it from
    // there.
    startBenchmarkRun: (domain, dataset, config) =>
      request(
        token,
        "POST",
        `/benchmark/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}/runs`,
        config
      ),
    // Every report row for the dataset, newest first — flat index rows: status,
    // config summary, live progress stamps (phase/check/run/current/total), and
    // headline KPIs once complete. The full document is a separate fetch.
    listBenchmarkReports: (domain, dataset) =>
      request(
        token,
        "GET",
        `/benchmark/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}/runs`
      ),
    // One report: {row, report} — report is null until the run completes. A
    // document too large to ride the Lambda response arrives as report_url (a
    // short-lived presigned S3 GET); follow it here so callers always see the
    // same {row, report} shape.
    getBenchmarkReport: async (domain, dataset, reportId) => {
      const res = await request(
        token,
        "GET",
        `/benchmark/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}/runs/${encodeURIComponent(reportId)}`
      )
      if (res && !res.report && res.report_url) {
        res.report = await fetchPresignedJson(res.report_url, "report")
      }
      return res
    },
    // The report's solver traces (per failed question/check/run: reasoning, tool
    // calls, files read). Large — fetched lazily when a human opens a row's
    // steps, and routinely past the Lambda response cap, in which case the
    // handler answers {traces_url} and the document is fetched from S3 here.
    getBenchmarkReportTraces: async (domain, dataset, reportId) => {
      const res = await request(
        token,
        "GET",
        `/benchmark/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}/runs/${encodeURIComponent(reportId)}/traces`
      )
      if (res && !res.traces && res.traces_url) {
        return fetchPresignedJson(res.traces_url, "solver traces")
      }
      return res
    },
    deleteBenchmarkReport: (domain, dataset, reportId) =>
      request(
        token,
        "DELETE",
        `/benchmark/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}/runs/${encodeURIComponent(reportId)}`
      ),
    // Kick the annotation aggregator for a complete report (409 while one runs).
    // Progress lands on the row's agg_status; the final set in the report JSON.
    aggregateReportAnnotations: (domain, dataset, reportId) =>
      request(
        token,
        "POST",
        `/benchmark/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}/runs/${encodeURIComponent(reportId)}/aggregate`
      ),
    // Batch-create the user's SELECTED final annotations ([{note, concept_id?}])
    // with submitted_via="benchmark"; a normal annotation harvest then applies
    // them (runAnnotationHarvest with the returned ids).
    applyReportAnnotations: (domain, dataset, reportId, annotations) =>
      request(
        token,
        "POST",
        `/benchmark/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}/runs/${encodeURIComponent(reportId)}/annotations`,
        { annotations }
      ),

    // Chat conversations (the per-user sidebar list). The chat RUNTIME writes the
    // index rows; the Control API serves this read/rename/delete side, scoped to
    // the caller's Cognito sub. Rename is PUT (not PATCH) to match the API GW CORS
    // allow_methods (see control_api.tf / docs/CHAT_AGENT.md §11).
    listChatThreads: () => request(token, "GET", "/chat/threads"),
    renameChatThread: (threadId, title) =>
      request(token, "PUT", `/chat/threads/${encodeURIComponent(threadId)}`, {
        title,
      }),
    deleteChatThread: (threadId) =>
      request(token, "DELETE", `/chat/threads/${encodeURIComponent(threadId)}`),
  }
}

// Upload a file via a presigned S3 POST (no auth header — the policy is signed).
// The server-signed `fields` carry the pinned key + a content-length-range
// condition, so S3 rejects an oversized or misplaced upload itself (threat #42).
// The file MUST be the last form part. A too-large body comes back as 403
// (EntityTooLarge) — surface it as a size error.
export async function uploadToPresigned({ url, fields }, file) {
  const form = new FormData()
  for (const [k, v] of Object.entries(fields || {})) form.append(k, v)
  form.append("file", file)
  const res = await fetch(url, { method: "POST", body: form })
  if (!res.ok) {
    const body = await res.text().catch(() => "")
    if (res.status === 403 && /EntityTooLarge/i.test(body)) {
      throw new Error("file exceeds the upload size limit")
    }
    throw new Error(`upload failed: ${res.status}`)
  }
}
