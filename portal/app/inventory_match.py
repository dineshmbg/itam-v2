"""Inventory match: upload a report or inventory received from the centre (BigFix, antivirus, patch, EDR...) and match it against the
asset register by Asset (CI) = computer name. A CI found in the report means the tool is installed; a CI missing from it means not.

Nothing in the asset data is changed. The result (summary, findings, per-asset rows) is stored as a run so it can be reopened and
downloaded again - an Excel action pack for the team and a PDF summary for management.
"""
import csv
import datetime as dt
import io
import re
import uuid
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import db
from .importer import UPLOADS

MAX_BYTES = 40 * 1024 * 1024
STAGE = UPLOADS / "inventory-match"
DEFAULT_CLASSES = ["DESKTOP", "LAPTOP", "WORKSTATION", "SERVER"]
ALL_CLASSES = ["DESKTOP", "LAPTOP", "WORKSTATION", "SERVER", "PRINTER", "SCANNER", "SWITCH", "ROUTER", "UPS", "MEDIA_CONVERTER"]
HIGH, MEDIUM, LOW = "High", "Medium", "Low"
PRIORITY_BY_STATUS = {"IN_USE": HIGH, "NOT_ON_NETWORK": MEDIUM, "NOT_IN_USE": MEDIUM, "STANDBY": MEDIUM}      # anything else (store, surplus, transferred...) is Low

DDL = ["""CREATE TABLE IF NOT EXISTS portal_match_run (
          run_id TEXT PRIMARY KEY, at TIMESTAMPTZ NOT NULL DEFAULT now(), username TEXT NOT NULL, tool TEXT NOT NULL, filename TEXT NOT NULL,
          params JSONB NOT NULL, summary JSONB NOT NULL, result JSONB NOT NULL)"""]

# header (lower-case, letters and digits only) -> field it most likely holds
GUESS = {
    "name": ("computername", "hostname", "devicename", "machinename", "assetci", "cino", "ci", "assetname", "computer", "nodename", "endpointname", "name", "device", "asset"),
    "ip": ("ipaddress", "ip", "ipv4", "ipv4address", "lastipaddress"),
    "os": ("os", "operatingsystem", "osname", "osversion", "platform"),
    "seen": ("lastreporttime", "lastreport", "lastseen", "lastcheckin", "lastcontact", "lastcommunication", "lastupdate", "lastscan", "lastconnected", "lastactivity"),
    "type": ("devicetype", "type", "computertype", "category"),
    "model": ("cpu", "model", "processor", "hardware"),
}


class MatchError(Exception):
    http_error = True

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- reading the uploaded file
def _cell(v):
    if isinstance(v, (dt.datetime, dt.date)):
        return v
    return "" if v is None else str(v).strip()


def _table(rows):
    """Header row = the first of the first 20 rows that has at least 2 filled cells and as many as the widest of them (title rows above are skipped)."""
    rows = [list(r) for r in rows]
    head = 0
    for i, r in enumerate(rows[:20]):
        filled = sum(1 for c in r if str(c).strip())
        if filled >= 2 and filled >= 0.6 * max(sum(1 for c in x if str(c).strip()) for x in rows[:20]):
            head = i
            break
    headers = [str(c).strip() or f"Column {j + 1}" for j, c in enumerate(rows[head])]
    body = [r + [""] * (len(headers) - len(r)) for r in rows[head + 1:] if any(str(c).strip() for c in r)]
    return headers, body


