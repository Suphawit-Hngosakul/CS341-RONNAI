"""Lambda 1: check_batch  (ขั้นแรก เริ่มเมื่อมีไฟล์ใหม่ใน incoming/ ผ่าน EventBridge)  ไฟล์นี้เคยประมวลผลสำเร็จแล้วหรือยัง

หลักการ (Module 05 Part D): ถ้ามี _SUCCESS.json ของ batch ที่ใช้ไฟล์เนื้อหาเดียวกัน -> ALREADY_PROCESSED (หยุด)
  1. อ่านชื่อไฟล์ incoming/<source>_<year>.csv  เช่น incoming/lst_2026.csv  (source ต้องมี contracts/<source>.yaml)
  2. คำนวณ sha256 ของไฟล์
  3. ดู batch เดิมใน raw/source=<source>/ ที่ manifest มี sha256 เดียวกัน และมี _SUCCESS.json อยู่ใน batch นั้น
  4. ซ้ำ -> ย้ายไฟล์ไป quarantine/duplicate/<source>/<ชื่อไฟล์>_<เวลา UTC>/ พร้อม report.json (ลบออกจาก incoming/) แล้วหยุด
     ใหม่ -> ส่ง event BatchChecked ให้ ingest_batch (ดู ingestion/pipeline_events.py)

input  : {"bucket": "...", "key": "incoming/lst_2026.csv"}   (หรือ event ของ EventBridge: detail.bucket.name / detail.object.key)
output : {"status": "NEW" | "ALREADY_PROCESSED" | "REJECTED",
          "bucket", "key", "source", "year", "sha256",
          "processed_batch_id" + "quarantine_key" (เมื่อ ALREADY_PROCESSED), "reason" (เมื่อ REJECTED)}

ตัวแปรสภาพแวดล้อม: BUCKET (ไม่บังคับ ถ้า event มี bucket), INCOMING_PREFIX (ค่าเริ่มต้น incoming/), RAW_PREFIX (raw/),
                    QUARANTINE_PREFIX (quarantine/)
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE if (HERE / "contracts").is_dir() else HERE.parents[1]       # bundle แบนบน Lambda | โครง repo
for p in (HERE, HERE.parents[1] / "ingestion"):
    if p.is_dir():
        sys.path.insert(0, str(p))
import pipeline_events as pe  # noqa: E402

NAME_RE = re.compile(r"(?P<source>[a-z][a-z0-9_]*)_(?P<year>\d{4})\.csv")


def _bucket_key(event):
    if "detail" in event:                                               # EventBridge: Object Created
        return event["detail"]["bucket"]["name"], event["detail"]["object"]["key"]
    return event.get("bucket") or os.environ["BUCKET"], event["key"]


def _sha256(s3, bucket, key):
    h = hashlib.sha256()
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    for chunk in iter(lambda: body.read(1024 * 1024), b""):
        h.update(chunk)
    return h.hexdigest()


def _processed_batch(s3, bucket, raw_prefix, source, year, sha256):
    """batch ของ source/ปีนี้ ที่ไฟล์ sha256 ตรงกัน และมี _SUCCESS.json แล้ว (ไม่เจอ -> None)"""
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{raw_prefix}source={source}/"):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith("/manifest.json"):
                continue
            m = json.loads(s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read())
            if int(m.get("year", 0)) != year or not any(f.get("sha256") == sha256 for f in m.get("files", [])):
                continue
            marker = obj["Key"].rsplit("/", 1)[0] + "/_SUCCESS.json"
            try:
                s3.head_object(Bucket=bucket, Key=marker)
                return m["batch_id"]
            except s3.exceptions.ClientError:
                continue
    return None


def _quarantine_duplicate(s3, bucket, key, source, year, sha256, processed_batch_id, qprefix, now=None):
    """ย้ายไฟล์ซ้ำไป quarantine/duplicate/<source>/<stem>_<เวลา UTC>/ (copy -> ตรวจขนาด -> report.json -> ลบต้นทาง)"""
    now = now or datetime.now(timezone.utc)
    folder = f"{qprefix}duplicate/{source}/{Path(key).stem}_{now:%Y%m%dT%H%M%SZ}/"
    dst = folder + Path(key).name
    s3.copy_object(Bucket=bucket, Key=dst, CopySource={"Bucket": bucket, "Key": key})
    size_src = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
    size_dst = s3.head_object(Bucket=bucket, Key=dst)["ContentLength"]
    if size_src != size_dst:
        raise RuntimeError(f"ขนาดไฟล์ที่ย้ายไป quarantine ไม่ตรง ({size_dst} != {size_src}) ไม่ลบต้นทาง")
    report = {"status": "DUPLICATE", "reason": "ไฟล์เนื้อหาเดียวกันเคยประมวลผลสำเร็จแล้ว",
              "source": source, "year": year, "sha256": sha256, "original_key": key,
              "processed_batch_id": processed_batch_id, "moved_at": now.isoformat(timespec="seconds")}
    s3.put_object(Bucket=bucket, Key=folder + "report.json", ContentType="application/json",
                  Body=json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    s3.delete_object(Bucket=bucket, Key=key)
    return dst


def lambda_handler(event, context=None):
    import boto3

    bucket, key = _bucket_key(event)
    incoming = os.environ.get("INCOMING_PREFIX", "incoming/").rstrip("/") + "/"
    raw_prefix = os.environ.get("RAW_PREFIX", "raw/").rstrip("/") + "/"
    qprefix = os.environ.get("QUARANTINE_PREFIX", "quarantine/").rstrip("/") + "/"
    out = {"bucket": bucket, "key": key}

    name = key[len(incoming):] if key.startswith(incoming) else None
    m = NAME_RE.fullmatch(name) if name and "/" not in name else None
    if not m:
        return {**out, "status": "REJECTED",
                "reason": f"ชื่อไฟล์ต้องเป็น {incoming}<source>_<year>.csv (ได้ {key!r})"}
    source, year = m["source"], int(m["year"])
    if not (ROOT / "contracts" / f"{source}.yaml").is_file():
        return {**out, "status": "REJECTED", "reason": f"ไม่มี contracts/{source}.yaml"}

    s3 = boto3.client("s3")
    sha256 = _sha256(s3, bucket, key)
    out.update(source=source, year=year, sha256=sha256)
    done = _processed_batch(s3, bucket, raw_prefix, source, year, sha256)
    if done:
        qkey = _quarantine_duplicate(s3, bucket, key, source, year, sha256, done, qprefix)
        return {**out, "status": "ALREADY_PROCESSED", "processed_batch_id": done, "quarantine_key": qkey}
    result = {**out, "status": "NEW"}
    pe.emit("BatchChecked", result)
    return result
