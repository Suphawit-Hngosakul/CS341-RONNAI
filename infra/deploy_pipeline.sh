#!/usr/bin/env bash
# =============================================================================
# CS341-RONNAI : สร้าง/อัปเดต Lambda 5 ตัวด้วยคำสั่งเดียว — AWS Academy Learner Lab / CloudShell
#
#   bash infra/deploy_pipeline.sh
#
#   สิ่งที่ทำ (รันซ้ำได้ ถ้ามีอยู่แล้วจะอัปเดตให้เป็นเวอร์ชันล่าสุด):
#     1) แพ็กเกจ + ส่ง Lambda 5 ตัวจาก function/*_function/
#          ronnai-check-batch, -ingest-batch, -validate-batch, -transform-batch, -verify-batch
#     2) เปิดการส่ง event ของ bucket ไป EventBridge + สร้างกฎ 5 ข้อ ให้ Lambda ต่อกันเอง (ไม่ใช้ Step Functions):
#          อัปโหลดไฟล์เข้า incoming/ -> check -> ingest -> validate -> transform -> verify
#          (แต่ละตัวส่ง event ให้ตัวถัดไป ดู ingestion/pipeline_events.py)
#     3) สร้างตาราง curated (ถ้ายังไม่มี) และ view ตอบคำถาม Q1 จาก sql/views/heat_vs_green.sql
#
#   ต้องมีก่อน: bash infra/ronnai_aws.sh setup (bucket + Glue + Athena) และตาราง Glue trusted (sql/glue_trusted.sql)
#
#   ปรับค่าได้ด้วย env var เช่น:
#     REGION=us-east-1 BUCKET=my-bucket ROLE_ARN=arn:aws:iam::123456789012:role/LabRole bash infra/deploy_pipeline.sh
#   ต้องมี: AWS CLI, python3 (+ pip และเน็ตสำหรับโหลด pandas/pyarrow ตอนแพ็กเกจ validate)
# =============================================================================
set -euo pipefail

REGION="${REGION:-us-east-1}"
AWS_PROFILE_NAME="${AWS_PROFILE_NAME:-}"
BUCKET="${BUCKET:-}"                          # เว้นว่าง = cs341-ronnai-<account_id> (เหมือน ronnai_aws.sh)
WORKGROUP="${WORKGROUP:-ronnai-wg}"
DB_TRUSTED="${DB_TRUSTED:-ronnai_trusted}"
DB_CURATED="${DB_CURATED:-ronnai_curated}"
ROLE_ARN="${ROLE_ARN:-}"                      # เว้นว่าง = arn:aws:iam::<account_id>:role/LabRole
RUNTIME="${RUNTIME:-python3.12}"
PY_VERSION="${RUNTIME#python}"
NAME_PREFIX="${NAME_PREFIX:-ronnai}"
VIEWS_SQL="${VIEWS_SQL:-sql/views/heat_vs_green.sql}"   # view ตอบ Q1 (path เทียบกับ repo)
PKG_PREFIX="${PKG_PREFIX:-lambda-packages/}"  # ที่เก็บ zip ใน bucket (zip ของ validate เกิน 50 MB ต้องส่งผ่าน S3)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

