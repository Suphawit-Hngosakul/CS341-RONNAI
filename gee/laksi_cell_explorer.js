// ============================================================
// เขตหลักสี่: data layer ราย cell (LST, NDVI, NDBI, อาคาร, ถนน)
// กดแผนที่ -> เห็นค่าของ cell นั้น | Export เป็นตาราง + Shapefile
// ============================================================

// ---------- 0. ข้อมูลนำเข้า ----------
var BASE = 'projects/turnkey-tribute-511903-t6/assets/';
var laksi = ee.FeatureCollection(BASE + 'laksi_boundary');
var bld   = ee.FeatureCollection(BASE + 'building');   // อัปโหลด building.shp เป็น asset ก่อน
var roi   = laksi.geometry();

// ถนน: ยังไม่มีไฟล์ -> ใส่ asset ID เมื่อมี (เช่น BASE + 'roads')
var ROAD_ASSET   = null;
var ROAD_IS_LINE = true;   // true = เส้นกลางถนน | false = polygon ผิวถนน
var ROAD_WIDTH_M = 8;      // ความกว้างสมมติ (ใช้เมื่อเป็นเส้น) ควรแทนด้วยค่าจริง

// ---------- 1. ตั้งค่า ----------
var CELL = 90;                 // ขนาด cell (เมตร) ควรเป็นพหุคูณของ 30
var CRS  = 'EPSG:32647';       // UTM 47N
var START_YEAR = 2021, END_YEAR = 2025;
var SUMMER_ONLY = true;        // true = มี.ค.-พ.ค.

Map.centerObject(roi, 13);

// ---------- 2. Landsat 8+9: ลบเมฆ, LST, NDVI, NDBI ----------
function prep(img) {
  var qa = img.select('QA_PIXEL');
  var clear = qa.bitwiseAnd(30).eq(0);          // bit 1-4: dilated cloud, cirrus, cloud, shadow
  var lst = img.select('ST_B10').multiply(0.00341802).add(149.0)
    .subtract(273.15).rename('lst_c');
  var sr = img.select(['SR_B4', 'SR_B5', 'SR_B6']).multiply(0.0000275).add(-0.2);
  var red = sr.select('SR_B4'), nir = sr.select('SR_B5'), swir = sr.select('SR_B6');
  var ndvi = nir.subtract(red).divide(nir.add(red)).rename('ndvi');
  var ndbi = swir.subtract(nir).divide(swir.add(nir)).rename('ndbi');
  return lst.addBands([ndvi, ndbi]).updateMask(clear)
    .copyProperties(img, ['system:time_start']);
}

var raw = ee.ImageCollection('LANDSAT/LC08/C02/T1_L2')
  .merge(ee.ImageCollection('LANDSAT/LC09/C02/T1_L2'))
  .filterBounds(roi)
  .filterDate(START_YEAR + '-01-01', (END_YEAR + 1) + '-01-01');
if (SUMMER_ONLY) raw = raw.filter(ee.Filter.calendarRange(3, 5, 'month'));

var col = raw.map(prep);
var comp = col.median()
  .addBands(col.select('lst_c').count().rename('n_valid'))
  .clip(roi);   // ตัดเฉพาะเขตหลักสี่ ทั้งการแสดงผลและการคำนวณ
print('จำนวนภาพที่ใช้', col.size());

// ---------- 3. สร้าง grid ให้ตรงกับ pixel ของ Landsat ----------
var tr = raw.first().select('ST_B10').projection().getInfo().transform; // [30,0,x0,0,-30,y0]
var bb = ee.List(roi.bounds(1, CRS).coordinates().get(0)).getInfo();
var xs = bb.map(function(p) { return p[0]; });
var ys = bb.map(function(p) { return p[1]; });
var xmin = Math.min.apply(null, xs), xmax = Math.max.apply(null, xs);
var ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys);
print('ขอบเขต UTM (ควรอยู่ราว x 66xxxx-67xxxx, y 153xxxx)', xmin, xmax, ymin, ymax);

var gx0 = tr[2] + Math.floor((xmin - tr[2]) / CELL) * CELL;
var gy0 = tr[5] + Math.floor((ymin - tr[5]) / CELL) * CELL;