def read_file(filename, data):
    """-> (headers, rows). Excel (.xlsx/.xlsm; the sheet with the most rows) or CSV."""
    low = filename.lower()
    if low.endswith((".xlsx", ".xlsm")):
        if data[:2] != b"PK":
            raise MatchError("That file is not an Excel workbook (it is damaged or a different format).")
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        best = []
        for ws in wb.worksheets:
            rows = [[_cell(c) for c in r] for r in ws.iter_rows(values_only=True)]
            if len(rows) > len(best):
                best = rows
        wb.close()
        if len(best) < 2:
            raise MatchError("The workbook has no rows to read.")
        return _table(best)
    if low.endswith((".csv", ".txt")):
        text = data.decode("utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect)]
        if len(rows) < 2:
            raise MatchError("The file has no rows to read.")
        return _table(rows)
    raise MatchError("Upload an Excel workbook (.xlsx) or a CSV file.")


def guess_mapping(headers):
    norm = [re.sub(r"[^a-z0-9]", "", h.lower()) for h in headers]
    out = {}
    for field, names in GUESS.items():
        for n in names:                                    # earlier names are the better match
            if n in norm:
                out[field] = headers[norm.index(n)]
                break
    return out


def stage(filename, data, user):
    name = re.sub(r"[^\w .()-]", "_", Path(filename or "report.xlsx").name)[:120]
    if not data or len(data) > MAX_BYTES:
        raise MatchError(f"The file is empty or larger than {MAX_BYTES // (1024 * 1024)} MB.")
    headers, rows = read_file(name, data)
    sid = uuid.uuid4().hex[:12]
    STAGE.mkdir(parents=True, exist_ok=True)
    (STAGE / f"{sid}__{name}").write_bytes(data)
    m = guess_mapping(headers)
    prefixes = {}
    if m.get("name"):
        i = headers.index(m["name"])
        for r in rows:
            p = re.match(r"[A-Za-z]{3,4}", str(r[i]).strip())
            if p:
                prefixes[p.group(0).upper()] = prefixes.get(p.group(0).upper(), 0) + 1
    return {"stage_id": sid, "filename": name, "rows": len(rows), "headers": headers, "mapping": m, "sample": [[str(c) for c in r] for r in rows[:6]],
            "prefixes": sorted(prefixes.items(), key=lambda kv: -kv[1])[:12]}


def _staged(stage_id):
    if not re.fullmatch(r"[0-9a-f]{12}", stage_id or ""):
        raise MatchError("Upload the file again.")
    hits = list(STAGE.glob(f"{stage_id}__*")) if STAGE.exists() else []
    if not hits:
        raise MatchError("That upload is no longer available. Upload the file again.")
    return hits[0].name.split("__", 1)[1], hits[0].read_bytes()


# ---------------------------------------------------------------- matching
def key_of(v):
    """A computer name as a match key: trimmed, upper-case, domain suffix dropped (host.domain.local -> HOST)."""
    s = str(v or "").strip().upper()
    return s.split(".")[0] if re.search(r"[A-Z]", s) else s


def _when(v):
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None) if not v.tzinfo else v.astimezone(dt.timezone.utc).replace(tzinfo=None)
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day)
    s = str(v or "").strip()
    if not s:
        return None
    for f in (lambda: parsedate_to_datetime(s), lambda: dt.datetime.fromisoformat(s.replace("Z", "+00:00")), lambda: dt.datetime.strptime(s[:10], "%d-%m-%Y"), lambda: dt.datetime.strptime(s[:10], "%d/%m/%Y")):
        try:
            d = f()
            return d.astimezone(dt.timezone.utc).replace(tzinfo=None) if d.tzinfo else d
        except Exception:      # noqa: BLE001 - try the next format
            continue
    return None


def _os_family(s):
    s = str(s or "").upper()
    for pat, label in ((r"WIN(DOWS)?\s*11", "Windows 11"), (r"WIN(DOWS)?\s*10", "Windows 10"), (r"WIN(DOWS)?\s*(8|7|XP|VISTA)", "Windows 8 or older"), (r"2012|2016|2019|2022|2025|SERVER", "Windows Server"), (r"LINUX|UBUNTU|RHEL|CENTOS|DEBIAN", "Linux"), (r"MAC", "macOS")):
        if re.search(pat, s):
            return label
    return "Other / unknown" if s else ""


def _assets(prefixes, classes):
    like = " OR ".join(["ci_no ILIKE %s"] * len(prefixes))
    return db.query(f"""SELECT ci_no, asset_class, asset_type, make, model, serial_no, hostname, ip_address, asset_status, user_name, location_code, engineer_name
                        FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND asset_class = ANY(%s) AND ({like}) ORDER BY ci_no""", [classes, *[p + "%" for p in prefixes]])


