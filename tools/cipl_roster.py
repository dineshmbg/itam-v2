"""CIPL employee roster -> standard CIPL_Employee_Master workbook + PostgreSQL (history, onboarding, lifecycle events).

  python tools/cipl_roster.py --build-template
  python tools/cipl_roster.py --raw "CIPL Employee Details 2026.xlsx" [--as-of 2026-09-21] [--out-dir masters] [--no-load-db] [--force]
  python tools/cipl_roster.py event --ecode A006584 --type RESIGNED --date 2026-10-31 [--last-working-date 2026-10-31] [--reason "..."]
  python tools/cipl_roster.py report

Sensitive fields (Aadhaar, UAN, bank account, IFSC) are NEVER read: columns are picked by header name and these are not on the list.
"""
import argparse
import collections
import datetime as dt
import os
import re
import sys
from pathlib import Path

import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

sys.path.insert(0, str(Path(__file__).parent))
import itam_locks  # noqa: E402

TEMPLATE_NAME = "CIPL_Employee_Master_Upload_Template.xlsx"
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
EXIT_STATUSES = {"RESIGNED", "TERMINATED", "TRANSFERRED"}
STATUSES = ["ACTIVE", "RESIGNED", "TERMINATED", "TRANSFERRED", "LEFT_ROSTER"]
ITEMS = ["JOINING_KIT", "ID_CARD", "ONSURITY", "MEDICAL_ESIC", "POLICE_VERIFICATION", "SALARY_ACCOUNT", "JACKETS", "ONGC_GATEPASS"]
ITEM_SRC = {"JOINING KIT": "JOINING_KIT", "ID CARD": "ID_CARD", "ONSURITY": "ONSURITY", "MEDICAL/ESIC": "MEDICAL_ESIC",
            "POLICE VERIFICATION": "POLICE_VERIFICATION", "SALARY ACCOUNT": "SALARY_ACCOUNT", "JACKETS": "JACKETS",
            "ONGC GATEPASS": "ONGC_GATEPASS"}
EXCLUDED_SENSITIVE = ["BANK ACCOUNT NO", "IFSC CODE", "Aadhar No.", "UAN No.", "ESIC IP No.", "Father's / Mother's / Spouse / Children names",
                      "Present / Permanent address", "Blood group", "Medical fitness certificate no.", "Group insurance / gratuity / mediclaim policy nos."]
BAD_EMAIL_DOMAIN = {"yhoo.com": "yahoo.com", "gmial.com": "gmail.com", "gamil.com": "gmail.com", "hotmial.com": "hotmail.com"}

# (column, group, description, dtype)
MASTER = [
    ("SNAPSHOT_DATE", "TRACE", "Date of the roster snapshot this row came from.", "date"),
    ("ECODE", "IDENTITY", "CIPL employee code (UNIQUE KEY, e.g. A003541).", "text"),
    ("EMPLOYEE_NAME", "IDENTITY", "Full name.", "text"),
    ("MOBILE_NO", "IDENTITY", "10-digit mobile, stored as text.", "text"),
    ("COMPANY_EMAIL", "IDENTITY", "CIPL company email.", "text"),
    ("PERSONAL_EMAIL", "IDENTITY", "Personal email.", "text"),
    ("DATE_OF_BIRTH", "IDENTITY", "From the ONGC registration form (CIPL (2)) when the employee is found there.", "date"),
    ("GENDER", "IDENTITY", "M / F, from the registration form.", "text"),
    ("DESIGNATION", "JOB", "Current designation.", "text"),
    ("LEVEL", "JOB", "SQ1 / SQ2 / SQ3 / Office Boy.", "text"),
    ("EDUCATION", "JOB", "Education qualification.", "text"),
    ("CERTIFICATIONS", "JOB", "Certifications, comma separated.", "text"),
    ("EXPERIENCE_TEXT", "JOB", "Experience exactly as written in the roster.", "text"),
    ("EXPERIENCE_YEARS", "JOB", "First number found in EXPERIENCE_TEXT (derived).", "num"),
    ("CLIENT", "ASSIGNMENT", "Client (ONGC).", "text"),
    ("LOCATION", "ASSIGNMENT", "Work location.", "text"),
    ("SKILL_CATEGORY", "ASSIGNMENT", "US / SS / S / HS / Supervisor / Executive (registration form).", "text"),
    ("DEPLOYED_AT", "ASSIGNMENT", "Office duty / field duty etc. (registration form).", "text"),
    ("DUTY_PATTERN", "ASSIGNMENT", "General / shift / 14-day (registration form).", "text"),
    ("ONGC_GATEPASS_NO", "ASSIGNMENT", "ONGC gate-pass / non-employee duty-pass number.", "text"),
    ("EMPLOYMENT_STATUS", "LIFECYCLE", "ACTIVE / RESIGNED / TERMINATED / TRANSFERRED / LEFT_ROSTER (dropped off the roster, exit unconfirmed).", "text"),
    ("DATE_OF_JOINING_ONGC", "LIFECYCLE", "Initial deployment date at ONGC (registration form).", "date"),
    ("DATE_CURRENT_CONTRACT", "LIFECYCLE", "Deployment date under the present contract (registration form).", "date"),
    ("RESIGNATION_DATE", "LIFECYCLE", "Date resignation / termination was given (set by an exit event).", "date"),
    ("LAST_WORKING_DATE", "LIFECYCLE", "Last working day.", "date"),
    ("EXIT_REASON", "LIFECYCLE", "Reason for exit.", "text"),
    ("ONBOARDING_STATUS", "ONBOARDING", "COMPLETE / PARTIAL / NOT_STARTED (derived from the checklist).", "text"),
    ("ONBOARDING_PENDING_ITEMS", "ONBOARDING", "Checklist items still pending.", "text"),
    ("REMARKS", "TRACE", "Free notes (stray values moved out of the wrong column land here).", "text"),
    ("DQ_FLAGS", "TRACE", "Semicolon-separated data-quality flags.", "text"),
    ("SOURCE_ROW", "TRACE", "Row number in the source roster sheet.", "int"),
]
COLS = [m[0] for m in MASTER]
DTYPE = {m[0]: m[3] for m in MASTER}
DATE_COLS = {c for c in COLS if DTYPE[c] == "date"}
CHECK_COLS = ["SNAPSHOT_DATE", "ECODE", "ITEM", "STATUS", "ITEM_VALUE", "STATUS_DATE"]
EVENT_COLS = ["SNAPSHOT_DATE", "ECODE", "EVENT_TYPE", "FIELD", "OLD_VALUE", "NEW_VALUE", "EVENT_DATE", "REASON", "SOURCE"]
TRACKED = [c for c in COLS if c not in {"SNAPSHOT_DATE", "ECODE", "EMPLOYMENT_STATUS", "ONBOARDING_STATUS", "ONBOARDING_PENDING_ITEMS",
                                         "DQ_FLAGS", "SOURCE_ROW", "REMARKS", "RESIGNATION_DATE", "LAST_WORKING_DATE", "EXIT_REASON"}]
