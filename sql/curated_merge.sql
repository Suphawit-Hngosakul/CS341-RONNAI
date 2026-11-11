-- community_heat: curated รวบ buffer เป็นคอลัมน์ (1 แถว = ชุมชน × ปี)
-- รันใน Athena: workgroup = ronnai-wg, database = ronnai_curated  (รันทีละคำสั่ง ตามเลข [1] [2] ...)
-- ต้องทำ #10 (ตาราง ronnai_trusted.lst / ndvi / buildings) ให้ผ่านก่อน
-- ชื่อคอลัมน์ = <ชื่อค่า>_<buffer เมตร> เช่น lst_mean_300, lst_mean_500
-- key = (community_id, year)  คาดว่ามี 36 แถวต่อปี (36 ชุมชน)
-- แหล่งไหนไม่มีข้อมูลของชุมชนนั้น คอลัมน์ของแหล่งนั้นเป็น NULL
-- ถ้าใช้ bucket อื่น แก้ LOCATION ใน [1] ถ้าเปลี่ยนปี แก้ year = 2026 ใน [2] ทั้ง 3 จุด

-- ================================================================ [1] สร้างตาราง Iceberg (รันครั้งเดียว)
CREATE TABLE IF NOT EXISTS ronnai_curated.community_heat (
  community_id                string,
  year                        int,
  lst_mean_300                double,
  lst_mean_500                double,
  lst_p90_300                 double,
  lst_p90_500                 double,
  lst_valid_pixel_ratio_300   double,
  lst_valid_pixel_ratio_500   double,
  lst_n_scenes_300            bigint,
  lst_n_scenes_500            bigint,
  ndvi_mean_300               double,
  ndvi_mean_500               double,
  green_ratio_300             double,
  green_ratio_500             double,
  ndvi_valid_pixel_ratio_300  double,
  ndvi_valid_pixel_ratio_500  double,
  ndvi_n_scenes_300           bigint,
  ndvi_n_scenes_500           bigint,
  builtup_pct_300             double,
  builtup_pct_500             double,
  building_count_300          bigint,
  building_count_500          bigint,
  updated_at                  timestamp
)
PARTITIONED BY (year)
LOCATION 's3://cs341-ronnai-730667596562/curated/community_heat/'
TBLPROPERTIES ('table_type' = 'ICEBERG', 'format' = 'parquet');

