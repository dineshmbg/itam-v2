"""Editing tests. Every test runs inside ONE database transaction that is rolled back at the end, so the real data is never changed.

  cd portal && .venv\\Scripts\\python -m pytest -q tests/test_edit.py
"""
import contextlib
import datetime as dt
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
try:
    import call_tracking  # noqa: E402
except ModuleNotFoundError:      # the loaders need pandas, which the portal's own venv does not ship: borrow the base interpreter's copy for this test only
    sys.path.append(str(Path(sys.base_prefix) / "Lib" / "site-packages"))
    import call_tracking  # noqa: E402
import itam_locks  # noqa: E402
from portal.app import config, db, edit  # noqa: E402
from portal.app.main import app  # noqa: E402

ED = "Test Editor"


@pytest.fixture()
def sandbox(monkeypatch):
    con = psycopg.connect(psycopg.conninfo.make_conninfo(**config.PG, password=db.password(), connect_timeout=8))
    itam_locks.ensure_tables(con)

    @contextlib.contextmanager
    def write():
        with con.transaction():          # savepoint: a failed edit rolls back just itself, like the real thing
            yield con

    def rows(sql, params=None):
        with con.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    monkeypatch.setattr(db, "write", write)
    monkeypatch.setattr(db, "query", rows)
    monkeypatch.setattr(db, "one", lambda sql, params=None: (rows(sql, params) or [None])[0])
    try:
        yield con
    finally:
        con.rollback()
        con.close()


def one(con, sql, params=()):
    with con.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def call_row(con, sr):
    return one(con, "SELECT * FROM svc_call WHERE sr_id = %s", (sr,))


def open_call(con):
    return one(con, "SELECT * FROM svc_call WHERE is_current = 1 AND call_status = 'OPEN' AND asset_key IS NOT NULL ORDER BY sr_id LIMIT 1")


def new_call(con, sr="TEST-SR-1", **kw):
    a = one(con, "SELECT asset_key FROM asset WHERE is_current = 1 AND record_level = 'ASSET' ORDER BY asset_key LIMIT 1")["asset_key"]
    eng = one(con, "SELECT engineer_key FROM portal_engineer ORDER BY 1 LIMIT 1")["engineer_key"]
    v = {"sr_id": sr, "cipl_call_date": "2026-09-01", "asset_key": a, "engineer": eng, "priority": "P2", "problem_description": "test problem", **kw}
    edit.create("calls", v, ED, "127.0.0.1")
    return sr


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize("changes,field", [
    ({"priority": "P9"}, "priority"), ({"engineer": "NOBODY AT ALL"}, "engineer"), ({"cipl_call_date": "2031-01-01"}, "cipl_call_date"),
    ({"cipl_call_date": "not-a-date"}, "cipl_call_date"), ({"cpf_no": "999999"}, "cpf_no"), ({"asset_key": "NO-SUCH-CI"}, "asset_key"),
    ({"is_current": 0}, "is_current"), ({"sr_id": "X"}, "sr_id"), ({"oem_rma_no": "NOPE"}, "oem_rma_no"), ({"problem_description": "x" * 501}, "problem_description"),
])
def test_invalid_values_are_rejected_with_field_message(sandbox, changes, field):
    sr = open_call(sandbox)["sr_id"]
    with pytest.raises(edit.Invalid) as e:
        edit.update("calls", sr, changes, {}, ED, "127.0.0.1")
    assert field in e.value.fields
    assert one(sandbox, "SELECT count(*) n FROM portal_audit WHERE record_key = %s", (sr,))["n"] == 0


def test_close_requires_a_date_not_before_logging(sandbox):
    c = open_call(sandbox)
    with pytest.raises(edit.Invalid) as e:
        edit.update("calls", c["sr_id"], {"call_status": "CLOSED"}, {}, ED, "127.0.0.1")
    assert "closed_date" in e.value.fields
    with pytest.raises(edit.Invalid) as e:
        edit.update("calls", c["sr_id"], {"call_status": "CLOSED", "closed_date": (c["cipl_call_date"] - dt.timedelta(days=1)).isoformat()}, {}, ED, "127.0.0.1")
    assert "closed_date" in e.value.fields


