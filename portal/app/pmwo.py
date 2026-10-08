"""Preventive-maintenance work orders: one per in-scope asset per quarter, with a checklist, findings and a two-step sign-off.

Layered on top of the quarterly PM in pm.py, never instead of it: completing a work order writes the same pm_date / pm_done_by to the asset
and the same pm_record row that "Record PM" writes, so the dashboards, snapshots, Past quarters and the e-mails keep working unchanged.

Life cycle (EN 13306 vocabulary, ITIL for the findings):
    OPEN -> IN_PROGRESS (first save) -> COMPLETED (engineer signs; asset PM date written) -> CLOSED
    COMPLETED closes when both gates are met:
      * technical verification - required for criticality A always, and for a fixed, repeatable sample of B and C (see SAMPLE_PCT);
        done by an administrator who is NOT the person who completed the work (segregation of duties);
      * owner acknowledgement - service acceptance by the asset's owner (the person, or the section for shared equipment). Owners cannot
        sign in yet, so an administrator records it on their behalf; after ACK_DAYS without an answer it is DEEMED accepted (and shown as such).
    An owner can DISPUTE: the work order goes back to IN_PROGRESS with a finding. CANCELLED (administrator, asset retired) and a
    deferral (engineer requests, administrator approves a later date) are the other exits.
A failed task becomes a finding. Major/Critical findings are flagged "call required"; the SR ID comes from the CIPL tracker, so it is linked
afterwards (the portal cannot invent one).
"""
import datetime as dt
import hashlib

from . import db, pm
from .pm import PmError

CRITICALITY = {"SERVER": "A", "ROUTER": "A", "SWITCH": "B", "UPS": "B", "MEDIA_CONVERTER": "B"}      # anything else: C
SAMPLE_PCT = {"A": 100, "B": 25, "C": 10}              # share of work orders that need technical verification
OWNER_PERSON = {"DESKTOP", "WORKSTATION"}              # everything else is shared equipment: the owner is the section
ACK_DAYS = 7
OPEN_STATES = ("OPEN", "IN_PROGRESS")
SEVERITIES = ("MINOR", "MAJOR", "CRITICAL")

