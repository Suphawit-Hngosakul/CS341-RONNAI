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

---

# Validation gate: raw → trusted / quarantine (Issue #13)

`validate_gate.py` อ่าน batch จาก `raw/` แล้วตรวจทุกแถวตาม `contracts/<source>.yaml`

```
raw/source=lst/batch_id=<id>/lst_2026.csv
        │  STANDARDIZE → BLOCK → WARN
        ├── ผ่าน ──► trusted/lst/year=2026/lst.parquet          (upsert ตาม key)
        └── ไม่ผ่าน ► quarantine/lst/<batch_id>/rows.csv + report.json   (ไม่ลบ ไม่เขียนทับ)
```

## วิธีรัน (PowerShell ที่โฟลเดอร์ repo)
```powershell
# ตรวจ batch ล่าสุดของแต่ละแหล่งใน raw/ (ต้อง ingest ก่อน)
python ingestion\validate_gate.py --source lst       --latest
python ingestion\validate_gate.py --source ndvi      --latest
python ingestion\validate_gate.py --source buildings --latest

# หรือระบุ batch เอง
python ingestion\validate_gate.py --source lst --batch-id lst_2026_20261111T1200

# ลองบนเครื่องโดยไม่แตะ AWS (เขียนผลลง .local_lake\ ด้วย layout เดียวกับ S3)
python ingestion\validate_gate.py --local --file data\gee_2026\lst_2026.csv --source lst --year 2026
```

## ระดับกฎ (มาจาก contract)
| ระดับ | ทำอะไร | ตัวอย่าง |
|---|---|---|
| **STANDARDIZE** | แก้ให้อัตโนมัติก่อนตรวจ แล้วนับจำนวนไว้ใน report | ตัด BOM, ตัดช่องว่าง, ชื่อคอลัมน์เป็นตัวเล็ก/ช่องว่างเป็น `_`, `building_count` `751.0` → `751` |
| **BLOCK (แถว)** | แถวนั้นไป quarantine | `lst_mean` นอก 15–70, `community_id` ไม่อยู่ใน `gee/communities.csv`, `buffer_m` ไม่ใช่ 300/500, ค่าว่างในคอลัมน์ที่ห้ามว่าง, ชนิดข้อมูลผิด, `year` ไม่ตรงปีของ batch, key ซ้ำ (กักทุกแถวที่ชนกัน) |
| **BLOCK (ทั้ง batch)** | ทั้งไฟล์ไป quarantine (เก็บไฟล์ต้นฉบับ) ไม่แตะ trusted | คอลัมน์ที่ต้องมีหายไป, ไฟล์ว่าง, อ่าน CSV ไม่ได้ |
| **WARN** | ผ่านเข้า trusted แต่จดใน log/report | `valid_pixel_ratio < 0.3`, `building_count = 0` หรือว่าง, มีคอลัมน์เกิน (ไม่เอาเข้า trusted) |

แถวที่ผิดหลายกฎนับเป็น 1 แถวใน `quarantined` แต่ไปโผล่ในทุกกฎที่ผิดใน `violations`
สคริปต์ตรวจว่า **ผ่าน + กัก = จำนวนแถว input** ทุกครั้ง

## ผลลัพธ์
- **trusted** `trusted/<source>/year=<year>/<source>.parquet` ไฟล์เดียวต่อแหล่งต่อปี
  - คอลัมน์ตาม contract **ยกเว้น `year`** (อยู่ใน path เป็น partition แบบ Hive) + `batch_id` (มาจาก batch ไหน)
  - ชนิด: string → `string`, int → `bigint` (int64), float → `double` (ใช้ตอนเขียน `CREATE EXTERNAL TABLE` ลงทะเบียน trusted ใน Glue)
  - **upsert ตาม key**: แถวที่ key ตรงกับ batch ใหม่ถูกแทน แถวอื่นคงไว้ ⇒ รันซ้ำไม่เกิดแถวซ้ำ และถ้า batch ใหม่มีแถวผิด ค่าเดิมที่ถูกใน trusted ยังอยู่