def test_ip_and_date_order_rules(sandbox):
    a = one(sandbox, "SELECT asset_key FROM asset WHERE is_current = 1 AND record_level = 'ASSET' LIMIT 1")["asset_key"]
    with pytest.raises(edit.Invalid) as e:
        edit.update("assets", a, {"ip_address": "10.1.2.999"}, {}, ED, "127.0.0.1")
    assert "ip_address" in e.value.fields
    r = one(sandbox, "SELECT rma_line_id FROM oem_rma WHERE is_current = 1 LIMIT 1")["rma_line_id"]
    with pytest.raises(edit.Invalid) as e:
        edit.update("rma", r, {"call_log_date": "2026-05-10", "replacement_received_date": "2026-05-01"}, {}, ED, "127.0.0.1")
    assert "replacement_received_date" in e.value.fields


# ---------------------------------------------------------------- updates
def test_closing_a_call_recomputes_derived_fields_and_writes_audit_and_lock(sandbox):
    c = open_call(sandbox)
    day = c["cipl_call_date"] + dt.timedelta(days=3)
    res = edit.update("calls", c["sr_id"], {"call_status": "CLOSED", "closed_date": day.isoformat()}, {"call_status": "OPEN", "closed_date": None}, ED, "10.0.0.5", "fixed on site")
    assert sorted(res["changed"]) == ["call_status", "closed_date"]
    r = call_row(sandbox, c["sr_id"])
    assert r["call_status"] == "CLOSED" and r["closed_date"] == day and r["tat_days"] == 3 and r["ageing_days"] is None
    a = one(sandbox, "SELECT * FROM portal_audit WHERE record_key = %s AND action = 'UPDATE'", (c["sr_id"],))
    assert a["editor"] == ED and a["client_ip"] == "10.0.0.5" and a["reason"] == "fixed on site"
    assert a["changes"]["call_status"] == {"old": "OPEN", "new": "CLOSED"}
    locks = {x["field"] for x in sandbox.execute("SELECT field FROM portal_lock WHERE dataset='calls' AND record_key=%s", (c["sr_id"],)).fetchall() for x in [{"field": x[0]}]}
    assert locks == {"call_status", "closed_date"}


def test_reopening_clears_closed_date(sandbox):
    c = one(sandbox, "SELECT * FROM svc_call WHERE is_current = 1 AND call_status = 'CLOSED' AND closed_date IS NOT NULL LIMIT 1")
    edit.update("calls", c["sr_id"], {"call_status": "OPEN"}, {}, ED, "127.0.0.1")
    r = call_row(sandbox, c["sr_id"])
    assert r["closed_date"] is None and r["tat_days"] is None and r["ageing_days"] is not None
    assert "OPEN_WITH_CLOSED_DATE" not in (r["dq_flags"] or "")


def test_stale_edit_is_refused_with_current_value(sandbox):
    c = open_call(sandbox)
    with pytest.raises(edit.Conflict) as e:
        edit.update("calls", c["sr_id"], {"priority": "P1"}, {"priority": "P3"}, ED, "127.0.0.1")
    assert e.value.current["priority"]["current"] == c["priority"]
    assert call_row(sandbox, c["sr_id"])["priority"] == c["priority"]


def test_unchanged_save_is_a_no_op(sandbox):
    c = open_call(sandbox)
    res = edit.update("calls", c["sr_id"], {"priority": c["priority"]}, {"priority": c["priority"]}, ED, "127.0.0.1")
    assert res["changed"] == []
    assert one(sandbox, "SELECT count(*) n FROM portal_audit WHERE record_key = %s", (c["sr_id"],))["n"] == 0


def test_changing_call_asset_moves_the_copied_asset_columns(sandbox):
    c = open_call(sandbox)
    other = one(sandbox, "SELECT * FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND asset_key <> %s AND make IS NOT NULL LIMIT 1", (c["asset_key"],))
    edit.update("calls", c["sr_id"], {"asset_key": other["asset_key"]}, {}, ED, "127.0.0.1")
    r = call_row(sandbox, c["sr_id"])
    assert (r["asset_class"], r["make"], r["model"], r["serial_no"]) == (other["asset_class"], other["make"], other["model"], other["serial_no"])