GROUP_FILL = {"IDENTITY": "1F3864", "JOB": "2E75B6", "ASSIGNMENT": "548235", "LIFECYCLE": "C00000", "ONBOARDING": "7030A0", "TRACE": "595959"}

F_BASE = Font(name="Arial", size=10)
F_BOLD = Font(name="Arial", size=10, bold=True)
F_HDR = Font(name="Arial", size=10, bold=True, color="FFFFFF")
F_TITLE = Font(name="Arial", size=14, bold=True)
THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
DATE_FMT = "DD-MMM-YYYY"


# ------------------------------------------------------------------ helpers
def blank(v):
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(v, str) and v.strip() in ("", "-", "NA", "N/A", "//")


def txt(v):
    return None if blank(v) else re.sub(r"\s+", " ", str(v).replace("\xa0", " ")).strip()


def to_date(v):
    if blank(v):
        return None
    if isinstance(v, pd.Timestamp):
        return v.date()
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    d = pd.to_datetime(str(v), errors="coerce", dayfirst=not re.match(r"\d{4}-\d{2}-\d{2}", str(v)) and "/" not in str(v))
    return None if pd.isna(d) else d.date()


def mob10(v):
    d = re.sub(r"\D", "", str(int(v)) if isinstance(v, float) and not pd.isna(v) else str(v or ""))
    return d[-10:] if len(d) >= 10 else None


def nname(v):
    return re.sub(r"[^A-Z]", "", str(v or "").upper())


def hdr(h):
    return re.sub(r"\s+", " ", str(h or "")).strip().upper()


def style_header(ws, names, fills=None):
    for i, n in enumerate(names, 1):
        c = ws.cell(row=1, column=i, value=n)
        c.font, c.border = F_HDR, BOX
        c.fill = PatternFill("solid", fgColor=(fills[i - 1] if fills else "1F3864"))
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def simple_sheet(wb, title, header, rows, widths):
    ws = wb.create_sheet(title)
    style_header(ws, header)
    for r in rows:
        ws.append(list(r))
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.font, c.alignment = F_BASE, Alignment(wrap_text=True, vertical="top")
            if isinstance(c.value, (dt.date, dt.datetime)):
                c.number_format = DATE_FMT
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(header))}{max(ws.max_row, 2)}"
    return ws


# ------------------------------------------------------------------ extraction
def find_header_row(ws, must, limit=8):
    for r in range(1, limit + 1):
        vals = [hdr(c.value) for c in ws[r]]
        if all(m in vals for m in must):
            return r, vals
    return None, None


def read_roster(path):
    wb = load_workbook(path, data_only=True)
    sheet = None
    for ws in wb:
        r, h = find_header_row(ws, ["ECODE", "ENAME"])
        if r and "DESIGNATION" in h:
            sheet, hrow, heads = ws, r, h
            break
    if sheet is None:
        sys.exit("No roster sheet with ECODE / ENAME / DESIGNATION headers found.")
    col = {h: i for i, h in enumerate(heads)}
    need = ["ECODE", "ENAME", "MOBILE", "DESIGNATION", "EMAIL", "LEVEL"]
    miss = [n for n in need if n not in col]
    if miss:
        sys.exit(f"Roster is missing expected columns: {miss}")
    recs = []
    for r in range(hrow + 1, sheet.max_row + 1):
        row = [c.value for c in sheet[r]]
        if blank(row[col["ECODE"]]):
            continue
        g = lambda k: row[col[k]] if k in col and col[k] < len(row) else None  # noqa: E731
        rec = {"SOURCE_ROW": r, "ECODE": txt(g("ECODE")).upper(), "EMPLOYEE_NAME": txt(g("ENAME")), "MOBILE_NO": mob10(g("MOBILE")),
               "COMPANY_EMAIL": txt(g("EMAIL")), "PERSONAL_EMAIL": txt(g("PERSONAL EMAIL")), "DESIGNATION": txt(g("DESIGNATION")),
               "LEVEL": txt(g("LEVEL")), "EDUCATION": txt(g("EDUCATION QUALIFICATION")), "CERTIFICATIONS": txt(g("CERTIFICATION")),
               "EXPERIENCE_TEXT": txt(g("EXPERIENCE")), "CLIENT": txt(g("CLIENT")), "LOCATION": txt(g("LOCATION")),
               "ONGC_GATEPASS_NO": txt(g("ONGC GATEPASS")), "_items": {}}
        for src, item in ITEM_SRC.items():
            rec["_items"][item] = txt(g(src))
        recs.append(rec)
    known = set(ITEM_SRC) | {"SR", "ECODE", "ENAME", "MOBILE", "DESIGNATION", "EMAIL", "PERSONAL EMAIL", "CERTIFICATION", "EDUCATION QUALIFICATION",
                             "EXPERIENCE", "LEVEL", "CLIENT", "LOCATION", "BANK ACCOUNT NO", "IFSC CODE"}
    new_cols = [h for h in heads if h and h not in known]
    return recs, sheet.title, new_cols


