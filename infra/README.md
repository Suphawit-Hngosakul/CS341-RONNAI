# infra/ — AWS data lake ของ RONNAI (Issue #2)

สร้างทุกอย่างของ Issue #2 ด้วย CloudFormation คำสั่งเดียว บน **AWS Academy Learner Lab**

| ไฟล์ | หน้าที่ |
|---|---|
| `ronnai-datalake.yaml` | CloudFormation template: S3 bucket + Glue databases + Athena workgroup |
| `ronnai_infra.py` | CLI สั่ง deploy / verify / status / destroy (ใช้ boto3 รันได้ทั้ง Windows/macOS/Linux) |

## สิ่งที่ถูกสร้าง

| Resource | ชื่อ | หมายเหตุ |
|---|---|---|
| S3 bucket | `ronnai-datalake-<account-id>-us-east-1` | เข้ารหัส SSE-S3, block public access, มีโฟลเดอร์ `incoming/ raw/ trusted/ curated/ quarantine/ athena-results/` |
| Glue database | `ronnai_trusted`, `ronnai_curated` | location ชี้ไป `trusted/` และ `curated/` |
| Athena workgroup | `ronnai-wg` | engine v3, ผลลัพธ์ไป `athena-results/`, **จำกัด scan 1 GB ต่อ query** (บังคับใช้ค่าของ workgroup) |
| Lifecycle | – | ลบผล Athena อายุ > 7 วัน, ลบไฟล์ค้างใน `incoming/` > 30 วัน, ยกเลิก multipart upload ค้าง |

ไม่มีการสร้าง IAM user/role (Learner Lab ไม่อนุญาต) ทุกอย่างทำงานด้วย `LabRole` ที่ lab ให้มา

## วิธีใช้

1. เข้า Learner Lab กด **Start Lab** รอไฟเขียว แล้วกด **AWS Details → AWS CLI: Show**
2. คัดลอกทั้งก้อนไปวางใน `~/.aws/credentials` (Windows: `%UserProfile%\.aws\credentials`) ใต้ `[default]`
   - credentials หมดอายุทุกครั้งที่ session lab จบ (~4 ชม.) ต้องคัดลอกใหม่
3. รันจาก root ของ repo

```bash
pip install -r requirements.txt
python infra/ronnai_infra.py deploy     # สร้าง/อัปเดต (รันซ้ำได้ ไม่สร้างซ้ำ) และเขียน config/settings.yaml ให้
python infra/ronnai_infra.py verify     # เช็คครบ 6 zone, 2 DB, scan limit และรัน SELECT 1 ใน Athena
python infra/ronnai_infra.py status     # ดูชื่อ bucket / workgroup
```

ตัวเลือก: `--region us-west-2`, `--profile <name>`, `deploy --scan-limit-mb 512`

ถ้าอยากใช้ AWS CLI ตรง ๆ ก็ได้ (ผลเหมือนกัน แต่ต้องสร้างโฟลเดอร์ zone เอง)

```bash
aws cloudformation deploy --region us-east-1 --stack-name ronnai-datalake \
  --template-file infra/ronnai-datalake.yaml
```

## ทำงาน 3 คน

Learner Lab ของแต่ละคนเป็นคนละ AWS account เลือกได้ 2 แบบ

- **แบบ A (แนะนำ, ง่ายสุด):** ทุกคนรัน `deploy` ใน lab ของตัวเอง ได้โครงสร้างเหมือนกันทุกตัว แล้วรัน `verify` เพื่อยืนยันว่า query ได้ (เกณฑ์ "ทั้ง 3 คนรัน query ได้")
- **แบบ B (ใช้ bucket กลาง):** คนที่เป็นเจ้าของ bucket รัน
  `python infra/ronnai_infra.py deploy --teammates 111122223333,444455556666`
  เพื่อให้ `LabRole` ของ account เพื่อนอ่าน/เขียน bucket ได้ (ใช้ได้เมื่อนโยบายของ lab อนุญาต cross-account)

## ลบทิ้ง (ประหยัด budget)

```bash
python infra/ronnai_infra.py destroy           # ลบได้เฉพาะตอน bucket ยังไม่มีข้อมูลจริง (กันข้อมูลหาย)
python infra/ronnai_infra.py destroy --purge   # ลบข้อมูลทุกไฟล์ใน bucket แล้วลบ stack (ถาวร, ต้องพิมพ์ชื่อ bucket ยืนยัน)
```

S3, Glue และ Athena คิดเงินตามการใช้งาน ไม่มีค่าเปิดทิ้งไว้ (นอกจากค่าเก็บไฟล์เล็กน้อย)
