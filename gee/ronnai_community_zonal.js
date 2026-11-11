// ============================================================
// RONNAI · M1 · Issue #4 (LST) + #5 (NDVI) + #6 (อาคาร)
// Zonal stats ต่อ "ชุมชน × buffer × ปี" -> CSV 3 ไฟล์ (lst, ndvi, buildings) ส่งเข้า ingestion #7
//
// credit: อ้างอิง/ดัดแปลงจากโค้ดของอาจารย์หมู  <-- ใส่ชื่อ/ลิงก์จริงตรงนี้
// logic ลบเมฆ + แปลง LST ต่อยอดจากสคริปต์ราย cell เขตหลักสี่ (ไฟล์เดิม)
//
// ใช้ได้ 2 แบบ: (ก) วางเป็นสคริปต์แยกใน gee/  (ข) วางต่อท้ายไฟล์เดิม
// ============================================================

// ---------- 0. ตั้งค่า ----------
var BASE = 'projects/turnkey-tribute-511903-t6/assets/';
var COMM_ASSET   = BASE + 'communities';  // asset ชุมชนจาก #3 (แก้ให้ตรงชื่อจริง)
var COMM_ID_PROP = 'community_id';        // ชื่อ property ของ community_id
// true = asset เป็นตาราง CSV ที่ไม่มี geometry (มีแค่คอลัมน์ lat, lon)
var BUILD_POINT_FROM_LATLON = false;

var BUFFERS     = [300, 500];  // เมตร
var YEARS       = [2026];      // M1 ใช้ปีเดียว; เพิ่มปีได้ เช่น [2024, 2025, 2026]
var MONTH_START = 3;           // มี.ค.
var MONTH_END   = 5;           // พ.ค. (รวมทั้งเดือน)
var CRS         = 'EPSG:32647';
var GREEN_NDVI  = 0.3;         // เกณฑ์ green_ratio (#5)

// อาคาร (#6): ค่าเริ่มต้น = Google Open Buildings v3 (ครอบคลุมทุกชุมชน, ข้อมูลคงที่ ไม่ใช่ปี YEARS)
// ถ้าจะใช้ OSM/ไฟล์ polygon อาคารของตัวเอง ให้อัปโหลดเป็น asset แล้วใส่ ID ตรงนี้ (ไม่ใช้ตัวกรอง confidence)
var BUILDINGS_ASSET = null;     // เช่น BASE + 'osm_buildings'
var BLDG_CONF_MIN   = 0.7;      // กรอง confidence ≥ 0.7 (เฉพาะ Open Buildings)
var BLDG_SCALE      = 5;        // เมตร: ความละเอียดตอนแปลงอาคารเป็นราสเตอร์
var COMPUTE_BUILDING_COUNT = true;  // ถ้ายัง internal error ให้ลองตั้ง false (ข้ามการนับอาคาร เหลือแค่ builtup_pct)

// ---------- 1. ชุมชน (ต้องมี community_id) ----------
var comm = ee.FeatureCollection(COMM_ASSET);
// กัน CSV ที่มี BOM: ชื่อ property จะเป็น '\uFEFFcommunity_id' (ตาไม่เห็นความต่าง) ทำให้ community_id ว่างทั้งไฟล์
var ID_KEY = comm.first().propertyNames().getInfo().filter(function (p) {
  return p.replace(/^\uFEFF/, '') === COMM_ID_PROP;
})[0];
if (!ID_KEY) { throw new Error('ไม่พบ property ' + COMM_ID_PROP + ' ใน ' + COMM_ASSET); }
if (BUILD_POINT_FROM_LATLON) {
  comm = comm.map(function (f) {
    return ee.Feature(
      ee.Geometry.Point([ee.Number(f.get('lon')), ee.Number(f.get('lat'))]),
      {community_id: f.get(ID_KEY)});
  });
} else {
  comm = comm.map(function (f) {
    return ee.Feature(f.geometry(), {community_id: f.get(ID_KEY)});
  });
}
var commGeom = comm.geometry();
Map.centerObject(commGeom, 11);
print('จำนวนชุมชน', comm.size());
print('community_id ไม่ว่าง (ต้องเท่ากับจำนวนชุมชน)', comm.filter(ee.Filter.notNull(['community_id'])).size());

// ---------- 2. ตัวช่วยร่วม ----------
function seasonStart(year) { return ee.Date.fromYMD(year, MONTH_START, 1); }
function seasonEnd(year)   { return ee.Date.fromYMD(year, MONTH_END + 1, 1); }  // end exclusive