def test_asset_user_and_pm_rules(sandbox):
    a = one(sandbox, "SELECT asset_key FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND asset_class = 'DESKTOP' LIMIT 1")["asset_key"]
    emp = one(sandbox, "SELECT cpf_no, employee_name FROM employee WHERE record_status = 'ACTIVE' LIMIT 1")
    sandbox.execute("UPDATE asset SET pm_quarter = 'Q2 JUL-SEP 2026' WHERE asset_key = %s", (a,))      # pin the quarter: the real data moves on every roll-over
    edit.update("assets", a, {"cpf_no": str(emp["cpf_no"]), "pm_date": "2026-08-15"}, {}, ED, "127.0.0.1")
    r = one(sandbox, "SELECT * FROM asset WHERE asset_key = %s", (a,))
    assert r["user_name"] == emp["employee_name"] and r["user_hr_status"] == "ACTIVE" and r["pm_status"] == "DONE"
    edit.update("assets", a, {"pm_date": "2025-01-10"}, {}, ED, "127.0.0.1")
    r = one(sandbox, "SELECT * FROM asset WHERE asset_key = %s", (a,))
    assert r["pm_status"] == "DONE_OUTSIDE_QUARTER" and "PM_OUTSIDE_QUARTER" in r["dq_flags"]


def test_other_flags_survive_an_edit(sandbox):
    a = one(sandbox, "SELECT asset_key, dq_flags FROM asset WHERE is_current = 1 AND dq_flags LIKE '%%GENERATED_KEY%%' LIMIT 1", ())
    if not a:
        pytest.skip("no generated-key rows")
    edit.update("assets", a["asset_key"], {"remarks": "checked"}, {}, ED, "127.0.0.1")
    assert "GENERATED_KEY" in one(sandbox, "SELECT dq_flags FROM asset WHERE asset_key = %s", (a["asset_key"],))["dq_flags"]


# ---------------------------------------------------------------- create / cascade / archive / reset
def test_create_call_and_receipt_cascades_to_call(sandbox):
    sr = new_call(sandbox, part_required="ABC-123")
    r = call_row(sandbox, sr)
    assert r["is_current"] == 1 and r["call_status"] == "OPEN" and r["spare_status"] == "PART_PENDING" and "OPEN_AWAITING_PART" in r["dq_flags"]
    created = edit.create("inward", {"inward_date": "2026-09-02", "sr_id": sr, "part_description": "test part"}, ED, "127.0.0.1")
    iid = created["id"]
    assert iid.startswith("IN-") and iid > "IN-0250"
    assert call_row(sandbox, sr)["spare_status"] == "PART_PENDING"
    edit.update("inward", iid, {"received_date": "2026-09-05"}, {}, ED, "127.0.0.1")
    r = call_row(sandbox, sr)
    assert r["spare_status"] == "PART_RECEIVED" and "OPEN_AWAITING_PART" not in (r["dq_flags"] or "")
    assert one(sandbox, "SELECT transit_days FROM spare_inward WHERE inward_id = %s", (iid,))["transit_days"] == 3
    assert one(sandbox, "SELECT count(*) n FROM portal_audit WHERE record_key = %s AND action = 'CASCADE'", (sr,))["n"] >= 1


def test_create_rejects_duplicates_and_missing_required(sandbox):
    sr = new_call(sandbox)
    with pytest.raises(edit.Conflict):
        new_call(sandbox, sr)
    with pytest.raises(edit.Invalid) as e:
        edit.create("calls", {"sr_id": "TEST-SR-2"}, ED, "127.0.0.1")
    assert {"cipl_call_date", "asset_key", "engineer", "problem_description"} <= set(e.value.fields)
    with pytest.raises(edit.Invalid):
        edit.create("inward", {"inward_date": "2026-09-02", "sr_id": "NO-SUCH-CALL", "part_description": "x"}, ED, "127.0.0.1")


def test_ids_are_sequential_per_register(sandbox):
    sr = new_call(sandbox)
    a = edit.create("outward", {"outward_date": "2026-09-02", "sr_id": sr, "part_description": "p1"}, ED, "127.0.0.1")["id"]
    b = edit.create("outward", {"outward_date": "2026-09-02", "sr_id": sr, "part_description": "p2"}, ED, "127.0.0.1")["id"]
    assert a.startswith("OUT-") and int(b.split("-")[1]) == int(a.split("-")[1]) + 1


