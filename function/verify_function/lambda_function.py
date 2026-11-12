"""Lambda 5: verify_batch  ตรวจตาราง curated แล้วเขียน _SUCCESS.json (ใช้ตรรกะของ ingestion/verify_curated.py)

ตรวจ 3 อย่าง (ผ่าน Athena): จำนวนแถว = จำนวนชุมชนใน gee/communities.csv, key ไม่ซ้ำ, coverage ครบตามเกณฑ์
ผ่าน -> เขียน
  - s3://<bucket>/curated/_SUCCESS.json  (ผลของ curated ทั้งตาราง: row_count, coverage, source_batches ฯลฯ)
  - s3://<bucket>/raw/source=<src>/batch_id=<id>/_SUCCESS.json  ของทุก batch ที่อยู่ใน trusted ตอนนี้ (ใช้ batch_id ในตาราง)
    check_batch ใช้ไฟล์หลังนี้ตัดสินว่า batch เคยสำเร็จแล้ว (ALREADY_PROCESSED)
ไม่ผ่าน -> VERIFY_FAILED ไม่เขียนอะไรเลย (ข้อมูลที่ MERGE ไปแล้วคงอยู่ รันซ้ำได้เพราะ MERGE ไม่สร้างแถวซ้ำ)

input  : output ของ transform (status TRANSFORMED)  {"bucket", "source", "year", "batch_id"}
output : {"status": "SUCCESS" | "VERIFY_FAILED", "row_count", "expected_rows", "duplicate_keys", "coverage_pct",
          "problems": [...] (เมื่อไม่ผ่าน), "success_key", "marked_batches": [...]}

ตัวแปรสภาพแวดล้อม: BUCKET, ATHENA_WORKGROUP (ronnai-wg), TRUSTED_DB (ronnai_trusted), CURATED_DB (ronnai_curated),
                    CURATED_PREFIX (curated/), RAW_PREFIX (raw/), MIN_COVERAGE (100)
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE if (HERE / "gee").is_dir() else HERE.parents[1]
for p in (HERE, HERE.parents[1] / "ingestion"):
    if p.is_dir():
        sys.path.insert(0, str(p))
import verify_curated as vc  # noqa: E402

TABLE = "community_heat"


def _make_athena():
    import boto3
    return vc.Athena(boto3.Session(), os.environ.get("ATHENA_WORKGROUP", "ronnai-wg"),
                     os.environ.get("CURATED_DB", "ronnai_curated"))


def lambda_handler(event, context=None):
    import boto3

    bucket, year = event.get("bucket") or os.environ["BUCKET"], int(event["year"])
    curated = os.environ.get("CURATED_PREFIX", "curated/").rstrip("/") + "/"
    raw = os.environ.get("RAW_PREFIX", "raw/").rstrip("/") + "/"
    s3 = boto3.client("s3")
    athena = _make_athena()

    expected = vc.expected_rows(TABLE, repo=ROOT)
    result, problems = vc.run_checks(athena, TABLE, year, expected, float(os.environ.get("MIN_COVERAGE", "100")))
    base = {"bucket": bucket, "year": year, "row_count": result["row_count"], "expected_rows": expected,
            "duplicate_keys": result["duplicate_keys"], "coverage_pct": result["coverage_pct"]}
    if problems:
        return {**base, "status": "VERIFY_FAILED", "problems": problems}

    now = datetime.now(timezone.utc)
    batches = vc.trusted_batches(athena, os.environ.get("TRUSTED_DB", "ronnai_trusted"), year)
    doc = {"batch_id": f"curated_{now:%Y%m%dT%H%M%S}", "status": "SUCCESS",
           "checked_at": now.isoformat(timespec="seconds"), "table": TABLE, "year": year,
           "row_count": result["row_count"], "expected_rows": expected, "duplicate_keys": 0,
           "coverage_pct": result["coverage_pct"], "complete_rows": result["complete_rows"],
           "source_batches": batches}
    success_key = f"{curated}_SUCCESS.json"
    s3.put_object(Bucket=bucket, Key=success_key, ContentType="application/json",
                  Body=json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"))

    wanted = {(s, b) for s, ids in batches.items() for b in ids}
    if event.get("source") and event.get("batch_id"):
        wanted.add((event["source"], event["batch_id"]))                  # batch ของ execution นี้ (กรณีตารางไม่มี batch_id)
    marked = []
    for src, batch_id in sorted(wanted):
        prefix = f"{raw}source={src}/batch_id={batch_id}/"
        try:
            s3.head_object(Bucket=bucket, Key=prefix + "manifest.json")   # ทำเครื่องหมายเฉพาะ batch ที่มีใน raw จริง
        except s3.exceptions.ClientError:
            continue
        marker = {"batch_id": batch_id, "status": "SUCCESS", "curated_batch_id": doc["batch_id"],
                  "checked_at": doc["checked_at"]}
        s3.put_object(Bucket=bucket, Key=prefix + "_SUCCESS.json", ContentType="application/json",
                      Body=json.dumps(marker, ensure_ascii=False, indent=2).encode("utf-8"))
        marked.append(batch_id)
    return {**base, "status": "SUCCESS", "success_key": success_key, "marked_batches": marked}
