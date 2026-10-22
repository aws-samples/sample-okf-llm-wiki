"""PlaywrightRenderer's thread affinity — no browser needed: the browser
touches (_render/_close) are stubbed to record which thread ran them."""

from __future__ import annotations

import threading

from canvas.render import PlaywrightRenderer


class _Recording(PlaywrightRenderer):
    def __init__(self, tmp_path):
        super().__init__(harness_path=str(tmp_path / "harness.html"))
        self.threads: list[tuple[str, int]] = []

    def _render(self, **kwargs):
        self.threads.append(("render", threading.get_ident()))
        return {"rendered": True}

    def _close(self):
        self.threads.append(("close", threading.get_ident()))


def _in_thread(fn):
    t = threading.Thread(target=fn)
    t.start()
    t.join()


def test_renders_and_close_from_different_threads_share_one_worker(tmp_path):
    # ToolNode runs each sync tool call on a FRESH thread and the runner closes
    # from yet another — Playwright's sync API only works on the thread that
    # started it, so every touch must land on the renderer's own worker.
    r = _Recording(tmp_path)
    kw = {"config": {}, "results": {}, "dials": {}}
    _in_thread(lambda: r.render(**kw))
    _in_thread(lambda: r.render(**kw))
    _in_thread(r.close)
    idents = {ident for _, ident in r.threads}
    assert [op for op, _ in r.threads] == ["render", "render", "close"]
    assert len(idents) == 1 and threading.get_ident() not in idents


def test_close_retires_the_worker_and_a_later_render_starts_fresh(tmp_path):
    r = _Recording(tmp_path)
    kw = {"config": {}, "results": {}, "dials": {}}
    r.render(**kw)
    r.close()
    r.close()  # idempotent
    assert r._worker is None
    assert r.render(**kw) == {"rendered": True}
    r.close()