def test_archive_needs_reason_and_free_of_dependents_then_restore(sandbox):
    sr = new_call(sandbox)
    iid = edit.create("inward", {"inward_date": "2026-09-02", "sr_id": sr, "part_description": "p"}, ED, "127.0.0.1")["id"]
    with pytest.raises(edit.Invalid):
        edit.archive("calls", sr, ED, "127.0.0.1", "")
    with pytest.raises(edit.Conflict) as e:
        edit.archive("calls", sr, ED, "127.0.0.1", "entered by mistake")
    assert "inward lines" in str(e.value)
    edit.archive("inward", iid, ED, "127.0.0.1", "duplicate entry")
    assert one(sandbox, "SELECT is_current FROM spare_inward WHERE inward_id = %s", (iid,))["is_current"] == 0
    edit.archive("calls", sr, ED, "127.0.0.1", "entered by mistake")
    with pytest.raises(edit.NotFound):
        edit.update("calls", sr, {"priority": "P1"}, {}, ED, "127.0.0.1")
    edit.restore("calls", sr, ED, "127.0.0.1")
    assert call_row(sandbox, sr)["is_current"] == 1
    assert one(sandbox, "SELECT count(*) n FROM portal_lock WHERE record_key = %s AND field = '__archived__'", (sr,))["n"] == 0


def test_cannot_archive_an_asset_that_has_calls(sandbox):
    c = open_call(sandbox)
    with pytest.raises(edit.Conflict):
        edit.archive("assets", c["asset_key"], ED, "127.0.0.1", "test archive")


def test_reset_returns_field_to_source_value(sandbox):
    c = open_call(sandbox)
    other = "P3" if c["priority"] != "P3" else "P1"
    edit.update("calls", c["sr_id"], {"priority": other}, {}, ED, "127.0.0.1")
    edit.reset("calls", c["sr_id"], ["priority"], ED, "127.0.0.1")
    assert call_row(sandbox, c["sr_id"])["priority"] == c["priority"]
    assert one(sandbox, "SELECT count(*) n FROM portal_lock WHERE record_key = %s AND field = 'priority'", (c["sr_id"],))["n"] == 0
    with pytest.raises(edit.Invalid):
        edit.reset("calls", c["sr_id"], ["priority"], ED, "127.0.0.1")


# ---------------------------------------------------------------- loader interplay
def _records_from_db(con, name):
    cols, key, table = call_tracking.TABLES[name]
    names = [c[0] for c in cols]
    with con.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT {','.join(names)} FROM {table} WHERE is_current = 1")
        return [{k.upper(): v for k, v in r.items()} for r in cur.fetchall()]


def test_reload_keeps_manual_edits_created_and_archived_records(sandbox):
    c = open_call(sandbox)
    manual = "P3" if c["priority"] != "P3" else "P1"
    gone = one(sandbox, "SELECT sr_id FROM svc_call WHERE is_current = 1 AND call_status = 'CLOSED' AND sr_id <> %s AND NOT EXISTS (SELECT 1 FROM spare_inward i WHERE i.sr_id = svc_call.sr_id AND i.is_current = 1) "
                        "AND NOT EXISTS (SELECT 1 FROM spare_outward o WHERE o.sr_id = svc_call.sr_id AND o.is_current = 1) AND NOT EXISTS (SELECT 1 FROM oem_rma r WHERE r.call_sr_id = svc_call.sr_id AND r.is_current = 1) LIMIT 1", (c["sr_id"],))["sr_id"]
    file_records = _records_from_db(sandbox, "CALLS")           # what the next source file will say: the data as it was before any portal edit
    edit.update("calls", c["sr_id"], {"priority": manual, "call_status": "CLOSED", "closed_date": (c["cipl_call_date"] + dt.timedelta(days=2)).isoformat()}, {}, ED, "127.0.0.1")
    new_sr = new_call(sandbox, "TEST-SR-KEEP")
    edit.archive("calls", gone, ED, "127.0.0.1", "test archive")
    call_tracking.load_table(sandbox, "CALLS", file_records, dt.date(2026, 9, 22), True)
    r = call_row(sandbox, c["sr_id"])
    assert r["priority"] == manual and r["call_status"] == "CLOSED" and r["tat_days"] == 2 and r["ageing_days"] is None
    assert call_row(sandbox, new_sr)["is_current"] == 1, "a record created in the portal must not be retired because it is not in the file"
    assert call_row(sandbox, gone)["is_current"] == 0, "a record archived in the portal must stay archived"
    src = one(sandbox, "SELECT source_value FROM portal_lock WHERE dataset='calls' AND record_key=%s AND field='priority'", (c["sr_id"],))["source_value"]
    assert src == c["priority"]


