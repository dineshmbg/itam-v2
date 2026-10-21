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


def test_effective_date_rules(fleet):
    with pytest.raises(Invalid, match="before the running quarter"):
        go(["TST-D-1"], effective_date=(Q["start"] - dt.timedelta(days=1)).isoformat())
    with pytest.raises(Invalid, match="future"):
        go(["TST-D-1"], effective_date=(dt.date.today() + dt.timedelta(days=1)).isoformat())
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
