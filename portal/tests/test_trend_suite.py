"""Sensor only / Full suite, the investigation report and the field report. Pure functions over synthetic rows - no database needed."""
from portal.app import inventory_match as m

U = {"username": "tester"}


def _row(ci, eng, installed, suite="", pc="", health=None):
    return {"ci": ci, "class": "DESKTOP", "type": "", "make": "HP", "model": "EliteDesk", "serial": "S1", "status": "IN_USE", "user": "JOHN DOE", "location": "ANK-A1", "engineer": eng,
            "installed": installed, "health": health or ("Healthy" if installed else "Not installed"), "issues": "", "advice": "", "suite": suite, "suite_pc": pc, "r_seen": None,
            "priority": "" if installed else "High", "action": "", "possible": "", "possible_by": "", "r_age": 1}


D = {"run_id": "x", "filename": "f.xlsx", "summary": {"tool": "Trend Micro Vision One", "as_of": "2026-10-24", "target": 95, "findings": []},
     "assets": [_row("ANK1", "P", False), _row("ANK2", "P", True, "Sensor only"), _row("ANK3", "P", True, "Full suite"), _row("ANK4", "S", True, "", "Full suite")]}


def test_suite_from_console_columns():
    assert m._suite("Enabled", "Enabled") == "Full suite"
    assert m._suite("Disabled", "Enabled") == "Full suite"          # switched off is still the full agent; it is reported as a fault elsewhere
    assert m._suite("-", "Enabled") == "Sensor only"
    assert m._suite("Not installed", "Disabled") == "Unclear"


def test_state_and_todo_in_plain_words():
    by = {r["ci"]: r for r in m._with_state(D["assets"])}
    assert by["ANK1"]["state"] == "Not installed" and by["ANK1"]["todo"].startswith("Install")
    assert by["ANK2"]["state"] == "Sensor only" and "no anti-malware" in by["ANK2"]["todo"]
    assert by["ANK3"]["todo"] == "Nothing to do."
    assert by["ANK4"]["state"] == "Full suite"                      # falls back to what the check script saw on the PC


def test_field_report_is_scoped_and_sorted():
    csv_, mime, ext = m.field_report(D, "csv", U, "P")
    lines = csv_.decode().splitlines()
    assert ext == "csv" and len(lines) == 3 and "ANK4" not in csv_.decode() and lines[1].split(",")[0] == "ANK1"      # only P's two jobs, not-installed first
    assert len(m.field_report(D, "xlsx", U, "P")[0]) > 1000


def test_investigation_for_one_engineer():
    for fmt in ("xlsx", "csv", "pdf"):
        assert m.investigation(D, fmt, U, "P")[0]
