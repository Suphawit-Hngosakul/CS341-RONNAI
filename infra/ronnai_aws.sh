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
PKG_PREFIX="${PKG_PREFIX:-lambda-packages/}"  # ที่เก็บ zip ใน bucket (zip ของ validate เกิน 50 MB ต้องส่งผ่าน S3)
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
trap 'rm -rf "$TMP_DIR"' EXIT

find_python() {   # python ตัวแรกที่รันได้จริง (บน Windows python3 อาจเป็นแค่ทางลัดไป Microsoft Store)
  local c
  for c in python3 python py; do
    command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1 \
      && { command -v "$c"; return 0; }
  done
  return 0
}

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

preflight() {
  command -v aws >/dev/null 2>&1 || die "ไม่พบ AWS CLI — ติดตั้ง AWS CLI v2 ก่อน"
  step "ตรวจ credentials"
  local ident
  if ! ident="$(aws_ sts get-caller-identity --query '[Account,Arn]' --output text 2>&1)"; then
    fail "$ident"
    die "credentials ใช้ไม่ได้/หมดอายุ — กด Start Lab แล้วคัดลอก AWS CLI credentials จาก 'AWS Details' ใหม่"
  fi
  ACCOUNT_ID="$(awk '{print $1}' <<<"$ident")"
  ok "Account: $ACCOUNT_ID"
  ok "Identity: $(awk '{print $2}' <<<"$ident")"
  [[ -z "$BUCKET" ]] && BUCKET="cs341-ronnai-${ACCOUNT_ID}"
  ok "Region: $REGION | Bucket: $BUCKET | Workgroup: $WORKGROUP"
}

# ---------------------------------------------------------------------------
setup() {
  preflight

  step "1) S3 bucket + zones"
  if aws_ s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
    skip "bucket $BUCKET มีอยู่แล้ว"
  else
    if [[ "$REGION" == "us-east-1" ]]; then
      aws_ s3api create-bucket --bucket "$BUCKET" >/dev/null \
        || die "สร้าง bucket ไม่ได้ (ชื่ออาจซ้ำกับคนอื่น ลองตั้ง BUCKET=ชื่ออื่น)"
    else
      aws_ s3api create-bucket --bucket "$BUCKET" \
        --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null \
        || die "สร้าง bucket ไม่ได้ (ชื่ออาจซ้ำกับคนอื่น ลองตั้ง BUCKET=ชื่ออื่น)"
    fi
    aws_ s3api wait bucket-exists --bucket "$BUCKET"
    ok "สร้าง bucket $BUCKET"
  fi

  if aws_ s3api put-public-access-block --bucket "$BUCKET" \
       --public-access-block-configuration \
       BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true >/dev/null 2>&1; then
    ok "ปิด public access"
  else
    warn "ตั้ง public access block ไม่ได้ (Learner Lab อาจไม่อนุญาต) — ข้ามได้"
  fi

  for z in "${ZONES[@]}"; do
    aws_ s3api put-object --bucket "$BUCKET" --key "$z/" >/dev/null
    ok "s3://$BUCKET/$z/"
  done

  cat >"$TMP_DIR/lifecycle.json" <<EOF
{
  "Rules": [
    {
      "ID": "expire-athena-results",
      "Filter": {"Prefix": "athena-results/"},
      "Status": "Enabled",
      "Expiration": {"Days": $RESULT_EXPIRE_DAYS}
    },
    {
      "ID": "abort-incomplete-uploads",
      "Filter": {"Prefix": ""},
      "Status": "Enabled",
      "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}
    }
  ]
}
EOF
  if aws_ s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
       --lifecycle-configuration "file://$TMP_DIR/lifecycle.json" >/dev/null 2>&1; then
    ok "lifecycle: ลบ athena-results/ ที่เก่ากว่า $RESULT_EXPIRE_DAYS วัน"
  else
    warn "ตั้ง lifecycle ไม่ได้ — ข้ามได้ ไม่กระทบการทำงาน"
  fi

  step "2) Glue databases"
  local db zone
  for pair in "$DB_TRUSTED:trusted" "$DB_CURATED:curated"; do
    db="${pair%%:*}"; zone="${pair##*:}"
    if aws_ glue get-database --name "$db" >/dev/null 2>&1; then
      skip "Glue database $db มีอยู่แล้ว"
    else
      cat >"$TMP_DIR/db.json" <<EOF
{"Name": "$db", "Description": "CS341-RONNAI $zone zone", "LocationUri": "s3://$BUCKET/$zone/"}
EOF
      aws_ glue create-database --database-input "file://$TMP_DIR/db.json" >/dev/null
      ok "สร้าง Glue database $db -> s3://$BUCKET/$zone/"
    fi
  done

  step "3) Athena workgroup (scan limit ${SCAN_LIMIT_MB} MB/query)"
  (( SCAN_LIMIT_MB >= 10 )) || die "SCAN_LIMIT_MB ต้อง >= 10"
  local cutoff=$(( SCAN_LIMIT_MB * 1024 * 1024 ))
  local output="s3://$BUCKET/athena-results/"
  if aws_ athena get-work-group --work-group "$WORKGROUP" >/dev/null 2>&1; then
    cat >"$TMP_DIR/wg_update.json" <<EOF
{
  "ResultConfigurationUpdates": {"OutputLocation": "$output"},
  "EnforceWorkGroupConfiguration": true,
  "PublishCloudWatchMetricsEnabled": false,
  "BytesScannedCutoffPerQuery": $cutoff,
  "EngineVersion": {"SelectedEngineVersion": "Athena engine version 3"}
}
EOF
    aws_ athena update-work-group --work-group "$WORKGROUP" --state ENABLED \
      --configuration-updates "file://$TMP_DIR/wg_update.json" >/dev/null
    ok "อัปเดต workgroup $WORKGROUP"
  else
    cat >"$TMP_DIR/wg.json" <<EOF
{
  "ResultConfiguration": {"OutputLocation": "$output"},
  "EnforceWorkGroupConfiguration": true,
  "PublishCloudWatchMetricsEnabled": false,
  "BytesScannedCutoffPerQuery": $cutoff,
  "EngineVersion": {"SelectedEngineVersion": "Athena engine version 3"}
}
EOF
    aws_ athena create-work-group --name "$WORKGROUP" \
      --description "CS341-RONNAI workgroup" \
      --configuration "file://$TMP_DIR/wg.json" >/dev/null
    ok "สร้าง workgroup $WORKGROUP"
  fi
  ok "ผล query -> $output"

  step "4) config/settings.yaml"
  write_settings

  printf '\n\033[32mเสร็จแล้ว\033[0m — ต่อด้วย: ./infra/ronnai_aws.sh verify\n'
}

