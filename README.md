# CS341-RONNAI

โครงการวิเคราะห์ความร้อนในชุมชน (CS341 Big Data Engineering)

> สถานะ: ตั้งโครง repo (Issue #1) — เนื้อหาในแต่ละหัวข้อจะเติมจริงตอน Issue #15

## Setup

1. ติดตั้ง Python 3.10+ แล้วสร้าง virtual environment
   ```bash
   python -m venv .venv
   # Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. คัดลอกค่าตั้งต้น: `config/settings.example.yaml` → `config/settings.yaml` แล้วแก้ bucket / region / workgroup
3. ตั้งค่า AWS credentials ผ่าน AWS CLI profile (ห้ามใส่ key ใน repo)
4. สร้าง S3 zones, Glue databases และ Athena workgroup บน Learner Lab ด้วยคำสั่งเดียว (Issue #2) — รายละเอียดใน [`infra/README.md`](infra/README.md)
   ```powershell
   powershell -ExecutionPolicy Bypass -File infra\ronnai_aws.ps1 -Action setup
   powershell -ExecutionPolicy Bypass -File infra\ronnai_aws.ps1 -Action verify
   ```
   (macOS/Linux/Terminal ของ Learner Lab: `bash infra/ronnai_aws.sh setup` แล้ว `bash infra/ronnai_aws.sh verify`)

## Run pipeline

TODO (เติมใน #15): คำสั่งรัน ingest → gate → MERGE → verify

**ขั้นที่ 1: นำเข้า incoming → raw** (Issue #11) รายละเอียดใน [`ingestion/README.md`](ingestion/README.md)
```powershell
python ingestion\ingest.py --file data\gee_2026\lst_2026.csv       --source lst       --year 2026
python ingestion\ingest.py --file data\gee_2026\ndvi_2026.csv      --source ndvi      --year 2026
python ingestion\ingest.py --file data\gee_2026\buildings_2026.csv --source buildings --year 2026
python ingestion\ingest.py --list
```

**ขั้นที่ 2: Validation gate raw → trusted / quarantine** (Issue #13) รายละเอียดใน [`ingestion/README.md`](ingestion/README.md#validation-gate-raw--trusted--quarantine-issue-13)
```powershell
python ingestion\validate_gate.py --source lst       --latest
python ingestion\validate_gate.py --source ndvi      --latest
python ingestion\validate_gate.py --source buildings --latest
```

**ขั้นที่ 3: ลงทะเบียนตาราง trusted ใน Glue (Issue #14)** รันใน Athena workgroup `ronnai-wg` ทีละบล็อกตาม [`sql/glue_trusted.sql`](sql/glue_trusted.sql) รายละเอียดใน [`sql/README.md`](sql/README.md)

**ขั้นที่ 4: รวมเป็นตาราง curated (Iceberg) + MERGE (Issue #15)** รันใน Athena ทีละบล็อกตาม [`sql/curated_merge.sql`](sql/curated_merge.sql)

> หมายเหตุ: ถ้า Windows Smart App Control บล็อก pandas ให้สร้าง venv ด้วย Python 3.11 (`py -3.11 -m venv .venv`) แล้วรันสคริปต์ด้วย `.\.venv\Scripts\python.exe` แทน `python` เช่น `.\.venv\Scripts\python.exe ingestion\validate_gate.py --source lst --latest`

## โครงสร้างข้อมูล

| Zone (S3 prefix) | เก็บอะไร |
|---|---|
| `incoming/` | ไฟล์ที่เพิ่งอัปโหลด รอตรวจ |
| `raw/` | ข้อมูลดิบ **ห้ามแก้ไขทับ** |
| `trusted/` | ผ่าน validation แล้ว (Parquet) |
| `curated/` | ตารางพร้อมใช้ (Apache Iceberg) |
| `quarantine/` | batch ที่ไม่ผ่าน พร้อม report (ไม่ลบ) |
| `athena-results/` | ผลลัพธ์ query ของ Athena |

## โครงสร้าง folder

```
gee/            # สคริปต์ GEE
ingestion/      # upload + validation gate
contracts/      # data contract (YAML) ต่อแหล่ง
transform/      # SQL/Python สำหรับ Iceberg MERGE, verify
sql/views/      # Athena views
analysis/       # notebook
dashboard/
config/         # settings.example.yaml
docs/           # architecture.png
```

## วิธีทำงานร่วมกัน

ทำงานใน branch แยก (เช่น `feature/7-ingestion`) แล้วเปิด Pull Request เข้า `main`
