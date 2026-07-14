"""Renderer-oracle test for orphan re-anchoring — catches the NEXT markdown-vs-DOM
gap before a user does.

The three false-orphan bugs we hit (frontmatter YAML keys, table pipes, code-fence
language tags) share one root cause: the raw ``.md`` carries text the reader never
sees, so a quote captured from the RENDERED DOM couldn't substring-match the raw
source. Rather than keep whack-a-moling, this test builds an INDEPENDENT oracle:

  1. render a kitchen-sink OKF doc to HTML with a real CommonMark renderer
     (``markdown-it-py`` — same CommonMark+GFM lineage as the UI's react-markdown),
  2. extract visible text from that HTML the way ``window.getSelection()`` would
     (drop tags, keep text; join blocks with spaces),
  3. slice realistic selections out of that VISIBLE text — exactly what a user
     could highlight — and assert every one re-anchors via ``is_orphaned`` == False,
  4. assert a genuinely-absent quote still orphans (no false negatives).

If a future block type (blockquote, nested list, …) leaks source-only text, a
slice spanning it won't match the body and this test fails — pointing at the gap.

Skipped when ``markdown-it-py`` isn't installed, so okf_core's runtime stays
dep-minimal; it's a dev/test-only oracle (see pyproject ``dev`` extras).
"""

from __future__ import annotations

from html.parser import HTMLParser

import pytest

from okf_core import annotations as anno

markdown_it = pytest.importorskip("markdown_it")


# A deliberately dense body: headings, paragraphs, emphasis/inline-code, a fenced
# SQL block, a GFM table, links, a blockquote, and nested lists. Each is a shape a
# real OKF join/table/metric doc uses, and each is a place source != render.
_KITCHEN_SINK = """---
type: Reference
title: races join circuits
description: how races link to circuits
---
# Overview

Join **race** events to their `circuit` dimension. See the
[circuits table](../tables/circuits.md) for the dimension.

```sql
SELECT * FROM "races" r JOIN "circuits" c ON r."circuitid" = c."circuitid"
```

`circuits.circuitid` is unique, so the relationship is many races to one circuit.

# Schema

| Column | Type | Description |
|---|---|---|
| `circuitid` | int | one row per circuit, the primary key |
| `country` | string | the circuit's country |

# Gotchas

> Some race rows do not resolve to a circuit; use a LEFT JOIN to keep the calendar.

Cardinality notes:

- A circuit can have many races.
- A race resolves to zero or one circuit.
  - Sprint weekends still map to a single circuit.

# Citations

- arn:aws:glue:eu-west-1:158204760618:table/formula_1/circuits
"""


class _VisibleText(HTMLParser):
    """Collect the visible text of rendered HTML, block by block.

    Mirrors what ``window.getSelection().toString()` yields: tag markup is gone,
    text between block boundaries is separated (so words from adjacent blocks
    don't fuse into one token). ``<td>``/``<th>`` also separate, so a cross-cell
    selection reads as space-joined — exactly the DOM behaviour the anchor relies on.
    """

    _BLOCKISH = {
        "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "td", "th",
        "blockquote", "pre", "code", "table", "thead", "tbody", "ul", "ol", "br",
    }

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._BLOCKISH:
            self._parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self._BLOCKISH:
            self._parts.append(" ")

    def handle_data(self, data):
        self._parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self._parts)


def _rendered_visible_text(markdown_body: str) -> str:
    """Render a markdown BODY to HTML, then extract its visible text."""
    md = markdown_it.MarkdownIt("commonmark").enable("table")
    html = md.render(markdown_body)
    p = _VisibleText()
    p.feed(html)
    # Collapse whitespace the way a DOM selection's toString would present it.
    return " ".join(p.text.split())


# Realistic selections a user could highlight in the rendered doc. Each is phrased
# as VISIBLE text (post-render), which is what the UI would capture as the quote.
_SELECTIONS = [
    "Join race events to their circuit dimension",  # prose w/ stripped bold+code
    "circuits table",  # a link's visible text
    'SELECT * FROM "races" r JOIN "circuits" c ON r."circuitid" = c."circuitid"',  # code
    "circuits.circuitid is unique, so the relationship is many races to one circuit",
    "one row per circuit, the primary key",  # inside a table cell
    "circuitid int one row per circuit",  # spans table cells
    "Some race rows do not resolve to a circuit; use a LEFT JOIN",  # blockquote
    "A race resolves to zero or one circuit",  # list item
    "Sprint weekends still map to a single circuit",  # nested list item
]


def _body_of(doc: str) -> str:
    # The annotatable surface is the body (frontmatter is stripped by the viewer),
    # and that's what the renderer oracle should see too.
    return anno.annotatable_text(doc)


@pytest.mark.parametrize("selection", _SELECTIONS)
def test_rendered_selection_reanchors(selection):
    # Sanity: the selection really IS visible text the renderer produced (so we're
    # testing a legitimate selection, not a hand-typed string that never renders).
    visible = _rendered_visible_text(_body_of(_KITCHEN_SINK))
    assert anno.normalize_text(selection) in anno.normalize_text(visible), (
        f"test selection is not actually in the rendered text: {selection!r}"
    )
    # The real assertion: such a selection must NOT be orphaned against the doc.
    assert not anno.is_orphaned(_KITCHEN_SINK, selection), (
        f"false orphan for a genuinely-rendered selection: {selection!r}"
    )


def test_absent_quote_still_orphans():
    # The oracle must not have loosened matching into false positives.
    assert anno.is_orphaned(_KITCHEN_SINK, "this sentence is nowhere in the document")


def test_every_visible_word_run_is_reachable():
    # Stronger sweep: slide a window over the rendered visible text and assert each
    # multi-word run re-anchors. Catches a block type that leaks source-only text
    # even if it's not one of the hand-picked selections above.
    words = _rendered_visible_text(_body_of(_KITCHEN_SINK)).split()
    misses = []
    for i in range(0, len(words) - 6):
        run = " ".join(words[i : i + 6])
        if anno.is_orphaned(_KITCHEN_SINK, run):
            misses.append(run)
    assert not misses, f"{len(misses)} visible run(s) failed to re-anchor, e.g. {misses[:3]}"
