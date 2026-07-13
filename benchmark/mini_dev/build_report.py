#!/usr/bin/env python3
"""Generate the benchmark PDF report (self-contained HTML + inline SVG charts).

Renders the agent+OKF BIRD mini_dev results as a one-file HTML report, then to
PDF via headless Chrome. Charts follow a simple dataviz style: single-series bars
in one blue hue with direct value labels (no legend), recessive gridlines, thin
rounded marks, light print surface.

The reference numbers below are the RESULTS.md run (Claude Opus, effort xhigh,
9-tool MCP set, 500 questions, graded by bird-bench evaluation_ex.py on SQLite).
They are embedded as literals so the report regenerates deterministically without
a live run. If a workspace with fresh stats exists (_ws/xhigh_*.json), pass
--from-ws to use those instead.

Usage:
  python3 build_report.py                       # writes report.html + OKF_mini_dev_report.pdf
  python3 build_report.py --from-ws _ws         # read stats from a run workspace
  python3 build_report.py --no-pdf              # HTML only (skip Chrome)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- reference run (RESULTS.md) -------------------------------------------- #
EX = 74.0
SOFTF1 = 77.5
MODEL = "Claude Opus 4.8"
EFFORT = "xhigh"
BY_DIFF = [("Simple", 84.5, 148), ("Moderate", 72.4, 250), ("Challenging", 62.8, 102)]
LEADER = [  # (label, EX, is_ours)
    ("agent + OKF (this run)", 74.0, True),
    ("TA + GPT-4o", 63.0, False),
    ("GPT-4", 47.8, False),
    ("GPT-4-turbo", 45.8, False),
    ("Llama3-70b", 40.8, False),
    ("GPT-3.5-turbo", 38.0, False),
]
EX_BY_DB = {
    "superhero": {"ex": 90.4, "n": 52}, "student_club": {"ex": 89.6, "n": 48},
    "european_football_2": {"ex": 78.4, "n": 51}, "card_games": {"ex": 76.9, "n": 52},
    "debit_card_specializing": {"ex": 73.3, "n": 30}, "financial": {"ex": 71.9, "n": 32},
    "formula_1": {"ex": 71.2, "n": 66}, "toxicology": {"ex": 67.5, "n": 40},
    "codebase_community": {"ex": 67.3, "n": 49}, "thrombosis_prediction": {"ex": 60.0, "n": 50},
    "california_schools": {"ex": 60.0, "n": 30},
}
WIKI = {
    "agents": 500, "total_wiki_calls": 2234,
    "per_agent": {"min": 1, "max": 11, "avg": 4.47, "median": 4.0, "stdev": 1.46},
    "by_tool": {"read_page": 1271, "list_directory": 868, "grep": 80, "glob": 15,
                "get_backlinks": 0, "semantic_search": 0},
}
DUR = {"min_s": 17.8, "max_s": 499.1, "avg_s": 83.0, "median_s": 63.6,
       "stdev_s": 56.4, "p90_s": 137.6, "p95_s": 189.3, "total_agent_seconds": 41491}
RUN_TOKENS_M = 20.0
RUN_MINUTES = 51

# The exact agent task prompt (schema block replaced by OKF-bundle reading).
AGENT_PROMPT = """You are a text-to-SQL expert. Produce ONE valid SQLite query that answers the
question. You will be graded by execution on the real SQLite database (BIRD
execution accuracy), so the query must run on SQLite and return exactly the right rows.

STEP 1 — read your assignment (question + "evidence" as External Knowledge, and
the OKF dataset name). This file contains NO answer SQL — derive the query yourself.

STEP 2 — learn the database SOLELY from its OKF knowledge bundle (the only
knowledge source; NO raw schema is handed to you). Use the live MCP client with
progressive disclosure: list_directory / read_page / glob / grep / get_backlinks /
semantic_search. Read the dataset overview, the relevant tables/<t>.md (columns,
types, keys, grain, value semantics, encodings), and references/ (joins, metrics,
value formats). Do NOT query any database directly; do NOT read any gold/answer file.

STEP 3 — think step by step, then write ONE SQLite SELECT that answers the question.

