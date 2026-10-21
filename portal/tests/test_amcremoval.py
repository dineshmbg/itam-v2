"""Remove from AMC. Every test runs in one rolled-back transaction on synthetic assets (TST-*): real data is never touched."""
import datetime as dt

import pytest
from starlette.testclient import TestClient

from portal.app import amcremoval, dashboards, pm
from portal.app.edit import Conflict, Invalid
from portal.app.main import app
from tests.conftest import HDR, fake_user
from tests.test_lifecycle import make_asset, n, one, rows

ED, IP = "TEST EDITOR", "127.0.0.1"
Q = pm.quarter(dt.date.today())


@pytest.fixture()
def fleet(box):
    """Three whole assets of different classes under AMC, plus one laptop and one warranty asset (both ineligible)."""
    box.execute("INSERT INTO portal_user (username, display_name, role, password_hash) VALUES ('TST-APPROVER','TST APPROVER','USER','x') ON CONFLICT DO NOTHING")
    make_asset(box, "TST-D-1", rate_value=1500)
    make_asset(box, "TST-S-1", asset_class="SWITCH", asset_type="CORE SWITCH", rate_value=9000)
    make_asset(box, "TST-U-1", asset_class="UPS", asset_type="UPS", rate_value=400)
    make_asset(box, "TST-L-1", asset_class="LAPTOP", asset_type="LAPTOP")
    make_asset(box, "TST-W-1", cover_type="WRTY")
    return box


def go(keys, **body):
    return amcremoval.remove(keys, {"reason": "SURPLUS", "approved_by": "TST-APPROVER", **body}, ED, IP)


def test_removes_one_asset_by_archiving_it_with_a_snapshot_and_audit(fleet):
    r = go(["TST-D-1"], note="verbal OK from SI")
    assert r["assets"] == ["TST-D-1"] and r["removal_no"].startswith("AMCR-") and r["removal_no"].endswith("-001")
    a = one(fleet, "SELECT * FROM asset WHERE asset_key = 'TST-D-1'")
    assert (a["is_current"], a["asset_status"], a["rate_value"]) == (0, "REMOVED_FROM_AMC", None)      # archived, rate cleared, row still there
    assert n(fleet, "SELECT count(*) FROM portal_lock WHERE dataset='assets' AND record_key='TST-D-1' AND field='__archived__'") == 1
    line = one(fleet, "SELECT * FROM amc_removal_line WHERE asset_key = 'TST-D-1'")
    assert line["reason"] == "SURPLUS" and line["snapshot"]["cover_type"] == "AMC" and float(line["snapshot"]["rate_value"]) == 1500
    head = one(fleet, "SELECT * FROM amc_removal WHERE removal_no = %s", (r["removal_no"],))
    assert (head["effective_date"], head["approved_by"], head["status"], head["requested_by"]) == (Q["start"], "TST-APPROVER", "APPLIED", ED)
    assert n(fleet, "SELECT count(*) FROM portal_audit WHERE dataset='assets' AND record_key='TST-D-1' AND action='AMC_REMOVE'") == 1


def test_removed_asset_leaves_the_asset_counts(fleet):
    key = "kpi" if "kpi" in dashboards.assets() else "k"
    before = dashboards.assets()[key]["assets"]
    go(["TST-D-1"])
    assert dashboards.assets()[key]["assets"] == before - 1


def test_one_request_can_mix_classes_and_types(fleet):
    r = go(["TST-D-1", "TST-S-1", "TST-U-1"], reason={"TST-D-1": "SURPLUS", "TST-S-1": "END_OF_LIFE", "TST-U-1": "REPLACED"})
    assert n(fleet, "SELECT count(*) FROM amc_removal_line WHERE removal_no = %s", (r["removal_no"],)) == 3
    assert n(fleet, "SELECT count(*) FROM asset WHERE asset_key LIKE 'TST-%%-1' AND is_current = 0") == 3
    assert one(fleet, "SELECT reason FROM amc_removal_line WHERE asset_key = 'TST-S-1'")["reason"] == "END_OF_LIFE"


