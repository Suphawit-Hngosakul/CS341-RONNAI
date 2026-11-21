"""นำเข้าไฟล์ข้อมูลขึ้น S3: incoming/ -> raw/ (Issue #11)

ขั้นตอน
  1. รับ input: path ไฟล์, ชื่อแหล่ง (--source เช่น lst), ปี (--year)
  2. สร้าง batch_id = <source>_<year>_<YYYYMMDDTHHMM>  เช่น lst_2026_20261111T1200
  3. อัปโหลดไป incoming/source=<src>/batch_id=<id>/<file>
     แล้วคัดลอกไป raw/source=<src>/batch_id=<id>/<file>
  4. เขียน manifest.json คู่กันใน raw/: ชื่อไฟล์, ขนาด, checksum (sha256), ingested_at
  5. ถ้า batch_id นี้มีอยู่แล้วใน raw/ จะหยุดทันที ไม่เขียนทับ

ตัวอย่าง
  python ingestion/ingest.py --file data/gee_2026/lst_2026.csv --source lst --year 2026
  python ingestion/ingest.py --file data/gee_2026/lst_2026.csv --source lst --year 2026 --dry-run
  python ingestion/ingest.py --list --source lst

ค่า bucket / region อ่านจาก config/settings.yaml (สร้างโดย infra/ronnai_aws.ps1 -Action setup)
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

SCRIPT_VERSION = "1.0.0"
KNOWN_SOURCES = {"lst", "ndvi", "buildings", "survey"}
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "settings.yaml"
BKK = timezone(timedelta(hours=7))  # batch_id ใช้เวลาไทย ให้อ่านตรงกับเวลาที่ทีมรันจริง

# exit code
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_BATCH_EXISTS = 2
EXIT_AWS = 3


class IngestError(Exception):
    def __init__(self, msg: str, code: int = EXIT_USAGE):
        super().__init__(msg)
        self.code = code


# ---------- helpers (ไม่แตะ AWS ทดสอบได้ง่าย) ----------

def load_settings(path: Path) -> dict:
    if not path.exists():
        raise IngestError(
            f"ไม่พบ {path}\n"
            "  รัน infra/ronnai_aws.ps1 -Action setup ก่อน หรือคัดลอก config/settings.example.yaml -> config/settings.yaml"
        )
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    bucket = (cfg.get("s3") or {}).get("bucket", "")
    if not bucket or "<" in bucket:
        raise IngestError(f"s3.bucket ใน {path} ยังไม่ได้ตั้งค่า (ตอนนี้: {bucket!r})")
    return cfg


def prefixes(cfg: dict) -> tuple[str, str]:
    p = (cfg.get("s3") or {}).get("prefixes") or {}
    incoming = p.get("incoming", "incoming/").rstrip("/") + "/"
    raw = p.get("raw", "raw/").rstrip("/") + "/"
    return incoming, raw


def validate_source(source: str) -> str:
    source = source.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", source):
        raise IngestError(f"--source ต้องเป็นตัวพิมพ์เล็ก/ตัวเลข/_ เท่านั้น (ได้ {source!r})")
    if source not in KNOWN_SOURCES:
        print(f"[WARN] source '{source}' ไม่อยู่ในรายการที่รู้จัก {sorted(KNOWN_SOURCES)} (ยังอัปโหลดต่อ)")
    return source


def validate_year(year: int) -> int:
    if not 2000 <= year <= 2100:
        raise IngestError(f"--year ต้องอยู่ระหว่าง 2000-2100 (ได้ {year})")
    return year


def make_batch_id(source: str, year: int, now: datetime | None = None) -> str:
    now = now or datetime.now(BKK)
    return f"{source}_{year}_{now.astimezone(BKK):%Y%m%dT%H%M}"


def validate_batch_id(batch_id: str, source: str, year: int) -> str:
    if not re.fullmatch(rf"{re.escape(source)}_{year}_\d{{8}}T\d{{4}}", batch_id):
        raise IngestError(
            f"--batch-id ต้องเป็นรูปแบบ {source}_{year}_YYYYMMDDTHHMM (ได้ {batch_id!r})"
        )
    return batch_id


def file_info(path: Path) -> dict:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    return {"file_name": path.name, "size_bytes": size, "sha256": h.hexdigest()}


def batch_prefix(zone_prefix: str, source: str, batch_id: str) -> str:
    return f"{zone_prefix}source={source}/batch_id={batch_id}/"


def build_manifest(*, batch_id, source, year, bucket, info, incoming_key, raw_key,
                   ingested_at: datetime) -> dict:
    return {
        "batch_id": batch_id,
        "source": source,
        "year": year,
        "bucket": bucket,
        "files": [{**info, "incoming_key": incoming_key, "raw_key": raw_key}],
        "file_count": 1,
        "ingested_at": ingested_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "ingested_by": getpass.getuser(),
        "script_version": SCRIPT_VERSION,
    }


# ---------- AWS ----------

def make_s3_client(cfg: dict):
    import boto3

    aws = cfg.get("aws") or {}
    profile = aws.get("profile") or None
    session = boto3.Session(profile_name=profile, region_name=aws.get("region", "us-east-1"))
    return session.client("s3")


def prefix_exists(s3, bucket: str, prefix: str) -> bool:
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    return resp.get("KeyCount", 0) > 0


def find_same_checksum(s3, bucket: str, raw_prefix: str, source: str, sha256: str) -> list[str]:
    """หา batch เดิมของ source เดียวกันที่มีไฟล์ checksum ตรงกัน (เตือนเฉย ๆ ไม่บล็อก)"""
    found = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{raw_prefix}source={source}/"):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith("/manifest.json"):
                continue
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            try:
                m = json.loads(body)
            except ValueError:
                continue
            if any(f.get("sha256") == sha256 for f in m.get("files", [])):
                found.append(m.get("batch_id", obj["Key"]))
    return found


def ingest(s3, cfg: dict, *, file_path: Path, source: str, year: int,
           batch_id: str | None = None, now: datetime | None = None,
           dry_run: bool = False) -> dict:
    if not file_path.is_file():
        raise IngestError(f"ไม่พบไฟล์ {file_path}")
    source = validate_source(source)
    year = validate_year(year)
    now = now or datetime.now(BKK)
    batch_id = validate_batch_id(batch_id, source, year) if batch_id else make_batch_id(source, year, now)

    bucket = cfg["s3"]["bucket"]
    incoming_zone, raw_zone = prefixes(cfg)
    info = file_info(file_path)
    in_prefix = batch_prefix(incoming_zone, source, batch_id)
    raw_prefix = batch_prefix(raw_zone, source, batch_id)
    incoming_key = in_prefix + info["file_name"]
    raw_key = raw_prefix + info["file_name"]
    manifest_key = raw_prefix + "manifest.json"
    manifest = build_manifest(batch_id=batch_id, source=source, year=year, bucket=bucket,
                              info=info, incoming_key=incoming_key, raw_key=raw_key,
                              ingested_at=now)

    print(f"batch_id : {batch_id}")
    print(f"ไฟล์      : {file_path} ({info['size_bytes']:,} bytes, sha256 {info['sha256'][:12]}...)")
    print(f"incoming : s3://{bucket}/{incoming_key}")
    print(f"raw      : s3://{bucket}/{raw_key}")
    print(f"manifest : s3://{bucket}/{manifest_key}")

    if dry_run:
        print("[DRY-RUN] ไม่ได้อัปโหลดจริง")
        return manifest

    # 5. ห้ามเขียนทับ batch เดิม
    if prefix_exists(s3, bucket, raw_prefix):
        raise IngestError(
            f"batch_id '{batch_id}' มีอยู่แล้วใน s3://{bucket}/{raw_prefix} -> หยุด ไม่เขียนทับ\n"
            "  ถ้าต้องการนำเข้าใหม่ ให้รันใหม่โดยไม่ใส่ --batch-id (จะได้ batch_id ตามเวลาปัจจุบัน)",
            EXIT_BATCH_EXISTS,
        )

    dup = find_same_checksum(s3, bucket, raw_zone, source, info["sha256"])
    if dup:
        print(f"[WARN] ไฟล์นี้ (checksum เดียวกัน) เคยนำเข้าแล้วใน batch: {', '.join(dup)}")

    meta = {"batch_id": batch_id, "source": source, "year": str(year), "sha256": info["sha256"]}

    # 3. อัปโหลดไป incoming/ แล้วคัดลอกไป raw/
    s3.upload_file(str(file_path), bucket, incoming_key, ExtraArgs={"Metadata": meta})
    s3.copy_object(Bucket=bucket, Key=raw_key,
                   CopySource={"Bucket": bucket, "Key": incoming_key},
                   MetadataDirective="COPY")

    # ตรวจว่า raw ได้ไฟล์ครบ (ขนาดตรง)
    head = s3.head_object(Bucket=bucket, Key=raw_key)
    if head["ContentLength"] != info["size_bytes"]:
        raise IngestError(
            f"ขนาดไฟล์ใน raw ({head['ContentLength']}) ไม่ตรงกับต้นฉบับ ({info['size_bytes']})",
            EXIT_AWS,
        )

    # 4. manifest.json คู่กันใน raw/
    s3.put_object(Bucket=bucket, Key=manifest_key,
                  Body=json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                  ContentType="application/json")

    print(f"[OK] นำเข้าเรียบร้อย: {batch_id}")
    return manifest


def list_batches(s3, cfg: dict, source: str | None) -> list[dict]:
    bucket = cfg["s3"]["bucket"]
    _, raw_zone = prefixes(cfg)
    prefix = f"{raw_zone}source={source}/" if source else raw_zone
    out = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("/manifest.json"):
                m = json.loads(s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read())
                out.append(m)
    out.sort(key=lambda m: (m.get("source", ""), m.get("ingested_at", "")))
    return out


# ---------- CLI ----------

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="นำเข้าไฟล์ขึ้น S3: incoming/ -> raw/ (Issue #11)")
    p.add_argument("--file", type=Path, help="path ไฟล์ที่จะนำเข้า เช่น data/gee_2026/lst_2026.csv")
    p.add_argument("--source", help="ชื่อแหล่งข้อมูล: lst, ndvi, buildings, survey")
    p.add_argument("--year", type=int, help="ปีของข้อมูล เช่น 2026")
    p.add_argument("--batch-id", help="กำหนด batch_id เอง (ปกติไม่ต้องใส่; ใช้ทดสอบการกันเขียนทับ)")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="path ของ settings.yaml")
    p.add_argument("--dry-run", action="store_true", help="แสดงว่าจะอัปโหลดที่ไหน โดยไม่อัปโหลดจริง")
    p.add_argument("--list", action="store_true", help="แสดง batch ที่อยู่ใน raw/ (กรองด้วย --source ได้)")
    args = p.parse_args(argv)
    if not args.list and not (args.file and args.source and args.year):
        p.error("ต้องใส่ --file, --source และ --year (หรือใช้ --list)")
    return args


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # ให้ภาษาไทยแสดงได้ใน PowerShell
        except (AttributeError, ValueError):
            pass
    args = parse_args(argv)
    try:
        cfg = load_settings(args.config)
        s3 = None if (args.dry_run and not args.list) else make_s3_client(cfg)
        if args.list:
            batches = list_batches(s3, cfg, args.source.lower() if args.source else None)
            if not batches:
                print("ยังไม่มี batch ใน raw/")
            for m in batches:
                f = m["files"][0]
                print(f"{m['batch_id']:<32} {f['file_name']:<24} {f['size_bytes']:>10,} B  {m['ingested_at']}")
            return EXIT_OK
        ingest(s3, cfg, file_path=args.file, source=args.source, year=args.year,
               batch_id=args.batch_id, dry_run=args.dry_run)
        return EXIT_OK
    except IngestError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return e.code
    except Exception as e:  # botocore errors
        name = type(e).__name__
        text = str(e)
        if any(k in text for k in ("ExpiredToken", "InvalidClientTokenId", "UnrecognizedClient")) \
                or name in ("NoCredentialsError", "PartialCredentialsError"):
            print("[ERROR] AWS credentials หมดอายุหรือไม่มี: เปิด Learner Lab -> AWS Details -> "
                  "คัดลอก AWS CLI ใหม่ไปวางใน ~/.aws/credentials แล้วรันอีกครั้ง", file=sys.stderr)
        else:
            print(f"[ERROR] {name}: {text}", file=sys.stderr)
        return EXIT_AWS


if __name__ == "__main__":
    sys.exit(main())