write_settings() {
  if [[ -f "$SETTINGS_FILE" ]]; then
    skip "มี $SETTINGS_FILE อยู่แล้ว (ไม่เขียนทับ) — เช็กว่า bucket = $BUCKET, region = $REGION, workgroup = $WORKGROUP"
    return
  fi
  mkdir -p "$(dirname "$SETTINGS_FILE")"
  cat >"$SETTINGS_FILE" <<EOF
# สร้างอัตโนมัติโดย infra/ronnai_aws.sh (ไฟล์นี้ถูก .gitignore ไว้ ห้าม commit)
# ห้ามใส่ access key / secret ในไฟล์นี้
aws:
  region: $REGION
  profile: ${AWS_PROFILE_NAME:-default}
s3:
  bucket: $BUCKET
  prefixes:
    incoming: incoming/
    raw: raw/
    trusted: trusted/
    curated: curated/
    quarantine: quarantine/
    athena_results: athena-results/
glue:
  databases:
    trusted: $DB_TRUSTED
    curated: $DB_CURATED
athena:
  workgroup: $WORKGROUP
  scan_limit_mb: $SCAN_LIMIT_MB
EOF
  ok "เขียน $SETTINGS_FILE"
}

# ---------------------------------------------------------------------------
verify() {
  preflight
  local errors=0

  step "S3 zones"
  local prefixes
  prefixes="$(aws_ s3api list-objects-v2 --bucket "$BUCKET" --delimiter / \
                --query 'CommonPrefixes[].Prefix' --output text 2>/dev/null || true)"
  for z in "${ZONES[@]}"; do
    if tr '\t' '\n' <<<"$prefixes" | grep -qxF -- "$z/"; then ok "$z/"; else fail "ไม่พบ $z/"; errors=$((errors+1)); fi
  done

  step "Glue databases"
  for db in "$DB_TRUSTED" "$DB_CURATED"; do
    if aws_ glue get-database --name "$db" >/dev/null 2>&1; then ok "$db"; else fail "ไม่พบ $db"; errors=$((errors+1)); fi
  done

  step "Athena workgroup"
  local cutoff out
  if cutoff="$(aws_ athena get-work-group --work-group "$WORKGROUP" \
                 --query 'WorkGroup.Configuration.BytesScannedCutoffPerQuery' --output text 2>/dev/null)"; then
    if [[ "$cutoff" =~ ^[0-9]+$ ]]; then
      ok "$WORKGROUP scan limit = $(( cutoff / 1024 / 1024 )) MB/query"
    else
      fail "$WORKGROUP ยังไม่ได้ตั้ง scan limit"; errors=$((errors+1))
    fi
    out="$(aws_ athena get-work-group --work-group "$WORKGROUP" \
            --query 'WorkGroup.Configuration.ResultConfiguration.OutputLocation' --output text)"
    ok "output = $out"
  else
    fail "ไม่พบ workgroup $WORKGROUP"; errors=$((errors+1))
  fi

  step "Athena: SELECT 1"
  local qid state value reason
  if qid="$(aws_ athena start-query-execution --work-group "$WORKGROUP" \
              --query-string "SELECT 1 AS ok" --query QueryExecutionId --output text 2>&1)"; then
    for _ in $(seq 1 30); do
      state="$(aws_ athena get-query-execution --query-execution-id "$qid" \
                 --query QueryExecution.Status.State --output text)"
      [[ "$state" == "SUCCEEDED" || "$state" == "FAILED" || "$state" == "CANCELLED" ]] && break
      sleep 1
    done
    if [[ "$state" == "SUCCEEDED" ]]; then
      value="$(aws_ athena get-query-results --query-execution-id "$qid" \
                 --query 'ResultSet.Rows[1].Data[0].VarCharValue' --output text)"
      ok "SELECT 1 -> $value (query id $qid)"
    else
      reason="$(aws_ athena get-query-execution --query-execution-id "$qid" \
                  --query QueryExecution.Status.StateChangeReason --output text 2>/dev/null || true)"
      fail "query $state: $reason"; errors=$((errors+1))
    fi
  else
    fail "$qid"; errors=$((errors+1))
  fi

  echo
  if (( errors == 0 )); then
    printf '\033[32mผ่านทุกข้อ\033[0m\n'
  else
    printf '\033[31mมีปัญหา %d ข้อ\033[0m — ลองรัน setup ใหม่\n' "$errors"; exit 1
  fi
}

