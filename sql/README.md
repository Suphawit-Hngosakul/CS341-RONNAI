# sql/ — Curated (Iceberg) วิธีรัน

สร้างตาราง curated จาก trusted ด้วย Athena: `trusted (Parquet) → MERGE → curated (Iceberg)`

## ตารางที่ได้

| ไฟล์ | ตาราง | 1 แถวคือ | key | แถวที่คาด (ปี 2026) | location |
|---|---|---|---|---|---|
| `curated_merge.sql` | `ronnai_curated.community_heat` | ชุมชน × ปี (buffer แยกเป็นคอลัมน์ เช่น `lst_mean_300`, `lst_mean_500`) | (community_id, year) | 36 | `s3://cs341-ronnai-730667596562/curated/community_heat/` |

แหล่งไหนไม่มีข้อมูลของชุมชนนั้น คอลัมน์ของแหล่งนั้นจะเป็น NULL (FULL OUTER JOIN ไม่ทิ้งแถว)

## ต้องมีก่อนรัน

1. ตาราง trusted ครบ 3 ตาราง: `ronnai_trusted.lst`, `ronnai_trusted.ndvi`, `ronnai_trusted.buildings` (สร้างด้วย `glue_trusted.sql` แล้ว `MSCK REPAIR TABLE` ให้เห็น partition)
2. ข้อมูลใน `trusted/` มาจาก gate ที่ผ่านแล้ว (`validate_gate.py`)
3. เปิด Athena console เลือก region us-east-1, workgroup **`ronnai-wg`**, database **`ronnai_curated`**

## ขั้นตอน

Athena รันได้ **ทีละ 1 statement** อย่าวางทั้งไฟล์ ให้คัดลอกทีละบล็อกตามเลข `[1] [2] ...` ใน `curated_merge.sql`

| บล็อก | ทำอะไร | รันกี่ครั้ง |
|---|---|---|
| [1] | `CREATE TABLE IF NOT EXISTS` ตาราง Iceberg | ครั้งเดียว (รันซ้ำไม่เสียหาย) |
| [2] | `MERGE INTO` จากตาราง trusted ทั้ง 3 | รันซ้ำได้ ผลเหมือนเดิม |
| [3] | ตรวจผล: จำนวนแถว, key ซ้ำ, แถวที่มีค่าครบ | หลัง MERGE ทุกครั้ง |
| [4] | ดูตัวอย่างข้อมูล | ตามต้องการ |

## ผลที่ควรได้จากบล็อก [3]

`total = 36`, `distinct_keys = 36`, `complete_rows = 36`

ถ้าไม่เท่านี้ ให้ตรวจก่อนว่าตาราง trusted มี 72 แถวต่อแหล่ง (`SELECT count(*) FROM ronnai_trusted.lst WHERE year = 2026` ฯลฯ) และรัน `MSCK REPAIR TABLE` แล้ว

## ปรับค่า

- **ปีอื่น:** แก้ `year = 2026` ในบล็อก [2] ทั้ง 3 จุด
- **bucket อื่น:** แก้ `LOCATION` ในบล็อก [1]

## หมายเหตุ

- MERGE ต้องมีตาราง trusted ก่อน จึงต้องทำ trusted ให้เสร็จก่อน curated
- SQL ชุดนี้ทดสอบ logic กับ DuckDB จำลอง ยังไม่ได้ยืนยันกับ Athena จริง: ถ้า `CREATE TABLE` หรือ `MERGE` ขึ้น error ให้เก็บข้อความ error ไว้
