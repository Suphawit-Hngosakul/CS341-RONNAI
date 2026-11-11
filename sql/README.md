# sql/ — Trusted (Glue) + Curated (Iceberg) วิธีรัน

ลำดับใน Athena: `trusted (Parquet) → ลงทะเบียนใน Glue → MERGE → curated (Iceberg)`

Athena รันได้ **ทีละ 1 statement** อย่าวางทั้งไฟล์ ให้คัดลอกทีละบล็อกตามเลข `[1] [2] ...` ในแต่ละไฟล์
เปิด Athena console เลือก region us-east-1, workgroup **`ronnai-wg`**

## ขั้นที่ 1: ลงทะเบียนตาราง trusted (glue_trusted.sql, Issue #14)

ทำให้ Parquet ที่ `validate_gate.py` เขียนลง `trusted/` query ได้ใน Athena (database **`ronnai_trusted`**)

| บล็อก | ทำอะไร | รันกี่ครั้ง |
|---|---|---|
| [1] | `CREATE EXTERNAL TABLE IF NOT EXISTS` 3 ตาราง | ครั้งเดียว (รันซ้ำไม่เสียหาย) |
| [2] | `MSCK REPAIR TABLE` โหลด partition `year=` | ทุกครั้งที่มีปีใหม่ใน `trusted/` |
| [3] | นับแถว / ชุมชน ต่อแหล่ง ปี 2026 | หลัง [2] ทุกครั้ง |
| [4] | `SELECT * ... LIMIT 10` ดูตัวอย่าง (acceptance #14) | ตามต้องการ |

### ผลที่ควรได้จากบล็อก [3]

| src | n_rows | n_communities |
|---|---|---|
| lst | 72 | 36 |
| ndvi | 72 | 36 |
| buildings | 72 | 36 |

(36 ชุมชน × 2 buffer = 72 แถว) ถ้าได้ 0 แถว ให้ตรวจว่ารันบล็อก [2] แล้ว และมีไฟล์ใน `trusted/SOURCE/year=2026/`

### ตาราง

| ตาราง | คอลัมน์ | location |
|---|---|---|
| `ronnai_trusted.lst` | `community_id` string, `buffer_m` bigint, `lst_mean` double, `lst_p90` double, `valid_pixel_ratio` double, `n_scenes` bigint, `batch_id` string, `year` int (partition) | `s3://BUCKET/trusted/lst/` |
| `ronnai_trusted.ndvi` | `community_id` string, `buffer_m` bigint, `ndvi_mean` double, `green_ratio` double, `valid_pixel_ratio` double, `n_scenes` bigint, `batch_id` string, `year` int (partition) | `s3://BUCKET/trusted/ndvi/` |
| `ronnai_trusted.buildings` | `community_id` string, `buffer_m` bigint, `builtup_pct` double, `building_count` bigint, `batch_id` string, `year` int (partition) | `s3://BUCKET/trusted/buildings/` |

`BUCKET` = `cs341-ronnai-<AWS account id>` ถ้าใช้ account อื่นให้แก้ `LOCATION` ในบล็อก [1]

### หมายเหตุ

- คอลัมน์จำนวนเต็มต้องเป็น **`bigint`** เพราะ Parquet เก็บเป็น int64 ถ้าประกาศเป็น `int` Athena จะ `SELECT` ไม่ผ่าน (type mismatch)
- `year` มาจาก partition ใน path (`year=YYYY/`) ไม่ได้อยู่ในไฟล์ query ที่กรอง `WHERE year = 2026` จึงสแกนเฉพาะ partition นั้น
- เลือกเขียน DDL เองแทน Glue Crawler เพราะ type คงที่ (crawler อาจเดา type ผิดหรือเปลี่ยนเอง), รันซ้ำได้ผลเหมือนเดิม (reproducible, อยู่ใน git) และไม่มีค่าใช้จ่ายของ crawler

## ขั้นที่ 2: Curated (Iceberg)

สร้างตาราง curated จาก trusted ด้วย Athena: `trusted (Parquet) → MERGE → curated (Iceberg)`

### ตารางที่ได้

| ไฟล์ | ตาราง | 1 แถวคือ | key | แถวที่คาด (ปี 2026) | location |
|---|---|---|---|---|---|
| `curated_merge.sql` | `ronnai_curated.community_heat` | ชุมชน × ปี (buffer แยกเป็นคอลัมน์ เช่น `lst_mean_300`, `lst_mean_500`) | (community_id, year) | 36 | `s3://cs341-ronnai-730667596562/curated/community_heat/` |

แหล่งไหนไม่มีข้อมูลของชุมชนนั้น คอลัมน์ของแหล่งนั้นจะเป็น NULL (FULL OUTER JOIN ไม่ทิ้งแถว)

### ต้องมีก่อนรัน

1. ตาราง trusted ครบ 3 ตาราง: `ronnai_trusted.lst`, `ronnai_trusted.ndvi`, `ronnai_trusted.buildings` (รัน `glue_trusted.sql` บล็อก [1]–[2] ใน [ขั้นที่ 1](#ขั้นที่-1-ลงทะเบียนตาราง-trusted-glue_trustedsql-issue-14) แล้ว)
2. ข้อมูลใน `trusted/` มาจาก gate ที่ผ่านแล้ว (`validate_gate.py`)
3. เปิด Athena console เลือก region us-east-1, workgroup **`ronnai-wg`**, database **`ronnai_curated`**

### ขั้นตอน

Athena รันได้ **ทีละ 1 statement** อย่าวางทั้งไฟล์ ให้คัดลอกทีละบล็อกตามเลข `[1] [2] ...` ใน `curated_merge.sql`

| บล็อก | ทำอะไร | รันกี่ครั้ง |
|---|---|---|
| [1] | `CREATE TABLE IF NOT EXISTS` ตาราง Iceberg | ครั้งเดียว (รันซ้ำไม่เสียหาย) |
| [2] | `MERGE INTO` จากตาราง trusted ทั้ง 3 | รันซ้ำได้ ผลเหมือนเดิม |
| [3] | ตรวจผล: จำนวนแถว, key ซ้ำ, แถวที่มีค่าครบ | หลัง MERGE ทุกครั้ง |
| [4] | ดูตัวอย่างข้อมูล | ตามต้องการ |

### ผลที่ควรได้จากบล็อก [3]

`total = 36`, `distinct_keys = 36`, `complete_rows = 36`

ถ้าไม่เท่านี้ ให้ตรวจก่อนว่าตาราง trusted มี 72 แถวต่อแหล่ง (`glue_trusted.sql` บล็อก [3]) และรัน `MSCK REPAIR TABLE` แล้ว

### ปรับค่า

- **ปีอื่น:** แก้ `year = 2026` ในบล็อก [2] ทั้ง 3 จุด
- **bucket อื่น:** แก้ `LOCATION` ในบล็อก [1]

### หมายเหตุ

- MERGE ต้องมีตาราง trusted ก่อน จึงต้องทำ trusted ให้เสร็จก่อน curated
- SQL ชุดนี้ทดสอบ logic กับ DuckDB จำลอง ยังไม่ได้ยืนยันกับ Athena จริง: ถ้า `CREATE TABLE` หรือ `MERGE` ขึ้น error ให้เก็บข้อความ error ไว้
