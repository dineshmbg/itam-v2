"""Preventive maintenance (PM): quarterly cycles, recording, live dashboard, quarter snapshots and roll-over.

A cycle is one financial-year quarter (Q1 APR-JUN, Q2 JUL-SEP, Q3 OCT-DEC, Q4 JAN-MAR). Each in-scope asset needs one PM inside the cycle.
The PM kick-off notice goes out KICKOFF_DAYS after the quarter starts. The current state lives on the asset (pm_date, pm_done_by, pm_signed_by,
pm_status - edits are protected like any other edit); pm_record keeps every PM that was recorded, and pm_snapshot freezes the whole picture when a
quarter closes so quarters can be compared afterwards.
"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import itam_locks as L  # noqa: E402
import itam_rules as R  # noqa: E402

from . import db, edit  # noqa: E402

KICKOFF_DAYS = 40
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
IN_SCOPE = "is_current = 1 AND record_level = 'ASSET' AND pm_status <> 'NOT_TRACKED'"

DDL = [
    """CREATE TABLE IF NOT EXISTS pm_cycle (
         quarter_label TEXT PRIMARY KEY, start_date DATE NOT NULL, end_date DATE NOT NULL, kickoff_date DATE NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED')),
         opened_at TIMESTAMPTZ NOT NULL DEFAULT now(), opened_by TEXT, closed_at TIMESTAMPTZ, closed_by TEXT, kickoff_notified_at TIMESTAMPTZ)""",
    """CREATE TABLE IF NOT EXISTS pm_record (
         pm_id BIGSERIAL PRIMARY KEY, quarter_label TEXT NOT NULL, asset_key TEXT NOT NULL, pm_date DATE NOT NULL, done_by TEXT, signed_by TEXT, remarks TEXT,
         recorded_by TEXT NOT NULL, recorded_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS ix_pm_record_q ON pm_record (quarter_label, asset_key)",
    """CREATE TABLE IF NOT EXISTS pm_snapshot (
         quarter_label TEXT NOT NULL, as_of DATE NOT NULL, asset_key TEXT NOT NULL, asset_class TEXT, engineer_name TEXT, location_code TEXT, pm_status TEXT, pm_date DATE,
         pm_done_by TEXT, pm_signed_by TEXT, cover_status TEXT, PRIMARY KEY (quarter_label, as_of, asset_key))""",
]


