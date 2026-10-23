"""The visual-inspection renderer behind the ``render_canvas`` tool.

The agent never touches the browser: this module serves the static harness
bundle (a Vite entry baked into the runtime image — real CanvasView tile
components, window-injected data, animations off) over a LOOPBACK HTTP server
— NOT file:// : Vite emits ``type="module"`` scripts, and Chromium refuses
module loads from a file:// opaque origin, so a file:// page renders an empty
body and the completion signal never fires (found live as a wait timeout).
It injects the config + per-tile results pre-navigation, waits for the page's
render-complete signal (the harness also arms a raw-script watchdog that
stamps ``timeout`` if the app never signals), and screenshots both themes at
the primary viewport — the ONE width the tool inlines (a second 760px pass
used to render two more screenshots that _render_blocks silently discarded).

The browser and the HTTP server are LAZY SINGLETONS on the renderer instance:
a job calls render_canvas several times (draft, post-fix, final), and
launching Chromium plus binding a port per call paid seconds of churn for
nothing. The runner closes the renderer when the job ends.

THREAD AFFINITY: Playwright's sync API is bound to the thread that started
it (greenlets), but LangGraph's ToolNode runs every sync tool call on a fresh
executor thread — and the runner's final close() on yet another. So every
browser touch (launch, render, close) is marshalled onto ONE dedicated
worker thread owned by the renderer; callers on any thread just block on it.

Screenshots land as FILES in the job workspace; the render_canvas tool then
inlines them as base64 image blocks in its result (tools._render_blocks) —
the agent's filesystem is virtual, so a path for it to read would point at
nothing. Captures are full_page: a canvas taller than the viewport is
stitched whole, never cropped (though very tall pages cost inspection
fidelity — the byte cap and the model's own downscaling both bite there).
A wait timeout is REPORTED (with the console errors, which usually name the
cause), never raised — one broken tile must not kill the authoring loop.

``PlaywrightRenderer`` is only importable where playwright + chromium exist
(the runtime image runs ``playwright install --with-deps chromium``); tests
inject fakes and the tool degrades to unavailable when ``renderer`` is None.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VIEWPORT = (1280, 800)
RENDER_TIMEOUT_MS = 20_000


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102 — request lines are noise
        pass


class PlaywrightRenderer:
    def __init__(self, *, harness_path: str, out_dir: str = "/tmp/canvas-render"):
        # harness_path points at harness.html; the whole dist dir is served.
        self.harness_dir = str(Path(harness_path).parent)
        self.harness_name = Path(harness_path).name
        self.out_dir = Path(out_dir)
        self._pw = None
        self._browser = None
        self._server = None
        self._url = ""
        # The ONE thread Playwright ever runs on (see module docstring).
        self._worker: ThreadPoolExecutor | None = None
        self._worker_lock = threading.Lock()

    def _on_worker(self, fn, *args, **kwargs):
        with self._worker_lock:
            if self._worker is None:
                self._worker = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="canvas-render"
                )
            worker = self._worker
        return worker.submit(fn, *args, **kwargs).result()

    def _ensure_server(self) -> str:
        if self._server is None:
            self._server = ThreadingHTTPServer(
                ("127.0.0.1", 0), partial(_QuietHandler, directory=self.harness_dir)
            )
            threading.Thread(target=self._server.serve_forever, daemon=True).start()
            self._url = (
                f"http://127.0.0.1:{self._server.server_address[1]}/{self.harness_name}"
            )
        return self._url

    def _ensure_browser(self):
        from playwright.sync_api import sync_playwright

        if self._pw is None:
            self._pw = sync_playwright().start()
        if self._browser is None or not self._browser.is_connected():
            self._browser = self._pw.chromium.launch()
        return self._browser

    def close(self) -> None:
        """Release the browser/driver/server. The runner calls this when the
        job ends (a microVM outlives one job); safe to call twice and from any
        thread — the teardown runs on the Playwright worker, which is then
        retired (the next job's first render starts a fresh one)."""
        with self._worker_lock:
            worker, self._worker = self._worker, None
        if worker is None:
            self._close()
            return
        try:
            worker.submit(self._close).result()
        finally:
            worker.shutdown(wait=True)

    def _close(self) -> None:
        for closer in (
            lambda: self._browser and self._browser.close(),
            lambda: self._pw and self._pw.stop(),
            lambda: self._server and self._server.shutdown(),
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001 — teardown must not raise
                pass
        self._browser = None
        self._pw = None
        self._server = None
        self._url = ""

    def render(
        self,
        *,
        config: dict[str, Any],
        results: dict[str, Any],
        dials: dict[str, Any],
    ) -> dict[str, Any]:
        return self._on_worker(
            self._render, config=config, results=results, dials=dials
        )

    def _render(
        self,
        *,
        config: dict[str, Any],
        results: dict[str, Any],
        dials: dict[str, Any],
    ) -> dict[str, Any]:
        from playwright.sync_api import TimeoutError as PwTimeout

        self.out_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"config": config, "results": results, "dials": dials})
        shots: list[dict[str, Any]] = []
        console: list[str] = []
        signals: set[str] = set()

        url = self._ensure_server()
        browser = self._ensure_browser()
        width, height = VIEWPORT
        try:
            for theme in ("light", "dark"):
                page = browser.new_page(viewport={"width": width, "height": height})
                page.on(
                    "console",
                    lambda m: console.append(f"[{m.type}] {m.text}")
                    if m.type in ("error", "warning")
                    else None,
                )
                page.on(
                    "pageerror",
                    lambda e: console.append(f"[pageerror] {e}"),
                )
                # The harness reads window.__CANVAS__ before mounting and
                # sets html.dark from __THEME__.
                page.add_init_script(
                    f"window.__CANVAS__ = {payload}; "
                    f"window.__THEME__ = {json.dumps(theme)};"
                )
                page.goto(url)
                # ANY value: "true" (all tiles settled) or the watchdog's
                # "timeout" (app never signalled — the screenshot still shows
                # whatever rendered).
                try:
                    page.wait_for_selector(
                        "body[data-render-complete]",
                        state="attached",
                        timeout=RENDER_TIMEOUT_MS,
                    )
                    signals.add(
                        page.get_attribute("body", "data-render-complete") or "?"
                    )
                except PwTimeout:
                    signals.add("no-signal")
                path = self.out_dir / f"canvas-{theme}-{width}.png"
                page.screenshot(path=str(path), full_page=True)
                shots.append({"path": str(path), "theme": theme, "width": width})
                page.close()
        except Exception:
            # A dead browser (crash, OOM) poisons the singleton — drop it so
            # the NEXT render relaunches, and let the tool report this one.
            # (_close, not close: we ARE the worker thread.)
            self._close()
            raise

        clean = signals <= {"true"}
        return {
            "rendered": True,
            "complete": clean,
            "screenshots": shots,
            "console_errors": console[:50],
            "note": (
                "the screenshots are attached inline below — INSPECT them; "
                "legibility judgments are yours, not the harness's"
                if clean
                else "the page never signalled render-complete "
                f"(signals: {sorted(signals)}) — the screenshots show whatever "
                "rendered; the console errors above usually name the cause"
            ),
        }