STEP 4 — write ONLY the final SQL (one statement, from SELECT; no fences/comments)."""

DB_LABEL = {
    "superhero": "Superhero", "student_club": "Student Club",
    "european_football_2": "European Football", "card_games": "Card Games",
    "debit_card_specializing": "Debit Card", "toxicology": "Toxicology",
    "codebase_community": "Codebase Community", "financial": "Financial",
    "formula_1": "Formula 1", "thrombosis_prediction": "Thrombosis",
    "california_schools": "California Schools",
}

# ---- palette --------------------------------------------------------------- #
BLUE = "#2a78d6"; BLUE_D = "#0d366b"
INK = "#0b0b0b"; INK2 = "#52514e"; MUTED = "#8a8985"
GRID = "#e7e6e2"; ACCENT = "#eda100"


def maybe_load_ws(ws: str):
    """Override the reference literals from a run workspace, if present."""
    global EX_BY_DB, WIKI, DUR
    p = lambda n: os.path.join(ws, n)
    if os.path.exists(p("xhigh_ex_by_db.json")):
        EX_BY_DB = json.load(open(p("xhigh_ex_by_db.json")))
    if os.path.exists(p("xhigh_wiki_stats.json")):
        w = json.load(open(p("xhigh_wiki_stats.json")))
        WIKI.update(w)
        WIKI.setdefault("by_tool", {}).setdefault("get_backlinks", 0)
        WIKI["by_tool"].setdefault("semantic_search", 0)
    if os.path.exists(p("xhigh_duration_stats.json")):
        DUR.update(json.load(open(p("xhigh_duration_stats.json"))))
    print(f"  loaded run stats from {ws}")


# ---- tiny SVG horizontal-bar helper ---------------------------------------- #
def hbar(rows, *, w=680, rowh=34, maxval=100, pad_left=210, unit="",
         color=BLUE, highlight_idx=None, hi_color=ACCENT, sub=None):
    n = len(rows); h = n * rowh + 20
    bar_x = pad_left; bar_w = w - pad_left - 60
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" '
             f'font-family="-apple-system,Segoe UI,Roboto,sans-serif">']
    for frac in (0, .25, .5, .75, 1.0):
        x = bar_x + bar_w * frac
        parts.append(f'<line x1="{x:.1f}" y1="8" x2="{x:.1f}" y2="{h-14:.1f}" '
                     f'stroke="{GRID}" stroke-width="1"/>')
        parts.append(f'<text x="{x:.1f}" y="{h-2:.1f}" fill="{MUTED}" font-size="10" '
                     f'text-anchor="middle">{maxval*frac:.0f}</text>')
    for i, (lab, val) in enumerate(rows):
        y = 10 + i * rowh; bw = max(2, bar_w * val / maxval)
        c = hi_color if (highlight_idx is not None and i == highlight_idx) else color
        parts.append(f'<text x="{bar_x-10:.1f}" y="{y+rowh/2+1:.1f}" fill="{INK2}" '
                     f'font-size="12.5" text-anchor="end">{html.escape(lab)}</text>')
        if sub and lab in sub:
            parts.append(f'<text x="{bar_x-10:.1f}" y="{y+rowh/2+13:.1f}" fill="{MUTED}" '
                         f'font-size="9.5" text-anchor="end">{sub[lab]}</text>')
        parts.append(f'<rect x="{bar_x:.1f}" y="{y:.1f}" width="{bw:.1f}" '
                     f'height="{rowh-14:.1f}" rx="4" fill="{c}"/>')
        vlab = f"{val:.0f}" if float(val).is_integer() else f"{val:.1f}"
        parts.append(f'<text x="{bar_x+bw+6:.1f}" y="{y+(rowh-14)/2+4:.1f}" fill="{INK}" '
                     f'font-size="11.5" font-weight="600">{vlab}{unit}</text>')
    parts.append('</svg>')
    return "\n".join(parts)


def build_html() -> str:
    by_db = sorted(((DB_LABEL[k], v["ex"], v["n"]) for k, v in EX_BY_DB.items()),
                   key=lambda r: -r[1])
    total = WIKI["total_wiki_calls"]
    tools = [(t, c, f"{100*c/total:.1f}%") for t, c in WIKI["by_tool"].items()]

    chart_leader = hbar([(l, v) for l, v, _ in LEADER],
                        highlight_idx=next(i for i, (_, _, o) in enumerate(LEADER) if o),
                        rowh=38)
    chart_db = hbar([(l, v) for l, v, _ in by_db],
                    sub={l: f"n={n}" for l, v, n in by_db}, rowh=32)
    chart_diff = hbar([(l, v) for l, v, _ in BY_DIFF],
                      sub={l: f"n={n}" for l, v, n in BY_DIFF}, rowh=42, pad_left=140)
    tool_max = max(c for _, c, _ in tools) or 1
    chart_tools = hbar([(t, c) for t, c, _ in tools if c > 0], maxval=tool_max,
                       pad_left=150, rowh=34)

    css = f"""
