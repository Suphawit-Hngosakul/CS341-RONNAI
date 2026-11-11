#!/usr/bin/env python3
"""สร้าง / ตรวจ / ลบ AWS infra ของ CS341-RONNAI (Issue #2) ด้วยคำสั่งเดียว

ใช้กับ AWS Academy Learner Lab: ไม่สร้าง IAM ใด ๆ, ใช้ credentials ของ lab (LabRole)

    python infra/ronnai_infra.py deploy            # สร้าง/อัปเดต stack + โฟลเดอร์ zone + เขียน config/settings.yaml
    python infra/ronnai_infra.py verify            # เช็ค S3 zones, Glue DBs, workgroup และรัน SELECT 1 ใน Athena
    python infra/ronnai_infra.py status            # ดู output ของ stack
    python infra/ronnai_infra.py destroy           # ลบ stack (bucket ต้องว่าง)
    python infra/ronnai_infra.py destroy --purge   # ลบข้อมูลทั้งหมดใน bucket แล้วลบ stack

ตัวเลือกร่วม: --region (default us-east-1), --profile, --project (default ronnai), --stack-name
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:  # pragma: no cover
    sys.exit("ต้องติดตั้ง boto3 ก่อน: pip install -r requirements.txt")

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).resolve().parent / "ronnai-datalake.yaml"
SETTINGS = ROOT / "config" / "settings.yaml"
ZONES = ["incoming/", "raw/", "trusted/", "curated/", "quarantine/", "athena-results/"]


def log(msg: str) -> None:
    print(f"[ronnai-infra] {msg}", flush=True)


def session(args) -> "boto3.Session":
    s = boto3.Session(profile_name=args.profile, region_name=args.region)
    try:
        ident = s.client("sts").get_caller_identity()
    except (NoCredentialsError, ClientError) as e:
        sys.exit(
            "เรียก AWS ไม่ได้ (credentials ไม่มีหรือหมดอายุ)\n"
            "  Learner Lab: กด Start Lab > AWS Details > AWS CLI: Show แล้วคัดลอกไปไว้ใน\n"
            "  ~/.aws/credentials (Windows: %UserProfile%\\.aws\\credentials) ใต้ [default]\n"
            f"  รายละเอียด: {e}"
        )
    log(f"account={ident['Account']} region={s.region_name} identity={ident['Arn']}")
    return s


def stack_outputs(cfn, name: str) -> dict:
    st = cfn.describe_stacks(StackName=name)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in st.get("Outputs", [])}


def stack_exists(cfn, name: str) -> str | None:
    try:
        return cfn.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"]
    except ClientError as e:
        if "does not exist" in str(e):
            return None
        raise


def print_failures(cfn, name: str) -> None:
    for ev in cfn.describe_stack_events(StackName=name)["StackEvents"][:30]:
        if ev["ResourceStatus"].endswith("FAILED"):
            log(f"  FAILED {ev['LogicalResourceId']}: {ev.get('ResourceStatusReason', '')}")


def deploy(args) -> None:
    s = session(args)
    cfn = s.client("cloudformation")
    params = [
        {"ParameterKey": "ProjectName", "ParameterValue": args.project},
        {"ParameterKey": "BytesScannedCutoffPerQuery", "ParameterValue": str(args.scan_limit_mb * 1024 * 1024)},
        {"ParameterKey": "TeammateAccountIds", "ParameterValue": args.teammates or ""},
    ]
    body = TEMPLATE.read_text(encoding="utf-8")
    status = stack_exists(cfn, args.stack_name)

    if status == "ROLLBACK_COMPLETE":
        log("stack เดิมสร้างไม่สำเร็จ (ROLLBACK_COMPLETE) กำลังลบก่อนสร้างใหม่")
        cfn.delete_stack(StackName=args.stack_name)
        cfn.get_waiter("stack_delete_complete").wait(StackName=args.stack_name)
        status = None

    try:
        if status is None:
            log(f"สร้าง stack {args.stack_name} ...")
            cfn.create_stack(StackName=args.stack_name, TemplateBody=body, Parameters=params,
                             Tags=[{"Key": "project", "Value": args.project}])
            waiter = "stack_create_complete"
        else:
            log(f"อัปเดต stack {args.stack_name} (สถานะเดิม {status}) ...")
            cfn.update_stack(StackName=args.stack_name, TemplateBody=body, Parameters=params)
            waiter = "stack_update_complete"
        cfn.get_waiter(waiter).wait(StackName=args.stack_name, WaiterConfig={"Delay": 5, "MaxAttempts": 120})
    except ClientError as e:
        if "No updates are to be performed" in str(e):
            log("ไม่มีอะไรเปลี่ยน stack เป็นปัจจุบันแล้ว")
        else:
            raise
    except Exception:
        print_failures(cfn, args.stack_name)
        sys.exit("deploy ไม่สำเร็จ ดูเหตุผลด้านบน")

    out = stack_outputs(cfn, args.stack_name)
    bucket = out["BucketName"]
    s3 = s.client("s3")
    for z in ZONES:
        s3.put_object(Bucket=bucket, Key=z, Body=b"")
    log(f"สร้างโฟลเดอร์ zone ใน s3://{bucket}/: {' '.join(ZONES)}")

    write_settings(out, args)
    log("เสร็จ ลองรัน: python infra/ronnai_infra.py verify")


def write_settings(out: dict, args) -> None:
    text = f"""# สร้างอัตโนมัติโดย infra/ronnai_infra.py deploy (ไฟล์นี้ถูก .gitignore ไว้)
