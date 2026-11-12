#!/usr/bin/env bash
# =============================================================================
# CS341-RONNAI : ตั้งค่า AWS (Issue #2) ผ่าน AWS CLI — สำหรับ AWS Academy Learner Lab
#
#   ใช้ได้ใน: Terminal ของ Learner Lab, AWS CloudShell, Git Bash (Windows), macOS/Linux
#
#   ./infra/ronnai_aws.sh all        คำสั่งเดียวตั้งแต่บัญชีว่าง: setup -> deploy -> view -> run (รันซ้ำได้)
#   ./infra/ronnai_aws.sh setup      สร้าง S3 bucket + 6 zones, Glue DB 2 อัน, Athena workgroup
#   ./infra/ronnai_aws.sh verify     ตรวจว่าครบ + รัน SELECT 1 ใน Athena
#   ./infra/ronnai_aws.sh deploy     สร้างตาราง trusted (sql/glue_trusted.sql) + สร้าง/อัปเดต Lambda 6 ตัว (function/)
#                                    (validate ใช้ pandas/pyarrow จาก Lambda layer ของ AWS: PANDAS_LAYER_ARN)
#                                    + state machine ของ Step Functions (infra/state_machine.asl.json)
#                                    + กฎ EventBridge 1 ข้อ: ไฟล์ใหม่ใน incoming/ -> Lambda start-pipeline -> เริ่ม execution
#                                    ใน execution: check -> ingest -> validate -> transform -> verify
#   ./infra/ronnai_aws.sh run        (Issue #18) อัปโหลด data/gee_<YEAR>/ ทั้ง 3 แหล่งเข้า incoming/ แล้วรอดูผลทีละขั้น
#                                    YEAR=2026 เป็นค่าเริ่มต้น  รันซ้ำได้: ไฟล์เดิมจะจบที่ AlreadyProcessed
#   ./infra/ronnai_aws.sh view       สร้างตาราง curated (ถ้ายังไม่มี) + view ตอบ Q1 (sql/views/heat_vs_green.sql)
#   ./infra/ronnai_aws.sh destroy    ลบทุกอย่าง: state machine + Lambda + กฎ EventBridge + workgroup + Glue + bucket
#                                    (ข้อมูลหายถาวร ต้องพิมพ์ชื่อ bucket ยืนยัน) ชื่อเดิม `teardown` ใช้ได้เหมือนกัน
#
#   เลข account/ชื่อ bucket ในไฟล์ SQL (cs341-ronnai-<เลข>) ถูกแทนด้วย bucket ของบัญชีที่ล็อกอินอยู่ให้เองตอนรัน
#   deploy / view (ไม่ต้องแก้ไฟล์ SQL) ถ้าใช้ชื่อ bucket อื่นให้ตั้ง BUCKET=...
#
#   ปรับค่าได้ด้วย env var เช่น:
#     REGION=us-east-1 BUCKET=my-bucket SCAN_LIMIT_MB=1024 ./infra/ronnai_aws.sh setup
#     ROLE_ARN=arn:aws:iam::<account>:role/LabRole ./infra/ronnai_aws.sh deploy   (ค่าเริ่มต้น = LabRole)
#
#   รันซ้ำได้ (idempotent): ของที่มีอยู่แล้วจะข้าม ไม่สร้างซ้ำ
# =============================================================================
set -euo pipefail

ACTION="${1:-}"
REGION="${REGION:-us-east-1}"                 # Learner Lab ใช้ us-east-1
AWS_PROFILE_NAME="${AWS_PROFILE_NAME:-}"      # เว้นว่าง = ใช้ default / env var
BUCKET="${BUCKET:-}"                          # เว้นว่าง = cs341-ronnai-<account_id>
WORKGROUP="${WORKGROUP:-ronnai-wg}"
DB_TRUSTED="${DB_TRUSTED:-ronnai_trusted}"
DB_CURATED="${DB_CURATED:-ronnai_curated}"
SCAN_LIMIT_MB="${SCAN_LIMIT_MB:-1024}"        # จำกัดข้อมูลที่สแกนต่อ query (ขั้นต่ำ 10 MB)
RESULT_EXPIRE_DAYS="${RESULT_EXPIRE_DAYS:-7}" # ลบผล query เก่าใน athena-results/ อัตโนมัติ
ZONES=(incoming raw trusted curated quarantine athena-results)

