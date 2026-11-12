# CS341-RONNAI

โครงการวิเคราะห์ความร้อนในชุมชน (CS341 Big Data Engineering)

Pipeline รันบน AWS ด้วย **Step Functions + Lambda** วางไฟล์ CSV เข้า `incoming/` แล้ว pipeline จะทำงานเองจนได้ตาราง curated ที่ผ่านการตรวจ (Issue #18)

## Quick start (คำสั่งเดียว ตั้งแต่บัญชีว่าง)

ต้องมี: AWS CLI v2, Python 3.10+ (มี pip), Git Bash (Windows) หรือ bash (macOS/Linux/Learner Lab terminal)
และไฟล์ข้อมูลใน `data/gee_2026/` (`lst_2026.csv`, `ndvi_2026.csv`, `buildings_2026.csv`)

1. เปิด Learner Lab → **Start Lab** → **AWS Details → AWS CLI: Show** คัดลอกทั้งก้อนไปวางทับ `~/.aws/credentials`
2. รันใน **Git Bash** ที่โฟลเดอร์ repo (PowerShell รันไฟล์ `.sh` ไม่ได้)
   ```bash
   aws sts get-caller-identity          # ต้องเห็นเลข account
   ./infra/ronnai_aws.sh all            # setup -> deploy -> view -> run
   ```
3. ท้ายผลลัพธ์จะสรุปแต่ละ execution และมีลิงก์ **ดูกราฟ** ใน Step Functions console

รันซ้ำได้: ของที่มีแล้วจะข้าม ไฟล์เดิมจะจบที่ `AlreadyProcessed` (ข้อมูลใน curated ไม่เปลี่ยน)
รายละเอียดทุกคำสั่ง ตัวแปรที่ปรับได้ และวิธีแก้ปัญหา: [`infra/README.md`](infra/README.md)

## Pipeline ทำงานยังไง

```
อัปไฟล์ incoming/<source>_<year>.csv
  -> S3 event "Object Created" -> EventBridge (กฎ ronnai-incoming-csv)
  -> Lambda ronnai-start-pipeline  (StartExecution)
  -> Step Functions ronnai-pipeline (1 ไฟล์ = 1 execution)
       CheckBatch       ไฟล์ซ้ำที่เคยสำเร็จ -> quarantine/duplicate/ และจบที่ AlreadyProcessed
       IngestBatch      incoming/ -> raw/source=<src>/batch_id=<id>/ + manifest.json
       ValidateBatch    ตรวจตาม contracts/<source>.yaml  แถวผ่าน -> trusted/  แถวไม่ผ่าน -> quarantine/
       TransformCurated เมื่อ trusted ครบ 3 แหล่ง: Athena MERGE -> curated (Iceberg)  ยังไม่ครบ -> WaitingForOtherSources
       VerifyCurated    ตรวจจำนวนแถว / key ซ้ำ / coverage แล้วเขียน _SUCCESS.json
```
ทุกขั้นมี Retry และ Catch ถ้าพังจะจบที่ `PipelineFailed` พร้อม error ของขั้นนั้น (ดูใน Graph view ของ execution)

| ส่วน | ไฟล์ |
|---|---|
| state machine | [`infra/state_machine.asl.json`](infra/state_machine.asl.json) |
| Lambda 6 ตัว | `function/<ชื่อ>_function/lambda_function.py` (start, batch=check, ingest, validate, transform, verify) |
| logic ที่ Lambda ใช้ร่วมกับ CLI | `ingestion/ingest.py`, `ingestion/validate_gate.py`, `ingestion/verify_curated.py` |
| data contract | `contracts/*.yaml` |
| SQL | `sql/glue_trusted.sql`, `sql/curated_merge.sql`, `sql/views/heat_vs_green.sql` (view ตอบ Q1) |

## รันทีละขั้นบนเครื่อง (ไม่ผ่าน Lambda ใช้ตอนดีบัก)

ต้องมี venv: `python -m venv .venv` แล้ว `pip install -r requirements-dev.txt` และ `config/settings.yaml` (คำสั่ง `setup` สร้างให้)

```powershell
python ingestion\ingest.py --file data\gee_2026\lst_2026.csv --source lst --year 2026   # incoming -> raw (Issue #11)
python ingestion\validate_gate.py --source lst --latest                                  # raw -> trusted / quarantine (Issue #13)
```
ทำซ้ำกับ `ndvi`, `buildings` แล้วรัน [`sql/glue_trusted.sql`](sql/glue_trusted.sql) และ [`sql/curated_merge.sql`](sql/curated_merge.sql) ใน Athena workgroup `ronnai-wg` รายละเอียดใน [`ingestion/README.md`](ingestion/README.md) และ [`sql/README.md`](sql/README.md)

> ถ้า Windows Smart App Control บล็อก pandas ให้สร้าง venv ด้วย Python 3.11 (`py -3.11 -m venv .venv`) แล้วรันด้วย `.\.venv\Scripts\python.exe` แทน `python`
> (คำสั่ง `infra/ronnai_aws.sh` ไม่ติดปัญหานี้ เพราะแค่ดาวน์โหลด wheel ของ Linux ไปแพ็กให้ Lambda ไม่ได้รัน pandas บน Windows)

ทดสอบ: `python -m pytest -q`

## โครงสร้างข้อมูล

| Zone (S3 prefix) | เก็บอะไร |
|---|---|
| `incoming/` | ไฟล์ที่เพิ่งอัปโหลด (ชื่อต้องเป็น `<source>_<year>.csv`) วางแล้ว pipeline เริ่มเอง |
| `raw/` | ข้อมูลดิบ แยกตาม batch **ห้ามแก้ไขทับ** + `manifest.json` และ `_SUCCESS.json` เมื่อ batch สำเร็จ |
| `trusted/` | ผ่าน validation แล้ว (Parquet) |
| `curated/` | ตารางพร้อมใช้ (Apache Iceberg) + `_SUCCESS.json` |
| `quarantine/` | แถว/batch ที่ไม่ผ่าน พร้อม report และไฟล์ซ้ำใน `quarantine/duplicate/` (ไม่ลบ) |
| `athena-results/` | ผลลัพธ์ query ของ Athena (ลบอัตโนมัติหลัง 7 วัน) |
| `lambda-packages/` | ที่พัก zip ของ Lambda เฉพาะกรณีไม่ใช้ layer และ zip ใหญ่เกิน 45 MB (ปกติว่าง) |

## โครงสร้าง folder

```
infra/          # ronnai_aws.sh (คำสั่งหลัก) + lib/ + tools/ + state_machine.asl.json
function/       # Lambda handler 6 ตัว
ingestion/      # ingest / validation gate / verify (ใช้ได้ทั้ง CLI และ Lambda)
contracts/      # data contract (YAML) ต่อแหล่ง
sql/            # Glue trusted, curated MERGE, views ตอบ Q1
gee/            # สคริปต์ GEE + communities.csv (master list)
tests/          # pytest (ไม่ต้องใช้ AWS จริง)
config/         # settings.example.yaml
data/           # ไฟล์ข้อมูล (ไม่อยู่ใน git)
analysis/  dashboard/  docs/
```

## วิธีทำงานร่วมกัน

ทำงานใน branch แยก (เช่น `feature/18-step-functions`) แล้วเปิด Pull Request เข้า `development`
