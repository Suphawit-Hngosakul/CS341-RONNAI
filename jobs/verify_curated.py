"""Issue #16: ตรวจผลลัพธ์ตาราง curated แล้วสร้าง curated/_SUCCESS.json

query ตรวจ 3 อย่าง (ผ่าน Athena)
  1. row count   จำนวนแถวต้องเท่ากับที่คาด (นับจาก gee/communities.csv)
  2. key ไม่ซ้ำ  GROUP BY <key> HAVING count(*) > 1 ต้องไม่มีผล
  3. coverage    % ของแถวที่มีค่าครบทั้ง LST, NDVI, built-up (ค่าเริ่มต้นต้อง 100%)

ผ่านทั้งหมด -> เขียน s3://<bucket>/curated/_SUCCESS.json (batch_id, เวลา, row count, coverage ฯลฯ) exit 0
ไม่ผ่าน     -> พิมพ์เหตุผล ไม่เขียนไฟล์ exit 1     (เกิดข้อผิดพลาดของตัวสคริปต์/AWS = exit 2)

ตาราง (--table)
  community_heat   1 แถว = ชุมชน x ปี (buffer แยกเป็นคอลัมน์)  key (community_id, year)  คาด = จำนวนชุมชน

ตัวอย่าง (รันที่ root ของ repo ตั้งค่าจาก config/settings.yaml)
  python ingestion/verify_curated.py                         # ตาราง community_heat ปี 2026
  python ingestion/verify_curated.py --min-coverage 90 --no-write  # ลองดูผล ไม่เขียน _SUCCESS.json

หมายเหตุ: _SUCCESS.json มีไฟล์เดียวที่ curated/ (ตาม issue) ถ้าตรวจหลายตารางสลับกัน ไฟล์จะถูกเขียนทับด้วยผลล่าสุด
          (ฟิลด์ table บอกว่าเป็นผลของตารางไหน)  ถ้ารอบล่าสุดไม่ผ่าน ไฟล์เก่าจากรอบก่อนไม่ถูกลบ สคริปต์จะเตือน
"""
import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAIL, ERROR = 1, 2

LONG_COMPLETE = ["lst_mean", "ndvi_mean", "builtup_pct"]
WIDE_COMPLETE = [f"{c}_{b}" for c in LONG_COMPLETE for b in (300, 500)]
TABLES = {
    "community_heat": dict(key=["community_id", "year"], complete=WIDE_COMPLETE, per_buffer=False),
}


class VerifyError(Exception):
    pass


# ---------------------------------------------------------------- AWS
def make_session(cfg):
    """ใช้ profile ใน settings ถ้าใช้ได้จริง ถ้าไม่มี/ไม่มี credentials (เช่น CloudShell) ใช้ credentials ของ session แทน"""
    import boto3
    from botocore.exceptions import ProfileNotFound
    aws = cfg.get("aws") or {}
    region, profile = aws.get("region", "us-east-1"), aws.get("profile") or None
    if profile:
        try:
            session = boto3.Session(profile_name=profile, region_name=region)
            if session.get_credentials() is not None:
                return session
        except ProfileNotFound:
            pass
    return boto3.Session(region_name=region)


class Athena:
    def __init__(self, session, workgroup, database, timeout=180):
        self.c = session.client("athena")
        self.workgroup, self.database, self.timeout = workgroup, database, timeout

    def query(self, sql):
        """รัน SQL แล้วคืน list ของ dict (ค่าเป็น string)"""
        qid = self.c.start_query_execution(
            QueryString=sql, WorkGroup=self.workgroup,
            QueryExecutionContext={"Database": self.database})["QueryExecutionId"]
        t0 = time.time()
        while True:
            st = self.c.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
            if st["State"] == "SUCCEEDED":
                break
            if st["State"] in ("FAILED", "CANCELLED"):
                raise VerifyError(f"Athena query {st['State']}: {st.get('StateChangeReason', '')}")
            if time.time() - t0 > self.timeout:
                raise VerifyError(f"Athena query ใช้เวลาเกิน {self.timeout} วินาที (query id {qid})")
            time.sleep(1)
        rows = []
        for page in self.c.get_paginator("get_query_results").paginate(QueryExecutionId=qid):
            rows += page["ResultSet"]["Rows"]
        if not rows:
            return []
        header = [d.get("VarCharValue", "") for d in rows[0]["Data"]]
        return [dict(zip(header, [d.get("VarCharValue") for d in r["Data"]])) for r in rows[1:]]


