"""Third UX/permission batch (2026-09-22): Team Lead/SI auto-fill, part-no dropdown by asset, inward/outward defaults,
full engineer self-edit, PM dashboard scoped to the logged-in engineer, Data integrity admin-only.

  cd portal && .venv\\Scripts\\python -m pytest -q tests/test_ux_batch3.py
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
from portal.app import config, db, edit, pm  # noqa: E402
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


# ---------------------------------------------------------------- Team Lead/SI + inward/outward defaults
def test_inward_outward_defaults(sandbox):
    s = edit.schema()
    inward = s["inward"]["defaults"]
    assert inward["inward_date"] == dt.date.today().isoformat()
    assert inward["received_date"] == dt.date.today().isoformat()
    tl = one(sandbox, "SELECT employee_name FROM cipl_employee WHERE employment_status='ACTIVE' AND designation ILIKE '%%team lead%%' AND designation ILIKE '%%si%%' ORDER BY employee_name LIMIT 1")
    if tl:
        assert inward["received_by"] == tl["employee_name"]
    outward = s["outward"]["defaults"]
    assert outward["outward_date"] == dt.date.today().isoformat()
    assert outward["sent_date"] == dt.date.today().isoformat()
    assert outward["sent_location"] == "CIPL WARE HOUSE" and outward["location"] == "NOIDA"


def test_part_no_fields_marked_suggest_depends_on_asset(sandbox):
    s = edit.schema()
    call_field = next(f for f in s["calls"]["fields"] if f["key"] == "part_required")
    inward_field = next(f for f in s["inward"]["fields"] if f["key"] == "part_no")
    for f in (call_field, inward_field):
        assert f["kind"] == "suggest" and f["lookup"] == "part_no" and f["depends_on"] == "asset_key"


def test_part_no_lookup_scoped_to_asset(sandbox, monkeypatch):
    row = one(sandbox, "SELECT asset_key, part_no FROM spare_inward WHERE is_current=1 AND part_no IS NOT NULL LIMIT 1")
    if not row:
        pytest.skip("no inward lines with a part_no in the data")
    as_user(monkeypatch, "ADMIN")
    with TestClient(app) as c:
        r = c.get("/api/edit/assets/lookup", params={"field": "part_no", "filter": row["asset_key"]})
        assert r.status_code == 200 and row["part_no"] in r.json()["values"]
        expected = {x["part_no"] for x in sandbox.execute("SELECT DISTINCT part_no FROM spare_inward WHERE is_current=1 AND part_no IS NOT NULL AND asset_key=%s", (row["asset_key"],)).fetchall() for x in [{"part_no": x[0]}]}
        assert set(r.json()["values"]) == expected
        assert c.get("/api/edit/assets/lookup", params={"field": "not_a_field"}).status_code == 400


# ---------------------------------------------------------------- engineer full self-edit
def test_engineer_edits_any_field_on_own_record_via_http(sandbox, monkeypatch):
    eng = one(sandbox, "SELECT engineer_key, ecode FROM portal_engineer WHERE ecode IS NOT NULL ORDER BY engineer_key LIMIT 1")
    as_user(monkeypatch, "USER", engineer_key=eng["engineer_key"])
    with TestClient(app) as c:
        for field, value in (("designation", "NEW DESIGNATION"), ("remarks", "SELF NOTE"), ("skill_category", "US")):
            r = c.post("/api/edit/engineers/update", json={"key": eng["engineer_key"], "changes": {field: value}}, headers=HDR)
            assert r.status_code == 200, r.text


# ---------------------------------------------------------------- PM dashboard scoping
def test_pm_dashboard_scoped_to_engineer(sandbox):
    row = one(sandbox, """SELECT engineer_name, count(*) n FROM asset WHERE is_current=1 AND record_level='ASSET' AND pm_status <> 'NOT_TRACKED'
                          AND engineer_name IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 1""")
    eng, n = row["engineer_name"], row["n"]
    unscoped = pm.dashboard()
    scoped = pm.dashboard(eng)
    assert scoped["kpi"]["scope"] == n
    assert scoped["kpi"]["scope"] < unscoped["kpi"]["scope"]
    assert all(r["label"] == eng for r in scoped["by_engineer"])
    assert scoped["cycle"] == unscoped["cycle"]          # cycle/quarter metadata is never scoped


def test_http_pm_dashboard_scoped_for_user_role(sandbox, monkeypatch):
    eng = one(sandbox, "SELECT engineer_name FROM asset WHERE is_current=1 AND engineer_name IS NOT NULL LIMIT 1")["engineer_name"]
    expected = pm.dashboard(eng)["kpi"]["scope"]
    as_user(monkeypatch, "USER", engineer_key=eng, username="PMUSER")
    with TestClient(app) as c:
        d = c.get("/api/pm/dashboard").json()
        assert d["kpi"]["scope"] == expected


# ---------------------------------------------------------------- Data integrity admin-only
def test_data_integrity_admin_only(sandbox, monkeypatch):
    as_user(monkeypatch, "USER")
    with TestClient(app) as c:
        assert c.get("/api/integrity").status_code == 403
    as_user(monkeypatch, "ADMIN")
    with TestClient(app) as c:
        assert c.get("/api/integrity").status_code == 200