# ---------------------------------------------------------------------------
# deploy: Lambda 6 ตัว + Step Functions + กฎ EventBridge (S3 -> Lambda start-pipeline -> state machine)

preflight_deploy() {   # ตรวจเพิ่มสำหรับ deploy (ต้องรัน preflight ก่อน)
  PY="$(find_python)"
  [[ -n "$PY" ]] || die "ไม่พบ python3"
  "$PY" -m pip --version >/dev/null 2>&1 || die "ไม่พบ pip ($PY -m pip)"
  [[ -z "$ROLE_ARN" ]] && ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/LabRole"
  ok "Role: $ROLE_ARN | Runtime: $RUNTIME"
  aws_ s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1 \
    || die "ไม่พบ bucket $BUCKET — รัน './infra/ronnai_aws.sh setup' ก่อน"
  aws_ iam get-role --role-name "${ROLE_ARN##*/}" >/dev/null 2>&1 \
    || warn "อ่าน role ${ROLE_ARN##*/} ไม่ได้ (อาจไม่มีสิทธิ์อ่าน) จะลองใช้ต่อ ถ้าสร้าง Lambda ไม่ผ่านให้ตรวจชื่อ role"
}

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
  step "แพ็กเกจ Lambda"
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
    sed -E "s/cs341-ronnai-[0-9]+/$BUCKET/g" "$REPO_DIR/sql/curated_merge.sql" >"$b/sql/curated_merge.sql"   # LOCATION = bucket ของบัญชีนี้
    "$PY" -c 'import shutil, sys; shutil.make_archive(sys.argv[2], "zip", sys.argv[1])' "$b" "$TMP_DIR/zips/$name"
    ok "$NAME_PREFIX-$name  ($(du -sk "$TMP_DIR/zips/$name.zip" | awk '{printf "%.1f", $1/1024}') MB zip)"
  done
}