ok()   { printf '  \033[32m[OK]\033[0m   %s\n' "$*"; }
skip() { printf '  \033[36m[SKIP]\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m[WARN]\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m[FAIL]\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
die()  { fail "$*"; exit 1; }

aws_() {
  if [[ -n "$AWS_PROFILE_NAME" ]]; then
    aws --profile "$AWS_PROFILE_NAME" --region "$REGION" "$@"
  else
    aws --region "$REGION" "$@"
  fi
}

# ชื่อ Lambda : โฟลเดอร์ใน function/ : timeout (วินาที) : memory (MB) : ชุดไลบรารี (light/heavy)
FUNCS=(
  "check-batch:batch_function:60:256:light"
  "ingest-batch:ingest_function:120:512:light"
  "validate-batch:validate_function:300:2048:heavy"
  "transform-batch:transform_function:900:256:light"
  "verify-batch:verify_function:300:256:light"
)

declare -A LAMBDA_ARNS
PY=""; ACCOUNT_ID=""

preflight() {
  command -v aws >/dev/null 2>&1 || die "ไม่พบ AWS CLI"
  PY="$(command -v python3 || command -v python || true)"
  [[ -n "$PY" ]] || die "ไม่พบ python3"
  "$PY" -m pip --version >/dev/null 2>&1 || die "ไม่พบ pip ($PY -m pip)"
  step "ตรวจ credentials"
  local ident
  if ! ident="$(aws_ sts get-caller-identity --query '[Account,Arn]' --output text 2>&1)"; then
    fail "$ident"
    die "credentials ใช้ไม่ได้/หมดอายุ — กด Start Lab แล้วคัดลอก AWS CLI credentials จาก 'AWS Details' ใหม่"
  fi
  ACCOUNT_ID="$(awk '{print $1}' <<<"$ident")"
  [[ -z "$BUCKET" ]] && BUCKET="cs341-ronnai-${ACCOUNT_ID}"
  [[ -z "$ROLE_ARN" ]] && ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/LabRole"
  ok "Account: $ACCOUNT_ID | Region: $REGION | Bucket: $BUCKET"
  ok "Role: $ROLE_ARN | Runtime: $RUNTIME"
  aws_ s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1 \
    || die "ไม่พบ bucket $BUCKET — รัน 'bash infra/ronnai_aws.sh setup' ก่อน"
  aws_ iam get-role --role-name "${ROLE_ARN##*/}" >/dev/null 2>&1 \
    || warn "อ่าน role ${ROLE_ARN##*/} ไม่ได้ (อาจไม่มีสิทธิ์อ่าน) จะลองใช้ต่อ ถ้าสร้าง Lambda ไม่ผ่านให้ตรวจชื่อ role"
}

# ---------------------------------------------------------------------------
pip_install() {   # pip_install <โฟลเดอร์> <แพ็กเกจ...>  (wheel ของ Lambda: linux x86_64)
  local target="$1"; shift
  "$PY" -m pip install -q --disable-pip-version-check --target "$target" \
    --platform manylinux2014_x86_64 --python-version "$PY_VERSION" --implementation cp \
    --only-binary=:all: "$@" || die "ติดตั้งแพ็กเกจ $* ไม่สำเร็จ (ตรวจอินเทอร์เน็ต)"
}

trim_deps() {     # ตัดของที่ Lambda ไม่ใช้ ให้ขนาดไม่เกินลิมิต 250 MB (ไม่แตะไฟล์ .so)
  find "$1" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
  find "$1" -type d \( -name tests -o -name test \) -prune -exec rm -rf {} + 2>/dev/null || true
  rm -rf "$1/pyarrow/include" "$1/pyarrow/src" 2>/dev/null || true
  find "$1" \( -name '*.pyi' -o -name '*.pyx' -o -name '*.pxd' -o -name '*.pxi' -o -name '*.a' \) -delete 2>/dev/null || true
}

build_packages() {
  step "1) แพ็กเกจ Lambda"
  [[ -d "$REPO_DIR/function" && -d "$REPO_DIR/ingestion" && -d "$REPO_DIR/contracts" ]] \
    || die "ไม่พบโฟลเดอร์ function/ ingestion/ contracts/ ใน $REPO_DIR (ต้องรันจาก repo)"
  local f
  for f in gee/communities.csv sql/curated_merge.sql \
           ingestion/ingest.py ingestion/validate_gate.py ingestion/verify_curated.py ingestion/pipeline_events.py; do
    [[ -f "$REPO_DIR/$f" ]] || die "ไม่พบ $f"
  done
  mkdir -p "$TMP_DIR/light" "$TMP_DIR/heavy" "$TMP_DIR/zips"
  pip_install "$TMP_DIR/light" pyyaml
  ok "ติดตั้ง pyyaml (ชุดเบา)"
  pip_install "$TMP_DIR/heavy" pandas pyarrow pyyaml
  trim_deps "$TMP_DIR/heavy"
  ok "ติดตั้ง pandas, pyarrow, pyyaml (ชุดของ validate) ขนาด $(du -sm "$TMP_DIR/heavy" | cut -f1) MB"
  local spec name dir to mem kind b
  for spec in "${FUNCS[@]}"; do
    IFS=: read -r name dir to mem kind <<<"$spec"
    [[ -f "$REPO_DIR/function/$dir/lambda_function.py" ]] || die "ไม่พบ function/$dir/lambda_function.py"
    b="$TMP_DIR/bundle-$name"
    mkdir -p "$b/contracts" "$b/gee" "$b/sql"
    cp -R "$TMP_DIR/$kind/." "$b/"
    cp "$REPO_DIR/function/$dir/lambda_function.py" "$b/"
    cp "$REPO_DIR"/ingestion/ingest.py "$REPO_DIR"/ingestion/validate_gate.py "$REPO_DIR"/ingestion/verify_curated.py \
       "$REPO_DIR"/ingestion/pipeline_events.py "$b/"
    cp "$REPO_DIR"/contracts/*.yaml "$b/contracts/"
    cp "$REPO_DIR/gee/communities.csv" "$b/gee/"
    cp "$REPO_DIR/sql/curated_merge.sql" "$b/sql/"
    "$PY" -c 'import shutil, sys; shutil.make_archive(sys.argv[2], "zip", sys.argv[1])' "$b" "$TMP_DIR/zips/$name"
    ok "$NAME_PREFIX-$name  ($(du -sk "$TMP_DIR/zips/$name.zip" | awk '{printf "%.1f", $1/1024}') MB zip)"
  done
}

deploy_lambdas() {
  step "2) Lambda 5 ตัว"
  local spec name dir to mem kind fn key envs
  envs="Variables={BUCKET=$BUCKET,ATHENA_WORKGROUP=$WORKGROUP,CURATED_DB=$DB_CURATED,TRUSTED_DB=$DB_TRUSTED}"
  for spec in "${FUNCS[@]}"; do
    IFS=: read -r name dir to mem kind <<<"$spec"
    fn="$NAME_PREFIX-$name"
    key="${PKG_PREFIX}${fn}.zip"
    aws_ s3 cp "$TMP_DIR/zips/$name.zip" "s3://$BUCKET/$key" --only-show-errors \
      || die "อัปโหลด $fn.zip ไป s3://$BUCKET/$key ไม่ได้"
    if aws_ lambda get-function --function-name "$fn" >/dev/null 2>&1; then
      aws_ lambda update-function-code --function-name "$fn" --s3-bucket "$BUCKET" --s3-key "$key" >/dev/null
      aws_ lambda wait function-updated --function-name "$fn"
      aws_ lambda update-function-configuration --function-name "$fn" --role "$ROLE_ARN" \
        --handler lambda_function.lambda_handler --runtime "$RUNTIME" --timeout "$to" --memory-size "$mem" \
        --environment "$envs" >/dev/null
      aws_ lambda wait function-updated --function-name "$fn"
      ok "อัปเดต $fn"
    else
      aws_ lambda create-function --function-name "$fn" --runtime "$RUNTIME" --role "$ROLE_ARN" \
        --handler lambda_function.lambda_handler --code "S3Bucket=$BUCKET,S3Key=$key" \
        --timeout "$to" --memory-size "$mem" --architectures x86_64 --environment "$envs" >/dev/null \
        || die "สร้าง $fn ไม่ได้ (ตรวจ role $ROLE_ARN และสิทธิ์ Lambda)"
      aws_ lambda wait function-active --function-name "$fn"
      ok "สร้าง $fn"
    fi
    LAMBDA_ARNS[$name]="$(aws_ lambda get-function --function-name "$fn" --query 'Configuration.FunctionArn' --output text)"
  done
}

# กฎ EventBridge: ชื่อกฎ | event pattern | Lambda ปลายทาง
rule_pattern() {   # rule_pattern <detail-type> : event ที่ Lambda ของ pipeline ส่งต่อกัน
  printf '{"source": ["ronnai.pipeline"], "detail-type": ["%s"]}' "$1"
}

put_rule() {       # put_rule <ชื่อกฎ> <pattern-json> <ชื่อ lambda (เช่น ingest-batch)>
  local rule="$1" pattern="$2" name="$3" fn arn err
  fn="$NAME_PREFIX-$name"; arn="${LAMBDA_ARNS[$name]}"
  printf '%s' "$pattern" >"$TMP_DIR/pattern.json"
  aws_ events put-rule --name "$rule" --event-pattern "file://$TMP_DIR/pattern.json" --state ENABLED >/dev/null \
    || die "สร้างกฎ $rule ไม่ได้"
  if ! err="$(aws_ lambda add-permission --function-name "$fn" --statement-id "events-$rule" \
        --action lambda:InvokeFunction --principal events.amazonaws.com \
        --source-arn "arn:aws:events:$REGION:$ACCOUNT_ID:rule/$rule" 2>&1)"; then
    grep -q ResourceConflictException <<<"$err" || die "ให้สิทธิ์กฎ $rule เรียก $fn ไม่ได้: $err"
  fi
  aws_ events put-targets --rule "$rule" --targets "Id=1,Arn=$arn" >/dev/null || die "ผูกกฎ $rule กับ $fn ไม่ได้"
  ok "กฎ $rule -> $fn"
}

enable_event_chain() {
  step "3) EventBridge: อัปโหลดไฟล์เข้า incoming/ แล้ว Lambda ต่อกันเอง"
  local cur compact
  cur="$(aws_ s3api get-bucket-notification-configuration --bucket "$BUCKET" --output json 2>/dev/null || true)"
  compact="${cur//[[:space:]]/}"
  if grep -q 'EventBridgeConfiguration' <<<"$cur"; then
    skip "bucket ส่ง event ไป EventBridge อยู่แล้ว"
  elif [[ -z "$compact" || "$compact" == "{}" ]]; then
    aws_ s3api put-bucket-notification-configuration --bucket "$BUCKET" \
      --notification-configuration '{"EventBridgeConfiguration": {}}' >/dev/null \
      || die "เปิด EventBridge notification ของ bucket ไม่ได้"
    ok "เปิดการส่ง event ของ bucket ไป EventBridge"
  else
    die "bucket มี notification (Lambda/SNS/SQS) ตั้งไว้แล้ว การตั้งใหม่จะเขียนทับของเดิม จึงหยุดไว้ก่อน — เปิด EventBridge เองที่ S3 > bucket > Properties แล้วรันใหม่"
  fi
  put_rule "$NAME_PREFIX-incoming-csv" \
    "{\"source\": [\"aws.s3\"], \"detail-type\": [\"Object Created\"], \"detail\": {\"bucket\": {\"name\": [\"$BUCKET\"]}, \"object\": {\"key\": [{\"prefix\": \"incoming/\"}]}}}" \
    check-batch
  put_rule "$NAME_PREFIX-batch-checked"       "$(rule_pattern BatchChecked)"       ingest-batch
  put_rule "$NAME_PREFIX-batch-ingested"      "$(rule_pattern BatchIngested)"      validate-batch
  put_rule "$NAME_PREFIX-batch-validated"     "$(rule_pattern BatchValidated)"     transform-batch
  put_rule "$NAME_PREFIX-curated-transformed" "$(rule_pattern CuratedTransformed)" verify-batch
}

run_athena_file() {   # run_athena_file <ไฟล์ .sql 1 statement>  0 = สำเร็จ
  local qid state reason
  qid="$(aws_ athena start-query-execution --query-string "$(cat "$1")" --work-group "$WORKGROUP" \
        --query-execution-context "Database=$DB_CURATED" --query QueryExecutionId --output text)" || return 1
  for _ in $(seq 1 90); do
    state="$(aws_ athena get-query-execution --query-execution-id "$qid" --query QueryExecution.Status.State --output text)"
    case "$state" in
      SUCCEEDED) return 0 ;;
      FAILED|CANCELLED)
        reason="$(aws_ athena get-query-execution --query-execution-id "$qid" --query QueryExecution.Status.StateChangeReason --output text)"
        printf '       %s: %s\n' "$state" "$reason"; return 1 ;;
    esac
    sleep 2
  done
  return 1
}

create_q1_views() {
  step "4) ตาราง curated + view ตอบ Q1 (Athena)"
  if [[ ! -f "$REPO_DIR/$VIEWS_SQL" ]]; then
    warn "ไม่พบ $VIEWS_SQL ข้ามขั้นนี้ (ตั้ง VIEWS_SQL=... ได้)"; return 0
  fi
  mkdir -p "$TMP_DIR/sql"
  "$PY" -c '
import re, sys
from pathlib import Path
merge, views, out = sys.argv[1:]
text = Path(merge).read_text(encoding="utf-8")                       # บล็อก [1] CREATE TABLE IF NOT EXISTS (ต้องมีตารางก่อนสร้าง view)
marks = list(re.finditer(r"^-- =+ \[(\d+)\]", text, re.M))
for i, m in enumerate(marks):
    if int(m[1]) == 1:
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].split("\n", 1)[1]
        Path(out, "00_curated_table.sql").write_text(body.strip().rstrip(";").strip(), encoding="utf-8")
code = "\n".join(l for l in Path(views).read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("--"))
n = 0
for st in code.split(";"):                                           # เอาเฉพาะ CREATE ... VIEW ข้าม SELECT ดูผล
    if st.strip().upper().startswith("CREATE"):
        n += 1
        Path(out, f"{n:02d}_view.sql").write_text(st.strip(), encoding="utf-8")
print(n)
' "$REPO_DIR/sql/curated_merge.sql" "$REPO_DIR/$VIEWS_SQL" "$TMP_DIR/sql" >"$TMP_DIR/nviews.txt" \
    || { warn "แยก SQL ไม่สำเร็จ ข้ามขั้นนี้"; return 0; }
  local f bad=0
  for f in "$TMP_DIR"/sql/*.sql; do
    if run_athena_file "$f"; then ok "$(basename "$f" .sql)"; else fail "$(basename "$f" .sql)"; bad=1; fi
  done
  (( bad )) && warn "สร้างบางอย่างไม่สำเร็จ (ตรวจ database $DB_CURATED, workgroup $WORKGROUP และสิทธิ์ Athena/Glue) — Lambda ยังใช้งานได้ รันสคริปต์ซ้ำหรือรัน $VIEWS_SQL เองได้" || true
}

summary() {
  step "เสร็จเรียบร้อย"
  printf '%s\n' \
    "  Flow อัตโนมัติ: incoming/<source>_<year>.csv -> check -> ingest -> validate -> transform -> verify" \
    "  ทดสอบ: อัปโหลดไฟล์ข้อมูลเข้า incoming/ (ครบ 3 แหล่ง ตัวสุดท้ายจะทำให้ transform + verify ทำงาน)" \
    "    aws s3 cp data/gee_2026/lst_2026.csv       s3://$BUCKET/incoming/lst_2026.csv" \
    "    aws s3 cp data/gee_2026/ndvi_2026.csv      s3://$BUCKET/incoming/ndvi_2026.csv" \
    "    aws s3 cp data/gee_2026/buildings_2026.csv s3://$BUCKET/incoming/buildings_2026.csv" \
    "  ไฟล์ซ้ำ (เคยสำเร็จแล้ว) จะถูกย้ายไป s3://$BUCKET/quarantine/duplicate/" \
    "  ดู log:  aws logs tail /aws/lambda/$NAME_PREFIX-check-batch --since 10m   (เปลี่ยนชื่อ -check-batch เป็นขั้นอื่นได้)" \
    "  ทดสอบ Lambda ทีละตัวโดยไม่ให้ไหลต่อ: ตั้ง env PIPELINE_EVENTS=off ที่ Lambda ตัวนั้นก่อน" \
    "  ผลลัพธ์ Q1: SELECT * FROM $DB_CURATED.v_q1_heat_vs_green;  (รันใน Athena workgroup $WORKGROUP)"
}

preflight
build_packages
deploy_lambdas
enable_event_chain
create_q1_views
summary
