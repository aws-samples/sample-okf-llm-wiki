"""control_api — the OKF Control API Lambda.

A single Lambda sits behind an API Gateway HTTP API (v2, payload format 2.0)
with a Cognito JWT authorizer. The authorizer enforces auth, so the Lambda can
trust ``requestContext.authorizer.jwt.claims``. A tiny internal router
(``app.route``) dispatches on ``(method, route_key)`` to pure handler functions
(``handlers``) that take injected boto3 clients so they are unit-testable with
moto / fakes.

Layout:
* ``app`` — the router, response helpers, and the ``lambda_handler`` entry point
  that builds clients from env.
* ``handlers`` — pure functions, one per endpoint, taking clients + parsed args.
"""