def _pct(a, b):
    return round(100 * a / b, 1) if b else 0.0


def analyse(headers, rows, mapping, params, today=None):
    today = today or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    tool = (params.get("tool") or "BigFix").strip()[:40] or "BigFix"
    prefixes = [p.strip().upper() for p in re.split(r"[,\s]+", params.get("prefix") or "ANK") if p.strip()] or ["ANK"]
    classes = [c for c in (params.get("classes") or DEFAULT_CLASSES) if c in ALL_CLASSES] or DEFAULT_CLASSES
    active_days, stale_days = int(params.get("active_days") or 7), int(params.get("stale_days") or 30)
    target = float(params.get("target") or 95)
    if not mapping.get("name") or mapping["name"] not in headers:
        raise MatchError("Choose the column that holds the computer name.")
    col = {f: headers.index(mapping[f]) for f in GUESS if mapping.get(f) in headers}

    # ---- the report: one entry per computer name (the most recent check-in wins), duplicates counted
    entries, dup_names, all_by_ip = {}, {}, {}
    for r in rows:
        k = key_of(r[col["name"]])
        if not k:
            continue
        e = {"name": str(r[col["name"]]).strip(), "key": k, "ip": str(r[col["ip"]]).strip() if "ip" in col else "", "os": str(r[col["os"]]).strip() if "os" in col else "",
             "type": str(r[col["type"]]).strip() if "type" in col else "", "seen": _when(r[col["seen"]]) if "seen" in col else None}
        if e["ip"]:
            all_by_ip.setdefault(e["ip"], e)
        if not any(k.startswith(p) for p in prefixes):
            continue
        old = entries.get(k)
        if old:
            dup_names[k] = dup_names.get(k, 1) + 1
            if (e["seen"] or dt.datetime.min) < (old["seen"] or dt.datetime.min):
                continue
        entries[k] = e
    in_scope_rows = len(entries)

    assets = _assets(prefixes, classes)
    ci_keys = {key_of(a["ci_no"]): a for a in assets}
    other = {key_of(r["ci_no"]): r for r in db.query("SELECT ci_no, asset_class, asset_type, asset_status FROM asset WHERE is_current = 1 AND (" + " OR ".join(["ci_no ILIKE %s"] * len(prefixes)) + ")",
                                                      [p + "%" for p in prefixes])}
    asset_by_ip = {a["ip_address"].strip(): a for a in assets if a.get("ip_address")}
    asset_by_host = {key_of(a["hostname"]): a for a in assets if a.get("hostname")}

    out = []
    for a in assets:
        k = key_of(a["ci_no"])
        e = entries.get(k)
        row = {"ci": a["ci_no"], "class": a["asset_class"], "type": a["asset_type"], "make": a["make"] or "", "model": a["model"] or "", "serial": a["serial_no"] or "", "status": a["asset_status"] or "",
               "user": a["user_name"] or "", "location": a["location_code"] or "", "engineer": a["engineer_name"] or "UNASSIGNED", "installed": e is not None,
               "r_name": "", "r_ip": "", "r_os": "", "r_os_family": "", "r_seen": None, "r_age": None, "reporting": "", "priority": "", "possible": "", "possible_by": "", "action": ""}
        if e:
            age = (today - e["seen"]).days if e["seen"] else None
            row.update(r_name=e["name"], r_ip=e["ip"], r_os=e["os"], r_os_family=_os_family(e["os"]), r_seen=e["seen"].strftime("%Y-%m-%d %H:%M") if e["seen"] else None, r_age=age,
                       reporting="" if age is None else "Active" if age <= active_days else "Stale" if age <= stale_days else "Dormant")
            if row["reporting"] == "Stale":
                row["action"] = f"Agent last reported {age} days ago - check the machine is on and connected."
            elif row["reporting"] == "Dormant":
                row["action"] = f"Agent silent for {age} days - locate the machine; repair the agent, or confirm it is retired and update the register."
        else:
            row["priority"] = PRIORITY_BY_STATUS.get(row["status"], LOW)
            hit = None
            if a.get("ip_address") and a["ip_address"].strip() in all_by_ip:
                hit, by = all_by_ip[a["ip_address"].strip()], "same IP address"
            elif a.get("hostname") and key_of(a["hostname"]) in entries:
                hit, by = entries[key_of(a["hostname"])], "same host name"
            if hit and key_of(hit["name"]) != k:
                row.update(possible=hit["name"], possible_by=by)
                row["action"] = f"Probably installed under a different name ({hit['name']}, {by}) - rename the computer to {a['ci_no']} or correct the CI, then re-check."
            elif row["priority"] == HIGH:
                row["action"] = f"Install the {tool} agent (or restore its connection) - the machine is in use."
            elif row["priority"] == MEDIUM:
                row["action"] = f"Install {tool} when the machine is back on the network / in use."
            else:
                row["action"] = f"Not urgent while {row['status'].replace('_', ' ').lower() or 'not in service'}; install {tool} before it is issued."
        out.append(row)

    matched_keys = set(ci_keys) & set(entries)
    unreg = []
    for k, e in sorted(entries.items()):
        if k in ci_keys:
            continue
        o = other.get(k)
        hit = asset_by_ip.get(e["ip"]) if e["ip"] else None
        if o:
            why, act = f"Registered, but as {o['asset_class'].title()} / {o['asset_type'].title()} (outside the classes checked)", "No action unless the asset class is wrong in the register."
        elif hit and key_of(hit["ci_no"]) not in entries:
            why, act = f"Not registered under this name - same IP as {hit['ci_no']}", f"Probably {hit['ci_no']}: rename the computer or correct the CI."
        else:
            why, act = "Not in the asset register", "Find the device and add it to the register, or confirm it is not ours / retired."
        unreg.append({"name": e["name"], "ip": e["ip"], "os": e["os"], "type": e["type"], "seen": e["seen"].strftime("%Y-%m-%d %H:%M") if e["seen"] else None,
                      "age": (today - e["seen"]).days if e["seen"] else None, "why": why, "action": act, "kind": "outside" if o else "mismatch" if hit and not o else "unregistered"})

    dups = [{"name": entries[k]["name"], "rows": n} for k, n in sorted(dup_names.items(), key=lambda kv: -kv[1])]

    # ---- the numbers
    total, inst = len(out), sum(1 for r in out if r["installed"])
    miss = [r for r in out if not r["installed"]]
    summary = {
        "tool": tool, "prefixes": prefixes, "classes": classes, "target": target, "active_days": active_days, "stale_days": stale_days,
        "report_rows": len(rows), "report_in_scope": in_scope_rows, "assets": total, "installed": inst, "missing": len(miss), "coverage": _pct(inst, total),
        "missing_high": sum(1 for r in miss if r["priority"] == HIGH), "missing_medium": sum(1 for r in miss if r["priority"] == MEDIUM), "missing_low": sum(1 for r in miss if r["priority"] == LOW),
        "active": sum(1 for r in out if r["reporting"] == "Active"), "stale": sum(1 for r in out if r["reporting"] == "Stale"), "dormant": sum(1 for r in out if r["reporting"] == "Dormant"),
        "unknown_age": sum(1 for r in out if r["installed"] and not r["reporting"]), "name_mismatch": sum(1 for r in miss if r["possible"]),
        "unregistered": sum(1 for u in unreg if u["kind"] != "outside"), "outside_scope": sum(1 for u in unreg if u["kind"] == "outside"), "duplicate_names": len(dups),
        "has_seen": "seen" in col, "has_os": "os" in col, "as_of": today.strftime("%Y-%m-%d"),
    }
    summary["in_use"] = sum(1 for r in out if r["status"] == "IN_USE")
    summary["in_use_installed"] = sum(1 for r in out if r["status"] == "IN_USE" and r["installed"])
    summary["in_use_coverage"] = _pct(summary["in_use_installed"], summary["in_use"])
    summary["rag"] = "green" if summary["coverage"] >= target else "amber" if summary["coverage"] >= target - 15 else "red"

    def by(field, label_fn=lambda v: v or "(none)"):
        g = {}
        for r in out:
            x = g.setdefault(label_fn(r[field]), {"label": label_fn(r[field]), "total": 0, "installed": 0, "missing": 0, "high": 0, "dormant": 0})
            x["total"] += 1
            x["installed"] += r["installed"]
            x["missing"] += not r["installed"]
            x["high"] += (not r["installed"]) and r["priority"] == HIGH
            x["dormant"] += r["reporting"] == "Dormant"
        for x in g.values():
            x["coverage"] = _pct(x["installed"], x["total"])
        return sorted(g.values(), key=lambda x: (-x["missing"], x["label"]))

    summary["by_class"], summary["by_status"] = by("class"), by("status", lambda v: (v or "(none)").replace("_", " "))
    summary["by_location"], summary["by_engineer"] = by("location"), by("engineer")
    mm = {}
    for r in miss:
        k = (r["make"] + " " + r["model"]).strip() or "(model not recorded)"
        mm[k] = mm.get(k, 0) + 1
    summary["gap_models"] = [{"label": k, "n": n} for k, n in sorted(mm.items(), key=lambda kv: -kv[1])[:10]]
    fam = {}
    for r in out:
        if r["installed"] and r["r_os_family"]:
            fam[r["r_os_family"]] = fam.get(r["r_os_family"], 0) + 1
    summary["os_mix"] = [{"label": k, "n": n} for k, n in sorted(fam.items(), key=lambda kv: -kv[1])]
    summary["findings"] = findings(summary)
    return summary, {"assets": out, "unregistered": unreg, "duplicates": dups}