@pytest.mark.parametrize("bad,why", [("TST-L-1", "laptops"), ("TST-W-1", "not covered by AMC"), ("TST-NOPE", "not found")])
def test_ineligible_assets_block_the_whole_request_and_nothing_changes(fleet, bad, why):
    with pytest.raises(Conflict, match=why):
        go(["TST-D-1", bad])
    assert one(fleet, "SELECT is_current FROM asset WHERE asset_key = 'TST-D-1'")["is_current"] == 1       # the good one was not removed either
    assert n(fleet, "SELECT count(*) FROM amc_removal WHERE requested_by = %s", (ED,)) == 0


def test_cannot_remove_twice(fleet):
    go(["TST-D-1"])
    with pytest.raises(Conflict, match="archived or not current"):
        go(["TST-D-1"])


def test_open_call_needs_an_override_note(fleet):
    fleet.execute("INSERT INTO svc_call (sr_id, asset_key, call_status, is_current, cipl_call_date) VALUES ('TST-SR-9','TST-D-1','OPEN',1,%s)", (dt.date.today(),))
    with pytest.raises(Conflict, match="open call"):
        go(["TST-D-1"])
    go(["TST-D-1"], override_note="Call is for a printer fault, not AMC work")
    assert one(fleet, "SELECT is_current FROM asset WHERE asset_key = 'TST-D-1'")["is_current"] == 0


def test_component_lines_are_refused_for_now(fleet):
    make_asset(fleet, "TST-D-1/MON", record_level="COMPONENT", parent_asset_key="TST-D-1")
    with pytest.raises(Conflict, match="component"):
        go(["TST-D-1"])


def test_effective_date_must_fall_in_the_running_quarter(fleet):
    with pytest.raises(Invalid, match="running quarter"):
        go(["TST-D-1"], effective_date=(Q["start"] - dt.timedelta(days=1)).isoformat())
    with pytest.raises(Invalid, match="running quarter"):
        go(["TST-D-1"], effective_date=(Q["end"] + dt.timedelta(days=1)).isoformat())
    go(["TST-D-1"], effective_date=dt.date.today().isoformat())
    assert one(fleet, "SELECT effective_date FROM amc_removal")["effective_date"] == dt.date.today()


def test_approver_must_be_an_active_portal_user_and_reason_is_required(fleet):
    with pytest.raises(Invalid, match="verbal approval"):
        go(["TST-D-1"], approved_by="NOBODY")
    fleet.execute("UPDATE portal_user SET active = FALSE WHERE username = 'TST-APPROVER'")
    with pytest.raises(Invalid, match="verbal approval"):
        go(["TST-D-1"])
    fleet.execute("UPDATE portal_user SET active = TRUE WHERE username = 'TST-APPROVER'")
    with pytest.raises(Invalid, match="reason"):
        go(["TST-D-1"], reason="")
    with pytest.raises(Invalid, match="at least one"):
        go([])


def test_request_numbers_run_on_within_the_quarter(fleet):
    a = go(["TST-D-1"])["removal_no"]
    b = go(["TST-S-1"])["removal_no"]
    assert a[:-3] == b[:-3] and int(b[-3:]) == int(a[-3:]) + 1


def test_restore_from_archive_still_possible_with_existing_mechanism(fleet):
    from portal.app import edit
    go(["TST-D-1"])
    edit.restore("assets", "TST-D-1", ED, IP, "test")
    assert one(fleet, "SELECT is_current FROM asset WHERE asset_key = 'TST-D-1'")["is_current"] == 1


def test_route_is_for_administrators_and_team_leaders_only(fleet, monkeypatch):
    body = {"keys": ["TST-D-1"], "reason": "SURPLUS", "approved_by": "TST-APPROVER"}
    fake_user(monkeypatch, "USER")
    with TestClient(app) as c:
        assert c.post("/api/amc-removal/remove", json=body, headers=HDR).status_code == 403
    fake_user(monkeypatch, "ADMIN")
    with TestClient(app) as c:
        r = c.post("/api/amc-removal/remove", json=body, headers=HDR)
        assert r.status_code == 200, r.text
        assert r.json()["assets"] == ["TST-D-1"]