// สร้างโซน buffer ต่อชุมชน พร้อม key และ n_scenes (จำนวนภาพที่ทับ buffer นั้น)
function zones(bufferM, year, rawCol) {
  return comm.map(function (f) {
    var g = f.geometry().buffer(bufferM, 1);
    return ee.Feature(g, {
      community_id: f.get('community_id'),
      buffer_m: bufferM,
      year: year,
      n_scenes: rawCol.filterBounds(g).size()
    });
  });
}

// ---------- 3. #4 LST: Landsat 8+9 C2 L2 ----------
function lstMasked(img) {
  var clear = img.select('QA_PIXEL').bitwiseAnd(30).eq(0);  // bit1-4: dilated cloud, cirrus, cloud, shadow
  return img.select('ST_B10').multiply(0.00341802).add(149.0).subtract(273.15)
    .rename('lst_c').updateMask(clear);
}

function lstTable(year, bufferM) {
  var raw = ee.ImageCollection('LANDSAT/LC08/C02/T1_L2')
    .merge(ee.ImageCollection('LANDSAT/LC09/C02/T1_L2'))
    .filterDate(seasonStart(year), seasonEnd(year))
    .filterBounds(commGeom);
  var col   = raw.map(lstMasked);
  var lst   = col.median().rename('lst_c');
  // valid = pixel นี้มีภาพที่ไม่ถูกเมฆบังอย่างน้อย 1 ภาพใน composite
  var valid = col.count().gt(0).unmask(0).rename('valid');

  var fc = zones(bufferM, year, raw);
  var a = ee.Image.cat([lst, valid]).reduceRegions({
    collection: fc, reducer: ee.Reducer.mean(), scale: 30, crs: CRS, tileScale: 4});   // props: lst_c, valid
  var b = lst.reduceRegions({
    collection: a, reducer: ee.Reducer.percentile([90]), scale: 30, crs: CRS, tileScale: 4}); // prop: p90
  return b.map(function (f) {
    return f.set({lst_mean: f.get('lst_c'), lst_p90: f.get('p90'),
                  valid_pixel_ratio: f.get('valid')});
  });
}

// ---------- 4. #5 NDVI: Sentinel-2 L2A (SCL mask) ----------
function ndviMasked(img) {
  var scl = img.select('SCL');
  // ตัด: no data(0) saturated(1) cloud shadow(3) cloud med/high(8,9) cirrus(10) snow(11)
  var clear = scl.neq(0).and(scl.neq(1)).and(scl.neq(3))
    .and(scl.neq(8)).and(scl.neq(9)).and(scl.neq(10)).and(scl.neq(11));
  return img.normalizedDifference(['B8', 'B4']).rename('ndvi').updateMask(clear);
}

function ndviTable(year, bufferM) {
  var raw = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
    .filterDate(seasonStart(year), seasonEnd(year))
    .filterBounds(commGeom);
  var col   = raw.map(ndviMasked);
  var ndvi  = col.median().rename('ndvi');
  var green = ndvi.gt(GREEN_NDVI).rename('green');          // mask ตาม ndvi -> เฉลี่ยเฉพาะ pixel ที่ใช้ได้
  var valid = col.count().gt(0).unmask(0).rename('valid');

  var fc = zones(bufferM, year, raw);
  var a = ee.Image.cat([ndvi, green, valid]).reduceRegions({
    collection: fc, reducer: ee.Reducer.mean(), scale: 10, crs: CRS, tileScale: 4});    // props: ndvi, green, valid
  return a.map(function (f) {
    return f.set({ndvi_mean: f.get('ndvi'), green_ratio: f.get('green'),
                  valid_pixel_ratio: f.get('valid')});
  });
}

// ---------- 4b. #6 ความหนาแน่นอาคาร ----------
// กรองด้วยพื้นที่ "รอบชุมชน" (buffer ใหญ่สุด) ไม่ใช่แค่จุดชุมชน ไม่งั้นได้เฉพาะอาคารที่ทับจุดพอดี
var bldRegion = commGeom.buffer(Math.max.apply(null, BUFFERS) + 100, 10);

function buildingsFC() {
  if (BUILDINGS_ASSET) {
    return ee.FeatureCollection(BUILDINGS_ASSET).filterBounds(bldRegion);
  }
  return ee.FeatureCollection('GOOGLE/Research/open-buildings/v3/polygons')
    .filterBounds(bldRegion)
    .filter(ee.Filter.gte('confidence', BLDG_CONF_MIN));
}