def test_asset_overrides_are_reapplied_to_incoming_rows(sandbox):
    a = one(sandbox, "SELECT * FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND asset_class = 'DESKTOP' LIMIT 1")
    edit.update("assets", a["asset_key"], {"location_code": "TESTLOC", "cover_expiry_date": "2020-01-01"}, {}, ED, "127.0.0.1")
    incoming = {k.upper(): v for k, v in a.items()}
    itam_locks.apply_records(sandbox, "assets", [incoming], "ASSET_KEY", dt.date(2026, 9, 22))
    assert incoming["LOCATION_CODE"] == "TESTLOC" and incoming["COVER_EXPIRY_DATE"] == dt.date(2020, 1, 1)
    assert incoming["COVER_STATUS"] in ("EXPIRED", "REMOVED") and (incoming["COVER_STATUS"] == "REMOVED" or "COVER_EXPIRED" in incoming["DQ_FLAGS"])


def test_rules_survive_missing_values_from_spreadsheets(sandbox):
    """pandas hands over NaN for empty cells; the rules must treat that as empty, not crash (found by importing the real files)."""
    import itam_rules as R
    nan = float("nan")
    row = {"asset_class": "DESKTOP", "record_level": "ASSET", "asset_status": "IN_USE", "cover_expiry_date": nan, "pm_date": nan, "pm_quarter": "Q2 JUL-SEP 2026", "dq_flags": nan, "ongc_asset_id": nan,
           "serial_no": nan, "ip_address": nan, "rate_value": nan}
    out = R.derive_asset({k: (None if isinstance(v, float) and v != v and k in ("cover_expiry_date", "pm_date") else v) for k, v in row.items()}, {}, dt.date(2026, 9, 22))
    assert out["pm_status"] == "PENDING" and "MISSING_ASSET_ID" in out["dq_flags"]


# ---------------------------------------------------------------- engineers
def test_engineer_exit_and_rejoin(sandbox):
    eng = one(sandbox, "SELECT engineer_key, ecode FROM portal_engineer WHERE ecode IS NOT NULL ORDER BY engineer_key LIMIT 1")
    edit.engineer_event(eng["engineer_key"], "RESIGNED", "2026-09-20", "test", ED, "127.0.0.1")
    assert one(sandbox, "SELECT employment_status FROM cipl_employee WHERE ecode = %s", (eng["ecode"],))["employment_status"] == "RESIGNED"
    with pytest.raises(edit.Conflict):
        edit.engineer_event(eng["engineer_key"], "RESIGNED", "2026-09-20", "again", ED, "127.0.0.1")
    edit.engineer_event(eng["engineer_key"], "REJOINED", "2026-09-21", "test", ED, "127.0.0.1")
    assert one(sandbox, "SELECT employment_status FROM cipl_employee WHERE ecode = %s", (eng["ecode"],))["employment_status"] == "ACTIVE"
    assert one(sandbox, "SELECT count(*) n FROM cipl_event_log WHERE ecode = %s AND source = 'MANUAL' AND snapshot_date = current_date", (eng["ecode"],))["n"] >= 2


# ---------------------------------------------------------------- HTTP layer
import datetime as _dt  # noqa: E402

from portal.app import web  # noqa: E402

HDR = {"X-Requested-With": "itam-portal", "Content-Type": "application/json"}


def as_user(monkeypatch, role="ADMIN", engineer_key=None, username="TEST"):
    user = {"user_id": 0, "username": username, "display_name": username, "role": role, "state": "ok", "email": None, "active": True, "totp_enabled": False, "must_change": False,
            "locked_until": None, "last_login_at": None, "created_at": _dt.datetime.now(_dt.timezone.utc), "engineer_key": engineer_key}

    async def fake(request):
        return user
    monkeypatch.setattr(web, "current_user", fake)
    return user