def findings(s):
    """Plain-language findings, most important first, each with the action behind it."""
    f, t = [], s["tool"]
    sev = {"green": "Good", "amber": "Needs attention", "red": "Critical"}[s["rag"]]
    f.append({"severity": sev, "finding": f"{t} is installed on {s['installed']:,} of {s['assets']:,} checked machines ({s['coverage']}%); the target is {s['target']:g}%.",
              "action": "Close the gap below, starting with machines in use." if s["missing"] else "Keep the register and the report in step."})
    if s["in_use"]:
        f.append({"severity": "Critical" if s["in_use_coverage"] < s["target"] - 15 else "Needs attention" if s["in_use_coverage"] < s["target"] else "Good",
                  "finding": f"Of the {s['in_use']:,} machines deployed to users, {s['in_use_installed']:,} have {t} ({s['in_use_coverage']}%). {s['missing_high']:,} deployed machines have no agent.",
                  "action": "Engineers to install or repair the agent on these first - they are the real exposure."})
    worst = [x for x in s["by_class"] if x["missing"]][:2]
    if worst and s["missing"]:
        f.append({"severity": "Needs attention", "finding": "Largest gaps by class: " + "; ".join(f"{x['label'].title()} {x['missing']:,} of {x['total']:,} missing ({x['coverage']}% covered)" for x in worst) + ".",
                  "action": "Check whether this class is outside the centre's deployment scope (e.g. laptops not on the domain) or a roll-out has not reached it."})
    eng = [x for x in s["by_engineer"] if x["missing"]][:3]
    if eng and len(s["by_engineer"]) > 1:
        f.append({"severity": "Needs attention", "finding": "Most machines without the agent sit with: " + "; ".join(f"{x['label'].title()} ({x['missing']:,})" for x in eng) + ".",
                  "action": "Share each engineer's list (Excel, 'By engineer' sheet) and agree a completion date."})
    if s["name_mismatch"]:
        f.append({"severity": "Needs attention", "finding": f"{s['name_mismatch']:,} machines counted as 'not installed' appear in the report under a different name (matched by IP address or host name).",
                  "action": "Rename the computer to its CI, or correct the CI in the register. Fixing the name raises coverage without installing anything."})
    if s["has_seen"] and (s["dormant"] or s["stale"]):
        f.append({"severity": "Needs attention", "finding": f"Of the machines that have the agent, {s['active']:,} reported within {s['active_days']} days, {s['stale']:,} within {s['stale_days']} days and {s['dormant']:,} not for over {s['stale_days']} days.",
                  "action": "Dormant machines are 'installed' on paper but unmanaged - locate them, repair the agent, or retire them from the register."})
    if s["unregistered"]:
        f.append({"severity": "Needs attention", "finding": f"{s['unregistered']:,} devices report to {t} with names that are not in the asset register.",
                  "action": "Identify each one and add it to the register (or confirm it is not ours). Unregistered devices are outside asset control."})
    if s["has_os"]:
        w10 = next((x["n"] for x in s["os_mix"] if x["label"] == "Windows 10"), 0)
        old = next((x["n"] for x in s["os_mix"] if x["label"] == "Windows 8 or older"), 0)
        if w10 or old:
            f.append({"severity": "Needs attention", "finding": f"{w10:,} reporting machines run Windows 10" + (f" and {old:,} run Windows 8 or older" if old else "") + ".",
                      "action": "Plan upgrades to a supported Windows release; see the OS column in the full list."})
    if s["duplicate_names"]:
        f.append({"severity": "Information", "finding": f"{s['duplicate_names']:,} computer names appear more than once in the report (re-imaged or re-registered machines).",
                  "action": "Ask the centre to clean up the stale duplicate records; the latest check-in was used here."})
    if s["missing_low"]:
        f.append({"severity": "Information", "finding": f"{s['missing_low']:,} of the missing machines are in store, surplus or transferred, so no agent is expected yet.",
                  "action": "No urgent action; install before they are issued to a user."})
    return f


