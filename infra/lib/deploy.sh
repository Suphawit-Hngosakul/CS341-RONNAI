# shellcheck shell=bash
# deploy: ตาราง trusted + Lambda 6 ตัว + state machine + trigger (S3 -> EventBridge -> start-pipeline)
# ใช้ผ่าน infra/ronnai_aws.sh เท่านั้น (อาศัยตัวแปรที่ตั้งไว้ในไฟล์นั้น)

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
           ingestion/ingest.py ingestion/validate_gate.py ingestion/verify_curated.py; do
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
    cp "$REPO_DIR"/ingestion/ingest.py "$REPO_DIR"/ingestion/validate_gate.py "$REPO_DIR"/ingestion/verify_curated.py "$b/"
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
  envs="Variables={BUCKET=$BUCKET,ATHENA_WORKGROUP=$WORKGROUP,CURATED_DB=$DB_CURATED,TRUSTED_DB=$DB_TRUSTED,STATE_MACHINE_ARN=$(sfn_arn)}"
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
  "$PY" "$SQL_SPLIT" statements "$REPO_DIR/$TRUSTED_SQL" "$TMP_DIR/trusted" --keep CREATE MSCK >/dev/null \
    || { warn "แยก SQL ไม่สำเร็จ ข้ามขั้นนี้"; return 0; }   # เอาเฉพาะ CREATE (ตาราง) และ MSCK (โหลด partition) ข้าม SELECT ตรวจผล
  local f bad=0 label
  for f in "$TMP_DIR"/trusted/*.sql; do
    label="$(tr -d '\r' <"$f" | tr -s ' \n' ' ' | cut -c1-70)"
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
