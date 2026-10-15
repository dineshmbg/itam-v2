"""Software / OS observations per asset: what a report, the check script or an engineer saw on a machine, kept as an append-only history.
Sources never overwrite each other (conflicts are shown side by side for the engineer to judge). The inspection date is stamped by the database
at insert - no caller can supply it - and a trigger refuses UPDATE and DELETE, so history cannot be rewritten."""
import datetime as dt
import re

from . import db

SOURCES = ("BIGFIX_REPORT", "TREND_REPORT", "SCRIPT", "MANUAL")
STALE_DAYS, VERY_STALE_DAYS = 30, 90
FIELDS = ("os_edition", "os_build", "os_feature_version", "trend_installed", "trend_version", "trend_status", "bigfix_installed", "bigfix_version",
          "vpn_name", "vpn_version", "vpn_other", "sap_gui_version", "o365_account", "o365_version", "last_user", "connection",
          "bitlocker", "tpm_secureboot", "office_channel", "domain_joined", "last_boot", "browser_version", "local_admins", "remarks")
BOOLS = {"trend_installed", "bigfix_installed", "vpn_other", "bitlocker", "tpm_secureboot", "domain_joined"}

DDL = [
    f"""CREATE TABLE IF NOT EXISTS portal_sw_obs (
          obs_id BIGSERIAL PRIMARY KEY, asset_key TEXT NOT NULL, source TEXT NOT NULL,
          inspected_at TIMESTAMPTZ NOT NULL DEFAULT now(), data_as_of TIMESTAMPTZ, recorded_by TEXT NOT NULL,
          {', '.join(f + (' BOOLEAN' if f in BOOLS else ' TEXT') for f in FIELDS)})""",
    "CREATE INDEX IF NOT EXISTS ix_sw_obs_asset ON portal_sw_obs (asset_key, source, inspected_at DESC)",
    """CREATE OR REPLACE FUNCTION portal_sw_obs_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN RAISE EXCEPTION 'portal_sw_obs is append-only'; END $$""",
    "DROP TRIGGER IF EXISTS portal_sw_obs_no_change ON portal_sw_obs",
    "CREATE TRIGGER portal_sw_obs_no_change BEFORE UPDATE OR DELETE ON portal_sw_obs FOR EACH ROW EXECUTE FUNCTION portal_sw_obs_append_only()",
]


class ObsError(Exception):
    http_error = True

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def record(asset_key, source, facts, by, data_as_of=None):
    """Add one observation and return its id. `inspected_at` is deliberately not a parameter: the database stamps it."""
    if source not in SOURCES:
        raise ObsError(f"Unknown source {source!r}.")
    bad = sorted(set(facts) - set(FIELDS))
    if bad:
        raise ObsError(f"Unknown field(s): {', '.join(bad)}.")
    cols = list(facts)
    with db.write() as con:
        row = con.execute(f"INSERT INTO portal_sw_obs (asset_key, source, recorded_by, data_as_of{''.join(', ' + c for c in cols)}) VALUES (%s,%s,%s,%s{', %s' * len(cols)}) RETURNING obs_id",
                          [asset_key, source, by, data_as_of] + [facts[c] for c in cols]).fetchone()
    return row[0] if not isinstance(row, dict) else row["obs_id"]


def _stale(r):
    age = (dt.datetime.now(dt.timezone.utc) - (r["data_as_of"] or r["inspected_at"])).days
    return "VERY_STALE" if age > VERY_STALE_DAYS else "STALE" if age > STALE_DAYS else None


def latest_by_source(asset_key):
    """The newest observation from each source, side by side; `stale` is STALE / VERY_STALE by the report's own date when it has one."""
    rows = db.query("SELECT DISTINCT ON (source) * FROM portal_sw_obs WHERE asset_key = %s ORDER BY source, obs_id DESC", [asset_key])
    return [{**r, "stale": _stale(r)} for r in rows]


# ---------------------------------------------------------------- reports (BigFix, Trend Micro) -> observations
SCOPE_CLASSES = ["DESKTOP", "WORKSTATION", "SERVER"]
CI_PREFIX = "ANK"      # every CI of this site starts with ANK; anything else in a report belongs to another site and is discarded
_BIGFIX_OS = re.compile(r"^\s*Win(?P<ed>\d+)\s+(?P<build>\d+\.\d+\.\d+\.\d+)(?:\s*\((?P<feat>[^)]+)\))?")


def _edition(code):
    return f"Windows {code}" if code in ("10", "11") else f"Windows Server {code}"


