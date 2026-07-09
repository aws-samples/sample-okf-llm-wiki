"""Unit tests for the Code Interpreter sandbox wrapper (offline, fake client).

Exercises the wrapper's contract without the bedrock-agentcore SDK or AWS:
stream parsing, output truncation, .context upload codegen, session lifecycle,
concurrency serialization, and the graceful-degradation of build_sandbox().
"""

from __future__ import annotations

import base64
import threading

import pytest

from harvest import code_interpreter as ci


class FakeCIClient:
    """Records data-plane calls and returns canned executeCode streams."""

    def __init__(self, exec_response=None):
        self.started = []
        self.stopped = []
        self.invokes = []
        self._exec_response = exec_response
        # captured code from the LAST executeCode call
        self.last_code = ""

    def start_code_interpreter_session(self, **kwargs):
        self.started.append(kwargs)
        return {"sessionId": "sess-123"}

    def stop_code_interpreter_session(self, **kwargs):
        self.stopped.append(kwargs)
        return {}

    def invoke_code_interpreter(self, **kwargs):
        self.invokes.append(kwargs)
        self.last_code = kwargs["arguments"]["code"]
        if callable(self._exec_response):
            return self._exec_response(kwargs)
        if self._exec_response is not None:
            return self._exec_response
        # Default: echo an upload-success marker so _write_files is happy.
        return _stream_ok("OKF_UPLOADED 1")


def _stream_ok(stdout: str, stderr: str = "", is_error: bool = False) -> dict:
    return {
        "stream": [
            {
                "result": {
                    "isError": is_error,
                    "structuredContent": {"stdout": stdout, "stderr": stderr},
                }
            }
        ]
    }


def _stream_content(text: str) -> dict:
    """A stream that uses content[].text instead of structuredContent."""
    return {"stream": [{"result": {"content": [{"type": "text", "text": text}]}}]}


# -- lifecycle ----------------------------------------------------------------


def test_start_is_idempotent_and_returns_session_id():
    client = FakeCIClient()
    sandbox = ci.CodeSandbox(client, "interp-1")
    sid1 = sandbox.start()
    sid2 = sandbox.start()
    assert sid1 == sid2 == "sess-123"
    assert len(client.started) == 1  # second start() did not re-open
    assert client.started[0]["codeInterpreterIdentifier"] == "interp-1"


def test_context_manager_starts_and_stops():
    client = FakeCIClient()
    with ci.CodeSandbox(client, "interp-1") as sandbox:
        assert sandbox._session_id == "sess-123"
    assert client.stopped and client.stopped[0]["sessionId"] == "sess-123"


def test_stop_is_best_effort_on_error():
    client = FakeCIClient()
    sandbox = ci.CodeSandbox(client, "interp-1")
    sandbox.start()

    def _boom(**_k):
        raise RuntimeError("teardown boom")

    client.stop_code_interpreter_session = _boom
    sandbox.stop()  # must not raise
    assert sandbox._session_id is None


# -- execution / stream parsing ----------------------------------------------


def test_run_code_parses_structured_content():
    client = FakeCIClient(exec_response=_stream_ok("hello", stderr="warn"))
    sandbox = ci.CodeSandbox(client, "interp-1")
    sandbox.start()
    out = sandbox.run_code("print('hello')")
    assert out["stdout"] == "hello"
    assert out["stderr"] == "warn"
    assert out["is_error"] is False


def test_run_code_parses_content_text_fallback():
    client = FakeCIClient(exec_response=_stream_content("from content"))
    sandbox = ci.CodeSandbox(client, "interp-1")
    sandbox.start()
    out = sandbox.run_code("x=1")
    assert out["stdout"] == "from content"


def test_run_code_flags_error():
    resp = {
        "stream": [
            {
                "result": {
                    "isError": True,
                    "content": [{"type": "error", "text": "Traceback"}],
                }
            }
        ]
    }
    client = FakeCIClient(exec_response=resp)
    sandbox = ci.CodeSandbox(client, "interp-1")
    sandbox.start()
    out = sandbox.run_code("raise Exception()")
    assert out["is_error"] is True
    assert "Traceback" in out["stderr"]


def test_run_code_uses_clear_context_and_python():
    client = FakeCIClient(exec_response=_stream_ok("ok"))
    sandbox = ci.CodeSandbox(client, "interp-1")
    sandbox.start()
    sandbox.run_code("print(1)")
    args = client.invokes[-1]["arguments"]
    assert args["language"] == "python"
    assert args["clearContext"] is True


def test_run_code_starts_session_lazily():
    client = FakeCIClient(exec_response=_stream_ok("ok"))
    sandbox = ci.CodeSandbox(client, "interp-1")
    # never called start() explicitly
    sandbox.run_code("print(1)")
    assert client.started  # _execute opened the session


