# shellcheck shell=bash
# destroy: ลบทุกอย่าง (ต้องพิมพ์ชื่อ bucket ยืนยัน)
# ใช้ผ่าน infra/ronnai_aws.sh เท่านั้น (อาศัยตัวแปรที่ตั้งไว้ในไฟล์นั้น)

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
