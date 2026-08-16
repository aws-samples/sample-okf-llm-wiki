"""Shared config for the BIRD mini_dev benchmark on OKF.

The mini_dev benchmark ships 500 SQLite text-to-SQL questions across 11
databases. To evaluate an OKF knowledge bundle we load those 11 databases into
an AWS Glue catalog (so the OKF harvester can author bundles from them), then
have an agent answer each question using *only* the bundle — read over the
consumption MCP server — and grade the emitted SQL with bird-bench's own
evaluator on the original SQLite databases.

Two names differ between mini_dev's world and the Glue catalog we load into:

  1. db_id naming. mini_dev calls one database ``european_football_2``; we load
     it as the Glue database ``european_football`` (the SQLite source folder is
     ``european_football_2``). Every other db_id maps to itself.
  2. Identifier case. Glue/Hive fold table and column names to lowercase. The
     loader lowercases every identifier so the catalog matches; grading happens
     on the original SQLite databases where case is preserved, so this only
     matters for the (optional) Athena path, not for the leaderboard score.

Nothing here is account-specific — bucket names are derived at load time from
the caller's own AWS account id (see load_bird_to_glue.py).
"""

from __future__ import annotations

# Region the Glue catalog + bundle bucket live in. Override via OKF_REGION.
import os

REGION = os.environ.get("OKF_REGION", "eu-west-1")

# The 11 mini_dev databases. For each: the mini_dev db_id (also the SQLite
# folder name), the Glue database name we load it as, and a short slug used to
# build a per-database S3 bucket name (okf-bird-<slug>-<account>).
#
# db_id -> (glue_db, bucket_slug)
DATABASES: dict[str, tuple[str, str]] = {
    "california_schools": ("california_schools", "calschools"),
    "card_games": ("card_games", "cardgames"),
    "codebase_community": ("codebase_community", "codebase"),
    "debit_card_specializing": ("debit_card_specializing", "debitcard"),
    "european_football_2": ("european_football", "football"),  # the one rename
    "financial": ("financial", "financial"),
    "formula_1": ("formula_1", "formula1"),
    "student_club": ("student_club", "studentclub"),
    "superhero": ("superhero", "superhero"),
    "thrombosis_prediction": ("thrombosis_prediction", "thrombosis"),
    "toxicology": ("toxicology", "toxicology"),
}

# The OKF data domain the bundles are registered under (data_domain in MCP calls).
DATA_DOMAIN = os.environ.get("OKF_BIRD_DOMAIN", "bird")


def glue_db(db_id: str) -> str:
    """Map a mini_dev db_id to its Glue database name."""
    try:
        return DATABASES[db_id][0]
    except KeyError as e:
        raise KeyError(
            f"unknown mini_dev db_id {db_id!r}; known: {sorted(DATABASES)}"
        ) from e


def bucket_slug(db_id: str) -> str:
    """Short slug used in the per-database S3 bucket name."""
    return DATABASES[db_id][1]


def bucket_name(db_id: str, account_id: str) -> str:
    """Per-database bucket: okf-bird-<slug>-<account>."""
    return f"okf-bird-{bucket_slug(db_id)}-{account_id}"
