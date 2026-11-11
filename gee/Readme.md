# gee/ — สคริปต์ดาวเทียม (Issue #4 LST, #5 NDVI, #6 อาคาร)

คำนวณ LST และ NDVI ต่อ **ชุมชน × buffer (300/500 ม.) × ปี** แล้ว export เป็น CSV 3 ไฟล์
เพื่อส่งเข้า ingestion (#7) → validation gate (#9)

## ไฟล์
| ไฟล์ | ใช้ทำอะไร |
|---|---|
| `ronnai_community_zonal.js` | สคริปต์หลักของ M1 (รันใน GEE Code Editor) |
| `laksi_cell_explorer.js` | สคริปต์เดิม: ข้อมูลราย cell 90 ม. + อาคาร ใช้สำรวจ/ทำแผนที่ **ไม่ส่งเข้า pipeline** |
| `communities.csv` | master list ชุมชน (#3) 36 ชุมชน (หลักสี่ 32, ดอนเมือง 4) เป็นอินพุตของสคริปต์ GEE และใช้ตรวจ `community_id` ใน gate (#9) / join ใน #11 |
| `build_communities.py` | สร้าง `communities.csv` จากไฟล์ต้นทางของ BMA |
| `source/bma_community_locations.json` | ไฟล์ต้นทาง (GeoJSON) "ที่ตั้งชุมชนในพื้นที่เขตกรุงเทพมหานคร" จาก BMA open data (data.go.th) พิกัดเป็น UTM 47N ดาวน์โหลดเมื่อ: ___ (ใส่วันที่) |

> ชื่อ/ที่วางของไฟล์ต้นทางให้ตรงกับที่อยู่ใน repo จริง

## สร้าง communities.csv ใหม่ (เมื่อมีชุมชนเพิ่ม/แก้)
```
pip install pandas pyproj
python gee/build_communities.py --src gee/source/bma_community_locations.json \
       --districts หลักสี่ ดอนเมือง --out gee/communities.csv
```
- ชี้ `--out` ไปไฟล์เดิม: `community_id` เดิมจะไม่เปลี่ยน ชุมชนใหม่ได้ id ต่อท้าย
- ไฟล์ต้องเป็น UTF-8 **ไม่มี BOM** ห้ามเปิดแล้วบันทึกใน Excel (BOM ทำให้ GEE ตั้งชื่อ property เป็น `\uFEFFcommunity_id`)
- รายการที่สคริปต์ตัดออกจะพิมพ์ไว้ใน output ให้จดลง `docs/data_gaps.md`

## สิ่งที่ต้องมีก่อนรัน
1. `communities.csv` อัปโหลดเป็น GEE asset (asset อยู่ในบัญชีผู้อัปโหลด ต้องแชร์ให้ทีม หรือให้แต่ละคนอัปโหลดของตัวเองแล้วแก้ `COMM_ASSET`)
   (Assets → New → Table upload, เลือก `lon` = X และ `lat` = Y, encoding UTF-8)
2. แก้ใน `ronnai_community_zonal.js`
   - `COMM_ASSET` = path ของ asset ชุมชน
   - `BUILD_POINT_FROM_LATLON = true` ถ้า asset ไม่มี geometry
   - `YEARS` (M1 ใช้ `[2026]`)
3. ใส่ credit อาจารย์หมูที่หัวไฟล์

## วิธีรัน
1. กด Run แล้วดู Console
   - "คาดว่าได้กี่แถว" ต้องเท่ากับ "LST แถวจริง" (36 ชุมชน × 2 buffer × 1 ปี = 72)
   - ตัวอย่างแถวต้องไม่ว่าง; ค่าที่สมเหตุสมผล: LST ราว 30–45 °C, NDVI −1..1
2. แท็บ **Tasks** → กด RUN ทั้ง 3 งาน (`ronnai_lst_2026`, `ronnai_ndvi_2026`, `ronnai_buildings_2026`)
3. ดาวน์โหลด CSV จาก Google Drive โฟลเดอร์ `GEE_exports`

## ผลลัพธ์
**`lst_2026.csv`** — Landsat 8+9 C2 L2, 1 มี.ค.–31 พ.ค., mask เมฆด้วย `QA_PIXEL`, composite แบบ median

| คอลัมน์ | ความหมาย |
|---|---|
| `community_id, buffer_m, year` | key (ต้องไม่ซ้ำ) |
| `lst_mean`, `lst_p90` | ค่าเฉลี่ย / เปอร์เซ็นไทล์ที่ 90 ของ LST (°C) ใน buffer |
| `valid_pixel_ratio` | สัดส่วน pixel ใน buffer ที่มีภาพไม่ติดเมฆอย่างน้อย 1 ภาพ |
| `n_scenes` | จำนวนภาพที่ทับ buffer (รวมภาพที่เมฆบังมาก) |

**`ndvi_2026.csv`** — Sentinel-2 L2A, ช่วงเวลาเดียวกัน, mask ด้วย `SCL`

| คอลัมน์ | ความหมาย |
|---|---|
| `ndvi_mean` | NDVI เฉลี่ยใน buffer |
| `green_ratio` | สัดส่วน pixel ที่ NDVI > 0.3 (จาก pixel ที่ใช้ได้) |
| `valid_pixel_ratio`, `n_scenes` | เหมือนด้านบน |

**`buildings_2026.csv`** — Google Open Buildings v3 (confidence ≥ 0.7) หรือ polygon อาคารของตัวเอง/OSM (ตั้ง `BUILDINGS_ASSET`)

| คอลัมน์ | ความหมาย |
|---|---|
| `builtup_pct` | พื้นที่หลังคาอาคาร / พื้นที่ buffer × 100 (ราสเตอร์ 5 ม. จึงเป็นค่าประมาณ) |
| `building_count` | จำนวนอาคารที่จุดกึ่งกลางอยู่ใน buffer (นับจากราสเตอร์ 5 ม. ค่าประมาณ; 0 = ไม่มีอาคาร หรือข้อมูลไม่ครอบคลุม ให้ gate WARN; ตั้ง `COMPUTE_BUILDING_COUNT = false` ได้ถ้านับแล้ว error) |

ข้อมูลอาคารเป็นชุดคงที่ (ไม่ใช่ภาพปี 2026) คอลัมน์ `year` คือปีที่วิเคราะห์เพื่อให้ key ตรงกับไฟล์อื่น

## ส่งต่อ
- **ห้ามอัปโหลดเข้า `raw/` เอง และไม่ commit CSV ผลลัพธ์ลง git** (เป็นข้อมูลดิบ `.gitignore` กันไว้)
- ส่งผ่านสคริปต์ ingestion (#7): `source=lst`, `source=ndvi` และ `source=buildings`, `year=2026`
- ถ้า #7 ยังไม่พร้อม ฝากไฟล์ไว้ที่ `incoming/` ชั่วคราว
- ชื่อคอลัมน์ต้องตรงกับ `contracts/lst.yaml`, `contracts/ndvi.yaml` และ `contracts/buildings.yaml` (#8)

## ข้อจำกัดที่ต้องเขียนใน report
- ชุมชนอยู่ใกล้กัน buffer ซ้อนทับ (300 ม.: 42 คู่, 500 ม.: 73 คู่) ค่าแต่ละชุมชนไม่ใช่ตัวอย่างอิสระ
- LST ความละเอียดจริง ~100 ม. (resample เป็น 30 ม.) ชุมชนเล็กได้ pixel น้อย
- หลังคาโลหะอาจทำให้ LST ต่ำกว่าอุณหภูมิจริง
- `n_scenes` นับภาพที่ทับ buffer ไม่ใช่เฉพาะภาพที่ใช้ได้
- Open Buildings อาจนับตกหล่นในชุมชนแออัด/หลังคาเล็ก ทำให้ `builtup_pct` ต่ำกว่าจริง
- สคริปต์ยังไม่ได้ทดสอบรันใน GEE ตอนเขียน ให้ตรวจ Console ก่อน export