def read_form(path):
    """CIPL (2) registration form: only whitelisted columns are read (never Aadhaar / UAN / bank)."""
    wb = load_workbook(path, data_only=True)
    for ws in wb:
        for r in range(1, 6):
            vals = [hdr(c.value) for c in ws[r]]
            if any(v.startswith("NAME OF CONTRACT LABOUR") for v in vals):
                heads = vals
                break
        else:
            continue
        def find(*keys, none_of=()):
            for i, v in enumerate(heads):
                if all(k in v for k in keys) and not any(n in v for n in none_of):
                    return i
            return None
        idx = {"NAME": find("NAME OF CONTRACT LABOUR"), "MOBILE": find("CONTACT NUMBER OF THE WORKER"), "DOB": find("DATE OF BIRTH"),
               "GENDER": find("GENDER"), "INIT": find("INITIAL DATE OF DEPLOYMENT"), "CUR": find("DATE OF DEPLOYMENT UNDER THE PRESENT CONTRACT"),
               "TERM_DATE": find("TERMINATION", "DATE", none_of=("REASON",)), "TERM_REASON": find("TERMINATION", "REASON"),
               "SKILL": find("SKILL CATEGORY"), "DEPLOYED": find("DEPLOYED AT"), "DUTY": find("DUTY PATTERN"), "PASS": find("NON EMPLOYEE DUTY PASS")}
        out = []
        for r in range(r + 1, ws.max_row + 1):
            row = [c.value for c in ws[r]]
            nm = row[idx["NAME"]] if idx["NAME"] is not None and idx["NAME"] < len(row) else None
            if blank(nm) or not re.search(r"[A-Za-z]{3}", str(nm)):
                continue
            g = lambda k: row[idx[k]] if idx.get(k) is not None and idx[k] < len(row) else None  # noqa: E731
            if blank(g("MOBILE")) and blank(g("INIT")):
                continue
            out.append({"row": r, "name": txt(nm), "mobile": mob10(g("MOBILE")), "dob": to_date(g("DOB")),
                        "gender": {"MALE": "M", "FEMALE": "F"}.get(str(g("GENDER") or "").strip().upper()),
                        "init": to_date(g("INIT")), "cur": to_date(g("CUR")), "term_date": to_date(g("TERM_DATE")),
                        "term_reason": txt(g("TERM_REASON")), "skill": txt(g("SKILL")), "deployed": txt(g("DEPLOYED")),
                        "duty": txt(g("DUTY")), "pass": txt(g("PASS"))})
        return out, ws.title
    return [], None


def read_orphan_lists(path, roster_sheet):
    wb = load_workbook(path, data_only=True)
    res = []
    for ws in wb:
        if ws.title == roster_sheet:
            continue
        r, h = find_header_row(ws, ["ECODE", "ENAME"], limit=12)
        if not r:
            continue
        # header cells may start in any column
        first = next(i for i, c in enumerate(ws[r]) if hdr(c.value) == "ECODE")
        hm = {hdr(c.value): i for i, c in enumerate(ws[r]) if c.value}
        for rr in range(r + 1, ws.max_row + 1):
            row = [c.value for c in ws[rr]]
            if blank(row[hm["ECODE"]]):
                continue
            res.append((ws.title, txt(row[hm["ECODE"]]).upper(), txt(row[hm["ENAME"]]), mob10(row[hm.get("MOBILE", first)]) if "MOBILE" in hm else None))
    return res


def build_frames(recs, form, as_of):
    by_m = {f["mobile"]: f for f in form if f["mobile"]}
    by_n = {nname(f["name"]): f for f in form}
    used = set()
    rows, checks = [], []
    for r in recs:
        f = by_m.get(r["MOBILE_NO"]) or by_n.get(nname(r["EMPLOYEE_NAME"]))
        flags, remarks = [], []
        row = {c: None for c in COLS}
        row.update({k: v for k, v in r.items() if k in COLS})
        row["SNAPSHOT_DATE"] = as_of
        row["EMPLOYMENT_STATUS"] = "ACTIVE"
        # cleaning
        pe = row["PERSONAL_EMAIL"]
        if pe:
            dom = pe.split("@")[-1].lower()
            if dom in BAD_EMAIL_DOMAIN:
                row["PERSONAL_EMAIL"] = pe.rsplit("@", 1)[0] + "@" + BAD_EMAIL_DOMAIN[dom]
                flags.append("PERSONAL_EMAIL_DOMAIN_FIXED")
        else:
            flags.append("PERSONAL_EMAIL_MISSING")
        cert = row["CERTIFICATIONS"]
        if cert and cert.upper() in {"CHAPRASHI", "PEON", "OFFICE BOY"}:
            remarks.append(f"Certification field held: {cert}")
            row["CERTIFICATIONS"] = None
            flags.append("CERT_FIELD_MOVED_TO_REMARKS")
        m = re.search(r"\d+(\.\d+)?", row["EXPERIENCE_TEXT"] or "")
        row["EXPERIENCE_YEARS"] = float(m.group()) if m else None
        for c, fl in (("CLIENT", "MISSING_CLIENT"), ("LOCATION", "MISSING_LOCATION"), ("EDUCATION", "MISSING_EDUCATION"),
                      ("EXPERIENCE_TEXT", "MISSING_EXPERIENCE")):
            if not row[c]:
                flags.append(fl)
        if not row["MOBILE_NO"]:
            flags.append("MOBILE_INVALID")
        if f:
            used.add(f["row"])
            row.update(DATE_OF_BIRTH=f["dob"], GENDER=f["gender"], SKILL_CATEGORY=f["skill"], DEPLOYED_AT=f["deployed"], DUTY_PATTERN=f["duty"],
                       DATE_OF_JOINING_ONGC=f["init"], DATE_CURRENT_CONTRACT=f["cur"])
            if f["term_date"]:
                row.update(EMPLOYMENT_STATUS="TERMINATED", RESIGNATION_DATE=f["term_date"], LAST_WORKING_DATE=f["term_date"], EXIT_REASON=f["term_reason"])
                flags.append("EXIT_FROM_REGISTRATION_FORM")
            if f["pass"] and row["ONGC_GATEPASS_NO"] and re.sub(r"\W", "", f["pass"]).upper() != re.sub(r"\W", "", row["ONGC_GATEPASS_NO"]).upper():
                flags.append("GATEPASS_MISMATCH_WITH_FORM")
            if not row["ONGC_GATEPASS_NO"] and f["pass"]:
                row["ONGC_GATEPASS_NO"] = f["pass"]
        else:
            flags.append("NOT_IN_REGISTRATION_FORM")
        # onboarding checklist
        pending = []
        for item in ITEMS:
            v = r["_items"].get(item)
            if item == "ONGC_GATEPASS":
                v = row["ONGC_GATEPASS_NO"] or v
            if v:
                status = "RECEIVED" if v.upper() == "RECEIVED" else "DONE"
                checks.append((as_of, r["ECODE"], item, status, None if v.upper() in ("RECEIVED", "DONE") else v, None))
            else:
                checks.append((as_of, r["ECODE"], item, "PENDING", None, None))
                pending.append(item)
        row["ONBOARDING_PENDING_ITEMS"] = ", ".join(pending) or None
        row["ONBOARDING_STATUS"] = "COMPLETE" if not pending else ("NOT_STARTED" if len(pending) == len(ITEMS) else "PARTIAL")
        if pending:
            flags.append("ONBOARDING_INCOMPLETE")
        row["REMARKS"] = "; ".join(remarks) or None
        row["DQ_FLAGS"] = "; ".join(flags) or None
        rows.append(row)
    df = pd.DataFrame(rows, columns=COLS).astype(object)
    df = df.where(df.notna(), None)
    unmatched = [f for f in form if f["row"] not in used]
    return df, pd.DataFrame(checks, columns=CHECK_COLS), unmatched


