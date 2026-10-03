"""PostgreSQL store for the Employee Master (HR) and the IT Asset Master, with history + change logs.

  python tools/master_db.py load --file Employee_Master_Upload_2026-09.xlsx
  python tools/master_db.py load-assets --file IT_Asset_Master_2026-Q2.xlsx
  python tools/master_db.py check-inventory --file "IT-IMMDSS ASSET INVENTORY 2026 -Q2.xlsx"
  python tools/master_db.py report | report-assets

Connection: host/port/db/user come from PGHOST / PGPORT / PGDATABASE / PGUSER (defaults localhost / 5432 / ongc_ank / ank_app).
The password is read by libpq from %APPDATA%\\postgresql\\pgpass.conf - it is never stored in the project.
"""
import argparse
import collections
import datetime as dt
import os
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd
import psycopg

sys.path.insert(0, str(Path(__file__).parent))
import itam_locks  # noqa: E402
import itam_rules  # noqa: E402
from hr_export_to_template import COLS, KEY, MASTER_SHEET, SPEC, clean  # noqa: E402

PG = dict(host=os.environ.get("PGHOST", "localhost"), port=int(os.environ.get("PGPORT", "5432")),
          dbname=os.environ.get("PGDATABASE", "ongc_ank"), user=os.environ.get("PGUSER", "ank_app"))
DB_LABEL = f"postgresql://{PG['user']}@{PG['host']}:{PG['port']}/{PG['dbname']}"
SQL_TYPE = {"int": "INTEGER", "date": "DATE"}

VIEWS = """
DROP VIEW IF EXISTS v_employee_current;
CREATE VIEW v_employee_current AS
SELECT e.*,
  date_part('year', age(current_date, e.date_of_birth))::int                          AS age,
  round(((current_date - e.date_of_join_ongc) / 365.25)::numeric, 1)                  AS service_years,
  round(((e.date_of_retirement - current_date) / 365.25)::numeric, 1)                 AS years_to_retirement
FROM employee e WHERE e.record_status = 'ACTIVE';

DROP VIEW IF EXISTS v_headcount_discipline_level;
CREATE VIEW v_headcount_discipline_level AS
SELECT discipline, level, COUNT(*) AS headcount FROM employee
WHERE record_status = 'ACTIVE' GROUP BY discipline, level;

DROP VIEW IF EXISTS v_retiring_soon;
CREATE VIEW v_retiring_soon AS
SELECT cpf_no, employee_name, designation, level, org_unit_name, date_of_retirement
FROM employee WHERE record_status = 'ACTIVE'
  AND date_of_retirement <= current_date + interval '24 months' ORDER BY date_of_retirement;

DROP VIEW IF EXISTS v_monthly_headcount;
CREATE VIEW v_monthly_headcount AS
SELECT load_date, COUNT(*) AS headcount FROM employee_history GROUP BY load_date ORDER BY load_date;
"""


def _pgpass_password():
    """Look up the password in the standard pgpass file (host:port:db:user:password)."""
    if os.environ.get("PGPASSWORD"):
        return os.environ["PGPASSWORD"]
    candidates = [os.environ.get("PGPASSFILE"), Path.home() / ".ongc_pgpass", Path(os.environ.get("APPDATA", "")) / "postgresql" / "pgpass.conf"]
    want = [PG["host"], str(PG["port"]), PG["dbname"], PG["user"]]
    for c in candidates:
        f = Path(c) if c else None
        if not f or not f.is_file():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split(":", 4)
            if len(parts) == 5 and all(p in ("*", w) for p, w in zip(parts[:4], want)):
                return parts[4]
    return None


class _DryRunConnection(psycopg.Connection):
    """ITAM_DRY_RUN=1: a rehearsal. Every loader runs exactly as it would for real - the same reads, the same safety stops, the same change
    counts - but nothing is ever committed, and closing the connection rolls everything back. One switch here covers every loader that
    gets its connection from connect()."""

    def commit(self):
        pass

    def close(self):
        if not self.closed:
            try:
                self.rollback()
                print("DRY RUN: nothing was saved - the database is exactly as it was.")
            finally:
                super().close()


def dry_run():
    return os.environ.get("ITAM_DRY_RUN") == "1"


def connect():
    try:
        cls = _DryRunConnection if dry_run() else psycopg.Connection
        con = cls.connect(**PG, password=_pgpass_password(), connect_timeout=8)
    except psycopg.OperationalError as e:
        sys.exit(f"Cannot connect to {DB_LABEL}: {e}")
    return con


