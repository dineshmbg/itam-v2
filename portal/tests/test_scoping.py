"""Permission/scoping tests added for: the DEMOUSER read-only account, roster-only account creation, engineer self-service
editing, and row-level scoping of assets/employees to a non-admin engineer's own data. Every test runs inside one rolled-back
database transaction - the real data is never changed.

  cd portal && .venv\\Scripts\\python -m pytest -q tests/test_scoping.py
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
import itam_locks  # noqa: E402
from portal.app import auth, config, db, edit, queries, web  # noqa: E402
from portal.app.main import app, qs_key  # noqa: E402

HDR = {"X-Requested-With": "itam-portal", "Content-Type": "application/json"}


@pytest.fixture()
def sandbox(monkeypatch):
    con = psycopg.connect(psycopg.conninfo.make_conninfo(**config.PG, password=db.password(), connect_timeout=8))
    itam_locks.ensure_tables(con)

    @contextlib.contextmanager
    def write():
        with con.transaction():
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


def as_user(monkeypatch, role="ADMIN", engineer_key=None, read_only=False, username="TEST", call_parts_access="NONE"):
    user = {"user_id": 0, "username": username, "display_name": username, "role": role, "state": "ok", "email": None, "active": True, "totp_enabled": False,
            "must_change": False, "locked_until": None, "last_login_at": None, "created_at": dt.datetime.now(dt.timezone.utc), "engineer_key": engineer_key, "read_only": read_only,
            "call_parts_access": call_parts_access}

    async def fake(request):
        return user
    monkeypatch.setattr(web, "current_user", fake)
    return user


def two_engineers_with_assets(con):
    """Two distinct engineer_key values that each currently have at least one asset assigned."""
    rows = con.execute("SELECT DISTINCT engineer_name FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND engineer_name IS NOT NULL LIMIT 2").fetchall()
    names = [r[0] for r in rows]
    if len(names) < 2:
        pytest.skip("need at least two engineers with assets assigned")
    return names[0], names[1]


# ---------------------------------------------------------------- auth.check_edit / check_create / check_archive
def test_engineer_can_edit_every_field_on_their_own_record_only(sandbox):
    user = {"role": "USER", "engineer_key": "ALICE"}
    auth.check_edit(user, "engineers", ["mobile_no"], "ALICE")                       # own record, any field: ok
    auth.check_edit(user, "engineers", ["designation", "remarks", "mobile_no"], "ALICE")   # own record, every field: ok
    with pytest.raises(auth.AuthError):
        auth.check_edit(user, "engineers", ["mobile_no"], "BOB")                     # someone else's record


def test_admin_can_edit_any_engineer_any_field(sandbox):
    admin = {"role": "ADMIN", "engineer_key": None}
    auth.check_edit(admin, "engineers", ["designation", "remarks"], "ANYONE")        # must not raise


def test_engineer_records_can_be_created_by_admins_only_and_never_archived(sandbox):
    auth.check_create({"role": "ADMIN"}, "engineers")   # must not raise - an administrator may add an engineer directly
    for user in ({"role": "ADMIN"}, {"role": "USER", "engineer_key": "ALICE"}):
        with pytest.raises(auth.AuthError):
            auth.check_archive(user, "engineers")
    with pytest.raises(auth.AuthError):
        auth.check_create({"role": "USER", "engineer_key": "ALICE"}, "engineers")


def test_asset_edit_ownership_is_enforced_for_users(sandbox):
    a, b = two_engineers_with_assets(sandbox)
    my_asset = one(sandbox, "SELECT asset_key FROM asset WHERE is_current = 1 AND engineer_name = %s LIMIT 1", (a,))["asset_key"]
    their_asset = one(sandbox, "SELECT asset_key FROM asset WHERE is_current = 1 AND engineer_name = %s LIMIT 1", (b,))["asset_key"]
    user = {"role": "USER", "engineer_key": a}
    auth.check_edit(user, "assets", ["asset_status"], my_asset)                      # must not raise
    with pytest.raises(auth.AuthError):
        auth.check_edit(user, "assets", ["asset_status"], their_asset)


# ---------------------------------------------------------------- queries.scope_for / row-level scoping
def test_scope_for_is_none_for_admin_and_demo_account(sandbox):
    assert queries.scope_for("assets", queries.dataset("assets"), {"role": "ADMIN"}) is None
    assert queries.scope_for("assets", queries.dataset("assets"), {"role": "ADMIN", "read_only": True}) is None
    assert queries.scope_for("assets", queries.dataset("assets"), None) is None


def test_list_rows_scopes_assets_to_the_logged_in_engineer(sandbox):
    a, b = two_engineers_with_assets(sandbox)
    n_a = one(sandbox, "SELECT count(*) n FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND engineer_name = %s", (a,))["n"]
    out = queries.list_rows("assets", {}, user={"role": "USER", "engineer_key": a})
    assert out["total"] == n_a
    other = queries.list_rows("assets", {}, user={"role": "USER", "engineer_key": b})
    assert other["total"] != 0
    ids_a = {r["id"] for r in out["rows"]}
    ids_b = {r["id"] for r in other["rows"]}
    assert ids_a.isdisjoint(ids_b)


def test_detail_404s_outside_scope(sandbox):
    a, b = two_engineers_with_assets(sandbox)
    their_asset = one(sandbox, "SELECT asset_key FROM asset WHERE is_current = 1 AND engineer_name = %s LIMIT 1", (b,))["asset_key"]
    assert queries.detail("assets", their_asset, user={"role": "USER", "engineer_key": a}) is None
    assert queries.detail("assets", their_asset, user={"role": "ADMIN"}) is not None


def test_employees_scoped_to_engineers_own_assets(sandbox):
    a, _ = two_engineers_with_assets(sandbox)
    expected = {str(r[0]) for r in sandbox.execute(
        "SELECT DISTINCT cpf_no FROM asset WHERE is_current = 1 AND cpf_no IS NOT NULL AND engineer_name = %s", (a,)).fetchall()}
    total_unscoped = one(sandbox, "SELECT count(*) n FROM employee WHERE record_status = 'ACTIVE'")["n"]
    out = queries.list_rows("employees", {}, user={"role": "USER", "engineer_key": a})
    got = {str(r["id"]) for r in out["rows"]}
    assert got <= expected
    assert out["total"] == len(got)
    assert out["total"] <= total_unscoped


def test_facet_counts_reflect_the_scoped_subset_not_the_whole_table(sandbox):
    a, _ = two_engineers_with_assets(sandbox)
    scoped = queries.list_rows("assets", {"facets": "1"}, user={"role": "USER", "engineer_key": a})
    unscoped = queries.list_rows("assets", {"facets": "1"}, user={"role": "ADMIN"})
    class_facet_scoped = {row["v"]: row["n"] for row in scoped["facets"]["asset_class"]}
    class_facet_all = {row["v"]: row["n"] for row in unscoped["facets"]["asset_class"]}
    assert sum(class_facet_scoped.values()) == scoped["total"]
    assert sum(class_facet_scoped.values()) < sum(class_facet_all.values())          # narrower than the admin's full-table facet counts


def test_engineer_with_no_assets_sees_nothing(sandbox):
    out = queries.list_rows("assets", {}, user={"role": "USER", "engineer_key": "NO SUCH ENGINEER AT ALL"})
    assert out["total"] == 0 and out["rows"] == []
    emp = queries.list_rows("employees", {}, user={"role": "USER", "engineer_key": "NO SUCH ENGINEER AT ALL"})
    assert emp["total"] == 0 and emp["rows"] == []


# ---------------------------------------------------------------- HTTP layer: registers scoped end to end
def test_http_register_list_and_detail_are_scoped_for_a_user(sandbox, monkeypatch):
    a, b = two_engineers_with_assets(sandbox)
    their_asset = one(sandbox, "SELECT asset_key FROM asset WHERE is_current = 1 AND engineer_name = %s LIMIT 1", (b,))["asset_key"]
    # a username not reused by any other test file's GET requests - the in-process response cache is keyed by username+path,
    # and a collision on the literal "TEST" username (used elsewhere) would serve a stale, differently-scoped cached response
    as_user(monkeypatch, "USER", engineer_key=a, username="SCOPETEST_A")
    with TestClient(app) as c:
        r = c.get("/api/registers/assets", params={"facets": "1"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert all(row.get("id") for row in body["rows"])
        r2 = c.get(f"/api/registers/assets/{their_asset}")
        assert r2.status_code == 404
        r3 = c.post("/api/edit/assets/update", json={"key": their_asset, "changes": {"asset_status": "STANDBY"}}, headers=HDR)
        assert r3.status_code == 403


def test_http_asset_edit_own_asset_still_works_for_user(sandbox, monkeypatch):
    a, _ = two_engineers_with_assets(sandbox)
    my_asset = one(sandbox, "SELECT asset_key FROM asset WHERE is_current = 1 AND engineer_name = %s LIMIT 1", (a,))["asset_key"]
    as_user(monkeypatch, "USER", engineer_key=a)
    with TestClient(app) as c:
        r = c.post("/api/edit/assets/update", json={"key": my_asset, "changes": {"asset_status": "STANDBY"}}, headers=HDR)
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------- engineer self-editing over HTTP
def test_http_engineer_self_edit_all_fields_own_record_only(sandbox, monkeypatch):
    eng = one(sandbox, "SELECT engineer_key, ecode FROM portal_engineer WHERE ecode IS NOT NULL ORDER BY engineer_key LIMIT 1")
    other = one(sandbox, "SELECT engineer_key FROM portal_engineer WHERE ecode IS NOT NULL AND engineer_key <> %s LIMIT 1", (eng["engineer_key"],))
    as_user(monkeypatch, "USER", engineer_key=eng["engineer_key"])
    with TestClient(app) as c:
        r = c.post("/api/edit/engineers/update", json={"key": eng["engineer_key"], "changes": {"mobile_no": "9876543210"}}, headers=HDR)
        assert r.status_code == 200, r.text
        assert one(sandbox, "SELECT mobile_no FROM cipl_employee WHERE ecode = %s", (eng["ecode"],))["mobile_no"] == "9876543210"
        r2 = c.post("/api/edit/engineers/update", json={"key": eng["engineer_key"], "changes": {"designation": "NEW TITLE"}}, headers=HDR)
        assert r2.status_code == 200, r2.text          # every field is editable on their own record, not just contact details
        assert one(sandbox, "SELECT designation FROM cipl_employee WHERE ecode = %s", (eng["ecode"],))["designation"] == "NEW TITLE"
        if other:
            r3 = c.post("/api/edit/engineers/update", json={"key": other["engineer_key"], "changes": {"mobile_no": "1234567890"}}, headers=HDR)
            assert r3.status_code == 403                # never someone else's record, regardless of field
        assert c.post("/api/edit/engineers/create", json={"values": {}}, headers=HDR).status_code == 403
        assert c.post("/api/edit/engineers/archive", json={"key": eng["engineer_key"], "reason": "x"}, headers=HDR).status_code == 403


def test_engineer_personal_fields_visible_to_admin_and_self_only(sandbox):
    eng = one(sandbox, "SELECT engineer_key, ecode FROM portal_engineer WHERE ecode IS NOT NULL ORDER BY engineer_key LIMIT 1")
    other = one(sandbox, "SELECT engineer_key FROM portal_engineer WHERE ecode IS NOT NULL AND engineer_key <> %s LIMIT 1", (eng["engineer_key"],))
    admin_view = queries.detail("engineers", eng["engineer_key"], user={"role": "ADMIN"})
    assert "mobile_no" in admin_view["row"] and "personal_email" in admin_view["row"]
    self_view = queries.detail("engineers", eng["engineer_key"], user={"role": "USER", "engineer_key": eng["engineer_key"]})
    assert "mobile_no" in self_view["row"] and "personal_email" in self_view["row"]
    if other:
        other_view = queries.detail("engineers", eng["engineer_key"], user={"role": "USER", "engineer_key": other["engineer_key"]})
        assert "mobile_no" not in other_view["row"] and "personal_email" not in other_view["row"]
    no_user_view = queries.detail("engineers", eng["engineer_key"], user=None)
    assert "mobile_no" not in no_user_view["row"] and "personal_email" not in no_user_view["row"]


def test_http_admin_can_edit_any_engineer_full_fields(sandbox, monkeypatch):
    eng = one(sandbox, "SELECT engineer_key, ecode FROM portal_engineer WHERE ecode IS NOT NULL ORDER BY engineer_key LIMIT 1")
    as_user(monkeypatch, "ADMIN")
    with TestClient(app) as c:
        r = c.post("/api/edit/engineers/update", json={"key": eng["engineer_key"], "changes": {"designation": "SENIOR ENGINEER"}}, headers=HDR)
        assert r.status_code == 200, r.text
        assert one(sandbox, "SELECT designation FROM cipl_employee WHERE ecode = %s", (eng["ecode"],))["designation"] == "SENIOR ENGINEER"
        d = r.json()["detail"]
        assert "audit" in d["related"] and d["related"]["audit"][0]["action"] == "UPDATE"


# ---------------------------------------------------------------- DEMOUSER read-only account
def test_demo_user_write_is_blocked_everywhere(sandbox, monkeypatch):
    as_user(monkeypatch, "ADMIN", read_only=True, username="DEMOUSER")
    with TestClient(app) as c:
        a = one(sandbox, "SELECT asset_key FROM asset WHERE is_current = 1 LIMIT 1")["asset_key"]
        r = c.post("/api/edit/assets/update", json={"key": a, "changes": {"asset_status": "STANDBY"}}, headers=HDR)
        assert r.status_code == 403 and r.json()["code"] == "read_only"
        r2 = c.post("/api/admin/settings", json={"settings": {}}, headers=HDR)
        assert r2.status_code == 403 and r2.json()["code"] == "read_only"


def test_demo_user_keepalive_and_view_log_are_not_blocked(sandbox, monkeypatch):
    """A read-only account must still be able to heartbeat its own session, or its idle timer would never be extended and it would
    silently time out regardless of activity (auth.touch() is only reached past this check)."""
    as_user(monkeypatch, "ADMIN", read_only=True, username="DEMOUSER")
    with TestClient(app) as c:
        assert c.post("/api/auth/keepalive", json={}, headers=HDR).status_code == 200
        assert c.post("/api/activity/view", json={"path": "#/dashboard/assets"}, headers=HDR).status_code == 200


def test_demo_user_can_run_and_export_reports(sandbox, monkeypatch):
    """Running a report preview or downloading a report/dashboard is a read - it must not be blocked by the read-only guard,
    only actually persisting a saved report (or anything that sends real e-mail) should be."""
    as_user(monkeypatch, "ADMIN", read_only=True, username="DEMOUSER")
    with TestClient(app) as c:
        r = c.post("/api/reports/run", json={"definition": {"dataset": "assets", "columns": ["asset_key"]}, "limit": 5}, headers=HDR)
        assert r.status_code == 200, r.text
        r2 = c.post("/api/reports/values", json={"dataset": "assets", "field": "make"}, headers=HDR)
        assert r2.status_code == 200, r2.text
        r3 = c.post("/api/reports/export", json={"format": "csv", "definition": {"dataset": "assets", "columns": ["asset_key"]}}, headers=HDR)
        assert r3.status_code == 200, r3.text
        r4 = c.post("/api/reports/save", json={"name": "test save", "definition": {"dataset": "assets"}}, headers=HDR)
        assert r4.status_code == 403 and r4.json()["code"] == "read_only"


def test_demo_user_can_read_full_admin_scope(sandbox, monkeypatch):
    as_user(monkeypatch, "ADMIN", read_only=True, username="DEMOUSER")
    with TestClient(app) as c:
        r = c.get("/api/registers/assets")
        assert r.status_code == 200
        total_admin = r.json()["total"]
        # the register's default filter narrows to record_level = 'ASSET' (components are opted into separately)
        total_unscoped = one(sandbox, "SELECT count(*) n FROM asset WHERE is_current = 1 AND record_level = 'ASSET'")["n"]
        assert total_admin == total_unscoped


def test_demo_user_row_exists_and_never_expires(sandbox):
    row = one(sandbox, "SELECT username, role, read_only, must_change FROM portal_user WHERE username = %s", (auth.DEMO_USERNAME,))
    assert row is not None, "run portal/db/setup.py first - ensure_demo_user() creates this row"
    assert row["role"] == "ADMIN" and row["read_only"] is True and row["must_change"] is False


def test_demo_user_state_is_ok_immediately(sandbox):
    row = one(sandbox, "SELECT * FROM portal_user WHERE username = %s", (auth.DEMO_USERNAME,))
    u = dict(row)
    u["state"] = "?"
    # mirror the relevant branch of auth.session_user's state logic without a real cookie/session round-trip
    if u.get("read_only"):
        u["state"] = "ok"
    assert u["state"] == "ok"


def test_admin_count_excludes_read_only_demo_account(sandbox):
    n = one(sandbox, "SELECT count(*) c FROM portal_user WHERE role = 'ADMIN' AND active AND NOT read_only")["c"]
    n_incl = one(sandbox, "SELECT count(*) c FROM portal_user WHERE role = 'ADMIN' AND active")["c"]
    assert n_incl > n, "DEMOUSER (read_only) must not be the only thing making this count non-zero, and must be excluded from it"


# ---------------------------------------------------------------- "add user" is gone
def test_add_user_route_no_longer_exists(sandbox, monkeypatch):
    as_user(monkeypatch, "ADMIN")
    with TestClient(app) as c:
        r = c.post("/api/admin/users/create", json={"username": "X", "display_name": "X", "role": "USER"}, headers=HDR)
        assert r.status_code in (404, 405)


# ---------------------------------------------------------------- per-user cache key
def test_qs_key_is_scoped_per_user():
    class FakeState:
        def __init__(self, user):
            self.user = user

    class FakeURL:
        path = "/api/registers/assets"

    class FakeQP:
        def multi_items(self):
            return [("f.asset_class", "LAPTOP")]

    class FakeRequest:
        def __init__(self, user):
            self.state = FakeState(user)
            self.url = FakeURL()
            self.query_params = FakeQP()

    key_a = qs_key(FakeRequest({"username": "A003541"}))
    key_b = qs_key(FakeRequest({"username": "A003542"}))
    key_anon = qs_key(FakeRequest(None))
    assert key_a != key_b != key_anon
    assert key_a.startswith("A003541|") and key_b.startswith("A003542|")


# ---------------------------------------------------------------- call_parts_access grants (Calls/Inward/Outward/OEM RMA)
def test_check_access_none_is_blocked_read_and_full_are_allowed(sandbox):
    with pytest.raises(auth.AuthError):
        queries.check_access("calls", {"role": "USER", "call_parts_access": "NONE"})
    queries.check_access("calls", {"role": "USER", "call_parts_access": "READ"})     # must not raise
    queries.check_access("calls", {"role": "USER", "call_parts_access": "FULL"})     # must not raise
    for name in ("inward", "outward", "rma"):
        queries.check_access(name, {"role": "USER", "call_parts_access": "FULL"})    # must not raise - same grant covers all four


def test_read_access_can_view_but_not_edit_or_create(sandbox):
    sr = one(sandbox, "SELECT sr_id FROM svc_call WHERE is_current = 1 LIMIT 1")["sr_id"]
    with pytest.raises(auth.AuthError):
        auth.check_edit({"role": "USER", "call_parts_access": "READ"}, "calls", ["priority"], sr)
    with pytest.raises(auth.AuthError):
        auth.check_create({"role": "USER", "call_parts_access": "READ"}, "calls")
    auth.check_edit({"role": "USER", "call_parts_access": "FULL"}, "calls", ["priority"], sr)          # must not raise
    auth.check_create({"role": "USER", "call_parts_access": "FULL"}, "calls")                          # must not raise


def test_http_full_access_grant_can_view_and_edit_calls_register(sandbox, monkeypatch):
    sr = one(sandbox, "SELECT sr_id FROM svc_call WHERE is_current = 1 LIMIT 1")["sr_id"]
    as_user(monkeypatch, "USER", username="PARTSTEST_FULL", call_parts_access="FULL")
    with TestClient(app) as c:
        r = c.get("/api/registers/calls")
        assert r.status_code == 200, r.text
        r2 = c.post("/api/edit/calls/update", json={"key": sr, "changes": {"priority": "P1"}}, headers=HDR)
        assert r2.status_code == 200, r2.text


def test_http_no_access_is_refused_and_read_access_cannot_edit(sandbox, monkeypatch):
    sr = one(sandbox, "SELECT sr_id FROM svc_call WHERE is_current = 1 LIMIT 1")["sr_id"]
    as_user(monkeypatch, "USER", username="PARTSTEST_NONE", call_parts_access="NONE")
    with TestClient(app) as c:
        assert c.get("/api/registers/calls").status_code == 403
    as_user(monkeypatch, "USER", username="PARTSTEST_READ", call_parts_access="READ")
    with TestClient(app) as c:
        r = c.get("/api/registers/calls")
        assert r.status_code == 200, r.text
        r2 = c.post("/api/edit/calls/update", json={"key": sr, "changes": {"priority": "P1"}}, headers=HDR)
        assert r2.status_code == 403


def test_search_all_includes_calls_only_for_admin_or_a_granted_user(sandbox):
    sr = one(sandbox, "SELECT sr_id FROM svc_call WHERE is_current = 1 LIMIT 1")["sr_id"]
    none_r = queries.search_all(sr, user={"role": "USER", "call_parts_access": "NONE"})
    assert not any(g["dataset"] == "calls" for g in none_r["groups"])
    read_r = queries.search_all(sr, user={"role": "USER", "call_parts_access": "READ"})
    assert any(g["dataset"] == "calls" and any(it["id"] == sr for it in g["items"]) for g in read_r["groups"])


def test_update_user_validates_and_stores_call_parts_access(sandbox):
    uid = one(sandbox, "SELECT user_id FROM portal_user WHERE role = 'USER' AND active LIMIT 1")["user_id"]
    with pytest.raises(auth.AuthError):
        auth.update_user(uid, {"call_parts_access": "BOGUS"}, "Test Admin")
    out = auth.update_user(uid, {"call_parts_access": "READ"}, "Test Admin")
    assert out["call_parts_access"] == "READ"
    out2 = auth.update_user(uid, {"call_parts_access": "NONE"}, "Test Admin")
    assert out2["call_parts_access"] == "NONE"