# ห้ามใส่ access key / secret ในไฟล์นี้
aws:
  region: {out['Region']}
  profile: {args.profile or 'default'}
s3:
  bucket: {out['BucketName']}
  prefixes:
    incoming: incoming/
    raw: raw/
    trusted: trusted/
    curated: curated/
    quarantine: quarantine/
    athena_results: athena-results/
glue:
  databases:
    trusted: {out['TrustedDatabaseName']}
    curated: {out['CuratedDatabaseName']}
athena:
  workgroup: {out['AthenaWorkGroupName']}
"""
    SETTINGS.write_text(text, encoding="utf-8")
    log(f"เขียน {SETTINGS.relative_to(ROOT)} แล้ว")


def verify(args) -> None:
    s = session(args)
    out = stack_outputs(s.client("cloudformation"), args.stack_name)
    ok = True

    s3 = s.client("s3")
    for z in ZONES:
        try:
            s3.head_object(Bucket=out["BucketName"], Key=z)
            log(f"OK  s3://{out['BucketName']}/{z}")
        except ClientError:
            ok = False
            log(f"MISSING s3://{out['BucketName']}/{z}")

    glue = s.client("glue")
    for db in (out["TrustedDatabaseName"], out["CuratedDatabaseName"]):
        try:
            glue.get_database(Name=db)
            log(f"OK  glue database {db}")
        except ClientError:
            ok = False
            log(f"MISSING glue database {db}")

    athena = s.client("athena")
    wg = athena.get_work_group(WorkGroup=out["AthenaWorkGroupName"])["WorkGroup"]["Configuration"]
    limit = wg.get("BytesScannedCutoffPerQuery")
    log(f"{'OK ' if limit else 'NO LIMIT'} workgroup {out['AthenaWorkGroupName']} scan limit = "
        f"{(limit or 0) / 1024 / 1024:.0f} MB/query")
    ok &= bool(limit)

    qid = athena.start_query_execution(QueryString="SELECT 1 AS ok",
                                       WorkGroup=out["AthenaWorkGroupName"])["QueryExecutionId"]
    while True:
        st = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if st["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)
    if st["State"] == "SUCCEEDED":
        val = athena.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"][1]["Data"][0]["VarCharValue"]
        log(f"OK  Athena SELECT 1 -> {val}")
    else:
        ok = False
        log(f"FAIL Athena SELECT 1: {st['State']} {st.get('StateChangeReason', '')}")

    sys.exit(0 if ok else "verify ไม่ผ่าน")


def status(args) -> None:
    s = session(args)
    cfn = s.client("cloudformation")
    st = stack_exists(cfn, args.stack_name)
    if not st:
        sys.exit(f"ยังไม่มี stack {args.stack_name} ใน {s.region_name}")
    log(f"stack {args.stack_name}: {st}")
    for k, v in stack_outputs(cfn, args.stack_name).items():
        log(f"  {k:24} {v}")


def destroy(args) -> None:
    s = session(args)
    cfn = s.client("cloudformation")
    if not stack_exists(cfn, args.stack_name):
        log("ไม่มี stack ให้ลบ")
        return
    bucket = stack_outputs(cfn, args.stack_name).get("BucketName")
    if bucket and args.purge:
        if not args.yes:
            ans = input(f"ลบไฟล์ทั้งหมดใน s3://{bucket} (รวม raw/) ถาวร? พิมพ์ชื่อ bucket เพื่อยืนยัน: ")
            if ans.strip() != bucket:
                sys.exit("ยกเลิก")
        log(f"ลบไฟล์ทั้งหมดใน s3://{bucket} ...")
        b = s.resource("s3").Bucket(bucket)
        b.object_versions.delete()
        b.objects.all().delete()
    elif bucket:
        # ลบแค่ marker โฟลเดอร์ว่างและผล Athena; ถ้ายังมีข้อมูลจริง CloudFormation จะลบ bucket ไม่ได้ (ตั้งใจ กันข้อมูลหาย)
        s3 = s.client("s3")
        for z in ZONES:
            s3.delete_object(Bucket=bucket, Key=z)
        s.resource("s3").Bucket(bucket).objects.filter(Prefix="athena-results/").delete()
    log(f"ลบ stack {args.stack_name} ...")
    cfn.delete_stack(StackName=args.stack_name)
    try:
        cfn.get_waiter("stack_delete_complete").wait(StackName=args.stack_name,
                                                      WaiterConfig={"Delay": 5, "MaxAttempts": 120})
    except Exception:
        print_failures(cfn, args.stack_name)
        sys.exit("ลบไม่สำเร็จ ถ้า bucket ยังมีข้อมูล ให้รันใหม่ด้วย --purge (ข้อมูลจะหายถาวร)")
    log("ลบเรียบร้อย")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows console อาจไม่ใช่ UTF-8 ทำให้พิมพ์ภาษาไทยไม่ได้
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--profile", default=None, help="AWS CLI profile (default: default / env vars)")
    p.add_argument("--project", default="ronnai")
    p.add_argument("--stack-name", default=None, help="default: <project>-datalake")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deploy")
    d.add_argument("--scan-limit-mb", type=int, default=1024, help="Athena scan limit ต่อ query (MB, ขั้นต่ำ 10)")
    d.add_argument("--teammates", default="", help="account ID ของ Learner Lab เพื่อน คั่นด้วย , (ไม่บังคับ)")
    sub.add_parser("verify")
    sub.add_parser("status")
    x = sub.add_parser("destroy")
    x.add_argument("--purge", action="store_true", help="ลบข้อมูลใน bucket ทั้งหมดก่อน (ถาวร)")
    x.add_argument("--yes", action="store_true", help="ไม่ถามยืนยันตอน --purge")

    args = p.parse_args()
    if args.cmd == "deploy" and args.scan_limit_mb < 10:
        p.error("--scan-limit-mb ต้องอย่างน้อย 10")
    args.stack_name = args.stack_name or f"{args.project}-datalake"
    {"deploy": deploy, "verify": verify, "status": status, "destroy": destroy}[args.cmd](args)


if __name__ == "__main__":
    main()