deploy_lambdas() {
  step "Lambda 6 ตัว (start-pipeline + 5 ขั้นของ state machine)"
  local spec name dir to mem kind fn key envs
  # PIPELINE_EVENTS=off: Step Functions เป็นตัวเรียกขั้นถัดไป Lambda ไม่ต้องส่ง event ต่อกันเองแล้ว
  envs="Variables={BUCKET=$BUCKET,ATHENA_WORKGROUP=$WORKGROUP,CURATED_DB=$DB_CURATED,TRUSTED_DB=$DB_TRUSTED,PIPELINE_EVENTS=off,STATE_MACHINE_ARN=$(sfn_arn)}"
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
  done
}

sfn_arn() { printf 'arn:aws:states:%s:%s:stateMachine:%s' "$REGION" "$ACCOUNT_ID" "${SFN_NAME:-$NAME_PREFIX-pipeline}"; }
OLD_CHAIN_RULES=(batch-checked batch-ingested batch-validated curated-transformed)   # กฎของแบบเดิม (Lambda ส่ง event ต่อกันเอง)

delete_rule() {    # delete_rule <ชื่อกฎ>  ลบ target ก่อนแล้วลบกฎ (ไม่มี -> ข้าม)
  local rule="$1" ids
  if aws_ events describe-rule --name "$rule" >/dev/null 2>&1; then
    ids="$(aws_ events list-targets-by-rule --rule "$rule" --query 'Targets[].Id' --output text 2>/dev/null || true)"
    if [[ -n "$ids" && "$ids" != "None" ]]; then aws_ events remove-targets --rule "$rule" --ids $ids >/dev/null; fi
    aws_ events delete-rule --name "$rule" >/dev/null && ok "ลบ $rule" || fail "ลบ $rule ไม่ได้"
  else
    skip "ไม่พบ $rule"
  fi
}

deploy_state_machine() {
  local name="${SFN_NAME:-$NAME_PREFIX-pipeline}" arn
  arn="$(sfn_arn)"
  step "Step Functions: $name"
  [[ -f "$REPO_DIR/$ASL_FILE" ]] || die "ไม่พบ $ASL_FILE"
  sed -e "s|\${REGION}|$REGION|g" -e "s|\${ACCOUNT_ID}|$ACCOUNT_ID|g" -e "s|\${NAME_PREFIX}|$NAME_PREFIX|g" \
    "$REPO_DIR/$ASL_FILE" >"$TMP_DIR/asl.json"
  if aws_ stepfunctions describe-state-machine --state-machine-arn "$arn" >/dev/null 2>&1; then
    aws_ stepfunctions update-state-machine --state-machine-arn "$arn" \
      --definition "file://$TMP_DIR/asl.json" --role-arn "$ROLE_ARN" >/dev/null \
      || die "อัปเดต state machine $name ไม่ได้"
    ok "อัปเดต $name"
  else
    aws_ stepfunctions create-state-machine --name "$name" --type STANDARD \
      --definition "file://$TMP_DIR/asl.json" --role-arn "$ROLE_ARN" >/dev/null \
      || die "สร้าง state machine $name ไม่ได้ (ตรวจ role $ROLE_ARN และสิทธิ์ Step Functions)"
    ok "สร้าง $name"
  fi
}