# ---------------------------------------------------------------- ตรวจ
def expected_rows(table, repo=REPO):
    """จำนวนแถวที่คาด: ชุมชนใน gee/communities.csv (x จำนวน buffer จาก contracts/lst.yaml ถ้าเป็นตารางต่อ buffer)"""
    path = repo / "gee" / "communities.csv"
    if not path.exists():
        raise VerifyError(f"ไม่พบ {path} (ใช้ --expected-rows ระบุจำนวนแถวเอง)")
    with path.open(encoding="utf-8-sig", newline="") as f:
        n = len({r["community_id"].strip() for r in csv.DictReader(f) if r["community_id"].strip()})
    if TABLES[table]["per_buffer"]:
        import yaml
        lst = yaml.safe_load((repo / "contracts" / "lst.yaml").read_text(encoding="utf-8"))
        buffers = next(c for c in lst["columns"] if c["name"] == "buffer_m")["checks"][0]["values"]
        return n * len(buffers)
    return n


def run_checks(athena, table, year, expected, min_coverage):
    spec, tbl = TABLES[table], f"{table}"
    keys = ", ".join(spec["key"])
    cond = " AND ".join(f"{c} IS NOT NULL" for c in spec["complete"])
    where = f"WHERE year = {int(year)}"
    out, problems = {}, []

    # 1) row count
    n = int(athena.query(f"SELECT count(*) AS n FROM {tbl} {where}")[0]["n"])
    out["row_count"], out["expected_rows"] = n, expected
    if n != expected:
        problems.append(f"row count = {n} (คาด {expected})")

    # 2) key ไม่ซ้ำ
    dup = athena.query(f"SELECT {keys}, count(*) AS c FROM {tbl} {where} GROUP BY {keys} HAVING count(*) > 1 LIMIT 20")
    out["duplicate_keys"] = len(dup)
    if dup:
        problems.append(f"key ซ้ำ {len(dup)}+ ชุด เช่น {dup[:3]}")

    # 3) coverage
    r = athena.query(f"SELECT sum(CASE WHEN {cond} THEN 1 ELSE 0 END) AS complete, count(*) AS total FROM {tbl} {where}")[0]
    complete, total = int(r["complete"] or 0), int(r["total"] or 0)
    cov = round(100.0 * complete / total, 2) if total else 0.0
    out.update(complete_rows=complete, coverage_pct=cov, min_coverage_pct=min_coverage)
    if cov < min_coverage:
        problems.append(f"coverage = {cov}% (ต้อง >= {min_coverage}%)  ครบ {complete}/{total} แถว")
    return out, problems


def trusted_batches(athena, trusted_db, year):
    """batch_id ที่อยู่ในตาราง trusted ของปีนั้น (gate เขียนคอลัมน์ batch_id ลงทุกแถว)

    คืน {source: [batch_id, ...]} ถ้าตาราง trusted ยังไม่มีคอลัมน์ batch_id (สร้างด้วย DDL เก่า)
    หรือ query ไม่สำเร็จ จะได้ list ว่างของแหล่งนั้นพร้อมข้อความเตือน ไม่ทำให้การตรวจล้ม
    """
    found = {}
    for src in ("lst", "ndvi", "buildings"):
        try:
            rows = athena.query(
                f"SELECT DISTINCT batch_id FROM {trusted_db}.{src} "
                f"WHERE year = {int(year)} AND batch_id IS NOT NULL ORDER BY batch_id")
            found[src] = [r["batch_id"] for r in rows]
        except VerifyError as e:
            found[src] = []
            print(f"[warn] อ่าน batch_id ของ {trusted_db}.{src} ไม่ได้ ({e}) "
                  "ถ้าตารางยังไม่มีคอลัมน์ batch_id ให้สร้างตารางใหม่ด้วย sql/glue_trusted.sql",
                  file=sys.stderr)
    return found


