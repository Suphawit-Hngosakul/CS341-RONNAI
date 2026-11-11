-- =====================================================================
-- glue_trusted.sql  (Issue #14)
-- ลงทะเบียนตาราง trusted (Parquet) ใน Glue Data Catalog ผ่าน Athena
-- Register trusted Parquet tables in the Glue Data Catalog via Athena.
--
-- วิธีรัน / How to run:
--   Athena console, region us-east-1, workgroup ronnai-wg
--   รันทีละ 1 statement ตามบล็อก [1] -> [2] -> [3] -> [4]
--   Run one statement at a time, block by block [1]..[4].
--
-- หมายเหตุ / Notes:
--   - คอลัมน์ตรงกับ Parquet ที่ validate_gate.py เขียน (int64 = bigint)
--     Columns match the Parquet written by the gate (int64 = bigint).
--   - year เป็น partition จาก path trusted/SOURCE/year=YYYY/
--     year is the partition taken from path trusted/SOURCE/year=YYYY/.
--   - AWS account อื่น: แก้ bucket cs341-ronnai-ACCOUNTID ใน LOCATION
--     For another AWS account change bucket cs341-ronnai-ACCOUNTID in LOCATION.
-- =====================================================================


-- ---------------------------------------------------------------------
-- [1] สร้างตาราง / create tables (รันครั้งเดียว รันซ้ำได้ / run once, safe to rerun)
-- ---------------------------------------------------------------------

-- [1a] LST: อุณหภูมิพื้นผิว / land surface temperature
CREATE EXTERNAL TABLE IF NOT EXISTS ronnai_trusted.lst (
    community_id      string,
    buffer_m          bigint,
    lst_mean          double,
    lst_p90           double,
    valid_pixel_ratio double,
    n_scenes          bigint,
    batch_id          string
)
PARTITIONED BY (year int)
STORED AS PARQUET
LOCATION 's3://cs341-ronnai-477710504722/trusted/lst/';

-- [1b] NDVI: พื้นที่สีเขียว / vegetation
CREATE EXTERNAL TABLE IF NOT EXISTS ronnai_trusted.ndvi (
    community_id      string,
    buffer_m          bigint,
    ndvi_mean         double,
    green_ratio       double,
    valid_pixel_ratio double,
    n_scenes          bigint,
    batch_id          string
)
PARTITIONED BY (year int)
STORED AS PARQUET
LOCATION 's3://cs341-ronnai-477710504722/trusted/ndvi/';

-- [1c] Buildings: สิ่งปลูกสร้าง / built-up
CREATE EXTERNAL TABLE IF NOT EXISTS ronnai_trusted.buildings (
    community_id      string,
    buffer_m          bigint,
    builtup_pct       double,
    building_count    bigint,
    batch_id          string
)
PARTITIONED BY (year int)
STORED AS PARQUET
LOCATION 's3://cs341-ronnai-477710504722/trusted/buildings/';


-- ---------------------------------------------------------------------
-- [2] โหลด partition / load partitions (รันซ้ำเมื่อมีปีใหม่ / rerun when a new year appears)
-- ---------------------------------------------------------------------
MSCK REPAIR TABLE ronnai_trusted.lst;
MSCK REPAIR TABLE ronnai_trusted.ndvi;
MSCK REPAIR TABLE ronnai_trusted.buildings;


-- ---------------------------------------------------------------------
-- [3] ตรวจผล / check: ปี 2026 ควรได้ 72 แถว และ 36 ชุมชน ต่อแหล่ง
--     expect 72 rows and 36 communities per source for 2026
-- ---------------------------------------------------------------------
SELECT 'lst' AS src, count(*) AS n_rows, count(DISTINCT community_id) AS n_communities
FROM ronnai_trusted.lst WHERE year = 2026
UNION ALL
SELECT 'ndvi', count(*), count(DISTINCT community_id)
FROM ronnai_trusted.ndvi WHERE year = 2026
UNION ALL
SELECT 'buildings', count(*), count(DISTINCT community_id)
FROM ronnai_trusted.buildings WHERE year = 2026;


-- ---------------------------------------------------------------------
-- [4] ดูตัวอย่าง / sample rows (acceptance ของ Issue #14)
-- ---------------------------------------------------------------------
SELECT * FROM ronnai_trusted.lst LIMIT 10;
SELECT * FROM ronnai_trusted.ndvi LIMIT 10;
SELECT * FROM ronnai_trusted.buildings LIMIT 10;