enable_trigger() {
  step "EventBridge: ไฟล์ใหม่ใน incoming/ -> เริ่ม execution ของ state machine"
  local cur compact rule="$NAME_PREFIX-incoming-csv" r
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
  printf '%s' "{\"source\": [\"aws.s3\"], \"detail-type\": [\"Object Created\"], \"detail\": {\"bucket\": {\"name\": [\"$BUCKET\"]}, \"object\": {\"key\": [{\"prefix\": \"incoming/\"}]}}}" \
    >"$TMP_DIR/pattern.json"
  aws_ events put-rule --name "$rule" --event-pattern "file://$TMP_DIR/pattern.json" --state ENABLED >/dev/null \
    || die "สร้างกฎ $rule ไม่ได้"
  # EventBridge -> Step Functions ตรง ๆ ต้องใช้ RoleArn ที่ LabRole ไม่ยอม (execution ไม่เริ่มเลย ไม่มี error)
  # จึงยิงเข้า Lambda start-pipeline (ใช้ resource policy) แล้ว Lambda เรียก StartExecution ให้
  local fn="$NAME_PREFIX-start-pipeline" fn_arn err
  fn_arn="$(aws_ lambda get-function --function-name "$fn" --query 'Configuration.FunctionArn' --output text | tr -d '\r')"
  if ! err="$(aws_ lambda add-permission --function-name "$fn" --statement-id "events-$rule" \
        --action lambda:InvokeFunction --principal events.amazonaws.com \
        --source-arn "arn:aws:events:$REGION:$ACCOUNT_ID:rule/$rule" 2>&1)"; then
    grep -q ResourceConflictException <<<"$err" || die "ให้สิทธิ์กฎ $rule เรียก $fn ไม่ได้: $err"
  fi
  aws_ events put-targets --rule "$rule" --targets "Id=1,Arn=$fn_arn" >/dev/null \
    || die "ผูกกฎ $rule กับ $fn ไม่ได้"
  ok "กฎ $rule -> $fn -> $(sfn_arn)"
  aws_ lambda remove-permission --function-name "$NAME_PREFIX-check-batch" --statement-id "events-$rule" >/dev/null 2>&1 || true

  step "ลบกฎ EventBridge ของแบบเดิม (Lambda ต่อกันเอง) ถ้ามี"
  for r in "${OLD_CHAIN_RULES[@]}"; do delete_rule "$NAME_PREFIX-$r"; done
}

