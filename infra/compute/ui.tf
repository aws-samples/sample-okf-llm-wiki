# UI hosting: private S3 bucket for the built React SPA, fronted by CloudFront
# with Origin Access Control. With OAC, S3 returns 403 for missing keys, so SPA
# routing needs custom error responses for BOTH 403 and 404 -> 200 /index.html.

resource "aws_s3_bucket" "ui" {
  bucket = local.ui_bucket
  tags   = var.tags
}

resource "aws_s3_bucket_public_access_block" "ui" {
  bucket                  = aws_s3_bucket.ui.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Disable ACLs entirely (threat #52): with BucketOwnerEnforced there is no ACL
# surface to misconfigure into public-read, so OAC is the ONLY read path.
resource "aws_s3_bucket_ownership_controls" "ui" {
  bucket = aws_s3_bucket.ui.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Versioning (threat #45): a compromised CI/deploy role that overwrites the SPA
# bundle with malicious JS cannot silently erase history — prior good versions
# remain for rollback and forensic diff. (Signing/SRI of the artifacts is the
# stronger, out-of-repo follow-up; versioning is the in-repo backstop.)
resource "aws_s3_bucket_versioning" "ui" {
  bucket = aws_s3_bucket.ui.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_cloudfront_origin_access_control" "ui" {
  name                              = "${var.name_prefix}-ui-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# AWS-managed CachingOptimized policy. NOTE (threat #53, cache-key abuse): this
# managed policy does NOT include query strings in the cache key and does NOT
# forward them to the origin, so a flood of unique-query-string requests all
# collapse to a single cached object rather than stampeding the S3 origin — the
# cache-key-abuse vector is already neutralized by this policy choice.
data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

# --- WAFv2 web ACL for the CDN (threat #50: volumetric / bot floods) ----------
# CLOUDFRONT-scope web ACLs MUST live in us-east-1 — created via the us_east_1
# provider alias regardless of the stack's region. Managed common rules + IP
# reputation + a per-IP rate cap. Gated by var.enable_waf.
resource "aws_wafv2_web_acl" "ui" {
  count    = var.enable_waf ? 1 : 0
  provider = aws.us_east_1
  name     = "${var.name_prefix}-ui-waf"
  scope    = "CLOUDFRONT"

  default_action {
    allow {}
  }

  rule {
    name     = "AWSManagedCommonRuleSet"
    priority = 1
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name_prefix}-ui-common"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "AWSManagedIpReputation"
    priority = 2
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesAmazonIpReputationList"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name_prefix}-ui-iprep"
      sampled_requests_enabled   = true
    }
  }

  # KnownBadInputs includes the Log4j2 JNDI-lookup mitigation (CVE-2021-44228 /
  # log4jshell — CKV_AWS_192) plus other known-malicious request patterns.
  rule {
    name     = "AWSManagedKnownBadInputs"
    priority = 3
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name_prefix}-ui-badinputs"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "RateLimitPerIp"
    priority = 4
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit              = var.waf_rate_limit_per_5min
        aggregate_key_type = "IP"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name_prefix}-ui-ratelimit"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.name_prefix}-ui-waf"
    sampled_requests_enabled   = true
  }

  tags = var.tags
}

# --- Security response headers for the SPA (threat #51) -----------------------
# HSTS, nosniff, referrer policy, frame-deny (clickjacking), and a CSP. The SPA
# build has NO inline <script> (only an external module bundle), so script-src
# 'self' is safe; React/shadcn inject runtime <style>, so style-src needs
# 'unsafe-inline'. connect-src must reach Cognito (IdP + hosted UI) and the API
# Gateway — allowed via the AWS API/Cognito wildcards (the SPA talks to standard
# AWS domains). Override the whole CSP with var.csp_override for a custom domain.
locals {
  csp_default = join("; ", [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self' data:",
    "connect-src 'self' https://*.amazonaws.com https://*.amazoncognito.com",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self' https://*.amazoncognito.com",
    "object-src 'none'",
  ])
  csp_value = var.csp_override != "" ? var.csp_override : local.csp_default
}

resource "aws_cloudfront_response_headers_policy" "ui" {
  name = "${var.name_prefix}-ui-security-headers"

  security_headers_config {
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      preload                    = true
      override                   = true
    }
    content_type_options {
      override = true
    }
    frame_options {
      frame_option = "DENY"
      override     = true
    }
    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }
    content_security_policy {
      content_security_policy = local.csp_value
      override                = true
    }
  }
}

# CloudFront access-log bucket (CKV_AWS_86). Standard logging delivers via the
# awslogsdelivery account, which writes with an ACL grant — so THIS bucket (logs
# only, never content) must keep ACLs enabled (BucketOwnerPreferred), unlike the
# UI content bucket which enforces BucketOwnerEnforced. Kept private via the PAB;
# logs expire after 90 days. A per-request audit trail for the CDN (who fetched
# what, cache hit/miss, edge status) to investigate abuse or a WAF bypass.
resource "aws_s3_bucket" "cf_logs" {
  bucket        = "${var.name_prefix}-cf-logs-${local.account_id}"
  force_destroy = true # disposable access logs; safe to empty on destroy
  tags          = var.tags
}

