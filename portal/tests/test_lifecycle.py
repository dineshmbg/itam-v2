"""Replace asset / Redeploy asset. Every test runs inside one rolled-back transaction on synthetic assets (keys TST-*): real data is never touched.

  cd portal && .venv\\Scripts\\python -m pytest -q tests/test_lifecycle.py
"""
import datetime as dt
import sys
from pathlib import Path

import pytest
from psycopg.rows import dict_row
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import itam_locks  # noqa: E402
from portal.app import edit, lifecycle  # noqa: E402
from portal.app.main import app  # noqa: E402
from tests.conftest import HDR, fake_user  # noqa: E402

ED = "TEST EDITOR"
TODAY = dt.date.today()
RET = f"TST-OLD-001-RET-{TODAY:%Y%m%d}"


def rows(con, sql, params=None):
    with con.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def one(con, sql, params=None):
    r = rows(con, sql, params)
    return r[0] if r else None


def n(con, sql, params=None):
    return list(one(con, sql, params).values())[0]


def make_asset(con, key, serial=None, ongc=None, cpf=None, eng=None, loc=None, status="IN_USE", **extra):
    cols = {"snapshot_date": TODAY, "asset_key": key, "ci_no": key, "asset_class": "DESKTOP", "asset_type": "DESKTOP", "record_level": "ASSET", "make": "TESTMAKE", "model": "TESTMODEL",
            "serial_no": serial, "ongc_asset_id": ongc, "cpf_no": cpf, "engineer_name": eng, "location_code": loc, "asset_status": status, "is_current": 1, "first_seen_date": TODAY,
            "last_seen_date": TODAY, "cover_type": "AMC", "cover_expiry_date": dt.date(2029, 4, 13), "pm_date": dt.date(2026, 8, 5), "pm_done_by": eng, "pm_signed_by": "SIGNER", **extra}
    cols = {c: v for c, v in cols.items() if v is not None}
    con.execute("INSERT INTO asset (" + ", ".join(cols) + ") VALUES (" + ", ".join(["%s"] * len(cols)) + ")", list(cols.values()))
    con.execute("INSERT INTO asset_snapshot (snapshot_date, asset_key, asset_class, asset_type, record_level) VALUES (%s,%s,'DESKTOP','DESKTOP','ASSET')", (TODAY, key))
    con.execute("INSERT INTO asset_change_log (snapshot_date, asset_key, change_type) VALUES (%s,%s,'ADDED')", (TODAY, key))


@pytest.fixture()
def pair(box):
    """TST-OLD-001 (the machine going out, has a user and a seat) and TST-NEW-001 (the new machine: no serial, no user)."""
    cpf = one(box, "SELECT cpf_no FROM employee WHERE employee_name IS NOT NULL ORDER BY cpf_no LIMIT 1")["cpf_no"]
    eng = one(box, "SELECT engineer_key FROM portal_engineer ORDER BY engineer_key LIMIT 1")["engineer_key"]
    make_asset(box, "TST-OLD-001", serial="TSTSER-OLD", ongc="TST-ONGC-OLD", cpf=cpf, eng=eng, loc="TST_LOC", floor_area="FOURTH FLOOR", room="CUBICLE", user_department="TST DEPT")
    make_asset(box, "TST-NEW-001", ongc="TST-ONGC-NEW", cover_type="WRTY", cover_expiry_date=dt.date(2030, 7, 24), pm_date=None, pm_done_by=None, pm_signed_by=None)
    return box, cpf, eng


def do_replace(**body):
    return lifecycle.replace("TST-OLD-001", "TST-NEW-001", {"reason": "END_OF_LIFE", "serial_no": "TSTSER-NEW", "carry": {"user": True, "location": True, "engineer": True},
                                                            "data_wiped": True, **body}, ED, "127.0.0.1")


