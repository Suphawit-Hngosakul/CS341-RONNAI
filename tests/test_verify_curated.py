"""ทดสอบ ingestion/verify_curated.py (Issue #16: ตรวจ curated + เขียน _SUCCESS.json)

รัน:  pip install -r requirements-dev.txt
      python -m pytest tests -q

Athena จำลองด้วย sqlite3 (ไลบรารีมาตรฐาน ไม่ต้องติดตั้งเพิ่ม) S3 จำลองด้วย moto
ใช้ gee/communities.csv ของจริงใน repo (36 ชุมชน) เป็นตัวกำหนดจำนวนแถวที่คาด
SQL ที่สคริปต์ใช้เป็น count / GROUP BY / CASE / DISTINCT ธรรมดา ที่ sqlite รันได้เหมือน Athena
ส่วนที่ไม่ได้ทดสอบที่นี่: การเชื่อมต่อ Athena จริง (class Athena) ต้องลองกับ AWS จริง
"""
import json
import sqlite3
import sys
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingestion"))
import verify_curated as vc  # noqa: E402

BUCKET = "cs341-ronnai-test"
KEY = "curated/_SUCCESS.json"
IDS = [f"C{i:04d}" for i in range(1, 37)]
VALUE_COLS = vc.TABLES["community_heat"]["complete"]      # lst_mean_300 ... builtup_pct_500
SOURCES = {"lst": "lst_2026_20261111T1200",
           "ndvi": "ndvi_2026_20261111T1205",
           "buildings": "buildings_2026_20261111T1210"}


class FakeAthena:
    """Athena จำลอง: รัน SQL ด้วย sqlite (ตาราง trusted อยู่ใน schema ronnai_trusted)"""

    def __init__(self, con):
        self.con = con

    def query(self, sql):
        try:
            cur = self.con.execute(sql)
        except sqlite3.OperationalError as e:           # Athena จะ FAILED แบบเดียวกัน
            raise vc.VerifyError(f"Athena query FAILED: {e}")
        cols = [d[0] for d in cur.description]
        return [{c: (None if v is None else str(v)) for c, v in zip(cols, row)} for row in cur.fetchall()]


def build_db(with_batch_id=True):
    con = sqlite3.connect(":memory:")
    cols = ", ".join(f"{c} REAL" for c in VALUE_COLS)
    con.execute(f"CREATE TABLE community_heat (community_id TEXT, year INTEGER, {cols})")
    for cid in IDS:
        con.execute(f"INSERT INTO community_heat VALUES (?, 2026, {', '.join('1.0' for _ in VALUE_COLS)})", [cid])
    con.execute("ATTACH ':memory:' AS ronnai_trusted")
    for src, batch in SOURCES.items():
        extra = ", batch_id TEXT" if with_batch_id else ""
        con.execute(f"CREATE TABLE ronnai_trusted.{src} (community_id TEXT, buffer_m INTEGER, year INTEGER{extra})")
        for cid in IDS:
            for b in (300, 500):
                if with_batch_id:
                    con.execute(f"INSERT INTO ronnai_trusted.{src} VALUES (?, ?, 2026, ?)", [cid, b, batch])
                else:
                    con.execute(f"INSERT INTO ronnai_trusted.{src} VALUES (?, ?, 2026)", [cid, b])
    return con


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    cfg = {"s3": {"bucket": BUCKET, "prefixes": {}},
           "athena": {"workgroup": "ronnai-wg"},
           "glue": {"databases": {"trusted": "ronnai_trusted", "curated": "ronnai_curated"}}}
    cfg_path = tmp_path / "settings.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        yield s3, cfg_path


def run(env, con, *extra):
    _, cfg_path = env
    return vc.main(["--config", str(cfg_path), *extra], athena_factory=lambda: FakeAthena(con))


def success_doc(s3):
    try:
        return json.loads(s3.get_object(Bucket=BUCKET, Key=KEY)["Body"].read())
    except s3.exceptions.NoSuchKey:
        return None


# ---------------------------------------------------------------- ผ่าน
def test_expected_rows_comes_from_communities_csv():
    assert vc.expected_rows("community_heat") == 36


