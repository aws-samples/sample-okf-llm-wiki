# Vendored third-party assets

## `chart.umd.min.js` — Chart.js v4.5.1 (MIT)

The UMD (browser global) build of [Chart.js](https://www.chartjs.org), copied
verbatim from `node_modules/chart.js/dist/chart.umd.min.js`.

**Why vendored instead of imported from `node_modules`:** the chat renders
model-authored chart "script code" inside a **sandboxed `<iframe>`**
(`sandbox="allow-scripts"`, no `allow-same-origin`, strict CSP, `connect-src
'none'`). That frame can't fetch anything, so Chart.js must be **inlined into the
iframe's `srcdoc`** at build time via a `?raw` import (see
`src/lib/chartIframe.js`). Chart.js's `package.json` `exports` map only permits
`chart.js`, `chart.js/auto`, and `chart.js/helpers` — a deep `?raw` import of
`chart.js/dist/chart.umd.min.js` is rejected by Vite's resolver. A local vendored
copy has no exports-map restriction, so `?raw` resolves cleanly.

The UMD build is fully self-contained (no sibling `require`/`import`) and
registers the `Chart` global, which is exactly what the inline `<script>` needs.

**To update:** bump `chart.js` in `package.json`, then
`cp node_modules/chart.js/dist/chart.umd.min.js src/vendor/chart.umd.min.js` and
update the version above.