var feats = [];
for (var x = gx0, i = 0; x < xmax; x += CELL, i++) {
  for (var y = gy0, j = 0; y < ymax; y += CELL, j++) {
    feats.push(ee.Feature(
      ee.Geometry.Rectangle([x, y, x + CELL, y + CELL], CRS, false),
      {cell_id: 'c' + i + '_' + j}));
  }
}
var cells = ee.FeatureCollection(feats)
  .filterBounds(roi)
  .map(function(c) {   // เก็บเฉพาะ cell ที่จุดกึ่งกลางอยู่ในเขตหลักสี่
    var inside = roi.contains(c.geometry().centroid(1), 1);
    return c.set('inside', ee.Number(ee.Algorithms.If(inside, 1, 0)));
  })
  .filter(ee.Filter.eq('inside', 1))
  .select(['cell_id']);
print('จำนวน cell', cells.size());

// ---------- 4. Layer อาคาร / ถนน (rasterize ที่ 3 m แล้วเฉลี่ยในแต่ละ cell) ----------
function mask(fc, name) {
  return ee.Image().byte().paint(fc, 1).unmask(0).rename(name);
}
var VEC_SCALE = 5;   // m  (ต้องหาร CELL ลงตัว) ใหญ่ขึ้น = เร็วขึ้น แต่หยาบขึ้น
var vecBands = [mask(bld, 'bldg_r')];
// สัดส่วนพื้นที่อาคารแยกตามรหัสวัสดุ BL_MATL (ความหมายรหัสต้องยืนยันกับผู้ให้ข้อมูล)
[0, 1, 2, 3, 8, 9].forEach(function(c) {
  vecBands.push(mask(bld.filter(ee.Filter.eq('BL_MATL', c)), 'mat' + c + '_r'));
});

// พื้นที่ที่ "น่าจะมีข้อมูลอาคาร" = footprint buffer 100 m (ไม่ต้อง union จึงเบา)
// ใช้แยก cell ที่ไม่มีอาคารจริง ออกจาก cell ที่ไม่มีข้อมูล
var bldBuf = bld.map(function(f) { return f.buffer(100, 5); });
vecBands.push(mask(bldBuf, 'cov_r'));

if (ROAD_ASSET) {
  var roads = ee.FeatureCollection(ROAD_ASSET);
  if (ROAD_IS_LINE) {
    roads = roads.map(function(f) { return f.buffer(ROAD_WIDTH_M / 2); });
  }
  vecBands.push(mask(roads, 'road_r'));
}

// จุด centroid ของอาคาร -> ราสเตอร์ผลรวม (จำนวน, ชั้น, GFA) แทนการกรองรายเซลล์
var bpts = bld.map(function(f) {
  var fl = ee.Number(f.get('BL_NSTOREY'));
  var a  = ee.Number(f.get('BL_AREA'));
  var ok = fl.gt(0);
  return ee.Feature(f.geometry().centroid(1), {
    one: 1, flpos: ok, flsum: fl.multiply(ok), gfa: a.multiply(fl.multiply(ok))});
});
['one', 'flpos', 'flsum', 'gfa'].forEach(function(p) {
  vecBands.push(bpts.reduceToImage([p], ee.Reducer.sum()).unmask(0).rename(p));
});
var vecStack = ee.Image.cat(vecBands);

// ---------- 5. ตารางค่าราย cell ----------
var NPIX = (CELL / VEC_SCALE) * (CELL / VEC_SCALE);   // จำนวนพิกเซลต่อ cell

function cellTable(fc) {
  var a = comp.reduceRegions({
    collection: fc, reducer: ee.Reducer.mean(), scale: 30, crs: CRS});
  var b = vecStack.reduceRegions({
    collection: a, reducer: ee.Reducer.mean(), scale: VEC_SCALE, crs: CRS});
  return b.map(function(c) {
    var n   = ee.Number(c.get('one')).multiply(NPIX);      // ผลรวมจุดอาคาร
    var nfl = ee.Number(c.get('flpos')).multiply(NPIX);
    var sfl = ee.Number(c.get('flsum')).multiply(NPIX);
    var gfa = ee.Number(c.get('gfa')).multiply(NPIX);
    return c.set({
      n_bldg: n.round(),
      floors: ee.Algorithms.If(nfl.gt(0.5), sfl.divide(nfl), null),
      far: gfa.divide(CELL * CELL),
      in_cover: ee.Number(ee.Algorithms.If(ee.Number(c.get('cov_r')).gte(0.5), 1, 0))
    });
  });
}