def test_http_write_guards(sandbox, monkeypatch):
    sr = open_call(sandbox)["sr_id"]
    body = {"key": sr, "changes": {"priority": "P3"}}
    with TestClient(app) as c:
        assert c.post("/api/edit/calls/update", json=body, headers=HDR).status_code == 401                             # not signed in
        as_user(monkeypatch)
        assert c.post("/api/edit/calls/update", json=body).status_code == 403                                         # no portal header
        assert c.post("/api/edit/calls/update", json=body, headers={**HDR, "Origin": "http://evil.example"}).status_code == 403
        assert c.post("/api/edit/calls/update", content=b'{"key": "x"}', headers={**HDR, "Content-Type": "text/plain"}).status_code == 403
        assert c.post("/api/edit/nosuch/update", json=body, headers=HDR).status_code == 400
        assert c.get("/api/edit/calls/update").status_code == 405                                                     # writes are POST only
    assert call_row(sandbox, sr)["priority"] == open_call(sandbox)["priority"]


def test_http_update_returns_fresh_detail_and_conflict_is_409(sandbox, monkeypatch):
    as_user(monkeypatch)
    c0 = open_call(sandbox)
    other = "P3" if c0["priority"] != "P3" else "P1"
    with TestClient(app) as c:
        r = c.post("/api/edit/calls/update", json={"key": c0["sr_id"], "changes": {"priority": other}, "expected": {"priority": c0["priority"]}}, headers=HDR)
        assert r.status_code == 200, r.text
        d = r.json()["detail"]
        assert d["row"]["priority"] == other and "priority" in d["overrides"] and d["overrides"]["priority"]["editor"] == "TEST"
        assert d["related"]["audit"][0]["action"] == "UPDATE"
        r = c.post("/api/edit/calls/update", json={"key": c0["sr_id"], "changes": {"priority": c0["priority"]}, "expected": {"priority": c0["priority"]}}, headers=HDR)
        assert r.status_code == 409 and r.json()["current"]["priority"]["current"] == other
        r = c.post("/api/edit/calls/update", json={"key": c0["sr_id"], "changes": {"priority": "P9"}}, headers=HDR)
        assert r.status_code == 400 and "priority" in r.json()["fields"]
        s = c.get("/api/edit/schema").json()
        assert "calls" in s["datasets"] and s["engineers"]
    assert one(sandbox, "SELECT count(*) n FROM portal_activity WHERE username = 'TEST' AND action = 'EDIT'")["n"] == 1


def test_groups_limit_what_a_user_can_change(sandbox, monkeypatch):
    """USER group: assets - any field except the Contract and Lifecycle groups (2026-10-02, blanket for every User, no
    per-person toggle), and only on assets assigned to them; no new assets, no asset archive. ADMIN: everything."""
    a_row = one(sandbox, "SELECT asset_key, engineer_name FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND asset_class = 'DESKTOP' AND engineer_name IS NOT NULL LIMIT 1")
    a = a_row["asset_key"]
    sandbox.execute("UPDATE asset SET pm_quarter = 'Q2 JUL-SEP 2026' WHERE asset_key = %s", (a,))      # pin the quarter: the real data moves on every roll-over
    as_user(monkeypatch, "USER", engineer_key=a_row["engineer_name"])
    with TestClient(app) as c:
        r = c.post("/api/edit/assets/update", json={"key": a, "changes": {"asset_status": "STANDBY", "pm_date": "2026-08-01", "location_code": "X1", "hostname": "RENAMED"}}, headers=HDR)
        assert r.status_code == 200, r.text
        assert one(sandbox, "SELECT asset_status, pm_status, location_code, hostname FROM asset WHERE asset_key = %s", (a,)) == {"asset_status": "STANDBY", "pm_status": "DONE", "location_code": "X1", "hostname": "RENAMED"}
        r = c.post("/api/edit/assets/update", json={"key": a, "changes": {"cover_expiry_date": "2027-01-01"}}, headers=HDR)
        assert r.status_code == 403 and "administrator" in r.json()["error"].lower()
        r = c.post("/api/edit/assets/update", json={"key": a, "changes": {"asset_status": "IN_USE", "purchase_cost": 999}}, headers=HDR)
        assert r.status_code == 403
        assert c.post("/api/edit/assets/create", json={"values": {"asset_key": "NEW-1", "asset_class": "DESKTOP", "asset_type": "DESKTOP"}}, headers=HDR).status_code == 403
        assert c.post("/api/edit/assets/archive", json={"key": a, "reason": "test archive"}, headers=HDR).status_code == 403
        assert c.post("/api/engineers/X/event", json={"type": "RESIGNED", "date": "2026-09-01"}, headers=HDR).status_code == 403
        # Calls, Inward, Outward and OEM RMA are administrator-only registers - a User cannot even reach validation
        assert c.post("/api/edit/calls/create", json={"values": {}}, headers=HDR).status_code == 403
        assert c.get("/api/registers/calls").status_code == 403
        assert c.get("/api/registers/inward").status_code == 403
        assert c.get("/api/registers/outward").status_code == 403
        assert c.get("/api/registers/rma").status_code == 403
        assert c.get("/api/dash/engineers").status_code == 403
        assert c.get("/api/audit").status_code == 403
        assert c.get("/api/pm/cycles").status_code == 403
    as_user(monkeypatch, "ADMIN")
    with TestClient(app) as c:
        r = c.post("/api/edit/assets/create", json={"values": {"asset_key": "test-new-1", "asset_class": "DESKTOP", "asset_type": "desktop", "make": "hp", "cpf_no": ""}}, headers=HDR)
        assert r.status_code == 200, r.text
        row = one(sandbox, "SELECT * FROM asset WHERE asset_key = 'TEST-NEW-1'")
        assert row["make"] == "HP" and row["asset_type"] == "DESKTOP" and row["is_current"] == 1 and row["pm_status"] == "PENDING"


