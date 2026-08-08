"""okf_core.lint — the offline whole-bundle linter.

Each test builds a KNOWN-GOOD bundle in tmp_path and perturbs exactly one
invariant, so a finding maps to one seeded defect (and the clean bundle
proves the checks don't fire on healthy shapes: int↔double join keys,
generated index.md without frontmatter).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okf_core import lint
from okf_core.lint import collect_sql_fences, lint_bundle


def _fm(type_: str, title: str = "T") -> str:
    return f"---\ntype: {type_}\ntitle: {title}\ndescription: d\ntimestamp: t\n---\n\n"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


_COLUMNS_TSV = "\n".join(
    [
        "table\tcolumn\ttype\tcomment",
        "races\traceid\tbigint\t",
        "races\tyear\tint\t",
        "races\tname\tvarchar\t",
        "results\tresultid\tbigint\t",
        "results\traceid\tbigint\t",
        "results\tpoints\tdouble\t",
        "results\tdriver.name\tvarchar\t",
        "races\tyear_part\tint\t(partition key)",
    ]
)


def _bundle(tmp_path: Path) -> Path:
    """A bundle every lint step passes: 2 snapshot tables with docs, linked
    guardrails + dataset overview, a join doc with a valid typed condition,
    a linked metric, and a generated (frontmatter-less) index.md."""
    root = tmp_path / "sales" / "f1"
    _write(root, ".metadata/tables/races.md", "# snapshot races\n")
    _write(root, ".metadata/tables/results.md", "# snapshot results\n")
    _write(root, ".metadata/columns.tsv", _COLUMNS_TSV + "\n")
    _write(
        root,
        "datasets/f1.md",
        _fm("Glue Database", "F1")
        + "## Working with this data — read first\n"
        + "See [usage guardrails](../references/usage_guardrails.md).\n\n"
        + "Tables: [races](../tables/races.md), [results](../tables/results.md).\n",
    )
    _write(
        root,
        "tables/races.md",
        _fm("Glue Table", "Races")
        + "# Schema\n\n| Column | Type | Description |\n|---|---|---|\n"
        + "| `raceid` | bigint | id |\n| `year` | int | season |\n\n"
        + "# Common query patterns\n\n```sql\nSELECT raceid FROM \"f1\".\"races\" LIMIT 10\n```\n\n"
        + "# Joins\n\n- [results](../references/joins/races__results.md)\n\n"
        + "# Metrics\n\n- [wins](../references/metrics/wins.md)\n",
    )
    _write(
        root,
        "tables/results.md",
        _fm("Glue Table", "Results")
        + "# Schema\n\n| Column | Type | Description |\n|---|---|---|\n"
        + "| `resultid` | bigint | id |\n| `raceid` | bigint | fk |\n"
        + "| `driver` | struct | nested driver record |\n\n"
        + "# Joins\n\n- [races](../references/joins/races__results.md)\n",
    )
    _write(
        root,
        "references/joins/races__results.md",
        _fm("Reference", "races-results join")
        + "```sql\nraces.raceid = results.raceid\n```\n\n"
        + "**Cardinality (measured):** 1:N\n",
    )
    _write(
        root,
        "references/metrics/wins.md",
        _fm("Reference", "Wins") + "```sql\nSELECT COUNT(*) FROM \"f1\".\"results\"\n```\n",
    )
    _write(root, "references/usage_guardrails.md", _fm("Reference", "Guardrails") + "Rules.\n")
    # Generated artifacts: no frontmatter, and never lintable concept docs.
    _write(root, "index.md", "# Subdirectories\n\n* [tables](tables/index.md)\n")
    _write(root, "tables/index.md", "# Glue Table\n\n* [Races](races.md)\n")
    return root


def _codes(report, severity=None):
    return sorted(
        f.code for f in report.findings if severity is None or f.severity == severity
    )


def test_clean_bundle_passes(tmp_path):
    report = lint_bundle(_bundle(tmp_path))
    assert report.findings == []
    assert [s.status for s in report.steps] == ["ok"] * 5
    assert report.ok is True


def test_missing_table_doc_is_an_error(tmp_path):
    root = _bundle(tmp_path)
    (root / "tables/results.md").unlink()
    report = lint_bundle(root)
    assert "missing-table-doc" in _codes(report, "error")
    assert report.ok is False
    # The finding names the table so the supervisor can re-dispatch its author.
    finding = next(f for f in report.findings if f.code == "missing-table-doc")
    assert finding.path == "tables/results.md"


def test_stale_table_doc_is_a_warning(tmp_path):
    root = _bundle(tmp_path)
    _write(root, "tables/ghost.md", _fm("Glue Table", "Ghost") + "# Overview\n")
    report = lint_bundle(root)
    assert "stale-table-doc" in _codes(report, "warning")
    assert report.ok is True  # warnings alone don't fail the lint


def test_no_snapshot_skips_snapshot_dependent_steps(tmp_path):
    root = _bundle(tmp_path)
    import shutil

    shutil.rmtree(root / ".metadata")
    report = lint_bundle(root)
    by_name = {s.name: s.status for s in report.steps}
    assert by_name["coverage"] == "skipped"
    assert by_name["joins"] == "skipped"
    # Snapshot-free checks still ran.
    assert by_name["frontmatter"] == "ok" and by_name["links"] == "ok"
    assert report.ok is True


def test_missing_guardrails_is_an_error_and_its_link_breaks(tmp_path):
    root = _bundle(tmp_path)
    (root / "references/usage_guardrails.md").unlink()
    report = lint_bundle(root)
    errors = _codes(report, "error")
    assert "missing-usage-guardrails" in errors
    assert "broken-link" in errors  # the dataset overview still points at it


def test_guardrails_not_linked_from_dataset_is_a_warning(tmp_path):
    root = _bundle(tmp_path)
    _write(
        root,
        "datasets/f1.md",
        _fm("Glue Database", "F1")
        + "Tables: [races](../tables/races.md), [results](../tables/results.md).\n",
    )
    report = lint_bundle(root)
    assert "guardrails-not-linked" in _codes(report, "warning")


def test_broken_link_is_an_error(tmp_path):
    root = _bundle(tmp_path)
    with (root / "tables/races.md").open("a", encoding="utf-8") as f:
        f.write("\nSee [gone](../references/enums/gone.md).\n")
    report = lint_bundle(root)
    finding = next(f for f in report.findings if f.code == "broken-link")
    assert finding.severity == "error"
    assert finding.path == "tables/races.md"
    assert "references/enums/gone" in finding.message


def test_orphan_reference_is_a_warning(tmp_path):
    root = _bundle(tmp_path)
    _write(root, "references/glossary/term.md", _fm("Reference", "Term") + "A term.\n")
    report = lint_bundle(root)
    finding = next(f for f in report.findings if f.code == "orphan-reference")
    assert finding.severity == "warning"
    assert finding.path == "references/glossary/term.md"


def test_invalid_frontmatter_is_an_error(tmp_path):
    root = _bundle(tmp_path)
    _write(root, "references/glossary/bad.md", "---\ntype: Reference\n---\n\nNo title.\n")
    report = lint_bundle(root)
    finding = next(f for f in report.findings if f.code == "invalid-frontmatter")
    assert finding.path == "references/glossary/bad.md"
    assert report.ok is False


def test_join_unknown_column_is_an_error(tmp_path):
    root = _bundle(tmp_path)
    _write(
        root,
        "references/joins/races__results.md",
        _fm("Reference", "join") + "```sql\nraces.raceidx = results.raceid\n```\n",
    )
    report = lint_bundle(root)
    finding = next(f for f in report.findings if f.code == "join-key-unknown-column")
    assert "races.raceidx" in finding.message


def test_join_type_mismatch_is_a_warning_and_numeric_families_compare(tmp_path):
    root = _bundle(tmp_path)
    _write(
        root,
        "references/joins/races__results.md",
        _fm("Reference", "join")
        + "```sql\nraces.name = results.raceid\nAND races.year = results.points\n```\n",
    )
    report = lint_bundle(root)
    mismatches = [f for f in report.findings if f.code == "join-key-type-mismatch"]
    # varchar vs bigint warns; int vs double is numeric-comparable and doesn't.
    assert len(mismatches) == 1
    assert "races.name" in mismatches[0].message


def test_join_types_cover_redshift_postgres_spellings(tmp_path):
    # A Redshift snapshot writes pg type names; `character varying` must
    # compare as text and `double precision` as numeric — not fall through to
    # "unknown family, check skipped".
    root = tmp_path / "b"
    _write(
        root,
        ".metadata/columns.tsv",
        "table\tcolumn\ttype\tcomment\n"
        "a\tkey\tcharacter varying(64)\t\n"
        "a\tamount\tdouble precision\t\n"
        "b\tkey\tbigint\t\n"
        "b\tamount\tnumeric(10,2)\t\n",
    )
    _write(
        root,
        "references/joins/a__b.md",
        _fm("Reference", "join")
        + "```sql\na.key = b.key\nAND a.amount = b.amount\n```\n",
    )
    report = lint_bundle(root)
    mismatches = [f for f in report.findings if f.code == "join-key-type-mismatch"]
    assert len(mismatches) == 1  # varchar-vs-bigint warns; the numerics don't
    assert "a.key" in mismatches[0].message


def test_join_doc_without_condition_is_a_warning(tmp_path):
    root = _bundle(tmp_path)
    _write(
        root,
        "references/joins/races__results.md",
        _fm("Reference", "join") + "Join races to results on raceid.\n",
    )
    report = lint_bundle(root)
    assert "join-doc-no-condition" in _codes(report, "warning")


def test_alias_side_does_not_shield_a_typo_on_the_known_side(tmp_path):
    """An unknown LEFT table (an alias) must not skip the RIGHT side's
    existence check — each side verifies independently."""
    root = _bundle(tmp_path)
    _write(
        root,
        "references/joins/races__results.md",
        _fm("Reference", "join") + "```sql\nr.raceid = results.raceidx\n```\n",
    )
    report = lint_bundle(root)
    finding = next(f for f in report.findings if f.code == "join-key-unknown-column")
    assert "results.raceidx" in finding.message


def test_commented_out_condition_is_not_a_condition(tmp_path):
    root = _bundle(tmp_path)
    _write(
        root,
        "references/joins/races__results.md",
        _fm("Reference", "join")
        + "```sql\n-- races.raceid = results.raceid\n```\n",
    )
    report = lint_bundle(root)
    assert "join-doc-no-condition" in _codes(report, "warning")


def test_link_into_a_dot_directory_is_an_error(tmp_path):
    """.metadata exists on the harvest mount but the published wiki never
    serves it — such a link is dead for every consumer."""
    root = _bundle(tmp_path)
    with (root / "tables/races.md").open("a", encoding="utf-8") as f:
        f.write("\nSee [snapshot](../.metadata/tables/races.md).\n")
    report = lint_bundle(root)
    finding = next(f for f in report.findings if f.code == "broken-link")
    assert "never serves" in finding.message


def test_reserved_table_name_is_a_warning_not_a_permanent_error(tmp_path):
    """A source table named 'log' (or 'index') can't have tables/log.md —
    that basename is reserved — so coverage must not demand it forever."""
    root = _bundle(tmp_path)
    _write(root, ".metadata/tables/log.md", "# snapshot log\n")
    report = lint_bundle(root)
    assert "missing-table-doc" not in _codes(report, "error")
    finding = next(f for f in report.findings if f.code == "reserved-table-name")
    assert finding.severity == "warning" and "`log`" in finding.message
    assert report.ok is True


def test_external_same_named_table_unions_columns(tmp_path):
    """A cross-run counterpart sharing a table NAME with the own snapshot must
    union columns (first-wins validated pair docs against the wrong schema)
    and drop conflicting types so no false mismatch fires."""
    root = _bundle(tmp_path)
    _write(
        root,
        ".metadata/external/dom/ds/columns.tsv",
        "table\tcolumn\ttype\tcomment\n"
        "races\traceid\tvarchar\t\n"  # conflicts with own bigint -> type drops
        "races\tcircuit_ref\tvarchar\t\n",  # counterpart-only column
    )
    _write(
        root,
        "references/joins/races__results.md",
        _fm("Reference", "join")
        + "```sql\nraces.raceid = results.raceid\n"
        + "AND races.circuit_ref = results.resultid\n```\n",
    )
    report = lint_bundle(root)
    # circuit_ref exists via the union — no unknown-column error…
    assert "join-key-unknown-column" not in _codes(report)
    mismatches = [f for f in report.findings if f.code == "join-key-type-mismatch"]
    # …its varchar vs bigint still warns, while the conflicting-type raceid
    # pair stays silent (type dropped rather than guessed).
    assert len(mismatches) == 1 and "circuit_ref" in mismatches[0].message


def test_unknown_join_table_is_skipped_not_guessed(tmp_path):
    # A cross-run counterpart table isn't in this bundle's snapshot; lint must
    # stay silent rather than flag columns it cannot know.
    root = _bundle(tmp_path)
    _write(
        root,
        "references/joins/races__weather.md",
        _fm("Reference", "join") + "```sql\nraces.raceid = weather.race_id\n```\n",
    )
    # Keep the links step quiet about the new doc.
    with (root / "tables/races.md").open("a", encoding="utf-8") as f:
        f.write("\n- [weather](../references/joins/races__weather.md)\n")
    report = lint_bundle(root)
    assert "join-key-unknown-column" not in _codes(report)


def test_failed_step_is_isolated_and_fails_the_report(tmp_path, monkeypatch):
    root = _bundle(tmp_path)

    def boom(ctx):
        raise RuntimeError("boom")

    monkeypatch.setattr(lint, "_STEPS", (("coverage", boom), *lint._STEPS[1:]))
    report = lint_bundle(root)
    assert report.steps[0].status == "failed"
    assert "boom" in report.steps[0].note
    assert [s.status for s in report.steps[1:]] == ["ok"] * 4
    assert report.ok is False  # a step that didn't run is not a pass


# -- SQL fence collection (feeds the harvest tool's EXPLAIN step) --------------


@pytest.mark.parametrize(
    ("sql", "statements", "fragment", "templated"),
    [
        ('SELECT raceid FROM "f1"."races"', 1, False, False),
        ("races.raceid = results.raceid", 0, True, False),
        ("SELECT * FROM t WHERE year = <year>", 1, False, True),
        ("SELECT CAST(x AS array<varchar>) FROM t", 1, False, False),
        ("SELECT x::varchar FROM t", 1, False, False),
        ("SELECT * FROM t WHERE y = :year", 1, False, True),
        ("SELECT * FROM t WHERE id IN (1, 2, ...)", 1, False, True),
        ("SELECT 1;\nSELECT 2", 2, False, False),
        ("EXPLAIN SELECT 1", 1, False, False),
        ("SHOW PARTITIONS t", 0, False, False),
        # Literal/comment contents must not steer classification.
        ("SELECT * FROM races WHERE name = 'Monaco; GP'", 1, False, False),
        ('SELECT p FROM t WHERE p = \'{"active":true}\'', 1, False, False),
        ("SELECT x FROM t WHERE note LIKE '%...%'", 1, False, False),
        ("-- context...\nSELECT 1", 1, False, False),
        ("/* top races per season */\nSELECT r.year FROM races r", 1, False, False),
        ("EXPLAIN (FORMAT TEXT) SELECT 1", 1, False, False),
        ("EXPLAIN ANALYZE VERBOSE SELECT 1", 1, False, False),
    ],
)
def test_fence_classification(tmp_path, sql, statements, fragment, templated):
    root = tmp_path / "b"
    _write(root, "tables/t.md", _fm("Glue Table") + f"```sql\n{sql}\n```\n")
    (fence,) = collect_sql_fences(root)
    assert len(fence.statements) == statements
    assert fence.fragment is fragment
    assert fence.templated is templated


def test_fences_only_sql_tagged_blocks_count(tmp_path):
    root = tmp_path / "b"
    _write(
        root,
        "tables/t.md",
        _fm("Glue Table")
        + "```json\n{\"a\": 1}\n```\n\n```sql\nSELECT 1\n```\n\n```\nplain\n```\n",
    )
    fences = collect_sql_fences(root)
    assert [f.text for f in fences] == ["SELECT 1"]
    assert fences[0].path == "tables/t.md"


def test_explain_prefix_is_stripped_never_doubled(tmp_path):
    root = tmp_path / "b"
    _write(
        root,
        "tables/t.md",
        _fm("Glue Table") + "```sql\nEXPLAIN ANALYZE SELECT * FROM t\n```\n",
    )
    (fence,) = collect_sql_fences(root)
    assert fence.statements == ["SELECT * FROM t"]


def test_semicolon_inside_string_literal_does_not_shear_the_statement(tmp_path):
    sql = "SELECT * FROM races WHERE name = 'Monaco; GP'"
    root = tmp_path / "b"
    _write(root, "tables/t.md", _fm("Glue Table") + f"```sql\n{sql}\n```\n")
    (fence,) = collect_sql_fences(root)
    assert fence.statements == [sql]  # intact — not split at the literal's ';'


def test_comment_headed_statement_ships_without_the_comment(tmp_path):
    root = tmp_path / "b"
    _write(
        root,
        "tables/t.md",
        _fm("Glue Table") + "```sql\n/* header */ EXPLAIN SELECT 1\n```\n",
    )
    (fence,) = collect_sql_fences(root)
    assert fence.statements == ["SELECT 1"]


def test_unclosed_fence_at_eof_still_counts(tmp_path):
    # CommonMark renders an unterminated fence as code — its SQL must not
    # silently escape validation.
    root = tmp_path / "b"
    _write(root, "tables/t.md", _fm("Glue Table") + "```sql\nSELECT 1\n")
    (fence,) = collect_sql_fences(root)
    assert fence.statements == ["SELECT 1"]


def test_sql_fence_quoted_inside_a_plain_fence_is_not_collected(tmp_path):
    # A bare ``` fence must be TRACKED: when it went untracked, a literal
    # ```sql line quoted inside it opened a phantom SQL fence and the quoted
    # example was EXPLAINed as runnable SQL (false gate errors).
    root = tmp_path / "b"
    _write(
        root,
        "tables/t.md",
        _fm("Glue Table")
        + "How to document a query:\n\n"
        + "```\nwrite a fence like this:\n```sql\nSELECT quoted_example FROM x\n```\n\n"
        + "```sql\nSELECT real FROM t\n```\n",
    )
    fences = collect_sql_fences(root)
    texts = [f.text for f in fences]
    assert all("quoted_example" not in t for t in texts)
    assert any("SELECT real FROM t" in t for t in texts)


def test_link_to_generated_index_is_not_a_broken_link(tmp_path):
    # index.md/log.md are wiped at full-harvest start and only re-created by
    # finalize — AFTER both lint gates — and the guard refuses agent writes
    # to them. A legitimate link to one must not be an unfixable gate error.
    root = _bundle(tmp_path)
    with (root / "tables/races.md").open("a", encoding="utf-8") as f:
        f.write("\nSee [all tables](index.md) and [the run log](../log.md).\n")
    report = lint_bundle(root)
    broken = [f.message for f in report.findings if f.code == "broken-link"]
    assert not any("index.md" in m or "log.md" in m for m in broken)


def test_fence_parity_survives_offtemplate_openers():
    # A multi-word info string ("```sql title=x") or a 4-space-indented fence
    # must still be TRACKED: an untracked opener's closing ``` used to open a
    # phantom fence, and every later real ```sql fence escaped the gate.
    from okf_core.lint import _sql_fences_in

    body = (
        "```sql title=demo\n"
        "SELECT 1\n"
        "```\n\n"
        "```sql\n"
        "SELECT 2\n"
        "```\n"
    )
    assert _sql_fences_in(body) == ["SELECT 1", "SELECT 2"]

    nested = "- a list item:\n\n    ```sql\n    SELECT 3\n    ```\n"
    assert [f.strip() for f in _sql_fences_in(nested)] == ["SELECT 3"]

    # Inline triple-backtick prose (info string containing a backtick) is NOT
    # an opener per CommonMark — it must not flip parity either.
    prose = "```x``` is a fence marker\n\n```sql\nSELECT 4\n```\n"
    assert _sql_fences_in(prose) == ["SELECT 4"]


def test_type_families_cover_redshift_spellings():
    # SVV_ALL_COLUMNS emits Postgres spellings; a missing one doesn't just
    # miss a column — _type_family returns None and the whole join
    # type-compat check silently skips.
    from okf_core.lint import _families_comparable, _type_family

    for t in ("text", "bpchar", "nchar(8)", "nvarchar(64)", "character varying(255)"):
        assert _type_family(t) == "text", t
    assert _type_family("time without time zone") == "time"
    assert _type_family("timestamp with time zone") == "timestamp"
    assert not _families_comparable(_type_family("text"), _type_family("bigint"))
