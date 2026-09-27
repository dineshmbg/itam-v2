"""New-asset form support: cascading suggestion lookups (Class->Type, Make->Model, OS family->OS) and the best-effort e-mail
notification to the assigned engineer on create. Runs inside one rolled-back database transaction.

  cd portal && .venv\\Scripts\\python -m pytest -q tests/test_new_asset_cascade.py
"""
import contextlib
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
from portal.app import config, db, mailer  # noqa: E402
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


def test_lookup_endpoint_returns_distinct_values(sandbox, monkeypatch):
    as_user(monkeypatch, "ADMIN")
    with TestClient(app) as c:
        r = c.get("/api/edit/assets/lookup", params={"field": "make"})
        assert r.status_code == 200 and r.json()["values"]
        cls = one(sandbox, "SELECT asset_class FROM asset WHERE is_current=1 AND asset_type IS NOT NULL LIMIT 1")["asset_class"]
        r2 = c.get("/api/edit/assets/lookup", params={"field": "asset_type", "filter": cls})
        assert r2.status_code == 200
        expected = {row[0] for row in sandbox.execute("SELECT DISTINCT asset_type FROM asset WHERE is_current=1 AND asset_class=%s", (cls,)).fetchall()}
        assert set(r2.json()["values"]) == expected
        assert c.get("/api/edit/assets/lookup", params={"field": "not_a_real_field"}).status_code == 400


def test_lookup_model_filtered_by_make(sandbox, monkeypatch):
    as_user(monkeypatch, "ADMIN")
    mk = one(sandbox, "SELECT make FROM asset WHERE is_current=1 AND make IS NOT NULL AND model IS NOT NULL LIMIT 1")["make"]
    expected = {r[0] for r in sandbox.execute("SELECT DISTINCT model FROM asset WHERE is_current=1 AND make=%s AND model IS NOT NULL", (mk,)).fetchall()}
    with TestClient(app) as c:
        r = c.get("/api/edit/assets/lookup", params={"field": "model", "filter": mk})
        assert set(r.json()["values"]) == expected


def test_new_asset_notifies_assigned_engineer_when_smtp_enabled(sandbox, monkeypatch):
    eng = one(sandbox, "SELECT engineer_key FROM portal_engineer WHERE ecode IS NOT NULL LIMIT 1")["engineer_key"]
    sent = {}
    monkeypatch.setattr(mailer, "get_smtp", lambda: {"enabled": True})
    monkeypatch.setattr(mailer, "engineer_email", lambda key: "engineer@example.com")

    def fake_send(to, subject, text, html_body=None, **kw):
        sent["to"], sent["subject"] = to, subject
    monkeypatch.setattr(mailer, "send", fake_send)
    as_user(monkeypatch, "ADMIN")
    with TestClient(app) as c:
        r = c.post("/api/edit/assets/create", json={"values": {"asset_key": "TEST-NOTIFY-1", "asset_class": "DESKTOP", "asset_type": "DESKTOP", "engineer_name": eng}}, headers=HDR)
        assert r.status_code == 200, r.text
    assert sent.get("to") == "engineer@example.com" and "TEST-NOTIFY-1" in sent.get("subject", "")


def test_new_asset_creation_still_succeeds_if_mail_send_fails(sandbox, monkeypatch):
    eng = one(sandbox, "SELECT engineer_key FROM portal_engineer WHERE ecode IS NOT NULL LIMIT 1")["engineer_key"]
    monkeypatch.setattr(mailer, "get_smtp", lambda: {"enabled": True})
    monkeypatch.setattr(mailer, "engineer_email", lambda key: "engineer@example.com")

    def boom(*a, **kw):
        raise mailer.MailError("smtp exploded")
    monkeypatch.setattr(mailer, "send", boom)
    as_user(monkeypatch, "ADMIN")
    with TestClient(app) as c:
        r = c.post("/api/edit/assets/create", json={"values": {"asset_key": "TEST-NOTIFY-2", "asset_class": "DESKTOP", "asset_type": "DESKTOP", "engineer_name": eng}}, headers=HDR)
        assert r.status_code == 200, r.text
    assert one(sandbox, "SELECT is_current FROM asset WHERE asset_key = 'TEST-NOTIFY-2'")["is_current"] == 1


def test_new_asset_without_engineer_sends_no_mail(sandbox, monkeypatch):
    called = []
    monkeypatch.setattr(mailer, "get_smtp", lambda: (called.append("get_smtp") or {"enabled": True}))
    as_user(monkeypatch, "ADMIN")
    with TestClient(app) as c:
        r = c.post("/api/edit/assets/create", json={"values": {"asset_key": "TEST-NOTIFY-3", "asset_class": "DESKTOP", "asset_type": "DESKTOP"}}, headers=HDR)
        assert r.status_code == 200, r.text
    assert called == []          # no engineer assigned - never even checks whether e-mail is set up
