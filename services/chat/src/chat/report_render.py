"""Headless-Chromium rendering for reports: chart specs → PNGs, HTML → PDF.

A report's chart blocks carry declarative ``renderChart`` specs; nothing
model-authored may execute at view time, so the specs are rasterized ONCE at
create_report time through the app's real renderer: a minimal harness page
(a flagged Vite entry over ``ui/src/lib/chartIframe.js``, baked into the
chat image) reads ``window.__OKF_REPORT_CHARTS__``, mounts one sandboxed
chart iframe per spec, and stamps ``body[data-render-complete]`` when every
frame settles. The page is loaded twice — ``__OKF_REPORT_THEME__`` "light"
then "dark" — and each chart element is screenshotted at 2× with a
transparent background, returned as per-theme data URIs the composer bakes
into the HTML — this render IS the agent's
"does my report actually render" verification: a failing chart refuses the
save. The PDF pass prints that SAME composed HTML — one artifact, one
appearance.

Chromium + the local HTTP server are lazy singletons (launch once per
microVM, not per create); any Playwright exception poisons the singleton —
``close()`` then re-raise — so the next call relaunches clean. Loopback HTTP,
never file://, because Vite emits module scripts Chromium refuses to load
from a file:// opaque origin.
"""

from __future__ import annotations

import base64
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# 2× a 760px-content column: crisp in the composed HTML and in print.
CHART_WIDTH = 800
DEVICE_SCALE = 2
RENDER_TIMEOUT_MS = 20_000


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # pragma: no cover - noise only
        pass


class ChartRenderer:
    """Owns the browser + server singletons. Safe to ``close()`` twice."""

    def __init__(self, *, harness_path: str):
        p = Path(harness_path)
        self.harness_dir = str(p.parent)
        self.harness_name = p.name
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._pw = None
        self._browser = None

    def _ensure_server(self) -> str:
        if self._server is None:
            self._server = ThreadingHTTPServer(
                ("127.0.0.1", 0), partial(_QuietHandler, directory=self.harness_dir)
            )
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True
            )
            self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}/{self.harness_name}"

    def _ensure_browser(self):
        from playwright.sync_api import sync_playwright

        if self._pw is None:
            self._pw = sync_playwright().start()
        if self._browser is None or not self._browser.is_connected():
            self._browser = self._pw.chromium.launch()
        return self._browser

    def close(self) -> None:
        for attr, stop in (("_browser", "close"), ("_pw", "stop")):
            obj = getattr(self, attr)
            if obj is not None:
                try:
                    getattr(obj, stop)()
                except Exception:  # noqa: BLE001 - teardown is best-effort
                    pass
                setattr(self, attr, None)
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            self._server = None
            self._thread = None

    def _render_pass(
        self, browser, url: str, charts: list[dict[str, Any]], theme: str
    ) -> list[str]:
        """One page load under one theme: the harness applies the injected
        ``__OKF_REPORT_THEME__`` before resolving the palette, and every
        screenshot omits the background so the PNGs stay transparent."""
        page = browser.new_page(
            viewport={"width": CHART_WIDTH + 60, "height": 700},
            device_scale_factor=DEVICE_SCALE,
        )
        errors: list[str] = []
        page.on(
            "console",
            lambda msg: errors.append(msg.text) if msg.type == "error" else None,
        )
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(
            f"window.__OKF_REPORT_CHARTS__ = {json.dumps(charts)};"
            f"window.__OKF_REPORT_THEME__ = {json.dumps(theme)};"
        )
        page.goto(url)
        page.wait_for_selector(
            "body[data-render-complete]",
            state="attached",
            timeout=RENDER_TIMEOUT_MS,
        )
        signal = page.get_attribute("body", "data-render-complete")
        failed = page.eval_on_selector_all(
            "[data-okf-chart][data-error]",
            "els => els.map(e => e.getAttribute('data-okf-chart'))",
        )
        if signal != "true" or failed:
            which = f"chart(s) {failed}" if failed else f"signal {signal!r}"
            detail = f"; console: {errors[:3]}" if errors else ""
            raise RuntimeError(f"chart render failed ({theme}) — {which}{detail}")
        uris: list[str] = []
        for i in range(len(charts)):
            shot = page.locator(f"[data-okf-chart='{i}']").screenshot(
                type="png", omit_background=True
            )
            uris.append(
                "data:image/png;base64," + base64.b64encode(shot).decode("ascii")
            )
        page.close()
        return uris

    def render_charts(self, charts: list[dict[str, Any]]) -> list[dict[str, str]]:
        """``charts`` = ``[{"spec": <renderChart spec>, "height": int}]`` in
        block order. Returns one ``{"light": uri, "dark": uri}`` dict per
        chart — two page loads, one per theme, each screenshotted with a
        transparent background so the composed report can show the right PNG
        per app theme. Raises RuntimeError naming the failing chart when a
        frame reports an error or the page never settles — a chart that
        cannot render must refuse the save, not ship a blank figure."""
        if not charts:
            return []
        url = self._ensure_server()
        try:
            browser = self._ensure_browser()
            by_theme = {
                theme: self._render_pass(browser, url, charts, theme)
                for theme in ("light", "dark")
            }
            return [
                {"light": light, "dark": dark}
                for light, dark in zip(by_theme["light"], by_theme["dark"])
            ]
        except Exception:
            # Poisoned singleton: a crashed/hung Chromium would fail every
            # subsequent call — drop it so the next render relaunches.
            self.close()
            raise

    def pdf(self, html: str) -> bytes:
        """Print the composed report HTML to PDF (A4, backgrounds on). The
        HTML is fully self-contained — data-URI images, inline CSS — so
        ``set_content`` needs no network and no server."""
        try:
            browser = self._ensure_browser()
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            out = page.pdf(
                format="A4",
                print_background=True,
                margin={
                    "top": "14mm",
                    "bottom": "16mm",
                    "left": "12mm",
                    "right": "12mm",
                },
            )
            page.close()
            return out
        except Exception:
            self.close()
            raise