- **quarantine** `quarantine/<source>/<batch_id>/` (สร้างเฉพาะเมื่อมีแถวถูกกัก)
  - `rows.csv` แถวที่ไม่ผ่าน (ค่าดิบ) + `_row_number` (บรรทัดในไฟล์) + `_violations`
  - `report.json` ผิดกฎข้อไหน กี่แถว ตัวอย่างแถว
  - ถ้ากักทั้ง batch จะเก็บไฟล์ต้นฉบับทั้งไฟล์แทน `rows.csv`
  - **ไม่ลบ ไม่เขียนทับ**: ถ้าโฟลเดอร์ของ batch นี้มีอยู่แล้ว สคริปต์หยุดทันที (exit 2)
- **log** `logs/gate/<batch_id>.json` (บนเครื่อง) report ของทุก batch รวมที่ผ่านหมดและ WARN

ตัวอย่าง `report.json`
```json
{"batch_id": "lst_2026_20261111T1200", "source": "lst", "year": 2026, "status": "PARTIAL",
 "total_rows": 74, "passed": 71, "quarantined": 3,
 "violations": [{"rule": "lst_mean between 15 and 70", "level": "BLOCK", "rows": 1,
                 "example": {"_row_number": 15, "community_id": "C0014", "buffer_m": 300, "year": 2026, "lst_mean": 120.4}}, ...],
 "warnings": [], "standardized": {"trim_whitespace": 1}}
```

exit code: `0` ผ่าน (รวมกรณีกักบางแถว), `1` input ผิด, `2` batch นี้อยู่ใน quarantine แล้ว, `3` ปัญหา AWS, `4` กักทั้ง batch

## Demo: ไฟล์ที่ตั้งใจให้ผิด
`tests/fixtures/lst_2026_bad_demo.csv` = ข้อมูล LST จริง 72 แถว + แก้/เพิ่มให้ผิด
| บรรทัด | แก้อะไร | ผล |
|---|---|---|
| 15 | C0014/300 `lst_mean = 120.4` | BLOCK `lst_mean between 15 and 70` |
| 42 | `"  C0005 "` มีช่องว่าง | STANDARDIZE ตัดช่องว่าง → ผ่าน |
| 74 | เพิ่ม `C9999` (ไม่มีชุมชนนี้) | BLOCK `community_id in master list` |
| 75 | เพิ่ม C0020 `buffer_m = 400` | BLOCK `buffer_m in [300, 500]` |

ผลที่ต้องได้: input 74 → ผ่าน 71 + กัก 3
```powershell
python ingestion\ingest.py --file tests\fixtures\lst_2026_bad_demo.csv --source lst --year 2026
python ingestion\validate_gate.py --source lst --latest
aws s3 cp s3://<bucket>/quarantine/lst/<batch_id>/report.json -
```
เพราะ trusted เป็น upsert ค่า C0014/300 ที่ถูกใน trusted ยังอยู่ (ไม่ถูกแทนด้วย 120.4) demo จึงไม่ทำให้ข้อมูลจริงเสีย
แต่ batch demo จะอยู่ใน quarantine ตลอด (ไม่ลบ) ซึ่งเป็นพฤติกรรมที่ต้องการ

`tests/fixtures/lst_2026_missing_column.csv` (ไม่มีคอลัมน์ `lst_p90`) ใช้โชว์การกักทั้ง batch

## Unit test
```powershell
python -m pytest tests -q
```
`tests/test_validate_gate.py` ครอบ: STANDARDIZE, BLOCK ทุกกฎ, WARN, กักทั้ง batch, ห้ามเขียนทับ quarantine, upsert trusted, ข้อมูลจริง 3 แหล่ง (ข้ามถ้าไม่มี `data/gee_2026/`), S3 จำลองด้วย moto
