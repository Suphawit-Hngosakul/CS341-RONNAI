# infra/ — ตั้งค่า AWS ผ่าน CLI (Issue #2)

สคริปต์สร้าง "บ้าน" ของข้อมูลบน **AWS Academy Learner Lab** ด้วยคำสั่งเดียว รันซ้ำได้ (ของที่มีแล้วจะข้าม ไม่สร้างซ้ำ)

| ไฟล์ | ใช้ที่ไหน |
|---|---|
| `ronnai_aws.ps1` | Windows PowerShell |
| `ronnai_aws.sh` | Terminal ของ Learner Lab, AWS CloudShell, Git Bash, macOS/Linux |

ทั้งสองไฟล์ทำงานเหมือนกัน มี 3 คำสั่ง: `setup` (สร้าง), `verify` (ตรวจ + รัน `SELECT 1`), `teardown` (ลบทั้งหมด)

## สิ่งที่ `setup` สร้าง

| บริการ | ชื่อ (ค่าเริ่มต้น) | รายละเอียด |
|---|---|---|
| S3 bucket | `cs341-ronnai-<account_id>` | ใส่เลข account ต่อท้ายเพราะชื่อ bucket ต้องไม่ซ้ำกับใครในโลก, ปิด public access |
| S3 zones | `incoming/ raw/ trusted/ curated/ quarantine/ athena-results/` | ความหมายของแต่ละ zone ดู README หลัก |
| S3 lifecycle | `athena-results/` | ลบผล query ที่เก่ากว่า 7 วันอัตโนมัติ (ประหยัดพื้นที่) |
| Glue database | `ronnai_trusted` → `s3://<bucket>/trusted/` | |
| Glue database | `ronnai_curated` → `s3://<bucket>/curated/` | |
| Athena workgroup | `ronnai-wg` | ผล query ไปที่ `athena-results/`, **จำกัดสแกน 1 GB ต่อ query**, บังคับใช้ค่าของ workgroup, engine v3 (รองรับ Iceberg) |
| ไฟล์ config | `config/settings.yaml` | สร้างให้อัตโนมัติถ้ายังไม่มี (ไฟล์นี้ถูก `.gitignore` ไว้) |

ไม่มีการสร้าง IAM user/role เพราะ Learner Lab ไม่อนุญาต ทุกอย่างรันด้วยสิทธิ์ `LabRole` ของ lab

---

## วิธีที่ 1: รันบน Windows (PowerShell)

### ครั้งแรกครั้งเดียว: ติดตั้ง AWS CLI v2
ดาวน์โหลดและติดตั้ง https://awscli.amazonaws.com/AWSCLIV2.msi แล้ว **เปิด PowerShell ใหม่** เช็กว่าได้ด้วย `aws --version`

### ทุกครั้งที่เปิด lab: ใส่ credentials
credentials ของ Learner Lab **หมดอายุทุกครั้งที่ session จบ (ประมาณ 4 ชม.)** ต้องคัดลอกใหม่ทุกรอบ

1. เข้า Learner Lab กด **Start Lab** รอจนวงกลมเป็นสีเขียว
2. กด **AWS Details** → ตรง *AWS CLI* กด **Show** แล้วคัดลอกข้อความทั้งก้อน (มี `[default]`, `aws_access_key_id`, `aws_secret_access_key`, `aws_session_token`)
3. วางทับทั้งไฟล์ `%USERPROFILE%\.aws\credentials`
   ```powershell
   mkdir $HOME\.aws -Force | Out-Null
   notepad $HOME\.aws\credentials     # วางข้อความ แล้วกดบันทึก
   ```
4. ทดสอบ: `aws sts get-caller-identity` ต้องเห็นเลข account

### รันสคริปต์ (ที่โฟลเดอร์ repo)
```powershell
cd "F:\My Drive\CS\CS341\Project\CS341-RONNAI"

powershell -ExecutionPolicy Bypass -File infra\ronnai_aws.ps1 -Action setup
powershell -ExecutionPolicy Bypass -File infra\ronnai_aws.ps1 -Action verify
```
ถ้า `verify` ขึ้น **ผ่านทุกข้อ** และ `SELECT 1 -> 1` แปลว่าพร้อมใช้งาน

