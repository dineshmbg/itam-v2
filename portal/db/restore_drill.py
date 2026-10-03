"""Restore drill: prove a backup can really be restored, and that what comes back matches the live data.

The portal's own "verify" only checks a backup file's checksum and that pg_restore can read it. This goes the whole way: it restores the
backup into a brand-new SCRATCH database, compares every table's row count with the live database, runs a few sanity queries, and then
drops the scratch database again. The live database is only ever read. The scratch database is named ongc_ank_drill_<timestamp>; the
script refuses to drop anything else.

Needs a PostgreSQL login that may create databases (the portal's own ank_app login may not). On the dev PC that is "postgres" from
~/.ongc_pgpass; elsewhere pass --admin-user and set DRILL_ADMIN_PASSWORD. Run it monthly, and before an important release.

  python portal/db/restore_drill.py                      # takes a fresh dump of the live data and restores THAT (tests the whole pipeline)
  python portal/db/restore_drill.py --backup backups/ongc_ank_20261002_020000_auto.dump      # restores an existing portal backup instead
  python portal/db/restore_drill.py --keep               # leave the scratch database in place for inspection (you drop it)

Exit code 0 = restore worked and matches, 1 = it did not.
"""
import argparse
import datetime as dt
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

from portal.app import backup, config, db  # noqa: E402

PREFIX = "ongc_ank_drill_"
# Tables the running portal writes to all the time: a small difference is normal between the moment of the dump and the moment we count.
VOLATILE = {"portal_activity", "portal_session", "portal_audit", "portal_backup", "notify_log", "portal_job", "portal_import_job"}


class DrillError(Exception):
    pass


def admin_password(user):
    if os.environ.get("DRILL_ADMIN_PASSWORD"):
        return os.environ["DRILL_ADMIN_PASSWORD"]
    want = [config.PG["host"], str(config.PG["port"]), "postgres", user]
    for cand in (os.environ.get("PGPASSFILE"), Path.home() / ".ongc_pgpass"):
        f = Path(cand) if cand else None
        if f and f.is_file():
            for line in f.read_text(encoding="utf-8").splitlines():
                parts = line.strip().split(":", 4)
                if len(parts) == 5 and all(p in ("*", w) for p, w in zip(parts[:4], want)):
                    return parts[4]
    return None


def _connect(dbname, user, password, autocommit=False):
    return psycopg.connect(host=config.PG["host"], port=config.PG["port"], dbname=dbname, user=user, password=password, connect_timeout=8, autocommit=autocommit)


def _counts(con):
    with con.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1")
        names = [r[0] for r in cur.fetchall()]
        out = {}
        for n in names:
            cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(n)))
            out[n] = cur.fetchone()[0]
    return out


def run(backup_file=None, admin_user="postgres", keep=False, say=print):
    pw = admin_password(admin_user)
    if not pw:
        raise DrillError(f"No password for the admin login '{admin_user}': add it to ~/.ongc_pgpass or set DRILL_ADMIN_PASSWORD.")
    scratch = PREFIX + dt.datetime.now().strftime("%Y%m%d%H%M%S")
    tmp = None
    env = dict(os.environ, PGPASSWORD=pw)
    result = {"ok": False, "scratch": scratch, "tables": 0, "mismatch": [], "warnings": [], "source": None}
    admin = _connect("postgres", admin_user, pw, autocommit=True)
    created = False
    try:
        if backup_file:
            dump = Path(backup_file)
            if not dump.is_file():
                raise DrillError(f"Backup file not found: {dump}")
            result["source"] = f"existing backup {dump.name}"
        else:
            tmp = tempfile.TemporaryDirectory()
            dump = Path(tmp.name) / "drill.dump"
            live_env = dict(os.environ, PGPASSWORD=db.password() or "")
            r = subprocess.run([backup.pg_tool("pg_dump"), *backup._conn_args(), "-d", config.PG["dbname"], "-Fc", "-Z", "6", "--no-owner", "-f", str(dump)],
                               env=live_env, capture_output=True, text=True, timeout=900)
            if r.returncode != 0 or not dump.is_file() or dump.stat().st_size == 0:
                raise DrillError("pg_dump of the live database failed: " + (r.stderr or "")[-300:])
            result["source"] = f"fresh dump of the live database ({dump.stat().st_size // 1024} KB)"
        say(f"Restoring {result['source']} into scratch database {scratch} ...")
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(scratch)))
        created = True
        r = subprocess.run([backup.pg_tool("pg_restore"), "-h", config.PG["host"], "-p", str(config.PG["port"]), "-U", admin_user, "-d", scratch,
                            "--no-owner", "--single-transaction", str(dump)], env=env, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise DrillError("pg_restore failed: " + (r.stderr or "")[-400:])
        live = _connect(config.PG["dbname"], config.PG["user"], db.password())
        drill = _connect(scratch, admin_user, pw)
        try:
            a, b = _counts(live), _counts(drill)
            result["tables"] = len(a)
            for t in sorted(set(a) | set(b)):
                if t not in b:
                    result["mismatch"].append(f"{t}: missing from the restored copy (live has {a[t]} rows)")
                elif t not in a:
                    result["warnings"].append(f"{t}: only in the restored copy ({b[t]} rows)")
                elif a[t] != b[t]:
                    (result["warnings"] if t in VOLATILE else result["mismatch"]).append(f"{t}: live {a[t]} rows, restored {b[t]} rows")
            with drill.cursor() as cur:
                cur.execute("SELECT extname FROM pg_extension WHERE extname = 'pg_trgm'")
                if not cur.fetchone():
                    result["mismatch"].append("extension pg_trgm (search) is missing from the restored copy")
                cur.execute("SELECT count(*) FROM pg_views WHERE schemaname = 'public'")
                views_restored = cur.fetchone()[0]
            with live.cursor() as cur:
                cur.execute("SELECT count(*) FROM pg_views WHERE schemaname = 'public'")
                if cur.fetchone()[0] != views_restored:
                    result["mismatch"].append("the number of views differs between live and the restored copy")
            with drill.cursor() as cur:         # the restored data must be usable, not just present
                cur.execute("SELECT count(*) FROM asset a JOIN svc_call c ON c.asset_key = a.asset_key WHERE a.is_current = 1")
                cur.fetchone()
        finally:
            live.close()
            drill.close()
        result["ok"] = not result["mismatch"]
    finally:
        if created and not keep:
            if not scratch.startswith(PREFIX):                                      # belt and braces: never drop anything but our own scratch database
                raise DrillError("refusing to drop a database that is not a drill database")
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(scratch)))
            say(f"Scratch database {scratch} dropped.")
        elif created:
            say(f"Scratch database {scratch} kept (--keep). Drop it yourself when done:  DROP DATABASE {scratch};")
        admin.close()
        if tmp:
            tmp.cleanup()
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backup")
    ap.add_argument("--admin-user", default="postgres")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    try:
        res = run(a.backup, a.admin_user, a.keep)
    except (DrillError, backup.BackupError, psycopg.Error) as e:
        print("RESTORE DRILL FAILED:", e)
        return 1
    for w in res["warnings"]:
        print("  note:", w)
    for m in res["mismatch"]:
        print("  MISMATCH:", m)
    print(("RESTORE DRILL PASSED" if res["ok"] else "RESTORE DRILL FAILED") + f" - {res['tables']} tables compared against {res['source']}.")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