# ใช้กับ deploy / view / destroy
ROLE_ARN="${ROLE_ARN:-}"                      # เว้นว่าง = arn:aws:iam::<account_id>:role/LabRole
RUNTIME="${RUNTIME:-python3.12}"
PY_VERSION="${RUNTIME#python}"
NAME_PREFIX="${NAME_PREFIX:-ronnai}"
VIEWS_SQL="${VIEWS_SQL:-sql/views/heat_vs_green.sql}"   # view ตอบ Q1 (path เทียบกับ repo)
TRUSTED_SQL="${TRUSTED_SQL:-sql/glue_trusted.sql}"      # ตาราง trusted ใน Glue (path เทียบกับ repo)
SFN_NAME="${SFN_NAME:-}"                      # เว้นว่าง = <NAME_PREFIX>-pipeline
ASL_FILE="${ASL_FILE:-infra/state_machine.asl.json}"    # นิยาม state machine (path เทียบกับ repo)
YEAR="${YEAR:-2026}"                          # ใช้กับ run
RUN_WAIT_SEC="${RUN_WAIT_SEC:-900}"           # run รอ execution จบได้นานสุดกี่วินาที
TRIGGER_WARMUP_SEC="${TRIGGER_WARMUP_SEC:-60}" # all: รอหลังสร้าง trigger ใหม่ก่อนอัปไฟล์ (บัญชีใหม่)
# Lambda layer สำเร็จรูปของ AWS (AWS SDK for pandas) มี pandas + pyarrow ให้ validate-batch ไม่ต้องแพ็กเอง
# เลขเวอร์ชันล่าสุดดูที่ https://aws-sdk-pandas.readthedocs.io/en/stable/layers.html
# ตั้ง PANDAS_LAYER_ARN=none เพื่อแพ็ก pandas + pyarrow เอง (แบบเดิม zip ~64 MB ส่งผ่าน S3)
PANDAS_LAYER_ARN="${PANDAS_LAYER_ARN:-arn:aws:lambda:${REGION}:336392948345:layer:AWSSDKPandas-Python${PY_VERSION//./}:32}"
PKG_PREFIX="${PKG_PREFIX:-lambda-packages/}"  # ที่พัก zip ใน bucket ใช้เฉพาะ zip ที่ใหญ่เกิน 45 MB (อัปเข้า Lambda ตรงไม่ได้)
# ชื่อ Lambda : โฟลเดอร์ใน function/ : timeout (วินาที) : memory (MB) : ชุดไลบรารี (light/heavy)
FUNCS=(
  "start-pipeline:start_function:30:128:light"
  "check-batch:batch_function:60:256:light"
  "ingest-batch:ingest_function:120:512:light"
  "validate-batch:validate_function:300:2048:heavy"
  "transform-batch:transform_function:900:256:light"
  "verify-batch:verify_function:300:256:light"
)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SETTINGS_FILE="$REPO_DIR/config/settings.yaml"
TMP_DIR="$(mktemp -d)"
# Git Bash บน Windows: aws.exe อ่าน path แบบ /tmp/... ใน file:// ไม่ได้ (ไม่ถูกแปลงให้)
# แปลงเป็น C:/Users/.../Temp/... ซึ่งทั้ง bash และ aws.exe/python.exe ใช้ได้
if command -v cygpath >/dev/null 2>&1; then
  TMP_DIR="$(cygpath -m "$TMP_DIR")"
  REPO_DIR="$(cygpath -m "$REPO_DIR")"
  SETTINGS_FILE="$REPO_DIR/config/settings.yaml"
fi
SQL_SPLIT="$REPO_DIR/infra/tools/sql_split.py"   # แยกไฟล์ SQL เป็นทีละ statement ให้ Athena
trap 'rm -rf "$TMP_DIR"' EXIT

# ---------------------------------------------------------------------------
# ฟังก์ชันแยกไว้ใน infra/lib/  (ไฟล์นี้มีแค่ค่าตั้งต้น + เลือกคำสั่ง)
for _lib in common setup deploy run view destroy; do
  # shellcheck source=/dev/null
  source "$SCRIPT_DIR/lib/$_lib.sh"
done

case "$ACTION" in
  setup)             setup ;;
  verify)            verify ;;
  deploy)            deploy ;;
  run)               run_pipeline ;;
  all)               setup; deploy; view; run_pipeline ;;
  view)              view ;;
  destroy|teardown)  destroy ;;
  *) echo "usage: $0 {all|setup|verify|deploy|run|view|destroy}"; exit 2 ;;
esac
