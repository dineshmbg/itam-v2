"""Per-user saved column layout (show/hide, reorder) for a register. Runs inside one rolled-back database transaction.

  cd portal && .venv\\Scripts\\python -m pytest -q tests/test_views_pref.py
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
from portal.app import config, db, views_pref  # noqa: E402
from portal.app.main import app  # noqa: E402
from tests.test_edit import as_user  # noqa: E402

HDR = {"X-Requested-With": "itam-portal", "Content-Type": "application/json"}


@pytest.fixture()
def sandbox(monkeypatch):
    con = psycopg.connect(psycopg.conninfo.make_conninfo(**config.PG, password=db.password(), connect_timeout=8))
    itam_locks.ensure_tables(con)
    for stmt in views_pref.DDL:
        con.execute(stmt)

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


def test_get_is_none_until_saved(sandbox):
    assert views_pref.get(42, "assets") is None
    views_pref.save(42, "assets", [{"key": "asset_key", "visible": True}, {"key": "make", "visible": False}])
    assert views_pref.get(42, "assets") == [{"key": "asset_key", "visible": True}, {"key": "make", "visible": False}]


def test_save_is_scoped_per_user_and_per_dataset(sandbox):
    views_pref.save(1, "assets", [{"key": "asset_key", "visible": True}])
    views_pref.save(2, "assets", [{"key": "make", "visible": True}])
    views_pref.save(1, "calls", [{"key": "sr_id", "visible": True}])
    assert views_pref.get(1, "assets") == [{"key": "asset_key", "visible": True}]
    assert views_pref.get(2, "assets") == [{"key": "make", "visible": True}]
    assert views_pref.get(1, "calls") == [{"key": "sr_id", "visible": True}]


def test_save_twice_replaces_the_view(sandbox):
    views_pref.save(1, "assets", [{"key": "asset_key", "visible": True}])
    views_pref.save(1, "assets", [{"key": "make", "visible": True}, {"key": "model", "visible": False}])
    assert views_pref.get(1, "assets") == [{"key": "make", "visible": True}, {"key": "model", "visible": False}]


def test_reset_deletes_the_saved_view(sandbox):
    views_pref.save(1, "assets", [{"key": "asset_key", "visible": True}])
    views_pref.reset(1, "assets")
    assert views_pref.get(1, "assets") is None


def test_at_least_one_column_must_stay_visible(sandbox):
    with pytest.raises(ValueError):
        views_pref.save(1, "assets", [{"key": "asset_key", "visible": False}, {"key": "make", "visible": False}])


def test_empty_or_duplicate_keys_are_dropped(sandbox):
    out = views_pref.save(1, "assets", [{"key": "asset_key", "visible": True}, {"key": "", "visible": True}, {"key": "asset_key", "visible": False}])
    assert out == [{"key": "asset_key", "visible": True}]        # the duplicate is dropped, keeping the first (visible) occurrence


def test_http_view_get_save_reset_round_trip(sandbox, monkeypatch):
    as_user(monkeypatch, "ADMIN")
    with TestClient(app) as c:
        assert c.get("/api/views/assets").json() == {"columns": None}
        r = c.post("/api/views/assets/save", json={"columns": [{"key": "asset_key", "visible": True}, {"key": "make", "visible": False}]}, headers=HDR)
        assert r.status_code == 200, r.text
        assert c.get("/api/views/assets").json()["columns"] == [{"key": "asset_key", "visible": True}, {"key": "make", "visible": False}]
        r2 = c.post("/api/views/assets/save", json={"columns": [{"key": "make", "visible": False}]}, headers=HDR)
        assert r2.status_code == 400          # nothing left visible
        r3 = c.post("/api/views/assets/reset", json={}, headers=HDR)
        assert r3.status_code == 200
        assert c.get("/api/views/assets").json() == {"columns": None}
        assert c.get("/api/views/nosuchdataset").status_code == 400
