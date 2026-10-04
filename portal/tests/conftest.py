import contextlib
import datetime as _dt
import os
import sys
from pathlib import Path

os.environ.setdefault("PORTAL_SCHEDULER", "0")     # background jobs must not run during tests

import psycopg  # noqa: E402
import pytest  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


@pytest.fixture(autouse=True)
def _fresh_ip_throttle():
    """The per-address failed-sign-in counter is process-wide; every test starts (and leaves) it empty."""
    from portal.app import auth
    auth.reset_ip_throttle()
    yield
    auth.reset_ip_throttle()


@pytest.fixture()
def box(monkeypatch):
    """One database transaction shared by the code under test, rolled back at the end: the real data is never changed."""
    from portal.app import auth, config, db, mailer, pm, pmwo, reports, importer, backup, views_pref, lifecycle   # noqa: F401
    import itam_locks
    con = psycopg.connect(psycopg.conninfo.make_conninfo(**config.PG, password=db.password(), connect_timeout=8))
    itam_locks.ensure_tables(con)
    auth.ensure_tables(con)
    for stmt in backup.DDL + pm.DDL + pmwo.DDL + mailer.DDL + importer.DDL + reports.DDL + views_pref.DDL + lifecycle.DDL:
        con.execute(stmt)
    con.commit()
    con.execute("SELECT 1")        # open the transaction: every write() below is then a savepoint of it and is rolled back, never committed
    auth._settings_cache["value"] = None

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
        auth._settings_cache["value"] = None


def fake_user(monkeypatch, role="ADMIN", username="TEST", engineer_key=None):
    from portal.app import web
    user = {"user_id": 0, "username": username, "display_name": username, "role": role, "state": "ok", "email": "test@example.com", "active": True, "totp_enabled": False,
            "must_change": False, "locked_until": None, "last_login_at": None, "created_at": _dt.datetime.now(_dt.timezone.utc), "engineer_key": engineer_key}

    async def fake(request):
        return user
    monkeypatch.setattr(web, "current_user", fake)
    return user


HDR = {"X-Requested-With": "itam-portal", "Content-Type": "application/json"}