# ---------------------------------------------------------------- replace
def test_replace_retires_old_and_gives_its_name_to_the_new_machine(pair):
    con, cpf, eng = pair
    con.execute("INSERT INTO svc_call (sr_id, asset_key, call_status, is_current, cipl_call_date) VALUES ('TST-SR-1','TST-OLD-001','CLOSED',1,%s)", (TODAY,))
    r = do_replace(disposal_ref="COND/1", hostname="TSTHOST1")
    assert r == {"retired_key": RET, "live_key": "TST-OLD-001", "carried": ["user", "location", "engineer"]}
    old = one(con, "SELECT * FROM asset WHERE asset_key = %s", (RET,))
    assert (old["is_current"], old["asset_status"], old["replaced_by_key"], old["retired_reason"], old["disposal_ref"], old["data_wiped"]) == (0, "REPLACED", "TST-OLD-001", "END_OF_LIFE", "COND/1", "YES")
    assert (old["serial_no"], old["ongc_asset_id"]) == ("TSTSER-OLD", "TST-ONGC-OLD")           # the retired machine keeps its physical identity
    live = one(con, "SELECT * FROM asset WHERE asset_key = 'TST-OLD-001'")
    assert (live["is_current"], live["ci_no"], live["hostname"], live["serial_no"], live["ongc_asset_id"]) == (1, "TST-OLD-001", "TSTHOST1", "TSTSER-NEW", "TST-ONGC-NEW")
    assert (live["cpf_no"], live["engineer_name"], live["location_code"], live["room"]) == (cpf, eng, "TST_LOC", "CUBICLE")
    assert live["user_name"] and live["user_department"] == "TST DEPT"                           # user name derived from the HR master by the carried CPF; department carried
    assert (live["cover_type"], live["cover_expiry_date"]) == ("WRTY", dt.date(2030, 7, 24))     # the new machine keeps its own cover
    assert live["pm_date"] is None and "MISSING_SERIAL" not in (live["dq_flags"] or "")
    assert one(con, "SELECT 1 FROM asset WHERE asset_key = 'TST-NEW-001'") is None
    assert n(con, "SELECT count(*) FROM asset WHERE ongc_asset_id IN ('TST-ONGC-OLD','TST-ONGC-NEW')") == 2        # one record each, none duplicated
    # history follows its machine
    assert n(con, "SELECT count(*) FROM svc_call WHERE asset_key = %s", (RET,)) == 1
    for table in ("asset_change_log", "asset_snapshot"):
        assert n(con, f"SELECT count(*) FROM {table} WHERE asset_key = %s", (RET,)) == 1
        assert n(con, f"SELECT count(*) FROM {table} WHERE asset_key = 'TST-OLD-001'") == 1
    assert n(con, "SELECT count(*) FROM svc_call WHERE asset_key = 'TST-OLD-001'") == 0
    # former names, manual-edit protection, audit
    assert {(a["alias_key"], a["asset_key"]) for a in rows(con, "SELECT * FROM asset_alias")} >= {("TST-OLD-001", RET), ("TST-NEW-001", "TST-OLD-001")}
    assert n(con, "SELECT count(*) FROM portal_lock WHERE dataset='assets' AND record_key=%s AND field=%s", (RET, itam_locks.ARCHIVED)) == 1
    assert n(con, "SELECT count(*) FROM portal_lock WHERE dataset='assets' AND record_key='TST-OLD-001' AND field IN ('serial_no','hostname','cpf_no')") == 3
    acts = {(a["record_key"], a["action"]) for a in rows(con, "SELECT record_key, action FROM portal_audit WHERE dataset='assets' AND record_key LIKE 'TST-%'")}
    assert (RET, "RETIRE") in acts and ("TST-OLD-001", "REPLACE") in acts


def test_replace_without_carry_over_leaves_the_seat_empty(pair):
    con, _, _ = pair
    do_replace(carry={})
    live = one(con, "SELECT * FROM asset WHERE asset_key = 'TST-OLD-001'")
    assert live["cpf_no"] is None and live["engineer_name"] is None and live["location_code"] is None
    assert live["hostname"] == "TST-OLD-001"                              # hostname defaults to the CI number


def test_replace_moves_component_lines_with_their_machine(pair):
    con, _, _ = pair
    make_asset(con, "TST-OLD-001/MON1", parent_asset_key="TST-OLD-001", asset_type="MONITOR", asset_class="DESKTOP")
    make_asset(con, "TST-NEW-001/MON1", parent_asset_key="TST-NEW-001", asset_type="MONITOR", asset_class="DESKTOP")
    do_replace()
    assert {r["asset_key"]: r["parent_asset_key"] for r in rows(con, "SELECT asset_key, parent_asset_key FROM asset WHERE asset_key LIKE 'TST-%/MON1' OR asset_key LIKE 'TST-OLD-001-RET%/MON1'")} == {
        f"{RET}/MON1": RET, "TST-OLD-001/MON1": "TST-OLD-001"}


