# infra/ — สร้างและรัน pipeline บน AWS (Issue #2, #18)

คำสั่งหลักคือ `infra/ronnai_aws.sh` ใช้กับ **AWS Academy Learner Lab** รันซ้ำได้ทุกคำสั่ง (ของที่มีแล้วจะข้าม/อัปเดต ไม่สร้างซ้ำ)

```bash
./infra/ronnai_aws.sh all       # คำสั่งเดียวตั้งแต่บัญชีว่าง: setup -> deploy -> view -> run
```

| คำสั่ง | ทำอะไร |
|---|---|
| `setup` | S3 bucket + 6 zones, Glue DB 2 อัน, Athena workgroup, `config/settings.yaml` |
| `verify` | ตรวจว่าของจาก `setup` ครบ + รัน `SELECT 1` ใน Athena |
| `deploy` | ตาราง trusted ใน Glue, Lambda 6 ตัว, state machine `ronnai-pipeline`, กฎ EventBridge (ไฟล์ใหม่ใน `incoming/` -> เริ่ม execution) |
| `view` | ตาราง curated (ถ้ายังไม่มี) + view ตอบ Q1 จาก `sql/views/heat_vs_green.sql` |
| `run` | อัปโหลด `data/gee_<YEAR>/` ทั้ง 3 แหล่งเข้า `incoming/` รอ execution จบ แล้วสรุปขั้นที่แต่ละ execution ผ่าน |
| `all` | `setup` -> `deploy` -> `view` -> `run` |
| `destroy` | ลบทุกอย่าง (กฎ EventBridge, state machine, Lambda, workgroup, Glue, bucket) ต้องพิมพ์ชื่อ bucket ยืนยัน **ข้อมูลหายถาวร** (ชื่อเดิม `teardown` ใช้ได้) |

`ronnai_aws.ps1` (PowerShell) ยังมีแค่ `setup` / `verify` / `teardown` ส่วน `deploy` / `run` / `all` ใช้ได้จาก `.sh` เท่านั้น

## ใช้ที่ไหนได้บ้าง

| ที่ | หมายเหตุ |
|---|---|
| **Git Bash บน Windows** | ใช้ได้ สคริปต์แปลง path ให้ `aws.exe` เอง **ห้ามรันจาก PowerShell** (`./infra/ronnai_aws.sh` ใน PowerShell จะเปิดไฟล์ด้วยโปรแกรมอื่นแล้วจบเงียบ ๆ) ถ้าอยู่ใน PowerShell ให้ใช้ `& "C:\Program Files\Git\bin\bash.exe" infra/ronnai_aws.sh all` |
| macOS / Linux | ใช้ได้ |
| Terminal ของ Learner Lab / CloudShell | ใช้ได้ ไม่ต้องคัดลอก credentials (ต้อง clone repo และวางไฟล์ `data/gee_2026/` เอง) |

ต้องมี: AWS CLI v2 (`aws --version`), Python 3.8+ ที่มี pip (ใช้แพ็ก Lambda และแยกไฟล์ SQL)

### ใส่ credentials (ทุกครั้งที่เปิด lab ใหม่ หมดอายุประมาณ 4 ชม.)
1. Learner Lab → **Start Lab** รอจนวงกลมเป็นสีเขียว
2. **AWS Details** → *AWS CLI* → **Show** คัดลอกทั้งก้อน (`[default]`, `aws_access_key_id`, `aws_secret_access_key`, `aws_session_token`)
3. วางทับทั้งไฟล์ `~/.aws/credentials` (Windows: `%USERPROFILE%\.aws\credentials`)
4. ทดสอบ: `aws sts get-caller-identity` ต้องเห็นเลข account

## สิ่งที่สร้างบน AWS

| บริการ | ชื่อ (ค่าเริ่มต้น) | สร้างโดย | รายละเอียด |
|---|---|---|---|
| S3 bucket | `cs341-ronnai-<account_id>` | setup | ปิด public access, zones `incoming/ raw/ trusted/ curated/ quarantine/ athena-results/` |
| S3 lifecycle | `athena-results/` | setup | ลบผล query ที่เก่ากว่า 7 วัน |
| Glue database | `ronnai_trusted`, `ronnai_curated` | setup | |
| Athena workgroup | `ronnai-wg` | setup | จำกัดสแกน 1 GB ต่อ query, engine v3 (Iceberg) |
| Glue table | `ronnai_trusted.{lst,ndvi,buildings}` | deploy | จาก `sql/glue_trusted.sql` |
| Lambda | `ronnai-start-pipeline` | deploy | รับ event จาก EventBridge แล้วเรียก `StartExecution` |
| Lambda | `ronnai-{check,ingest,validate,transform,verify}-batch` | deploy | 5 ขั้นของ state machine (validate memory 2 GB) |
| Lambda layer | `AWSSDKPandas-Python312` (ของ AWS ไม่ได้สร้างใหม่) | deploy | แนบให้ `validate-batch` ได้ pandas + pyarrow โดยไม่ต้องแพ็กเอง |
| Step Functions | `ronnai-pipeline` | deploy | นิยามใน `state_machine.asl.json` (STANDARD) |
| EventBridge rule | `ronnai-incoming-csv` | deploy | S3 Object Created ใต้ `incoming/` -> `ronnai-start-pipeline` |
| Glue table / view | `ronnai_curated.community_heat` + view Q1 | view | จาก `sql/curated_merge.sql` บล็อก [1] และ `sql/views/heat_vs_green.sql` |