def test_schema_readonly_matches_asset_locked_fields_for_a_user(sandbox, monkeypatch):
    """The edit form (record.js) only renders fields the schema marks !readonly - check_edit alone is not enough, a
    field has to be offered by /api/edit/schema too (caught as a real bug once already, 2026-10-01). Contract and
    Lifecycle fields must be readonly for a plain User; everything else must not be; asset_access=FULL unlocks all
    of it, same as check_edit's own bypass."""
    as_user(monkeypatch, "USER", engineer_key="SOME-ENGINEER")
    with TestClient(app) as c:
        fields = {f["key"]: f for f in c.get("/api/edit/schema").json()["datasets"]["assets"]["fields"]}
    for locked in ("cover_type", "cover_expiry_date", "rate_component", "rate_value", "purchase_date", "purchase_cost", "vendor_name", "po_no", "refresh_due_date"):
        assert fields[locked]["readonly"] is True, locked
    for open_field in ("asset_status", "hostname", "location_code", "cpf_no", "engineer_name", "make", "model", "remarks"):
        assert fields[open_field]["readonly"] is False, open_field


def test_schema_unlocks_everything_with_asset_access_full(sandbox, monkeypatch):
    u = as_user(monkeypatch, "USER", engineer_key="SOME-ENGINEER")
    u["asset_access"] = "FULL"
    with TestClient(app) as c:
        fields = {f["key"]: f for f in c.get("/api/edit/schema").json()["datasets"]["assets"]["fields"]}
    assert fields["cover_expiry_date"]["readonly"] is False
    assert fields["purchase_cost"]["readonly"] is False


def test_text_is_stored_in_upper_case(sandbox):
    sr = new_call(sandbox, "TEST-SR-UP", problem_description="mixed Case problem", zone="north zone")
    r = call_row(sandbox, sr)
    assert r["problem_description"] == "MIXED CASE PROBLEM" and r["zone"] == "NORTH ZONE"
    edit.update("calls", sr, {"site": "Ankleshwar Asset"}, {}, ED, "127.0.0.1")
    assert call_row(sandbox, sr)["site"] == "ANKLESHWAR ASSET"
    sandbox.execute("UPDATE svc_call SET spare_issue_note = 'lower text' WHERE sr_id = %s", (sr,))     # the database trigger enforces it for every writer
    assert call_row(sandbox, sr)["spare_issue_note"] == "LOWER TEXT"


