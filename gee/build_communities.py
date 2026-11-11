"""สร้าง communities.csv (schema ของ issue #3) จากไฟล์ "ที่ตั้งชุมชนในพื้นที่เขตกรุงเทพมหานคร" (BMA open data)

ใช้:
  python build_communities.py --src community_bma.csv   # หรือ .json/.geojson --districts หลักสี่ ดอนเมือง --out communities.csv

หมายเหตุ:
- ผู้เขียนยังไม่เห็นหัวคอลัมน์จริงของไฟล์ BMA สคริปต์จึงเดาชื่อคอลัมน์จากรายการด้านล่าง
  ถ้าเดาไม่ได้จะหยุดพร้อมบอกว่าไฟล์มีคอลัมน์อะไร ให้แก้ NAME_COLS / DISTRICT_COLS / LAT_COLS / LON_COLS
- community_id คงที่: ถ้ามี communities.csv เดิมอยู่ จะใช้ id เดิมของ (ชื่อ, เขต) เดิม แล้วออก id ใหม่ต่อท้ายเท่านั้น
"""
import argparse
import json
import os
import sys

import pandas as pd

NAME_COLS = ["name", "community_name", "ชื่อชุมชน", "ชุมชน", "comm_name", "NAME_T", "NAME"]
DISTRICT_COLS = ["district", "dcode", "เขต", "ชื่อเขต", "dname", "DISTRICT", "district_name", "amphoe"]
LAT_COLS = ["__lat", "lat", "latitude", "ละติจูด", "y", "LAT", "Latitude"]
LON_COLS = ["__lon", "lon", "lng", "longitude", "ลองจิจูด", "x", "LON", "Longitude"]

# รหัสเขตของ กทม. (dcode ในไฟล์) -> ชื่อเขต  ตรวจจากชื่อชุมชนในไฟล์: 1041 มี "ชุมชนหลักสี่/ตลาดหลักสี่"
# 1036 = ดอนเมือง ตามรหัสเขตของ กทม. (ยืนยันกับ พจนานุกรมข้อมูล PDF) รหัสอื่นจะแสดงเป็นตัวเลขเดิม
DCODE_NAMES = {"1041": "หลักสี่", "1036": "ดอนเมือง"}

# ขอบเขตคร่าว ๆ ของกรุงเทพฯ ใช้จับพิกัดที่สลับ lat/lon หรือหลุดตำแหน่ง
BKK_LAT = (13.4, 14.1)
BKK_LON = (100.3, 100.95)


def _center(geom):
    """จุดพิกัดจาก geometry: Point ใช้ตรง ๆ; อย่างอื่นใช้กึ่งกลาง bounding box (โดยประมาณ)"""
    if not geom:
        return None, None
    pts = []

    def walk(c):
        if c and isinstance(c[0], (int, float)):
            pts.append(c)
        else:
            for i in c:
                walk(i)
    walk(geom.get("coordinates", []))
    if not pts:
        return None, None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(ys) + max(ys)) / 2, (min(xs) + max(xs)) / 2   # lat, lon


def read_geojson(path):
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    feats = data.get("features") if isinstance(data, dict) else None
    crs = None
    if isinstance(data, dict):
        crs = ((data.get("crs") or {}).get("properties") or {}).get("name")
    if feats is None:   # JSON ธรรมดา (ตารางรายการ ไม่ใช่ GeoJSON)
        return pd.json_normalize(data if isinstance(data, list) else data.get("records", data))
    rows = []
    for ft in feats:
        r = dict(ft.get("properties") or {})
        r["__lat"], r["__lon"] = _center(ft.get("geometry"))
        rows.append(r)
    df = pd.DataFrame(rows)
    df.attrs["crs"] = crs
    return df


def read_any(path):
    if path.lower().endswith((".json", ".geojson")):
        return read_geojson(path)
    for enc in ("utf-8-sig", "utf-8", "cp874", "tis-620"):
        try:
            return pd.read_csv(path, encoding=enc)
        except UnicodeDecodeError:
            continue
    sys.exit(f"อ่านไฟล์ไม่ได้ (encoding): {path}")