# ---------------------------------------------------------------- main
def main(argv=None, athena_factory=None):
    ap = argparse.ArgumentParser(description="ตรวจตาราง curated แล้วสร้าง curated/_SUCCESS.json")
    ap.add_argument("--table", choices=sorted(TABLES), default="community_heat")
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--min-coverage", type=float, default=100.0, help="coverage ขั้นต่ำเป็น %% (ค่าเริ่มต้น 100)")
    ap.add_argument("--expected-rows", type=int, help="จำนวนแถวที่คาด (ค่าเริ่มต้น: คำนวณจาก gee/communities.csv)")
    ap.add_argument("--config", type=Path, default=REPO / "config" / "settings.yaml")
    ap.add_argument("--no-write", action="store_true", help="ตรวจอย่างเดียว ไม่เขียน _SUCCESS.json")
    args = ap.parse_args(argv)

    try:
        import yaml
        if not args.config.exists():
            raise VerifyError(f"ไม่พบ {args.config}  (สร้างด้วย infra/ronnai_aws.sh setup)")
        cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
        bucket, prefixes = cfg["s3"]["bucket"], cfg["s3"].get("prefixes") or {}
        curated_prefix = prefixes.get("curated", "curated/")
        trusted_db = cfg["glue"]["databases"]["trusted"]
        key = f"{curated_prefix}_SUCCESS.json"
        session = make_session(cfg) if athena_factory is None else None
        athena = (athena_factory or (lambda: Athena(session, cfg["athena"]["workgroup"],
                                                    cfg["glue"]["databases"]["curated"])))()
        expected = args.expected_rows if args.expected_rows is not None else expected_rows(args.table)

        print(f"[verify] ตาราง {args.table} ปี {args.year}  คาด {expected} แถว  coverage ขั้นต่ำ {args.min_coverage}%")
        result, problems = run_checks(athena, args.table, args.year, expected, args.min_coverage)
        print(f"  row count  : {result['row_count']} (คาด {expected})")
        print(f"  key ซ้ำ    : {result['duplicate_keys']} ชุด")
        print(f"  coverage   : {result['coverage_pct']}%  (ครบ {result['complete_rows']} แถว)")

        s3 = (session or make_session(cfg)).client("s3")
        if problems:
            print("[FAIL] ตรวจไม่ผ่าน ไม่เขียน _SUCCESS.json:")
            for p in problems:
                print("  -", p)
            try:
                s3.head_object(Bucket=bucket, Key=key)
                print(f"[warn] s3://{bucket}/{key} จากรอบก่อนยังอยู่ (ไม่ใช่ผลของรอบนี้) ตรวจเวลาในไฟล์ก่อนนำไปใช้")
            except Exception:
                pass
            return FAIL

        now = datetime.now(timezone.utc)
        doc = {
            "batch_id": f"curated_{now:%Y%m%dT%H%M%S}",
            "status": "SUCCESS",
            "checked_at": now.isoformat(timespec="seconds"),
            "table": args.table, "year": args.year,
            "row_count": result["row_count"], "expected_rows": expected,
            "duplicate_keys": 0,
            "coverage_pct": result["coverage_pct"], "complete_rows": result["complete_rows"],
            "source_batches": trusted_batches(athena, trusted_db, args.year),
        }
        if args.no_write:
            print("[PASS] ผ่านทั้งหมด (--no-write: ไม่ได้เขียนไฟล์)")
            print(json.dumps(doc, ensure_ascii=False, indent=2))
            return 0
        s3.put_object(Bucket=bucket, Key=key, ContentType="application/json",
                      Body=json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"))
        print(f"[PASS] ผ่านทั้งหมด -> s3://{bucket}/{key}")
        return 0
    except VerifyError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return ERROR
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}", file=sys.stderr)
        return ERROR


if __name__ == "__main__":
    sys.exit(main())