# ---------------------------------------------------------------- run, store, reopen
def run(stage_id, mapping, params, user):
    fname, data = _staged(stage_id)
    headers, rows = read_file(fname, data)
    summary, result = analyse(headers, rows, mapping or {}, params or {})
    run_id = uuid.uuid4().hex[:12]
    with db.write() as con:
        import json
        con.execute("INSERT INTO portal_match_run (run_id, username, tool, filename, params, summary, result) VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)",
                    (run_id, user, summary["tool"], fname, json.dumps(params or {}), json.dumps(summary, default=str), json.dumps(result, default=str)))
    return {"run_id": run_id, "filename": fname, "summary": summary, **result}


def get(run_id):
    r = db.one("SELECT * FROM portal_match_run WHERE run_id = %s", [run_id])
    if not r:
        raise MatchError("That analysis no longer exists.", 404)
    return {"run_id": r["run_id"], "at": r["at"], "username": r["username"], "filename": r["filename"], "summary": r["summary"], **r["result"]}


def history(limit=15):
    return db.query("SELECT run_id, at, username, tool, filename, summary->>'coverage' AS coverage, summary->>'assets' AS assets, summary->>'installed' AS installed FROM portal_match_run ORDER BY at DESC LIMIT %s", [limit])


# ---------------------------------------------------------------- files
def _t(title, columns, rows):
    return {"title": title, "columns": columns, "rows": rows}