ปรับค่าได้ เช่น
```powershell
powershell -ExecutionPolicy Bypass -File infra\ronnai_aws.ps1 -Action setup -ScanLimitMB 512 -Bucket cs341-ronnai-team
```
พารามิเตอร์ทั้งหมด: `-Region` (us-east-1) `-AwsProfile` `-Bucket` `-Workgroup` (ronnai-wg) `-DbTrusted` `-DbCurated` `-ScanLimitMB` (1024) `-ResultExpireDays` (7)

## วิธีที่ 2: รันใน Terminal ของ Learner Lab (ไม่ต้องคัดลอก credentials)

หน้า Learner Lab มี terminal ที่ตั้งค่า AWS CLI ไว้แล้ว
```bash
git clone https://github.com/Suphawit-Hngosakul/CS341-RONNAI.git
cd CS341-RONNAI && git checkout development
bash infra/ronnai_aws.sh setup
bash infra/ronnai_aws.sh verify
```
ปรับค่าด้วย env var เช่น `SCAN_LIMIT_MB=512 BUCKET=cs341-ronnai-team bash infra/ronnai_aws.sh setup`

> ถ้ารันวิธีนี้ `config/settings.yaml` จะถูกสร้างใน terminal ของ lab ไม่ใช่บนเครื่องเรา ให้คัดลอกค่า bucket ที่สคริปต์พิมพ์ออกมาไปใส่ `config/settings.yaml` บนเครื่องเอง

---

## ให้ทั้ง 3 คนรัน query ได้ (ข้อ 4 ของ issue)

Learner Lab ของแต่ละคนเป็น **AWS account แยกกัน** และสร้าง IAM user ให้เพื่อนไม่ได้ จึงแนะนำแบบนี้
- ทุกคนรัน `setup` + `verify` ใน lab ของตัวเอง → ได้ bucket ของตัวเอง (ชื่อต่างกันตามเลข account) และเห็น `SELECT 1 -> 1` เป็นหลักฐานว่า query ได้
- pipeline จริง (ingest → gate → curated) ให้รันที่ account ของเจ้าของ storage (ศุภวิชญ์) เป็นหลัก แล้วเพื่อนรัน pipeline ชุดเดียวกันใน account ตัวเองได้ เพราะทุกอย่างอ่านชื่อ bucket จาก `config/settings.yaml`
- ข้อจำกัดนี้ควรเขียนไว้ใน `docs/` หรือ report ด้วย

## ลบทั้งหมด (ตอนจบวิชา)
```powershell
powershell -ExecutionPolicy Bypass -File infra\ronnai_aws.ps1 -Action teardown
```
สคริปต์จะให้พิมพ์ชื่อ bucket เพื่อยืนยัน **ข้อมูลทุก zone รวม raw/ และ quarantine/ จะหายถาวร**

## เจอปัญหา

| อาการ | วิธีแก้ |
|---|---|
| `ExpiredToken` / `InvalidClientTokenId` / `credentials ใช้ไม่ได้` | session lab หมดอายุ กด Start Lab แล้วคัดลอก credentials ใหม่ |
| `running scripts is disabled on this system` | ใช้ `powershell -ExecutionPolicy Bypass -File ...` ตามตัวอย่าง |
| `สร้าง bucket ไม่ได้` | ชื่อซ้ำกับคนอื่น ใส่ `-Bucket ชื่อใหม่` (ตัวพิมพ์เล็ก ตัวเลข และ `-` เท่านั้น) |
| `[WARN] ตั้ง public access block / lifecycle ไม่ได้` | Learner Lab บางรุ่นไม่ให้สิทธิ์ ข้ามได้ ไม่กระทบการทำงาน |
| `Query cancelled! : Bytes scanned limit was exceeded` | query สแกนเกิน limit แปลว่า scan limit ทำงาน ให้กรองข้อมูลให้แคบลง หรือรัน setup ใหม่ด้วย `-ScanLimitMB` ที่สูงขึ้น |
| ภาษาไทยเป็นตัวอ่านไม่ออก | ใช้ Windows Terminal หรือพิมพ์ `chcp 65001` ก่อนรัน |
| `ไม่พบ AWS CLI` | ติดตั้ง AWS CLI v2 แล้วปิดเปิด PowerShell ใหม่ |
