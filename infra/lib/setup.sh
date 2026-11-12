# shellcheck shell=bash
# setup / verify: S3 bucket + zones, Glue DB, Athena workgroup, config/settings.yaml
# ใช้ผ่าน infra/ronnai_aws.sh เท่านั้น (อาศัยตัวแปรที่ตั้งไว้ในไฟล์นั้น)

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
