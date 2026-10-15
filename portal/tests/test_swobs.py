"""Software/OS observations: inspection date is server-stamped and unforgeable, rows are append-only, latest per source, stale flags. Rolled back (conftest.box)."""
import datetime as dt

import psycopg
import pytest

from portal.app import swobs


def test_inspected_at_is_stamped_by_the_server(box):
    before = dt.datetime.now(dt.timezone.utc)
    oid = swobs.record("A1", "BIGFIX_REPORT", {"os_build": "10.0.26200.9445"}, by="TEST")
    row = box.execute("SELECT inspected_at FROM portal_sw_obs WHERE obs_id = %s", (oid,)).fetchone()
    assert row[0] >= before - dt.timedelta(seconds=5)
    with pytest.raises(TypeError):
        swobs.record("A1", "MANUAL", {}, by="TEST", inspected_at=dt.datetime(2020, 1, 1))
    with pytest.raises(swobs.ObsError):
        swobs.record("A1", "MANUAL", {"inspected_at": "2020-01-01"}, by="TEST")


def test_rejects_unknown_source_and_field(box):
    with pytest.raises(swobs.ObsError):
        swobs.record("A1", "GUESS", {}, by="TEST")
    with pytest.raises(swobs.ObsError):
        swobs.record("A1", "MANUAL", {"colour": "red"}, by="TEST")


def test_rows_cannot_be_updated_or_deleted(box):
    oid = swobs.record("A1", "MANUAL", {"vpn_name": "Zscaler"}, by="TEST")
    for sql in ("UPDATE portal_sw_obs SET vpn_name = 'x' WHERE obs_id = %s", "DELETE FROM portal_sw_obs WHERE obs_id = %s"):
        with pytest.raises(psycopg.errors.RaiseException):
            with box.transaction():
                box.execute(sql, (oid,))


def test_latest_per_source_side_by_side_with_stale_flags(box):
    swobs.record("A2", "BIGFIX_REPORT", {"os_build": "10.0.19045.1"}, by="T")
    new = swobs.record("A2", "BIGFIX_REPORT", {"os_build": "10.0.19045.2"}, by="T", data_as_of=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=45))
    swobs.record("A2", "TREND_REPORT", {"os_build": "19045"}, by="T", data_as_of=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=100))
    got = {r["source"]: r for r in swobs.latest_by_source("A2")}
    assert set(got) == {"BIGFIX_REPORT", "TREND_REPORT"}
    assert got["BIGFIX_REPORT"]["obs_id"] == new and got["BIGFIX_REPORT"]["os_build"] == "10.0.19045.2"
    assert got["BIGFIX_REPORT"]["stale"] == "STALE" and got["TREND_REPORT"]["stale"] == "VERY_STALE"
    swobs.record("A2", "MANUAL", {}, by="T")
    assert {r["source"]: r for r in swobs.latest_by_source("A2")}["MANUAL"]["stale"] is None


# ---------------------------------------------------------------- report ingest (task 2)
TODAY = dt.date.today()


def _asset(con, key, cls="DESKTOP"):
    con.execute("""INSERT INTO asset (snapshot_date, asset_key, ci_no, asset_class, asset_type, record_level, asset_status, is_current, first_seen_date, last_seen_date)
                   VALUES (%s,%s,%s,%s,%s,'ASSET','IN_USE',1,%s,%s)""", (TODAY, key, key, cls, cls, TODAY, TODAY))


BIGFIX = ("Computer Name,Device Type,IP Address,OS,CPU,Last Report Time\n"
          "ANKZZ-PC1,Desktop,10.0.0.1,Win11 10.0.26200.9445 (25H2),i7,\"Wed, 14 Oct 2026 21:16:35 +0000\"\n"
          "ankzz-pc2.ongc.local,Desktop,10.0.0.2,Win10 10.0.19045.6456 (22H2),i5,\"Thu, 15 Oct 2026 02:14:30 +0000\"\n"
          "ANKNOPE-9,Desktop,10.0.0.3,Win11 10.0.26200.1 (25H2),i5,\"Thu, 15 Oct 2026 02:14:30 +0000\"\n").encode()
TREND = ("Endpoint name,Object type,Protection Manager,Last agent status reported,Agent runtime protection status,Endpoint GUID,Agent connection status,XDR for Endpoints (EDR),Endpoint group,OS name,OS version,Last logged on user,Agent version status\n"
         "ANKZZ-PC1,Endpoint,Standard,Last 24 hours (2026-10-06 10:39:40),Normal,g1,Communicating,Enabled,G,Windows 11,10.0 (Build 26200),ONGC\\68047,controlledLatestVersion\n"
         "ANKZZ-SRV1,Endpoint,Standard,Last 24 hours (2026-10-06 10:39:40),Normal,g2,Not communicating,Enabled,G,Windows Server 2019,10.0 (Build 17763),ONGC\\141662,olderVersion\n").encode()


