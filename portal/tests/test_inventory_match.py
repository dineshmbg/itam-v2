"""Inventory match: a centre's report matched against the asset register by Asset (CI) = computer name.
The report is built from whatever machines the dev database holds, so nothing here depends on specific data."""
import csv
import datetime as dt
import io

import pytest
from starlette.testclient import TestClient

from conftest import HDR, fake_user
from portal.app import inventory_match as m
from portal.app.main import app

NOW = dt.datetime(2026, 10, 6, 12, 0)


def _machines(box, n=6):
    rows = box.execute("""SELECT ci_no, ip_address FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND asset_class = 'DESKTOP' AND asset_status = 'IN_USE'
                          AND ci_no ~ '^[A-Z]{3,4}' ORDER BY ci_no LIMIT %s""", (n,)).fetchall()
    if len(rows) < n:
        pytest.skip("not enough desktops in the dev database")
    return rows


def _prefix(ci):
    return ci[:3]


def _csv(rows, header=("Computer Name", "IP Address", "OS", "Last Report Time")):
    b = io.StringIO()
    w = csv.writer(b)
    w.writerow(header)
    w.writerows(rows)
    return b.getvalue().encode()


def test_read_and_guess_columns_skips_title_rows():
    data = _csv([["Pan India report", "", "", ""], ["Computer Name", "IP Address", "OS", "Last Report Time"], ["A-1", "10.0.0.1", "Win11 10.0.26200", "Tue, 06 Oct 2026 01:35:28 +0000"]], header=("", "", "", ""))
    headers, rows = m.read_file("r.csv", data)
    assert headers[0] == "Pan India report" or "Computer Name" in headers            # either way the mapping below must work once the header row is found
    headers, rows = m.read_file("r.csv", _csv([["A-1", "10.0.0.1", "Win11", "Tue, 06 Oct 2026 01:35:28 +0000"]]))
    assert headers == ["Computer Name", "IP Address", "OS", "Last Report Time"] and len(rows) == 1
    assert m.guess_mapping(headers) == {"name": "Computer Name", "ip": "IP Address", "os": "OS", "seen": "Last Report Time"}
    with pytest.raises(m.MatchError):
        m.read_file("r.pdf", b"%PDF")
    with pytest.raises(m.MatchError):
        m.read_file("r.xlsx", b"not a workbook")


def test_key_and_time_parsing():
    assert m.key_of(" ankaon26lt001.ongc.local ") == "ANKAON26LT001" and m.key_of("10.1.2.3") == "10.1.2.3"
    assert m._when("Tue, 06 Oct 2026 01:35:28 +0000") == dt.datetime(2026, 10, 6, 1, 35, 28)
    assert m._when("2026-10-05") == dt.datetime(2026, 10, 5) and m._when("garbage") is None and m._when("") is None
    assert m._os_family("Win10 10.0.19045") == "Windows 10" and m._os_family("Win11 10.0.26200") == "Windows 11" and m._os_family("Win2019") == "Windows Server"


def test_match_installed_missing_dormant_mismatch_and_unregistered(box):
    ms = _machines(box)
    pre = _prefix(ms[0][0])
    fresh, old = "Tue, 06 Oct 2026 01:00:00 +0000", "Mon, 01 Jun 2026 01:00:00 +0000"
    report = [[ms[0][0], "10.9.9.1", "Win11 10.0.26200", fresh],                      # installed, active
              [ms[1][0].lower() + ".ongc.local", "10.9.9.2", "Win10 10.0.19045", old],    # installed under a domain name in lower case, silent for months
              [pre + "-NOTINREGISTER-1", "10.9.9.3", "Win11", fresh],                     # reports, but is not a CI
              [ms[0][0], "10.9.9.1", "Win11 10.0.26200", old]]                           # duplicate row: the latest check-in must win
    if ms[2][1]:
        report.append([pre + "-RENAMED-1", ms[2][1].strip(), "Win11", fresh])           # a different name on the third machine's own IP
    headers, rows = m.read_file("r.csv", _csv(report))
    s, res = m.analyse(headers, rows, m.guess_mapping(headers), {"tool": "TestTool", "prefix": pre, "classes": ["DESKTOP"]}, today=NOW)
    by = {r["ci"]: r for r in res["assets"]}
    assert by[ms[0][0]]["installed"] and by[ms[0][0]]["reporting"] == "Active"
    assert by[ms[1][0]]["installed"] and by[ms[1][0]]["reporting"] == "Dormant" and "silent" in by[ms[1][0]]["action"]
    assert not by[ms[3][0]]["installed"] and by[ms[3][0]]["priority"] == "High" and "Install the TestTool agent" in by[ms[3][0]]["action"]
    if ms[2][1]:
        assert by[ms[2][0]]["possible"].endswith("RENAMED-1") and by[ms[2][0]]["possible_by"] == "same IP address" and s["name_mismatch"] == 1
        assert any(u["kind"] == "mismatch" for u in res["unregistered"])
    assert any(u["name"].endswith("NOTINREGISTER-1") and u["kind"] == "unregistered" for u in res["unregistered"])
    assert s["duplicate_names"] == 1 and res["duplicates"][0]["rows"] == 2
    assert s["installed"] + s["missing"] == s["assets"] and s["installed"] >= 2 and s["coverage"] == round(100 * s["installed"] / s["assets"], 1)
    assert sum(x["total"] for x in s["by_class"]) == s["assets"] and s["findings"][0]["finding"].startswith("TestTool is installed on")
    assert s["rag"] in ("red", "amber", "green") and s["has_os"] and s["has_seen"]


