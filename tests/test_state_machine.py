"""ตรวจโครง infra/state_machine.asl.json (Issue #18) โดยไม่ต้องใช้ AWS

- JSON ถูกต้อง, Next/Default ทุกตัวชี้ไป state ที่มีจริง, ทุก state เดินถึงได้จาก StartAt
- Task ทั้ง 5 ชี้ไป Lambda ที่ infra/ronnai_aws.sh deploy สร้าง (FUNCS) ตามลำดับ check -> ingest -> validate -> transform -> verify
- ค่า status ที่ Choice ใช้ ต้องเป็นค่าที่ Lambda คืนจริง (ดูจากโค้ดใน function/)
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASL = ROOT / "infra" / "state_machine.asl.json"
DEPLOY = ROOT / "infra" / "ronnai_aws.sh"


def load():
    return json.loads(ASL.read_text(encoding="utf-8"))


def targets(state):
    out = [state.get("Next"), state.get("Default")]
    out += [c["Next"] for c in state.get("Choices", [])]
    out += [c["Next"] for c in state.get("Catch", [])]
    return [t for t in out if t]


def test_all_targets_exist_and_reachable():
    d = load()
    states = d["States"]
    assert d["StartAt"] in states
    for name, st in states.items():
        for t in targets(st):
            assert t in states, f"{name} -> {t} ไม่มี state นี้"
    seen, todo = set(), [d["StartAt"]]
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            todo += targets(states[n])
    assert seen == set(states), f"state ที่เดินไปไม่ถึง: {set(states) - seen}"


def test_tasks_call_deployed_lambdas_in_order():
    d = load()
    funcs = re.findall(r'^\s+"([a-z-]+):[a-z_]+_function:', DEPLOY.read_text(encoding="utf-8"), re.M)
    assert funcs[0] == "start-pipeline"                                     # ตัวเริ่ม execution (ไม่อยู่ใน state machine)
    funcs = funcs[1:]
    assert funcs == ["check-batch", "ingest-batch", "validate-batch", "transform-batch", "verify-batch"]
    order, n = [], d["StartAt"]
    while n:
        st = d["States"][n]
        if st["Type"] == "Task":
            order.append(st["Resource"].rsplit(":function:", 1)[1].replace("${NAME_PREFIX}-", ""))
            n = st["Next"]
        elif st["Type"] == "Choice":
            n = st["Choices"][0]["Next"]                                  # ทางหลัก (happy path)
        else:
            n = None
    assert order == funcs


def test_tasks_retry_and_catch():
    for name, st in load()["States"].items():
        if st["Type"] == "Task":
            assert st.get("Retry"), f"{name} ไม่มี Retry"
            assert any("States.ALL" in c["ErrorEquals"] for c in st.get("Catch", [])), f"{name} ไม่มี Catch"


def test_choice_values_are_real_lambda_statuses():
    src = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "function").glob("*/lambda_function.py"))
    src += (ROOT / "ingestion" / "validate_gate.py").read_text(encoding="utf-8")   # PASSED/PARTIAL มาจาก gate
    for name, st in load()["States"].items():
        for c in st.get("Choices", []):
            assert f'"{c["StringEquals"]}"' in src, f"{name}: ไม่มี Lambda ไหนคืน status {c['StringEquals']}"


def test_placeholders_are_only_the_ones_deploy_fills():
    assert set(re.findall(r"\$\{([A-Z_]+)\}", ASL.read_text(encoding="utf-8"))) == {"REGION", "ACCOUNT_ID", "NAME_PREFIX"}


def test_start_function_execution_name():
    import importlib.util
    spec = importlib.util.spec_from_file_location("start_fn", ROOT / "function" / "start_function" / "lambda_function.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    name = m.execution_name("incoming/lst_2026.csv", "0b1c2d3e-aaaa-bbbb-cccc-1234567890ab")
    assert name == "lst_2026-csv-0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"
    assert len(m.execution_name("incoming/" + "x" * 200 + ".csv", "e" * 36)) <= 80