def test_bigfix_ingest_matches_our_assets_and_stores_full_build(box):
    _asset(box, "ANKZZ-PC1"), _asset(box, "ANKZZ-PC2")
    s = swobs.ingest_report("bigfix.csv", BIGFIX, by="TEST")
    assert (s["product"], s["rows"], s["matched"], s["unmatched"], s["recorded"]) == ("bigfix", 3, 2, 1, 2) and s["unmatched_names"] == ["ANKNOPE-9"] and s["ignored_other_sites"] == 0
    a = {r["source"]: r for r in swobs.latest_by_source("ANKZZ-PC1")}["BIGFIX_REPORT"]
    assert (a["os_edition"], a["os_build"], a["os_feature_version"]) == ("Windows 11", "10.0.26200.9445", "25H2")
    assert a["data_as_of"].astimezone(dt.timezone.utc).isoformat().startswith("2026-10-14T21:16:35")
    assert {r["source"]: r for r in swobs.latest_by_source("ANKZZ-PC2")}["BIGFIX_REPORT"]["os_edition"] == "Windows 10"
    assert not box.execute("SELECT 1 FROM portal_sw_obs WHERE asset_key = 'ANKNOPE-9'").fetchone()


def test_reimport_of_identical_report_adds_nothing_but_new_data_adds_rows(box):
    _asset(box, "ANKZZ-PC1")
    assert swobs.ingest_report("b.csv", BIGFIX, by="T")["recorded"] == 1
    again = swobs.ingest_report("b.csv", BIGFIX, by="T")
    assert (again["recorded"], again["unchanged"]) == (0, 1)
    newer = BIGFIX.replace(b"14 Oct 2026 21:16:35", b"15 Oct 2026 21:16:35")
    assert swobs.ingest_report("b.csv", newer, by="T")["recorded"] == 1
    assert box.execute("SELECT count(*) FROM portal_sw_obs WHERE asset_key = 'ANKZZ-PC1'").fetchone()[0] == 2


def test_trend_ingest_keeps_status_user_and_server_scope(box):
    _asset(box, "ANKZZ-PC1"), _asset(box, "ANKZZ-SRV1", "SERVER"), _asset(box, "ANKZZ-PR1", "PRINTER")
    s = swobs.ingest_report("trend.csv", TREND, by="T")
    assert (s["product"], s["matched"], s["recorded"]) == ("trendmicro", 2, 2)
    t = {r["source"]: r for r in swobs.latest_by_source("ANKZZ-SRV1")}["TREND_REPORT"]
    assert (t["trend_installed"], t["trend_status"], t["connection"], t["last_user"], t["os_build"]) == (True, "olderVersion", "Not communicating", "ONGC\\141662", "17763")
    assert t["os_edition"] == "Windows Server 2019"


def test_unrecognised_file_is_refused(box):
    with pytest.raises(swobs.ObsError):
        swobs.ingest_report("x.csv", b"a,b\n1,2\n", by="T")


def test_names_not_starting_with_ank_are_discarded_not_reported(box):
    _asset(box, "ANKZZ-PC1")
    other = BIGFIX + b"AAAA-INTWS103,Desktop,10.0.0.9,Win11 10.0.26200.9445 (25H2),i7,\"Wed, 14 Oct 2026 21:16:35 +0000\"\n"
    s = swobs.ingest_report("b.csv", other, by="T")
    assert (s["rows"], s["ignored_other_sites"], s["unmatched_names"]) == (4, 1, ["ankzz-pc2.ongc.local", "ANKNOPE-9"])


# ---------------------------------------------------------------- record view (task 3)
def test_asset_record_carries_software_panel_only_for_in_scope_classes(box):
    from portal.app import queries
    _asset(box, "ANKZZ-PC1"), _asset(box, "ANKZZ-PR1", "PRINTER")
    swobs.record("ANKZZ-PC1", "BIGFIX_REPORT", {"os_edition": "Windows 10", "os_build": "10.0.19045.6456"}, by="T")
    swobs.record("ANKZZ-PC1", "TREND_REPORT", {"os_edition": "Windows 11", "os_build": "19045", "last_user": "ONGC\68047"}, by="T")
    swobs.record("ANKZZ-PR1", "MANUAL", {"remarks": "x"}, by="T")
    sw = queries.detail("assets", "ANKZZ-PC1", user={"role": "ADMIN"})["related"]["software"]
    assert {r["source"] for r in sw["latest"]} == {"BIGFIX_REPORT", "TREND_REPORT"} and len(sw["history"]) == 2
    big = next(r for r in sw["latest"] if r["source"] == "BIGFIX_REPORT")
    assert "Windows 10 is past end of support" in big["flags"] and big["inspected_at"]
    assert [c["field"] for c in sw["conflicts"]] == ["Windows edition"]            # builds agree (19045 both); editions differ
    assert queries.detail("assets", "ANKZZ-PR1", user={"role": "ADMIN"})["related"]["software"] is None


