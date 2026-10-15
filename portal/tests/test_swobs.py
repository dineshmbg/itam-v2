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
