"""Lambda 3: validate_batch  raw/ -> trusted/ + quarantine/  (ใช้ gate ของ ingestion/validate_gate.py)

ตรวจตาม contracts/<source>.yaml แถวที่ผ่านลง trusted/<source>/year=<yyyy>/<source>.parquet (upsert ตาม key)
แถวที่ไม่ผ่านลง quarantine/<source>/<batch_id>/ (ไม่ลบ ไม่เขียนทับ)

input  : output ของ ingest  {"bucket", "source", "year", "batch_id"}
output : {"status": "PASSED" | "PARTIAL" | "BATCH_BLOCKED", "total_rows", "passed", "quarantined",
          "bucket", "source", "year", "batch_id", "already_validated": bool}
  PASSED        ทุกแถวผ่าน
  PARTIAL       บางแถวถูกกัก ที่ผ่านไป trusted แล้ว (ให้ state machine เดินต่อ)
  BATCH_BLOCKED ผิดระดับไฟล์ (คอลัมน์หาย ไฟล์ว่าง อ่านไม่ได้) กักทั้ง batch ไม่แตะ trusted (ให้ state machine หยุด)
รันซ้ำ batch ที่เคยตรวจแล้ว (gate ไม่เขียนทับ quarantine) -> คืนสถานะจาก report.json เดิม ไม่ตรวจซ้ำ
error อื่น (ไม่พบ batch, contract) -> raise
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE if (HERE / "contracts").is_dir() else HERE.parents[1]
for p in (HERE, HERE.parents[1] / "ingestion"):
    if p.is_dir():
        sys.path.insert(0, str(p))
import validate_gate as gate  # noqa: E402


def lambda_handler(event, context=None):
    import boto3

    bucket, source, batch_id = event.get("bucket") or os.environ["BUCKET"], event["source"], event["batch_id"]
    cfg = {"s3": {"bucket": bucket, "prefixes": {}}}
    store = gate.S3Store(boto3.client("s3"), bucket)
    manifest = gate.find_batch(store, cfg, source, batch_id, latest=False)
    year, f = int(manifest["year"]), manifest["files"][0]
    base = {"bucket": bucket, "source": source, "year": year, "batch_id": batch_id}

    data = store.read(f["raw_key"])
    if data is None:
        raise gate.GateError(f"ไม่พบไฟล์ใน raw: {f['raw_key']}")
    try:
        rep = gate.gate_batch(store, cfg, source=source, year=year, batch_id=batch_id, data=data,
                              file_name=f["file_name"], contracts_dir=ROOT / "contracts", repo_root=ROOT)
        already = False
    except gate.GateError as e:
        if e.code != gate.EXIT_ALREADY_DONE:
            raise
        prev = store.read(gate.quarantine_prefix(cfg, source, batch_id) + "report.json")
        rep, already = json.loads(prev), True                             # เคยตรวจแล้ว ใช้ผลเดิม
    return {**base, "status": rep["status"], "total_rows": rep["total_rows"], "passed": rep["passed"],
            "quarantined": rep["quarantined"], "already_validated": already}