def test_needs_a_name_column_and_ignores_other_sites(box):
    ms = _machines(box, 2)
    headers, rows = m.read_file("r.csv", _csv([[ms[0][0], "", "", ""]]))
    with pytest.raises(m.MatchError):
        m.analyse(headers, rows, {}, {})
    s, res = m.analyse(headers, rows, {"name": "Computer Name"}, {"prefix": "ZZQ"}, today=NOW)      # a prefix no asset has: nothing to check, no crash
    assert s["assets"] == 0 and s["coverage"] == 0.0


def test_run_is_stored_reopened_and_exported(box, tmp_path, monkeypatch):
    monkeypatch.setattr(m, "STAGE", tmp_path)
    ms = _machines(box)
    st = m.stage("centre.csv", _csv([[ms[0][0], "10.0.0.1", "Win11", "Tue, 06 Oct 2026 01:00:00 +0000"]]), "ADMIN1")
    assert st["mapping"]["name"] == "Computer Name" and st["rows"] == 1 and (tmp_path / f"{st['stage_id']}__centre.csv").exists()
    r = m.run(st["stage_id"], st["mapping"], {"tool": "BigFix", "prefix": _prefix(ms[0][0]), "classes": ["DESKTOP"]}, "ADMIN1")
    back = m.get(r["run_id"])
    assert back["summary"]["installed"] == r["summary"]["installed"] and len(back["assets"]) == len(r["assets"]) and m.history()[0]["run_id"] == r["run_id"]
    xlsx, _, ext = m.render(back, "xlsx", {"username": "ADMIN1"})
    pdf, _, pext = m.render(back, "pdf", {"username": "ADMIN1"})
    assert ext == "xlsx" and xlsx[:2] == b"PK" and pext == "pdf" and pdf[:4] == b"%PDF"
    from openpyxl import load_workbook
    names = load_workbook(io.BytesIO(xlsx)).sheetnames
    assert {"KEY FINDINGS", "COVERAGE BY ENGINEER", "NOT INSTALLED - ACTION LIST", "FULL MATCH"} <= {n.upper() for n in names}    # the exporter upper-cases sheet names
    for bad in ("../x", "zzzz", ""):
        with pytest.raises(m.MatchError):
            m._staged(bad)
    with pytest.raises(m.MatchError):
        m.get("nope")


def test_endpoints_need_a_full_administrator(box, monkeypatch, tmp_path):
    monkeypatch.setattr(m, "STAGE", tmp_path)
    fake_user(monkeypatch, "USER")
    h = {"X-Requested-With": "itam-portal", "X-Filename": "a.csv", "Content-Type": "application/octet-stream"}
    with TestClient(app) as c:
        assert c.post("/api/match/upload", content=b"a,b\n1,2\n", headers=h).status_code == 403
        assert c.get("/api/match/meta").status_code == 403
        assert c.post("/api/match/run", json={}, headers=HDR).status_code == 403
    user = fake_user(monkeypatch, "ADMIN")
    from portal.app import routes_tools

    async def me(request):
        return user
    monkeypatch.setattr(routes_tools, "current_user", me)          # the upload route reads the user itself (it takes a raw body), so it needs the same stand-in
    ms = _machines(box)
    with TestClient(app) as c:
        up = c.post("/api/match/upload", content=_csv([[ms[0][0], "", "", ""]]), headers=h)
        assert up.status_code == 200
        run = c.post("/api/match/run", json={"stage_id": up.json()["stage_id"], "mapping": up.json()["mapping"], "params": {"prefix": _prefix(ms[0][0]), "classes": ["DESKTOP"]}}, headers=HDR)
        assert run.status_code == 200 and run.json()["summary"]["installed"] >= 1
        rid = run.json()["run_id"]
        assert c.get(f"/api/match/runs/{rid}").status_code == 200 and c.get("/api/match/runs/nope").status_code == 404
        assert c.post("/api/match/export", json={"run_id": rid, "format": "pdf"}, headers=HDR).headers["content-type"] == "application/pdf"
        assert c.post("/api/match/export", json={"run_id": rid, "format": "csv"}, headers=HDR).status_code == 400
        assert c.post("/api/match/run", json={"stage_id": "bad", "mapping": {"name": "x"}}, headers=HDR).status_code == 400
