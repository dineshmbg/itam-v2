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


def test_apex_files_read_as_one_product(tmp_path):
    csv_ = b"Domain,Endpoint,IP Address,Connection Status,Platform,Architecture,Logon User\nWorkgroup_ank\\,ANKAA1,10.1.1.1,Offline,Windows 11,x64,ONGC\\123\n"
    from openpyxl import Workbook
    import io
    wb = Workbook()
    ws = wb.active
    ws.append(["Endpoint name", "Object type", "Protection Manager", "OS name", "OS type"])
    ws.append(["ANKBB2$", "Desktop", "Active Directory (on-premises)", "Windows 11 Pro 10.0 (26200)", "Windows"])
    ws2 = wb.create_sheet("Ankleshwar")
    ws2.append(["Endpoint name", "Object type", "Protection Manager", "OS name", "OS type"])
    ws2.append(["ANKBB2$", "Desktop", "Active Directory (on-premises)", "Windows 11 Pro 10.0 (26200)", "Windows"])
    buf = io.BytesIO()
    wb.save(buf)
    h1, r1 = m.read_file("list.csv", csv_)
    h2, r2 = m.read_file("old.xlsx", buf.getvalue())
    assert m.profile_file(h1)["product"] == "apex" and m.profile_file(h2)["product"] == "apex"
    assert len(r2) == 2 and m.key_of(r2[0][0]) == "ANKBB2"          # same-heading sheets are joined; the trailing $ is not part of the name
    col = {"name": 0, "ip": h1.index("IP address"), "os": h1.index("OS name"), "conn": h1.index("Connection status"), "user": h1.index("Last logged on user")}
    e = m._entry(r1[0], col, "apex")
    assert e["advice"] and e["user"].endswith("123") and "suite" not in e


def test_consolidated_report():
    d = {**D, "unregistered": [{"name": "ANKZZ9", "ip": "", "os": "", "files": "a.csv", "why": "x", "action": "Add it", "kind": "unregistered"}], "duplicates": [{"name": "ANK2", "rows": 2, "detail": ""}]}
    for r in d["assets"]:
        r.update(r_name=r["ci"], r_files="a.csv", r_conn="Online", r_ip="", r_os="")
    merged, absent = m._merged_rows(d)
    assert [r["state"] for r in merged] == ["Sensor only", "Not in register", "Full suite", "Full suite"] and [r["endpoint"] for r in absent] == ["ANK1"]
    assert "ANKZZ9" not in m.consolidated(d, "csv", U, "P")[0].decode()                                  # an engineer never sees other people's rows
    for fmt in ("xlsx", "csv", "pdf"):
        assert m.consolidated(d, fmt, U)[0] and m.consolidated(d, fmt, U, "P")[0]