def test_output_is_truncated(monkeypatch):
    monkeypatch.setattr(ci, "_MAX_OUTPUT_CHARS", 10)
    client = FakeCIClient(exec_response=_stream_ok("x" * 100))
    sandbox = ci.CodeSandbox(client, "interp-1")
    sandbox.start()
    out = sandbox.run_code("print('x'*100)")
    assert out["stdout"].startswith("x" * 10)
    assert "truncated" in out["stdout"]
    assert len(out["stdout"]) < 100


def test_execute_serialized_under_lock():
    """Concurrent run_code calls must not overlap on the shared session."""
    overlaps = []
    active = {"n": 0}
    gate = threading.Lock()

    def _resp(_kwargs):
        with gate:
            active["n"] += 1
            if active["n"] > 1:
                overlaps.append(True)
        # simulate work
        threading.Event().wait(0.01)
        with gate:
            active["n"] -= 1
        return _stream_ok("ok")

    client = FakeCIClient(exec_response=_resp)
    sandbox = ci.CodeSandbox(client, "interp-1")
    sandbox.start()
    threads = [
        threading.Thread(target=sandbox.run_code, args=("print(1)",)) for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not overlaps  # the lock kept executes strictly serial


# -- .context upload ----------------------------------------------------------


def test_upload_context_no_dir_is_noop(tmp_path):
    client = FakeCIClient()
    sandbox = ci.CodeSandbox(client, "interp-1")
    sandbox.start()
    assert sandbox.upload_context(tmp_path) == []
    # only the (absent) upload — no executeCode invoked
    assert client.invokes == []


def test_upload_context_writes_files_via_generated_code(tmp_path):
    ctx = tmp_path / ".context"
    ctx.mkdir()
    (ctx / "spec.docx").write_bytes(b"\x00binary\xff")
    (ctx / "notes.md").write_text("hello")

    client = FakeCIClient()  # default response includes OKF_UPLOADED marker
    sandbox = ci.CodeSandbox(client, "interp-1")
    sandbox.start()
    names = sandbox.upload_context(tmp_path)

    assert sorted(names) == ["notes.md", "spec.docx"]
    code = client.last_code
    # The generated code decodes base64 and writes under the sandbox context dir.
    assert ci.SANDBOX_CONTEXT_DIR in code
    assert "base64.b64decode" in code
    # The docx bytes are embedded base64-encoded (binary preserved).
    assert base64.b64encode(b"\x00binary\xff").decode("ascii") in code


def test_upload_context_raises_when_marker_missing(tmp_path):
    ctx = tmp_path / ".context"
    ctx.mkdir()
    (ctx / "a.txt").write_text("x")
    client = FakeCIClient(exec_response=_stream_ok("", stderr="disk full"))
    sandbox = ci.CodeSandbox(client, "interp-1")
    sandbox.start()
    with pytest.raises(RuntimeError, match="upload failed"):
        sandbox.upload_context(tmp_path)


def test_upload_context_respects_byte_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "_MAX_UPLOAD_BYTES", 10)
    ctx = tmp_path / ".context"
    ctx.mkdir()
    (ctx / "a.txt").write_bytes(b"12345")  # under, included
    (ctx / "b.txt").write_bytes(b"67890abc")  # pushes total over 10 -> stops
    client = FakeCIClient()
    sandbox = ci.CodeSandbox(client, "interp-1")
    sandbox.start()
    names = sandbox.upload_context(tmp_path)
    assert "a.txt" in names
    assert "b.txt" not in names


# -- build_sandbox graceful degradation --------------------------------------


def test_build_sandbox_none_without_env(monkeypatch):
    monkeypatch.delenv("OKF_CODE_INTERPRETER_ID", raising=False)
    assert ci.build_sandbox() is None


def test_build_sandbox_none_on_client_error(monkeypatch):
    monkeypatch.setenv("OKF_CODE_INTERPRETER_ID", "interp-1")

    import boto3

    def _boom(*_a, **_k):
        raise RuntimeError("no boto")

    monkeypatch.setattr(boto3, "client", _boom)
    assert ci.build_sandbox() is None


# -- the LangChain tool the agent actually gets -------------------------------


def test_make_run_code_tool_delegates_to_sandbox():
    client = FakeCIClient(exec_response=_stream_ok("42"))
    sandbox = ci.CodeSandbox(client, "interp-1")
    sandbox.start()
    tool = ci.make_run_code_tool(sandbox)
    assert tool.name == "run_code"
    # Invoking the tool runs code on the sandbox and returns the bounded result.
    out = tool.invoke({"code": "print(6*7)"})
    assert out["stdout"] == "42"
    # The tool description steers the agent at the .context extraction use case.
    assert "/tmp/okf_context/" in tool.description
    assert "markitdown" in tool.description
    assert "network-isolated" in tool.description.lower()
