"""ทดสอบ function/quarantine_function (Issue #32) และ check_batch ที่ไม่ย้ายไฟล์เองแล้ว กับ S3 จำลอง (moto)"""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
BUCKET = "test-bucket"
NOW = datetime(2026, 11, 22, 17, 0, 0, tzinfo=timezone.utc)


def _load(folder, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "function" / folder / "lambda_function.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


qf = _load("quarantine_function", "quarantine_fn")


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "testing")
    with mock_aws():
        c = boto3.client("s3", region_name="us-east-1")
        c.create_bucket(Bucket=BUCKET)
        yield c


def keys(s3, prefix=""):
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", []))


def test_duplicate_moved_to_quarantine_duplicate_with_report(s3):
    s3.put_object(Bucket=BUCKET, Key="incoming/lst_2026.csv", Body=b"a,b\n1,2\n")
    event = {"status": "ALREADY_PROCESSED", "bucket": BUCKET, "key": "incoming/lst_2026.csv", "source": "lst",
             "year": 2026, "sha256": "abc", "processed_batch_id": "20261122T1600"}
    out = qf.quarantine_file(s3, BUCKET, event, now=NOW)
    folder = "quarantine/duplicate/lst/lst_2026_20261122T170000Z/"
    assert out == {"quarantine_kind": "duplicate", "quarantine_key": folder + "lst_2026.csv",
                   "quarantine_report": folder + "report.json"}
    assert keys(s3) == [folder + "lst_2026.csv", folder + "report.json"]                  # incoming/ ว่างแล้ว
    rep = json.loads(s3.get_object(Bucket=BUCKET, Key=folder + "report.json")["Body"].read())
    assert rep["status"] == "DUPLICATE" and rep["processed_batch_id"] == "20261122T1600"
    assert rep["original_key"] == "incoming/lst_2026.csv" and rep["sha256"] == "abc"
    assert s3.get_object(Bucket=BUCKET, Key=folder + "lst_2026.csv")["Body"].read() == b"a,b\n1,2\n"


def test_rejected_moved_to_quarantine_rejected_with_reason(s3):
    s3.put_object(Bucket=BUCKET, Key="incoming/LST 2026.csv", Body=b"x")
    event = {"status": "REJECTED", "bucket": BUCKET, "key": "incoming/LST 2026.csv", "reason": "ชื่อไฟล์ผิด"}
    out = qf.quarantine_file(s3, BUCKET, event, now=NOW)
    assert out["quarantine_kind"] == "rejected"
    assert out["quarantine_key"] == "quarantine/rejected/LST 2026_20261122T170000Z/LST 2026.csv"
    assert keys(s3, "incoming/") == []
    rep = json.loads(s3.get_object(Bucket=BUCKET, Key=out["quarantine_report"])["Body"].read())
    assert rep["status"] == "REJECTED" and rep["reason"] == "ชื่อไฟล์ผิด" and "source" not in rep


def test_missing_source_file_is_not_an_error(s3):
    """retry หลังย้ายสำเร็จแล้ว (ต้นทางหายไปแล้ว) -> ไม่ error ไม่สร้าง report ซ้ำ"""
    event = {"status": "REJECTED", "bucket": BUCKET, "key": "incoming/gone.csv"}
    out = qf.quarantine_file(s3, BUCKET, event, now=NOW)
    assert out == {"quarantine_kind": "rejected", "quarantine_key": None, "quarantine_report": None}
    assert keys(s3) == []


def test_unknown_status_raises(s3):
    with pytest.raises(qf.QuarantineError):
        qf.quarantine_file(s3, BUCKET, {"status": "NEW", "key": "incoming/lst_2026.csv"}, now=NOW)


def test_handler_keeps_input_fields_and_uses_prefix_env(s3, monkeypatch):
    monkeypatch.setenv("QUARANTINE_PREFIX", "q")
    s3.put_object(Bucket=BUCKET, Key="incoming/ndvi_2026.csv", Body=b"x")
    event = {"status": "ALREADY_PROCESSED", "bucket": BUCKET, "key": "incoming/ndvi_2026.csv", "source": "ndvi"}
    out = qf.lambda_handler(event)
    assert out["status"] == "ALREADY_PROCESSED" and out["source"] == "ndvi"   # state machine ใช้ $.status ต่อ
    assert out["quarantine_key"].startswith("q/duplicate/ndvi/ndvi_2026_")


def test_check_batch_only_reports_duplicate_and_leaves_file(s3, monkeypatch):
    """check_batch ไม่ย้ายไฟล์เองแล้ว (#32) ไฟล์ยังอยู่ใน incoming/ ให้ QuarantineFile ย้าย"""
    import hashlib
    cb = _load("batch_function", "batch_fn")
    body = b"community_id,buffer_m\n"
    sha = hashlib.sha256(body).hexdigest()
    s3.put_object(Bucket=BUCKET, Key="incoming/lst_2026.csv", Body=body)
    s3.put_object(Bucket=BUCKET, Key="raw/source=lst/batch_id=b1/manifest.json",
                  Body=json.dumps({"batch_id": "b1", "year": 2026, "files": [{"sha256": sha}]}).encode())
    s3.put_object(Bucket=BUCKET, Key="raw/source=lst/batch_id=b1/_SUCCESS.json", Body=b"{}")
    out = cb.lambda_handler({"bucket": BUCKET, "key": "incoming/lst_2026.csv"})
    assert out["status"] == "ALREADY_PROCESSED" and out["processed_batch_id"] == "b1"
    assert "quarantine_key" not in out
    assert "incoming/lst_2026.csv" in keys(s3, "incoming/")
