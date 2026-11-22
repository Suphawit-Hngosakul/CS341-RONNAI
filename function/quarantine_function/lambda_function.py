"""Lambda: quarantine_file  (Issue #32) ย้ายไฟล์ที่ไม่ควรเข้า pipeline จาก incoming/ ไป quarantine/ ที่เดียว

เดิมการกักไฟล์ซ้ำทำอยู่ใน check_batch และไฟล์ชื่อผิดถูกทิ้งค้างใน incoming/ ตอนนี้แยกมาเป็น Lambda ตัวนี้
ให้ state machine เรียกหลัง CheckBatch เมื่อ status ไม่ใช่ NEW  จะเพิ่มเหตุผลการกักแบบใหม่ก็แก้ที่นี่ที่เดียว (KINDS)

  ALREADY_PROCESSED -> quarantine/duplicate/<source>/<ชื่อไฟล์>_<เวลา UTC>/   ไฟล์เนื้อหาเดียวกันเคยสำเร็จแล้ว
  REJECTED          -> quarantine/rejected/<ชื่อไฟล์>_<เวลา UTC>/            ชื่อไฟล์ผิด หรือไม่มี contract
ขั้นตอน: copy -> ตรวจขนาดตรงกัน -> เขียน report.json -> ลบต้นทาง (ขนาดไม่ตรง = raise ไม่ลบต้นทาง)

ไม่เกี่ยวกับการกักแถว/ทั้ง batch ของ ValidateBatch (quarantine/<source>/<batch_id>/) ซึ่งยังทำใน jobs/validate_gate.py
เพราะ CLI ใช้ตัวเดียวกัน

input  : output ของ check_batch  {"status", "bucket", "key", "source"?, "year"?, "sha256"?,
                                   "processed_batch_id"? (ALREADY_PROCESSED), "reason"? (REJECTED)}
output : input เดิม + {"quarantine_kind", "quarantine_key", "quarantine_report"}
  ไฟล์ต้นทางหายไปแล้ว (เช่น retry หลังลบสำเร็จ หรือมีคนลบเอง) -> quarantine_key = null ไม่ error
  status ที่ไม่รู้จัก -> raise QuarantineError

ตัวแปรสภาพแวดล้อม: BUCKET (ไม่บังคับ ถ้า event มี bucket), QUARANTINE_PREFIX (ค่าเริ่มต้น quarantine/)
"""
import json
import os
from datetime import datetime, timezone
from pathlib import PurePosixPath

KINDS = {   # status ของ check_batch -> (โฟลเดอร์ใน quarantine/, แยกโฟลเดอร์ตาม source ไหม, เหตุผลใน report.json)
    "ALREADY_PROCESSED": ("duplicate", True, "ไฟล์เนื้อหาเดียวกันเคยประมวลผลสำเร็จแล้ว"),
    "REJECTED": ("rejected", False, "ชื่อไฟล์ไม่ตรงรูปแบบ incoming/<source>_<year>.csv หรือไม่มี contract"),
}
REPORT_FIELDS = ("source", "year", "sha256", "processed_batch_id")


class QuarantineError(Exception):
    pass


def quarantine_folder(qprefix, status, key, source=None, now=None):
    """โฟลเดอร์ปลายทางใน quarantine/ ของไฟล์นี้ (ลงท้ายด้วย /)"""
    if status not in KINDS:
        raise QuarantineError(f"ไม่รู้จัก status {status!r} (รองรับ {sorted(KINDS)})")
    kind, by_source, _ = KINDS[status]
    now = now or datetime.now(timezone.utc)
    parts = [qprefix.rstrip("/"), kind] + ([source] if by_source and source else [])
    return "/".join(parts) + f"/{PurePosixPath(key).stem}_{now:%Y%m%dT%H%M%SZ}/"


def _exists(s3, bucket, key):
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except s3.exceptions.ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def quarantine_file(s3, bucket, event, qprefix="quarantine/", now=None):
    """ย้าย event["key"] ไป quarantine/ ตาม event["status"] คืน dict ที่จะต่อท้าย output"""
    status, key = event.get("status"), event["key"]
    now = now or datetime.now(timezone.utc)
    folder = quarantine_folder(qprefix, status, key, event.get("source"), now)
    kind, _, reason = KINDS[status]
    out = {"quarantine_kind": kind, "quarantine_key": None, "quarantine_report": None}
    if not _exists(s3, bucket, key):
        print({"quarantine_skip": f"ไม่พบ s3://{bucket}/{key} (อาจถูกย้าย/ลบไปแล้ว)"})
        return out

    dst = folder + PurePosixPath(key).name
    s3.copy_object(Bucket=bucket, Key=dst, CopySource={"Bucket": bucket, "Key": key})
    size_src = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
    size_dst = s3.head_object(Bucket=bucket, Key=dst)["ContentLength"]
    if size_src != size_dst:
        raise QuarantineError(f"ขนาดไฟล์ที่ย้ายไป quarantine ไม่ตรง ({size_dst} != {size_src}) ไม่ลบต้นทาง")
    report = {"status": kind.upper(), "reason": event.get("reason") or reason, "original_key": key,
              **{f: event[f] for f in REPORT_FIELDS if event.get(f) is not None},
              "moved_at": now.isoformat(timespec="seconds")}
    s3.put_object(Bucket=bucket, Key=folder + "report.json", ContentType="application/json",
                  Body=json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    s3.delete_object(Bucket=bucket, Key=key)
    return {**out, "quarantine_key": dst, "quarantine_report": folder + "report.json"}


def lambda_handler(event, context=None):
    import boto3

    bucket = event.get("bucket") or os.environ["BUCKET"]
    qprefix = os.environ.get("QUARANTINE_PREFIX", "quarantine/").rstrip("/") + "/"
    return {**event, "bucket": bucket, **quarantine_file(boto3.client("s3"), bucket, event, qprefix)}