def pick(df, candidates, what):
    lower = {str(c).strip().lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    sys.exit(f"หาคอลัมน์ {what} ไม่เจอ ไฟล์มีคอลัมน์: {list(df.columns)}\n-> แก้รายการชื่อคอลัมน์ด้านบนของสคริปต์")


def to_wgs84(out, crs_name):
    """ถ้าพิกัดเป็นเมตร (UTM) แปลงเป็น lat/lon (WGS84); ถ้าเป็นองศาอยู่แล้วไม่ทำอะไร"""
    big = (out["lat"].abs() > 360) | (out["lon"].abs() > 360)
    if not big.any():
        return out
    try:
        from pyproj import Transformer
    except ImportError:
        sys.exit("พิกัดเป็นเมตร (UTM) ต้องแปลงเป็น lat/lon: pip install pyproj แล้วรันใหม่")
    digits = "".join(ch for ch in str(crs_name or "")[-5:] if ch.isdigit())
    epsg = digits if len(digits) == 5 else "32647"
    if not crs_name:
        print("คำเตือน: ไฟล์ไม่ระบุ CRS สมมติว่าเป็น EPSG:32647 (UTM 47N)")
    tr = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    lon, lat = tr.transform(out.loc[big, "lon"].values, out.loc[big, "lat"].values)
    out = out.copy()
    out.loc[big, "lon"], out.loc[big, "lat"] = lon, lat
    print(f"แปลงพิกัด EPSG:{epsg} -> WGS84 {int(big.sum())} แถว")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--districts", nargs="+", required=True, help="ชื่อเขต เช่น หลักสี่ ดอนเมือง")
    ap.add_argument("--out", default="communities.csv")
    ap.add_argument("--prefix", default="C", help="คำนำหน้า community_id")
    ap.add_argument("--source", default="BMA open data: ที่ตั้งชุมชนในพื้นที่เขตกรุงเทพมหานคร")
    a = ap.parse_args()

    df = read_any(a.src)
    c_name, c_dist = pick(df, NAME_COLS, "ชื่อชุมชน"), pick(df, DISTRICT_COLS, "เขต")
    c_lat, c_lon = pick(df, LAT_COLS, "lat"), pick(df, LON_COLS, "lon")

    out = pd.DataFrame({
        "name": df[c_name].astype(str).str.strip(),
        "district": (df[c_dist].astype(str).str.strip().str.replace("^เขต", "", regex=True)
                     .replace(DCODE_NAMES)),
        "lat": pd.to_numeric(df[c_lat], errors="coerce"),
        "lon": pd.to_numeric(df[c_lon], errors="coerce"),
    })
    out = to_wgs84(out, df.attrs.get("crs"))
    out = out[out["district"].isin(a.districts)].copy()

    gaps = []
    # แถวที่ไม่มีพิกัด
    bad = out["lat"].isna() | out["lon"].isna()
    gaps += [(r.name, r.district, "ไม่มีพิกัด") for r in out[bad].itertuples()]
    out = out[~bad]
    # พิกัดสลับ lat/lon -> สลับกลับ; ยังหลุดกรุงเทพฯ -> เข้า data_gaps
    swap = ~out["lat"].between(*BKK_LAT) & out["lon"].between(*BKK_LAT) & out["lat"].between(*BKK_LON)
    out.loc[swap, ["lat", "lon"]] = out.loc[swap, ["lon", "lat"]].values
    outside = ~(out["lat"].between(*BKK_LAT) & out["lon"].between(*BKK_LON))
    gaps += [(r.name, r.district, f"พิกัดนอกกรุงเทพฯ ({r.lat}, {r.lon})") for r in out[outside].itertuples()]
    out = out[~outside]

    # ชื่อ+เขตซ้ำ -> เก็บแถวแรก และจดไว้
    dup = out.duplicated(["name", "district"], keep="first")
    gaps += [(r.name, r.district, "ชื่อซ้ำในเขตเดียวกัน (ตัดออก)") for r in out[dup].itertuples()]
    out = out[~dup]

    # community_id คงที่
    old = pd.read_csv(a.out, encoding="utf-8-sig") if os.path.exists(a.out) else None
    ids = {}
    nxt = 1
    if old is not None and len(old):
        ids = {(r.name, r.district): r.community_id for r in old.itertuples()}
        nums = pd.to_numeric(old["community_id"].str.extract(r"(\d+)$")[0], errors="coerce")
        nxt = int(nums.max()) + 1 if nums.notna().any() else 1
    cid = []
    for r in out.sort_values(["district", "name"]).itertuples():
        key = (r.name, r.district)
        if key not in ids:
            ids[key] = f"{a.prefix}{nxt:04d}"
            nxt += 1
        cid.append((r.Index, ids[key]))
    out["community_id"] = pd.Series(dict(cid))
    out["source"] = a.source
    out = out[["community_id", "name", "district", "lat", "lon", "source"]].sort_values("community_id")

    assert out["community_id"].is_unique
    out.to_csv(a.out, index=False, encoding="utf-8-sig")
    print(f"เขียน {len(out)} ชุมชน -> {a.out}")
    print(out.groupby("district").size().to_string())
    if gaps:
        print(f"\nต้องตรวจเอง {len(gaps)} รายการ (ใส่ใน docs/data_gaps.md):")
        for g in gaps:
            print(" -", *g)


if __name__ == "__main__":
    main()