def _bigfix(g):
    m = _BIGFIX_OS.match(g("os"))
    if not m:
        return {}
    return {"os_edition": _edition(m["ed"]), "os_build": m["build"], "os_feature_version": m["feat"]}


def _trend(g):
    build = re.search(r"Build\s+(\d+)", g("osver"))
    return {"trend_installed": True, "trend_status": g("agent") or None, "connection": g("conn") or None, "last_user": g("user") or None,
            "os_edition": g("os") or None, "os_build": build[1] if build else None, "trend_version": g("unified") or None}


def ingest_report(filename, data, by):
    """Read a BigFix or Trend Micro export (recognised from its columns, like the inventory match does) and record one observation for every row
    whose computer name is one of our desktop/workstation/server assets; names not starting with ANK are discarded unseen. Rows that match nothing are counted and named, not stored. A row whose
    (asset, source, report time) is already recorded is `unchanged`: re-importing a file adds nothing."""
    from . import inventory_match as im
    try:
        headers, rows = im.read_file(filename, data)
    except im.MatchError as e:
        raise ObsError(str(e)) from e
    prof = im.profile_file(headers, rows)
    product = prof["product"]
    if product not in ("bigfix", "trendmicro") or "name" not in prof["mapping"]:
        raise ObsError("That file is not a recognised BigFix or Trend Micro export.")
    have = {h.strip().lower(): i for i, h in enumerate(headers)}
    idx = {f: headers.index(h) for f, h in prof["mapping"].items()}
    idx.update({f: have[h.lower()] for f, h in im.PRODUCTS[product]["extra"].items() if h.lower() in have})
    if "unified agent version" in have:
        idx["unified"] = have["unified agent version"]
    g_row = lambda r: (lambda f: ("" if f not in idx or isinstance(r[idx[f]], (dt.datetime, dt.date)) else str(r[idx[f]]).strip()))      # noqa: E731
    source, build = ("BIGFIX_REPORT", _bigfix) if product == "bigfix" else ("TREND_REPORT", _trend)
    ours = {im.key_of(a["ci_no"]): a["asset_key"] for a in db.query("SELECT asset_key, ci_no FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND asset_class = ANY(%s)", [SCOPE_CLASSES])}
    latest, ignored = {}, 0
    for r in rows:
        key = im.key_of(r[idx["name"]])
        when = im._seen(r[idx["seen"]], product) if "seen" in idx else None
        if not key.startswith(CI_PREFIX):
            ignored += 1                                 # another site's machine: not ours, not even reported as unmatched
            continue
        if key and (key not in latest or (when or dt.datetime.min) >= (latest[key][1] or dt.datetime.min)):
            latest[key] = (r, when)                      # a machine listed twice: the newest record wins
    known = {(r["asset_key"], r["data_as_of"]) for r in db.query("SELECT asset_key, data_as_of FROM portal_sw_obs WHERE source = %s", [source])}
    out = {"product": product, "rows": len(rows), "matched": 0, "unmatched": 0, "unmatched_names": [], "recorded": 0, "unchanged": 0, "ignored_other_sites": 0}
    for key, (r, when) in latest.items():
        ak = ours.get(key)
        if not ak:
            out["unmatched"] += 1
            out["unmatched_names"].append(str(r[idx["name"]]).strip())
            continue
        out["matched"] += 1
        as_of = when.replace(tzinfo=dt.timezone.utc) if when else None
        if (ak, as_of) in known:
            out["unchanged"] += 1
            continue
        record(ak, source, {k: v for k, v in build(g_row(r)).items() if v is not None}, by, data_as_of=as_of)
        out["recorded"] += 1
    out["ignored_other_sites"] = ignored
    out["unmatched_names"] = out["unmatched_names"][:50]
    return out


# ---------------------------------------------------------------- the asset record's "Software" tab
def _build_tail(v):
    """BigFix says 10.0.19045.6456, Trend only 19045: compare on the Windows build number (third part) so the two can be set side by side."""
    parts = str(v or "").split(".")
    return parts[2] if len(parts) >= 3 else (parts[0] if parts and parts[0].isdigit() else None)