def test_replace_refuses_while_the_old_machine_has_open_work(pair):
    con, _, _ = pair
    con.execute("INSERT INTO svc_call (sr_id, asset_key, call_status, is_current, cipl_call_date) VALUES ('TST-SR-2','TST-OLD-001','OPEN',1,%s)", (TODAY,))
    with pytest.raises(edit.Conflict, match="open call"):
        do_replace()
    assert one(con, "SELECT is_current FROM asset WHERE asset_key = 'TST-OLD-001'")["is_current"] == 1       # nothing changed
    con.execute("UPDATE svc_call SET call_status = 'CLOSED' WHERE sr_id = 'TST-SR-2'")
    con.execute("INSERT INTO oem_rma (rma_line_id, asset_key, is_current, return_status) VALUES ('TST-RMA-1','TST-OLD-001',1,'PENDING')")
    with pytest.raises(edit.Conflict, match="RMA"):
        do_replace()


@pytest.mark.parametrize("body,field", [({"serial_no": ""}, "serial_no"), ({"serial_no": "TSTSER-OLD"}, "serial_no"), ({"reason": "WHIM"}, "reason"), ({"date": "not-a-date"}, "date"),
                                        ({"replacement_key": "TST-OLD-001"}, "replacement_key")])
def test_replace_validation(pair, body, field):
    con, _, _ = pair
    new_key = body.pop("replacement_key", "TST-NEW-001")
    with pytest.raises(edit.Invalid) as e:
        lifecycle.replace("TST-OLD-001", new_key, {"reason": "END_OF_LIFE", "serial_no": "TSTSER-NEW", **body}, ED, "ip")
    assert field in e.value.fields


def test_replace_refuses_the_same_machine_twice_or_a_missing_one(pair):
    con, _, _ = pair
    make_asset(con, "TST-DUP-001", serial="TSTSER-OLD")                      # same serial as the machine being replaced
    with pytest.raises(edit.Invalid, match="same machine"):
        lifecycle.replace("TST-OLD-001", "TST-DUP-001", {"reason": "FAILED"}, ED, "ip")
    with pytest.raises(edit.NotFound):
        lifecycle.replace("TST-OLD-001", "TST-NOPE-001", {"reason": "FAILED", "serial_no": "X1"}, ED, "ip")
    make_asset(con, "TST-OTHER-001", serial="TSTSER-NEW")                    # the new serial already belongs to another live asset
    with pytest.raises(edit.Conflict, match="TST-OTHER-001"):
        do_replace()


def test_replace_rolls_back_everything_if_a_step_fails(pair, monkeypatch):
    con, _, _ = pair
    real = edit._audit

    def boom(con_, editor, ip, dataset, key, action, *a, **k):
        if action == "REPLACE":
            raise RuntimeError("simulated failure")
        return real(con_, editor, ip, dataset, key, action, *a, **k)
    monkeypatch.setattr(edit, "_audit", boom)
    with pytest.raises(RuntimeError):
        do_replace()
    assert one(con, "SELECT is_current, serial_no FROM asset WHERE asset_key = 'TST-OLD-001'") == {"is_current": 1, "serial_no": "TSTSER-OLD"}
    assert one(con, "SELECT 1 FROM asset WHERE asset_key = 'TST-NEW-001'") and not one(con, "SELECT 1 FROM asset WHERE asset_key = %s", (RET,))
    assert n(con, "SELECT count(*) FROM asset_alias WHERE alias_key LIKE 'TST-%'") == 0


def test_a_replaced_machine_cannot_be_restored_only_redeployed(pair):
    con, _, _ = pair
    do_replace()
    with pytest.raises(edit.Conflict, match="Redeploy"):
        edit.restore("assets", RET, ED, "ip")


# ---------------------------------------------------------------- redeploy
def do_redeploy(**body):
    return lifecycle.redeploy(RET, "TST-TRN-014", {"data_wiped": True, "purpose": "training room", **body}, ED, "ip")