# ------------------------------------------------------------------ database
def db():
    import master_db
    return master_db, master_db.connect()


def db_setup(con):
    num = {"EXPERIENCE_YEARS": "NUMERIC(5,1)", "SOURCE_ROW": "INTEGER"}
    cols = ", ".join(f"{c} {'DATE' if c in DATE_COLS else num.get(c, 'TEXT')}" for c in COLS)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS cipl_employee ({cols}, IS_ON_ROSTER INTEGER, FIRST_SEEN_DATE DATE, LAST_SEEN_DATE DATE, PRIMARY KEY (ECODE));
        CREATE TABLE IF NOT EXISTS cipl_employee_snapshot ({cols}, PRIMARY KEY (SNAPSHOT_DATE, ECODE));
        CREATE TABLE IF NOT EXISTS cipl_onboarding (ECODE TEXT, ITEM TEXT, STATUS TEXT, ITEM_VALUE TEXT, STATUS_DATE DATE, SNAPSHOT_DATE DATE, PRIMARY KEY (ECODE, ITEM));
        CREATE TABLE IF NOT EXISTS cipl_event_log (EVENT_ID BIGSERIAL PRIMARY KEY, SNAPSHOT_DATE DATE, ECODE TEXT, EVENT_TYPE TEXT, FIELD TEXT,
            OLD_VALUE TEXT, NEW_VALUE TEXT, EVENT_DATE DATE, REASON TEXT, SOURCE TEXT);
        CREATE INDEX IF NOT EXISTS ix_cipl_event_ecode ON cipl_event_log (ECODE);
        DROP VIEW IF EXISTS v_cipl_active;
        CREATE VIEW v_cipl_active AS SELECT * FROM cipl_employee WHERE employment_status = 'ACTIVE';
        DROP VIEW IF EXISTS v_cipl_onboarding_pending;
        CREATE VIEW v_cipl_onboarding_pending AS
          SELECT e.ecode, e.employee_name, o.item FROM cipl_onboarding o JOIN cipl_employee e USING (ecode)
          WHERE o.status = 'PENDING' AND e.employment_status = 'ACTIVE' ORDER BY e.ecode, o.item;
        DROP VIEW IF EXISTS v_cipl_headcount;
        CREATE VIEW v_cipl_headcount AS
          SELECT designation, level, COUNT(*) AS headcount FROM cipl_employee WHERE employment_status = 'ACTIVE' GROUP BY designation, level;
        DROP VIEW IF EXISTS v_cipl_lifecycle;
        CREATE VIEW v_cipl_lifecycle AS
          SELECT ecode, event_type, event_date, snapshot_date, field, old_value, new_value, reason, source FROM cipl_event_log ORDER BY event_date, event_id;
    """)
    con.commit()


def db_state():
    """(current rows by ECODE, previous snapshot frame, snapshot date) or (None, None, None) when the DB is unreachable / empty."""
    try:
        m, con = db()
    except SystemExit:
        return None, None, None
    try:
        db_setup(con)
        cur = m.query_df(con, "SELECT * FROM cipl_employee")
        cur.columns = [c.upper() for c in cur.columns]
        snaps = m.query_df(con, "SELECT MAX(snapshot_date) d FROM cipl_employee_snapshot")["d"].iloc[0]
        prev = None
        if pd.notna(snaps):
            prev = m.query_df(con, "SELECT * FROM cipl_employee_snapshot WHERE snapshot_date = %s", (snaps,))
            prev.columns = [c.upper() for c in prev.columns]
        return cur, prev, (snaps if pd.notna(snaps) else None)
    finally:
        con.close()


def _cmp(v):
    if v is None or (not isinstance(v, (str, dt.date)) and pd.isna(v)):
        return None
    if isinstance(v, dt.date):
        return v.isoformat()
    if hasattr(v, "quantize"):
        return repr(float(v))
    return str(v)


def merge_and_events(df, chk, as_of, cur, prev, prev_date):
    """Overlay lifecycle from DB, carry leavers, compute AUTO events. Returns (df, events)."""
    events = []
    if cur is None or cur.empty:
        return df, events
    curi = cur.set_index("ECODE")
    roster = set(df["ECODE"])
    for i, r in df.iterrows():
        e = r["ECODE"]
        if e in curi.index and curi.at[e, "EMPLOYMENT_STATUS"] in EXIT_STATUSES:
            df.at[i, "EMPLOYMENT_STATUS"] = curi.at[e, "EMPLOYMENT_STATUS"]
            for c in ("RESIGNATION_DATE", "LAST_WORKING_DATE", "EXIT_REASON"):
                v = curi.at[e, c]
                df.at[i, c] = None if v is None or (not isinstance(v, str) and pd.isna(v)) else (v.date() if hasattr(v, "date") and not isinstance(v, dt.date) else v)
            df.at[i, "DQ_FLAGS"] = "; ".join(filter(None, [r["DQ_FLAGS"], "STILL_ON_ROSTER_AFTER_EXIT"]))
    carried = []
    for e in curi.index:
        if e in roster:
            continue
        row = {c: (None if (v := (e if c == "ECODE" else curi.at[e, c])) is None or (not isinstance(v, (str, dt.date)) and pd.isna(v)) else v) for c in COLS}
        for c in DATE_COLS:
            if row[c] is not None and hasattr(row[c], "date") and not isinstance(row[c], dt.date):
                row[c] = row[c].date()
        if row["EMPLOYMENT_STATUS"] == "ACTIVE":
            row["EMPLOYMENT_STATUS"] = "LEFT_ROSTER"
            if prev is not None and e in set(prev["ECODE"]):
                events.append((as_of, e, "LEFT_ROSTER", "EMPLOYMENT_STATUS", "ACTIVE", "LEFT_ROSTER", as_of, "Dropped off the roster; exit not confirmed", "AUTO"))
        carried.append(row)
    if carried:
        df = pd.concat([df, pd.DataFrame(carried, columns=COLS)], ignore_index=True)
    if prev is not None:
        pi = prev.set_index("ECODE")
        for _, r in df[df["ECODE"].isin(roster)].iterrows():
            e = r["ECODE"]
            if e not in pi.index:
                was_exit = e in curi.index and curi.at[e, "EMPLOYMENT_STATUS"] in EXIT_STATUSES | {"LEFT_ROSTER"}
                events.append((as_of, e, "REJOINED" if was_exit else "JOINED", None, None, None, r["DATE_OF_JOINING_ONGC"] or as_of, None, "AUTO"))
                continue
            for c in TRACKED:
                a, b = _cmp(pi.at[e, c]), _cmp(r[c])
                if a != b:
                    events.append((as_of, e, "DETAIL_CHANGED", c, a, b, as_of, None, "AUTO"))
    return df, events


def onboarding_events(chk, as_of):
    try:
        m, con = db()
    except SystemExit:
        return []
    try:
        old = m.query_df(con, "SELECT ecode, item, status FROM cipl_onboarding")
    finally:
        con.close()
    if old.empty:
        return []
    o = {(a, b): c for a, b, c in old.itertuples(index=False)}
    ev = []
    for _, r in chk.iterrows():
        k = (r["ECODE"], r["ITEM"])
        if k in o and o[k] != r["STATUS"]:
            ev.append((as_of, r["ECODE"], "ONBOARDING_ITEM", r["ITEM"], o[k], r["STATUS"], as_of, None, "AUTO"))
    return ev


def db_load(df, chk, events, as_of, force):
    m, con = db()
    try:
        db_setup(con)
        itam_locks.ensure_tables(con)
        locked = itam_locks.load_overrides(con, "engineers")[0]
        idx = df.index[df["ECODE"].isin(set(locked))]
        recs = [{k: (None if not isinstance(v, (str, list, dict)) and pd.isna(v) else v) for k, v in df.loc[i].to_dict().items()} for i in idx]
        itam_locks.apply_records(con, "engineers", recs, "ECODE", as_of)   # keep manual contact-detail edits made in the portal
        for i, rec in zip(idx, recs):
            for c, v in rec.items():
                df.at[i, c] = v
        cur = con.cursor()
        cur.execute("SELECT MAX(snapshot_date) FROM cipl_employee_snapshot")
        latest = cur.fetchone()[0]
        if latest and as_of < latest:
            sys.exit(f"Load blocked: snapshot {as_of} is older than already loaded {latest}.")
        roster = df[df["EMPLOYMENT_STATUS"] != "LEFT_ROSTER"]
        cur.execute("SELECT COUNT(*) FROM cipl_employee WHERE is_on_roster = 1")
        n_prev = cur.fetchone()[0]
        lost = int(cur.execute("SELECT COUNT(*) FROM cipl_employee WHERE is_on_roster = 1 AND ecode <> ALL(%s)", (list(roster["ECODE"]),)).fetchone()[0])
        if n_prev and lost > 0.3 * n_prev and not force:
            sys.exit(f"Load blocked: {lost} of {n_prev} roster employees are missing (>30%). Check the file or use --force.")
        allc = COLS + ["IS_ON_ROSTER", "FIRST_SEEN_DATE", "LAST_SEEN_DATE"]
        cur.execute("SELECT ecode, first_seen_date, last_seen_date FROM cipl_employee")
        seen = {a: (b, c) for a, b, c in cur.fetchall()}
        rows = []
        for r in df[COLS].itertuples(index=False):
            e = r[COLS.index("ECODE")]
            on = 0 if r[COLS.index("EMPLOYMENT_STATUS")] == "LEFT_ROSTER" else 1
            first = seen.get(e, (None, None))[0] or as_of
            last = as_of if on else (seen.get(e, (None, None))[1] or as_of)
            rows.append([None if (v is None or (not isinstance(v, (str, dt.date)) and pd.isna(v))) else v for v in r] + [on, first, last])
        upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in allc if c not in ("ECODE", "FIRST_SEEN_DATE"))
        cur.executemany(f"INSERT INTO cipl_employee ({','.join(allc)}) VALUES ({','.join(['%s'] * len(allc))}) ON CONFLICT (ecode) DO UPDATE SET {upd}", rows)
        cur.execute("DELETE FROM cipl_employee_snapshot WHERE snapshot_date = %s", (as_of,))
        srows = [r[:len(COLS)] for r in rows if r[len(COLS)] == 1]
        cur.executemany(f"INSERT INTO cipl_employee_snapshot ({','.join(COLS)}) VALUES ({','.join(['%s'] * len(COLS))})", srows)
        cur.executemany("""INSERT INTO cipl_onboarding (ecode,item,status,item_value,status_date,snapshot_date) VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (ecode,item) DO UPDATE SET status=EXCLUDED.status, item_value=EXCLUDED.item_value, snapshot_date=EXCLUDED.snapshot_date""",
                        [(r.ECODE, r.ITEM, r.STATUS, r.ITEM_VALUE, r.STATUS_DATE, as_of) for r in chk.itertuples(index=False)])
        cur.execute("DELETE FROM cipl_event_log WHERE snapshot_date = %s AND source = 'AUTO'", (as_of,))
        cur.executemany(f"INSERT INTO cipl_event_log ({','.join(EVENT_COLS)}) VALUES ({','.join(['%s'] * len(EVENT_COLS))})", events)
        con.commit()
    finally:
        con.close()
    kinds = collections.Counter(e[2] for e in events)
    print(f"Database load OK: snapshot {as_of}, {len(roster)} on roster, {len(df) - len(roster)} carried (left roster) -> {m.DB_LABEL}")
    print(f"  events: {dict(kinds) or 'none'}")


def manual_event(ecode, etype, date, reason, lwd):
    m, con = db()
    try:
        db_setup(con)
        cur = con.cursor()
        cur.execute("SELECT employment_status FROM cipl_employee WHERE ecode = %s", (ecode,))
        r = cur.fetchone()
        if not r:
            sys.exit(f"ECODE {ecode} is not in the database.")
        d = to_date(date)
        lw = to_date(lwd) if lwd else d
        if etype in EXIT_STATUSES:
            cur.execute("UPDATE cipl_employee SET employment_status=%s, resignation_date=%s, last_working_date=%s, exit_reason=%s WHERE ecode=%s",
                        (etype, d, lw, reason, ecode))
        elif etype == "REJOINED":
            cur.execute("UPDATE cipl_employee SET employment_status='ACTIVE', resignation_date=NULL, last_working_date=NULL, exit_reason=NULL WHERE ecode=%s", (ecode,))
        cur.execute("INSERT INTO cipl_event_log (snapshot_date,ecode,event_type,field,old_value,new_value,event_date,reason,source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'MANUAL')",
                    (dt.date.today(), ecode, etype, "EMPLOYMENT_STATUS", r[0], etype if etype in EXIT_STATUSES else "ACTIVE", d, reason))
        con.commit()
    finally:
        con.close()
    print(f"Recorded {etype} for {ecode} on {d} (last working day {lw}).")


def report():
    m, con = db()
    try:
        db_setup(con)
        for title, q in (("Employees by status", "SELECT employment_status, COUNT(*) n FROM cipl_employee GROUP BY 1 ORDER BY 1"),
                         ("Active headcount", "SELECT * FROM v_cipl_headcount ORDER BY 3 DESC"),
                         ("Onboarding items pending (active)", "SELECT item, COUNT(*) n FROM v_cipl_onboarding_pending GROUP BY item ORDER BY n DESC"),
                         ("Lifecycle events", "SELECT event_type, source, COUNT(*) n FROM cipl_event_log GROUP BY 1,2 ORDER BY 1")):
            print(f"\n{title}\n{m.query_df(con, q).to_string(index=False)}")
    finally:
        con.close()


# ------------------------------------------------------------------ workbooks
def write_master(ws, df, min_last=0):
    ws.title = "EMPLOYEE_MASTER"
    style_header(ws, COLS, [GROUP_FILL[m[1]] for m in MASTER])
    ws.row_dimensions[1].height = 32
    if df is not None:
        for r, rec in enumerate(df[COLS].itertuples(index=False), 2):
            for i, v in enumerate(rec, 1):
                if v is None or (not isinstance(v, (str, dt.date)) and pd.isna(v)):
                    continue
                ws.cell(row=r, column=i, value=v)
    for i, c in enumerate(COLS, 1):
        ws.column_dimensions[get_column_letter(i)].width = {"EMPLOYEE_NAME": 30, "DESIGNATION": 28, "COMPANY_EMAIL": 32, "PERSONAL_EMAIL": 28, "DQ_FLAGS": 40,
                                                            "ONBOARDING_PENDING_ITEMS": 34, "EDUCATION": 24, "CERTIFICATIONS": 30}.get(c, 16)
    for row in ws.iter_rows(min_row=2, max_row=max(ws.max_row, min_last)):
        for c in row:
            c.font = F_BASE
            n = COLS[c.column - 1]
            if n in DATE_COLS:
                c.number_format = DATE_FMT
            elif n in ("MOBILE_NO", "ONGC_GATEPASS_NO"):
                c.number_format = "@"
    last = max(ws.max_row + 200, min_last)
    for name, f in (("EMPLOYMENT_STATUS", '"' + ",".join(STATUSES) + '"'), ("ONBOARDING_STATUS", '"COMPLETE,PARTIAL,NOT_STARTED"'), ("GENDER", '"M,F"')):
        dv = DataValidation(type="list", formula1=f, allow_blank=True, errorStyle="warning")
        ws.add_data_validation(dv)
        col = get_column_letter(COLS.index(name) + 1)
        dv.add(f"{col}2:{col}{last}")
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLS))}{max(ws.max_row, 2)}"


def write_common_sheets(wb, chk, events, df=None, extra=None, template=False):
    ws = wb.create_sheet("ONBOARDING_CHECKLIST")
    style_header(ws, CHECK_COLS)
    if chk is not None:
        for rec in chk.itertuples(index=False):
            ws.append(list(rec))
    for i, w in enumerate((16, 12, 24, 14, 26, 16), 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2, max_row=max(ws.max_row, 500 if template else 2)):
        for c in row:
            c.font = F_BASE
            if c.column in (1, 6):
                c.number_format = DATE_FMT
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:F{max(ws.max_row, 2)}"
    ws = wb.create_sheet("EVENT_LOG")
    style_header(ws, EVENT_COLS)
    for e in events or []:
        ws.append(list(e))
    for i, w in enumerate((16, 12, 18, 24, 24, 24, 16, 40, 10), 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2, max_row=max(ws.max_row, 500 if template else 2)):
        for c in row:
            c.font = F_BASE
            if c.column in (1, 7):
                c.number_format = DATE_FMT
    dv = DataValidation(type="list", formula1='"JOINED,RESIGNED,TERMINATED,TRANSFERRED,REJOINED,LEFT_ROSTER,DETAIL_CHANGED,ONBOARDING_ITEM,NOTE"', allow_blank=True, errorStyle="warning")
    ws.add_data_validation(dv)
    dv.add("C2:C2000")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:I{max(ws.max_row, 2)}"


def write_dictionary(wb):
    rows = [(m[0], m[1], m[2]) for m in MASTER]
    simple_sheet(wb, "FIELD_DICTIONARY", ["COLUMN", "GROUP", "DESCRIPTION"], rows, [28, 14, 110])
    lk = wb.create_sheet("LOOKUPS")
    lists = {"EMPLOYMENT_STATUS": STATUSES, "ONBOARDING_STATUS": ["COMPLETE", "PARTIAL", "NOT_STARTED"], "CHECKLIST_ITEM": ITEMS,
             "CHECKLIST_STATUS": ["DONE", "RECEIVED", "PENDING"], "EVENT_TYPE": ["JOINED", "RESIGNED", "TERMINATED", "TRANSFERRED", "REJOINED", "LEFT_ROSTER", "DETAIL_CHANGED", "ONBOARDING_ITEM", "NOTE"],
             "LEVEL": ["SQ1", "SQ2", "SQ3", "Office Boy"]}
    style_header(lk, list(lists))
    for i, vals in enumerate(lists.values(), 1):
        for r, v in enumerate(vals, 2):
            lk.cell(row=r, column=i, value=v).font = F_BASE
        lk.column_dimensions[get_column_letter(i)].width = 24


def readme(ws, lines, title="CIPL Employee Master"):
    ws.title = "README"
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 112
    ws["A1"] = title
    ws["A1"].font = F_TITLE
    for i, (a, b) in enumerate(lines, 3):
        ws.cell(row=i, column=1, value=a).font = F_BOLD
        c = ws.cell(row=i, column=2, value=b)
        c.font, c.alignment = F_BASE, Alignment(wrap_text=True, vertical="top")
        ws.cell(row=i, column=1).alignment = Alignment(vertical="top")


TEMPLATE_LINES = [
    ("Purpose", "Standard structure for the CIPL contractor roster at ONGC Ankleshwar: who is on the team, their onboarding progress and every joining / exit event."),
    ("Sheets", "EMPLOYEE_MASTER (one row per ECODE, all employees ever, with status) | ONBOARDING_CHECKLIST (one row per employee per item) | EVENT_LOG (joins, exits, changes)."),
    ("Key", "ECODE. Upload = insert new ECODE, update existing."),
    ("Lifecycle", "EMPLOYMENT_STATUS: ACTIVE, RESIGNED, TERMINATED, TRANSFERRED, LEFT_ROSTER. Leavers are never deleted; they keep their row with dates and reason. LEFT_ROSTER = disappeared from the roster, exit not yet confirmed."),
    ("Recording an exit", "Add a row to EVENT_LOG (EVENT_TYPE RESIGNED / TERMINATED / TRANSFERRED, ECODE, EVENT_DATE, REASON) or tell Claude; it updates the status, RESIGNATION_DATE, LAST_WORKING_DATE and EXIT_REASON."),
    ("Not stored", "Aadhaar, UAN, bank account, IFSC and family / address details are deliberately excluded."),
    ("Formats", "Dates DD-MMM-YYYY. MOBILE_NO and ONGC_GATEPASS_NO are text. Blank = unknown (never '-' or 'NA')."),
    ("Header colours", "Dark blue = identity, blue = job, green = assignment, red = lifecycle, purple = onboarding, grey = trace."),
    ("Refresh", "Give the raw CIPL Employee Details workbook; Claude converts it with tools/cipl_roster.py and loads PostgreSQL (cipl_* tables), keeping history."),
]


def build_template(path):
    wb = Workbook()
    readme(wb.active, TEMPLATE_LINES, "CIPL Employee Master - Standard Bulk Upload Template")
    write_master(wb.create_sheet(), None, min_last=1000)
    write_common_sheets(wb, None, None, template=True)
    write_dictionary(wb)
    try:
        wb.save(path)
    except PermissionError:
        sys.exit(f"Cannot write {path} - it is open in Excel. Close it and run again.")


def template_headers():
    for f in sorted(TEMPLATE_DIR.glob("*.xlsx")):
        n = f.name.lower()
        if "cipl" in n and "master" in n and "template" in n and not n.startswith("~$"):
            wb = load_workbook(f, read_only=True)
            out = {s: [c for c in next(wb[s].iter_rows(min_row=1, max_row=1, values_only=True)) if c] for s in ("EMPLOYEE_MASTER", "ONBOARDING_CHECKLIST", "EVENT_LOG")}
            wb.close()
            return f.name, out
    return None, None


def sensitive_scan(df, chk):
    """Abort if anything that looks like an Aadhaar / bank number (11+ digit run) reached the output."""
    for name, frame in (("EMPLOYEE_MASTER", df), ("ONBOARDING_CHECKLIST", chk)):
        for c in frame.columns:
            for v in frame[c]:
                if isinstance(v, str) and re.search(r"\d{11,}", re.sub(r"[\s-]", "", v)):
                    sys.exit(f"STOPPED: {name}.{c} contains a long digit sequence (possible Aadhaar / bank number). Nothing written.")


def write_output(path, df, chk, events, orphans, unmatched, roster_sheet, form_sheet, new_cols, as_of):
    wb = Workbook()
    n_on = int((df["EMPLOYMENT_STATUS"] == "ACTIVE").sum())
    lines = [("Built from", f"Roster sheet '{roster_sheet}'" + (f" enriched from '{form_sheet}'" if form_sheet else "")),
             ("Snapshot", as_of.strftime("%d-%b-%Y")),
             ("Rows", f"{len(df)} employees in EMPLOYEE_MASTER: {n_on} ACTIVE, {int(df['EMPLOYMENT_STATUS'].isin(EXIT_STATUSES).sum())} exited, {int((df['EMPLOYMENT_STATUS'] == 'LEFT_ROSTER').sum())} left the roster (unconfirmed)."),
             ("Events this run", f"{len(events)} (see EVENT_LOG)."),
             ("Excluded on purpose", "Aadhaar, UAN, bank account, IFSC, family and address details were never read."),
             ("Check first", "RECONCILIATION (people in other sheets but not on the roster) and DATA_QUALITY.")]
    readme(wb.active, lines)
    write_master(wb.create_sheet(), df)
    write_common_sheets(wb, chk, events)
    dq = collections.Counter(f for s in df["DQ_FLAGS"].dropna() for f in s.split("; "))
    rows = [(k, v) for k, v in dq.most_common()]
    if new_cols:
        rows.append(("New roster columns not in mapping (review)", ", ".join(new_cols)))
    simple_sheet(wb, "DATA_QUALITY", ["FLAG / CHECK", "ROWS"], rows, [46, 60])
    rec = [("ORPHAN_ECODE", s, e, n, m, "In this sheet but not on the roster - possible leaver; confirm and record an exit event") for s, e, n, m in orphans]
    rec += [("FORM_WORKER_NOT_ON_ROSTER", form_sheet, None, u["name"], u["mobile"], "In the ONGC registration form but not matched to a roster employee") for u in unmatched]
    rec += [("ROSTER_EMPLOYEE_NOT_IN_FORM", roster_sheet, r["ECODE"], r["EMPLOYEE_NAME"], r["MOBILE_NO"], "On the roster but not in the registration form (no joining date available)")
            for _, r in df.iterrows() if r["DQ_FLAGS"] and "NOT_IN_REGISTRATION_FORM" in r["DQ_FLAGS"]]
    simple_sheet(wb, "RECONCILIATION", ["TYPE", "SOURCE SHEET", "ECODE", "NAME", "MOBILE", "ACTION"], rec, [30, 20, 12, 30, 14, 80])
    smap = [("Roster", "ECODE, ENAME, Mobile, DESIGNATION, EMAIL, PERSONAL EMAIL, LEVEL, EDUCATION QUALIFICATION, CERTIFICATION, EXPERIENCE, CLIENT, LOCATION, ONGC GATEPASS", "EMPLOYEE_MASTER"),
            ("Roster", "JOINING KIT, ID CARD, ONSURITY, MEDICAL/ESIC, POLICE VERIFICATION, SALARY ACCOUNT, JACKETS, ONGC GATEPASS", "ONBOARDING_CHECKLIST (blank = PENDING)"),
            ("Registration form", "Date of Birth, Gender, Initial date of deployment, Date of deployment (present contract), Termination date / reason, Skill category, Deployed at, Duty pattern, Duty pass no.", "EMPLOYEE_MASTER (matched by mobile, else name)")]
    smap += [("EXCLUDED (never read)", x, "-") for x in EXCLUDED_SENSITIVE]
    simple_sheet(wb, "SOURCE_MAPPING", ["SOURCE", "COLUMNS", "GOES TO"], smap, [24, 110, 46])
    write_dictionary(wb)
    try:
        wb.save(path)
    except PermissionError:
        sys.exit(f"Cannot write {path} - it is open in Excel. Close it and run again.")


def run(raw, as_of, out_dir, load, force):
    recs, roster_sheet, new_cols = read_roster(raw)
    form, form_sheet = read_form(raw)
    orphans_all = read_orphan_lists(raw, roster_sheet)
    roster_codes = {r["ECODE"] for r in recs}
    orphans, seen = [], set()
    for s, e, n, m in orphans_all:
        if e not in roster_codes and (s, e) not in seen:
            seen.add((s, e))
            orphans.append((s, e, n, m))
    df, chk, unmatched = build_frames(recs, form, as_of)
    if df["ECODE"].duplicated().any():
        sys.exit(f"Duplicate ECODE on the roster: {df.loc[df['ECODE'].duplicated(), 'ECODE'].tolist()}")
    cur, prev, prev_date = db_state()
    df, events = merge_and_events(df, chk, as_of, cur, prev, prev_date)
    events += onboarding_events(chk, as_of) if cur is not None else []
    tname, thdr = template_headers()
    if thdr:
        exp = {"EMPLOYEE_MASTER": COLS, "ONBOARDING_CHECKLIST": CHECK_COLS, "EVENT_LOG": EVENT_COLS}
        bad = [k for k in exp if thdr[k] != exp[k]]
        if bad:
            sys.exit(f"Output structure differs from template {tname} in {bad} - stopped.")
        print("Structure check OK against template:", tname)
    sensitive_scan(df, chk)
    path = Path(out_dir) / f"CIPL_Employee_Master_{as_of:%Y-%m-%d}.xlsx"
    write_output(path, df, chk, events, orphans, unmatched, roster_sheet, form_sheet, new_cols, as_of)
    print(f"Wrote {path}: {len(df)} employees ({int((df.EMPLOYMENT_STATUS == 'ACTIVE').sum())} active), {len(chk)} checklist rows, {len(events)} events")
    print(f"  reconciliation: {len(orphans)} orphan ECODEs, {len(unmatched)} form workers not on roster")
    if load:
        db_load(df, chk, events, as_of, force)
    return df


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", choices=["event", "report"])
    ap.add_argument("--raw")
    ap.add_argument("--as-of")
    ap.add_argument("--out-dir", default=str(Path(__file__).resolve().parent.parent / "masters"))
    ap.add_argument("--build-template", action="store_true")
    ap.add_argument("--no-load-db", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="rehearse the database load: everything runs and is reported, nothing is saved")
    ap.add_argument("--ecode")
    ap.add_argument("--type", choices=["RESIGNED", "TERMINATED", "TRANSFERRED", "REJOINED", "NOTE"])
    ap.add_argument("--date")
    ap.add_argument("--last-working-date")
    ap.add_argument("--reason")
    a = ap.parse_args()
    if a.dry_run:
        os.environ["ITAM_DRY_RUN"] = "1"
    if a.build_template:
        TEMPLATE_DIR.mkdir(exist_ok=True)
        build_template(TEMPLATE_DIR / TEMPLATE_NAME)
        print("Wrote", TEMPLATE_DIR / TEMPLATE_NAME)
    if a.cmd == "report":
        report()
    elif a.cmd == "event":
        if not (a.ecode and a.type and a.date):
            sys.exit("event needs --ecode, --type and --date")
        manual_event(a.ecode.upper(), a.type, a.date, a.reason, a.last_working_date)
    elif a.raw:
        run(a.raw, pd.to_datetime(a.as_of).date() if a.as_of else dt.date.today(), a.out_dir, a.dry_run or not a.no_load_db, a.force)
    elif not a.build_template:
        ap.print_help()