// ---------- 6. แสดงผล ----------
var visLST  = {min: 30, max: 46, palette: ['2c7bb6', 'abd9e9', 'ffffbf', 'fdae61', 'd7191c']};
var visNDVI = {min: 0, max: 0.7, palette: ['8c510a', 'f6e8c3', 'a6d96a', '1a9850']};
Map.addLayer(comp.select('lst_c'), visLST, 'LST (°C)');
Map.addLayer(comp.select('ndvi'), visNDVI, 'NDVI', false);
Map.addLayer(comp.select('ndbi'), {min: -0.3, max: 0.3, palette: ['green', 'white', 'brown']}, 'NDBI', false);
Map.addLayer(bld.filterBounds(roi).style({color: '444444', fillColor: '44444455', width: 1}), {}, 'อาคาร (ดูพื้นที่ครอบคลุม)');
Map.addLayer(cells.style({color: 'ffffff', fillColor: '00000000', width: 1}), {}, 'grid ' + CELL + ' m');
Map.addLayer(laksi.style({color: 'blue', fillColor: '00000000'}), {}, 'เขตหลักสี่');

// ---------- 7. กดแผนที่ -> เห็นค่าของ cell ----------
var panel = ui.Panel({style: {position: 'bottom-left', padding: '8px', width: '300px'}});
Map.add(panel);
var selLayer = null;

function fmt(v, d) {
  return (v === null || v === undefined) ? '–' : Number(v).toFixed(d);
}
function line(k, v) { panel.add(ui.Label(k + ': ' + v, {margin: '1px 0'})); }

Map.onClick(function(c) {
  panel.clear();
  panel.add(ui.Label('กำลังคำนวณ...'));
  var pt = ee.Geometry.Point([c.lon, c.lat]);
  var cell = cells.filterBounds(pt).limit(1);

  if (selLayer) Map.layers().remove(selLayer);
  selLayer = ui.Map.Layer(cell.style({color: 'yellow', fillColor: '00000000', width: 3}),
                          {}, 'cell ที่เลือก');
  Map.layers().add(selLayer);

  cellTable(cell).first().evaluate(function(f) {
    panel.clear();
    if (!f) { panel.add(ui.Label('คลิกนอกพื้นที่เขต')); return; }
    var p = f.properties;
    panel.add(ui.Label('Cell ' + p.cell_id, {fontWeight: 'bold'}));
    line('LST (°C)', fmt(p.lst_c, 1));
    line('NDVI', fmt(p.ndvi, 2));
    line('NDBI', fmt(p.ndbi, 2));
    line('จำนวนภาพที่ใช้ (เฉลี่ย)', fmt(p.n_valid, 1));
    panel.add(ui.Label(p.in_cover ? '— อาคาร —' : '— อาคาร (นอกพื้นที่ที่มีข้อมูล ค่า 0 ไม่น่าเชื่อถือ) —',
                       {fontWeight: 'bold'}));
    line('จำนวนอาคาร', p.n_bldg);
    line('สัดส่วนพื้นที่อาคาร', fmt(p.bldg_r * 100, 1) + ' %');
    line('จำนวนชั้นเฉลี่ย', fmt(p.floors, 1));
    line('FAR (พื้นที่ใช้สอย/พื้นที่ cell)', fmt(p.far, 2));
    line('วัสดุ (รหัส 1/2/3/8/9 % ของ cell)',
         [p.mat1_r, p.mat2_r, p.mat3_r, p.mat8_r, p.mat9_r]
           .map(function(v) { return fmt(v * 100, 0); }).join(' / '));
    line('สัดส่วนถนน', p.road_r === undefined ? 'ยังไม่มีข้อมูลถนน' : fmt(p.road_r * 100, 1) + ' %');
  });
});

// ---------- 8. Export (กด RUN ในแท็บ Tasks) ----------
// ส่งออกงานเดียว (Shapefile) พอ: ไฟล์ .dbf เปิดเป็นตารางได้ และ QGIS export เป็น CSV ต่อได้
var keep = ['cell_id', 'lst_c', 'ndvi', 'ndbi', 'n_valid',
  'n_bldg', 'bldg_r', 'floors', 'far', 'in_cover',
  'mat0_r', 'mat1_r', 'mat2_r', 'mat3_r', 'mat8_r', 'mat9_r'];
if (ROAD_ASSET) keep.push('road_r');
var table = cellTable(cells).select(keep);

Export.table.toDrive({
  collection: table, description: 'Laksi_cells_' + CELL + 'm_v2',
  folder: 'GEE_exports', fileFormat: 'SHP'});
