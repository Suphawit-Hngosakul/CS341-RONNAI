-- ============================================================
-- Q1: ชุมชนเขียวน้อย ตึกแน่น ร้อนกว่าไหม
-- ที่มา : ronnai_curated.community_heat (1 แถว/ชุมชน, buffer แยกเป็นคอลัมน์)
-- ใช้ buffer 300 และ 500 m (ทำแยกกัน ไม่ปนกัน)
-- รันทีละ statement ใน Athena (workgroup ronnai-wg, database ronnai_curated)
--   [1] view v_q1_base         : ดึงค่าที่ใช้ จัดเป็นแถวละ (ชุมชน, buffer)
--   [2] view v_q1_heat_vs_green: ตาราง 4 กลุ่ม เทียบ LST เฉลี่ย
--   [3] view v_q1_correlation  : corr(lst, ndvi) และ corr(lst, builtup)
--   [4] SELECT ผลลัพธ์ (จด "Data scanned" หลังรัน)
-- นิยามกลุ่ม: แบ่งด้วย median ของ NDVI และ builtup_pct "ภายในแต่ละ buffer/ปี"
--   ndvi_group    = green_high ถ้า ndvi_mean   >= median, ไม่งั้น green_low
--   builtup_group = built_high ถ้า builtup_pct >= median, ไม่งั้น built_low
--   (ค่าที่เท่ากับ median นับเป็นกลุ่ม high; median ใช้ approx_percentile(x, 0.5))
-- ============================================================

-- [1] ------------------------------------------------------
CREATE OR REPLACE VIEW ronnai_curated.v_q1_base AS
SELECT community_id, year, 300 AS buffer_m,
       lst_mean_300 AS lst_mean, ndvi_mean_300 AS ndvi_mean, builtup_pct_300 AS builtup_pct
FROM ronnai_curated.community_heat
UNION ALL
SELECT community_id, year, 500 AS buffer_m,
       lst_mean_500 AS lst_mean, ndvi_mean_500 AS ndvi_mean, builtup_pct_500 AS builtup_pct
FROM ronnai_curated.community_heat;

-- [2] ------------------------------------------------------
CREATE OR REPLACE VIEW ronnai_curated.v_q1_heat_vs_green AS
WITH base AS (
  SELECT * FROM ronnai_curated.v_q1_base
  WHERE lst_mean IS NOT NULL AND ndvi_mean IS NOT NULL AND builtup_pct IS NOT NULL
),
med AS (
  SELECT buffer_m, year,
         approx_percentile(ndvi_mean, 0.5)   AS ndvi_median,
         approx_percentile(builtup_pct, 0.5) AS builtup_median
  FROM base
  GROUP BY buffer_m, year
),
grp AS (
  SELECT b.buffer_m, b.year, b.community_id, b.lst_mean, b.ndvi_mean, b.builtup_pct,
         CASE WHEN b.ndvi_mean   >= m.ndvi_median    THEN 'green_high' ELSE 'green_low' END AS ndvi_group,
         CASE WHEN b.builtup_pct >= m.builtup_median THEN 'built_high' ELSE 'built_low' END AS builtup_group
  FROM base b
  JOIN med m ON b.buffer_m = m.buffer_m AND b.year = m.year
)
SELECT buffer_m, year, ndvi_group, builtup_group,
       count(*)                   AS n_communities,
       round(avg(lst_mean), 3)    AS lst_mean_avg,
       round(avg(ndvi_mean), 4)   AS ndvi_mean_avg,
       round(avg(builtup_pct), 2) AS builtup_pct_avg
FROM grp
GROUP BY buffer_m, year, ndvi_group, builtup_group;

-- [3] ------------------------------------------------------
CREATE OR REPLACE VIEW ronnai_curated.v_q1_correlation AS
SELECT buffer_m, year,
       count(*)                          AS n_communities,
       round(corr(lst_mean, ndvi_mean), 4)   AS corr_lst_ndvi,      -- คาดว่าติดลบ
       round(corr(lst_mean, builtup_pct), 4) AS corr_lst_builtup    -- คาดว่าเป็นบวก
FROM ronnai_curated.v_q1_base
WHERE lst_mean IS NOT NULL AND ndvi_mean IS NOT NULL AND builtup_pct IS NOT NULL
GROUP BY buffer_m, year;

-- [4] ผลลัพธ์ (รันทีละอัน แล้วดู "Data scanned" ใต้ผล) -----------
SELECT * FROM ronnai_curated.v_q1_heat_vs_green ORDER BY buffer_m, ndvi_group DESC, builtup_group DESC;
SELECT * FROM ronnai_curated.v_q1_correlation ORDER BY buffer_m;
