# shellcheck shell=bash
# view: ตาราง curated + view ตอบ Q1
# ใช้ผ่าน infra/ronnai_aws.sh เท่านั้น (อาศัยตัวแปรที่ตั้งไว้ในไฟล์นั้น)

view() {
  preflight
  PY="$(find_python)"
  [[ -n "$PY" ]] || die "ไม่พบ python3"
  step "ตาราง curated + view ตอบ Q1 (Athena)"
  if [[ ! -f "$REPO_DIR/$VIEWS_SQL" ]]; then
    warn "ไม่พบ $VIEWS_SQL ข้ามขั้นนี้ (ตั้ง VIEWS_SQL=... ได้)"; return 0
  fi
  mkdir -p "$TMP_DIR/sql"
  # บล็อก [1] ของ curated_merge.sql = CREATE TABLE IF NOT EXISTS (ต้องมีตารางก่อนสร้าง view) + CREATE VIEW ทุกอันใน $VIEWS_SQL
  { "$PY" "$SQL_SPLIT" block "$REPO_DIR/sql/curated_merge.sql" 1 "$TMP_DIR/sql/00_curated_table.sql" \
    && "$PY" "$SQL_SPLIT" statements "$REPO_DIR/$VIEWS_SQL" "$TMP_DIR/sql" --keep CREATE --suffix _view >/dev/null; } \
    || { warn "แยก SQL ไม่สำเร็จ ข้ามขั้นนี้"; return 0; }
  local f bad=0
  for f in "$TMP_DIR"/sql/*.sql; do
    if run_athena_file "$f"; then ok "$(basename "$f" .sql)"; else fail "$(basename "$f" .sql)"; bad=1; fi
  done
  (( bad )) && warn "สร้างบางอย่างไม่สำเร็จ (ตรวจ database $DB_CURATED, workgroup $WORKGROUP และสิทธิ์ Athena/Glue) — Lambda ยังใช้งานได้ รันสคริปต์ซ้ำหรือรัน $VIEWS_SQL เองได้" || true
}