def query_df(con, sql, params=None):
    with con.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def hr_connect():
    con = connect()
    cols = ", ".join(f"{c} {SQL_TYPE.get(SPEC[c][2], 'TEXT')}" for c in COLS)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS employee ({cols}, PRIMARY KEY ({KEY}));
        CREATE TABLE IF NOT EXISTS employee_history (LOAD_DATE DATE, {cols}, PRIMARY KEY (LOAD_DATE, {KEY}));
        CREATE TABLE IF NOT EXISTS change_log (SNAPSHOT_DATE DATE, CPF_NO INTEGER, EMPLOYEE_NAME TEXT,
            CHANGE_TYPE TEXT, FIELD TEXT, OLD_VALUE TEXT, NEW_VALUE TEXT);
        CREATE INDEX IF NOT EXISTS ix_log_cpf ON change_log (CPF_NO);
    """)
    con.execute(VIEWS)
    con.commit()
    return con


def typed(df):
    out = pd.DataFrame({c: [clean(SPEC[c][2], v) for v in df[c]] for c in COLS}, dtype=object)
    return out.where(out.notna(), None)


def _none(v):
    return None if v is None or (not isinstance(v, (str, dt.date)) and pd.isna(v)) else v


def _upsert_sql(table, cols, key, extra_cols=(), no_update=()):
    allc = list(cols) + list(extra_cols)
    upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in allc if c != key and c not in no_update)
    return (f"INSERT INTO {table} ({','.join(allc)}) VALUES ({','.join(['%s'] * len(allc))}) "
            f"ON CONFLICT ({key}) DO UPDATE SET {upd}")


def load(xlsx):
    m = typed(pd.read_excel(xlsx, sheet_name=MASTER_SHEET, dtype=object))
    m = m[m[KEY].notna()]
    load_date = m.loc[m["RECORD_STATUS"] == "ACTIVE", "SNAPSHOT_DATE"].max()
    log = pd.read_excel(xlsx, sheet_name="CHANGE_LOG", dtype=object)
    con = hr_connect()
    try:
        rows = [[_none(v) for v in r] for r in m[COLS].values.tolist()]
        with con.cursor() as cur:
            cur.executemany(_upsert_sql("employee", COLS, KEY), rows)
            cur.execute("DELETE FROM employee_history WHERE load_date = %s", (load_date,))
            act = [[load_date] + r for r, s in zip(rows, m["RECORD_STATUS"]) if s == "ACTIVE"]
            cur.executemany(f"INSERT INTO employee_history (LOAD_DATE,{','.join(COLS)}) VALUES (%s,{','.join(['%s'] * len(COLS))})", act)
            cur.execute("DELETE FROM change_log WHERE snapshot_date = %s", (load_date,))
            lrows = []
            for _, r in log.iterrows():
                lrows.append((pd.to_datetime(r["SNAPSHOT_DATE"]).date(), int(r["CPF_NO"]), r["EMPLOYEE_NAME"], r["CHANGE_TYPE"],
                              None if pd.isna(r["FIELD"]) else r["FIELD"],
                              None if pd.isna(r["OLD_VALUE"]) else str(r["OLD_VALUE"]),
                              None if pd.isna(r["NEW_VALUE"]) else str(r["NEW_VALUE"])))
            cur.executemany("INSERT INTO change_log VALUES (%s,%s,%s,%s,%s,%s,%s)", lrows)
            cur.execute("SELECT COUNT(*) FROM employee")
            n_emp = cur.fetchone()[0]
        con.commit()
    finally:
        con.close()
    print(f"Loaded HR snapshot {load_date}: {len(act)} active rows, {len(lrows)} change-log rows. employee table: {n_emp} rows -> {DB_LABEL}")


def check_inventory(xlsx, out_dir):
    con = hr_connect()
    try:
        master = query_df(con, "SELECT cpf_no, employee_name, record_status FROM employee")
    finally:
        con.close()
    status = dict(zip(master["cpf_no"], master["record_status"]))
    book = pd.ExcelFile(xlsx)
    rows, summary = [], []
    for sheet in book.sheet_names:
        d = book.parse(sheet)
        cpf_col = next((c for c in d.columns if str(c).strip().upper().startswith("CPF")), None)
        if cpf_col is None:
            continue
        eng = next((c for c in d.columns if "ENGIN" in str(c).upper()), None)
        cpf = pd.to_numeric(d[cpf_col], errors="coerce")
        valid = cpf.notna()
        bad = 0
        for i in d.index[valid]:
            st = status.get(int(cpf[i]), "NOT IN MASTER")
            if st != "ACTIVE":
                bad += 1
                rows.append((sheet, d.at[i, "CI NO"] if "CI NO" in d.columns else None, int(cpf[i]),
                             d.at[i, eng] if eng else None, st))
        summary.append((sheet, int(valid.sum()), bad))
    res = pd.DataFrame(rows, columns=["SHEET", "CI_NO", "CPF_NO", "ENGINEER", "MASTER_STATUS"])
    summ = pd.DataFrame(summary, columns=["SHEET", "ROWS_WITH_CPF", "CPF_NOT_ACTIVE_IN_MASTER"])
    path = Path(out_dir) / "Inventory_CPF_Check.xlsx"
    with pd.ExcelWriter(path) as w:
        summ.to_excel(w, sheet_name="SUMMARY", index=False)
        res.to_excel(w, sheet_name="UNMATCHED_ASSETS", index=False)
        by_cpf = res.groupby(["CPF_NO", "MASTER_STATUS"]).size().reset_index(name="ASSETS").sort_values("ASSETS", ascending=False)
        by_cpf.to_excel(w, sheet_name="UNMATCHED_BY_CPF", index=False)
        for ws in w.book.worksheets:
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = 24
    print(summ.to_string(index=False))
    print(f"\n{len(res)} asset rows / {res['CPF_NO'].nunique()} CPFs not active in master -> {path}")


def report():
    con = hr_connect()
    try:
        for title, q in (("Monthly headcount", "SELECT * FROM v_monthly_headcount"),
                         ("Active by class", "SELECT class, COUNT(*) n FROM v_employee_current GROUP BY class ORDER BY class"),
                         ("Retiring in next 24 months", "SELECT COUNT(*) n FROM v_retiring_soon"),
                         ("Change log by type", "SELECT change_type, COUNT(*) n FROM change_log GROUP BY change_type")):
            print(f"\n{title}\n{query_df(con, q).to_string(index=False)}")
    finally:
        con.close()


# ---------------------------------------------------------------- IT assets
ASSET_TRACKED_EXCLUDE = {"SNAPSHOT_DATE", "DQ_FLAGS", "COVER_STATUS", "PM_QUARTER", "PM_DATE", "PM_STATUS", "PM_DONE_BY", "PM_SIGNED_BY",
                         "PM_TRACKER_DATE", "SOURCE_SHEET", "SOURCE_ROW", "USER_NAME", "USER_DESIGNATION", "USER_LEVEL", "USER_MOBILE",
                         "USER_RETIREMENT_DATE", "USER_HR_STATUS", "USER_DEPARTMENT"}
ASSET_VIEWS = """
DROP VIEW IF EXISTS v_asset_current;
CREATE VIEW v_asset_current AS SELECT * FROM asset WHERE is_current = 1;
DROP VIEW IF EXISTS v_asset_count_by_class;
CREATE VIEW v_asset_count_by_class AS
SELECT asset_class, record_level, COUNT(*) AS n FROM asset WHERE is_current = 1 GROUP BY asset_class, record_level;
DROP VIEW IF EXISTS v_cover_attention;
CREATE VIEW v_cover_attention AS
SELECT asset_key, asset_class, asset_type, model, cpf_no, user_name, cover_type, cover_expiry_date, cover_status
FROM asset WHERE is_current = 1 AND cover_status IN ('EXPIRED', 'EXPIRING_90D') ORDER BY cover_expiry_date;
DROP VIEW IF EXISTS v_pm_pending;
CREATE VIEW v_pm_pending AS
SELECT asset_key, asset_class, asset_type, engineer_name, location_code, room FROM asset WHERE is_current = 1 AND pm_status = 'PENDING';
DROP VIEW IF EXISTS v_asset_user_not_active;
CREATE VIEW v_asset_user_not_active AS
SELECT asset_key, asset_class, cpf_no, user_name, user_hr_status FROM asset WHERE is_current = 1 AND user_hr_status <> 'ACTIVE';
"""


def asset_connect():
    from inventory_to_master import COLS as A_COLS, DATE_COLS
    con = hr_connect()
    num = {"CPF_NO": "INTEGER", "SOURCE_ROW": "INTEGER", "RATE_VALUE": "NUMERIC(12,2)"}
    cols = ", ".join(f"{c} {'DATE' if c in DATE_COLS else num.get(c, 'TEXT')}" for c in A_COLS)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS asset ({cols}, IS_CURRENT INTEGER, FIRST_SEEN_DATE DATE, LAST_SEEN_DATE DATE, PRIMARY KEY (ASSET_KEY));
        CREATE TABLE IF NOT EXISTS asset_snapshot ({cols}, PRIMARY KEY (SNAPSHOT_DATE, ASSET_KEY));
        CREATE TABLE IF NOT EXISTS asset_change_log (SNAPSHOT_DATE DATE, ASSET_KEY TEXT, CHANGE_TYPE TEXT, FIELD TEXT, OLD_VALUE TEXT, NEW_VALUE TEXT);
        CREATE INDEX IF NOT EXISTS ix_asset_cpf ON asset (CPF_NO);
        CREATE INDEX IF NOT EXISTS ix_asset_class ON asset (ASSET_CLASS);
    """)
    con.execute(ASSET_VIEWS)
    con.commit()
    return con, A_COLS


