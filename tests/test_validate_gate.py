"""ทดสอบ ingestion/validate_gate.py (Validation gate)

รัน:  pip install -r requirements-dev.txt
      python -m pytest tests -q

ใช้ contracts/*.yaml และ gee/communities.csv ของจริงใน repo
ข้อมูลจริง (data/gee_2026/*.csv) ไม่อยู่ใน repo ถ้าไม่มีไฟล์ test นั้นจะถูกข้าม
"""
import io
import json
import sys
from pathlib import Path

import boto3
import pandas as pd
import pytest
from moto import mock_aws

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingestion"))
import ingest  # noqa: E402
import validate_gate as gate  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
DATA = ROOT / "data" / "gee_2026"
CFG = {"s3": {"bucket": "cs341-ronnai-test", "prefixes": {}}}

LST_HEADER = "community_id,buffer_m,year,lst_mean,lst_p90,valid_pixel_ratio,n_scenes"
GOOD_LST = [
    "C0001,300,2026,48.2,50.1,1.0,11",
    "C0001,500,2026,47.9,49.8,1.0,11",
    "C0002,300,2026,46.3,49.6,1.0,11",
    "C0002,500,2026,46.0,49.1,1.0,11",
]


def csv(header, rows, bom=False):
    text = "\n".join([header, *rows]) + "\n"
    return (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")


def run(source, data, year=2026, batch_id="t_batch"):
    contract = gate.load_contract(source)
    return gate.run_gate(data, contract, batch_id=batch_id, year=year,
                         master=gate.load_master_list(contract))


def rules(result):
    return {v["rule"]: v["rows"] for v in result.report["violations"]}


def assert_no_rows_lost(result):
    r = result.report
    assert r["passed"] + r["quarantined"] == r["total_rows"]
    assert len(result.trusted) == r["passed"]
    if not result.blocked_batch:
        assert len(result.quarantined) == r["quarantined"]


# ---------------------------------------------------------------- STANDARDIZE

def test_clean_file_passes():
    r = run("lst", csv(LST_HEADER, GOOD_LST))
    assert r.report["status"] == "PASSED" and r.report["passed"] == 4
    assert_no_rows_lost(r)


def test_standardize_bom_whitespace_and_header_names():
    header = "﻿ Community_ID ,buffer_m,year,LST_mean,lst_p90,valid pixel ratio,n_scenes"
    rows = ["  C0001 , 300 ,2026, 48.2 ,50.1,1.0,11", *GOOD_LST[1:]]
    r = run("lst", csv(header, rows))
    assert r.report["status"] == "PASSED", r.report
    assert r.trusted.loc[0, "community_id"] == "C0001"
    assert r.trusted.loc[0, "buffer_m"] == 300
    std = r.report["standardized"]
    assert std["normalize_column_names"] >= 3 and std["trim_whitespace"] >= 3


def test_building_count_float_string_becomes_int():
    header = "community_id,buffer_m,year,builtup_pct,building_count"
    r = run("buildings", csv(header, ["C0001,300,2026,36.3,751.0", "C0001,500,2026,30.1,",
                                      "C0002,300,2026,27.6,0.0"]))
    assert r.report["passed"] == 3
    assert str(r.trusted["building_count"].dtype) == "Int64"
    assert r.trusted["building_count"].tolist()[0] == 751
    assert pd.isna(r.trusted["building_count"].tolist()[1])
    assert r.report["standardized"]["cast building_count float->int"] == 2
    warns = {w["rule"]: w["rows"] for w in r.report["warnings"]}
    assert warns == {"building_count != 0": 1, "building_count not null": 1}


def test_non_integral_int_is_blocked():
    header = "community_id,buffer_m,year,builtup_pct,building_count"
    r = run("buildings", csv(header, ["C0001,300,2026,36.3,751.5", "C0002,300,2026,27.6,509"]))
    assert rules(r) == {"building_count is int": 1}
    assert r.report["passed"] == 1


# ---------------------------------------------------------------- BLOCK แถว

def test_lst_120_goes_to_quarantine():
    rows = [*GOOD_LST, "C0014,300,2026,120.4,121.0,1.0,11"]
    r = run("lst", csv(LST_HEADER, rows))
    assert r.report["status"] == "PARTIAL"
    assert rules(r) == {"lst_mean between 15 and 70": 1, "lst_p90 between 15 and 70": 1}
    v = r.report["violations"][0]
    assert v["example"]["community_id"] == "C0014" and v["example"]["lst_mean"] == 120.4
    assert r.report["quarantined"] == 1           # แถวเดียวผิด 2 กฎ นับเป็น 1 แถว
    assert r.quarantined.loc[0, "_violations"] == "lst_mean between 15 and 70; lst_p90 between 15 and 70"
    assert_no_rows_lost(r)


@pytest.mark.parametrize("row, rule", [
    ("C9999,300,2026,48.2,50.1,1.0,11", "community_id in master list"),
    ("C0003,400,2026,48.2,50.1,1.0,11", "buffer_m in [300, 500]"),
    ("C0003,300,2026,abc,50.1,1.0,11", "lst_mean is float"),
    ("C0003,300,2026,,50.1,1.0,11", "lst_mean not null"),
    ("C0003,300,2026,48.2,50.1,1.5,11", "valid_pixel_ratio between 0 and 1"),
    ("C0003,300,2026,48.2,50.1,1.0,-1", "n_scenes >= 0"),
    ("C0003,300,2025,48.2,50.1,1.0,11", "year == 2026"),
])
def test_block_rules(row, rule):
    r = run("lst", csv(LST_HEADER, [*GOOD_LST, row]))
    assert rule in rules(r), r.report["violations"]
    assert r.report["passed"] == 4 and r.report["quarantined"] == 1
    assert_no_rows_lost(r)


def test_duplicate_key_quarantines_all_copies():
    rows = [*GOOD_LST, "C0001,300,2026,40.0,45.0,1.0,11"]
    r = run("lst", csv(LST_HEADER, rows))
    assert rules(r) == {"unique key (community_id, buffer_m, year)": 2}
    assert r.report["passed"] == 3
    assert_no_rows_lost(r)


def test_ndvi_out_of_range():
    header = "community_id,buffer_m,year,ndvi_mean,green_ratio,valid_pixel_ratio,n_scenes"
    r = run("ndvi", csv(header, ["C0001,300,2026,0.2,0.24,1.0,23", "C0002,300,2026,1.7,0.4,1.0,23"]))
    assert rules(r) == {"ndvi_mean between -1 and 1": 1}


# ---------------------------------------------------------------- WARN

def test_low_valid_pixel_ratio_is_warn_only():
    r = run("lst", csv(LST_HEADER, [*GOOD_LST, "C0003,300,2026,48.2,50.1,0.2,11"]))
    assert r.report["quarantined"] == 0 and r.report["passed"] == 5
    assert r.report["warnings"][0]["rule"] == "valid_pixel_ratio >= 0.3"


def test_extra_column_is_warn_and_dropped():
    r = run("lst", csv(LST_HEADER + ",note", [g + ",x" for g in GOOD_LST]))
    assert r.report["passed"] == 4
    assert "note" not in r.trusted.columns
    assert r.report["warnings"][0]["rule"] == "no unexpected columns"


# ---------------------------------------------------------------- BLOCK ทั้ง batch

def test_missing_column_blocks_whole_batch():
    r = run("lst", (FIXTURES / "lst_2026_missing_column.csv").read_bytes())
    assert r.blocked_batch and r.report["status"] == "BATCH_BLOCKED"
    assert r.report["passed"] == 0 and r.report["quarantined"] == r.report["total_rows"] == 5
    assert "lst_p90" in r.report["violations"][0]["detail"]


def test_empty_file_blocks_whole_batch():
    r = run("lst", csv(LST_HEADER, []))
    assert r.blocked_batch and r.report["violations"][0]["rule"] == "row_count >= 1"


# ---------------------------------------------------------------- เขียนผล (local store)

def gate_local(tmp_path, source, data, batch_id, name="f.csv"):
    store = gate.LocalStore(tmp_path / "lake")
    return store, gate.gate_batch(store, {}, source=source, year=2026, batch_id=batch_id,
                                  data=data, file_name=name, log_dir=tmp_path / "logs")


def test_demo_bad_file_end_to_end(tmp_path):
    """ไฟล์ที่ตั้งใจให้ผิด (ใช้ตอน demo): LST = 120.4, ชุมชนไม่มีจริง, buffer 400"""
    data = (FIXTURES / "lst_2026_bad_demo.csv").read_bytes()
    store, rep = gate_local(tmp_path, "lst", data, "lst_2026_20261111T1200")
    assert rep["total_rows"] == 74 and rep["passed"] == 71 and rep["quarantined"] == 3
    assert {v["rule"] for v in rep["violations"]} == {
        "lst_mean between 15 and 70", "community_id in master list", "buffer_m in [300, 500]"}
    assert rep["standardized"]["trim_whitespace"] == 1

    qdir = tmp_path / "lake" / "quarantine" / "lst" / "lst_2026_20261111T1200"
    report = json.loads((qdir / "report.json").read_text(encoding="utf-8"))
    assert report["quarantined"] == 3
    ex = next(v for v in report["violations"] if v["rule"].startswith("lst_mean"))["example"]
    assert ex["community_id"] == "C0014" and ex["lst_mean"] == 120.4
    bad = pd.read_csv(qdir / "rows.csv")
    assert len(bad) == 3 and 120.4 in bad["lst_mean"].tolist()

    trusted = pd.read_parquet(tmp_path / "lake" / "trusted" / "lst" / "year=2026" / "lst.parquet")
    assert len(trusted) == 71 and trusted["lst_mean"].max() <= 70
    assert "year" not in trusted.columns and set(trusted["batch_id"]) == {"lst_2026_20261111T1200"}
    assert (tmp_path / "logs" / "lst_2026_20261111T1200.json").exists()


def test_quarantine_is_never_overwritten(tmp_path):
    data = (FIXTURES / "lst_2026_bad_demo.csv").read_bytes()
    store, _ = gate_local(tmp_path, "lst", data, "b1")
    report_path = tmp_path / "lake" / "quarantine" / "lst" / "b1" / "report.json"
    before = report_path.read_bytes()
    with pytest.raises(gate.GateError) as e:
        gate_local(tmp_path, "lst", csv(LST_HEADER, GOOD_LST), "b1")
    assert e.value.code == gate.EXIT_ALREADY_DONE
    assert report_path.read_bytes() == before


def test_blocked_batch_keeps_original_and_leaves_trusted(tmp_path):
    gate_local(tmp_path, "lst", csv(LST_HEADER, GOOD_LST), "good")
    tpath = tmp_path / "lake" / "trusted" / "lst" / "year=2026" / "lst.parquet"
    before = tpath.read_bytes()
    data = (FIXTURES / "lst_2026_missing_column.csv").read_bytes()
    _, rep = gate_local(tmp_path, "lst", data, "bad", name="lst_2026_missing_column.csv")
    assert rep["status"] == "BATCH_BLOCKED"
    assert (tmp_path / "lake" / "quarantine" / "lst" / "bad" / "lst_2026_missing_column.csv").read_bytes() == data
    assert (tmp_path / "lake" / "quarantine" / "lst" / "bad" / "report.json").exists()
    assert tpath.read_bytes() == before


def test_trusted_upsert_is_idempotent_and_keeps_last_good(tmp_path):
    gate_local(tmp_path, "lst", csv(LST_HEADER, GOOD_LST), "b1")
    gate_local(tmp_path, "lst", csv(LST_HEADER, GOOD_LST), "b2")   # รันซ้ำ: ไม่เกิดแถวซ้ำ
    tpath = tmp_path / "lake" / "trusted" / "lst" / "year=2026" / "lst.parquet"
    assert len(pd.read_parquet(tpath)) == 4
    # batch ใหม่: C0001/300 ผิด -> ค่าเดิมใน trusted ยังอยู่, C0002/300 ถูกแก้ค่า
    gate_local(tmp_path, "lst", csv(LST_HEADER, ["C0001,300,2026,120,50,1.0,11",
                                                 "C0002,300,2026,44.4,49.6,1.0,11"]), "b3")
    t = pd.read_parquet(tpath).set_index(["community_id", "buffer_m"])
    assert len(t) == 4
    assert t.loc[("C0001", 300), "lst_mean"] == 48.2
    assert t.loc[("C0002", 300), "lst_mean"] == 44.4 and t.loc[("C0002", 300), "batch_id"] == "b3"


# ---------------------------------------------------------------- ข้อมูลจริง 3 แหล่ง

@pytest.mark.parametrize("source", ["lst", "ndvi", "buildings"])
def test_real_data_passes(tmp_path, source):
    path = DATA / f"{source}_2026.csv"
    if not path.exists():
        pytest.skip(f"ไม่มี {path} (ข้อมูลจริงไม่อยู่ใน repo)")
    _, rep = gate_local(tmp_path, source, path.read_bytes(), f"{source}_2026_20261111T1200")
    assert rep["passed"] + rep["quarantined"] == rep["total_rows"] == 72
    assert rep["quarantined"] == 0, rep["violations"]


# ---------------------------------------------------------------- S3 (moto) + CLI

@pytest.fixture
def s3(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x",
                 "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=CFG["s3"]["bucket"])
        yield client


def test_cli_s3_ingest_then_gate(s3, tmp_path, monkeypatch, capsys):
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone(timedelta(hours=7)))
    bad = FIXTURES / "lst_2026_bad_demo.csv"
    ingest.ingest(s3, CFG, file_path=bad, source="lst", year=2026, now=now)

    cfg_path = tmp_path / "settings.yaml"
    cfg_path.write_text(f"aws:\n  region: us-east-1\ns3:\n  bucket: {CFG['s3']['bucket']}\n", encoding="utf-8")
    monkeypatch.setattr(ingest, "make_s3_client", lambda cfg: s3)
    argv = ["--source", "lst", "--latest", "--config", str(cfg_path), "--log-dir", str(tmp_path / "logs")]
    assert gate.main(argv) == gate.EXIT_OK
    assert "120.4" in capsys.readouterr().out

    b = CFG["s3"]["bucket"]
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=b)["Contents"])
    assert "trusted/lst/year=2026/lst.parquet" in keys
    assert "quarantine/lst/lst_2026_20261111T1200/report.json" in keys
    assert "quarantine/lst/lst_2026_20261111T1200/rows.csv" in keys
    t = pd.read_parquet(io.BytesIO(s3.get_object(Bucket=b, Key="trusted/lst/year=2026/lst.parquet")["Body"].read()))
    assert len(t) == 71

    # รันซ้ำ batch เดิม -> หยุด ไม่เขียนทับ quarantine
    assert gate.main(argv) == gate.EXIT_ALREADY_DONE


def test_cli_local_mode(tmp_path):
    argv = ["--local", "--file", str(FIXTURES / "lst_2026_missing_column.csv"), "--source", "lst",
            "--year", "2026", "--local-root", str(tmp_path / "lake"), "--log-dir", str(tmp_path / "logs")]
    assert gate.main(argv) == gate.EXIT_BATCH_BLOCKED
    assert gate.main(argv) == gate.EXIT_BATCH_BLOCKED   # batch_id ใหม่อัตโนมัติ ไม่ชนของเดิม
    assert len(list((tmp_path / "lake" / "quarantine" / "lst").iterdir())) == 2
