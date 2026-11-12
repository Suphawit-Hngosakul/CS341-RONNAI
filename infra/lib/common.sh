# shellcheck shell=bash
# ของที่ทุกคำสั่งใช้: ข้อความ, aws_, preflight, Athena, ชื่อ state machine, ลบกฎ EventBridge
# ใช้ผ่าน infra/ronnai_aws.sh เท่านั้น (อาศัยตัวแปรที่ตั้งไว้ในไฟล์นั้น)

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