ไม่มีการสร้าง IAM role ทุกอย่างใช้ `LabRole` ของ lab

**ทำไมใช้ Lambda layer:** pandas + pyarrow ใหญ่ ~210 MB ถ้าแพ็กเอง zip ของ validate จะ ~64 MB เกินลิมิต 50 MB ที่อัปเข้า Lambda ตรงได้ ต้องพักไว้ใน S3 (`lambda-packages/`) และ deploy ช้าเพราะต้องดาวน์โหลด pandas ทุกครั้ง
ใช้ layer สำเร็จรูปของ AWS แทน ทำให้ zip ทุกตัวเหลือ < 1 MB อัปตรงได้ deploy เร็วขึ้น และเปิดดูโค้ดใน Lambda console ได้
ถ้าใช้ layer ไม่ได้ (เช่นเลขเวอร์ชันถูกถอนออก) สคริปต์จะเตือนแล้วกลับไปแพ็ก pandas เองผ่าน S3 ให้อัตโนมัติ
เปลี่ยนเวอร์ชันได้ด้วย `PANDAS_LAYER_ARN=...` (ดูเลขล่าสุดที่ https://aws-sdk-pandas.readthedocs.io/en/stable/layers.html) หรือ `PANDAS_LAYER_ARN=none` เพื่อแพ็กเอง

**ทำไมต้องมี `ronnai-start-pipeline` คั่น:** กฎ EventBridge ที่ยิงเข้า Step Functions ตรง ๆ ต้องให้ EventBridge assume role ซึ่ง `LabRole` ไม่อนุญาต (execution จะไม่เริ่มเลยโดยไม่มี error) ส่วน EventBridge -> Lambda ใช้แค่ resource policy ของ Lambda จึงทำได้

## ผลของ `run`

แต่ละไฟล์ = 1 execution ผลที่เป็นไปได้

| จบที่ | ความหมาย |
|---|---|
| `PipelineSucceeded` | ผ่านทุกขั้น เขียน `curated/_SUCCESS.json` แล้ว |
| `WaitingForOtherSources` | ไฟล์นี้ลง trusted แล้ว แต่แหล่งอื่นของปีเดียวกันยังไม่ครบ (execution ของไฟล์ที่มาทีหลังจะทำ transform + verify) |
| `AlreadyProcessed` | ไฟล์เนื้อหาเดียวกันเคยสำเร็จแล้ว ย้ายไป `quarantine/duplicate/` (รันซ้ำจะได้แบบนี้) |
| `FileRejected` | ชื่อไฟล์ไม่ใช่ `<source>_<year>.csv` หรือไม่มี contract |
| `BatchBlocked` | ไฟล์เสียทั้งไฟล์ (คอลัมน์หาย ไฟล์ว่าง) เก็บใน `quarantine/` ไม่แตะ trusted |
| `VerifyFailed` | curated ไม่ผ่าน (จำนวนแถว / key ซ้ำ / coverage) |
| `PipelineFailed` | Lambda error หลัง retry ดู error ในกราฟของ execution |

ดูทีละขั้น: Step Functions console -> `ronnai-pipeline` -> เลือก execution -> **Graph view**
ดู log: `aws logs tail /aws/lambda/ronnai-check-batch --since 10m` (เปลี่ยนชื่อ Lambda ได้)

## ปรับค่าด้วย env var

```bash
YEAR=2026 RUN_WAIT_SEC=900 ./infra/ronnai_aws.sh run
BUCKET=cs341-ronnai-team SCAN_LIMIT_MB=512 ./infra/ronnai_aws.sh setup
```
ตัวแปรทั้งหมด (ค่าเริ่มต้น): `REGION` (us-east-1), `BUCKET` (cs341-ronnai-<account>), `WORKGROUP` (ronnai-wg), `DB_TRUSTED`, `DB_CURATED`, `SCAN_LIMIT_MB` (1024), `ROLE_ARN` (LabRole), `NAME_PREFIX` (ronnai), `SFN_NAME` (<NAME_PREFIX>-pipeline), `YEAR` (2026), `RUN_WAIT_SEC` (900), `TRIGGER_WARMUP_SEC` (60), `PANDAS_LAYER_ARN` (AWSSDKPandas-Python312:32, `none` = แพ็กเอง), `AWS_PROFILE_NAME`

## โครงไฟล์

```
infra/
├── ronnai_aws.sh            # ค่าตั้งต้น + เลือกคำสั่ง
├── lib/
│   ├── common.sh            # ข้อความ, aws_, preflight, run_athena_file, ชื่อ state machine
│   ├── setup.sh             # setup / verify
│   ├── deploy.sh            # ตาราง trusted, แพ็ก + สร้าง Lambda, state machine, trigger
│   ├── run.sh               # run
│   ├── view.sh              # view
│   └── destroy.sh           # destroy
├── tools/sql_split.py       # แยกไฟล์ SQL เป็นทีละ statement ให้ Athena
├── state_machine.asl.json   # นิยาม Step Functions (${REGION} ${ACCOUNT_ID} ${NAME_PREFIX} ถูกแทนตอน deploy)
└── ronnai_aws.ps1           # PowerShell (setup / verify / teardown)
```

## ทีม 3 คน

Learner Lab ของแต่ละคนเป็น AWS account แยกกัน สร้าง IAM user ให้เพื่อนไม่ได้ ทุกคนรัน `./infra/ronnai_aws.sh all` ใน account ตัวเองได้ (ชื่อ bucket ต่างกันตามเลข account) pipeline หลักใช้ account ของเจ้าของ storage (ศุภวิชญ์)

## เจอปัญหา

| อาการ | วิธีแก้ |
|---|---|
| รันแล้วไม่มีอะไรขึ้นเลย | รันจาก PowerShell อยู่ ให้รันใน Git Bash (ดูตาราง "ใช้ที่ไหนได้บ้าง") |
| `ExpiredToken` / `credentials ใช้ไม่ได้` | session lab หมดอายุ กด Start Lab แล้วคัดลอก credentials ใหม่ |
| `Unable to load paramfile file:///tmp/...` | สคริปต์เวอร์ชันเก่า อัปเดต `infra/` ให้ล่าสุด (แก้เรื่อง path ของ Git Bash แล้ว) |
| `run` ค้างที่ `เริ่มแล้ว 0/3` ในบัญชีใหม่ | trigger ที่เพิ่งสร้างยังไม่ส่ง event (ไฟล์ที่อัปช่วงนั้นหายไปเลย) สคริปต์รอ `TRIGGER_WARMUP_SEC` (60) ก่อนอัป และถ้า 60 วินาทียังไม่เริ่มจะอัปไฟล์นั้นซ้ำให้เอง ถ้ายังไม่ได้ ดู `aws logs tail /aws/lambda/ronnai-start-pipeline --since 10m` |
| มี execution `FileRejected` ที่ key = `incoming/` | object โฟลเดอร์ที่ `setup` สร้าง ไปเรียก trigger (สคริปต์เวอร์ชันเก่า) เวอร์ชันนี้ `start-pipeline` ข้ามไฟล์ที่ไม่ใช่ `.csv` แล้ว |
| `ERROR: pip's dependency resolver ... python-dateutil` ตอนแพ็ก Lambda | เกิดเฉพาะตอนแพ็ก pandas เอง เป็นคำเตือนจาก pip ในเครื่อง ไม่กระทบ zip ของ Lambda ข้ามได้ |
| `[WARN] ใช้ layer ... ไม่ได้` | สคริปต์แพ็ก pandas เองแทนแล้ว ใช้งานได้ปกติ ถ้าอยากใช้ layer ให้ตั้ง `PANDAS_LAYER_ARN` เป็นเลขเวอร์ชันล่าสุด |
| `ไม่พบ state machine ... รัน deploy ก่อน` | รัน `./infra/ronnai_aws.sh deploy` (หรือ `all`) |
| `[WARN] ตั้ง public access block / lifecycle ไม่ได้` | Learner Lab บางรุ่นไม่ให้สิทธิ์ ข้ามได้ |
| `Bytes scanned limit was exceeded` | query สแกนเกิน limit ให้กรองให้แคบลง หรือ `SCAN_LIMIT_MB=...` แล้วรัน `setup` ใหม่ |
| `ไม่พบ data/gee_2026/...` | วางไฟล์ CSV 3 ไฟล์ไว้ใน `data/gee_2026/` (ไม่อยู่ใน git) หรือตั้ง `YEAR=` |
| ภาษาไทยเป็นตัวอ่านไม่ออก | ใช้ Windows Terminal หรือ `chcp 65001` |
