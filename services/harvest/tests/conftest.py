"""Harvest test hermeticity.

The extractor fleet (ar_author map-reduce) is ON by default in production;
with real AWS credentials in a developer's shell an unguarded
``author_policy_doc`` unit test would build a live Bedrock client and CALL
it. Force the kill switch off for every test — the fleet's own tests inject
fakes through the ``extract`` seam (or flip the env back on explicitly).
"""

import pytest


@pytest.fixture(autouse=True)
def _no_live_extractor_fleet(monkeypatch):
    monkeypatch.setenv("OKF_POLICY_FANOUT", "false")