def _asset_norm(df, A_COLS):
    from inventory_to_master import DATE_COLS
    out = {}
    for c in A_COLS:
        vals = []
        for v in df[c]:
            if v is None or (not isinstance(v, str) and pd.isna(v)):
                vals.append(None)
            elif c in DATE_COLS:
                d = pd.to_datetime(v, errors="coerce")
                vals.append(None if pd.isna(d) else d.date())
            elif c in ("CPF_NO", "SOURCE_ROW"):
                vals.append(int(float(v)))
            elif c == "RATE_VALUE":
                vals.append(float(v))
            else:
                vals.append(str(v))
        out[c] = vals
    return pd.DataFrame(out, dtype=object)


def _cmp(v):
    if v is None or (not isinstance(v, (str, dt.date, Decimal)) and pd.isna(v)):
        return None
    if isinstance(v, Decimal):
        return repr(float(v))
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, dt.date):
        return v.isoformat()
    return str(v)


def load_assets(xlsx, force=False):
    con, A_COLS = asset_connect()
    try:
        raw = pd.read_excel(xlsx, sheet_name="ASSET_MASTER", dtype=object, keep_default_na=False, na_values=[""])   # keep the literal status "NA"
        if list(raw.columns) != A_COLS:
            sys.exit("ASSET_MASTER columns do not match the standard template - nothing loaded.")
        raw = raw[raw["ASSET_KEY"].notna()]
        df = _asset_norm(raw, A_COLS)
        for c in df.columns:                                       # text rule: upper case everywhere (the database enforces it too)
            if c not in itam_rules.NO_UPPER:
                df[c] = df[c].map(itam_rules.upper_text)
        problems = []
        if df["ASSET_KEY"].duplicated().any():
            problems.append("duplicate ASSET_KEY")
        for c in ("SNAPSHOT_DATE", "ASSET_KEY", "ASSET_CLASS", "ASSET_TYPE", "RECORD_LEVEL"):
            n = int(df[c].isna().sum())
            if n:
                problems.append(f"{n} rows missing required {c}")
        if df["SNAPSHOT_DATE"].nunique() != 1:
            problems.append("more than one SNAPSHOT_DATE in file")
        if problems:
            sys.exit("Load blocked: " + "; ".join(problems))
        snap = df["SNAPSHOT_DATE"].iloc[0]
        itam_locks.ensure_tables(con)
        ident = df[["ASSET_KEY", "SERIAL_NO", "ONGC_ASSET_ID", "SOURCE_ROW"]].astype(object)
        stale = itam_locks.alias_conflicts(con, ident.where(ident.notna(), None).values.tolist())      # a file that undoes a Replace / Redeploy made in the portal
        if stale:
            more = f"\n  ... and {len(stale) - 25} more" if len(stale) > 25 else ""
            sys.exit("Load blocked - the file does not match a replacement already made in the portal:\n  " + "\n  ".join(stale[:25]) + more)
        locked = itam_locks.load_overrides(con, "assets")[0]
        idx = df.index[df["ASSET_KEY"].isin(set(locked))]
        recs = [{k: (None if not isinstance(v, (str, list, dict)) and pd.isna(v) else v) for k, v in df.loc[i].to_dict().items()} for i in idx]     # NaN -> None
        created, archived = itam_locks.apply_records(con, "assets", recs, "ASSET_KEY", snap)   # keep manual edits made in the portal
        for i, rec in zip(idx, recs):
            for c, v in rec.items():
                df.at[i, c] = v
        with con.cursor() as cur:
            cur.execute("SELECT MAX(snapshot_date) FROM asset_snapshot")
            latest = cur.fetchone()[0]
            if latest and snap < latest:
                sys.exit(f"Load blocked: snapshot {snap} is older than already loaded {latest}.")
            cur.execute("SELECT MAX(snapshot_date) FROM asset_snapshot WHERE snapshot_date < %s", (snap,))
            prev_date = cur.fetchone()[0]
            prev = None
            if prev_date:
                prev = query_df(con, "SELECT * FROM asset_snapshot WHERE snapshot_date = %s", (prev_date,))
                prev.columns = [c.upper() for c in prev.columns]
                prev = prev.set_index("ASSET_KEY")
                gone = set(prev.index) - set(df["ASSET_KEY"])
                if len(gone) > 0.2 * max(len(prev), 1) and not force:
                    sys.exit(f"Load blocked: {len(gone)} of {len(prev)} assets from {prev_date} are missing (>20%). "
                             "A sheet may be missing. Re-check the file or use --force.")

            log = []
            if prev is not None:
                new = df.set_index("ASSET_KEY")
                for k in new.index.difference(prev.index):
                    log.append((snap, k, "ADDED", None, None, None))
                for k in prev.index.difference(new.index):
                    log.append((snap, k, "REMOVED", None, None, None))
                for k in new.index.intersection(prev.index):
                    for c in A_COLS:
                        if c in ASSET_TRACKED_EXCLUDE or c == "ASSET_KEY":
                            continue
                        a, b = _cmp(prev.at[k, c]), _cmp(new.at[k, c])
                        if a != b:
                            log.append((snap, k, "CHANGED", c, a, b))

            rows = [[_none(v) for v in r] for r in df[A_COLS].values.tolist()]
            cur.execute("DELETE FROM asset_snapshot WHERE snapshot_date = %s", (snap,))
            cur.execute("DELETE FROM asset_change_log WHERE snapshot_date = %s", (snap,))
            cur.executemany(f"INSERT INTO asset_snapshot ({','.join(A_COLS)}) VALUES ({','.join(['%s'] * len(A_COLS))})", rows)
            cur.execute("SELECT asset_key, first_seen_date FROM asset")
            first = dict(cur.fetchall())
            k_i = A_COLS.index("ASSET_KEY")
            sql = _upsert_sql("asset", A_COLS, "ASSET_KEY", extra_cols=("IS_CURRENT", "FIRST_SEEN_DATE", "LAST_SEEN_DATE"), no_update=("FIRST_SEEN_DATE",))
            cur.executemany(sql, [r + [1, first.get(r[k_i]) or snap, snap] for r in rows])
            cur.execute("UPDATE asset SET is_current = 0 WHERE last_seen_date < %s AND asset_key <> ALL(%s)", (snap, list(created) or [""]))   # portal-created rows are not in files
            if archived:
                cur.execute("UPDATE asset SET is_current = 0 WHERE asset_key = ANY(%s)", (list(archived),))   # portal-archived rows stay archived
            cur.executemany("INSERT INTO asset_change_log VALUES (%s,%s,%s,%s,%s,%s)", log)
            cur.execute("SELECT COUNT(*) FROM asset WHERE is_current = 1")
            n_now = cur.fetchone()[0]
        con.commit()
    finally:
        con.close()
    kinds = collections.Counter(l[2] for l in log)
    print(f"Database load OK: snapshot {snap}, {len(df)} rows -> {DB_LABEL}")
    print(f"  current assets in DB: {n_now} | previous snapshot: {prev_date or 'none (baseline load)'} | changes: {dict(kinds) or 'none'}")