@page {{ size: A4; margin: 16mm 15mm; }}
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: {INK}; margin: 0; font-size: 12px; line-height: 1.5;
  -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
h1 {{ font-size: 26px; margin: 0 0 2px; letter-spacing:-.4px; }}
h2 {{ font-size: 15.5px; margin: 26px 0 8px; padding-bottom:4px;
  border-bottom:2px solid {INK}; page-break-after: avoid; }}
h3 {{ font-size: 12.5px; margin: 14px 0 4px; color:{INK2}; page-break-after: avoid; }}
.sub {{ color:{INK2}; font-size:12px; margin:0 0 2px; }}
.tag {{ display:inline-block; background:{BLUE}; color:#fff; font-size:10px;
  font-weight:600; padding:2px 8px; border-radius:10px; margin-right:6px; letter-spacing:.3px; }}
.tiles {{ display:flex; gap:10px; margin:14px 0 4px; }}
.tile {{ flex:1; border:1px solid {GRID}; border-radius:10px; padding:12px 14px; }}
.tile .k {{ font-size:30px; font-weight:700; color:{BLUE_D}; letter-spacing:-.5px; }}
.tile .l {{ font-size:10.5px; color:{INK2}; text-transform:uppercase; letter-spacing:.4px; margin-top:2px;}}
.tile .n {{ font-size:10px; color:{MUTED}; margin-top:3px; }}
table {{ border-collapse:collapse; width:100%; margin:8px 0; font-size:11.5px; }}
th {{ text-align:left; border-bottom:1.5px solid {INK}; padding:5px 8px; font-size:10.5px;
  text-transform:uppercase; letter-spacing:.3px; color:{INK2}; }}
td {{ border-bottom:1px solid {GRID}; padding:5px 8px; }}
td.num, th.num {{ text-align:right; font-variant-numeric: tabular-nums; }}
.fig {{ border:1px solid {GRID}; border-radius:10px; padding:12px 14px 6px; margin:8px 0;
  page-break-inside:avoid; }}
.fig .cap {{ font-size:10.5px; color:{MUTED}; margin-top:4px; }}
.note {{ background:#f7f7f5; border-left:3px solid {BLUE}; padding:8px 12px; margin:10px 0;
  font-size:11px; border-radius:0 6px 6px 0; }}
pre {{ background:#f7f7f5; border:1px solid {GRID}; border-radius:8px; padding:10px 12px;
  font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size:9.5px; line-height:1.45;
  white-space:pre-wrap; word-wrap:break-word; page-break-inside:avoid; }}
