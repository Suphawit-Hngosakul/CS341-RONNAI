"""infra/tools/sql_split.py: แยก SQL ให้ ronnai_aws.sh ส่ง Athena ทีละ statement"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("sql_split", ROOT / "infra" / "tools" / "sql_split.py")
ss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ss)


def test_glue_trusted_keeps_create_and_msck_only():
    sts = ss.statements((ROOT / "sql" / "glue_trusted.sql").read_text(encoding="utf-8"), {"CREATE", "MSCK"})
    assert [s.split(None, 1)[0].upper() for s in sts] == ["CREATE"] * 3 + ["MSCK"] * 3


def test_curated_block_1_is_create_table():
    body = ss.block((ROOT / "sql" / "curated_merge.sql").read_text(encoding="utf-8"), 1)
    assert body.upper().startswith("CREATE TABLE") and not body.endswith(";")


def test_output_is_lf_even_from_crlf_input(tmp_path):
    src = tmp_path / "x.sql"
    src.write_bytes(b"-- comment\r\nCREATE VIEW a AS\r\nSELECT 1;\r\nSELECT 2;\r\n")
    assert ss.main(["statements", str(src), str(tmp_path / "out"), "--suffix", "_view"]) == 0
    data = (tmp_path / "out" / "01_view.sql").read_bytes()
    assert b"\r" not in data and data.startswith(b"CREATE VIEW a AS")
    assert not (tmp_path / "out" / "02_view.sql").exists()
