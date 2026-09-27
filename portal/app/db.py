"""PostgreSQL access. The portal opens every connection READ ONLY: it can never modify inventory data.

The password is read from the standard pgpass file (never stored in the project):
PGPASSWORD, PGPASSFILE, ~/.ongc_pgpass, or %APPDATA%/postgresql/pgpass.conf.
"""
import contextlib
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

_pool = None


def password():
    if os.environ.get("PGPASSWORD"):
        return os.environ["PGPASSWORD"]
    want = [config.PG["host"], str(config.PG["port"]), config.PG["dbname"], config.PG["user"]]
    for cand in (os.environ.get("PGPASSFILE"), Path.home() / ".ongc_pgpass", Path(os.environ.get("APPDATA", "")) / "postgresql" / "pgpass.conf"):
        f = Path(cand) if cand else None
        if not f or not f.is_file():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split(":", 4)
            if len(parts) == 5 and all(p in ("*", w) for p, w in zip(parts[:4], want)):
                return parts[4]
    return None


def conninfo():
    return psycopg.conninfo.make_conninfo(**config.PG, password=password(), connect_timeout=8,
                                          options=f"-c default_transaction_read_only=on -c statement_timeout={config.STATEMENT_TIMEOUT_MS} -c application_name=itam-portal")


def open_pool():
    global _pool
    _pool = ConnectionPool(conninfo(), min_size=config.POOL_MIN, max_size=config.POOL_MAX, open=False,
                           kwargs={"autocommit": True, "row_factory": dict_row}, name="itam")
    _pool.open(wait=True, timeout=15)
    return _pool


def close_pool():
    global _pool
    if _pool:
        _pool.close()
        _pool = None


def pool():
    if _pool is None:
        open_pool()
    return _pool


def query(sql, params=None):
    with pool().connection() as con:
        return con.execute(sql, params).fetchall()


def one(sql, params=None):
    with pool().connection() as con:
        return con.execute(sql, params).fetchone()


@contextlib.contextmanager
def write():
    """The ONLY write path used by the running portal: one short transaction per edit (commit on success, rollback on any error).
    Regular reads keep using the read-only pool."""
    con = psycopg.connect(psycopg.conninfo.make_conninfo(**config.PG, password=password(), connect_timeout=8,
                                                         options=f"-c statement_timeout={config.STATEMENT_TIMEOUT_MS} -c lock_timeout=5000 -c application_name=itam-portal-edit"))
    try:
        with con.transaction():
            yield con
    finally:
        con.close()


def admin_connection():
    """Writable connection - ONLY for db/setup.py (indexes, triggers, engineer table)."""
    return psycopg.connect(psycopg.conninfo.make_conninfo(**config.PG, password=password(), connect_timeout=8), autocommit=True, row_factory=dict_row)