def test_redeploy_brings_the_same_machine_back_under_a_new_name(pair):
    con, _, _ = pair
    do_replace()
    r = do_redeploy(hostname="TSTTRN14", location_code="TST_TRAINING", cpf_no="")
    assert r == {"live_key": "TST-TRN-014", "former_key": RET}
    m = one(con, "SELECT * FROM asset WHERE asset_key = 'TST-TRN-014'")
    assert (m["serial_no"], m["ongc_asset_id"]) == ("TSTSER-OLD", "TST-ONGC-OLD")                    # identity unchanged: still ONE record for this machine
    assert (m["is_current"], m["asset_status"], m["ci_no"], m["hostname"], m["location_code"]) == (1, "IN_USE", "TST-TRN-014", "TSTTRN14", "TST_TRAINING")
    assert m["retired_on"] is None and m["replaced_by_key"] is None and m["cpf_no"] is None and m["pm_date"] is None
    assert m["user_name"] is None and m["user_department"] is None                               # the old user's details do not linger on a machine with no user
    assert "REDEPLOYED" in m["remarks"] and "TRAINING ROOM" in m["remarks"]
    assert one(con, "SELECT 1 FROM asset WHERE asset_key = %s", (RET,)) is None
    assert n(con, "SELECT count(*) FROM asset WHERE serial_no = 'TSTSER-OLD'") == 1
    assert n(con, "SELECT count(*) FROM asset_change_log WHERE asset_key = 'TST-TRN-014'") == 1       # its history came with it
    assert n(con, "SELECT count(*) FROM portal_lock WHERE dataset='assets' AND record_key='TST-TRN-014' AND field=%s", (itam_locks.ARCHIVED,)) == 0
    assert {a["alias_key"] for a in rows(con, "SELECT alias_key FROM asset_alias WHERE asset_key = 'TST-TRN-014'")} >= {"TST-OLD-001", RET}
    assert (("TST-TRN-014", "REDEPLOY") in {(a["record_key"], a["action"]) for a in rows(con, "SELECT record_key, action FROM portal_audit WHERE record_key = 'TST-TRN-014'")})


def test_redeploy_rules(pair):
    con, _, _ = pair
    do_replace()
    with pytest.raises(edit.Invalid) as e:
        lifecycle.redeploy(RET, "TST-TRN-014", {}, ED, "ip")
    assert "data_wiped" in e.value.fields
    for forbidden in ("serial_no", "ongc_asset_id", "ongc_census_no"):
        with pytest.raises(edit.Invalid) as e:
            do_redeploy(**{forbidden: "NEW"})
        assert forbidden in e.value.fields
    with pytest.raises(edit.Conflict, match="already exists"):
        lifecycle.redeploy(RET, "TST-OLD-001", {"data_wiped": True}, ED, "ip")      # the name now held by the replacement machine
    with pytest.raises(edit.NotFound):
        lifecycle.redeploy("TST-OLD-001", "TST-TRN-014", {"data_wiped": True}, ED, "ip")                               # a live machine is not "retired"


def test_a_condemned_machine_needs_an_approval_reference(pair):
    con, _, _ = pair
    do_replace(reason="CONDEMNED")
    with pytest.raises(edit.Invalid) as e:
        do_redeploy()
    assert "approval_ref" in e.value.fields
    do_redeploy(approval_ref="MGR/IT/2026/118")
    assert "APPROVAL MGR/IT/2026/118" in one(con, "SELECT remarks FROM asset WHERE asset_key = 'TST-TRN-014'")["remarks"]


def test_redeploy_refuses_a_name_formerly_used_by_another_machine(pair):
    con, _, _ = pair
    do_replace()
    with pytest.raises(edit.Conflict, match="formerly the name"):
        lifecycle.redeploy(RET, "TST-NEW-001", {"data_wiped": True}, ED, "ip")      # TST-NEW-001 was the NEW machine's earlier name


def test_redeploy_refuses_when_another_live_asset_has_the_same_serial(pair):
    con, _, _ = pair
    do_replace()
    make_asset(con, "TST-CLONE-001", serial="TSTSER-OLD")
    with pytest.raises(edit.Conflict, match="TST-CLONE-001"):
        do_redeploy()