def report_assets():
    con, _ = asset_connect()
    try:
        for title, q in (("Snapshots loaded", "SELECT snapshot_date, COUNT(*) n FROM asset_snapshot GROUP BY snapshot_date ORDER BY 1"),
                         ("Current by class", "SELECT * FROM v_asset_count_by_class ORDER BY 1, 2"),
                         ("Cover attention", "SELECT cover_status, COUNT(*) n FROM v_cover_attention GROUP BY cover_status"),
                         ("Change log", "SELECT change_type, COUNT(*) n FROM asset_change_log GROUP BY change_type")):
            print(f"\n{title}\n{query_df(con, q).to_string(index=False)}")
    finally:
        con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["load", "check-inventory", "report", "load-assets", "report-assets"])
    ap.add_argument("--file")
    ap.add_argument("--out-dir", default=str(Path(__file__).resolve().parent.parent / "masters"))
    ap.add_argument("--force", action="store_true", help="load-assets: skip the mass-removal safety stop")
    a = ap.parse_args()
    if a.cmd == "report":
        report()
    elif a.cmd == "report-assets":
        report_assets()
    elif not a.file:
        sys.exit("--file is required")
    elif a.cmd == "load-assets":
        load_assets(a.file, force=a.force)
    elif a.cmd == "load":
        load(a.file)
    else:
        check_inventory(a.file, a.out_dir)
