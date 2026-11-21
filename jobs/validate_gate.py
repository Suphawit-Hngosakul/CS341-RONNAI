"""ด่านตรวจคุณภาพ (Validation gate, Issue #13): raw/ -> trusted/ + quarantine/

ขั้นตอน (ตาม contracts/<source>.yaml)
  1. อ่าน contract ของแหล่งนั้น
  2. STANDARDIZE  ตัด BOM, ตัดช่องว่าง, ปรับชื่อคอลัมน์ (ตัวเล็ก/ช่องว่าง -> _), แปลงชนิดข้อมูล
                  (เช่น building_count '751.0' -> 751)
     BLOCK        แถวที่ผิด -> quarantine/  ถ้าผิดทั้งไฟล์ (คอลัมน์หาย, ไม่มีแถว, อ่านไม่ได้) กักทั้ง batch
     WARN         ผ่านได้ แต่จดลง log และ report
  3. แถวที่ผ่าน -> Parquet ที่ trusted/<source>/year=<year>/<source>.parquet
     (upsert ตาม key: แถว key เดิมถูกแทนด้วยของ batch ใหม่ แถวอื่นในปีนั้นคงไว้ -> รันซ้ำผลเหมือนเดิม)
  4. แถวที่ไม่ผ่าน -> quarantine/<source>/<batch_id>/rows.csv + report.json
  5. ไม่ลบ/ไม่เขียนทับอะไรใน quarantine: ถ้า quarantine/<source>/<batch_id>/ มีอยู่แล้วจะหยุด

จำนวนแถว trusted (ของ batch นี้) + quarantined = จำนวนแถว input เสมอ (สคริปต์ assert ไว้)

ตัวอย่าง (S3 จริง อ่าน batch จาก raw/ ที่ ingest.py สร้าง)
  python ingestion/validate_gate.py --source lst --latest
  python ingestion/validate_gate.py --source lst --batch-id lst_2026_20261111T1200

ตัวอย่าง (บนเครื่อง ไม่แตะ AWS: เขียนผลลง .local_lake/ ด้วย layout เดียวกับ S3)
  python ingestion/validate_gate.py --local --file data/gee_2026/lst_2026.csv --source lst --year 2026
  python ingestion/validate_gate.py --local --file tests/fixtures/lst_2026_bad_demo.csv --source lst --year 2026
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yaml

SCRIPT_VERSION = "1.0.0"
REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = REPO_ROOT / "contracts"
DEFAULT_CONFIG = REPO_ROOT / "config" / "settings.yaml"
DEFAULT_LOCAL_ROOT = REPO_ROOT / ".local_lake"
DEFAULT_LOG_DIR = REPO_ROOT / "logs" / "gate"
BKK = timezone(timedelta(hours=7))
MAX_EXAMPLES = 3

EXIT_OK = 0                # ผ่านทั้งหมด หรือมีบางแถวถูกกัก (ดู report)
EXIT_USAGE = 1
EXIT_ALREADY_DONE = 2      # batch นี้มีใน quarantine แล้ว (ไม่เขียนทับ)
EXIT_AWS = 3
EXIT_BATCH_BLOCKED = 4     # ทั้ง batch ถูกกัก (ผิดระดับไฟล์)


class GateError(Exception):
    def __init__(self, msg: str, code: int = EXIT_USAGE):
        super().__init__(msg)
        self.code = code


# ====================================================================== contract

def load_contract(source: str, contracts_dir: Path = CONTRACTS_DIR) -> dict:
    path = contracts_dir / f"{source}.yaml"
    if not path.exists():
        raise GateError(f"ไม่พบ contract {path}")
    with path.open(encoding="utf-8") as f:
        c = yaml.safe_load(f)
    if c.get("source") != source:
        raise GateError(f"{path}: source ใน contract ({c.get('source')!r}) ไม่ตรงกับ {source!r}")
    return c


def load_master_list(contract: dict, repo_root: Path = REPO_ROOT) -> set[str] | None:
    ml = contract.get("master_list")
    if not ml:
        return None
    path = repo_root / ml["path"]
    if not path.exists():
        raise GateError(f"ไม่พบ master list {path} (ต้องใช้ตรวจ in_master_list)")
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    return set(df[ml["column"]].str.strip())


# ====================================================================== storage
# S3 กับโฟลเดอร์บนเครื่องใช้ key แบบเดียวกัน เช่น trusted/lst/year=2026/lst.parquet

class LocalStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.label = str(self.root)

    def read(self, key: str) -> bytes | None:
        p = self.root / key
        return p.read_bytes() if p.is_file() else None

    def write(self, key: str, data: bytes) -> None:
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def exists_prefix(self, prefix: str) -> bool:
        p = self.root / prefix
        return p.is_dir() and any(p.iterdir())

    def list(self, prefix: str) -> list[str]:
        base = self.root / prefix
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.root)).replace("\\", "/")
                      for p in base.rglob("*") if p.is_file())


class S3Store:
    def __init__(self, client, bucket: str):
        self.s3 = client
        self.bucket = bucket
        self.label = f"s3://{bucket}"

    def read(self, key: str) -> bytes | None:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None

    def write(self, key: str, data: bytes) -> None:
        ctype = {"json": "application/json", "csv": "text/csv"}.get(key.rsplit(".", 1)[-1],
                                                                      "application/octet-stream")
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=ctype)

    def exists_prefix(self, prefix: str) -> bool:
        return self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix, MaxKeys=1).get("KeyCount", 0) > 0

    def list(self, prefix: str) -> list[str]:
        out = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            out += [o["Key"] for o in page.get("Contents", [])]
        return sorted(out)


def zone(cfg: dict, name: str) -> str:
    p = ((cfg.get("s3") or {}).get("prefixes") or {}).get(name, f"{name}/")
    return p.rstrip("/") + "/"


def trusted_key(cfg: dict, source: str, year: int) -> str:
    return f"{zone(cfg, 'trusted')}{source}/year={year}/{source}.parquet"


def quarantine_prefix(cfg: dict, source: str, batch_id: str) -> str:
    return f"{zone(cfg, 'quarantine')}{source}/{batch_id}/"


# ====================================================================== rules

def rule_label(col: str, chk: dict) -> str:
    r = chk["rule"]
    if r == "range":
        return f"{col} between {chk['min']} and {chk['max']}"
    if r == "min":
        return f"{col} >= {chk['value']}"
    if r == "max":
        return f"{col} <= {chk['value']}"
    if r == "allowed_values":
        return f"{col} in {chk['values']}"
    if r == "not_equal":
        return f"{col} != {chk['value']}"
    if r == "not_null":
        return f"{col} not null"
    if r == "in_master_list":
        return f"{col} in master list"
    return f"{col} {r}"


def check_mask(series: pd.Series, chk: dict, master: set[str] | None) -> pd.Series:
    """คืน mask ของแถวที่ 'ผิด' กฎ (ค่าว่างไม่นับว่าผิด ยกเว้นกฎ not_null)"""
    r = chk["rule"]
    notna = series.notna()
    if r == "not_null":
        return ~notna
    if r == "range":
        bad = (series < chk["min"]) | (series > chk["max"])
    elif r == "min":
        bad = series < chk["value"]
    elif r == "max":
        bad = series > chk["value"]
    elif r == "allowed_values":
        bad = ~series.isin(chk["values"])
    elif r == "not_equal":
        bad = series == chk["value"]
    elif r == "in_master_list":
        if master is None:
            raise GateError("contract ใช้กฎ in_master_list แต่ไม่ได้กำหนด master_list")
        bad = ~series.isin(master)
    else:
        raise GateError(f"ไม่รู้จักกฎ {r!r} (คอลัมน์ {series.name})")
    return bad.fillna(False).astype(bool) & notna


def normalize_col(name: str) -> str:
    return re.sub(r"\s+", "_", name.replace("﻿", "").strip()).lower()


# ====================================================================== gate

@dataclass
class GateResult:
    report: dict
    trusted: pd.DataFrame          # แถวที่ผ่าน (ชนิดข้อมูลตาม contract)
    quarantined: pd.DataFrame      # แถวที่ไม่ผ่าน (ค่าดิบ + _row_number + _violations)
    blocked_batch: bool = False
    warnings: list = field(default_factory=list)


def _json_value(v):
    if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NA:
        return None
    if hasattr(v, "item"):
        v = v.item()
    return v


def _example(raw: pd.DataFrame, typed: pd.DataFrame, idx, cols: list[str]) -> dict:
    ex = {}
    for c in cols:
        if c not in raw.columns:
            continue
        v = typed.at[idx, c] if c in typed.columns and pd.notna(typed.at[idx, c]) else raw.at[idx, c]
        ex[c] = _json_value(v)
    return ex


def run_gate(csv_bytes: bytes, contract: dict, *, batch_id: str, year: int,
             master: set[str] | None) -> GateResult:
    source = contract["source"]
    columns = contract["columns"]
    col_names = [c["name"] for c in columns]
    key = contract.get("key", [])
    std_steps = set(contract.get("standardize") or [])
    standardized: dict[str, int] = {}

    def note_std(what: str, n: int):
        if n:
            standardized[what] = standardized.get(what, 0) + int(n)

    base_report = {
        "batch_id": batch_id, "source": source, "year": year,
        "contract_version": contract.get("version"),
        "gate_version": SCRIPT_VERSION,
        "validated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    def blocked(rule: str, detail: str, total_rows: int, raw_df=None) -> GateResult:
        rep = {**base_report, "status": "BATCH_BLOCKED", "total_rows": total_rows, "passed": 0,
               "quarantined": total_rows,
               "violations": [{"rule": rule, "level": "BLOCK", "scope": "batch",
                               "rows": total_rows, "detail": detail}],
               "warnings": [], "standardized": standardized}
        q = raw_df if raw_df is not None else pd.DataFrame()
        return GateResult(rep, pd.DataFrame(), q, blocked_batch=True)

    # ---- อ่านไฟล์ (strip BOM ด้วย utf-8-sig) ทุกค่าเป็น string ก่อน
    encoding = "utf-8-sig" if "strip_bom_from_header" in std_steps else "utf-8"
    if csv_bytes.startswith(b"\xef\xbb\xbf"):
        note_std("strip_bom_from_header", 1)
    try:
        raw = pd.read_csv(io.BytesIO(csv_bytes), dtype=str, keep_default_na=False, encoding=encoding)
    except (UnicodeDecodeError, pd.errors.ParserError, pd.errors.EmptyDataError) as e:
        n = max(csv_bytes.count(b"\n") - 1, 0)
        return blocked("file readable as CSV (utf-8)", f"{type(e).__name__}: {e}", n)
    total = len(raw)

    # ---- STANDARDIZE: ชื่อคอลัมน์
    new_cols = [normalize_col(c) for c in raw.columns]
    renames = {**(contract.get("rename") or {})}
    new_cols = [renames.get(c, c) for c in new_cols]
    note_std("normalize_column_names", sum(a != b for a, b in zip(raw.columns, new_cols)))
    raw.columns = new_cols

    # ---- STANDARDIZE: ตัดช่องว่าง
    if "trim_whitespace" in std_steps:
        for c in raw.columns:
            stripped = raw[c].str.strip()
            note_std("trim_whitespace", (stripped != raw[c]).sum())
            raw[c] = stripped

    raw.insert(0, "_row_number", range(2, total + 2))  # เลขบรรทัดในไฟล์ (บรรทัด 1 = header)

    # ---- BLOCK ระดับไฟล์
    missing = [c for c in col_names if c not in raw.columns]
    if missing:
        return blocked("required_columns", f"ไม่มีคอลัมน์ {missing}", total, raw)
    if len(set(raw.columns)) != len(raw.columns):
        dup = sorted({c for c in raw.columns if list(raw.columns).count(c) > 1})
        return blocked("required_columns", f"ชื่อคอลัมน์ซ้ำ {dup}", total, raw)
    for tc in contract.get("table_checks") or []:
        if tc["rule"] == "row_count_min" and total < tc["value"]:
            return blocked(f"row_count >= {tc['value']}", f"มี {total} แถว", total, raw)

    warnings_out = []
    extra = [c for c in raw.columns if c not in col_names and c != "_row_number"]
    if extra:
        warnings_out.append({"rule": "no unexpected columns", "level": "WARN", "rows": total,
                             "detail": f"คอลัมน์เกิน {extra} (ไม่เอาเข้า trusted)"})

    violations: dict[str, dict] = {}    # rule -> {level, mask}
    order: list[str] = []

    def add(rule: str, level: str, mask: pd.Series, cols: list[str]):
        if not mask.any():
            return
        if rule not in violations:
            violations[rule] = {"level": level, "mask": mask.copy(), "cols": cols}
            order.append(rule)
        else:
            violations[rule]["mask"] |= mask

    # ---- STANDARDIZE: cast_types + ตรวจค่าว่าง
    typed = pd.DataFrame(index=raw.index)
    for col in columns:
        name, ctype = col["name"], col.get("type", "string")
        s = raw[name].replace("", pd.NA)
        if ctype == "string":
            typed[name] = s.astype("string")
        elif ctype in ("float", "int"):
            num = pd.to_numeric(s, errors="coerce")
            not_number = s.notna() & num.isna()
            add(f"{name} is {ctype}", "BLOCK", not_number, ["_row_number", *key, name])
            if ctype == "int":
                not_integral = num.notna() & (num != num.round())
                add(f"{name} is {ctype}", "BLOCK", not_integral, ["_row_number", *key, name])
                had_decimal = s.notna() & ~not_number & ~not_integral & s.str.contains(r"[.eE]", na=False)
                note_std(f"cast {name} float->int", had_decimal.sum())
                typed[name] = num.where(~not_integral).round().astype("Int64")
            else:
                typed[name] = num.astype("float64")
        else:
            raise GateError(f"ไม่รู้จักชนิด {ctype!r} ของคอลัมน์ {name}")

        if not col.get("nullable", True):
            add(f"{name} not null", "BLOCK", s.isna(), ["_row_number", *key, name])

    # ---- ปีในไฟล์ต้องตรงกับปีของ batch (เพราะเขียนลง partition year=<year>)
    if "year" in typed.columns:
        add(f"year == {year}", "BLOCK", typed["year"].notna() & (typed["year"] != year),
            ["_row_number", *key])

    # ---- กฎรายคอลัมน์
    for col in columns:
        for chk in col.get("checks") or []:
            mask = check_mask(typed[col["name"]], chk, master)
            add(rule_label(col["name"], chk), chk.get("level", "BLOCK").upper(), mask,
                ["_row_number", *dict.fromkeys([*key, col["name"]])])

    # ---- key ซ้ำ: กักทุกแถวที่ key ชนกัน (ไม่รู้ว่าแถวไหนถูก)
    for tc in contract.get("table_checks") or []:
        if tc["rule"] == "unique_key" and key:
            dup = typed.duplicated(subset=key, keep=False) & typed[key].notna().all(axis=1)
            add(f"unique key ({', '.join(key)})", tc.get("level", "BLOCK").upper(), dup,
                ["_row_number", *key])

    # ---- แยกแถว
    block_mask = pd.Series(False, index=raw.index)
    per_row: dict = {i: [] for i in raw.index}
    viol_out = []
    for rule in order:
        v = violations[rule]
        m = v["mask"]
        idxs = list(raw.index[m])
        entry = {"rule": rule, "level": v["level"], "rows": len(idxs),
                 "example": _example(raw, typed, idxs[0], v["cols"]),
                 "examples": [_example(raw, typed, i, v["cols"]) for i in idxs[:MAX_EXAMPLES]]}
        if v["level"] == "BLOCK":
            block_mask |= m
            for i in idxs:
                per_row[i].append(rule)
            viol_out.append(entry)
        else:
            warnings_out.append(entry)

    passed_idx = raw.index[~block_mask]
    q_idx = raw.index[block_mask]
    assert len(passed_idx) + len(q_idx) == total  # ไม่มีแถวหาย

    trusted = typed.loc[passed_idx, col_names].reset_index(drop=True)
    quarantined = raw.loc[q_idx].copy()
    quarantined["_violations"] = ["; ".join(per_row[i]) for i in q_idx]

    report = {**base_report,
              "status": "PASSED" if len(q_idx) == 0 else "PARTIAL",
              "total_rows": total, "passed": int(len(passed_idx)), "quarantined": int(len(q_idx)),
              "violations": viol_out, "warnings": warnings_out, "standardized": standardized}
    return GateResult(report, trusted, quarantined.reset_index(drop=True), warnings=warnings_out)


# ====================================================================== write outputs

def to_parquet_bytes(df: pd.DataFrame) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.Table.from_pandas(df, preserve_index=False)
    # ใช้ string ธรรมดา (ไม่ใช่ large_string) ให้ Glue/Athena อ่านง่าย
    schema = pa.schema([f.with_type(pa.string()) if pa.types.is_large_string(f.type) else f
                        for f in table.schema]).remove_metadata()
    buf = io.BytesIO()
    pq.write_table(table.cast(schema), buf)
    return buf.getvalue()


def upsert_trusted(store, key_path: str, new: pd.DataFrame, key: list[str]) -> tuple[int, int]:
    """รวมแถวใหม่เข้ากับไฟล์ trusted ของปีนั้น: key เดียวกันใช้ของใหม่ คืน (แถวก่อน, แถวหลัง)"""
    old_bytes = store.read(key_path)
    before = 0
    if old_bytes is not None:
        old = pd.read_parquet(io.BytesIO(old_bytes))
        before = len(old)
        if len(new):
            k_new = pd.MultiIndex.from_frame(new[key].astype(str))
            keep = ~pd.MultiIndex.from_frame(old[key].astype(str)).isin(k_new)
            old = old[keep]
        merged = pd.concat([old, new], ignore_index=True) if len(new) else old
    else:
        merged = new
    merged = merged.sort_values(key).reset_index(drop=True)
    store.write(key_path, to_parquet_bytes(merged))
    return before, len(merged)


def write_outputs(store, cfg: dict, result: GateResult, contract: dict, *,
                  source: str, year: int, batch_id: str, original_bytes: bytes,
                  original_name: str) -> dict:
    rep = result.report
    qprefix = quarantine_prefix(cfg, source, batch_id)
    written = {}

    if result.blocked_batch:
        # กักทั้ง batch: เก็บไฟล์ต้นฉบับทั้งไฟล์ไว้ ไม่แตะ trusted
        store.write(qprefix + original_name, original_bytes)
        written["quarantine_file"] = qprefix + original_name
    else:
        if rep["passed"] > 0:
            tkey = trusted_key(cfg, source, year)
            out = result.trusted.drop(columns=["year"], errors="ignore")  # year อยู่ใน path (partition)
            out["batch_id"] = batch_id
            before, after = upsert_trusted(store, tkey, out, [k for k in contract["key"] if k != "year"])
            written["trusted"] = tkey
            rep["trusted_rows_before"], rep["trusted_rows_after"] = before, after
        if rep["quarantined"] > 0:
            store.write(qprefix + "rows.csv",
                        result.quarantined.to_csv(index=False).encode("utf-8"))
            written["quarantine_rows"] = qprefix + "rows.csv"

    if rep["quarantined"] > 0:
        store.write(qprefix + "report.json",
                    json.dumps(rep, ensure_ascii=False, indent=2).encode("utf-8"))
        written["report"] = qprefix + "report.json"
    rep["outputs"] = {k: f"{store.label}/{v}" for k, v in written.items()}
    return rep


# ====================================================================== orchestration

def find_batch(store, cfg: dict, source: str, batch_id: str | None, latest: bool) -> dict:
    raw_zone = zone(cfg, "raw")
    if latest:
        manifests = [k for k in store.list(f"{raw_zone}source={source}/") if k.endswith("/manifest.json")]
        if not manifests:
            raise GateError(f"ไม่มี batch ของ {source} ใน {raw_zone} (รัน ingestion/ingest.py ก่อน)")
        ms = [json.loads(store.read(k)) for k in manifests]
        return max(ms, key=lambda m: (m.get("ingested_at", ""), m["batch_id"]))
    key = f"{raw_zone}source={source}/batch_id={batch_id}/manifest.json"
    data = store.read(key)
    if data is None:
        raise GateError(f"ไม่พบ {store.label}/{key} (batch_id ผิด หรือยังไม่ได้ ingest)")
    return json.loads(data)


def gate_batch(store, cfg: dict, *, source: str, year: int, batch_id: str,
               data: bytes, file_name: str, contracts_dir: Path = CONTRACTS_DIR,
               repo_root: Path = REPO_ROOT, log_dir: Path | None = None) -> dict:
    contract = load_contract(source, contracts_dir)
    qprefix = quarantine_prefix(cfg, source, batch_id)
    if store.exists_prefix(qprefix):
        raise GateError(f"{store.label}/{qprefix} มีอยู่แล้ว: batch นี้ผ่าน gate ไปแล้ว "
                        "(ไม่เขียนทับ quarantine) ถ้าต้องการตรวจใหม่ให้ ingest เป็น batch ใหม่",
                        EXIT_ALREADY_DONE)
    master = load_master_list(contract, repo_root)
    result = run_gate(data, contract, batch_id=batch_id, year=year, master=master)
    rep = write_outputs(store, cfg, result, contract, source=source, year=year,
                        batch_id=batch_id, original_bytes=data, original_name=file_name)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"{batch_id}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                                   encoding="utf-8")
    return rep


def print_summary(rep: dict) -> None:
    print(f"batch_id    : {rep['batch_id']}  ({rep['source']}, year={rep['year']})")
    print(f"สถานะ       : {rep['status']}")
    print(f"แถว input   : {rep['total_rows']}  ->  ผ่าน {rep['passed']}  +  กัก {rep['quarantined']}")
    for k, n in rep.get("standardized", {}).items():
        print(f"[STANDARDIZE] {k}: {n}")
    for v in rep["violations"]:
        ex = v.get("example") or v.get("detail")
        print(f"[BLOCK] {v['rule']}: {v['rows']} แถว  ตัวอย่าง {json.dumps(ex, ensure_ascii=False)}")
    for w in rep["warnings"]:
        ex = w.get("example") or w.get("detail")
        print(f"[WARN]  {w['rule']}: {w['rows']} แถว  ตัวอย่าง {json.dumps(ex, ensure_ascii=False)}")
    if "trusted_rows_after" in rep:
        print(f"trusted     : {rep['trusted_rows_before']} -> {rep['trusted_rows_after']} แถว")
    for k, v in rep.get("outputs", {}).items():
        print(f"{k:<12}: {v}")


# ====================================================================== CLI

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Validation gate: raw/ -> trusted/ + quarantine/")
    p.add_argument("--source", required=True, help="lst, ndvi, buildings (ต้องมี contracts/<source>.yaml)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--batch-id", help="batch ใน raw/ ที่จะตรวจ เช่น lst_2026_20261111T1200")
    g.add_argument("--latest", action="store_true", help="ตรวจ batch ล่าสุดของ source นี้ใน raw/")
    p.add_argument("--local", action="store_true",
                   help="ไม่แตะ AWS: อ่าน --file บนเครื่อง เขียนผลลง --local-root")
    p.add_argument("--file", type=Path, help="(--local) path ไฟล์ CSV")
    p.add_argument("--year", type=int, help="(--local) ปีของข้อมูล")
    p.add_argument("--local-root", type=Path, default=DEFAULT_LOCAL_ROOT,
                   help="(--local) โฟลเดอร์ที่ใช้แทน bucket (ค่าเริ่มต้น .local_lake/)")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="path ของ settings.yaml")
    p.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help="ที่เก็บ log ของแต่ละ batch")
    args = p.parse_args(argv)
    if args.local:
        if not (args.file and args.year):
            p.error("--local ต้องใส่ --file และ --year")
    elif not (args.batch_id or args.latest):
        p.error("ต้องใส่ --batch-id หรือ --latest (หรือใช้ --local --file ...)")
    return args


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = parse_args(argv)
    source = args.source.strip().lower()
    try:
        if args.local:
            if not args.file.is_file():
                raise GateError(f"ไม่พบไฟล์ {args.file}")
            cfg = {}
            store = LocalStore(args.local_root)
            batch_id = args.batch_id or f"{source}_{args.year}_{datetime.now(BKK):%Y%m%dT%H%M%S}_local"
            n = 2
            while not args.batch_id and store.exists_prefix(quarantine_prefix(cfg, source, batch_id)):
                batch_id = f"{batch_id.rsplit('_local', 1)[0]}_local{n}"
                n += 1
            year, data, file_name = args.year, args.file.read_bytes(), args.file.name
        else:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from ingest import load_settings, make_s3_client, IngestError
            try:
                cfg = load_settings(args.config)
            except IngestError as e:
                raise GateError(str(e)) from e
            store = S3Store(make_s3_client(cfg), cfg["s3"]["bucket"])
            m = find_batch(store, cfg, source, args.batch_id, args.latest)
            batch_id, year = m["batch_id"], int(m["year"])
            f = m["files"][0]
            data = store.read(f["raw_key"])
            if data is None:
                raise GateError(f"ไม่พบไฟล์ใน raw: {f['raw_key']}")
            file_name = f["file_name"]
        rep = gate_batch(store, cfg, source=source, year=year, batch_id=batch_id,
                         data=data, file_name=file_name, log_dir=args.log_dir)
        print_summary(rep)
        return EXIT_BATCH_BLOCKED if rep["status"] == "BATCH_BLOCKED" else EXIT_OK
    except GateError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return e.code
    except Exception as e:
        if not type(e).__module__.startswith(("botocore", "boto3")):
            raise  # bug ในสคริปต์: ให้เห็น traceback เต็ม
        name, text = type(e).__name__, str(e)
        if any(k in text for k in ("ExpiredToken", "InvalidClientTokenId", "UnrecognizedClient")) \
                or name in ("NoCredentialsError", "PartialCredentialsError"):
            print("[ERROR] AWS credentials หมดอายุหรือไม่มี: เปิด Learner Lab -> AWS Details -> "
                  "คัดลอก AWS CLI ใหม่ไปวางใน ~/.aws/credentials แล้วรันอีกครั้ง", file=sys.stderr)
        else:
            print(f"[ERROR] {name}: {text}", file=sys.stderr)
        return EXIT_AWS


if __name__ == "__main__":
    sys.exit(main())
