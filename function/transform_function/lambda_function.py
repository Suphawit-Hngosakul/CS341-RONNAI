"""Lambda 4: transform_batch  trusted (Parquet) -> curated (Iceberg) ด้วย Athena MERGE (sql/curated_merge.sql)

  1. ถ้า trusted ของปีนี้ยังไม่ครบทั้ง 3 แหล่ง (lst, ndvi, buildings) -> WAITING_FOR_SOURCES (ยังไม่ MERGE)
  2. รันบล็อก [1] CREATE TABLE IF NOT EXISTS และบล็อก [2] MERGE INTO ของ sql/curated_merge.sql ผ่าน Athena
     MERGE รันซ้ำได้ผลเหมือนเดิม (ไม่สร้างแถวซ้ำ) จึงปลอดภัยเมื่อ Step Functions retry หลังล้มเหลวกลางทาง

input  : output ของ validate (status PASSED หรือ PARTIAL)  {"bucket", "source", "year", "batch_id"}
output : {"status": "TRANSFORMED" | "WAITING_FOR_SOURCES", "missing_sources": [...], "merge_query_id", ...}
error  : Athena query FAILED/CANCELLED/หมดเวลา -> raise

ตัวแปรสภาพแวดล้อม: BUCKET, ATHENA_WORKGROUP (ronnai-wg), CURATED_DB (ronnai_curated), TRUSTED_PREFIX (trusted/),
                    ATHENA_TIMEOUT_SEC (ค่าเริ่มต้น 600)
หมายเหตุ: curated_merge.sql ระบุปีเป็น `year = 2026` ฟังก์ชันนี้แทนค่านั้นด้วยปีของ event
"""
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE if (HERE / "sql").is_dir() else HERE.parents[1]
for p in (HERE, HERE.parents[1] / "ingestion"):
    if p.is_dir():
        sys.path.insert(0, str(p))
import pipeline_events as pe  # noqa: E402

SQL_FILE = ROOT / "sql" / "curated_merge.sql"
SOURCES = ("lst", "ndvi", "buildings")
BLOCK_RE = re.compile(r"^-- =+ \[(\d+)\]", re.M)


class TransformError(Exception):
    pass


def sql_blocks(text):
    """แยกไฟล์ SQL ตามหัวบล็อก `-- ==== [n] ...` คืน {n: statement เดียว (ตัด ; ท้าย)}"""
    marks = list(BLOCK_RE.finditer(text))
    out = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].split("\n", 1)[1] if "\n" in text[m.end():end] else ""
        out[int(m[1])] = body.strip().rstrip(";").strip()
    return out


def _has_objects(s3, bucket, prefix):
    return s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1).get("KeyCount", 0) > 0


def _run_athena(client, sql, workgroup, database, timeout):
    qid = client.start_query_execution(QueryString=sql, WorkGroup=workgroup,
                                       QueryExecutionContext={"Database": database})["QueryExecutionId"]
    t0 = time.time()
    while True:
        st = client.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if st["State"] == "SUCCEEDED":
            return qid
        if st["State"] in ("FAILED", "CANCELLED"):
            raise TransformError(f"Athena {st['State']}: {st.get('StateChangeReason', '')} (query {qid})")
        if time.time() - t0 > timeout:
            raise TransformError(f"Athena ใช้เวลาเกิน {timeout} วินาที (query {qid})")
        time.sleep(2)


def lambda_handler(event, context=None):
    import boto3

    event = pe.payload(event)                                             # รับ event จาก EventBridge (BatchValidated)
    bucket = event.get("bucket") or os.environ["BUCKET"]
    year = int(event["year"])
    trusted = os.environ.get("TRUSTED_PREFIX", "trusted/").rstrip("/") + "/"
    s3 = boto3.client("s3")
    missing = [s for s in SOURCES if not _has_objects(s3, bucket, f"{trusted}{s}/year={year}/")]
    base = {"bucket": bucket, "year": year, "source": event.get("source"), "batch_id": event.get("batch_id")}
    if missing:
        return {**base, "status": "WAITING_FOR_SOURCES", "missing_sources": missing}

    blocks = sql_blocks(SQL_FILE.read_text(encoding="utf-8"))
    if 1 not in blocks or 2 not in blocks:
        raise TransformError(f"{SQL_FILE} ไม่มีบล็อก [1] และ [2]")
    athena = boto3.client("athena")
    workgroup = os.environ.get("ATHENA_WORKGROUP", "ronnai-wg")
    database = os.environ.get("CURATED_DB", "ronnai_curated")
    timeout = int(os.environ.get("ATHENA_TIMEOUT_SEC", "600"))
    create_id = _run_athena(athena, blocks[1], workgroup, database, timeout)
    merge_id = _run_athena(athena, blocks[2].replace("year = 2026", f"year = {year}"), workgroup, database, timeout)
    result = {**base, "status": "TRANSFORMED", "missing_sources": [],
              "create_query_id": create_id, "merge_query_id": merge_id}
    pe.emit("CuratedTransformed", result)
    return result
