"""GET /report/{id}: presigned artifact urls resolved from the composite id
alone (no database row), 404 semantics, and honest pdf-absent degrade."""

from __future__ import annotations

import pytest

from control_api import handlers
from control_api.handlers import ApiError
from okf_core import reports as rp

from tests.conftest import BUCKET

RID = "rep~motorsport~f1~20260815T120000Z~a1b2c3d4"
PREFIX = rp.report_s3_prefix("motorsport", "f1", "20260815T120000Z", "a1b2c3d4")


def _seed(s3, *, pdf=True):
    s3.put_object(Bucket=BUCKET, Key=rp.report_html_key(PREFIX), Body=b"<html/>")
    s3.put_object(Bucket=BUCKET, Key=rp.report_blocks_key(PREFIX), Body=b"{}")
    if pdf:
        s3.put_object(Bucket=BUCKET, Key=rp.report_pdf_key(PREFIX), Body=b"%PDF")


def test_get_report_presigns_all_artifacts(cfg):
    _seed(cfg.s3)
    out = handlers.get_report(cfg.s3, bucket=BUCKET, report_id=RID)
    assert out["data_domain"] == "motorsport" and out["dataset"] == "f1"
    assert rp.report_html_key(PREFIX) in out["html_url"]
    assert rp.report_pdf_key(PREFIX) in out["pdf_url"]
    assert rp.report_blocks_key(PREFIX) in out["blocks_url"]


def test_get_report_pdf_absent_degrades_honestly(cfg):
    _seed(cfg.s3, pdf=False)
    out = handlers.get_report(cfg.s3, bucket=BUCKET, report_id=RID)
    assert out["pdf_url"] == "" and out["html_url"]


def test_get_report_404s(cfg):
    with pytest.raises(ApiError) as e:
        handlers.get_report(cfg.s3, bucket=BUCKET, report_id="garbage")
    assert e.value.status == 404
    with pytest.raises(ApiError) as e:  # well-formed id, nothing stored
        handlers.get_report(cfg.s3, bucket=BUCKET, report_id=RID)
    assert e.value.status == 404
