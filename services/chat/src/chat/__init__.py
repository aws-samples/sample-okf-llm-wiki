"""OKF chat agent: a LangGraph agent hosted on Bedrock AgentCore Runtime that
answers questions about the wiki, streamed to the UI as Sparky-style typed SSE
chunks (text / think / tool / end) — the browser owns the reader + rendering.

See docs/CHAT_AGENT.md for the design. Modules:

- ``config`` — ``ChatConfig.from_env`` (OKF_CHAT_* knobs) + model/effort
  resolution and catalog validation (delegates construction to
  ``okf_aws.model_factory``).
- ``tools`` — wrap the reused ``ConsumptionTools`` methods as LangChain tools,
  with optional per-run dataset-scope pre-binding.
- ``graph`` — ``build_graph`` (a ``create_agent`` react agent + DynamoDBSaver
  checkpointer).
- ``server`` — the raw FastAPI app (/invocations + /ping): CORS for the
  browser-direct call, per-user checkpoint namespacing, the ``type``-discriminated
  input envelope, and ``process_stream_data`` (LangGraph run -> typed chunks).
"""