-- ================================================================ [2] MERGE (รันซ้ำได้ ผลเหมือนเดิม)
-- แต่ละแหล่งถูกรวบ buffer เป็นคอลัมน์ก่อน (max(CASE WHEN buffer_m = ... )) แล้ว FULL OUTER JOIN ด้วย (community_id, year)
MERGE INTO ronnai_curated.community_heat AS t
USING (
  WITH
  l AS (
    SELECT community_id, year,
      max(CASE WHEN buffer_m = 300 THEN lst_mean END) AS lst_mean_300,
      max(CASE WHEN buffer_m = 500 THEN lst_mean END) AS lst_mean_500,
      max(CASE WHEN buffer_m = 300 THEN lst_p90 END) AS lst_p90_300,
      max(CASE WHEN buffer_m = 500 THEN lst_p90 END) AS lst_p90_500,
      max(CASE WHEN buffer_m = 300 THEN valid_pixel_ratio END) AS lst_valid_pixel_ratio_300,
      max(CASE WHEN buffer_m = 500 THEN valid_pixel_ratio END) AS lst_valid_pixel_ratio_500,
      max(CASE WHEN buffer_m = 300 THEN n_scenes END) AS lst_n_scenes_300,
      max(CASE WHEN buffer_m = 500 THEN n_scenes END) AS lst_n_scenes_500
    FROM ronnai_trusted.lst WHERE year = 2026 GROUP BY community_id, year
  ),
  n AS (
    SELECT community_id, year,
      max(CASE WHEN buffer_m = 300 THEN ndvi_mean END) AS ndvi_mean_300,
      max(CASE WHEN buffer_m = 500 THEN ndvi_mean END) AS ndvi_mean_500,
      max(CASE WHEN buffer_m = 300 THEN green_ratio END) AS green_ratio_300,
      max(CASE WHEN buffer_m = 500 THEN green_ratio END) AS green_ratio_500,
      max(CASE WHEN buffer_m = 300 THEN valid_pixel_ratio END) AS ndvi_valid_pixel_ratio_300,
      max(CASE WHEN buffer_m = 500 THEN valid_pixel_ratio END) AS ndvi_valid_pixel_ratio_500,
      max(CASE WHEN buffer_m = 300 THEN n_scenes END) AS ndvi_n_scenes_300,
      max(CASE WHEN buffer_m = 500 THEN n_scenes END) AS ndvi_n_scenes_500
    FROM ronnai_trusted.ndvi WHERE year = 2026 GROUP BY community_id, year
  ),
  b AS (
    SELECT community_id, year,
      max(CASE WHEN buffer_m = 300 THEN builtup_pct END) AS builtup_pct_300,
      max(CASE WHEN buffer_m = 500 THEN builtup_pct END) AS builtup_pct_500,
      max(CASE WHEN buffer_m = 300 THEN building_count END) AS building_count_300,
      max(CASE WHEN buffer_m = 500 THEN building_count END) AS building_count_500
    FROM ronnai_trusted.buildings WHERE year = 2026 GROUP BY community_id, year
  )
  SELECT
    coalesce(l.community_id, n.community_id, b.community_id) AS community_id,
    coalesce(l.year, n.year, b.year) AS year,
    l.lst_mean_300,
    l.lst_mean_500,
    l.lst_p90_300,
    l.lst_p90_500,
    l.lst_valid_pixel_ratio_300,
    l.lst_valid_pixel_ratio_500,
    l.lst_n_scenes_300,
    l.lst_n_scenes_500,
    n.ndvi_mean_300,
    n.ndvi_mean_500,
    n.green_ratio_300,
    n.green_ratio_500,
    n.ndvi_valid_pixel_ratio_300,
    n.ndvi_valid_pixel_ratio_500,
    n.ndvi_n_scenes_300,
    n.ndvi_n_scenes_500,
    b.builtup_pct_300,
    b.builtup_pct_500,
    b.building_count_300,
    b.building_count_500
  FROM l
  FULL OUTER JOIN n ON l.community_id = n.community_id AND l.year = n.year
  FULL OUTER JOIN b ON coalesce(l.community_id, n.community_id) = b.community_id
                   AND coalesce(l.year, n.year) = b.year
) AS s
ON t.community_id = s.community_id AND t.year = s.year
WHEN MATCHED AND (
     t.lst_mean_300 IS DISTINCT FROM s.lst_mean_300
  OR t.lst_mean_500 IS DISTINCT FROM s.lst_mean_500
  OR t.lst_p90_300 IS DISTINCT FROM s.lst_p90_300
  OR t.lst_p90_500 IS DISTINCT FROM s.lst_p90_500
  OR t.lst_valid_pixel_ratio_300 IS DISTINCT FROM s.lst_valid_pixel_ratio_300
  OR t.lst_valid_pixel_ratio_500 IS DISTINCT FROM s.lst_valid_pixel_ratio_500
  OR t.lst_n_scenes_300 IS DISTINCT FROM s.lst_n_scenes_300
  OR t.lst_n_scenes_500 IS DISTINCT FROM s.lst_n_scenes_500
  OR t.ndvi_mean_300 IS DISTINCT FROM s.ndvi_mean_300
  OR t.ndvi_mean_500 IS DISTINCT FROM s.ndvi_mean_500
  OR t.green_ratio_300 IS DISTINCT FROM s.green_ratio_300
  OR t.green_ratio_500 IS DISTINCT FROM s.green_ratio_500
  OR t.ndvi_valid_pixel_ratio_300 IS DISTINCT FROM s.ndvi_valid_pixel_ratio_300
  OR t.ndvi_valid_pixel_ratio_500 IS DISTINCT FROM s.ndvi_valid_pixel_ratio_500
  OR t.ndvi_n_scenes_300 IS DISTINCT FROM s.ndvi_n_scenes_300
  OR t.ndvi_n_scenes_500 IS DISTINCT FROM s.ndvi_n_scenes_500
  OR t.builtup_pct_300 IS DISTINCT FROM s.builtup_pct_300
  OR t.builtup_pct_500 IS DISTINCT FROM s.builtup_pct_500
  OR t.building_count_300 IS DISTINCT FROM s.building_count_300
  OR t.building_count_500 IS DISTINCT FROM s.building_count_500
) THEN UPDATE SET
  lst_mean_300 = s.lst_mean_300,
  lst_mean_500 = s.lst_mean_500,
  lst_p90_300 = s.lst_p90_300,
  lst_p90_500 = s.lst_p90_500,
  lst_valid_pixel_ratio_300 = s.lst_valid_pixel_ratio_300,
  lst_valid_pixel_ratio_500 = s.lst_valid_pixel_ratio_500,
  lst_n_scenes_300 = s.lst_n_scenes_300,
  lst_n_scenes_500 = s.lst_n_scenes_500,
  ndvi_mean_300 = s.ndvi_mean_300,
  ndvi_mean_500 = s.ndvi_mean_500,
  green_ratio_300 = s.green_ratio_300,
  green_ratio_500 = s.green_ratio_500,
  ndvi_valid_pixel_ratio_300 = s.ndvi_valid_pixel_ratio_300,
  ndvi_valid_pixel_ratio_500 = s.ndvi_valid_pixel_ratio_500,
  ndvi_n_scenes_300 = s.ndvi_n_scenes_300,
  ndvi_n_scenes_500 = s.ndvi_n_scenes_500,
  builtup_pct_300 = s.builtup_pct_300,
  builtup_pct_500 = s.builtup_pct_500,
  building_count_300 = s.building_count_300,
  building_count_500 = s.building_count_500,
  updated_at = current_timestamp
