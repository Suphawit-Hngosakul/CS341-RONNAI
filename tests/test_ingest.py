"""ทดสอบ ingestion/ingest.py กับ S3 จำลอง (moto) ไม่ต้องใช้ AWS จริง

รัน:  pip install -r requirements-dev.txt
      python -m pytest tests -q
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestion"))
import ingest  # noqa: E402

BUCKET = "cs341-ronnai-test"
CFG = {
    "aws": {"region": "us-east-1"},
    "s3": {"bucket": BUCKET, "prefixes": {"incoming": "incoming/", "raw": "raw/"}},
}
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone(timedelta(hours=7)))


@pytest.fixture
def s3(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x",
                 "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def csv_files(tmp_path):
    files = {}
    for src, header in [("lst", "community_id,buffer_m,year,lst_mean"),
                        ("ndvi", "community_id,buffer_m,year,ndvi_mean"),
                        ("buildings", "community_id,buffer_m,year,builtup_pct")]:
        p = tmp_path / f"{src}_2026.csv"
        p.write_text(f"{header}\nC0001,300,2026,1.0\n", encoding="utf-8")
        files[src] = p
    return files


def keys(s3, prefix=""):
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", []))


def test_batch_id_format():
    assert ingest.make_batch_id("lst", 2026, NOW) == "lst_2026_20261111T1200"


def test_ingest_three_sources(s3, csv_files):
    for src, path in csv_files.items():
        ingest.ingest(s3, CFG, file_path=path, source=src, year=2026, now=NOW)

    for src, path in csv_files.items():
        bid = f"{src}_2026_20261111T1200"
        raw_prefix = f"raw/source={src}/batch_id={bid}/"
        assert keys(s3, raw_prefix) == sorted([raw_prefix + "manifest.json", raw_prefix + path.name])
        assert keys(s3, f"incoming/source={src}/batch_id={bid}/") == [
            f"incoming/source={src}/batch_id={bid}/{path.name}"]

        raw_body = s3.get_object(Bucket=BUCKET, Key=raw_prefix + path.name)["Body"].read()
        assert raw_body == path.read_bytes()

        m = json.loads(s3.get_object(Bucket=BUCKET, Key=raw_prefix + "manifest.json")["Body"].read())
        f = m["files"][0]
        assert m["batch_id"] == bid and m["source"] == src and m["year"] == 2026
        assert f["file_name"] == path.name
        assert f["size_bytes"] == path.stat().st_size
        assert f["sha256"] == ingest.file_info(path)["sha256"]
        assert m["ingested_at"] == "2026-11-11T05:00:00+00:00"


def test_same_batch_id_is_not_overwritten(s3, csv_files, tmp_path):
    path = csv_files["lst"]
    ingest.ingest(s3, CFG, file_path=path, source="lst", year=2026, now=NOW)
    raw_key = f"raw/source=lst/batch_id=lst_2026_20261111T1200/{path.name}"
    before = s3.get_object(Bucket=BUCKET, Key=raw_key)["Body"].read()

    changed = tmp_path / "other" / path.name
    changed.parent.mkdir()
    changed.write_text("community_id,buffer_m,year,lst_mean\nC0001,300,2026,99.9\n", encoding="utf-8")

    with pytest.raises(ingest.IngestError) as e:
        ingest.ingest(s3, CFG, file_path=changed, source="lst", year=2026,
                      batch_id="lst_2026_20261111T1200", now=NOW)
    assert e.value.code == ingest.EXIT_BATCH_EXISTS
    assert s3.get_object(Bucket=BUCKET, Key=raw_key)["Body"].read() == before


def test_reupload_new_batch_keeps_old(s3, csv_files, capsys):
    path = csv_files["ndvi"]
    ingest.ingest(s3, CFG, file_path=path, source="ndvi", year=2026, now=NOW)
    ingest.ingest(s3, CFG, file_path=path, source="ndvi", year=2026, now=NOW + timedelta(minutes=5))
    assert "เคยนำเข้าแล้ว" in capsys.readouterr().out
    manifests = [k for k in keys(s3, "raw/source=ndvi/") if k.endswith("manifest.json")]
    assert len(manifests) == 2
    assert [m["batch_id"] for m in ingest.list_batches(s3, CFG, "ndvi")] == [
        "ndvi_2026_20261111T1200", "ndvi_2026_20261111T1205"]


def test_dry_run_uploads_nothing(s3, csv_files):
    ingest.ingest(s3, CFG, file_path=csv_files["lst"], source="lst", year=2026, now=NOW, dry_run=True)
    assert keys(s3) == []


@pytest.mark.parametrize("kwargs", [
    {"source": "LST/../x", "year": 2026},
    {"source": "lst", "year": 1990},
    {"source": "lst", "year": 2026, "batch_id": "ndvi_2026_20261111T1200"},
])
def test_bad_input(s3, csv_files, kwargs):
    with pytest.raises(ingest.IngestError):
        ingest.ingest(s3, CFG, file_path=csv_files["lst"], now=NOW, **kwargs)


def test_missing_file(s3, tmp_path):
    with pytest.raises(ingest.IngestError):
        ingest.ingest(s3, CFG, file_path=tmp_path / "nope.csv", source="lst", year=2026)


def test_cli_exit_code_on_duplicate(s3, csv_files, tmp_path, monkeypatch):
    cfg_path = tmp_path / "settings.yaml"
    cfg_path.write_text(f"aws:\n  region: us-east-1\ns3:\n  bucket: {BUCKET}\n", encoding="utf-8")
    monkeypatch.setattr(ingest, "make_s3_client", lambda cfg: s3)
    argv = ["--file", str(csv_files["lst"]), "--source", "lst", "--year", "2026",
            "--batch-id", "lst_2026_20261111T1200", "--config", str(cfg_path)]
    assert ingest.main(argv) == ingest.EXIT_OK
    assert ingest.main(argv) == ingest.EXIT_BATCH_EXISTS
