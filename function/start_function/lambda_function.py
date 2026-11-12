"""Lambda 0: start_pipeline  ไฟล์ใหม่ใน incoming/ -> เริ่ม execution ของ state machine (Step Functions)

ทำไมต้องมีตัวนี้: กฎ EventBridge ที่ยิงเข้า Step Functions ตรง ๆ ต้องให้ EventBridge assume role (RoleArn)
ซึ่ง LabRole ของ Learner Lab ไม่อนุญาต  แต่ EventBridge -> Lambda ใช้แค่ resource policy ของ Lambda (add-permission)
แล้ว Lambda นี้ (รันด้วย LabRole) เรียก states:StartExecution ให้แทน

input  : event ของ EventBridge (S3 Object Created)  detail.bucket.name / detail.object.key
output : {"status": "STARTED" | "ALREADY_STARTED", "execution_arn" | "execution_name", "bucket", "key"}
  ส่ง event ทั้งก้อนเป็น input ของ execution (CheckBatch อ่าน detail.bucket.name / detail.object.key เอง)
  ชื่อ execution = <ชื่อไฟล์>-<event id>  EventBridge ส่ง event เดิมซ้ำ -> ชื่อเดิม -> ไม่เกิด execution ซ้ำ

ตัวแปรสภาพแวดล้อม: STATE_MACHINE_ARN (infra/ronnai_aws.sh deploy ตั้งให้)
"""
import json
import os
import re
import uuid


def execution_name(key, event_id):
    stem = re.sub(r"[^A-Za-z0-9_-]", "-", key.rsplit("/", 1)[-1])[:40]
    return f"{stem}-{event_id}"[:80]


def lambda_handler(event, context=None):
    import boto3

    detail = event.get("detail") or {}
    bucket, key = detail["bucket"]["name"], detail["object"]["key"]
    name = execution_name(key, event.get("id") or str(uuid.uuid4()))
    sfn = boto3.client("stepfunctions")
    base = {"bucket": bucket, "key": key}
    try:
        resp = sfn.start_execution(stateMachineArn=os.environ["STATE_MACHINE_ARN"], name=name,
                                   input=json.dumps(event, ensure_ascii=False, default=str))
    except sfn.exceptions.ExecutionAlreadyExists:
        return {**base, "status": "ALREADY_STARTED", "execution_name": name}
    print(json.dumps({"started": resp["executionArn"], "key": key}, ensure_ascii=False))
    return {**base, "status": "STARTED", "execution_arn": resp["executionArn"]}