DDL = [
    """CREATE TABLE IF NOT EXISTS pm_checklist (
         checklist_id SERIAL PRIMARY KEY, asset_class TEXT NOT NULL, name TEXT NOT NULL, version INT NOT NULL DEFAULT 1,
         status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','RETIRED')), created_by TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         UNIQUE (asset_class, version))""",
    """CREATE TABLE IF NOT EXISTS pm_checklist_task (
         task_id SERIAL PRIMARY KEY, checklist_id INT NOT NULL REFERENCES pm_checklist ON DELETE CASCADE, seq INT NOT NULL, text TEXT NOT NULL,
         kind TEXT NOT NULL DEFAULT 'PASSFAIL' CHECK (kind IN ('PASSFAIL','VALUE')), unit TEXT, min_value NUMERIC, max_value NUMERIC,
         mandatory BOOLEAN NOT NULL DEFAULT TRUE, critical BOOLEAN NOT NULL DEFAULT FALSE, every_n INT NOT NULL DEFAULT 1 CHECK (every_n IN (1,2,4)),
         UNIQUE (checklist_id, seq))""",
    "CREATE SEQUENCE IF NOT EXISTS pm_wo_no_seq",
    """CREATE TABLE IF NOT EXISTS pm_work_order (
         wo_id BIGSERIAL PRIMARY KEY, wo_no TEXT NOT NULL UNIQUE, quarter_label TEXT NOT NULL, asset_key TEXT NOT NULL, asset_class TEXT,
         checklist_id INT REFERENCES pm_checklist, criticality TEXT NOT NULL DEFAULT 'C', due_date DATE NOT NULL,
         state TEXT NOT NULL DEFAULT 'OPEN' CHECK (state IN ('OPEN','IN_PROGRESS','COMPLETED','CLOSED','CANCELLED')),
         legacy BOOLEAN NOT NULL DEFAULT FALSE, started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ, completed_by TEXT, done_by TEXT, pm_date DATE,
         minutes_spent INT, remarks TEXT,
         verify_required BOOLEAN NOT NULL DEFAULT FALSE, verified_by TEXT, verified_at TIMESTAMPTZ, verify_note TEXT,
         owner_kind TEXT, owner_cpf INT, owner_name TEXT, owner_dept TEXT,
         ack_state TEXT NOT NULL DEFAULT 'PENDING' CHECK (ack_state IN ('PENDING','ACKNOWLEDGED','DISPUTED','DEEMED','NA')),
         ack_by TEXT, ack_at TIMESTAMPTZ, ack_recorded_by TEXT, ack_note TEXT, ack_due DATE,
         defer_status TEXT NOT NULL DEFAULT 'NONE' CHECK (defer_status IN ('NONE','REQUESTED','APPROVED','DECLINED')), defer_reason TEXT, defer_to DATE, defer_decided_by TEXT,
         cancel_reason TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (quarter_label, asset_key))""",
    "CREATE INDEX IF NOT EXISTS ix_pm_wo_q ON pm_work_order (quarter_label, state)",
    "CREATE INDEX IF NOT EXISTS ix_pm_wo_asset ON pm_work_order (asset_key)",
    """CREATE TABLE IF NOT EXISTS pm_wo_result (
         wo_id BIGINT NOT NULL REFERENCES pm_work_order ON DELETE CASCADE, seq INT NOT NULL, text TEXT NOT NULL, kind TEXT NOT NULL, unit TEXT,
         min_value NUMERIC, max_value NUMERIC, mandatory BOOLEAN NOT NULL, critical BOOLEAN NOT NULL,
         result TEXT CHECK (result IN ('PASS','FAIL','NA')), value NUMERIC, note TEXT, PRIMARY KEY (wo_id, seq))""",
    """CREATE TABLE IF NOT EXISTS pm_finding (
         finding_id BIGSERIAL PRIMARY KEY, wo_id BIGINT NOT NULL REFERENCES pm_work_order ON DELETE CASCADE, asset_key TEXT NOT NULL, quarter_label TEXT NOT NULL,
         seq INT NOT NULL, severity TEXT NOT NULL CHECK (severity IN ('MINOR','MAJOR','CRITICAL')), description TEXT NOT NULL, call_required BOOLEAN NOT NULL DEFAULT FALSE,
         sr_id TEXT, status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED')), close_note TEXT,
         created_by TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), closed_by TEXT, closed_at TIMESTAMPTZ, UNIQUE (wo_id, seq))""",
    "CREATE INDEX IF NOT EXISTS ix_pm_finding_open ON pm_finding (status, severity)",
    """CREATE TABLE IF NOT EXISTS pm_wo_event (
         event_id BIGSERIAL PRIMARY KEY, wo_id BIGINT NOT NULL REFERENCES pm_work_order ON DELETE CASCADE, at TIMESTAMPTZ NOT NULL DEFAULT now(),
         actor TEXT NOT NULL, action TEXT NOT NULL, note TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_pm_wo_event ON pm_wo_event (wo_id, event_id)",
]

# (text, kind, unit, min, max, mandatory, critical, every_n)
_T = lambda text, mand=True, crit=False, n=1: (text, "PASSFAIL", None, None, None, mand, crit, n)          # noqa: E731
_V = lambda text, unit, lo, hi, mand=True, crit=False, n=1: (text, "VALUE", unit, lo, hi, mand, crit, n)    # noqa: E731
STARTER = {
    "DESKTOP": ("Desktop PM", [
        _T("Physical condition: casing, cables and ports undamaged"), _T("Cabinet, vents, fan, keyboard and mouse cleaned of dust"),
        _T("Starts normally; no disk or system errors in the event log"), _T("Antivirus installed, running, definitions current"),
        _T("Operating-system security patches up to date"), _T("Free disk space adequate (15% or more)"),
        _T("Monitor, keyboard and mouse working", mand=False), _T("Asset tag legible and matches the register", mand=False),
        _T("Disk health (SMART) clean", crit=True, n=2), _T("BIOS / firmware reviewed", mand=False, n=4)]),
    "PRINTER": ("Printer PM", [
        _T("Physical condition and paper path clear"), _T("Rollers and exterior cleaned"), _T("Test print: quality acceptable"),
        _T("Toner / ink level checked, spare available", mand=False), _T("Connection (network or USB) stable"),
        _T("Asset tag legible and matches the register", mand=False), _V("Page counter reading", "pages", None, None, mand=False), _T("Firmware reviewed", mand=False, n=2)]),
    "SCANNER": ("Scanner PM", [
        _T("Physical condition: cover, cable and feeder undamaged"), _T("Glass and rollers cleaned"), _T("Test scan: image quality acceptable"),
        _T("Driver / software working with the host PC"), _T("Asset tag legible and matches the register", mand=False)]),
    "SERVER": ("Server PM", [
        _T("Visual check: no alarm LEDs, fans turning, cabling tidy"), _V("Inlet temperature", "C", 10, 35), _T("Disk / RAID health clean", crit=True),
        _T("Power supplies both healthy (redundancy intact)"), _T("Operating-system patches and antivirus current"), _T("Last backup completed successfully within 24 hours", crit=True),
        _T("System and hardware logs reviewed, nothing unresolved"), _T("Dust filters and exterior cleaned", mand=False), _T("Firmware / BIOS / management controller within supported level", n=2),
        _T("Restore of a backup tested", n=4)]),
    "SWITCH": ("Network switch PM", [
        _T("Power, fan and port LEDs normal; no alarms"), _T("Cabling and patching tidy; labels intact", mand=False), _T("Temperature and fan status normal"),
        _T("Interface error counters and logs reviewed"), _T("Uplinks up and stable"), _T("Configuration backup taken and stored", crit=True, n=2), _T("Firmware reviewed against supported level", n=2)]),
    "ROUTER": ("Router PM", [
        _T("Power, fan and link LEDs normal; no alarms"), _T("Cabling and labels intact", mand=False), _T("Temperature and fan status normal"),
        _T("Link status and routing neighbours stable; logs reviewed"), _T("Configuration backup taken and stored", crit=True), _T("Firmware reviewed against supported level", n=2)]),
    "MEDIA_CONVERTER": ("Media converter PM", [
        _T("Power adapter and LEDs normal"), _T("Link LEDs show both sides up"), _T("Fibre patch cords and connectors clean, not bent"), _T("Labelled and matches the register", mand=False)]),
    "UPS": ("UPS PM", [
        _T("Visual check: no alarms, no swelling or leakage, vents clear"), _V("Load", "%", 0, 80), _V("Output voltage", "V", 210, 240),
        _T("Battery test passed (transfer and runtime)", crit=True, n=2), _T("Battery age within service life"), _T("Dust cleaned from vents", mand=False),
        _T("Full-load backup runtime test", crit=True, n=4)]),
}
STARTER["WORKSTATION"] = ("Workstation PM", STARTER["DESKTOP"][1])


def _first(row):
    """First column of a row, whether the connection hands back tuples (the portal) or dicts (the set-up script's admin connection)."""
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def seed_checklists(con, by="system"):
    """Give every class its starter checklist. A class that already has one is left alone, except that a checklist with no tasks
    at all (an earlier run that stopped half-way) is completed. Existing checklists and tasks are never changed or removed."""
    for cls, (name, tasks) in STARTER.items():
        row = con.execute("SELECT checklist_id FROM pm_checklist WHERE asset_class = %s ORDER BY version LIMIT 1", (cls,)).fetchone()
        cid = _first(row) if row else _first(con.execute("INSERT INTO pm_checklist (asset_class, name, version, created_by) VALUES (%s,%s,1,%s) RETURNING checklist_id", (cls, name, by)).fetchone())
        if con.execute("SELECT 1 FROM pm_checklist_task WHERE checklist_id = %s LIMIT 1", (cid,)).fetchone():
            continue
        for i, (text, kind, unit, lo, hi, mand, crit, n) in enumerate(tasks, 1):
            con.execute("INSERT INTO pm_checklist_task (checklist_id, seq, text, kind, unit, min_value, max_value, mandatory, critical, every_n) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (cid, i, text, kind, unit, lo, hi, mand, crit, n))


# ---------------------------------------------------------------- helpers
def _q_index(label):
    return int(label[1]) - 1              # "Q3 OCT-DEC 2026" -> 2 (Apr-Jun = 0)


def _sampled(asset_key, label, pct):
    return int(hashlib.md5(f"{asset_key}|{label}".encode()).hexdigest()[:8], 16) % 100 < pct      # repeatable: the same asset is always in or out for a quarter


def criticality(asset_class):
    return CRITICALITY.get(asset_class, "C")


def _event(con, wo_id, actor, action, note=None):
    con.execute("INSERT INTO pm_wo_event (wo_id, actor, action, note) VALUES (%s,%s,%s,%s)", (wo_id, actor, action, (note or None)))


def _cols(cur):
    return [c.name for c in cur.description]


def _one(con, sql, params=()):
    cur = con.execute(sql, params)
    r = cur.fetchone()
    return dict(zip(_cols(cur), r)) if r else None


def _all(con, sql, params=()):
    cur = con.execute(sql, params)
    cols = _cols(cur)
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _wo(con, wo_id, eng=None, lock=True):
    """The work order (locked for update), or an error. `eng` restricts to a user's own assets; None means no restriction (administrator)."""
    w = _one(con, f"""SELECT w.*, a.engineer_name FROM pm_work_order w LEFT JOIN asset a ON a.asset_key = w.asset_key AND a.is_current = 1
                      WHERE w.wo_id = %s {'FOR UPDATE OF w' if lock else ''}""", (wo_id,))
    if not w:
        raise PmError("That work order does not exist.", 404)
    if eng is not None and (w["engineer_name"] or "") != eng:
        raise PmError("That work order belongs to another engineer.", 403)
    return w


def _settle(con, wo_id):
    w = _one(con, "SELECT state, verify_required, verified_by, ack_state FROM pm_work_order WHERE wo_id = %s", (wo_id,))
    if w["state"] == "COMPLETED" and (not w["verify_required"] or w["verified_by"]) and w["ack_state"] in ("ACKNOWLEDGED", "DEEMED", "NA"):
        con.execute("UPDATE pm_work_order SET state = 'CLOSED' WHERE wo_id = %s", (wo_id,))
        _event(con, wo_id, "system", "CLOSED", "verification and owner acknowledgement complete")


def _checklist_tasks(con, checklist_id, label):
    if not checklist_id:
        return []
    qi = _q_index(label)
    return [t for t in _all(con, "SELECT * FROM pm_checklist_task WHERE checklist_id = %s ORDER BY seq", (checklist_id,)) if qi % t["every_n"] == 0]


def _materialise(con, w):
    """Freeze this quarter's task list into the work order the first time it is worked on. Later checklist edits never change it."""
    if con.execute("SELECT 1 FROM pm_wo_result WHERE wo_id = %s LIMIT 1", (w["wo_id"],)).fetchone():
        return
    for t in _checklist_tasks(con, w["checklist_id"], w["quarter_label"]):
        con.execute("""INSERT INTO pm_wo_result (wo_id, seq, text, kind, unit, min_value, max_value, mandatory, critical) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (w["wo_id"], t["seq"], t["text"], t["kind"], t["unit"], t["min_value"], t["max_value"], t["mandatory"], t["critical"]))


# ---------------------------------------------------------------- generation
def generate(by="system", on=None):
    """Create this quarter's work order for every in-scope asset that does not have one. Safe to run again at any time.
    An asset whose PM was already recorded inside the quarter (imported, or recorded the old way) gets a closed 'legacy' work order,
    so the figures agree and nothing has to be done twice."""
    q = pm.quarter(on or dt.date.today())
    with db.write() as con:
        seed_checklists(con)
        lists = {r[0]: r[1] for r in con.execute("SELECT asset_class, max(checklist_id) FROM pm_checklist WHERE status = 'ACTIVE' GROUP BY 1").fetchall()}
        assets = _all(con, f"""SELECT asset_key, asset_class, user_name, user_department, cpf_no, pm_date, pm_done_by FROM asset
                               WHERE {pm.IN_SCOPE} AND asset_key NOT IN (SELECT asset_key FROM pm_work_order WHERE quarter_label = %s) ORDER BY asset_key""", (q["label"],))
        if not assets:
            return {"quarter": q["label"], "created": 0, "legacy": 0}
        fy = q["start"].year if q["start"].month >= 4 else q["start"].year - 1
        nums = [r[0] for r in con.execute("SELECT nextval('pm_wo_no_seq') FROM generate_series(1, %s)", (len(assets),)).fetchall()]
        legacy = 0
        for a, n in zip(assets, nums):
            crit = criticality(a["asset_class"])
            done_in_q = a["pm_date"] is not None and q["start"] <= a["pm_date"] <= q["end"]
            person = a["asset_class"] in OWNER_PERSON
            common = (f"PMWO-{fy}-{n:05d}", q["label"], a["asset_key"], a["asset_class"], lists.get(a["asset_class"]), crit, q["end"],
                      "PERSON" if person else "SECTION", a["cpf_no"] if person else None, a["user_name"] if person else None, a["user_department"])
            if done_in_q:
                legacy += 1
                con.execute("""INSERT INTO pm_work_order (wo_no, quarter_label, asset_key, asset_class, checklist_id, criticality, due_date, owner_kind, owner_cpf, owner_name, owner_dept,
                                                          state, legacy, completed_at, completed_by, done_by, pm_date, ack_state)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'CLOSED',TRUE,%s,'legacy',%s,%s,'NA')""",
                             (*common, dt.datetime.combine(a["pm_date"], dt.time(12)), a["pm_done_by"], a["pm_date"]))
            else:
                con.execute("""INSERT INTO pm_work_order (wo_no, quarter_label, asset_key, asset_class, checklist_id, criticality, due_date, owner_kind, owner_cpf, owner_name, owner_dept, verify_required)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (*common, _sampled(a["asset_key"], q["label"], SAMPLE_PCT[crit])))
        con.execute("""INSERT INTO pm_wo_event (wo_id, actor, action, note) SELECT wo_id, 'system', 'CREATED', CASE WHEN legacy THEN 'PM already recorded for this quarter' END
                       FROM pm_work_order WHERE quarter_label = %s AND wo_no = ANY(%s)""", (q["label"], [f"PMWO-{fy}-{n:05d}" for n in nums]))
    return {"quarter": q["label"], "created": len(assets), "legacy": legacy}


# ---------------------------------------------------------------- reading
BUCKETS = {
    "open": "w.state IN ('OPEN','IN_PROGRESS')",
    "todo": "w.state = 'OPEN'",
    "progress": "w.state = 'IN_PROGRESS'",
    "overdue": "w.state IN ('OPEN','IN_PROGRESS') AND EFFDUE < CURRENT_DATE",
    "verify": "w.state = 'COMPLETED' AND w.verify_required AND w.verified_by IS NULL",
    "owner": "w.state = 'COMPLETED' AND w.ack_state = 'PENDING'",
    "deferral": "w.defer_status = 'REQUESTED' AND w.state IN ('OPEN','IN_PROGRESS')",
    "closed": "w.state = 'CLOSED'",
    "cancelled": "w.state = 'CANCELLED'",
}
EFFDUE = "CASE WHEN w.defer_status = 'APPROVED' THEN w.defer_to ELSE w.due_date END"


def _where(label, eng, bucket=None, q=None, engineer=None, cls=None, location=None):
    where, p = ["w.quarter_label = %(label)s"], {"label": label}
    if eng is not None:
        where.append("a.engineer_name = %(eng)s"); p["eng"] = eng
    if bucket and bucket != "all":
        if bucket not in BUCKETS:
            raise PmError("Unknown list.")
        where.append("(" + BUCKETS[bucket].replace("EFFDUE", EFFDUE) + ")")
    if engineer:
        where.append("a.engineer_name = %(engineer)s" if engineer != "UNASSIGNED" else "a.engineer_name IS NULL")
        if engineer != "UNASSIGNED":
            p["engineer"] = engineer
    if cls:
        where.append("w.asset_class = %(cls)s"); p["cls"] = cls
    if location:
        where.append("a.location_code = %(loc)s"); p["loc"] = location
    if q:
        where.append("(w.wo_no ILIKE %(q)s OR w.asset_key ILIKE %(q)s OR a.engineer_name ILIKE %(q)s OR a.location_code ILIKE %(q)s OR w.owner_name ILIKE %(q)s OR a.serial_no ILIKE %(q)s)")
        p["q"] = f"%{q}%"
    return " AND ".join(where), p


def _label(label):
    return label or pm.quarter(dt.date.today())["label"]


def summary(eng=None, label=None):
    label = _label(label)
    w, p = _where(label, eng)
    j = "FROM pm_work_order w LEFT JOIN asset a ON a.asset_key = w.asset_key AND a.is_current = 1"
    k = db.one(f"""SELECT count(*) FILTER (WHERE w.state <> 'CANCELLED') AS total,
                          count(*) FILTER (WHERE w.state IN ('COMPLETED','CLOSED')) AS done,
                          count(*) FILTER (WHERE w.state = 'OPEN') AS todo, count(*) FILTER (WHERE w.state = 'IN_PROGRESS') AS progress,
                          count(*) FILTER (WHERE w.state IN ('OPEN','IN_PROGRESS') AND {EFFDUE} < CURRENT_DATE) AS overdue,
                          count(*) FILTER (WHERE w.state = 'COMPLETED' AND w.verify_required AND w.verified_by IS NULL) AS verify,
                          count(*) FILTER (WHERE w.state = 'COMPLETED' AND w.ack_state = 'PENDING') AS owner,
                          count(*) FILTER (WHERE w.defer_status = 'REQUESTED' AND w.state IN ('OPEN','IN_PROGRESS')) AS deferral,
                          count(*) FILTER (WHERE w.state = 'CLOSED') AS closed, count(*) FILTER (WHERE w.state = 'CANCELLED') AS cancelled,
                          count(*) FILTER (WHERE w.ack_state = 'DEEMED') AS deemed, count(*) FILTER (WHERE w.ack_state = 'DISPUTED') AS disputed,
                          count(*) FILTER (WHERE w.legacy) AS legacy
                   {j} WHERE {w}""", p)
    k["pct_done"] = round(100.0 * k["done"] / k["total"], 1) if k["total"] else None
    fw = "f.quarter_label = %(label)s" + (" AND a.engineer_name = %(eng)s" if eng is not None else "")
    f = db.one(f"""SELECT count(*) FILTER (WHERE f.status = 'OPEN') AS open, count(*) FILTER (WHERE f.status = 'OPEN' AND f.severity = 'CRITICAL') AS critical,
                          count(*) FILTER (WHERE f.status = 'OPEN' AND f.call_required AND f.sr_id IS NULL) AS call_needed
                   FROM pm_finding f LEFT JOIN asset a ON a.asset_key = f.asset_key AND a.is_current = 1 WHERE {fw}""", p)
    by_eng = db.query(f"""SELECT coalesce(a.engineer_name, 'UNASSIGNED') AS label, count(*) AS total, count(*) FILTER (WHERE w.state IN ('COMPLETED','CLOSED')) AS done,
                                 count(*) FILTER (WHERE w.state IN ('OPEN','IN_PROGRESS') AND {EFFDUE} < CURRENT_DATE) AS overdue
                          {j} WHERE {w} AND w.state <> 'CANCELLED' GROUP BY 1 ORDER BY count(*) FILTER (WHERE w.state IN ('OPEN','IN_PROGRESS')) DESC, 1""", p)
    by_cls = db.query(f"SELECT w.asset_class AS label, count(*) AS total, count(*) FILTER (WHERE w.state IN ('COMPLETED','CLOSED')) AS done {j} WHERE {w} AND w.state <> 'CANCELLED' GROUP BY 1 ORDER BY 1", p)
    q = pm.quarter(dt.date.today())
    return {"quarter": label, "kpi": k, "findings": f, "by_engineer": by_eng, "by_class": by_cls, "ack_days": ACK_DAYS,
            "generated": bool(db.one("SELECT 1 AS x FROM pm_work_order WHERE quarter_label = %s LIMIT 1", [label])),
            "quarters": [r["quarter_label"] for r in db.query("SELECT DISTINCT quarter_label FROM pm_work_order ORDER BY 1 DESC")], "current": q["label"]}


def list_orders(eng=None, label=None, bucket=None, q=None, engineer=None, cls=None, location=None, limit=300, offset=0):
    label = _label(label)
    w, p = _where(label, eng, bucket, q, engineer, cls, location)
    base = f"FROM pm_work_order w LEFT JOIN asset a ON a.asset_key = w.asset_key AND a.is_current = 1 WHERE {w}"
    total = db.one(f"SELECT count(*) AS n {base}", p)["n"]
    rows = db.query(f"""SELECT w.wo_id, w.wo_no, w.asset_key, w.asset_class, w.criticality, w.state, w.legacy, w.due_date, {EFFDUE} AS eff_due, w.defer_status, w.verify_required, w.verified_by, w.ack_state,
                               w.owner_name, w.owner_dept, w.owner_kind, w.completed_at, w.done_by, w.pm_date, a.engineer_name, a.location_code, trim(coalesce(a.make,'') || ' ' || coalesce(a.model,'')) AS model,
                               ({EFFDUE} < CURRENT_DATE AND w.state IN ('OPEN','IN_PROGRESS')) AS overdue,
                               (SELECT count(*) FROM pm_finding f WHERE f.wo_id = w.wo_id AND f.status = 'OPEN') AS open_findings
                        {base} ORDER BY (w.state IN ('OPEN','IN_PROGRESS')) DESC, {EFFDUE}, a.location_code, w.asset_key LIMIT %(limit)s OFFSET %(offset)s""", {**p, "limit": limit, "offset": offset})
    return {"quarter": label, "total": total, "rows": rows}


def detail(wo_id, eng=None):
    w = db.one("""SELECT w.*, a.engineer_name, a.location_code, a.make, a.model, a.serial_no, a.user_name, a.user_department, a.cover_status
                  FROM pm_work_order w LEFT JOIN asset a ON a.asset_key = w.asset_key AND a.is_current = 1 WHERE w.wo_id = %s""", [wo_id])
    if not w:
        raise PmError("That work order does not exist.", 404)
    if eng is not None and (w["engineer_name"] or "") != eng:
        raise PmError("That work order belongs to another engineer.", 403)
    results = db.query("SELECT * FROM pm_wo_result WHERE wo_id = %s ORDER BY seq", [wo_id])
    if not results and not w["legacy"]:        # not started yet: show what will be asked
        results = [{"seq": t["seq"], "text": t["text"], "kind": t["kind"], "unit": t["unit"], "min_value": t["min_value"], "max_value": t["max_value"], "mandatory": t["mandatory"],
                    "critical": t["critical"], "result": None, "value": None, "note": None}
                   for t in db.query("SELECT * FROM pm_checklist_task WHERE checklist_id = %s ORDER BY seq", [w["checklist_id"]]) if _q_index(w["quarter_label"]) % t["every_n"] == 0] if w["checklist_id"] else []
    w["results"] = results
    w["findings"] = db.query("SELECT * FROM pm_finding WHERE wo_id = %s ORDER BY seq", [wo_id])
    w["events"] = db.query("SELECT at, actor, action, note FROM pm_wo_event WHERE wo_id = %s ORDER BY event_id", [wo_id])
    w["previous"] = db.query("SELECT wo_no, quarter_label, state, pm_date, done_by FROM pm_work_order WHERE asset_key = %s AND wo_id <> %s ORDER BY due_date DESC LIMIT 4", [w["asset_key"], wo_id])
    w["ack_days"] = ACK_DAYS
    return w


# ---------------------------------------------------------------- engineer actions
def _apply_results(con, w, results):
    by_seq = {r["seq"]: r for r in _all(con, "SELECT * FROM pm_wo_result WHERE wo_id = %s", (w["wo_id"],))}
    for r in results or []:
        cur = by_seq.get(int(r.get("seq", -1)))
        if not cur:
            raise PmError("A checklist line was not found on this work order.")
        res, val, note = r.get("result") or None, r.get("value"), (str(r.get("note") or "").strip()[:300] or None)
        if cur["kind"] == "VALUE":
            if res == "NA":
                val = None
            elif val in (None, ""):
                res, val = None, None
            else:
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    raise PmError(f"'{cur['text']}': enter a number.") from None
                lo, hi = cur["min_value"], cur["max_value"]
                res = "FAIL" if (lo is not None and val < float(lo)) or (hi is not None and val > float(hi)) else "PASS"
        else:
            val = None
            if res not in (None, "PASS", "FAIL", "NA"):
                raise PmError("Each line is Pass, Fail or N/A.")
        con.execute("UPDATE pm_wo_result SET result = %s, value = %s, note = %s WHERE wo_id = %s AND seq = %s", (res, val, note, w["wo_id"], cur["seq"]))


def save(wo_id, results, minutes, remarks, user, eng=None):
    with db.write() as con:
        w = _wo(con, wo_id, eng)
        if w["state"] not in OPEN_STATES:
            raise PmError("This work order is already completed.", 409)
        _materialise(con, w)
        _apply_results(con, w, results)
        mins = None if minutes in (None, "") else max(0, min(int(minutes), 24 * 60))
        con.execute("UPDATE pm_work_order SET state = 'IN_PROGRESS', started_at = coalesce(started_at, now()), minutes_spent = coalesce(%s, minutes_spent), remarks = %s WHERE wo_id = %s",
                    (mins, (str(remarks or "").strip()[:300] or None), wo_id))
        if w["state"] == "OPEN":
            _event(con, wo_id, user["username"], "STARTED")
    return detail(wo_id, eng)


def _severity(r):
    return "CRITICAL" if r["critical"] else "MAJOR" if r["mandatory"] else "MINOR"


def _complete_in(con, w, pm_date, user, ip, via_batch=False):
    problems = []
    rows = _all(con, "SELECT * FROM pm_wo_result WHERE wo_id = %s ORDER BY seq", (w["wo_id"],))
    for r in rows:
        if r["result"] is None and r["mandatory"]:
            problems.append(f"'{r['text']}' has not been answered")
        elif r["result"] == "FAIL" and not (r["note"] or "").strip():
            problems.append(f"'{r['text']}' failed: say what you found")
        elif r["result"] == "NA" and r["mandatory"] and not (r["note"] or "").strip():
            problems.append(f"'{r['text']}' is N/A: say why")
    if problems:
        raise PmError("Cannot complete: " + "; ".join(problems[:4]) + ("…" if len(problems) > 4 else "."))
    d, q = pm._parse_pm_date(pm_date or dt.date.today().isoformat())
    if q["label"] != w["quarter_label"]:
        raise PmError(f"This work order belongs to {w['quarter_label']}; only the current quarter's work can be completed.", 409)
    done_by = w["engineer_name"] or user["display_name"]
    pm.record_in(con, [w["asset_key"]], d, q, done_by, None, f"PM work order {w['wo_no']}", user, ip)
    for r in rows:
        if r["result"] != "FAIL":
            continue
        sev = _severity(r)
        desc = f"{r['text']}" + (f" - reading {r['value']:g} {r['unit'] or ''}".rstrip() if r["value"] is not None else "") + f": {r['note']}"
        con.execute("""INSERT INTO pm_finding (wo_id, asset_key, quarter_label, seq, severity, description, call_required, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (wo_id, seq) DO UPDATE SET description = EXCLUDED.description WHERE pm_finding.status = 'OPEN'""",
                     (w["wo_id"], w["asset_key"], w["quarter_label"], r["seq"], sev, desc[:500], sev in ("MAJOR", "CRITICAL"), user["username"]))
    con.execute("""UPDATE pm_work_order SET state = 'COMPLETED', completed_at = now(), completed_by = %s, done_by = %s, pm_date = %s, ack_state = 'PENDING',
                          ack_due = %s, ack_by = NULL, ack_at = NULL, ack_note = NULL, verified_by = NULL, verified_at = NULL, verify_note = NULL WHERE wo_id = %s""",
                 (user["username"], done_by, d, d + dt.timedelta(days=ACK_DAYS), w["wo_id"]))
    n_fail = sum(1 for r in rows if r["result"] == "FAIL")
    _event(con, w["wo_id"], user["username"], "COMPLETED_BATCH" if via_batch else "COMPLETED", f"{n_fail} finding(s)" if n_fail else None)


def complete(wo_id, pm_date, user, ip, eng=None):
    with db.write() as con:
        w = _wo(con, wo_id, eng)
        if w["state"] not in OPEN_STATES:
            raise PmError("This work order is already completed.", 409)
        _materialise(con, w)
        _complete_in(con, w, pm_date, user, ip)
    return detail(wo_id, eng)


def batch_complete(wo_ids, pm_date, minutes, user, ip, eng=None):
    """The batch sheet: every unanswered Pass/Fail line becomes Pass (answers already given, including Fails, are kept). Work orders with a
    reading to take cannot be done this way. Each is its own savepoint, so one problem does not stop the rest. Logged as COMPLETED_BATCH."""
    done, skipped = 0, []
    mins = None if minutes in (None, "") else max(0, min(int(minutes), 24 * 60))
    with db.write() as con:
        for wid in list(dict.fromkeys(int(x) for x in (wo_ids or [])))[:500]:
            w = _one(con, "SELECT wo_no FROM pm_work_order WHERE wo_id = %s", (wid,))
            try:
                with con.transaction():
                    w = _wo(con, wid, eng)
                    if w["state"] not in OPEN_STATES:
                        raise PmError("already completed")
                    _materialise(con, w)
                    blocked = con.execute("SELECT 1 FROM pm_wo_result WHERE wo_id = %s AND kind = 'VALUE' AND mandatory AND result IS NULL LIMIT 1", (wid,)).fetchone()
                    if blocked:
                        raise PmError("it needs a reading - open it and complete it one by one")
                    con.execute("UPDATE pm_wo_result SET result = 'PASS' WHERE wo_id = %s AND kind = 'PASSFAIL' AND result IS NULL", (wid,))
                    con.execute("UPDATE pm_work_order SET started_at = coalesce(started_at, now()), minutes_spent = coalesce(%s, minutes_spent) WHERE wo_id = %s", (mins, wid))
                    _complete_in(con, w, pm_date, user, ip, via_batch=True)
                done += 1
            except PmError as e:
                skipped.append({"wo_no": (w or {}).get("wo_no", str(wid)), "reason": str(e)})
    return {"completed": done, "skipped": skipped}


def defer_request(wo_id, reason, to_date, user, eng=None):
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise PmError("Give the reason for the deferral.")
    try:
        to = dt.date.fromisoformat(str(to_date)[:10])
    except ValueError:
        raise PmError("Choose the new date.") from None
    with db.write() as con:
        w = _wo(con, wo_id, eng)
        if w["state"] not in OPEN_STATES:
            raise PmError("Only work that is still open can be deferred.", 409)
        if to <= w["due_date"] or to > w["due_date"] + dt.timedelta(days=60):
            raise PmError(f"The new date must be after {w['due_date']:%d %b %Y} and within 60 days of it.")
        con.execute("UPDATE pm_work_order SET defer_status = 'REQUESTED', defer_reason = %s, defer_to = %s, defer_decided_by = NULL WHERE wo_id = %s", (reason[:300], to, wo_id))
        _event(con, wo_id, user["username"], "DEFER_REQUESTED", f"to {to:%d %b %Y}: {reason}")
    return detail(wo_id, eng)


# ---------------------------------------------------------------- administrator actions
def defer_decide(wo_id, approve, note, user):
    with db.write() as con:
        w = _wo(con, wo_id)
        if w["defer_status"] != "REQUESTED":
            raise PmError("There is no deferral waiting for a decision.", 409)
        con.execute("UPDATE pm_work_order SET defer_status = %s, defer_decided_by = %s WHERE wo_id = %s", ("APPROVED" if approve else "DECLINED", user["username"], wo_id))
        _event(con, wo_id, user["username"], "DEFER_APPROVED" if approve else "DEFER_DECLINED", note)
    return detail(wo_id)


def verify(wo_ids, approve, note, user):
    note = (note or "").strip()
    if not approve and len(note) < 5:
        raise PmError("Say why the work is being sent back.")
    done, skipped = 0, []
    with db.write() as con:
        for wid in list(dict.fromkeys(int(x) for x in (wo_ids or [])))[:500]:
            w = _wo(con, wid)
            why = None
            if w["state"] != "COMPLETED" or not w["verify_required"] or w["verified_by"]:
                why = "not waiting for verification"
            elif (w["completed_by"] or "").upper() == user["username"].upper():
                why = "you completed it yourself - someone else must verify it"
            if why:
                skipped.append({"wo_no": w["wo_no"], "reason": why}); continue
            if approve:
                con.execute("UPDATE pm_work_order SET verified_by = %s, verified_at = now(), verify_note = %s WHERE wo_id = %s", (user["username"], note[:300] or None, wid))
                _event(con, wid, user["username"], "VERIFIED", note)
                _settle(con, wid)
            else:
                con.execute("UPDATE pm_work_order SET state = 'IN_PROGRESS', completed_at = NULL, ack_state = 'PENDING' WHERE wo_id = %s", (wid,))
                _event(con, wid, user["username"], "VERIFICATION_REJECTED", note)
            done += 1
    return {"done": done, "skipped": skipped}


def acknowledge(wo_ids, action, by, note, user, ip):
    """An administrator records the owner's answer (the owner cannot sign in yet). ACK: accepted. DISPUTE: sent back to the engineer."""
    action, by, note = (action or "").upper(), (by or "").strip(), (note or "").strip()
    if action not in ("ACK", "DISPUTE"):
        raise PmError("Choose acknowledge or dispute.")
    if action == "DISPUTE" and len(note) < 5:
        raise PmError("Say what the owner disputes.")
    done, skipped = 0, []
    with db.write() as con:
        for wid in list(dict.fromkeys(int(x) for x in (wo_ids or [])))[:500]:
            w = _wo(con, wid)
            if w["state"] != "COMPLETED" or w["ack_state"] != "PENDING":
                skipped.append({"wo_no": w["wo_no"], "reason": "not waiting for the owner"}); continue
            who = (by or w["owner_name"] or w["owner_dept"] or "").strip()
            if action == "ACK":
                if not who:
                    skipped.append({"wo_no": w["wo_no"], "reason": "type the name of the person who confirmed (no owner is recorded)"}); continue
                con.execute("UPDATE pm_work_order SET ack_state = 'ACKNOWLEDGED', ack_by = %s, ack_at = now(), ack_recorded_by = %s, ack_note = %s WHERE wo_id = %s", (who[:80], user["username"], note[:300] or None, wid))
                if con.execute("SELECT 1 FROM asset WHERE asset_key = %s AND is_current = 1 AND pm_date = %s", (w["asset_key"], w["pm_date"])).fetchone():
                    from . import edit
                    edit.update("assets", w["asset_key"], {"pm_signed_by": who[:80]}, {}, user["username"], ip, f"PM {w['wo_no']} acknowledged by owner", con=con)
                _event(con, wid, user["username"], "OWNER_ACKNOWLEDGED", f"{who}{': ' + note if note else ''}")
                _settle(con, wid)
            else:
                con.execute("UPDATE pm_work_order SET state = 'IN_PROGRESS', completed_at = NULL, ack_state = 'DISPUTED', ack_by = %s, ack_at = now(), ack_recorded_by = %s, ack_note = %s WHERE wo_id = %s",
                            (who[:80] or None, user["username"], note[:300], wid))
                con.execute("""INSERT INTO pm_finding (wo_id, asset_key, quarter_label, seq, severity, description, call_required, created_by) VALUES (%s,%s,%s,0,'MAJOR',%s,TRUE,%s)
                               ON CONFLICT (wo_id, seq) DO UPDATE SET description = EXCLUDED.description, status = 'OPEN', closed_at = NULL, closed_by = NULL""",
                             (wid, w["asset_key"], w["quarter_label"], f"Owner disputes the PM: {note}"[:500], user["username"]))
                _event(con, wid, user["username"], "OWNER_DISPUTED", f"{who}: {note}")
            done += 1
    return {"done": done, "skipped": skipped}


def deem_overdue_acks():
    """Owners who have not answered within ACK_DAYS are deemed to have accepted. Visible as DEEMED everywhere; run daily."""
    with db.write() as con:
        ids = [r[0] for r in con.execute("UPDATE pm_work_order SET ack_state = 'DEEMED', ack_at = now() WHERE state = 'COMPLETED' AND ack_state = 'PENDING' AND ack_due < CURRENT_DATE RETURNING wo_id").fetchall()]
        for wid in ids:
            _event(con, wid, "system", "OWNER_DEEMED", f"no answer within {ACK_DAYS} days")
            _settle(con, wid)
    return len(ids)


def cancel(wo_id, reason, user):
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise PmError("Give the reason (for example: asset retired or replaced).")
    with db.write() as con:
        w = _wo(con, wo_id)
        if w["state"] not in OPEN_STATES:
            raise PmError("Only work that is still open can be cancelled.", 409)
        con.execute("UPDATE pm_work_order SET state = 'CANCELLED', cancel_reason = %s WHERE wo_id = %s", (reason[:300], wo_id))
        _event(con, wo_id, user["username"], "CANCELLED", reason)
    return detail(wo_id)


# ---------------------------------------------------------------- findings
def findings(eng=None, status="OPEN", label=None, limit=300):
    where, p = ["1 = 1"], {"limit": limit}
    if status in ("OPEN", "CLOSED"):
        where.append("f.status = %(status)s"); p["status"] = status
    if label:
        where.append("f.quarter_label = %(label)s"); p["label"] = label
    if eng is not None:
        where.append("a.engineer_name = %(eng)s"); p["eng"] = eng
    rows = db.query(f"""SELECT f.finding_id, f.wo_id, w.wo_no, f.asset_key, w.asset_class, f.quarter_label, f.severity, f.description, f.call_required, f.sr_id, f.status, f.close_note,
                               f.created_at, f.closed_by, f.closed_at, a.engineer_name, a.location_code
                        FROM pm_finding f JOIN pm_work_order w ON w.wo_id = f.wo_id LEFT JOIN asset a ON a.asset_key = f.asset_key AND a.is_current = 1
                        WHERE {' AND '.join(where)} ORDER BY (f.status = 'OPEN') DESC, array_position(ARRAY['CRITICAL','MAJOR','MINOR'], f.severity), f.created_at DESC LIMIT %(limit)s""", p)
    return {"rows": rows}


def finding_update(finding_id, changes, user, eng=None):
    with db.write() as con:
        f = _one(con, """SELECT f.*, a.engineer_name FROM pm_finding f LEFT JOIN asset a ON a.asset_key = f.asset_key AND a.is_current = 1 WHERE f.finding_id = %s FOR UPDATE OF f""", (finding_id,))
        if not f:
            raise PmError("That finding does not exist.", 404)
        if eng is not None and (f["engineer_name"] or "") != eng:
            raise PmError("That finding belongs to another engineer.", 403)
        if f["status"] == "CLOSED":
            raise PmError("This finding is closed.", 409)
        if changes.get("severity"):
            sev = str(changes["severity"]).upper()
            if sev not in SEVERITIES:
                raise PmError("Severity is Minor, Major or Critical.")
            if eng is not None and SEVERITIES.index(sev) < SEVERITIES.index(f["severity"]):
                raise PmError("Only an administrator can lower a finding's severity.", 403)
            con.execute("UPDATE pm_finding SET severity = %s, call_required = %s WHERE finding_id = %s", (sev, sev in ("MAJOR", "CRITICAL"), finding_id))
            _event(con, f["wo_id"], user["username"], "FINDING_SEVERITY", f"finding {f['seq']}: {f['severity']} to {sev}")
        if changes.get("sr_id"):
            sr = str(changes["sr_id"]).strip().upper()
            if not con.execute("SELECT 1 FROM svc_call WHERE sr_id = %s AND is_current = 1", (sr,)).fetchone():
                raise PmError(f"No call with SR ID {sr} is in the call tracker yet. Raise it with CIPL first, then link it here.")
            con.execute("UPDATE pm_finding SET sr_id = %s WHERE finding_id = %s", (sr, finding_id))
            _event(con, f["wo_id"], user["username"], "FINDING_CALL_LINKED", f"finding {f['seq']} linked to {sr}")
        if changes.get("close"):
            note = str(changes.get("note") or "").strip()
            if len(note) < 5:
                raise PmError("Say what was done to resolve it.")
            con.execute("UPDATE pm_finding SET status = 'CLOSED', close_note = %s, closed_by = %s, closed_at = now() WHERE finding_id = %s", (note[:300], user["username"], finding_id))
            _event(con, f["wo_id"], user["username"], "FINDING_CLOSED", f"finding {f['seq']}: {note}")
    return {"ok": True}


def checklists():
    rows = db.query("""SELECT c.checklist_id, c.asset_class, c.name, c.version, c.status, t.seq, t.text, t.kind, t.unit, t.min_value, t.max_value, t.mandatory, t.critical, t.every_n
                       FROM pm_checklist c JOIN pm_checklist_task t USING (checklist_id) ORDER BY c.asset_class, c.version, t.seq""")
    out = {}
    for r in rows:
        c = out.setdefault(r["checklist_id"], {k: r[k] for k in ("checklist_id", "asset_class", "name", "version", "status")} | {"tasks": []})
        c["tasks"].append({k: r[k] for k in ("seq", "text", "kind", "unit", "min_value", "max_value", "mandatory", "critical", "every_n")})
    return {"checklists": list(out.values()), "criticality": CRITICALITY, "sample_pct": SAMPLE_PCT, "ack_days": ACK_DAYS}
