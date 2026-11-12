"""ตัวช่วยส่งต่อระหว่าง Lambda ของ pipeline ผ่าน EventBridge (ใช้โดย function/*_function/)

แต่ละ Lambda ทำงานเสร็จแล้วส่ง event เข้า default bus ของ EventBridge  กฎ (rule) ที่ infra/deploy_pipeline.sh สร้างไว้
จะส่ง event นั้นให้ Lambda ตัวถัดไป:

  incoming/<source>_<year>.csv --(S3 Object Created)--> check_batch
  check_batch   --BatchChecked-->     ingest_batch        (เฉพาะ status NEW)
  ingest_batch  --BatchIngested-->    validate_batch
  validate_batch --BatchValidated-->  transform_batch     (เฉพาะ PASSED / PARTIAL)
  transform_batch --CuratedTransformed--> verify_batch    (เฉพาะ TRANSFORMED)

event: source = "ronnai.pipeline", detail-type = ชื่อด้านบน, detail = ผลลัพธ์ของขั้นก่อนหน้า (JSON)
ตั้ง PIPELINE_EVENTS=off เพื่อปิดการส่ง (ใช้ตอนทดสอบ Lambda ทีละตัว ไม่ให้ไหลต่อ)
"""
import json
import os

SOURCE = "ronnai.pipeline"


def payload(event):
    """event จาก EventBridge ของ pipeline -> detail ; เรียกตรง ๆ ด้วย dict ธรรมดา -> คืนตามเดิม"""
    if isinstance(event, dict) and event.get("source") == SOURCE and isinstance(event.get("detail"), dict):
        return event["detail"]
    return event


def emit(detail_type, detail, client=None):
    """ส่ง event ไปขั้นถัดไป  คืน True เมื่อส่งแล้ว  ผิดพลาด -> raise (Lambda async จะลองใหม่ให้เอง)"""
    if os.environ.get("PIPELINE_EVENTS", "on").lower() == "off":
        return False
    if client is None:
        import boto3
        client = boto3.client("events")
    resp = client.put_events(Entries=[{"Source": SOURCE, "DetailType": detail_type,
                                       "Detail": json.dumps(detail, ensure_ascii=False, default=str)}])
    if resp.get("FailedEntryCount"):
        raise RuntimeError(f"ส่ง event {detail_type} ไม่สำเร็จ: {resp.get('Entries')}")
    return True