def panel(asset_key, asset_class):
    """What the record window shows: the newest observation per source (with its flags), conflicts between sources for the engineer to judge,
    and the full history. None for classes outside the tracked scope."""
    if asset_class not in SCOPE_CLASSES:
        return None
    latest = latest_by_source(asset_key)
    for r in latest:
        r["flags"] = [f for f in (r["stale"] and ("Report is more than %d days old" % (VERY_STALE_DAYS if r["stale"] == "VERY_STALE" else STALE_DAYS)),
                                  r["os_edition"] == "Windows 10" and "Windows 10 is past end of support") if f]
    conflicts = []
    for label, get in (("Windows edition", lambda r: r["os_edition"]), ("Windows build", lambda r: _build_tail(r["os_build"]))):
        seen = {r["source"]: get(r) for r in latest if get(r)}
        if len(set(seen.values())) > 1:
            conflicts.append({"field": label, "values": seen})
    history = db.query("SELECT * FROM portal_sw_obs WHERE asset_key = %s ORDER BY obs_id DESC LIMIT 100", [asset_key])
    return {"latest": latest, "conflicts": conflicts, "history": history}


# ---------------------------------------------------------------- inventory match: report versus what was seen on the machine
SOURCE_LABELS = {"BIGFIX_REPORT": "BigFix report", "TREND_REPORT": "Trend Micro report", "SCRIPT": "Script", "MANUAL": "Entered by hand"}
_PRODUCT_FLAG = {"trendmicro": ("trend_installed", "Trend Micro"), "bigfix": ("bigfix_installed", "BigFix")}


def cross_check(rows, product):
    """Add `observed` / `xcheck` to every inventory-match row whose machine has a Script or hand-entered observation about this product, and say where
    it contradicts the report. Rows with no such observation are left exactly as they were. Returns how many rows contradict."""
    if product not in _PRODUCT_FLAG or not rows:
        return 0
    flag, label = _PRODUCT_FLAG[product]
    obs = db.query(f"""SELECT DISTINCT ON (asset_key, source) asset_key, source, inspected_at, {flag} AS state FROM portal_sw_obs
                       WHERE asset_key = ANY(%s) AND source IN ('SCRIPT', 'MANUAL') AND {flag} IS NOT NULL ORDER BY asset_key, source, obs_id DESC""", [[r["ci"] for r in rows]])
    by = {}
    for o in obs:
        by.setdefault(o["asset_key"], []).append(o)
    n = 0
    for r in rows:
        mine = by.get(r["ci"])
        if not mine:
            continue
        r["observed"] = "; ".join(f"{SOURCE_LABELS[o['source']]} {o['inspected_at']:%d %b %Y}: {label} {'installed' if o['state'] else 'not installed'}" for o in mine)
        bad = [o for o in mine if bool(o["state"]) != bool(r["installed"])]
        if bad:
            o = bad[0]
            r["xcheck"] = (f"The report lists this machine, but {SOURCE_LABELS[o['source']]} ({o['inspected_at']:%d %b %Y}) found {label} not installed." if r["installed"]
                           else f"The report does not list this machine, but {SOURCE_LABELS[o['source']]} ({o['inspected_at']:%d %b %Y}) found {label} installed.")
            n += 1
    return n


# ---------------------------------------------------------------- the check script (a .bat: reads the PC, installs nothing)
import hmac      # noqa: E402
import secrets   # noqa: E402
from pathlib import Path   # noqa: E402

MAX_CHECK_BYTES = 20_000
_TEMPLATE = Path(__file__).with_name("ankcheck.bat.tmpl")
DEFAULT_DROP = r"\10.205.64.203\ank-helpdesk\common-for-all\check-results"
_SERVER_NAME = re.compile(r"Windows Server\s+(\d{4}(?:\s+R2)?)", re.I)


