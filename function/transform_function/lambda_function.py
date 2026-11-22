"""Lambda 4: transform_batch  trusted (Parquet) -> curated (Iceberg) ด้วย Athena MERGE (sql/curated_merge.sql)

  1. ถ้า trusted ของปีนี้ยังไม่ครบทั้ง 3 แหล่ง (lst, ndvi, buildings) -> WAITING_FOR_SOURCES (ยังไม่ MERGE)
  2. รัน MSCK REPAIR TABLE ของตาราง trusted ทั้ง 3 ตาราง (ลงทะเบียน partition year=… ที่เพิ่งถูกเขียน ไม่งั้น Athena มองไม่เห็นข้อมูลใหม่)
  2b. (#33) ตรวจ zone_type ของ trusted ทั้ง 3 แหล่งในปีนี้: ต้องมีค่าเดียวกันและไม่ว่าง ไม่งั้นคืน ZONE_MISMATCH (ไม่ MERGE)
      กันกรณีข้อมูลเวอร์ชันจุดเดิมค้างอยู่ใน trusted แล้วอัป polygon มาแค่บางแหล่ง -> curated ปนสองเวอร์ชัน
  3. รันบล็อก [1] CREATE TABLE IF NOT EXISTS และบล็อก [2] MERGE INTO ของ sql/curated_merge.sql ผ่าน Athena
     MERGE รันซ้ำได้ผลเหมือนเดิม (ไม่สร้างแถวซ้ำ) จึงปลอดภัยเมื่อ Step Functions retry หลังล้มเหลวกลางทาง

input  : output ของ validate (status PASSED หรือ PARTIAL)  {"bucket", "source", "year", "batch_id"}
output : {"status": "TRANSFORMED" | "WAITING_FOR_SOURCES" | "ZONE_MISMATCH", "missing_sources": [...],
          "zone_types": {source: [zone_type, ...]}, "merge_query_id", ...}
error  : Athena query FAILED/CANCELLED/หมดเวลา -> raise

ตัวแปรสภาพแวดล้อม: BUCKET, ATHENA_WORKGROUP (ronnai-wg), CURATED_DB (ronnai_curated), TRUSTED_DB (ronnai_trusted),
                    TRUSTED_PREFIX (trusted/),
                    ATHENA_TIMEOUT_SEC (ค่าเริ่มต้น 600)
หมายเหตุ: curated_merge.sql ระบุปีเป็น `year = 2026` ฟังก์ชันนี้แทนค่านั้นด้วยปีของ event
"""
import os
import re
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE if (HERE / "sql").is_dir() else HERE.parents[1]

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


def _query_rows(client, sql, workgroup, database, timeout):
    """รัน SELECT แล้วคืน list ของ dict (ค่าเป็น string หรือ None)"""
    qid = _run_athena(client, sql, workgroup, database, timeout)
    rows = []
    for page in client.get_paginator("get_query_results").paginate(QueryExecutionId=qid):
        rows += page["ResultSet"]["Rows"]
    if not rows:
        return []
    header = [d.get("VarCharValue", "") for d in rows[0]["Data"]]
    return [dict(zip(header, [d.get("VarCharValue") for d in r["Data"]])) for r in rows[1:]]


def zone_type_sql(trusted_db, year):
    """zone_type ที่มีใน trusted ของปีนี้ แยกตามแหล่ง (NULL = แถวเก่าที่ไม่มีคอลัมน์ zone_type)"""
    return " UNION ALL ".join(
        f"SELECT '{s}' AS source, zone_type FROM {trusted_db}.{s} WHERE year = {int(year)} GROUP BY zone_type"
        for s in SOURCES)


def zone_problems(rows):
    """rows จาก zone_type_sql -> ({source: [zone_type,...]}, [ปัญหา])  ผ่านเมื่อทุกแหล่งมี zone_type ค่าเดียวกันค่าเดียว"""
    found = {s: [] for s in SOURCES}
    for r in rows:
        found.setdefault(r["source"], []).append(r["zone_type"])
    problems = []
    for s, zs in found.items():
        if any(z in (None, "") for z in zs):
            problems.append(f"{s}: มีแถวที่ไม่มี zone_type (ข้อมูลเวอร์ชันจุดเดิมค้างใน trusted)")
        if len({z for z in zs if z}) > 1:
            problems.append(f"{s}: มีหลาย zone_type {sorted(z for z in zs if z)}")
    all_z = {z for zs in found.values() for z in zs if z}
    if len(all_z) > 1:
        problems.append(f"แต่ละแหล่งใช้ zone_type ต่างกัน {dict((s, sorted(set(z for z in zs if z))) for s, zs in found.items())}")
    found = {s: sorted({z for z in zs if z}) for s, zs in found.items()}
    return found, problems


def lambda_handler(event, context=None):
    import boto3

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
    trusted_db = os.environ.get("TRUSTED_DB", "ronnai_trusted")
    for src in SOURCES:                                                   # ลงทะเบียน partition ใหม่ก่อน MERGE (ตารางต้องมีอยู่แล้ว)
        _run_athena(athena, f"MSCK REPAIR TABLE {trusted_db}.{src}", workgroup, trusted_db, timeout)
    zones, problems = zone_problems(_query_rows(athena, zone_type_sql(trusted_db, year), workgroup, trusted_db, timeout))
    if problems:
        print({"zone_mismatch": problems})
        return {**base, "status": "ZONE_MISMATCH", "missing_sources": [], "zone_types": zones, "problems": problems}
    create_id = _run_athena(athena, blocks[1], workgroup, database, timeout)
    merge_id = _run_athena(athena, blocks[2].replace("year = 2026", f"year = {year}"), workgroup, database, timeout)
    return {**base, "status": "TRANSFORMED", "missing_sources": [], "zone_types": zones,
            "repaired_tables": list(SOURCES), "create_query_id": create_id, "merge_query_id": merge_id}
