"""Lambda 2: ingest  incoming/<source>_<year>.csv -> raw/source=<src>/batch_id=<id>/ + manifest.json

ใช้ฟังก์ชันของ ingestion/ingest.py (batch_id, file_info, manifest) ผลลัพธ์ในรูปแบบเดียวกับ ingest.py ที่รันมือ
  - batch_id = <source>_<year>_<YYYYMMDDTHHMM> (เวลาไทย)
  - คัดลอกไฟล์ incoming -> raw (ไม่ลบไฟล์ใน incoming) แล้วเขียน manifest.json (มี sha256, file_name, raw_key)
  - รันซ้ำ (Step Functions retry) ใน batch_id เดิมที่ manifest sha256 ตรงกัน -> ใช้ batch เดิม ไม่เกิด batch ซ้ำ

input  : output ของ check_batch (status NEW)  {"bucket", "key", "source", "year", "sha256"}
output : {"status": "INGESTED", "bucket", "source", "year", "batch_id", "raw_key", "sha256"}
error  : ไฟล์ใน incoming เปลี่ยนไประหว่างขั้น (sha256 ไม่ตรง), batch_id ชนกับไฟล์อื่น -> raise (ให้ Step Functions จับด้วย Catch)

ตัวแปรสภาพแวดล้อม: RAW_PREFIX (ค่าเริ่มต้น raw/)
"""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
for p in (HERE, HERE.parents[1] / "ingestion"):                          # bundle แบน | โครง repo
    if p.is_dir():
        sys.path.insert(0, str(p))
import ingest  # noqa: E402
import pipeline_events as pe  # noqa: E402


class IngestFunctionError(Exception):
    pass


def lambda_handler(event, context=None):
    import boto3

    os.environ.setdefault("LOGNAME", "lambda")                           # build_manifest เรียก getpass.getuser()
    event = pe.payload(event)                                             # รับ event จาก EventBridge (BatchChecked)
    bucket, key = event["bucket"], event["key"]
    source = ingest.validate_source(event["source"])
    year = ingest.validate_year(int(event["year"]))
    raw_zone = os.environ.get("RAW_PREFIX", "raw/").rstrip("/") + "/"
    s3 = boto3.client("s3")

    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / Path(key).name
        s3.download_file(bucket, key, str(local))
        info = ingest.file_info(local)
    if event.get("sha256") and info["sha256"] != event["sha256"]:
        raise IngestFunctionError(f"ไฟล์ {key} เปลี่ยนไประหว่างตรวจกับนำเข้า (sha256 ไม่ตรง)")

    now = datetime.now(ingest.BKK)
    batch_id = ingest.make_batch_id(source, year, now)
    raw_prefix = ingest.batch_prefix(raw_zone, source, batch_id)
    raw_key, manifest_key = raw_prefix + info["file_name"], raw_prefix + "manifest.json"
    result = {"status": "INGESTED", "bucket": bucket, "source": source, "year": year,
              "batch_id": batch_id, "raw_key": raw_key, "sha256": info["sha256"]}

    try:                                                                  # retry ของรอบก่อนที่ทำเสร็จแล้ว
        old = json.loads(s3.get_object(Bucket=bucket, Key=manifest_key)["Body"].read())
        if any(f.get("sha256") == info["sha256"] for f in old.get("files", [])):
            pe.emit("BatchIngested", result)                              # รอบก่อนอาจส่งต่อไม่สำเร็จ ส่งซ้ำได้ (ขั้นถัดไปรันซ้ำได้)
            return result
        raise IngestFunctionError(f"batch_id {batch_id} มีอยู่แล้วและเป็นไฟล์อื่น (ลองใหม่อีกนาที)")
    except s3.exceptions.NoSuchKey:
        pass

    s3.copy_object(Bucket=bucket, Key=raw_key, CopySource={"Bucket": bucket, "Key": key},
                   Metadata={"batch_id": batch_id, "source": source, "year": str(year),
                             "sha256": info["sha256"]},
                   MetadataDirective="REPLACE")
    size = s3.head_object(Bucket=bucket, Key=raw_key)["ContentLength"]
    if size != info["size_bytes"]:
        raise IngestFunctionError(f"ขนาดไฟล์ใน raw ({size}) ไม่ตรงกับต้นฉบับ ({info['size_bytes']})")

    manifest = ingest.build_manifest(batch_id=batch_id, source=source, year=year, bucket=bucket, info=info,
                                     incoming_key=key, raw_key=raw_key, ingested_at=now)
    manifest["ingested_by"] = "lambda:ingest_function"
    s3.put_object(Bucket=bucket, Key=manifest_key, ContentType="application/json",
                  Body=json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    pe.emit("BatchIngested", result)
    return result