ASSET_COLS = [("ci", "Asset (CI)"), ("class", "Class"), ("type", "Type"), ("make", "Make"), ("model", "Model"), ("serial", "Serial no"), ("status", "Asset status"), ("user", "User"), ("location", "Location"),
              ("engineer", "Engineer"), ("priority", "Priority"), ("action", "Action"), ("possible", "Seen in report as"), ("possible_by", "Matched by")]
INSTALLED_COLS = [("ci", "Asset (CI)"), ("class", "Class"), ("make", "Make"), ("model", "Model"), ("status", "Asset status"), ("user", "User"), ("location", "Location"), ("engineer", "Engineer"),
                  ("r_ip", "Report IP"), ("r_os", "Report OS"), ("r_seen", "Last report"), ("r_age", "Days silent"), ("reporting", "Reporting"), ("action", "Action")]
GROUP_COLS = [("label", "{}"), ("total", "Machines"), ("installed", "Installed"), ("missing", "Not installed"), ("high", "Not installed, in use"), ("coverage", "Coverage %"), ("dormant", "Agent silent")]


def _group(title, label, rows):
    return _t(title, [(k, label if k == "label" else v) for k, v in GROUP_COLS], rows)


def _yn(rows):
    return [{**r, "installed": "Yes" if r["installed"] else "No"} for r in rows]


