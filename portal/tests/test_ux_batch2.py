"""Second large UX/permission batch (2026-09-22, same day): new-call defaults, admin-only registers (Calls/Inward/Outward/RMA,
Engineers dashboard, Change log, Cycles and snapshots), and per-engineer scoped Assets/Call tracker dashboards.

  cd portal && .venv\\Scripts\\python -m pytest -q tests/test_ux_batch2.py
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
from portal.app import config, dashboards, db, edit  # noqa: E402
from portal.app.main import app  # noqa: E402
from tests.test_edit import as_user  # noqa: E402

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


# ---------------------------------------------------------------- new-record defaults
def test_new_call_defaults(sandbox):
    s = edit.schema()
    d = s["calls"]["defaults"]
    assert d["cipl_call_date"] == dt.date.today().isoformat()
    assert d["zone"] == "WEST" and d["site"] == "ONGC ANKLESHWAR"
    # site_incharge is the *name* of whoever currently holds the Team Leader/SI designation, not the literal designation text
    expected = one(sandbox, "SELECT employee_name FROM cipl_employee WHERE employment_status='ACTIVE' AND designation ILIKE '%%team lead%%' AND designation ILIKE '%%si%%' ORDER BY employee_name LIMIT 1")
    assert d.get("site_incharge") == (expected["employee_name"] if expected else None)


def test_new_inward_outward_date_defaults(sandbox):
    s = edit.schema()
    assert s["inward"]["defaults"]["inward_date"] == dt.date.today().isoformat()
    assert s["outward"]["defaults"]["outward_date"] == dt.date.today().isoformat()


def test_calls_asset_field_marked_fill_from_asset(sandbox):
    s = edit.schema()
    f = next(x for x in s["calls"]["fields"] if x["key"] == "asset_key")
    assert f.get("fill_from_asset") is True


# ---------------------------------------------------------------- admin-only registers
ADMIN_ONLY_ROUTES_GET = ["/api/registers/calls", "/api/registers/inward", "/api/registers/outward", "/api/registers/rma",
                         "/api/dash/engineers", "/api/audit", "/api/pm/cycles"]


def test_admin_only_registers_blocked_for_user_role(sandbox, monkeypatch):
    call = one(sandbox, "SELECT sr_id FROM svc_call WHERE is_current=1 LIMIT 1")
    as_user(monkeypatch, "USER")
    with TestClient(app) as c:
        for path in ADMIN_ONLY_ROUTES_GET:
            r = c.get(path)
            assert r.status_code == 403, f"{path}: {r.status_code}"
        assert c.get(f"/api/registers/calls/{call['sr_id']}").status_code == 403
        assert c.post("/api/edit/calls/create", json={"values": {}}, headers=HDR).status_code == 403
        assert c.post("/api/edit/calls/update", json={"key": call["sr_id"], "changes": {}}, headers=HDR).status_code == 403
        assert c.post("/api/edit/calls/archive", json={"key": call["sr_id"], "reason": "x"}, headers=HDR).status_code == 403


def test_admin_only_registers_reachable_for_admin(sandbox, monkeypatch):
    as_user(monkeypatch, "ADMIN")
    with TestClient(app) as c:
        for path in ADMIN_ONLY_ROUTES_GET:
            r = c.get(path)
            assert r.status_code == 200, f"{path}: {r.status_code} {r.text}"


def test_search_excludes_admin_only_datasets_for_user(sandbox, monkeypatch):
    """A User's search never returns the administrator-only registers. (It may well return fewer OTHER groups than an administrator -
    e.g. assets - because a User only sees their own; that is row scoping, tested elsewhere, not what this test is about.)"""
    call = one(sandbox, "SELECT sr_id, problem_description FROM svc_call WHERE is_current=1 AND problem_description IS NOT NULL LIMIT 1")
    q = call["problem_description"][:12]
    as_user(monkeypatch, "ADMIN")
    with TestClient(app) as c:
        admin_groups = {g["dataset"] for g in c.get("/api/search", params={"q": q}).json()["groups"]}
    as_user(monkeypatch, "USER", username="SEARCHUSER")
    with TestClient(app) as c:
        user_groups = {g["dataset"] for g in c.get("/api/search", params={"q": q}).json()["groups"]}
    assert "calls" in admin_groups                                    # the search term was taken from a call, so an administrator finds it
    assert not (user_groups & {"calls", "inward", "outward", "rma"})


# ---------------------------------------------------------------- scoped dashboards
def test_assets_dashboard_scoped_to_engineer(sandbox):
    row = one(sandbox, "SELECT engineer_name, count(*) n FROM asset WHERE is_current=1 AND record_level='ASSET' AND engineer_name IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 1")
    eng, n = row["engineer_name"], row["n"]
    unscoped = dashboards.assets()
    scoped = dashboards.assets(eng)
    assert scoped["kpi"]["assets"] == n
    assert scoped["kpi"]["assets"] < unscoped["kpi"]["assets"]
    assert all(r["label"] == eng for r in scoped["by_engineer"])


def test_calls_dashboard_scoped_to_engineer(sandbox):
    row = one(sandbox, "SELECT engineer, count(*) n FROM svc_call WHERE is_current=1 AND engineer IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 1")
    eng, n = row["engineer"], row["n"]
    unscoped = dashboards.calls()
    scoped = dashboards.calls(eng)
    assert scoped["kpi"]["total"] == n
    assert scoped["kpi"]["total"] < unscoped["kpi"]["total"]
    assert all(r["label"] == eng for r in scoped["by_engineer"])


def test_http_dash_assets_and_calls_scoped_for_user_role(sandbox, monkeypatch):
    eng = one(sandbox, "SELECT engineer_name FROM asset WHERE is_current=1 AND engineer_name IS NOT NULL LIMIT 1")["engineer_name"]
    expected = dashboards.assets(eng)["kpi"]["assets"]
    as_user(monkeypatch, "USER", engineer_key=eng, username="DASHUSER")
    with TestClient(app) as c:
        d = c.get("/api/dash/assets").json()
        assert d["kpi"]["assets"] == expected


# ---------------------------------------------------------------- engineer card privacy already covered in test_tools.py