# ---------------------------------------------------------------- admin-created engineers
def test_create_engineer_gets_a_full_record_with_a_portal_ecode(sandbox):
    r = edit.create_engineer({"employee_name": "Test New Engineer", "designation": "field engineer", "bank_name": "test bank",
                              "bank_account_no": "123456", "bank_ifsc": "test0001234", "epfo_no": "epfo-1", "esic_no": "esic-1",
                              "uniform_shirt_size": "42 inch", "uniform_trouser_size": "L"}, ED, "127.0.0.1")
    key = r["id"]
    assert key == "TEST NEW ENGINEER"
    eng = one(sandbox, "SELECT * FROM portal_engineer WHERE engineer_key = %s", (key,))
    assert eng["source"] == "PORTAL" and eng["ecode"].startswith("PORTAL-")
    row = one(sandbox, "SELECT * FROM cipl_employee WHERE ecode = %s", (eng["ecode"],))
    assert row["is_on_roster"] == 1 and row["employment_status"] == "ACTIVE"
    assert row["bank_name"] == "TEST BANK" and row["bank_account_no"] == "123456" and row["epfo_no"] == "EPFO-1"
    assert row["uniform_shirt_size"] == "42 INCH" and row["uniform_trouser_size"] == "L"
    # a newly created engineer must be immediately editable through the normal path (active_col = is_on_roster)
    edit.update("engineers", key, {"designation": "senior field engineer"}, {}, ED, "127.0.0.1")
    assert one(sandbox, "SELECT designation FROM cipl_employee WHERE ecode = %s", (eng["ecode"],))["designation"] == "SENIOR FIELD ENGINEER"
    assert one(sandbox, "SELECT count(*) n FROM portal_lock WHERE dataset = 'engineers' AND record_key = %s AND field = %s", (key, itam_locks.CREATED))["n"] == 1
    assert one(sandbox, "SELECT count(*) n FROM portal_audit WHERE dataset = 'engineers' AND record_key = %s AND action = 'CREATE'", (key,))["n"] == 1


def test_create_engineer_rejects_duplicate_name_and_missing_name(sandbox):
    edit.create_engineer({"employee_name": "Dup Engineer"}, ED, "127.0.0.1")
    with pytest.raises(edit.Conflict):
        edit.create_engineer({"employee_name": "dup engineer"}, ED, "127.0.0.1")
    with pytest.raises(edit.Invalid) as e:
        edit.create_engineer({"employee_name": "  "}, ED, "127.0.0.1")
    assert "employee_name" in e.value.fields


def test_create_engineer_ecodes_are_sequential(sandbox):
    a = edit.create_engineer({"employee_name": "Seq Engineer One"}, ED, "127.0.0.1")["id"]
    b = edit.create_engineer({"employee_name": "Seq Engineer Two"}, ED, "127.0.0.1")["id"]
    ea = one(sandbox, "SELECT ecode FROM portal_engineer WHERE engineer_key = %s", (a,))["ecode"]
    eb = one(sandbox, "SELECT ecode FROM portal_engineer WHERE engineer_key = %s", (b,))["ecode"]
    assert int(eb.split("-")[1]) == int(ea.split("-")[1]) + 1


def test_asset_rate_component_and_value_validate_and_save(sandbox):
    key = one(sandbox, "SELECT asset_key FROM asset WHERE is_current = 1 AND record_level = 'ASSET' ORDER BY asset_key LIMIT 1")["asset_key"]
    edit.update("assets", key, {"rate_component": "s.60", "rate_value": "1,295.5"}, {}, ED, "127.0.0.1")
    row = one(sandbox, "SELECT rate_component, rate_value FROM asset WHERE asset_key = %s", (key,))
    assert row["rate_component"] == "S.60" and float(row["rate_value"]) == 1295.50
    for bad in ("abc", "-5", "1e400", "NaN"):
        with pytest.raises(edit.Invalid) as e:
            edit.update("assets", key, {"rate_value": bad}, {}, ED, "127.0.0.1")
        assert "rate_value" in e.value.fields


def test_rates_endpoint_offers_rates_by_type_most_common_first(sandbox, monkeypatch):
    import asyncio
    from portal.app import routes_edit

    class Req:
        def __init__(self, **q):
            self.query_params = q

    def call(**q):
        import json
        return json.loads(asyncio.run(routes_edit.rates(Req(**q))).body)["rates"]
    top = one(sandbox, "SELECT asset_type, rate_component, rate_value FROM asset WHERE is_current = 1 AND rate_component IS NOT NULL AND asset_type = 'PRINTER' "
                       "GROUP BY 1, 2, 3 ORDER BY count(*) DESC LIMIT 1")
    r = call(type="printer")
    assert r and r[0]["component"] == top["rate_component"] and r[0]["value"] == float(top["rate_value"])
    assert len({x["component"] for x in r}) == len(r)                      # one entry per component
    assert call(component=top["rate_component"])[0]["value"] == float(top["rate_value"])
    assert call(type="NO SUCH TYPE") == []