def parse_check(text):
    """The script's KEY=VALUE text -> {computer, facts}. Strict: it must start as one of ours, name a computer, and stay small."""
    if len(text.encode("utf-8", "replace")) > MAX_CHECK_BYTES:
        raise ObsError("That is not a check-script result (too large).")
    kv = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep and re.fullmatch(r"[A-Z0-9_]{2,24}", k.strip()):
            kv[k.strip()] = v.strip()[:200]
    if kv.get("ANKCHECK") != "1":
        raise ObsError("That is not a check-script result.")
    computer = kv.get("COMPUTER", "").upper()
    if not re.fullmatch(r"[A-Z0-9._-]{1,63}", computer):
        raise ObsError("The result does not name a computer.")
    g = kv.get
    facts = {"trend_installed": bool(g("TREND_NAME")), "bigfix_installed": bool(g("BIGFIX_NAME")) or g("BIGFIX_SVC") == "1"}
    for fld, key in (("trend_version", "TREND_VER"), ("bigfix_version", "BIGFIX_VER"), ("sap_gui_version", "SAP_VER"), ("o365_account", "O365_ACCOUNT"), ("o365_version", "OFFICE_VER"),
                     ("office_channel", "OFFICE_CHANNEL"), ("last_user", "LAST_USER"), ("browser_version", "EDGE_VER")):
        if g(key):
            facts[fld] = g(key)
    if g("TREND_NAME"):
        facts["trend_status"] = g("TREND_NAME")
    build = g("WIN_BUILD", "")
    if build.isdigit():
        server = g("WIN_INSTALLTYPE", "").lower() == "server" or "server" in g("WIN_PRODUCT", "").lower()
        m = _SERVER_NAME.search(g("WIN_PRODUCT", ""))
        facts["os_edition"] = (f"Windows Server {m[1]}" if m else "Windows Server") if server else ("Windows 11" if int(build) >= 22000 else "Windows 10")   # ProductName still says 10 on 11
        facts["os_build"] = f"10.0.{build}" + (f".{g('WIN_UBR')}" if g("WIN_UBR", "").isdigit() else "")
        if g("WIN_DISPLAY"):
            facts["os_feature_version"] = g("WIN_DISPLAY")
    other = g("VPN_OTHER", "").strip(" ;")
    if g("ZSCALER_NAME"):
        facts["vpn_name"], facts["vpn_version"] = "Zscaler", g("ZSCALER_VER") or None
    else:
        facts["vpn_name"] = f"{other.split(';')[0].strip()} (not Zscaler)" if other else "None found"
    facts["vpn_other"] = bool(other)
    if g("DOMAIN"):
        facts["domain_joined"] = True
    if g("SECUREBOOT") in ("0", "1"):
        facts["tpm_secureboot"] = g("SECUREBOOT") == "1"
    notes = ([f"Other VPN: {other}"] if other else []) + ([f"ran {g('RUN_AT_RAW')} (PC clock)"] if g("RUN_AT_RAW") else [])
    if notes:
        facts["remarks"] = "; ".join(notes)
    return {"computer": computer, "facts": {k: v for k, v in facts.items() if v is not None}}


def ingest_check(text, by):
    """Record one check-script result against the matching desktop/workstation/server asset (name starts with ANK)."""
    c = parse_check(text)
    key = c["computer"].split(".")[0]
    asset = db.one("SELECT asset_key FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND asset_class = ANY(%s) AND upper(ci_no) = %s", [SCOPE_CLASSES, key]) if key.startswith(CI_PREFIX) else None
    if not asset:
        return {"computer": c["computer"], "matched": False, "recorded": 0}
    record(asset["asset_key"], "SCRIPT", c["facts"], by)
    return {"computer": c["computer"], "matched": True, "recorded": 1}


def check_token(create=False, by="system", rotate=False):
    """The shared token baked into the script. Stored in portal_setting; it only lets a script add observations for our own machines."""
    row = db.one("SELECT value FROM portal_setting WHERE key = 'sw_check_token'")
    if row and not rotate:
        return row["value"]
    if not (create or rotate):
        return None
    tok = secrets.token_urlsafe(24)
    with db.write() as con:
        con.execute("INSERT INTO portal_setting (key, value, updated_by) VALUES ('sw_check_token', to_jsonb(%s::text), %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by", (tok, by))
    return tok


def token_ok(given):
    real = check_token()
    return bool(real) and bool(given) and hmac.compare_digest(str(real), str(given))


def script_text(portal_url, token, drop=DEFAULT_DROP):
    """The .bat, ready to put on the share: this portal's address and the token are filled in, Windows line endings."""
    body = _TEMPLATE.read_text(encoding="utf-8").replace("__URL__", portal_url.rstrip("/")).replace("__TOKEN__", token).replace("__DROP__", drop)
    return body.replace("\r\n", "\n").replace("\n", "\r\n")



# ---------------------------------------------------------------- manual entry and the Excel bulk template
TEMPLATE_COLUMNS = [("Windows edition", "os_edition"), ("Windows build", "os_build"), ("Windows feature version", "os_feature_version"), ("Trend Micro installed (Yes/No)", "trend_installed"),
                    ("Trend Micro version", "trend_version"), ("BigFix installed (Yes/No)", "bigfix_installed"), ("BigFix version", "bigfix_version"), ("VPN", "vpn_name"), ("VPN version", "vpn_version"),
                    ("Other VPN present (Yes/No)", "vpn_other"), ("SAP GUI version", "sap_gui_version"), ("Microsoft 365 account", "o365_account"), ("Office version", "o365_version"),
                    ("Office update channel", "office_channel"), ("Last logged-on user", "last_user"), ("BitLocker on (Yes/No)", "bitlocker"), ("TPM / Secure Boot on (Yes/No)", "tpm_secureboot"),
                    ("Domain joined (Yes/No)", "domain_joined"), ("Last boot", "last_boot"), ("Browser version", "browser_version"), ("Local administrators", "local_admins"), ("Remarks", "remarks")]