# ---------------------------------------------------------------- inventory match integration (task 4)
def _ank(box, n=3):
    from test_inventory_match import _machines
    rows = [r[0] for r in _machines(box, 40) if r[0].startswith("ANK")]
    if len(rows) < n:
        pytest.skip("not enough ANK desktops in the dev database")
    return rows[:n]


def test_match_cross_checks_report_against_script_and_manual_observations(box):
    from test_inventory_match import TM, _go, _tm
    a, b, c = _ank(box)
    swobs.record(a, "SCRIPT", {"trend_installed": False}, by="T")           # report will list a, the machine says no agent
    swobs.record(b, "MANUAL", {"trend_installed": True}, by="T")            # report will not list b, an engineer saw the agent
    summary, res = _go([(TM, [_tm(a), _tm(c)])], {"prefix": "ANK"})
    by = {r["ci"]: r for r in res["assets"]}
    assert "not installed" in by[a]["xcheck"] and "Script" in by[a]["xcheck"]
    assert "installed" in by[b]["xcheck"] and "Entered by hand" in by[b]["xcheck"]
    assert "xcheck" not in by[c] and "observed" not in by[c]                  # no observation: the row is exactly as before
    assert summary["xcheck"] == 2


def test_match_without_observations_adds_nothing(box):
    from test_inventory_match import TM, _go, _tm
    a, *_ = _ank(box, 1)
    summary, res = _go([(TM, [_tm(a)])], {"prefix": "ANK"})
    assert "xcheck" not in summary and all("xcheck" not in r for r in res["assets"])


def test_running_a_match_records_the_report_as_observations(box):
    from test_inventory_match import TM, _csv, _tm
    from portal.app import inventory_match as m
    a, *_ = _ank(box, 1)
    st = m.stage("trend.csv", _csv([_tm(a)], TM), "ADMIN1")
    r = m.run(st["stage_id"], st["mapping"], {"prefix": "ANK"}, "ADMIN1")
    assert r["summary"]["observations"]["recorded"] == 1
    assert {x["source"] for x in swobs.latest_by_source(a)} == {"TREND_REPORT"}


# ---------------------------------------------------------------- check script (task 5)
CHECK = """ANKCHECK=1
COMPUTER=ankzz-pc1
WIN_PRODUCT=Windows 10 Pro
WIN_BUILD=26200
WIN_UBR=9445
WIN_DISPLAY=25H2
WIN_INSTALLTYPE=Client
TREND_NAME=Trend Micro Apex One Security Agent
TREND_VER=14.0.12345
BIGFIX_SVC=1
BIGFIX_NAME=BigFix Client
BIGFIX_VER=11.0.2.130
ZSCALER_NAME=Zscaler
ZSCALER_VER=4.3.0.1
VPN_OTHER=
SAP_VER=8.00 PL 5
O365_ACCOUNT=68047@ongc.co.in
OFFICE_VER=16.0.19029.20136
LAST_USER=ONGC\68047
DOMAIN=ongc.co.in
EDGE_VER=141.0.3537.71
SECUREBOOT=1
RUN_AT_RAW=15-10-2026 20:11:09.55
"""


def test_parse_check_derives_the_facts():
    c = swobs.parse_check(CHECK)
    f = c["facts"]
    assert c["computer"] == "ANKZZ-PC1"
    assert (f["os_edition"], f["os_build"], f["os_feature_version"]) == ("Windows 11", "10.0.26200.9445", "25H2")       # build >= 22000 is Windows 11 whatever ProductName says
    assert (f["trend_installed"], f["trend_version"], f["bigfix_installed"], f["bigfix_version"]) == (True, "14.0.12345", True, "11.0.2.130")
    assert (f["vpn_name"], f["vpn_version"], f["vpn_other"], f["sap_gui_version"], f["o365_account"], f["last_user"], f["domain_joined"]) == ("Zscaler", "4.3.0.1", False, "8.00 PL 5", "68047@ongc.co.in", "ONGC\68047", True)