WHEN NOT MATCHED THEN INSERT (
  community_id, year, lst_mean_300, lst_mean_500, lst_p90_300, lst_p90_500, lst_valid_pixel_ratio_300, lst_valid_pixel_ratio_500, lst_n_scenes_300, lst_n_scenes_500, ndvi_mean_300, ndvi_mean_500, green_ratio_300, green_ratio_500, ndvi_valid_pixel_ratio_300, ndvi_valid_pixel_ratio_500, ndvi_n_scenes_300, ndvi_n_scenes_500, builtup_pct_300, builtup_pct_500, building_count_300, building_count_500, updated_at
) VALUES (
  s.community_id, s.year, s.lst_mean_300, s.lst_mean_500, s.lst_p90_300, s.lst_p90_500, s.lst_valid_pixel_ratio_300, s.lst_valid_pixel_ratio_500, s.lst_n_scenes_300, s.lst_n_scenes_500, s.ndvi_mean_300, s.ndvi_mean_500, s.green_ratio_300, s.green_ratio_500, s.ndvi_valid_pixel_ratio_300, s.ndvi_valid_pixel_ratio_500, s.ndvi_n_scenes_300, s.ndvi_n_scenes_500, s.builtup_pct_300, s.builtup_pct_500, s.building_count_300, s.building_count_500, current_timestamp
);

-- ================================================================ [3] ตรวจผล
-- คาดว่า: total = 36, distinct_keys = 36, complete_rows = ชุมชนที่มีค่าครบทั้ง LST/NDVI/อาคาร ทั้งสอง buffer (ถ้าข้อมูลครบ = 36)
SELECT
  count(*) AS total,
  count(DISTINCT concat(community_id, '|', cast(year AS varchar))) AS distinct_keys,
  count_if(lst_mean_300 IS NOT NULL AND lst_mean_500 IS NOT NULL AND lst_p90_300 IS NOT NULL AND lst_p90_500 IS NOT NULL AND lst_valid_pixel_ratio_300 IS NOT NULL AND lst_valid_pixel_ratio_500 IS NOT NULL AND lst_n_scenes_300 IS NOT NULL AND lst_n_scenes_500 IS NOT NULL AND ndvi_mean_300 IS NOT NULL AND ndvi_mean_500 IS NOT NULL AND green_ratio_300 IS NOT NULL AND green_ratio_500 IS NOT NULL AND ndvi_valid_pixel_ratio_300 IS NOT NULL AND ndvi_valid_pixel_ratio_500 IS NOT NULL AND ndvi_n_scenes_300 IS NOT NULL AND ndvi_n_scenes_500 IS NOT NULL AND builtup_pct_300 IS NOT NULL AND builtup_pct_500 IS NOT NULL) AS complete_rows
FROM ronnai_curated.community_heat;

-- ================================================================ [4] ดูตัวอย่าง
SELECT * FROM ronnai_curated.community_heat ORDER BY community_id LIMIT 10;
