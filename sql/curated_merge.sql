-- Issue #11: ตาราง curated แบบเดียวกว้าง (Apache Iceberg) + MERGE จาก trusted
-- รันใน Athena: workgroup = ronnai-wg, database = ronnai_curated  (รันทีละคำสั่ง ตามเลข [1] [2] ...)
-- ต้องทำ #10 (glue_trusted.sql) ให้ผ่านก่อน เพราะ MERGE อ่านจากตาราง ronnai_trusted.lst / ndvi / buildings
--
-- หนึ่งแถว = (community_id, buffer_m, year)  ค่า LST + NDVI + อาคารอยู่แถวเดียวกัน
-- แหล่งไหนไม่มีข้อมูลของ key นั้น คอลัมน์ของแหล่งนั้นจะเป็น NULL (FULL OUTER JOIN ไม่ทิ้งแถวของแหล่งใดเลย)
-- คอลัมน์ที่ชื่อซ้ำข้ามแหล่งถูกเติมคำนำหน้า: lst_valid_pixel_ratio / ndvi_valid_pixel_ratio, lst_n_scenes / ndvi_n_scenes
-- ถ้าใช้ bucket อื่น แก้ LOCATION ใน [1] ให้ตรง ถ้าเปลี่ยนปี แก้ year = 2026 ใน [2] ทั้ง 3 จุด

-- ================================================================ [1] สร้างตาราง Iceberg (รันครั้งเดียว)
CREATE TABLE IF NOT EXISTS ronnai_curated.community_heat (
  community_id          string,
  buffer_m              bigint,
  year                  int,
  lst_mean              double,
  lst_p90               double,
  lst_valid_pixel_ratio double,
  lst_n_scenes          bigint,
  ndvi_mean             double,
  green_ratio           double,
  ndvi_valid_pixel_ratio double,
  ndvi_n_scenes         bigint,
  builtup_pct           double,
  building_count        bigint,
  updated_at            timestamp
)
PARTITIONED BY (year)
LOCATION 's3://cs341-ronnai-730667596562/curated/community_heat/'
TBLPROPERTIES ('table_type' = 'ICEBERG', 'format' = 'parquet');

-- ================================================================ [2] MERGE (รันซ้ำได้ ผลเหมือนเดิม)
-- ON ใช้ key ครบสามตัว รวม year (ให้ Iceberg เลือกอ่านเฉพาะ partition ปีนั้น)
-- UPDATE เฉพาะแถวที่ค่าเปลี่ยนจริง (IS DISTINCT FROM รู้จัก NULL) ไม่เขียนแถวเดิมซ้ำโดยไม่จำเป็น
MERGE INTO ronnai_curated.community_heat AS t
USING (
  SELECT
    coalesce(l.community_id, n.community_id, b.community_id) AS community_id,
    coalesce(l.buffer_m,     n.buffer_m,     b.buffer_m)     AS buffer_m,
    coalesce(l.year,         n.year,         b.year)         AS year,
    l.lst_mean, l.lst_p90,
    l.valid_pixel_ratio AS lst_valid_pixel_ratio,
    l.n_scenes          AS lst_n_scenes,
    n.ndvi_mean, n.green_ratio,
    n.valid_pixel_ratio AS ndvi_valid_pixel_ratio,
    n.n_scenes          AS ndvi_n_scenes,
    b.builtup_pct, b.building_count
  FROM (SELECT * FROM ronnai_trusted.lst       WHERE year = 2026) l
  FULL OUTER JOIN (SELECT * FROM ronnai_trusted.ndvi      WHERE year = 2026) n
    ON  l.community_id = n.community_id AND l.buffer_m = n.buffer_m AND l.year = n.year
  FULL OUTER JOIN (SELECT * FROM ronnai_trusted.buildings WHERE year = 2026) b
    ON  coalesce(l.community_id, n.community_id) = b.community_id
    AND coalesce(l.buffer_m,     n.buffer_m)     = b.buffer_m
    AND coalesce(l.year,         n.year)         = b.year
) AS s
ON  t.community_id = s.community_id AND t.buffer_m = s.buffer_m AND t.year = s.year
WHEN MATCHED AND (
     t.lst_mean               IS DISTINCT FROM s.lst_mean
  OR t.lst_p90                IS DISTINCT FROM s.lst_p90
  OR t.lst_valid_pixel_ratio  IS DISTINCT FROM s.lst_valid_pixel_ratio
  OR t.lst_n_scenes           IS DISTINCT FROM s.lst_n_scenes
  OR t.ndvi_mean              IS DISTINCT FROM s.ndvi_mean
  OR t.green_ratio            IS DISTINCT FROM s.green_ratio
  OR t.ndvi_valid_pixel_ratio IS DISTINCT FROM s.ndvi_valid_pixel_ratio
  OR t.ndvi_n_scenes          IS DISTINCT FROM s.ndvi_n_scenes
  OR t.builtup_pct            IS DISTINCT FROM s.builtup_pct
  OR t.building_count         IS DISTINCT FROM s.building_count
) THEN UPDATE SET
  lst_mean = s.lst_mean, lst_p90 = s.lst_p90,
  lst_valid_pixel_ratio = s.lst_valid_pixel_ratio, lst_n_scenes = s.lst_n_scenes,
  ndvi_mean = s.ndvi_mean, green_ratio = s.green_ratio,
  ndvi_valid_pixel_ratio = s.ndvi_valid_pixel_ratio, ndvi_n_scenes = s.ndvi_n_scenes,
  builtup_pct = s.builtup_pct, building_count = s.building_count,
  updated_at = current_timestamp
WHEN NOT MATCHED THEN INSERT (
  community_id, buffer_m, year,
  lst_mean, lst_p90, lst_valid_pixel_ratio, lst_n_scenes,
  ndvi_mean, green_ratio, ndvi_valid_pixel_ratio, ndvi_n_scenes,
  builtup_pct, building_count, updated_at
) VALUES (
  s.community_id, s.buffer_m, s.year,
  s.lst_mean, s.lst_p90, s.lst_valid_pixel_ratio, s.lst_n_scenes,
  s.ndvi_mean, s.green_ratio, s.ndvi_valid_pixel_ratio, s.ndvi_n_scenes,
  s.builtup_pct, s.building_count, current_timestamp
);

-- ================================================================ [3] ตรวจผล: จำนวนแถว / key ซ้ำ / ความครอบคลุมของแต่ละแหล่ง
-- คาดว่า: total = 72, distinct_keys = 72, แต่ละ has_* = 72
SELECT
  count(*)                                                            AS total,
  count(DISTINCT concat(community_id, '|', cast(buffer_m AS varchar), '|', cast(year AS varchar))) AS distinct_keys,
  count(lst_mean)       AS has_lst,
  count(ndvi_mean)      AS has_ndvi,
  count(builtup_pct)    AS has_buildings
FROM ronnai_curated.community_heat;

-- ================================================================ [4] ดูตัวอย่าง
SELECT * FROM ronnai_curated.community_heat ORDER BY community_id, buffer_m LIMIT 10;

-- ================================================================ [5] ตรวจว่า MERGE รันซ้ำแล้วไม่เขียนซ้ำ
-- รัน [2] อีกรอบ แล้วรันคำสั่งนี้: ถ้าแถวไม่เปลี่ยน จะไม่มี snapshot ใหม่ที่ added_rows > 0 (ดูคอลัมน์ summary)
SELECT committed_at, operation, summary FROM "ronnai_curated"."community_heat$snapshots" ORDER BY committed_at;
