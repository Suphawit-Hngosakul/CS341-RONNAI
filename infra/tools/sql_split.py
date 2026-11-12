"""แยกไฟล์ SQL เป็นไฟล์ละ 1 statement ให้ infra/ronnai_aws.sh ส่งเข้า Athena ทีละอัน (Athena รับครั้งละ statement เดียว)

  python sql_split.py statements <file.sql> <outdir> [--keep CREATE MSCK] [--suffix _view]
      ตัดบรรทัดคอมเมนต์ (--) แล้วแยกด้วย ; เก็บเฉพาะ statement ที่ขึ้นต้นด้วยคำใน --keep
      เขียน <outdir>/01<suffix>.sql, 02<suffix>.sql, ...  พิมพ์จำนวนที่เขียน
  python sql_split.py block <file.sql> <n> <outfile>
      ดึงบล็อก `-- ==== [n] ...` (รูปแบบของ sql/curated_merge.sql) เขียนเป็น statement เดียว

เขียนไฟล์แบบ LF เสมอ (บน Windows ถ้าเขียนเป็น CRLF ข้อความที่สคริปต์แสดงจะเพี้ยน)
"""
import argparse
import re
import sys
from pathlib import Path

BLOCK_RE = re.compile(r"^-- =+ \[(\d+)\]", re.M)


def statements(text, keep):
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    out = []
    for st in code.split(";"):
        st = st.strip()
        if st and st.split(None, 1)[0].upper() in keep:
            out.append(st)
    return out


def block(text, n):
    text = text.replace("\r\n", "\n")
    marks = list(BLOCK_RE.finditer(text))
    for i, m in enumerate(marks):
        if int(m[1]) == n:
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            body = text[m.end():end].split("\n", 1)
            return body[1].strip().rstrip(";").strip() if len(body) > 1 else ""
    raise SystemExit(f"ไม่พบบล็อก [{n}]")


def write(path, text):
    Path(path).write_text(text + "\n", encoding="utf-8", newline="\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("statements")
    s.add_argument("file")
    s.add_argument("outdir")
    s.add_argument("--keep", nargs="+", default=["CREATE"])
    s.add_argument("--suffix", default="")
    b = sub.add_parser("block")
    b.add_argument("file")
    b.add_argument("n", type=int)
    b.add_argument("outfile")
    a = ap.parse_args(argv)

    text = Path(a.file).read_text(encoding="utf-8")
    if a.cmd == "block":
        write(a.outfile, block(text, a.n))
        return 0
    sts = statements(text, {k.upper() for k in a.keep})
    Path(a.outdir).mkdir(parents=True, exist_ok=True)
    for i, st in enumerate(sts, 1):
        write(Path(a.outdir, f"{i:02d}{a.suffix}.sql"), st)
    print(len(sts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