def test_pass_writes_success_json_with_source_batches(env, capsys):
    s3, _ = env
    assert run(env, build_db()) == 0
    doc = success_doc(s3)
    assert doc["status"] == "SUCCESS" and doc["table"] == "community_heat" and doc["year"] == 2026
    assert doc["row_count"] == 36 == doc["expected_rows"]
    assert doc["duplicate_keys"] == 0
    assert doc["coverage_pct"] == 100.0 and doc["complete_rows"] == 36
    assert doc["batch_id"].startswith("curated_")
    assert doc["source_batches"] == {src: [b] for src, b in SOURCES.items()}
    assert "[PASS]" in capsys.readouterr().out


def test_no_write_passes_without_file(env):
    s3, _ = env
    assert run(env, build_db(), "--no-write") == 0
    assert success_doc(s3) is None


def test_trusted_without_batch_id_column_still_passes_with_warning(env, capsys):
    s3, _ = env
    assert run(env, build_db(with_batch_id=False)) == 0
    assert success_doc(s3)["source_batches"] == {"lst": [], "ndvi": [], "buildings": []}
    assert "batch_id" in capsys.readouterr().err           # เตือนให้สร้างตารางใหม่


def test_trusted_batches_lists_every_distinct_batch():
    con = build_db()
    con.execute("UPDATE ronnai_trusted.lst SET batch_id = 'lst_2026_B' WHERE community_id = 'C0001'")
    found = vc.trusted_batches(FakeAthena(con), "ronnai_trusted", 2026)
    assert found["lst"] == ["lst_2026_20261111T1200", "lst_2026_B"]
    assert found["ndvi"] == [SOURCES["ndvi"]]


# ---------------------------------------------------------------- ไม่ผ่าน
def test_duplicate_key_fails(env, capsys):
    s3, _ = env
    con = build_db()
    con.execute("INSERT INTO community_heat SELECT * FROM community_heat WHERE community_id = 'C0001'")
    assert run(env, con) == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out and "key ซ้ำ 1+ ชุด" in out and "row count = 37" in out
    assert success_doc(s3) is None


def test_duplicate_key_alone_fails_even_when_row_count_matches(env, capsys):
    s3, _ = env
    con = build_db()
    con.execute("DELETE FROM community_heat WHERE community_id = 'C0036'")
    con.execute("INSERT INTO community_heat SELECT * FROM community_heat WHERE community_id = 'C0001'")
    assert run(env, con) == 1                                # 36 แถวพอดี แต่ C0001 ซ้ำ
    out = capsys.readouterr().out
    assert "key ซ้ำ 1+ ชุด" in out and "row count =" not in out
    assert success_doc(s3) is None


def test_missing_row_fails(env):
    s3, _ = env
    con = build_db()
    con.execute("DELETE FROM community_heat WHERE community_id = 'C0036'")
    assert run(env, con) == 1
    assert success_doc(s3) is None


def test_null_value_fails_coverage_but_passes_with_lower_threshold(env):
    s3, _ = env
    con = build_db()
    con.execute("UPDATE community_heat SET lst_mean_300 = NULL WHERE community_id = 'C0005'")
    assert run(env, con) == 1                               # 35/36 = 97.22% < 100
    assert success_doc(s3) is None
    assert run(env, con, "--min-coverage", "95") == 0
    assert success_doc(s3)["coverage_pct"] == 97.22


def test_expected_rows_option_overrides_csv(env):
    assert run(env, build_db(), "--expected-rows", "35", "--no-write") == 1


def test_failed_run_keeps_old_success_json_and_warns(env, capsys):
    s3, _ = env
    s3.put_object(Bucket=BUCKET, Key=KEY, Body=b'{"old": true}')
    con = build_db()
    con.execute("DELETE FROM community_heat WHERE community_id = 'C0036'")
    assert run(env, con) == 1
    assert "รอบก่อนยังอยู่" in capsys.readouterr().out
    assert success_doc(s3) == {"old": True}                  # ไม่ลบ ไม่เขียนทับ


# ---------------------------------------------------------------- error
def test_missing_config_is_error_exit_2(tmp_path, capsys):
    code = vc.main(["--config", str(tmp_path / "nope.yaml")], athena_factory=lambda: None)
    assert code == 2
    assert "ไม่พบ" in capsys.readouterr().err