def test_parse_check_server_other_vpn_and_nothing_found():
    c = swobs.parse_check("ANKCHECK=1\nCOMPUTER=ANKSRV1\nWIN_PRODUCT=Windows Server 2019 Standard\nWIN_BUILD=17763\nWIN_UBR=1\nWIN_INSTALLTYPE=Server\nVPN_OTHER=FortiClient\n")
    f = c["facts"]
    assert f["os_edition"] == "Windows Server 2019" and f["trend_installed"] is False and f["bigfix_installed"] is False
    assert f["vpn_other"] is True and f["vpn_name"] == "FortiClient (not Zscaler)" and "FortiClient" in f["remarks"]


def test_parse_check_rejects_junk():
    for bad in ("hello", "ANKCHECK=1\n", "ANKCHECK=1\nCOMPUTER=" + "A" * 80, "x" * 100000):
        with pytest.raises(swobs.ObsError):
            swobs.parse_check(bad)


def test_ingest_check_records_a_script_observation_for_our_assets_only(box):
    _asset(box, "ANKZZ-PC1"), _asset(box, "ANKZZ-PR1", "PRINTER")
    r = swobs.ingest_check(CHECK, by="check script")
    assert (r["matched"], r["recorded"]) == (True, 1)
    s = {x["source"]: x for x in swobs.latest_by_source("ANKZZ-PC1")}["SCRIPT"]
    assert s["trend_version"] == "14.0.12345" and s["recorded_by"] == "check script" and "PC clock" in s["remarks"]
    assert swobs.ingest_check(CHECK.replace("ankzz-pc1", "ankzz-pr1"), by="x")["matched"] is False      # printer: out of scope
    assert swobs.ingest_check(CHECK.replace("ankzz-pc1", "ZZZ-OTHER"), by="x")["matched"] is False


def test_check_token_is_stable_rotatable_and_constant_time_checked(box):
    t1 = swobs.check_token(create=True, by="ADMIN")
    assert len(t1) >= 30 and swobs.check_token() == t1 and swobs.token_ok(t1) and not swobs.token_ok("nope") and not swobs.token_ok("")
    t2 = swobs.check_token(create=True, by="ADMIN", rotate=True)
    assert t2 != t1 and not swobs.token_ok(t1) and swobs.token_ok(t2)


def test_script_text_has_the_address_and_token_and_windows_line_endings():
    s = swobs.script_text("https://10.205.64.25", "TOKEN123", r"\srv\share\drop")
    assert 'set "URL=https://10.205.64.25"' in s and "/api/software/check" in s and "TOKEN123" in s and r"\srv\share\drop" in s
    assert s.count("\r\n") > 40 and "\n" not in s.replace("\r\n", "")
    assert "powershell" not in s.lower().replace("no powershell", "")


def test_check_endpoints(box, monkeypatch):
    from starlette.testclient import TestClient
    from conftest import HDR, fake_user
    from portal.app.main import app
    _asset(box, "ANKZZ-PC1")
    tok = swobs.check_token(create=True, by="T")
    c = TestClient(app)
    assert c.post("/api/software/check", content=CHECK).status_code == 403
    assert c.post("/api/software/check", content=CHECK, headers={"X-Check-Token": "wrong"}).status_code == 403
    ok = c.post("/api/software/check", content=CHECK, headers={"X-Check-Token": tok})
    assert ok.status_code == 200 and ok.json()["recorded"] == 1
    assert c.post("/api/software/check", content=CHECK.replace("ankzz-pc1", "ANKNONE"), headers={"X-Check-Token": tok}).status_code == 422
    assert c.post("/api/software/check", content=b"x" * 30000, headers={"X-Check-Token": tok}).status_code == 413
    assert c.post("/api/software/check", content="junk", headers={"X-Check-Token": tok}).status_code == 400
    from portal.app import routes_swobs
    u = fake_user(monkeypatch, role="ADMIN", username="ADM")

    async def me(request):                       # the upload route reads the user itself (raw body), so it needs the same stand-in
        return u
    monkeypatch.setattr(routes_swobs, "current_user", me)
    up = c.post("/api/software/check-upload", content=CHECK, headers={"X-Requested-With": "itam-portal"})
    assert up.status_code == 200 and up.json()["matched"] is True
    dl = c.post("/api/software/script", json={}, headers=HDR)
    assert dl.status_code == 200 and tok in dl.text and "attachment" in dl.headers["content-disposition"]
    u = fake_user(monkeypatch, role="USER", username="U1")
    assert c.post("/api/software/script", json={}, headers=HDR).status_code == 403
    assert c.post("/api/software/check-upload", content=CHECK, headers={"X-Requested-With": "itam-portal"}).status_code == 403
