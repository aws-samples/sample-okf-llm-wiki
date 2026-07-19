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
    listDomains: () => request(token, "GET", "/domains"),
    // sourceType defaults to "glue" (the only supported source today). The
    // backend also accepts a bare glue_database for back-compat, but we send the
    // first-class `source` object so the data model is exercised end to end.
    setDomainMapping: (domain, dataset, glueDatabase, sourceType = "glue") =>
      request(token, "PUT", `/domains/${domain}/datasets/${dataset}`, {
        source: { type: sourceType, glue_database: glueDatabase },
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

    // Harvest. `model`/`effort` are the per-run picker selection (optional): when
    // omitted the backend uses its deploy-time default. The Control API validates
    // the pair against the model catalog and 400s an unknown model/effort.
    startHarvest: (dataDomain, dataset, mode = "full", model, effort) =>
      request(token, "POST", "/harvest", {
        data_domain: dataDomain,
        dataset,
        mode,
        ...(model ? { model } : {}),
        ...(effort ? { effort } : {}),
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
    readBundleFile: (domain, dataset, key) =>
      request(
        token,
        "GET",
        `/bundle/${domain}/${dataset}/file?key=${encodeURIComponent(key)}`
      ),
    bundleGraph: (domain, dataset) =>
      request(token, "GET", `/bundle/${domain}/${dataset}/graph`),

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
    // {status:"complete", skipped:true}).
    runAnnotationHarvest: (domain, dataset) =>
      request(
        token,
        "POST",
        `/harvest/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}/annotations/run`
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

    // Recursive-improvement benchmark. GET/PUT the dataset's saved settings
    // ({enabled, questions_key, max_iterations}); the PUT is validated + clamped
    // server-side (400 on a bad value). The stop target is FIXED (judge accuracy
    // >= 90%), so it is not a setting. The CSV (question,gold_sql) uploads via a
    // SEPARATE presign that pins an OFF-MOUNT key (benchmark/<d>/<ds>/questions.csv,
    // NOT under okf/) so the gold is unreadable by the harvest agent — see
    // docs/CONVENTIONS.md and docs/BENCHMARK_GUIDE.md.
    getBenchmarkSettings: (domain, dataset) =>
      request(
        token,
        "GET",
        `/benchmark/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}`
      ),
    setBenchmarkSettings: (domain, dataset, settings) =>
      request(
        token,
        "PUT",
        `/benchmark/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}`,
        settings
      ),
    presignBenchmarkUpload: (domain, dataset, contentType) =>
      request(
        token,
        "POST",
        `/benchmark/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}/presign`,
        { content_type: contentType }
      ),
    // Parse the uploaded CSV with the SAME parser the harvest runtime uses and
    // report {uploaded, valid, count, total_in_csv, dropped, capped, error} — so
    // the UI shows the exact question count a harvest would benchmark, and flags a
    // bad format before the user relies on it.
    inspectBenchmarkQuestions: (domain, dataset) =>
      request(
        token,
        "GET",
        `/benchmark/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}/questions`
      ),
    // One benchmark round's per-question review (all buckets, with gold + predicted
    // SQL). Off-mount S3 read behind the Cognito-authed API — this gold-carrying
    // detail is NEVER exposed to the harvest agent, only to the human here. 404 if
    // the round hasn't persisted a review. session = runtime_session_id from the
    // harvest feed; iteration = 0-based round index.
    getBenchmarkReview: (domain, dataset, session, iteration) =>
      request(
        token,
        "GET",
        `/benchmark/${encodeURIComponent(domain)}/${encodeURIComponent(dataset)}/reviews/${encodeURIComponent(session)}/${encodeURIComponent(iteration)}`
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