_YES, _NO = {"yes", "y", "true", "1"}, {"no", "n", "false", "0"}


def template_headers():
    return ["Asset (CI)"] + [h for h, _ in TEMPLATE_COLUMNS]


def _clean_facts(raw):
    """Typed values for one manual observation: blanks are dropped, Yes/No become booleans, nothing unknown or oversized gets through."""
    facts = {}
    for k, v in (raw or {}).items():
        if k not in FIELDS:
            raise ObsError(f"Unknown field: {k}.")
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        if k in BOOLS:
            s = str(v).strip().lower()
            if s not in _YES | _NO:
                raise ObsError(f"{k}: answer Yes or No.")
            facts[k] = s in _YES
        else:
            s = str(v).strip()
            if len(s) > 200:
                raise ObsError(f"{k}: too long (200 characters at most).")
            facts[k] = s
    if not facts:
        raise ObsError("Enter at least one value.")
    return facts


def _in_scope_asset(key):
    return db.one("SELECT asset_key, engineer_name FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND asset_class = ANY(%s) AND upper(asset_key) = upper(%s)", [SCOPE_CLASSES, str(key or "").strip()])


def record_manual(asset_key, raw, by):
    facts = _clean_facts(raw)
    asset = _in_scope_asset(asset_key)
    if not asset:
        raise ObsError("Software is tracked for desktops, workstations and servers only.", 404)
    return record(asset["asset_key"], "MANUAL", facts, by)


def template_bytes():
    import io
    from openpyxl import Workbook
    from openpyxl.worksheet.datavalidation import DataValidation
    wb = Workbook()
    ws = wb.active
    ws.title = "Software"
    ws.append(template_headers())
    dv = DataValidation(type="list", formula1='"Yes,No"', allow_blank=True)
    ws.add_data_validation(dv)
    for i, (h, f) in enumerate(TEMPLATE_COLUMNS, start=2):
        letter = ws.cell(1, i).column_letter
        ws.column_dimensions[letter].width = 22
        if f in BOOLS:
            dv.add(f"{letter}2:{letter}1000")
    ws.column_dimensions["A"].width = 20
    ws.freeze_panes = "B2"
    n = wb.create_sheet("How to fill")
    for line in ("One row per machine. Asset (CI) must be a desktop, workstation or server in the register.", "Leave a cell blank when you did not check it - blank is not the same as No.",
                 "Yes/No columns accept Yes or No only.", "Every row is saved as an observation 'Entered by hand' with the date and your name; the date cannot be set or changed.",
                 "If any row has a problem, nothing from the file is saved: fix the rows listed and upload again."):
        n.append([line])
    n.column_dimensions["A"].width = 110
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def ingest_workbook(data, by, engineer_key=None):
    """Read a filled template. All or nothing: observations cannot be deleted, so one bad row means nothing is saved.
    `engineer_key`: an engineer may only fill in the machines assigned to them (administrators and team leads pass None)."""
    import io
    from openpyxl import load_workbook
    if data[:2] != b"PK":
        raise ObsError("That is not an Excel workbook (.xlsx).")
    try:
        ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
    except Exception as e:      # noqa: BLE001
        raise ObsError("That Excel file could not be read.") from e
    if not rows or str(rows[0][0] or "").strip() != "Asset (CI)":
        raise ObsError("This is not the Software upload template (the first column must be 'Asset (CI)'). Download the template again.")
    field_of = {h: f for h, f in TEMPLATE_COLUMNS}
    cols = {i: field_of[str(c).strip()] for i, c in enumerate(rows[0]) if str(c or "").strip() in field_of}
    todo, errors = [], []
    for n, r in enumerate(rows[1:], start=2):
        ci = str(r[0] or "").strip()
        if not ci and not any(str(c or "").strip() for c in r):
            continue
        try:
            asset = _in_scope_asset(ci)
            if not asset:
                raise ObsError(f"{ci or '(blank)'} is not a desktop, workstation or server in the register.")
            if engineer_key and asset["engineer_name"] != engineer_key:
                raise ObsError(f"{ci} is not assigned to you.")
            todo.append((asset["asset_key"], _clean_facts({f: r[i] for i, f in cols.items() if i < len(r)})))
        except ObsError as e:
            errors.append(f"Row {n}: {e}")
    if errors or not todo:
        return {"recorded": 0, "errors": errors or ["The file has no rows to save."]}
    for key, facts in todo:
        record(key, "MANUAL", facts, by)
    return {"recorded": len(todo), "errors": []}