class PmError(Exception):
    http_error = True

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def quarter(d):
    """Financial-year quarter containing date d."""
    fy = d.year if d.month >= 4 else d.year - 1
    idx = ((d.month - 4) % 12) // 3                      # 0..3, Apr-Jun = 0
    m0 = (3 + 3 * idx) % 12                                # 0-based first month of the quarter
    start = dt.date(fy + (1 if m0 < 3 else 0), m0 + 1, 1)
    end_m0 = m0 + 3
    end = dt.date(start.year + end_m0 // 12, end_m0 % 12 + 1, 1) - dt.timedelta(days=1)
    label = f"Q{idx + 1} {MON[m0]}-{MON[(m0 + 2) % 12]} {start.year}"
    return {"label": label, "start": start, "end": end, "kickoff": start + dt.timedelta(days=KICKOFF_DAYS)}


def next_quarter(d):
    return quarter(quarter(d)["end"] + dt.timedelta(days=1))


def active_quarter(con=None, today=None):
    """The quarter the PM figures on the assets belong to: the label the in-scope assets carry. That label only moves when a quarter
    is closed (rollover) or an import brings a new one - it is NOT simply the quarter containing today. The two differ from the
    first day of a new quarter until somebody closes the old one; `overdue` is True for exactly that stretch."""
    today = today or dt.date.today()
    sql = f"SELECT pm_quarter FROM asset WHERE {IN_SCOPE} AND pm_quarter IS NOT NULL GROUP BY 1 ORDER BY count(*) DESC, 1 DESC LIMIT 1"
    if con is not None:
        row = con.execute(sql).fetchone()
        label = row[0] if row else None
    else:
        row = db.one(sql)
        label = row["pm_quarter"] if row else None
    start, _ = R.quarter_window(label)
    q = quarter(start or today)
    q["overdue"] = q["end"] < today
    return q


def _opening(q, today):
    """The quarter a roll-over of q opens: the one containing today when q is already over, otherwise the one right after q (early cut-over)."""
    return quarter(max(today, q["end"] + dt.timedelta(days=1)))


def ensure_cycle(con, on=None, by="system"):
    q = quarter(on or dt.date.today())
    con.execute("INSERT INTO pm_cycle (quarter_label, start_date, end_date, kickoff_date, opened_by) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (quarter_label) DO NOTHING",
                (q["label"], q["start"], q["end"], q["kickoff"], by))
    return q


def _rows(sql, params=()):
    return db.query(sql, params)


def dashboard(eng=None):
    """`eng`: a non-admin engineer's own engineer_key - scopes every asset-level figure to their own assets ("my PM worklist"
    rather than the whole fleet). None (an administrator) means unscoped. The cycle/quarter metadata (dates, kickoff, snapshot
    history) is the same for everyone regardless and is never scoped.

    The cycle shown is active_quarter() - the one the counts actually belong to - not the calendar quarter: after a quarter ends
    and before it is closed, the counts are still the old quarter's and are labelled as such (cycle.overdue), never as the new one."""
    today = dt.date.today()
    q = active_quarter(today=today)
    cyc = db.one("SELECT * FROM pm_cycle WHERE quarter_label = %s", [q["label"]])
    label_in_data = db.one("SELECT max(pm_quarter) AS l FROM asset WHERE is_current = 1")["l"]
    scope = IN_SCOPE + (" AND engineer_name = %(eng)s" if eng else "")
    p = {"eng": eng}
    k = db.one(f"""SELECT count(*) AS scope, count(*) FILTER (WHERE pm_status = 'DONE') AS done, count(*) FILTER (WHERE pm_status = 'DONE_OUTSIDE_QUARTER') AS stale,
                          count(*) FILTER (WHERE pm_status = 'PENDING') AS pending, count(*) FILTER (WHERE pm_status = 'PENDING' AND engineer_name IS NULL) AS unassigned,
                          count(*) FILTER (WHERE asset_status = 'NOT_ON_NETWORK') AS off_network
                   FROM asset WHERE {scope}""", p)
    total_days = (q["end"] - q["start"]).days + 1
    elapsed = max(0, min(total_days, (today - q["start"]).days + 1))
    k.update(pct_done=round(100.0 * k["done"] / k["scope"], 1) if k["scope"] else None, elapsed_days=elapsed, total_days=total_days, days_left=max(0, (q["end"] - today).days),
             expected_pct=round(100.0 * elapsed / total_days, 1), kickoff_in_days=(q["kickoff"] - today).days, data_quarter=label_in_data)
    by_eng = _rows(f"""SELECT coalesce(engineer_name, 'UNASSIGNED') AS label, count(*) AS scope, count(*) FILTER (WHERE pm_status = 'DONE') AS done,
                             count(*) FILTER (WHERE pm_status = 'DONE_OUTSIDE_QUARTER') AS stale, count(*) FILTER (WHERE pm_status = 'PENDING') AS pending
                       FROM asset WHERE {scope} GROUP BY 1 ORDER BY pending DESC, 1""", p)
    by_loc = _rows(f"""SELECT coalesce(location_code, 'UNKNOWN') AS label, count(*) AS scope, count(*) FILTER (WHERE pm_status = 'DONE') AS done,
                             count(*) FILTER (WHERE pm_status = 'PENDING') AS pending FROM asset WHERE {scope} GROUP BY 1 ORDER BY pending DESC, 1 LIMIT 12""", p)
    by_class = _rows(f"""SELECT asset_class AS label, count(*) AS scope, count(*) FILTER (WHERE pm_status = 'DONE') AS done, count(*) FILTER (WHERE pm_status = 'DONE_OUTSIDE_QUARTER') AS stale,
                              count(*) FILTER (WHERE pm_status = 'PENDING') AS pending FROM asset WHERE {scope} GROUP BY 1 ORDER BY pending DESC, 1""", p)
    burn = _rows(f"""SELECT pm_date AS day, count(*) AS n FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND pm_date BETWEEN %(start)s AND %(end)s{' AND engineer_name = %(eng)s' if eng else ''}
                    GROUP BY 1 ORDER BY 1""", {"start": q["start"], "end": q["end"], "eng": eng})
    signers = _rows(f"""SELECT coalesce(pm_signed_by, 'NOT RECORDED') AS label, count(*) AS n FROM asset WHERE {scope} AND pm_status IN ('DONE','DONE_OUTSIDE_QUARTER') GROUP BY 1 ORDER BY 2 DESC LIMIT 8""", p)
    quarters = _rows("""SELECT quarter_label AS label, as_of, count(*) AS scope, count(*) FILTER (WHERE pm_status = 'DONE') AS done, count(*) FILTER (WHERE pm_status = 'PENDING') AS pending
                        FROM pm_snapshot GROUP BY 1, 2 ORDER BY 2""")
    recent = _rows(f"""SELECT pm_id AS id, quarter_label, asset_key, pm_date, done_by, signed_by, recorded_by, recorded_at FROM pm_record
                      {"WHERE asset_key IN (SELECT asset_key FROM asset WHERE engineer_name = %(eng)s)" if eng else ""} ORDER BY pm_id DESC LIMIT 10""", p)
    return {"cycle": {"label": q["label"], "start": q["start"], "end": q["end"], "kickoff": q["kickoff"], "status": cyc["status"] if cyc else "OPEN", "kickoff_sent": cyc["kickoff_notified_at"] if cyc else None,
                      "overdue": q["overdue"], "calendar_label": quarter(today)["label"]},
            "kpi": k, "by_engineer": by_eng, "by_location": by_loc, "by_class": by_class, "burn": burn, "signers": signers, "quarters": quarters, "recent": recent}


def cycles():
    return {"cycles": _rows("SELECT * FROM pm_cycle ORDER BY start_date DESC"),
            "current": {k: v for k, v in quarter(dt.date.today()).items()},
            "snapshots": _rows("SELECT quarter_label, as_of, count(*) AS assets FROM pm_snapshot GROUP BY 1, 2 ORDER BY 2 DESC")}


# ---------------------------------------------------------------- past quarters
STATUS_WORDS = {"PENDING": "Scheduled", "DONE": "Completed", "DONE_OUTSIDE_QUARTER": "Completed late"}
HISTORY_ROW_LIMIT = 2000


def _snap_scope(eng):
    return (" AND engineer_name = %(eng)s" if eng else "")


def history(eng=None):
    """Every quarter that has a frozen snapshot, newest first, each shown as it stood in its LAST snapshot (the closing picture; the
    weekly and after-import ones sit between). `eng` scopes the figures to one engineer's own assets, as the dashboard does."""
    rows = _rows(f"""WITH last AS (SELECT quarter_label, max(as_of) AS as_of, count(DISTINCT as_of) AS snapshots FROM pm_snapshot GROUP BY 1)
                     SELECT s.quarter_label AS label, l.as_of, l.snapshots, count(*) AS scope, count(*) FILTER (WHERE s.pm_status = 'DONE') AS done,
                            count(*) FILTER (WHERE s.pm_status = 'DONE_OUTSIDE_QUARTER') AS stale, count(*) FILTER (WHERE s.pm_status = 'PENDING') AS pending
                     FROM pm_snapshot s JOIN last l ON l.quarter_label = s.quarter_label AND l.as_of = s.as_of
                     WHERE true{_snap_scope(eng)} GROUP BY 1, 2, 3""", {"eng": eng})
    cyc = {c["quarter_label"]: c for c in _rows("SELECT quarter_label, start_date, end_date, status, closed_at, closed_by FROM pm_cycle")}
    recorded = {r["quarter_label"]: r["n"] for r in _rows("SELECT quarter_label, count(*) AS n FROM pm_record GROUP BY 1")}
    out = []
    for r in rows:
        c = cyc.get(r["label"])
        start, end = (c["start_date"], c["end_date"]) if c else R.quarter_window(r["label"])
        r.update(start=start, end=end, status=c["status"] if c else None, closed_at=c["closed_at"] if c else None, closed_by=c["closed_by"] if c else None,
                 recorded=recorded.get(r["label"], 0), pct_done=round(100.0 * (r["done"] + r["stale"]) / r["scope"], 1) if r["scope"] else None)
        out.append(r)
    out.sort(key=lambda r: (r["start"] or dt.date.min), reverse=True)
    return {"quarters": out}


def _quarter_snapshots(label):
    return [r["as_of"] for r in _rows("SELECT DISTINCT as_of FROM pm_snapshot WHERE quarter_label = %s ORDER BY as_of DESC", [label])]


def history_assets(label, as_of=None, eng=None, status=None, q=None, limit=HISTORY_ROW_LIMIT):
    """Asset-by-asset rows of one frozen snapshot, optionally narrowed to a status and/or a text search. Returns (rows, total, as_of, available dates)."""
    dates = _quarter_snapshots(label)
    if not dates:
        raise PmError(f"There is no snapshot for {label}.", 404)
    if as_of is None:
        as_of = dates[0]
    elif as_of not in dates:
        raise PmError(f"{label} has no snapshot dated {as_of:%d %b %Y}.", 404)
    where, p = ["quarter_label = %(label)s", "as_of = %(as_of)s"], {"label": label, "as_of": as_of, "eng": eng, "lim": limit}
    if eng:
        where.append("engineer_name = %(eng)s")
    if status:
        if status not in STATUS_WORDS:
            raise PmError("Unknown PM status.")
        where.append("pm_status = %(status)s")
        p["status"] = status
    if q and q.strip():
        where.append("(asset_key ILIKE %(q)s OR engineer_name ILIKE %(q)s OR location_code ILIKE %(q)s OR asset_class ILIKE %(q)s OR pm_done_by ILIKE %(q)s OR pm_signed_by ILIKE %(q)s)")
        p["q"] = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    w = " AND ".join(where)
    rows = _rows(f"SELECT asset_key, asset_class, engineer_name, location_code, pm_status, pm_date, pm_done_by, pm_signed_by, cover_status FROM pm_snapshot WHERE {w} ORDER BY asset_key LIMIT %(lim)s", p)
    total = db.one(f"SELECT count(*) AS n FROM pm_snapshot WHERE {w}", p)["n"]
    return rows, total, as_of, dates


def history_detail(label, as_of=None, eng=None, status=None, q=None):
    """One past quarter as it stood on a snapshot date: totals, breakdowns, the PM actually recorded in that quarter, and the asset list."""
    rows, total, as_of, dates = history_assets(label, as_of, eng, status, q)
    p = {"label": label, "as_of": as_of, "eng": eng}
    base = f"quarter_label = %(label)s AND as_of = %(as_of)s{_snap_scope(eng)}"
    k = db.one(f"""SELECT count(*) AS scope, count(*) FILTER (WHERE pm_status = 'DONE') AS done, count(*) FILTER (WHERE pm_status = 'DONE_OUTSIDE_QUARTER') AS stale,
                          count(*) FILTER (WHERE pm_status = 'PENDING') AS pending, count(*) FILTER (WHERE pm_status = 'PENDING' AND engineer_name IS NULL) AS unassigned
                   FROM pm_snapshot WHERE {base}""", p)
    k["pct_done"] = round(100.0 * (k["done"] + k["stale"]) / k["scope"], 1) if k["scope"] else None

    def by(col, name):
        return _rows(f"""SELECT coalesce({col}, '{name}') AS label, count(*) AS scope, count(*) FILTER (WHERE pm_status = 'DONE') AS done,
                                count(*) FILTER (WHERE pm_status = 'DONE_OUTSIDE_QUARTER') AS stale, count(*) FILTER (WHERE pm_status = 'PENDING') AS pending
                         FROM pm_snapshot WHERE {base} GROUP BY 1 ORDER BY pending DESC, 1""", p)
    cyc = db.one("SELECT start_date, end_date, status, closed_at, closed_by FROM pm_cycle WHERE quarter_label = %s", [label])
    start, end = (cyc["start_date"], cyc["end_date"]) if cyc else R.quarter_window(label)
    recorded = _rows(f"""SELECT pm_id AS id, asset_key, pm_date, done_by, signed_by, remarks, recorded_by, recorded_at FROM pm_record WHERE quarter_label = %(label)s
                        {"AND asset_key IN (SELECT asset_key FROM pm_snapshot WHERE quarter_label = %(label)s AND engineer_name = %(eng)s)" if eng else ""} ORDER BY pm_date, pm_id LIMIT 500""", p)
    return {"label": label, "as_of": as_of, "snapshots": dates, "start": start, "end": end, "status": cyc["status"] if cyc else None,
            "closed_at": cyc["closed_at"] if cyc else None, "closed_by": cyc["closed_by"] if cyc else None,
            "kpi": k, "by_engineer": by("engineer_name", "UNASSIGNED"), "by_class": by("asset_class", "UNKNOWN"), "by_location": by("location_code", "UNKNOWN"),
            "recorded": recorded, "assets": {"rows": rows, "total": total, "limit": HISTORY_ROW_LIMIT, "status": status, "q": q}}


def history_tables(label, as_of, eng=None, status=None, q=None):
    """The same picture as printable tables (Excel / PDF / CSV): first the asset list, then the breakdowns."""
    d = history_detail(label, as_of, eng, status, q)
    for r in d["assets"]["rows"]:
        r["pm_status"] = STATUS_WORDS.get(r["pm_status"], r["pm_status"])
    cols = [("label", "Name"), ("scope", "In scope"), ("done", "Completed"), ("stale", "Completed late"), ("pending", "Scheduled")]
    k = d["kpi"]
    tables = [{"title": "Assets", "columns": [("asset_key", "Asset"), ("asset_class", "Class"), ("engineer_name", "Engineer"), ("location_code", "Location"), ("pm_status", "PM status"),
                                              ("pm_date", "PM date"), ("pm_done_by", "Done by"), ("pm_signed_by", "Signed by")], "rows": d["assets"]["rows"]},
              {"title": "By engineer", "columns": [("label", "Engineer")] + cols[1:], "rows": d["by_engineer"]},
              {"title": "By class", "columns": [("label", "Class")] + cols[1:], "rows": d["by_class"]},
              {"title": "By location", "columns": [("label", "Location")] + cols[1:], "rows": d["by_location"]}]
    kpis = [("Quarter", label), ("Snapshot taken", f"{d['as_of']:%d %b %Y}"), ("Assets in scope", k["scope"]), ("PM completed", k["done"]), ("Completed late", k["stale"]),
            ("PM scheduled (not done)", k["pending"]), ("Completion (%)", k["pct_done"])]
    return d, tables, kpis


# ---------------------------------------------------------------- recording
def _parse_pm_date(pm_date):
    try:
        d = dt.date.fromisoformat(str(pm_date)[:10])
    except ValueError:
        raise PmError("Enter the date the PM was done.") from None
    q = quarter(dt.date.today())
    if d > dt.date.today():
        raise PmError("The PM date cannot be in the future.")
    if not q["start"] <= d <= q["end"]:
        raise PmError(f"The PM date must fall in the current cycle ({q['label']}: {q['start']:%d %b %Y} to {q['end']:%d %b %Y}).")
    return d, q


def record_in(con, keys, d, q, done_by, signed_by, remarks, user, ip):
    """Write the PM onto each asset (and into pm_record) inside the caller's transaction. Returns the number recorded."""
    done = 0
    ensure_cycle(con)
    for k in keys:
        changes = {"pm_date": d.isoformat()}
        if done_by:
            changes["pm_done_by"] = done_by
        if signed_by:
            changes["pm_signed_by"] = signed_by
        row = con.execute("SELECT pm_status, record_level FROM asset WHERE asset_key = %s AND is_current = 1", (k,)).fetchone()
        if not row:
            raise PmError(f"{k} is not in the asset register.", 404)
        if row[0] == "NOT_TRACKED" or row[1] != "ASSET":
            raise PmError(f"{k} is not in the PM scope (laptops and components are not tracked).")
        edit.update("assets", k, changes, {}, user["username"], ip, remarks or f"PM {q['label']}", con=con)
        con.execute("INSERT INTO pm_record (quarter_label, asset_key, pm_date, done_by, signed_by, remarks, recorded_by) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (q["label"], k, d, (done_by or "").upper() or None, (signed_by or "").upper() or None, (remarks or "").upper() or None, user["username"]))
        done += 1
    return done


def record(keys, pm_date, done_by, signed_by, remarks, user, ip):
    """Record a completed PM for one or many assets in a single transaction."""
    keys = [str(k).upper() for k in dict.fromkeys(keys or [])][:500]
    if not keys:
        raise PmError("Select at least one asset.")
    d, q = _parse_pm_date(pm_date)
    with db.write() as con:
        done = record_in(con, keys, d, q, done_by, signed_by, remarks, user, ip)
    return {"recorded": done, "quarter": q["label"]}


# ---------------------------------------------------------------- snapshots and roll-over
def capture_snapshot(con, label, as_of):
    con.execute("DELETE FROM pm_snapshot WHERE quarter_label = %s AND as_of = %s", (label, as_of))
    n = con.execute(f"""INSERT INTO pm_snapshot (quarter_label, as_of, asset_key, asset_class, engineer_name, location_code, pm_status, pm_date, pm_done_by, pm_signed_by, cover_status)
                        SELECT %s, %s, asset_key, asset_class, engineer_name, location_code, pm_status, pm_date, pm_done_by, pm_signed_by, cover_status FROM asset WHERE {IN_SCOPE}""", (label, as_of)).rowcount
    return n


def snapshot_now(user, ip):
    with db.write() as con:
        q = active_quarter(con)                              # labelled with the quarter the figures belong to, not the calendar one
        n = capture_snapshot(con, q["label"], dt.date.today())
    return {"quarter": q["label"], "assets": n}


def rollover_preview():
    today = dt.date.today()
    q = active_quarter(today=today)
    nq = _opening(q, today)
    k = db.one(f"SELECT count(*) AS scope, count(*) FILTER (WHERE pm_status IN ('DONE','DONE_OUTSIDE_QUARTER')) AS with_pm, count(*) FILTER (WHERE pm_status = 'PENDING') AS pending FROM asset WHERE {IN_SCOPE}")
    return {"closing": q["label"], "closing_end": q["end"], "overdue": q["overdue"], "opening": nq["label"], "opening_start": nq["start"],
            "starts_in_days": (q["end"] - today).days + 1, **k}


def rollover(user, ip, force=False):
    """Close the quarter the assets are in (freeze a snapshot) and open the next: every in-scope asset goes back to PM pending for the new quarter.
    The previous values stay in pm_snapshot / pm_record. Refused before the quarter has ended unless force=True (early cut-over).

    "The quarter the assets are in" is active_quarter(), not quarter(today): a quarter is normally closed a day or more AFTER it ends,
    and by then quarter(today) is already the new one - closing that would skip a quarter and leave the old one open for ever.
    A PM already dated inside the quarter being opened (recorded between the quarter ending and this close) is kept, not wiped."""
    today = dt.date.today()
    q = active_quarter(today=today)
    nq = _opening(q, today)
    if not q["overdue"] and not force:
        raise PmError(f"{q['label']} runs until {q['end']:%d %b %Y}. Use an early roll-over only if you really are cutting over now.", 409)
    with db.write() as con:
        capture_snapshot(con, q["label"], min(today, q["end"]))
        ensure_cycle(con, q["start"], user["username"])
        con.execute("UPDATE pm_cycle SET status = 'CLOSED', closed_at = now(), closed_by = %s WHERE quarter_label = %s", (user["username"], q["label"]))
        ensure_cycle(con, nq["start"], user["username"])
        rows = con.execute(f"SELECT asset_key FROM asset WHERE {IN_SCOPE} FOR UPDATE").fetchall()
        keys = [r[0] for r in rows]
        con.execute(f"UPDATE asset SET pm_quarter = %s WHERE {IN_SCOPE}", (nq["label"],))
        con.execute(f"UPDATE asset SET pm_date = NULL, pm_done_by = NULL, pm_signed_by = NULL WHERE {IN_SCOPE} AND (pm_date IS NULL OR pm_date < %s)", (nq["start"],))
        con.execute(f"""DELETE FROM portal_lock WHERE dataset = 'assets' AND field = ANY(%s)
                        AND record_key NOT IN (SELECT asset_key FROM asset WHERE {IN_SCOPE} AND pm_date IS NOT NULL)""", (["pm_date", "pm_done_by", "pm_signed_by"],))
        with con.cursor() as cur:
            cur.execute(f"SELECT * FROM asset WHERE {IN_SCOPE}")
            cols = [c.name for c in cur.description]
            for r in cur.fetchall():
                row = dict(zip(cols, r))
                new = R.derive_asset(row, {}, today)      # pm_quarter is the new label; pm_date is empty -> PENDING (or kept, inside it -> DONE)
                con.execute("UPDATE asset SET pm_status = %s, dq_flags = %s WHERE asset_key = %s", (new["pm_status"], new["dq_flags"], row["asset_key"]))
        con.execute("INSERT INTO portal_audit (editor, client_ip, dataset, record_key, action, changes, reason) VALUES (%s,%s,'assets',%s,'UPDATE',%s::jsonb,%s)",
                    (user["username"], ip, "PM-ROLLOVER", json.dumps({"pm_quarter": {"old": q["label"], "new": nq["label"]}, "assets": {"new": len(keys)}}), f"PM cycle rolled over to {nq['label']}"))
    return {"closed": q["label"], "opened": nq["label"], "assets": len(keys)}


_ = L