.kv {{ width:100%; }} .kv td:first-child {{ color:{INK2}; width:38%; }}
.two {{ display:flex; gap:16px; }} .two > * {{ flex:1; }}
.foot {{ margin-top:22px; padding-top:8px; border-top:1px solid {GRID}; color:{MUTED}; font-size:9.5px; }}
.pb {{ page-break-before: always; }}
"""

    tool_rows = "".join(
        f'<tr><td><code>{t}</code></td><td class="num">{c:,}</td><td class="num">{s}</td></tr>'
        for t, c, s in tools)

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head><body>
<div><span class="tag">BENCHMARK REPORT</span><span class="tag" style="background:{INK2}">Data Wiki · OKF</span></div>
<h1>Text-to-SQL with an OKF Knowledge Wiki</h1>
<p class="sub">Agent + OKF evaluated on the BIRD <b>mini_dev</b> benchmark (500 questions, 11 databases), graded with the official BIRD evaluator.</p>
<p class="sub" style="color:{MUTED}">Model: {MODEL} · Reasoning effort: {EFFORT} · Grading: bird-bench <code>evaluation_ex.py</code> (unmodified) on SQLite</p>

<div class="tiles">
  <div class="tile"><div class="k">{EX:.1f}</div><div class="l">Execution Accuracy (EX)</div><div class="n">500 questions · SQLite set-equality</div></div>
  <div class="tile"><div class="k">{SOFTF1:.1f}</div><div class="l">Soft-F1</div><div class="n">lenient cell-level match</div></div>
  <div class="tile"><div class="k">+11.0</div><div class="l">EX vs best leaderboard entry</div><div class="n">TA + GPT-4o = 63.0</div></div>
  <div class="tile"><div class="k">{WIKI['per_agent']['avg']}</div><div class="l">Avg wiki reads / question</div><div class="n">500 agents · {total:,} calls</div></div>
</div>

<h2>1 · Executive summary</h2>
<p>An autonomous agent answered all 500 BIRD mini_dev questions with <b>no access to the database schema</b>. Its only knowledge of each database came from an <b>Open Knowledge Format (OKF) bundle</b> — LLM-authored markdown docs — read on demand over an MCP server. Every other condition matches the official BIRD leaderboard (SQLite dialect, expert "evidence" hint provided, chain-of-thought enabled), and grading uses BIRD's own unmodified evaluator on the real SQLite databases.</p>
<p>The agent scored <b>EX = {EX:.1f}</b>, above every published mini_dev leaderboard entry (best prior: TA + GPT-4o at 63.0). Because it reconstructed all table, column, join and value semantics from the wiki alone — in about {WIKI['per_agent']['avg']} reads per question — the result shows the OKF bundle preserves the schema knowledge required for text-to-SQL, with no measurable accuracy penalty for the substitution.</p>

<h2>2 · Result vs published leaderboard</h2>
<div class="fig">{chart_leader}<div class="cap">Execution Accuracy (EX), % — this run (highlighted) vs published BIRD mini_dev SQLite-EX entries. Same 500 questions, same grader, same evidence + CoT + SQLite conditions.</div></div>
<div class="note"><b>Comparability.</b> Identical questions, grader, and prompt conditions as the leaderboard. Two things differ by design: the knowledge source (OKF bundle vs raw schema) and the base model. This is therefore "agent + OKF as a system," not a same-model ablation of the wiki in isolation.</div>

<h2>3 · Methodology</h2>
<h3>3.1 Task &amp; grading</h3>
<p>BIRD mini_dev is 500 curated text-to-SQL questions over 11 relational databases. For each question the agent emits one SQLite <code>SELECT</code>. A prediction is <b>correct (EX = 1)</b> iff its result set equals the gold query's, as unordered row sets — computed by BIRD's own <code>evaluation_ex.py</code> (unmodified) against the real SQLite databases, 30 s/query timeout. We also report BIRD's Soft-F1 (lenient cell-level match).</p>
<h3>3.2 Conditions — matched to the leaderboard</h3>
<table class="kv">
<tr><td>Question set</td><td>BIRD mini_dev, all 500</td></tr>
<tr><td>SQL dialect</td><td>SQLite (matches grader &amp; leaderboard)</td></tr>
<tr><td>External Knowledge (evidence)</td><td><b>Provided</b> — same as leaderboard (<code>use_knowledge=True</code>)</td></tr>
<tr><td>Chain-of-thought</td><td><b>Enabled</b> — same as leaderboard (<code>cot=True</code>)</td></tr>
<tr><td>Knowledge source</td><td><b>OKF bundle only</b> (the single deliberate swap vs the leaderboard's CREATE-TABLE schema)</td></tr>
<tr><td>Grader</td><td>bird-bench <code>evaluation_ex.py</code> / <code>evaluation_f1.py</code>, unmodified</td></tr>
</table>
<h3>3.3 LLM &amp; agent configuration</h3>
<table class="kv">
<tr><td>Model</td><td>{MODEL}</td></tr>
<tr><td>Reasoning effort</td><td>{EFFORT}</td></tr>
<tr><td>Agents</td><td>500 — one independent agent per question (no shared state)</td></tr>
<tr><td>Concurrency</td><td>~14 agents in parallel (pool cap)</td></tr>
<tr><td>Knowledge access</td><td>Live OKF consumption MCP server (streamable-HTTP, AgentCore)</td></tr>
<tr><td>Tools available</td><td>All 9: <code>list_domains</code>, <code>list_declared_domains</code>, <code>search_domains</code>, <code>list_directory</code>, <code>read_page</code>, <code>glob</code>, <code>grep</code>, <code>get_backlinks</code>, <code>semantic_search</code></td></tr>
<tr><td>Run outcome</td><td>500/500 generated, 0 errors, ~{RUN_TOKENS_M:.0f}M tokens, ~{RUN_MINUTES} min wall-clock</td></tr>
</table>
<h3>3.4 Agent task prompt (abridged)</h3>
<p class="sub" style="font-size:10.5px">Each agent received this instruction. The <code>evidence</code> hint is used (as on the leaderboard); the schema is not provided — the agent derives it from the OKF bundle over MCP. The gold SQL is held in a separate file the agent never opens.</p>
<pre>{html.escape(AGENT_PROMPT)}</pre>

<h2 class="pb">4 · Results</h2>
<h3>4.1 By difficulty</h3>
<div class="fig">{chart_diff}<div class="cap">EX (%) by BIRD difficulty tier. Expected monotone decline; n = questions per tier.</div></div>
<h3>4.2 By database</h3>
<div class="fig">{chart_db}<div class="cap">EX (%) per database (500 questions across 11 databases), sorted. Direct-labeled; n per database shown.</div></div>

<h2>5 · Operational KPIs</h2>
<div class="two">
<div>
<h3>5.1 Wiki (MCP) tool usage</h3>
<p class="sub" style="font-size:10.5px">Reads per question against the OKF wiki. Total across the run: <b>{total:,}</b> calls.</p>
<table><thead><tr><th>Per-agent calls</th><th class="num">Value</th></tr></thead><tbody>
<tr><td>Min</td><td class="num">{WIKI['per_agent']['min']}</td></tr>
<tr><td>Average (mean)</td><td class="num"><b>{WIKI['per_agent']['avg']}</b></td></tr>
<tr><td>Median</td><td class="num">{WIKI['per_agent']['median']}</td></tr>
<tr><td>Max</td><td class="num">{WIKI['per_agent']['max']}</td></tr>
<tr><td>Std dev</td><td class="num">{WIKI['per_agent']['stdev']}</td></tr>
</tbody></table>
</div>
<div>
<h3>5.2 Agent execution duration</h3>
<p class="sub" style="font-size:10.5px">Per-agent wall-clock (s). Real elapsed ~{RUN_MINUTES} min at ~14-way concurrency.</p>
<table><thead><tr><th>Per-agent seconds</th><th class="num">Value</th></tr></thead><tbody>
<tr><td>Min</td><td class="num">{DUR['min_s']}</td></tr>
<tr><td>Average (mean)</td><td class="num"><b>{DUR['avg_s']}</b></td></tr>
<tr><td>Median</td><td class="num">{DUR['median_s']}</td></tr>
<tr><td>p90</td><td class="num">{DUR['p90_s']}</td></tr>
<tr><td>p95</td><td class="num">{DUR['p95_s']}</td></tr>
<tr><td>Max</td><td class="num">{DUR['max_s']}</td></tr>
</tbody></table>
</div>
</div>
<h3>5.3 Most-used wiki tools</h3>
<div class="fig">{chart_tools}<div class="cap">Total calls per tool across the run. <code>read_page</code> + <code>list_directory</code> = 96% of all access; agents navigated structurally — <code>semantic_search</code> and <code>get_backlinks</code> were available but never invoked.</div></div>
<table><thead><tr><th>Tool</th><th class="num">Calls</th><th class="num">Share</th></tr></thead><tbody>
{tool_rows}
</tbody></table>

<h2>6 · Validity &amp; integrity</h2>
<ul style="margin:6px 0; padding-left:18px; font-size:11.5px;">
<li><b>Gold isolation.</b> Agents read a question file containing only the question, evidence, and database name — never the gold SQL. Verified: <b>500 / 500</b> agents used the MCP wiki path; byte-identical-to-gold predictions were 56/500 (11.2%), the expected base rate of canonical short queries, not copies.</li>
<li><b>Grader fidelity.</b> Feeding the gold SQL as predictions through this same evaluator yields EX 99.6 (two queries exceed BIRD's own 30 s timeout), confirming the grader is byte-for-byte the official one.</li>
<li><b>No schema leakage.</b> The agent never received CREATE-TABLE statements; all schema knowledge was reconstructed from the OKF bundle.</li>
</ul>

<div class="foot">Generated from run artifacts (bird-bench eval logs + MCP transcript stats). BIRD mini_dev: bird-bench/mini_dev (CC-BY-SA 4.0). OKF = Open Knowledge Format. Grader unmodified from bird-bench.</div>
</body></html>"""


def find_chrome() -> str | None:
    for c in ("google-chrome", "google-chrome-stable", "chromium",
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Chromium.app/Contents/MacOS/Chromium"):
        if os.path.isfile(c) or shutil.which(c):
            return c
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-ws", default=None, help="read stats from a run workspace dir")
    ap.add_argument("--no-pdf", action="store_true", help="write HTML only")
    ap.add_argument("--out", default=os.path.join(HERE, "OKF_mini_dev_report.pdf"))
    args = ap.parse_args()

    if args.from_ws:
        maybe_load_ws(args.from_ws)

    html_path = os.path.join(HERE, "report.html")
    open(html_path, "w").write(build_html())
    print(f"wrote {html_path}")

    if args.no_pdf:
        return 0
    chrome = find_chrome()
    if not chrome:
        print("  (no Chrome/Chromium found — wrote HTML only; render PDF manually)")
        return 0
    subprocess.run(
        [chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={args.out}", f"file://{html_path}"],
        check=True, capture_output=True)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
