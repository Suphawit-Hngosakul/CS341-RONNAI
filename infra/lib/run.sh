# shellcheck shell=bash
# run (Issue #18): อัปโหลด 3 แหล่งเข้า incoming/ แล้วรอดูผลของแต่ละ execution
# ใช้ผ่าน infra/ronnai_aws.sh เท่านั้น (อาศัยตัวแปรที่ตั้งไว้ในไฟล์นั้น)

run_pipeline() {
  preflight
  local arn dir src f before all new running n waited=0 ex status out states retried=0
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
  upload() {   # upload <source...>
    local s f
    for s in "$@"; do
      f="${s}_$YEAR.csv"
      aws_ s3 cp "$dir/$f" "s3://$BUCKET/incoming/$f" --only-show-errors || die "อัปโหลด $f ไม่ได้"
      ok "$f"
    done
  }
  before="$(list_execs)"

  # บัญชีใหม่: S3 -> EventBridge และกฎที่เพิ่งสร้างต้องใช้เวลาสักพักกว่าจะเริ่มส่ง event
  # ไฟล์ที่อัปทันทีหลัง deploy จะไม่ถูกส่งต่อ (event หายไปเลย ไม่ใช่มาช้า) จึงรอก่อนอัป
  if [[ "${TRIGGER_FRESH:-0}" == 1 ]]; then
    step "รอ ${TRIGGER_WARMUP_SEC} วินาทีให้ trigger ที่เพิ่งสร้างเริ่มทำงาน"
    sleep "$TRIGGER_WARMUP_SEC"
  fi

  step "อัปโหลดเข้า s3://$BUCKET/incoming/ (ปี $YEAR)"
  upload lst ndvi buildings

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
    if (( n < 3 && waited == 60 && !retried )); then              # event ของบางไฟล์หาย: อัปไฟล์นั้นซ้ำ 1 ครั้ง
      retried=1
      local missing=()
      for src in lst ndvi buildings; do
        grep -q ":${src}_$YEAR-csv-" <<<"$new" || missing+=("$src")
      done
      if (( ${#missing[@]} )); then
        warn "ผ่านไป 60 วินาที execution ของ ${missing[*]} ยังไม่เริ่ม (event อาจหายเพราะ trigger เพิ่งสร้าง) อัปโหลดซ้ำ"
        upload "${missing[@]}"
      fi
    elif (( n == 0 && waited == 150 )); then
      warn "ยังไม่มี execution เริ่มเลย ดู log ของตัวเริ่ม: aws logs tail /aws/lambda/$NAME_PREFIX-start-pipeline --since 10m"
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
