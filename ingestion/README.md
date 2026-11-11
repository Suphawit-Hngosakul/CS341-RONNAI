# ingestion/ — นำเข้าข้อมูล incoming → raw (Issue #11)

`ingest.py` เอาไฟล์ (CSV จาก GEE, Excel แบบสอบถาม) ขึ้น S3 แล้วเก็บสำเนาต้นฉบับไว้ใน `raw/` พร้อม `manifest.json`

```
ไฟล์บนเครื่อง ──upload──► incoming/source=lst/batch_id=lst_2026_20261111T1200/lst_2026.csv
                           │ copy
                           ▼
                    raw/source=lst/batch_id=lst_2026_20261111T1200/lst_2026.csv
                    raw/source=lst/batch_id=lst_2026_20261111T1200/manifest.json
```

## ก่อนรัน
1. `pip install -r requirements.txt`
2. มี `config/settings.yaml` แล้ว (สร้างจาก `infra\ronnai_aws.ps1 -Action setup`) สคริปต์อ่าน `s3.bucket`, `aws.region`, `aws.profile` จากไฟล์นี้
3. ใส่ credentials ของ Learner Lab ใหม่ทุกรอบ (ดู `infra/README.md`) ทดสอบด้วย `aws sts get-caller-identity`

## วิธีรัน (PowerShell ที่โฟลเดอร์ repo)
```powershell
# ดูก่อนว่าจะอัปโหลดไปที่ไหน (ไม่แตะ AWS)
python ingestion\ingest.py --file data\gee_2026\lst_2026.csv --source lst --year 2026 --dry-run

# นำเข้าทั้ง 3 แหล่งของ GEE
python ingestion\ingest.py --file data\gee_2026\lst_2026.csv       --source lst       --year 2026
python ingestion\ingest.py --file data\gee_2026\ndvi_2026.csv      --source ndvi      --year 2026
python ingestion\ingest.py --file data\gee_2026\buildings_2026.csv --source buildings --year 2026

# ดู batch ทั้งหมดใน raw/
python ingestion\ingest.py --list
python ingestion\ingest.py --list --source lst
```

| อาร์กิวเมนต์ | ความหมาย |
|---|---|
| `--file` | path ไฟล์ |
| `--source` | `lst`, `ndvi`, `buildings`, `survey` (ชื่ออื่นได้แต่จะเตือน) |
| `--year` | ปีของข้อมูล |
| `--batch-id` | กำหนดเอง (ปกติไม่ต้องใส่) ต้องเป็นรูปแบบ `<source>_<year>_YYYYMMDDTHHMM` |
| `--dry-run` | แสดง path ที่จะอัปโหลด ไม่อัปโหลดจริง |
| `--list` | แสดง batch ใน `raw/` จาก manifest |
| `--config` | path ของ settings.yaml (ค่าเริ่มต้น `config/settings.yaml`) |

## กติกา
- **batch_id** = แหล่ง + ปี + เวลาที่รัน (เวลาไทย ละเอียดถึงนาที) เช่น `lst_2026_20261111T1200`
- **ห้ามเขียนทับ**: ถ้า `raw/source=<src>/batch_id=<id>/` มีอยู่แล้ว สคริปต์หยุดและออกด้วย exit code `2` ไม่แตะไฟล์เดิม
- ถ้านำเข้าไฟล์เดิม (checksum เดียวกัน) ซ้ำใน batch ใหม่ จะขึ้น `[WARN]` แต่ยังนำเข้าให้ (ต้นฉบับเก่ายังอยู่ครบ)
- ไฟล์ใน `incoming/` ไม่ถูกลบอัตโนมัติ ลบเองได้หลังตรวจว่า `raw/` ครบแล้ว
- exit code: `0` สำเร็จ, `1` input ผิด, `2` batch ซ้ำ, `3` ปัญหา AWS (เช่น credentials หมดอายุ)

### manifest.json
```json
{
  "batch_id": "lst_2026_20261111T1200",
  "source": "lst",
  "year": 2026,
  "bucket": "cs341-ronnai-<account_id>",
  "files": [{
    "file_name": "lst_2026.csv",
    "size_bytes": 4266,
    "sha256": "c8bf90d3...",
    "incoming_key": "incoming/source=lst/batch_id=lst_2026_20261111T1200/lst_2026.csv",
    "raw_key": "raw/source=lst/batch_id=lst_2026_20261111T1200/lst_2026.csv"
  }],
  "file_count": 1,
  "ingested_at": "2026-11-11T05:00:00+00:00",
  "ingested_by": "view",
  "script_version": "1.0.0"
}
```
`ingested_at` เก็บเป็นเวลา UTC

## ทดสอบ "อัปโหลดซ้ำแล้วไม่เขียนทับ" (เกณฑ์เสร็จของ issue)
```powershell
python ingestion\ingest.py --file data\gee_2026\lst_2026.csv --source lst --year 2026 --batch-id lst_2026_20261111T1200
python ingestion\ingest.py --file data\gee_2026\lst_2026.csv --source lst --year 2026 --batch-id lst_2026_20261111T1200
# ครั้งที่ 2 ต้องขึ้น [ERROR] batch_id ... มีอยู่แล้ว ... ไม่เขียนทับ
echo $LASTEXITCODE   # 2
```

## Unit test (ไม่ต้องใช้ AWS จริง ใช้ S3 จำลองด้วย moto)
```powershell
pip install -r requirements-dev.txt
python -m pytest tests -q
```