def tables(d, management=False):
    s, a = d["summary"], d["assets"]
    miss = sorted((r for r in a if not r["installed"]), key=lambda r: ({HIGH: 0, MEDIUM: 1, LOW: 2}[r["priority"]], r["engineer"], r["ci"]))
    quiet = sorted((r for r in a if r["reporting"] in ("Stale", "Dormant")), key=lambda r: -(r["r_age"] or 0))
    t = [_t("Key findings", [("severity", "Rating"), ("finding", "Finding"), ("action", "What to do")], s["findings"]),
         _group("Coverage by class", "Class", s["by_class"]), _group("Coverage by engineer", "Engineer", s["by_engineer"])]
    if management:                  # the PDF is the short version: findings, class, engineer and the worst machines; the Excel pack has everything
        t.append(_t(f"Top {min(25, len(miss))} machines to fix first (deployed, no agent)", [("ci", "Asset (CI)"), ("class", "Class"), ("model", "Model"), ("user", "User"), ("location", "Location"), ("engineer", "Engineer")],
                    [r for r in miss if r["priority"] == HIGH][:25]))
        return t
    t += [_group("Coverage by location", "Location", s["by_location"]), _group("Coverage by asset status", "Asset status", s["by_status"]),
          _t("Models with most gaps", [("label", "Make / model"), ("n", "Not installed")], s["gap_models"]),
          _t("Not installed - action list", ASSET_COLS, miss),
          _t("Installed but not reporting", INSTALLED_COLS, quiet),
          _t("Name differs from register", [("name", "Name in report"), ("ip", "IP"), ("why", "Finding"), ("action", "Action")], [u for u in d["unregistered"] if u["kind"] == "mismatch"]),
          _t("In report, not in register", [("name", "Name in report"), ("ip", "IP"), ("os", "OS"), ("seen", "Last report"), ("age", "Days silent"), ("why", "Finding"), ("action", "Action")], [u for u in d["unregistered"] if u["kind"] != "mismatch"]),
          _t("Duplicate names in report", [("name", "Computer name"), ("rows", "Rows in report")], d["duplicates"]),
          _t("Full match", [("ci", "Asset (CI)"), ("installed", "Installed")] + INSTALLED_COLS[1:13] + [("priority", "Priority")], _yn(a))]
    return t


def kpis(s):
    t = s["tool"]
    k = [("Machines checked", s["assets"]), (f"{t} installed", s["installed"]), ("Not installed", s["missing"]), ("Coverage (%)", s["coverage"]), ("Target (%)", s["target"]),
         ("Deployed, no agent", s["missing_high"]), ("Deployed machines covered (%)", s["in_use_coverage"]), ("Installed, silent over %d days" % s["stale_days"], s["dormant"]),
         ("Not in register", s["unregistered"]), ("Name differs from register", s["name_mismatch"])]
    return k


def render(d, fmt, user):
    from . import export
    s = d["summary"]
    ch = [{"title": "Coverage by class (%)", "rows": [{"label": x["label"].title(), "n": x["coverage"]} for x in s["by_class"]]},
          {"title": "Machines without the agent, by engineer", "rows": [{"label": x["label"].title(), "n": x["missing"]} for x in s["by_engineer"] if x["missing"]][:12]},
          {"title": "Machines without the agent, by location", "rows": [{"label": x["label"], "n": x["missing"]} for x in s["by_location"] if x["missing"]][:12]}]
    if s["has_os"] and s["os_mix"]:
        ch.append({"title": "Operating systems on installed machines", "rows": s["os_mix"]})
    title = f"{s['tool']} coverage - {', '.join(s['prefixes'])}"
    sub = f"Report file: {d['filename']} - {s['assets']:,} register machines checked - {dt.date.today():%d %b %Y}"
    return export.render(fmt, title, sub, tables(d, management=fmt == "pdf"), kpis(s), ch, footer=user["username"])