resource "aws_s3_bucket_public_access_block" "cf_logs" {
  bucket                  = aws_s3_bucket.cf_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ACLs enabled (ObjectWriter) so CloudFront's log-delivery account can write and
# own the delivered objects. This is REQUIRED for CloudFront standard logging and
# is why the log bucket cannot use the UI bucket's BucketOwnerEnforced setting.
resource "aws_s3_bucket_ownership_controls" "cf_logs" {
  bucket = aws_s3_bucket.cf_logs.id
  rule {
    object_ownership = "ObjectWriter"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "cf_logs" {
  bucket = aws_s3_bucket.cf_logs.id
  rule {
    id     = "expire-cf-logs"
    status = "Enabled"
    filter {}
    expiration {
      days = 90
    }
  }
}

resource "aws_cloudfront_distribution" "ui" {
  # checkov:skip=CKV_AWS_174:With the default *.cloudfront.net certificate, CloudFront pins its own modern SNI/TLS support and IGNORES minimum_protocol_version — the field only takes effect once a custom ACM cert + domain (aliases) is wired in. See the viewer_certificate block. Not a runtime TLS gap.
  enabled             = true
  default_root_object = "index.html"
  comment             = "${var.name_prefix} OKF UI"
  price_class         = "PriceClass_100"

  # Per-request CDN access logging (CKV_AWS_86) to the dedicated logs bucket.
  logging_config {
    bucket          = aws_s3_bucket.cf_logs.bucket_domain_name
    include_cookies = false
    prefix          = "cloudfront/"
  }

  # Volumetric / bot protection (threat #50). null when var.enable_waf = false.
  web_acl_id = var.enable_waf ? aws_wafv2_web_acl.ui[0].arn : null

  origin {
    domain_name              = aws_s3_bucket.ui.bucket_regional_domain_name
    origin_id                = "ui-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.ui.id
  }

  default_cache_behavior {
    target_origin_id           = "ui-s3"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.ui.id # CSP/HSTS (threat #51)
  }

  # SPA fallback (OAC S3 -> 403 on miss; handle 404 too).
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }
  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # nosemgrep: terraform.aws.security.aws-insecure-cloudfront-distribution-tls-version
  viewer_certificate {
    # Default *.cloudfront.net cert. NOTE (threat #51 TLS floor): with the default
    # cert CloudFront pins its own SNI/TLS support and IGNORES
    # minimum_protocol_version — a modern TLS floor (TLSv1.2_2021) only takes
    # effect once a CUSTOM ACM cert (acm_certificate_arn + a custom domain via
    # aliases) is configured. Wire that in for a production custom domain.
    # (The default cert already negotiates TLSv1.2+ at the edge; the flagged
    # min-version field is simply inert until a custom cert is attached.)
    cloudfront_default_certificate = true
  }

  tags = var.tags
}

# Bucket policy: allow only this CloudFront distribution (via OAC) to read.
data "aws_iam_policy_document" "ui_bucket" {
  statement {
    sid       = "AllowCloudFrontOAC"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.ui.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.ui.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "ui" {
  bucket = aws_s3_bucket.ui.id
  policy = data.aws_iam_policy_document.ui_bucket.json
}

# CORS for the BUNDLE bucket (durable stack). Context-doc uploads go straight
# from the browser to S3 via a presigned PUT (see control_api.presign_context_
# upload + ui uploadToPresigned). A cross-origin PUT with a Content-Type header
# triggers an OPTIONS preflight, which S3 answers only if the bucket has a CORS
# rule allowing that origin — otherwise the browser sees a 403 on OPTIONS and
# the upload never starts. Lives in the compute stack (not durable) because the
# allowed origin is the CloudFront domain, which is created here. Origins match
# the Cognito callback URLs (prod CloudFront + localhost dev); see deploy.sh.
resource "aws_s3_bucket_cors_configuration" "bundles" {
  bucket = local.d.bundle_bucket

  cors_rule {
    # POST: context-doc uploads use a presigned POST (multipart form) so S3 can
    # enforce a content-length-range size cap (threat #42). PUT/GET/HEAD retained
    # for compatibility. A cross-origin POST triggers an OPTIONS preflight S3 only
    # answers if POST is allowed here.
    allowed_methods = ["GET", "PUT", "POST", "HEAD"]
    allowed_origins = [
      "https://${aws_cloudfront_distribution.ui.domain_name}",
      "http://localhost:5173",
    ]
    # The preflight advertises the headers the upload will send (Content-Type);
    # "*" covers Content-Type plus any the signer includes. ExposeHeaders lets the
    # app read the returned ETag if it ever needs to.
    allowed_headers = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}