create_trusted_tables() {   # CREATE EXTERNAL TABLE + MSCK REPAIR จาก glue_trusted.sql (แทนชื่อ bucket ให้ตรงบัญชี)
  step "ตาราง trusted (Glue) จาก $TRUSTED_SQL"
  if [[ ! -f "$REPO_DIR/$TRUSTED_SQL" ]]; then
    warn "ไม่พบ $TRUSTED_SQL ข้ามขั้นนี้ (ตั้ง TRUSTED_SQL=... ได้) — ต้องสร้างตาราง trusted เองก่อน Lambda จะ MERGE ได้"; return 0
  fi
  mkdir -p "$TMP_DIR/trusted"
  "$PY" -c '
import re, sys
from pathlib import Path
src, out = sys.argv[1:]
code = "\n".join(l for l in Path(src).read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("--"))
n = 0
for st in code.split(";"):                                           # เอาเฉพาะ CREATE (ตาราง) และ MSCK (โหลด partition) ข้าม SELECT ตรวจผล
    first = st.strip().split(None, 1)[0].upper() if st.strip() else ""
    if first in ("CREATE", "MSCK"):
        n += 1
        Path(out, f"{n:02d}.sql").write_text(st.strip(), encoding="utf-8")
print(n)
' "$REPO_DIR/$TRUSTED_SQL" "$TMP_DIR/trusted" >/dev/null || { warn "แยก SQL ไม่สำเร็จ ข้ามขั้นนี้"; return 0; }
  local f bad=0 label
  for f in "$TMP_DIR"/trusted/*.sql; do
    label="$(tr -s ' \n' ' ' <"$f" | cut -c1-70)"
    if run_athena_file "$f"; then ok "$label"; else fail "$label"; bad=1; fi
  done
  if (( bad )); then
    warn "สร้างตาราง trusted ไม่สำเร็จบางส่วน (ตรวจ database $DB_TRUSTED, workgroup $WORKGROUP และสิทธิ์ Athena/Glue)"
  fi
  warn "ถ้ามีตาราง trusted เดิมที่ไม่มีคอลัมน์ batch_id อยู่แล้ว CREATE ... IF NOT EXISTS จะไม่แก้ให้ ต้อง DROP แล้วรันใหม่"
}

deploy() {
  preflight
  preflight_deploy
  create_trusted_tables
  build_packages
  deploy_lambdas
  deploy_state_machine
  enable_trigger
  step "เสร็จเรียบร้อย"
  printf '%s\n' \
    "  Flow อัตโนมัติ: incoming/<source>_<year>.csv -> Step Functions $(sfn_arn)" \
    "                  CheckBatch -> IngestBatch -> ValidateBatch -> TransformCurated -> VerifyCurated" \
    "  ต่อด้วย: ./infra/ronnai_aws.sh view   (สร้างตาราง curated + view ตอบ Q1)" \
    "  รันทั้ง pipeline: ./infra/ronnai_aws.sh run   (อัปโหลด data/gee_$YEAR/ ทั้ง 3 แหล่ง แล้วรอดูผลแต่ละขั้น)" \
    "  ไฟล์ซ้ำ (เคยสำเร็จแล้ว) จะถูกย้ายไป s3://$BUCKET/quarantine/duplicate/ และ execution จบที่ AlreadyProcessed" \
    "  ดูทีละขั้น: Step Functions console > $NAME_PREFIX-pipeline > execution > Graph view" \
    "  ดู log ของ Lambda: aws logs tail /aws/lambda/$NAME_PREFIX-check-batch --since 10m"
}

# ---------------------------------------------------------------------------
# run (Issue #18): อัปโหลด 3 แหล่งเข้า incoming/ แล้วรอดู execution ที่เกิดขึ้น
run_pipeline() {
  preflight
  local arn dir src f before all new running n waited=0 ex status out states
  arn="$(sfn_arn)"
  aws_ stepfunctions describe-state-machine --state-machine-arn "$arn" >/dev/null 2>&1 \
    || die "ไม่พบ state machine $arn — รัน './infra/ronnai_aws.sh deploy' ก่อน"
  dir="$REPO_DIR/data/gee_$YEAR"
  for src in lst ndvi buildings; do
    [[ -f "$dir/${src}_$YEAR.csv" ]] || die "ไม่พบ $dir/${src}_$YEAR.csv (ตั้ง YEAR=... ได้)"
  done
  list_execs() {
    aws_ stepfunctions list-executions --state-machine-arn "$arn" --max-results 100 --no-paginate \
      --query 'executions[].executionArn' --output text | tr -d '\r' | tr '\t' '\n' | grep -v '^None$' | sort || true
  }
  before="$(list_execs)"

  step "อัปโหลดเข้า s3://$BUCKET/incoming/ (ปี $YEAR)"
  for src in lst ndvi buildings; do
    f="${src}_$YEAR.csv"
    aws_ s3 cp "$dir/$f" "s3://$BUCKET/incoming/$f" --only-show-errors || die "อัปโหลด $f ไม่ได้"
    ok "$f"
  done

  step "รอ execution ทั้ง 3 จบ (สูงสุด $RUN_WAIT_SEC วินาที)"
  while :; do
    all="$(list_execs)"
    new="$(comm -13 <(printf '%s\n' "$before") <(printf '%s\n' "$all") | grep -v '^$' || true)"
    n="$(grep -c . <<<"$new" || true)"
    running=0
    for ex in $new; do
      [[ "$(aws_ stepfunctions describe-execution --execution-arn "$ex" --query status --output text | tr -d '\r')" == RUNNING ]] && running=$((running + 1))
    done
    printf '  ... %3ss  เริ่มแล้ว %s/3  กำลังรัน %s\n' "$waited" "$n" "$running"
    (( n >= 3 && running == 0 )) && break
    if (( n == 0 && waited >= 60 )); then
      warn "ผ่านไป ${waited} วินาทียังไม่มี execution เริ่มเลย ดู log ของตัวเริ่ม:"
      warn "  aws logs tail /aws/lambda/$NAME_PREFIX-start-pipeline --since 10m"
    fi
    (( waited >= RUN_WAIT_SEC )) && { warn "รอเกิน $RUN_WAIT_SEC วินาที ดูต่อที่ Step Functions console"; break; }
    sleep 10; waited=$((waited + 10))
  done

  step "ผลแต่ละ execution (ขั้นที่ผ่าน -> สถานะ)"
  local failed=0
  for ex in $new; do
    status="$(aws_ stepfunctions describe-execution --execution-arn "$ex" --query status --output text | tr -d '\r')"
    states="$(aws_ stepfunctions get-execution-history --execution-arn "$ex" --max-results 200 --no-paginate \
      --query "events[?type=='TaskStateExited' || type=='ChoiceStateExited' || type=='SucceedStateExited'].stateExitedEventDetails.name" \
      --output text | tr -d '\r' | tr '\t' ' ' | sed 's/ / -> /g')"
    out="$(aws_ stepfunctions describe-execution --execution-arn "$ex" \
      --query '[output, error, cause]' --output text | tr -d '\r' | tr '\t' ' ' | cut -c1-300)"
    case "$status" in
      SUCCEEDED) ok "${ex##*:}  $status" ;;
      *) fail "${ex##*:}  $status"; failed=1 ;;
    esac
    printf '         ขั้น: %s\n         %s\n' "${states:-"-"}" "$out"
  done
  printf '\n  ดูกราฟ: https://%s.console.aws.amazon.com/states/home?region=%s#/statemachines/view/%s\n' "$REGION" "$REGION" "$arn"
  (( failed )) && die "มี execution ที่ไม่สำเร็จ" || ok "เสร็จ (รันซ้ำได้: ไฟล์เดิมจะจบที่ AlreadyProcessed)"
}

# ---------------------------------------------------------------------------
# view: ตาราง curated + view ตอบ Q1
run_athena_file() {   # run_athena_file <ไฟล์ .sql 1 statement>  0 = สำเร็จ
  local qid state reason
  qid="$(aws_ athena start-query-execution --query-string "$(sed -E "s/cs341-ronnai-[0-9]+/$BUCKET/g" "$1")" --work-group "$WORKGROUP" \
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

view() {
  preflight
  PY="$(find_python)"
  [[ -n "$PY" ]] || die "ไม่พบ python3"
  step "ตาราง curated + view ตอบ Q1 (Athena)"
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

# ---------------------------------------------------------------------------
destroy_pipeline() {   # ลบกฎ EventBridge ก่อน (กันไฟล์ใหม่ไปเริ่ม execution) แล้วลบ state machine และ Lambda
  local r spec name fn arn
  step "ลบกฎ EventBridge"
  delete_rule "$NAME_PREFIX-incoming-csv"
  for r in "${OLD_CHAIN_RULES[@]}"; do delete_rule "$NAME_PREFIX-$r"; done
  step "ลบ state machine"
  arn="$(sfn_arn)"
  if aws_ stepfunctions describe-state-machine --state-machine-arn "$arn" >/dev/null 2>&1; then
    aws_ stepfunctions delete-state-machine --state-machine-arn "$arn" >/dev/null && ok "ลบ ${arn##*:}" || fail "ลบ ${arn##*:} ไม่ได้"
  else
    skip "ไม่พบ ${arn##*:}"
  fi
  step "ลบ Lambda"
  for spec in "${FUNCS[@]}"; do
    name="${spec%%:*}"; fn="$NAME_PREFIX-$name"
    if aws_ lambda get-function --function-name "$fn" >/dev/null 2>&1; then
      aws_ lambda delete-function --function-name "$fn" >/dev/null && ok "ลบ $fn" || fail "ลบ $fn ไม่ได้"
    else
      skip "ไม่พบ $fn"
    fi
  done
}

destroy() {
  preflight
  printf '\n\033[31mจะลบถาวร:\033[0m state machine + Lambda 6 ตัว (%s-*), กฎ EventBridge, bucket s3://%s (รวม raw/ และ quarantine/), Glue DB %s %s, workgroup %s\n' \
    "$NAME_PREFIX" "$BUCKET" "$DB_TRUSTED" "$DB_CURATED" "$WORKGROUP"
  read -r -p "พิมพ์ชื่อ bucket เพื่อยืนยัน: " answer
  [[ "$answer" == "$BUCKET" ]] || die "ยกเลิก (ชื่อไม่ตรง)"

  destroy_pipeline

  step "ลบ Athena workgroup"
  aws_ athena delete-work-group --work-group "$WORKGROUP" --recursive-delete-option >/dev/null 2>&1 \
    && ok "$WORKGROUP" || skip "ไม่พบ $WORKGROUP"

  step "ลบ Glue databases (รวมตารางข้างใน)"
  for db in "$DB_TRUSTED" "$DB_CURATED"; do
    aws_ glue delete-database --name "$db" >/dev/null 2>&1 && ok "$db" || skip "ไม่พบ $db"
  done

  step "ลบ S3 bucket"
  if aws_ s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
    aws_ s3 rm "s3://$BUCKET" --recursive --only-show-errors
    aws_ s3api delete-bucket --bucket "$BUCKET"
    ok "$BUCKET"
  else
    skip "ไม่พบ $BUCKET"
  fi
}

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