# ---------------------------------------------------------------- T3: billing and future-dated removals
def test_billing_is_pro_rata_on_the_quarterly_rate():
    q = pm.quarter(dt.date(2026, 10, 21))                                   # Q3: 1 Oct - 31 Dec 2026, 92 days
    assert (q["start"], q["end"]) == (dt.date(2026, 10, 1), dt.date(2026, 12, 31))
    full = amcremoval.billing(1500, dt.date(2026, 10, 1))
    assert (full["days"], full["quarter_days"], str(full["amount"])) == (92, 92, "1500.00")      # removed on day 1 = the whole quarter is credited
    assert (amcremoval.billing(1500, dt.date(2026, 11, 1))["days"], str(amcremoval.billing(1500, dt.date(2026, 11, 1))["amount"])) == (61, "994.57")
    assert str(amcremoval.billing(1500, dt.date(2026, 12, 31))["amount"]) == "16.30"                # last day only: 1 of 92 days
    assert amcremoval.billing(None, dt.date(2026, 10, 1))["amount"] is None


def test_credit_is_stored_on_the_line_when_applied(fleet):
    r = go(["TST-D-1", "TST-S-1"], effective_date=Q["start"].isoformat())
    lines = {x["asset_key"]: x for x in rows(fleet, "SELECT * FROM amc_removal_line WHERE removal_no = %s", (r["removal_no"],))}
    qdays = (Q["end"] - Q["start"]).days + 1
    assert (lines["TST-D-1"]["credit_days"], str(lines["TST-D-1"]["credit_amount"])) == (qdays, "1500.00")
    assert str(lines["TST-S-1"]["credit_amount"]) == "9000.00"
    assert [x for x in amcremoval.requests() if x["removal_no"] == r["removal_no"]][0]["credit_total"] == 10500


def test_impact_shows_days_credit_and_total_before_anything_is_changed(fleet):
    out = amcremoval.impact(["TST-D-1", "TST-S-1", "TST-L-1"], Q["start"].isoformat())
    by = {a["asset_key"]: a for a in out["assets"]}
    assert str(by["TST-D-1"]["amount"]) == "1500.00" and by["TST-L-1"]["blocked"] == "laptops are outside AMC scope"
    assert str(out["total"]) == "10500.00" and out["quarter"]["days"] == (Q["end"] - Q["start"]).days + 1
    assert one(fleet, "SELECT is_current FROM asset WHERE asset_key = 'TST-D-1'")["is_current"] == 1        # read only


@pytest.fixture()
def tomorrow():
    d = dt.date.today() + dt.timedelta(days=1)
    if d > Q["end"]:
        pytest.skip("today is the last day of the quarter - no later day to test with")
    return d


def test_a_later_date_waits_and_is_applied_when_it_arrives(fleet, tomorrow, monkeypatch):
    r = go(["TST-D-1"], effective_date=tomorrow.isoformat())
    assert r["status"] == "PENDING"
    assert one(fleet, "SELECT is_current, rate_value FROM asset WHERE asset_key = 'TST-D-1'") == {"is_current": 1, "rate_value": 1500}      # untouched
    assert amcremoval.apply_due() == []                                                                  # its day has not come
    monkeypatch.setattr(amcremoval, "TODAY", lambda: tomorrow)
    assert amcremoval.apply_due() == [(r["removal_no"], 1, 0)]
    assert one(fleet, "SELECT is_current, asset_status FROM asset WHERE asset_key = 'TST-D-1'") == {"is_current": 0, "asset_status": "REMOVED_FROM_AMC"}
    assert one(fleet, "SELECT status FROM amc_removal WHERE removal_no = %s", (r["removal_no"],))["status"] == "APPLIED"
    assert amcremoval.apply_due() == []                                                                  # never applied twice