def test_a_generically_archived_asset_can_be_redeployed_with_approval(pair):
    con, _, _ = pair
    make_asset(con, "TST-ARCH-001", serial="TSTSER-ARCH", ongc="TST-ONGC-ARCH")
    edit.archive("assets", "TST-ARCH-001", ED, "ip", "archived for the test")
    with pytest.raises(edit.Invalid):
        lifecycle.redeploy("TST-ARCH-001", "TST-ARCH-002", {"data_wiped": True}, ED, "ip")
    lifecycle.redeploy("TST-ARCH-001", "TST-ARCH-002", {"data_wiped": True, "approval_ref": "OK/1"}, ED, "ip")
    assert one(con, "SELECT serial_no, is_current FROM asset WHERE asset_key = 'TST-ARCH-002'") == {"serial_no": "TSTSER-ARCH", "is_current": 1}


# ---------------------------------------------------------------- read side, access, importer guard
def test_preflight_and_candidates(pair):
    con, _, _ = pair
    p = lifecycle.preflight("TST-OLD-001")
    assert p["open_calls"] == 0 and p["open_rma"] == 0 and p["history"]["change log"] == 1 and not p["retired"]
    assert {c["asset_key"] for c in lifecycle.candidates("TST-NEW", "TST-OLD-001")} == {"TST-NEW-001"}
    assert lifecycle.candidates("T", "") == [] and all(c["asset_key"] != "TST-OLD-001" for c in lifecycle.candidates("TST-OLD", "TST-OLD-001"))
    do_replace()
    p = lifecycle.preflight(RET)
    assert p["retired"] and p["row"]["replaced_by_key"] == "TST-OLD-001" and {a["alias_key"] for a in p["aliases"]} == {"TST-OLD-001"}


def test_only_real_administrators_can_use_the_endpoints(pair, monkeypatch):
    con, _, _ = pair
    body = {"key": "TST-OLD-001", "replacement_key": "TST-NEW-001", "reason": "END_OF_LIFE", "serial_no": "TSTSER-NEW"}
    fake_user(monkeypatch, "USER")
    with TestClient(app) as c:
        assert c.get("/api/lifecycle/preflight", params={"key": "TST-OLD-001"}).status_code == 403
        assert c.post("/api/lifecycle/replace", json=body, headers=HDR).status_code == 403
    assert one(con, "SELECT is_current FROM asset WHERE asset_key = 'TST-OLD-001'")["is_current"] == 1
    fake_user(monkeypatch, "ADMIN")
    with TestClient(app) as c:
        assert c.get("/api/lifecycle/preflight", params={"key": "TST-OLD-001"}).status_code == 200
        r = c.post("/api/lifecycle/replace", json=body, headers=HDR)
        assert r.status_code == 200, r.text
        assert r.json()["retired_key"] == RET and r.json()["detail"]["row"]["serial_no"] == "TSTSER-NEW"
        r = c.post("/api/lifecycle/redeploy", json={"key": RET, "new_key": "TST-TRN-014", "data_wiped": True, "approval_ref": "OK"}, headers=HDR)
        assert r.status_code == 200, r.text
        assert r.json()["detail"]["related"]["aliases"]


def test_the_loader_guard_stops_a_stale_sheet(pair):
    con, _, _ = pair
    assert itam_locks.alias_conflicts(con, [("TST-OLD-001", "TSTSER-OLD", None, 491)]) == []        # nothing replaced yet: no objection
    do_replace()
    stale_old = itam_locks.alias_conflicts(con, [("TST-OLD-001", "TSTSER-OLD", "TST-ONGC-OLD", 491)])     # row 491 still describes the old machine under the reused name
    assert len(stale_old) == 1 and "row 491" in stale_old[0] and "TSTSER-NEW" in stale_old[0]
    stale_new = itam_locks.alias_conflicts(con, [("TST-NEW-001", "TSTSER-NEW", None, 1017)])              # the new machine's row still has its pre-replacement name
    assert len(stale_new) == 1 and "former name" in stale_new[0] and "TST-OLD-001" in stale_new[0]
    assert itam_locks.alias_conflicts(con, [("TST-OLD-001", "tstser-new", "TST-ONGC-NEW", 1017), ("SOME-OTHER", "X", None, 5)]) == []      # sheet already corrected: loads normally
