#!/usr/bin/env bash
# End-to-end deploy for OKF on AWS.
#
#   ./scripts/deploy.sh            # run the WHOLE pipeline (prompts for config once)
#   ./scripts/deploy.sh all        # same as above, non-interactive if config exists
#   ./scripts/deploy.sh <stage>    # run a single stage (durable|images|compute|cognito-urls|ui)
#   ./scripts/deploy.sh dev-env    # write ui/.env.local for local `npm run dev` (no deploy)
#   ./scripts/deploy.sh destroy    # tear everything down (preflight blocks on
#                                  #   leftovers TF can't remove; shows fix steps)
#   ./scripts/deploy.sh destroy --force  # auto-remediate the blockers, then destroy
#
# Requires: awscli (authenticated), terraform, docker (buildx for ARM64), node/npm, jq.
# Config (region, TF state bucket, Cognito admin user) is gathered once and saved
# to scripts/.deployment.config; image URIs + CloudFront URL flow between stages
# automatically. By default a small VPC is provisioned for the harvest runtime's
# S3 Files mount (skipped if you set TF_VAR_harvest_vpc_subnet_ids).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$ROOT/scripts/.deployment.config"

RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[0;33m'; NC='\033[0m'
info()  { echo -e "${BLUE}$*${NC}"; }
ok()    { echo -e "${GREEN}$*${NC}"; }
warn()  { echo -e "${YELLOW}$*${NC}"; }
die()   { echo -e "${RED}$*${NC}" >&2; exit 1; }

tf() { terraform -chdir="$1" "${@:2}"; }

# ---------------------------------------------------------------------------
# Config: load existing, or gather interactively and persist.
# ---------------------------------------------------------------------------

# NOTE: must return 0 even when the file is absent, or `set -e` aborts the whole
# script the first time it's called (before any config exists).
load_config() { if [ -f "$CONFIG" ]; then source "$CONFIG"; fi; }

save_config() {
  cat > "$CONFIG" <<EOF
AWS_REGION=$AWS_REGION
TF_STATE_BUCKET=$TF_STATE_BUCKET
NAME_PREFIX=$NAME_PREFIX
ADMIN_EMAIL=$ADMIN_EMAIL
ADMIN_USERNAME=$ADMIN_USERNAME
ADMIN_GIVEN_NAME=$ADMIN_GIVEN_NAME
ADMIN_FAMILY_NAME=$ADMIN_FAMILY_NAME
EOF
  ok "Saved config -> $CONFIG"
}

ask() { # ask "prompt" VAR "default"
  local prompt="$1" var="$2" def="${3:-}" val=""
  if [ -n "$def" ]; then read -r -p "$(echo -e "${BLUE}${prompt} [${def}]: ${NC}")" val; val="${val:-$def}"
  else while [ -z "$val" ]; do read -r -p "$(echo -e "${BLUE}${prompt}: ${NC}")" val; done; fi
  eval "$var=\"\$val\""
}

gather_config() {
  load_config
  if [ -n "${AWS_REGION:-}" ] && [ -n "${TF_STATE_BUCKET:-}" ] && [ -n "${ADMIN_EMAIL:-}" ]; then
    info "Using existing config (region=$AWS_REGION, state=$TF_STATE_BUCKET, admin=$ADMIN_EMAIL)."
    info "Delete $CONFIG to reconfigure."
    return
  fi
  info "First-time setup — a few questions:"
  ask "AWS region" AWS_REGION "${AWS_REGION:-us-east-1}"
  local acct; acct="$(aws sts get-caller-identity --query Account --output text)"
  ask "Terraform state bucket (created if missing)" TF_STATE_BUCKET "${TF_STATE_BUCKET:-okf-tfstate-${acct}}"
  ask "Resource name prefix" NAME_PREFIX "${NAME_PREFIX:-okf}"
  ask "Admin email (Cognito emails a temp password)" ADMIN_EMAIL "${ADMIN_EMAIL:-}"
  ask "Admin username" ADMIN_USERNAME "${ADMIN_USERNAME:-$ADMIN_EMAIL}"
  ask "Admin given name" ADMIN_GIVEN_NAME "${ADMIN_GIVEN_NAME:-OKF}"
  ask "Admin family name" ADMIN_FAMILY_NAME "${ADMIN_FAMILY_NAME:-Admin}"
  save_config
}

# ---------------------------------------------------------------------------
# Prereqs / helpers
# ---------------------------------------------------------------------------

require() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }
check_prereqs() {
  for t in aws terraform docker node npm jq; do require "$t"; done
  # Fail early and clearly if AWS creds aren't configured (otherwise the first
  # aws call dies mid-run with a cryptic error under `set -e`).
  aws sts get-caller-identity >/dev/null 2>&1 \
    || die "AWS credentials not found or expired. Run your login (e.g. 'aws sso login' / 'aws login') and retry."
}

ensure_state_bucket() {
  # Probe the bucket and CAPTURE the error, so we only try to create it on a
  # genuine 404 (Not Found). A head-bucket failure is ambiguous — expired creds
  # (ExpiredToken), a 403 (bucket exists but owned/denied), or a real 404 all
  # make the call non-zero. Blindly creating on ANY failure leads to a confusing
  # "BucketAlreadyExists" when the real cause is expired creds against a bucket
  # that already exists (and is ours). Distinguish them.
  local head_err
  if head_err="$(aws s3api head-bucket --bucket "$TF_STATE_BUCKET" 2>&1)"; then
    return 0  # exists + accessible — nothing to do
  fi
  if echo "$head_err" | grep -qiE "ExpiredToken|expired|InvalidClientTokenId|credential|Unable to locate|AccessDenied|Forbidden|\(403\)"; then
    die "Cannot reach state bucket s3://$TF_STATE_BUCKET — this looks like an AUTH problem, not a missing bucket:
  ${head_err}
Refresh your AWS credentials (e.g. 'aws sso login' / your Isengard login) and re-run. The bucket already exists from a prior deploy; it does NOT need recreating."
  fi
  # Only a true Not Found reaches here — create it.
  info "Creating Terraform state bucket s3://$TF_STATE_BUCKET ..."
  if [ "$AWS_REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$TF_STATE_BUCKET" --region "$AWS_REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$TF_STATE_BUCKET" --region "$AWS_REGION" \
      --create-bucket-configuration "LocationConstraint=$AWS_REGION" >/dev/null
  fi
  aws s3api put-bucket-versioning --bucket "$TF_STATE_BUCKET" \
    --versioning-configuration Status=Enabled
  ok "State bucket ready."
}

acct_id() { aws sts get-caller-identity --query Account --output text; }
ecr_base() { echo "$(acct_id).dkr.ecr.${AWS_REGION}.amazonaws.com"; }

ecr_login() {
  # Private ECR (our okf-* repos, in the deploy region) — for PUSH.
  aws ecr get-login-password --region "$AWS_REGION" \
    | docker login --username AWS --password-stdin "$(ecr_base)"

  # ECR PUBLIC (public.ecr.aws) — for PULLING the base image in the Dockerfiles.
  # This is REQUIRED, not optional: a stale/expired public.ecr.aws credential
  # left in ~/.docker/config.json makes the base-image pull 403 (Docker sends the
  # bad token instead of pulling anonymously). Re-authenticating overwrites it
  # with a fresh token. ECR Public tokens are issued ONLY from us-east-1,
  # regardless of the deploy region. We first drop any stale entry so the fresh
  # login is clean.
  docker logout public.ecr.aws >/dev/null 2>&1 || true
  aws ecr-public get-login-password --region us-east-1 \
    | docker login --username AWS --password-stdin public.ecr.aws \
    || die "Could not authenticate to ECR Public (public.ecr.aws). Needed to pull the base image. Check AWS creds / that 'aws ecr-public get-login-password --region us-east-1' works."
}
ensure_repo() {
  aws ecr describe-repositories --repository-names "$1" --region "$AWS_REGION" >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name "$1" --region "$AWS_REGION" >/dev/null
}

# Name of the dedicated buildx builder this script owns.
BUILDX_BUILDER="okf-arm64"

# Own a fresh ARM64 buildx builder rather than inheriting whatever ambient
# builder is active. The docker-container driver (needed to build+push linux/arm64
# from any host) runs BuildKit in its own container and caches registry auth at
# BOOT — so a stale builder can keep serving an expired public.ecr.aws token even
# after we re-login on the host, causing the base-image pull to 403. Recreating it
# guarantees BuildKit boots with the credentials ecr_login just refreshed.
ensure_builder() {
  require docker
  docker buildx rm "$BUILDX_BUILDER" >/dev/null 2>&1 || true
  docker buildx create --name "$BUILDX_BUILDER" --driver docker-container --bootstrap >/dev/null \
    || die "Could not create buildx builder '$BUILDX_BUILDER' (need docker buildx / docker-container driver)."
}

build_push() { # svc repo -> prints IMMUTABLE image uri (repo:<tag>)
  local svc="$1" repo="$2"
  ensure_repo "$repo"
  # Tag with a UNIQUE, IMMUTABLE tag (utc timestamp + short git sha if available)
  # AND :latest. Returning the unique tag is critical: Terraform's container_uri
  # is a plain string, so if we always passed :latest it would never change and
  # `terraform apply` would see no diff — the runtime would keep serving the OLD
  # image even after a fresh push. A unique tag forces the Create/UpdateAgentRuntime.
  local sha ts tag uri
  sha="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)"
  ts="$(date -u +%Y%m%d%H%M%S)"
  tag="${ts}-${sha}"
  uri="$(ecr_base)/$repo:${tag}"
  # CRITICAL: fail hard if the build/push fails. Without this check the function
  # would still echo a tag for an image that was never pushed — the config would
  # capture a phantom URI and `compute` would later die with "image does not
  # exist". buildx writes progress to stderr (>&2) so only the URI reaches stdout.
  if ! docker buildx build --builder "$BUILDX_BUILDER" --platform linux/arm64 \
      -f "$ROOT/services/$svc/Dockerfile" \
      -t "$uri" \
      -t "$(ecr_base)/$repo:latest" \
      --push "$ROOT/services" >&2; then
    die "docker build/push FAILED for $svc ($repo). No image was pushed — NOT updating image URIs (ecr_login refreshed public.ecr.aws + private ECR and ensure_builder recreated the buildx builder, so this is a real build error — check the log above)."
  fi
  echo "$uri"
}

# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

stage_durable() {
  ensure_state_bucket
  tf "$ROOT/infra/durable" init -reconfigure -input=false \
    -backend-config="bucket=$TF_STATE_BUCKET" -backend-config="region=$AWS_REGION"
  tf "$ROOT/infra/durable" apply -auto-approve -input=false \
    -var="region=$AWS_REGION" \
    -var="name_prefix=${NAME_PREFIX:-okf}" \
    -var="admin_email=${ADMIN_EMAIL:-}" \
    -var="admin_username=${ADMIN_USERNAME:-}" \
    -var="admin_given_name=${ADMIN_GIVEN_NAME:-OKF}" \
    -var="admin_family_name=${ADMIN_FAMILY_NAME:-Admin}"
  ok "Durable stack applied."
}

stage_images() {
  ecr_login       # refresh BOTH private ECR (push) + ECR Public (base-image pull)
  ensure_builder  # recreate the buildx builder so BuildKit boots with fresh auth
  HARVEST_URI="$(build_push harvest okf-harvest)"
  CONSUMPTION_URI="$(build_push consumption_mcp okf-consumption)"
  # Persist for the compute stage.
  grep -v -E '^(HARVEST_IMAGE_URI|CONSUMPTION_IMAGE_URI)=' "$CONFIG" > "$CONFIG.tmp" 2>/dev/null || true
  mv "$CONFIG.tmp" "$CONFIG" 2>/dev/null || true
  echo "HARVEST_IMAGE_URI=$HARVEST_URI" >> "$CONFIG"
  echo "CONSUMPTION_IMAGE_URI=$CONSUMPTION_URI" >> "$CONFIG"
  ok "Images pushed: $HARVEST_URI , $CONSUMPTION_URI"
}

# Verify an "<acct>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>" image tag really
# exists in ECR before Terraform points a runtime at it — turns the cryptic
# "UpdateAgentRuntime ValidationException: image does not exist" into a clear,
# early failure that tells you to (re)build images.
verify_ecr_image() {
  local uri="$1" repo tag
  [ -z "$uri" ] && return 0 # empty = runtime not managed; TF count() handles it
  repo="${uri##*/}"; repo="${repo%%:*}"
  tag="${uri##*:}"
  aws ecr describe-images --repository-name "$repo" --image-ids "imageTag=$tag" \
    --region "$AWS_REGION" >/dev/null 2>&1 \
    || die "Image not found in ECR: $uri
The config points compute at an image tag that isn't in the repository (a prior 'images' build likely failed). Run './scripts/deploy.sh images' to (re)build + push, then retry compute."
}

stage_compute() {
  load_config
  verify_ecr_image "${HARVEST_IMAGE_URI:-}"
  verify_ecr_image "${CONSUMPTION_IMAGE_URI:-}"
  "$ROOT/scripts/build_lambdas.sh"
  tf "$ROOT/infra/compute" init -reconfigure -input=false \
    -backend-config="bucket=$TF_STATE_BUCKET" -backend-config="region=$AWS_REGION"
  tf "$ROOT/infra/compute" apply -auto-approve -input=false \
    -var="region=$AWS_REGION" \
    -var="name_prefix=${NAME_PREFIX:-okf}" \
    -var="durable_state_bucket=$TF_STATE_BUCKET" \
    -var="harvest_image_uri=${HARVEST_IMAGE_URI:-}" \
    -var="consumption_image_uri=${CONSUMPTION_IMAGE_URI:-}"
  ok "Compute stack applied."
}

stage_cognito_urls() {
  # Feed the CloudFront URL into the Cognito app-client callback/logout URLs
  # (a durable re-apply, not a console edit).
  local cf; cf="$(tf "$ROOT/infra/compute" output -raw ui_cloudfront_domain)"
  tf "$ROOT/infra/durable" apply -auto-approve -input=false \
    -var="region=$AWS_REGION" \
    -var="name_prefix=${NAME_PREFIX:-okf}" \
    -var="admin_email=${ADMIN_EMAIL:-}" \
    -var="admin_username=${ADMIN_USERNAME:-}" \
    -var="admin_given_name=${ADMIN_GIVEN_NAME:-OKF}" \
    -var="admin_family_name=${ADMIN_FAMILY_NAME:-Admin}" \
    -var="ui_callback_urls=[\"${cf}/callback.html\",\"http://localhost:5173/callback.html\"]" \
    -var="ui_logout_urls=[\"${cf}/\",\"http://localhost:5173/\"]"
  ok "Cognito callback/logout URLs now include $cf"
}

stage_ui() {
  # Build-time env from compute outputs, build, sync to the UI bucket.
  eval "$(tf "$ROOT/infra/compute" output -json ui_env \
    | python3 -c 'import json,sys
for k,v in json.load(sys.stdin).items(): print(f"export {k}={v}")')"
  ( cd "$ROOT/ui" && npm ci && npm run build )
  local bucket; bucket="$(tf "$ROOT/infra/compute" output -raw ui_bucket)"

  # Cache strategy: the /assets/* files are content-hashed by Vite, so they are
  # safe to cache forever (immutable). The HTML entry points (index.html,
  # callback.html) are NOT hashed and reference the current asset hashes, so
  # they must NEVER be cached — otherwise a browser/CloudFront keeps serving a
  # stale index.html that points at asset hashes a prior `--delete` sync purged.
  # Those purged assets then 404 -> our SPA fallback rewrites them to
  # index.html (HTTP 200, text/html), so the browser receives HTML where it
  # expected CSS/JS and renders completely unstyled.
  #
  # Upload the immutable, hashed assets FIRST (long cache), then the HTML with
  # no-cache, so the HTML never points at assets that aren't up yet.
  aws s3 sync "$ROOT/ui/dist" "s3://$bucket" --delete \
    --exclude "*.html" \
    --cache-control "public, max-age=31536000, immutable"
  aws s3 sync "$ROOT/ui/dist" "s3://$bucket" \
    --exclude "*" --include "*.html" \
    --cache-control "no-cache" \
    --content-type "text/html"
  ok "UI synced to s3://$bucket"

  # Invalidate CloudFront so the edge serves the new HTML immediately instead of
  # a cached index.html that references now-deleted asset hashes. Non-fatal if
  # the distribution id can't be read (older stack) — warn and continue.
  local dist; dist="$(tf "$ROOT/infra/compute" output -raw ui_cloudfront_distribution_id 2>/dev/null || echo '')"
  if [ -n "$dist" ]; then
    info "Invalidating CloudFront distribution $dist ..."
    aws cloudfront create-invalidation --distribution-id "$dist" --paths "/*" >/dev/null
    ok "CloudFront invalidation created for $dist."
  else
    warn "No ui_cloudfront_distribution_id output — skipping CloudFront invalidation."
    warn "Re-apply the compute stack ('./scripts/deploy.sh compute') to add it."
  fi
}

stage_dev_env() {
  # Write ui/.env.local from the deployed compute stack's ui_env output so
  # `npm run dev` (Vite on :5173) talks to the real Cognito + Control API. This
  # is the local-development counterpart to stage_ui (which bakes the same env
  # into the production build). .env.local is git-ignored (*.local).
  #
  # Login/CORS already support localhost:
  #   - auth.js derives redirect_uri from window.location.origin, and the
  #     Cognito app client whitelists http://localhost:5173/callback.html (see
  #     stage_cognito_urls / ui_callback_urls).
  #   - the Control API's CORS allow_origins is "*".
  local envfile="$ROOT/ui/.env.local"
  tf "$ROOT/infra/compute" output -json ui_env \
    | python3 -c 'import json,sys
env = json.load(sys.stdin)
print("# Generated by ./scripts/deploy.sh dev-env — points local Vite at the")
print("# deployed Cognito + Control API. Git-ignored (*.local). Do not commit.")
for k, v in env.items():
    print(f"{k}={v}")' > "$envfile"
  ok "Wrote $envfile"
  info "Start the local dev server with:  cd ui && npm run dev   (http://localhost:5173)"
}

print_summary() {
  local cf; cf="$(tf "$ROOT/infra/compute" output -raw ui_cloudfront_domain 2>/dev/null || echo '')"
  echo ""
  ok "=========================================================="
  ok " OKF deployment complete."
  [ -n "$cf" ] && echo -e " Console (login):  ${BLUE}${cf}${NC}"
  echo -e " API endpoint:     ${BLUE}$(tf "$ROOT/infra/compute" output -raw control_api_endpoint 2>/dev/null || echo n/a)${NC}"
  if [ -n "${ADMIN_EMAIL:-}" ]; then
    echo -e " Admin user:       ${BLUE}${ADMIN_EMAIL}${NC}"
    warn " Check ${ADMIN_EMAIL} for a temporary password from no-reply@verificationemail.com."
  fi
  ok "=========================================================="
}

# ---------------------------------------------------------------------------
# Destroy preflight guardrail
# ---------------------------------------------------------------------------
# `terraform destroy` fails HARD (and confusingly) on two classes of leftover
# state that Terraform won't clean up on its own, because our buckets are
# versioned + force_destroy=false and the S3 File System owns service-managed
# resources:
#
#   1. An S3 File System still attached to the bundle bucket. It (a) refuses to
#      delete without --force-delete when it has data pending export
#      (ConflictException), (b) keeps agentic_ai mount-target ENIs in the
#      subnets that pin the harvest security group (DeleteSecurityGroup
#      DependencyViolation — the "Still destroying... 15m" hang), and (c) blocks
#      the durable bucket delete (PutBucketVersioning / DeleteBucket
#      BucketHasS3FileSystemAttached).
#   2. Non-empty VERSIONED buckets (bundles, ui). force_destroy=false means TF
#      won't empty them, so DeleteBucket returns BucketNotEmpty and
#      PutBucketVersioning returns 409.
#
# This preflight detects both BEFORE running terraform and blocks with the exact
# remediation, so the destroy never limps through the swallowed-error path we
# used to have. Re-run with `--force` to auto-remediate and proceed.

# Emit the Key/VersionId of every version + delete marker as compact JSON lines.
empty_versioned_bucket() { # bucket
  local b="$1" tmp del n
  tmp="$(mktemp -d)"
  info "Emptying versioned bucket s3://$b (all object versions + delete markers) ..."
  while :; do
    aws s3api list-object-versions --bucket "$b" --region "$AWS_REGION" \
      --max-keys 1000 --output json > "$tmp/lov.json" 2>/dev/null || break
    n="$(jq '((.Versions // []) + (.DeleteMarkers // [])) | length' "$tmp/lov.json")"
    [ "${n:-0}" -eq 0 ] && break
    jq '{Objects: [((.Versions // []) + (.DeleteMarkers // []))[] | {Key, VersionId}], Quiet: true}' \
      "$tmp/lov.json" > "$tmp/del.json"
    aws s3api delete-objects --bucket "$b" --region "$AWS_REGION" \
      --delete "file://$tmp/del.json" >/dev/null
  done
  rm -rf "$tmp"
  ok "Emptied s3://$b"
}

# Force-delete an S3 File System and WAIT for it to disappear — the wait matters
# because that is what releases the service-managed mount-target ENIs, which in
# turn unblocks the security group and the bundle bucket.
force_delete_s3_filesystem() { # fs-id
  local fs_id="$1" i
  warn "Force-deleting S3 File System $fs_id — this DISCARDS any data pending export to S3."
  aws s3files delete-file-system --file-system-id "$fs_id" --force-delete --region "$AWS_REGION" \
    || die "delete-file-system failed for $fs_id (see error above)."
  info "Waiting for $fs_id to finish deleting (releases mount-target ENIs) ..."
  for i in $(seq 1 60); do
    if ! aws s3files list-file-systems --region "$AWS_REGION" \
        --query "fileSystems[?fileSystemId=='$fs_id'].fileSystemId" --output text 2>/dev/null \
        | grep -q .; then
      ok "S3 File System $fs_id deleted."
      return 0
    fi
    sleep 10
  done
  warn "S3 File System $fs_id still deleting after ~10m; continuing — Terraform will retry."
}

bucket_nonempty() { # bucket -> 0 if it exists AND has any version/marker
  local b="$1" n
  aws s3api head-bucket --bucket "$b" >/dev/null 2>&1 || return 1  # gone/inaccessible
  n="$(aws s3api list-object-versions --bucket "$b" --region "$AWS_REGION" \
        --max-keys 1 --output json 2>/dev/null \
        | jq '((.Versions // []) + (.DeleteMarkers // [])) | length' 2>/dev/null)"
  [ "${n:-0}" -ne 0 ]
}

destroy_preflight() {
  require aws; require jq
  aws sts get-caller-identity >/dev/null 2>&1 \
    || die "AWS credentials not found or expired. Log in (e.g. 'aws sso login') and retry."
  [ -n "${AWS_REGION:-}" ] || die "AWS_REGION not set — is $CONFIG present? Run a deploy first, or set AWS_REGION."

  local acct; acct="$(acct_id)"
  local prefix="${NAME_PREFIX:-okf}"
  local bundle_bucket="${TF_VAR_bundle_bucket_name:-${prefix}-bundles-${acct}}"
  local ui_bucket="${TF_VAR_ui_bucket_name:-${prefix}-ui-${acct}}"
  local bundle_arn="arn:aws:s3:::${bundle_bucket}"

  local -a blockers=() steps=() fs_ids=() full_buckets=()

  # 1. S3 File System(s) attached to the bundle bucket.
  local fs_out
  if fs_out="$(aws s3files list-file-systems --region "$AWS_REGION" \
        --query "fileSystems[?bucket=='${bundle_arn}'].fileSystemId" --output text 2>&1)"; then
    local id
    for id in $fs_out; do
      [ -z "$id" ] && continue
      fs_ids+=("$id")
      blockers+=("S3 File System $id is still attached to s3://$bundle_bucket (blocks the bucket delete, the harvest security group, and its mount-target ENIs).")
      steps+=("aws s3files delete-file-system --file-system-id $id --force-delete --region $AWS_REGION")
    done
  else
    # `aws s3files` unknown => CLI too old to check. Warn rather than silently pass.
    if echo "$fs_out" | grep -qiE "Invalid choice|argument command|not.*valid"; then
      warn "This AWS CLI can't check S3 Files (no 's3files' command). Cannot verify a file system isn't still attached to s3://$bundle_bucket — upgrade the AWS CLI if destroy fails with BucketHasS3FileSystemAttached."
    fi
  fi

  # 2. Non-empty versioned buckets.
  local b
  for b in "$bundle_bucket" "$ui_bucket"; do
    if bucket_nonempty "$b"; then
      full_buckets+=("$b")
      blockers+=("Bucket s3://$b is versioned and NOT empty (DeleteBucket → BucketNotEmpty; PutBucketVersioning → 409).")
      steps+=("./scripts/deploy.sh destroy --force   # empties s3://$b (all versions + delete markers)")
    fi
  done

  if [ "${#blockers[@]}" -eq 0 ]; then
    ok "Destroy preflight: no known blockers (no attached file system, buckets empty)."
    return 0
  fi

  echo ""
  warn "Destroy preflight found ${#blockers[@]} blocker(s) that would make 'terraform destroy' fail:"
  for b in "${blockers[@]}"; do echo -e "  ${RED}✗${NC} $b"; done
  echo ""

  if [ "${FORCE_CLEANUP:-0}" = "1" ]; then
    warn "--force given: auto-remediating the blockers above."
    local id
    for id in "${fs_ids[@]}"; do force_delete_s3_filesystem "$id"; done
    for b in "${full_buckets[@]}"; do empty_versioned_bucket "$b"; done
    ok "Preflight remediation complete — proceeding with destroy."
    return 0
  fi

  info "To resolve, either run these steps manually:"
  local s
  for s in "${steps[@]}"; do echo -e "  ${BLUE}\$${NC} $s"; done
  echo ""
  die "Destroy blocked. Re-run to auto-remediate + tear down:
  ${BLUE}./scripts/deploy.sh destroy --force${NC}"
}

stage_destroy() {
  [ "${1:-}" = "--force" ] && FORCE_CLEANUP=1
  load_config
  warn "Destroying compute then durable stacks (this deletes the bundle bucket + vectors)."
  read -r -p "Type 'destroy' to confirm: " c; [ "$c" = "destroy" ] || die "aborted"

  # Guardrail: check for (and optionally clear) the leftovers Terraform can't
  # remove itself, BEFORE running terraform — so destroy fails loudly here with
  # actionable steps instead of hanging then erroring deep in the apply.
  destroy_preflight

  if ! tf "$ROOT/infra/compute" destroy -auto-approve \
      -var="region=$AWS_REGION" -var="durable_state_bucket=$TF_STATE_BUCKET" \
      -var="harvest_image_uri=${HARVEST_IMAGE_URI:-}" -var="consumption_image_uri=${CONSUMPTION_IMAGE_URI:-}"; then
    die "compute destroy failed (see errors above). If it's a lingering S3 File System or a non-empty bucket, re-run: ./scripts/deploy.sh destroy --force"
  fi
  if ! tf "$ROOT/infra/durable" destroy -auto-approve -var="region=$AWS_REGION"; then
    die "durable destroy failed (see errors above)."
  fi
  ok "Destroyed."
}

run_all() {
  check_prereqs
  gather_config
  info "== 1/5 durable =="       ; stage_durable
  info "== 2/5 images =="        ; stage_images
  info "== 3/5 compute =="       ; stage_compute
  info "== 4/5 cognito-urls ==" ; stage_cognito_urls
  info "== 5/5 ui =="            ; stage_ui
  print_summary
}

STAGE="${1:-all}"
case "$STAGE" in
  all|"")        run_all ;;
  durable)       check_prereqs; gather_config; stage_durable ;;
  images)        check_prereqs; load_config; stage_images ;;
  compute)       check_prereqs; load_config; stage_compute ;;
  cognito-urls)  load_config; stage_cognito_urls ;;
  ui)            check_prereqs; load_config; stage_ui ;;
  dev-env)       check_prereqs; load_config; stage_dev_env ;;
  summary)       load_config; print_summary ;;
  destroy)       stage_destroy "${2:-}" ;;
  *)             die "unknown stage: $STAGE (use: all|durable|images|compute|cognito-urls|ui|dev-env|summary|destroy)" ;;
esac