def test_a_pending_asset_that_became_ineligible_is_skipped_with_the_reason(fleet, tomorrow, monkeypatch):
    r = go(["TST-D-1", "TST-S-1"], effective_date=tomorrow.isoformat())
    fleet.execute("UPDATE asset SET cover_type = 'WRTY' WHERE asset_key = 'TST-S-1'")                   # cover changed in the meantime
    monkeypatch.setattr(amcremoval, "TODAY", lambda: tomorrow)
    assert amcremoval.apply_due() == [(r["removal_no"], 1, 1)]
    assert one(fleet, "SELECT skipped_reason FROM amc_removal_line WHERE asset_key = 'TST-S-1'")["skipped_reason"] == "it is not covered by AMC"
    assert one(fleet, "SELECT is_current FROM asset WHERE asset_key = 'TST-S-1'")["is_current"] == 1


def test_a_pending_request_can_be_cancelled_but_an_applied_one_cannot(fleet, tomorrow):
    r = go(["TST-D-1"], effective_date=tomorrow.isoformat())
    amcremoval.cancel(r["removal_no"], ED, IP)
    assert amcremoval.apply_due() == [] and one(fleet, "SELECT is_current FROM asset WHERE asset_key = 'TST-D-1'")["is_current"] == 1
    done = go(["TST-S-1"])
    with pytest.raises(Conflict, match="waiting"):
        amcremoval.cancel(done["removal_no"], ED, IP)


# ---------------------------------------------------------------- T4: what an archived asset drops out of
def test_removed_asset_drops_out_of_pm_scope_and_cover_alerts(fleet):
    from portal.app import mailer
    scope = f"SELECT count(*) FROM asset WHERE {pm.IN_SCOPE} AND asset_key = 'TST-D-1'"
    fleet.execute("UPDATE asset SET pm_status = 'PENDING', engineer_name = 'TST ENG', cover_expiry_date = current_date + 10, cover_status = 'EXPIRING_90D' WHERE asset_key = 'TST-D-1'")
    assert n(fleet, scope) == 1
    go(["TST-D-1"])
    assert n(fleet, scope) == 0                                                                           # no longer in the PM list or PM counts
    for rule in ("AMC_EXPIRED", "AMC_EXPIRING"):
        got = mailer.detect(rule, {"lookback_days": 3650, "within_days": 90}, today=dt.date(2026, 10, 19))      # a Monday
        assert not [i for d in got.values() for i in d["rows"] if i["asset_key"] == "TST-D-1"]


def test_the_engineer_is_told_the_asset_left_their_list(fleet):
    from portal.app import mailer
    fleet.execute("UPDATE asset SET engineer_name = 'TST ENG' WHERE asset_key = 'TST-D-1'")
    go(["TST-D-1"])
    got = mailer.detect("ASSET_REMOVED", {"lookback_days": 7}, today=dt.date.today())
    assert [i["asset_key"] for i in got["TST ENG"]["rows"]] == ["TST-D-1"]


def test_the_archive_lock_the_inventory_loader_reads_is_set(fleet):
    import itam_locks
    go(["TST-D-1"])
    _, _, archived = itam_locks.load_overrides(fleet, "assets")
    assert "TST-D-1" in archived                                  # master_db.py keeps these rows is_current = 0 on every re-import


def test_read_routes_serialise_money_and_dates(fleet, monkeypatch):
    fake_user(monkeypatch, "ADMIN")
    with TestClient(app) as c:
        r = c.get(f"/api/amc-removal/impact?keys=TST-D-1,TST-S-1&date={Q['start']}", headers=HDR)
        assert r.status_code == 200, r.text
        assert float(r.json()["total"]) == 10500 and r.json()["assets"][0]["days"] == (Q["end"] - Q["start"]).days + 1
        assert c.post("/api/amc-removal/remove", json={"keys": ["TST-D-1"], "reason": "SURPLUS", "approved_by": "TST-APPROVER"}, headers=HDR).status_code == 200
        rows = c.get("/api/amc-removal/requests", headers=HDR).json()["rows"]
        assert rows[0]["status"] == "APPLIED" and float(rows[0]["credit_total"]) == 1500