function bldgTable(year, bufferM) {
  var bld = buildingsFC();
  // ทุกอย่างทำผ่านราสเตอร์ + reduceRegions (ไม่ซ้อน filterBounds ในลูป เพราะ collection อาคารใหญ่ ~4 หมื่นหลัง)
  // builtup_pct = พื้นที่หลังคาอาคาร / พื้นที่ buffer (ตัดตามขอบ buffer จริง)
  var built = ee.Image().byte().paint(bld, 1).unmask(0).rename('built');
  var fc = comm.map(function (f) {
    return ee.Feature(f.geometry().buffer(bufferM, 1),
      {community_id: f.get('community_id'), buffer_m: bufferM, year: year});
  });
  var r = built.reduceRegions({
    collection: fc, reducer: ee.Reducer.mean(), scale: BLDG_SCALE, crs: CRS, tileScale: 8});  // ภาพแบนด์เดียว -> property 'mean'
  r = r.map(function (f) {
    return f.set('builtup_pct', ee.Number(f.get('mean')).multiply(100));
  });
  if (!COMPUTE_BUILDING_COUNT) { return r; }
  // building_count = จำนวนจุดกึ่งกลางอาคารใน buffer (นับจากราสเตอร์ของจุด)
  var cent = bld.map(function (b) { return ee.Feature(b.geometry().centroid(1), {n: 1}); });
  var cnt = cent.reduceToImage(['n'], ee.Reducer.sum()).unmask(0).rename('cnt');
  var c = cnt.reduceRegions({
    collection: r, reducer: ee.Reducer.sum(), scale: BLDG_SCALE, crs: CRS, tileScale: 8});   // property 'sum'
  return c.map(function (f) {
    return f.set('building_count', ee.Number(f.get('sum')).round());
  });
}

// ---------- 5. รวมทุกปี × buffer ----------
var lstAll = ee.FeatureCollection([]);
var ndviAll = ee.FeatureCollection([]);
var bldgAll = ee.FeatureCollection([]);
YEARS.forEach(function (y) {
  BUFFERS.forEach(function (b) {
    lstAll  = lstAll.merge(lstTable(y, b));
    ndviAll = ndviAll.merge(ndviTable(y, b));
    bldgAll = bldgAll.merge(bldgTable(y, b));
  });
});

var LST_COLS  = ['community_id', 'buffer_m', 'year', 'lst_mean', 'lst_p90',
                 'valid_pixel_ratio', 'n_scenes'];
var NDVI_COLS = ['community_id', 'buffer_m', 'year', 'ndvi_mean', 'green_ratio',
                 'valid_pixel_ratio', 'n_scenes'];
var BLDG_COLS = ['community_id', 'buffer_m', 'year', 'builtup_pct', 'building_count'];

// ---------- 6. ตรวจเบื้องต้น (ก่อนกด RUN export) ----------
print('คาดว่าได้กี่แถว = ชุมชน × buffer × ปี', comm.size().multiply(BUFFERS.length * YEARS.length));
print('LST แถวจริง', lstAll.size());
print('ตัวอย่าง LST', lstAll.limit(5));
print('ตัวอย่าง NDVI', ndviAll.limit(5));
print('จำนวนอาคารในพื้นที่ศึกษา (ต้องเป็นหลักพัน-หมื่น ไม่ใช่ 0)', buildingsFC().size());
print('ตัวอย่างอาคาร (buffer แรก 3 แถว)', bldgTable(YEARS[0], BUFFERS[0]).limit(3));

Map.addLayer(comm.style({color: 'red', pointSize: 3}), {}, 'ชุมชน');
Map.addLayer(comm.map(function (f) {
  return ee.Feature(f.geometry().buffer(BUFFERS[BUFFERS.length - 1], 1));
}).style({color: 'orange', fillColor: '00000000'}), {}, 'buffer ใหญ่สุด');

// ---------- 7. Export CSV (กด RUN ในแท็บ Tasks) ----------
var tag = YEARS.join('_');
Export.table.toDrive({
  collection: lstAll, description: 'ronnai_lst_' + tag, folder: 'GEE_exports',
  fileNamePrefix: 'lst_' + tag, fileFormat: 'CSV', selectors: LST_COLS});
Export.table.toDrive({
  collection: ndviAll, description: 'ronnai_ndvi_' + tag, folder: 'GEE_exports',
  fileNamePrefix: 'ndvi_' + tag, fileFormat: 'CSV', selectors: NDVI_COLS});
Export.table.toDrive({
  collection: bldgAll, description: 'ronnai_buildings_' + tag, folder: 'GEE_exports',
  fileNamePrefix: 'buildings_' + tag, fileFormat: 'CSV', selectors: BLDG_COLS});
