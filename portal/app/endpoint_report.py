"""Detailed endpoint report: the endpoint list of an analysed centre report (Vision One console, Apex One list, old Apex inventory ...), compared machine by machine with what the
software inventory (the ankcheck.bat PC check) found on the PC itself, with the Trend Micro state of each machine in its own column (Sensor only / Full suite).

It is built live from the newest analyses and PC checks, so it is always current. Nothing is guessed: a machine nothing says anything about is "Not determined" and listed under
"Check these PCs". Before it is released every conclusion is recomputed by a second, simpler route (`selfcheck`); one disagreement marks the report "do not circulate" and it is not e-mailed.

Made automatically with every analysis of an endpoint report (see trend_investigation.on_run, which attaches it to the investigation e-mail), downloadable by everyone: an engineer gets
the machines assigned to them, an administrator or team lead gets all of them.
"""
import collections
import datetime as dt
import re

from . import db, export, swobs
from . import inventory_match as im
from . import trend_investigation as ti

ENDPOINT_RE = re.compile(r"apex|trend|officescan|worry|vision|endpoint|security agent", re.I)
C = collections.Counter
CMP = ["Confirmed on the PC", "CONTRADICTED by the PC", "Supported by a centre report (not checked on the PC)", "Not verifiable - listed only"]
SERVICE_TXT = re.compile(r";\s*(?:processes|services seen)")


class EndpointReportError(Exception):
    http_error = True

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def is_endpoint_run(run):
    """An analysis of an endpoint report: Vision One, Apex One, Trend Micro, the old inventory, anything named endpoint. BigFix is not one."""
    s = run["summary"] or {}
    if s.get("product") == "bigfix":
        return False
    names = [run.get("tool") or "", run.get("filename") or ""] + [f.get("name", "") for f in s.get("files", [])]
    return s.get("product") in ("trendmicro", "apex") or bool(ENDPOINT_RE.search(" ".join(names)))


def latest_run(published_only=False):
    for r in db.query("SELECT run_id, tool, filename, summary, published_at FROM portal_match_run ORDER BY at DESC LIMIT 60"):
        if is_endpoint_run(r) and (r["published_at"] or not published_only):
            return r["run_id"]
    return None


# ---------------------------------------------------------------- evidence
def _day(v):
    return str(v)[:10] if v else ""


def _pc(p):
    """What the PC check saw on the machine itself."""
    if not p or p.get("trend_installed") is None:
        return {"checked": "Not checked", "date": _day(p and p.get("inspected_at")), "agent": "Not checked", "product": "", "version": "", "services": "", "sensor": "", "suite": "", "os": ""}
    t = p.get("trend_services") or ""
    low = t.lower()
    sensor = ("Yes" if ("endpointbasecamp.exe" in low or "tmbmsrv" in low) else "No") if "processes" in t else "Not recorded (older script)"
    installed = bool(p["trend_installed"])
    suite = swobs.suite_from_services(t)
    if installed and not suite and "security agent" in (p.get("trend_status") or "").lower():
        suite = "Full suite"
    return {"checked": "Yes", "date": _day(p["inspected_at"]), "agent": "Yes" if installed else "No", "product": p.get("trend_status") or "", "version": p.get("trend_version") or "",
            "services": SERVICE_TXT.split(t)[0], "sensor": sensor, "suite": suite if installed else "", "os": " ".join(x for x in (p.get("os_edition"), p.get("os_build")) if x)}


def _console_text(v):
    return f"Anti-malware: {v['t_am'] or '-'}; EDR sensor: {v['t_edr'] or '-'}; sensor connectivity: {v.get('t_sensor_conn') or '-'}"


def _view(v):
    """What is switched ON in the Vision One console (not what is installed)."""
    am_on, edr_on = (v["t_am"] or "").lower() != "disabled", (v["t_edr"] or "").lower() in ("enabled", "enabling")
    return {(True, True): "Both (anti-malware + EDR sensor)", (True, False): "Anti-malware only (EDR sensor off)", (False, True): "EDR sensor only (anti-malware DISABLED)",
            (False, False): "Neither (all protection DISABLED)"}[(am_on, edr_on)]


def gather(run_id=None):
    run_id = run_id or latest_run()
    if not run_id:
        raise EndpointReportError("No endpoint report has been analysed yet. Run Inventory match with the centre's endpoint report first.", 404)
    primary = im.get(run_id)
    if not is_endpoint_run({"summary": primary["summary"], "tool": primary["summary"].get("tool"), "filename": primary["filename"]}):
        raise EndpointReportError("That analysis is not an endpoint report.", 400)
    vision, lists, bigfix = ti._pick()
    others = [im.get(r["run_id"]) for r in ([vision] if vision else []) + lists if r["run_id"] != run_id]
    prefixes = primary["summary"].get("prefixes") or ["ANK"]
    classes = primary["summary"].get("classes") or im.DEFAULT_CLASSES
    register = im._assets(prefixes, classes)
    arows = db.query("SELECT asset_key, ci_no, cpf_no FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND ci_no = ANY(%s)", [[a["ci_no"] for a in register]])
    ak = {r["ci_no"]: r["asset_key"] for r in arows}
    cpf = {r["ci_no"]: r["cpf_no"] for r in arows}
    keys = list(ak.values())
    pc = {o["asset_key"]: o for o in db.query("SELECT DISTINCT ON (asset_key) * FROM portal_sw_obs WHERE source = 'SCRIPT' AND asset_key = ANY(%s) ORDER BY asset_key, obs_id DESC", [keys])}
    bf = {o["asset_key"]: o for o in db.query("SELECT DISTINCT ON (asset_key) asset_key, data_as_of FROM portal_sw_obs WHERE source = 'BIGFIX_REPORT' AND asset_key = ANY(%s) ORDER BY asset_key, obs_id DESC", [keys])}
    other_sec = collections.defaultdict(list)
    for a in db.query("""WITH l AS (SELECT DISTINCT ON (asset_key) asset_key, COALESCE(apps_obs_id, obs_id) holder FROM portal_sw_obs WHERE source = 'SCRIPT' AND apps_hash IS NOT NULL AND asset_key = ANY(%s)
                                    ORDER BY asset_key, obs_id DESC)
                        SELECT l.asset_key, a.name, a.version FROM l JOIN portal_sw_obs_app a ON a.obs_id = l.holder
                        WHERE a.name ~* 'antivirus|anti-virus|endpoint security|zenworks|mcafee|symantec|kaspersky|sophos|eset|crowdstrike|sentinel|bitdefender|quick heal|cylance' AND a.name !~* 'trend|apex one|officescan'""", [keys]):
        other_sec[a["asset_key"]].append(f"{a['name']} {a['version'] or ''}".strip())
    by = lambda run: {im.key_of(a["ci"]): a for a in run["assets"]}      # noqa: E731
    return {"primary": primary, "pby": by(primary), "others": [(r, by(r)) for r in others], "register": register, "ak": ak, "cpf": cpf, "pc": pc, "bf": bf, "sec": other_sec,
            "bigfix": im.get(bigfix["run_id"]) if bigfix else None}


# ---------------------------------------------------------------- one row per machine
def _decide(pcf, console, serverlist, listed):
    """(Sensor only / Full suite, basis, confidence). The PC itself first, then a console's own columns, then an Apex One server list. Never guessed."""
    if pcf["agent"] == "Yes" and pcf["suite"]:
        return pcf["suite"], "PC check (" + (pcf["services"] or pcf["product"]) + ")", "High"
    if pcf["agent"] == "No":
        return "Not installed", "PC check: no Trend Micro found on the PC", "High"
    if console is not None and "t_am" in console:
        cs = im._suite(console["t_am"], console["t_edr"])
        if cs in ("Full suite", "Sensor only"):
            return cs, "Vision One console: " + _console_text(console).split("; sensor")[0], "Medium"
    if serverlist:
        return "Full suite", f"Apex One server list: Security Agent registered ({serverlist})", "Medium"
    return "Not determined", "Listed only (no agent evidence in the report)" if listed else "In no centre report and not checked on the PC", "-"


def machines(g):
    primary, pby = g["primary"], g["pby"]
    out = []
    for a in g["register"]:
        k = im.key_of(a["ci_no"])
        pr = pby.get(k)
        listed = bool(pr and pr["installed"])
        rows = [(r, by.get(k)) for r, by in g["others"]]
        rows = [(r, x) for r, x in rows if x and x["installed"]]
        console = next((x for r, x in [(primary, pr if listed else None)] + rows if x and "t_am" in x), None)
        serverlist = next((x["r_conn"] for r, x in [(primary, pr if listed else None)] + rows if x and x.get("r_conn")), "")
        akey = g["ak"].get(a["ci_no"])
        pcf = _pc(g["pc"].get(akey))
        final, basis, conf = _decide(pcf, console, serverlist, listed or bool(rows))
        view = _view(console) if console else ""
        sec = "; ".join(sorted(set(g["sec"].get(akey, []))))
        notes = []
        if pcf["agent"] == "No" and (listed or rows or serverlist):
            notes.append("Listed by the centre but no Trend Micro found on the PC")
        if pcf["agent"] == "No" and sec:
            notes.append("Not protected by Trend Micro, but another security product is installed: " + sec)
        if pcf["agent"] == "Yes" and view.startswith(("EDR sensor only", "Neither")):
            notes.append("PC runs the full agent but the console shows anti-malware DISABLED: central team to confirm the policy")
        elif console and view.startswith(("EDR sensor only", "Neither")):
            notes.append("Console shows anti-malware DISABLED")
        if pcf["agent"] == "Yes" and not (listed or rows or serverlist):
            notes.append("Agent on the PC but in none of the centre lists we hold")
        if serverlist.lower() == "offline":
            notes.append("The Apex One server showed this agent Offline")
        if console and (console.get("t_conn") or "").lower() == "not communicating":
            notes.append("Console: agent not communicating")
        if listed and (pr["r_name"] or "").endswith("$"):
            notes.append("Name in the file ends with $ (computer account); matched to the register by the name without it")
        if pcf["agent"] == "Yes":
            cmp_ = CMP[0]
        elif pcf["agent"] == "No":
            cmp_ = CMP[1]
        elif serverlist or console is not None:
            cmp_ = CMP[2]
        else:
            cmp_ = CMP[3]
        status = (a["asset_status"] or "").replace("_", " ").capitalize()
        todo = {CMP[0]: "Nothing.", CMP[1]: "Verify on the PC: install the agent or correct the list.",
                CMP[2]: "The agent was Offline in the Apex One server list: confirm it reconnects, then run ankcheck.bat." if serverlist.lower() == "offline" else "Run ankcheck.bat to confirm on the PC.",
                CMP[3]: "Run ankcheck.bat on this PC." if status == "In use" else f"Asset status is '{status}': confirm where the machine is, then run ankcheck.bat if it is switched on."}[cmp_]
        if cmp_ == CMP[0] and view.startswith(("EDR sensor only", "Neither")):
            todo = "Central team: confirm whether anti-malware is switched off by policy."
        b = g["bf"].get(akey)
        out.append({"ci": a["ci_no"], "class": (a["asset_class"] or "").title(), "make": a["make"] or "", "model": a["model"] or "", "serial": a["serial_no"] or "", "user": (a["user_name"] or "").title(), "cpf": g["cpf"].get(a["ci_no"]) or "",
                    "location": a["location_code"] or "", "engineer": (a["engineer_name"] or "UNASSIGNED"), "status": status, "listed": listed, "file_name": pr["r_name"] if listed else "",
                    "file_type": (pr.get("r_type") or "") if listed else "", "file_os": pr["r_os"] if listed else "", "file_mgr": (pr.get("r_mgr") or "") if listed else "",
                    "lists": ", ".join(ti._label(r) for r, _ in rows), "serverlist": ("Yes - " + serverlist) if serverlist else "No", "console": _console_text(console) if console else "", "view": view,
                    "console_conn": (console.get("t_conn") or "") if console else "", "bf": _day(b["data_as_of"]) if b else "", "pc_checked": pcf["checked"], "pc_date": pcf["date"], "pc_agent": pcf["agent"],
                    "pc_product": pcf["product"], "pc_version": pcf["version"], "pc_services": pcf["services"], "pc_sensor": pcf["sensor"], "pc_os": pcf["os"], "other_sec": sec, "final": final, "basis": basis,
                    "conf": conf, "cmp": cmp_, "todo": todo, "notes": "; ".join(notes), "_suite": pcf["suite"], "_cs": im._suite(console["t_am"], console["t_edr"]) if console and "t_am" in console else "",
                    "_server": bool(serverlist), "_console": console is not None})
    return out


# ---------------------------------------------------------------- the second route: every conclusion recomputed from the database and the stored analysis rows
def selfcheck(g, ms):
    """Recompute the comparison of every listed machine a different way (a direct query of the newest PC check, and the stored analysis rows) and compare. -> (checked, [differences])."""
    keys = [g["ak"][m["ci"]] for m in ms if m["ci"] in g["ak"]]
    pcy = {r["asset_key"]: r["trend_installed"] for r in db.query("SELECT DISTINCT ON (asset_key) asset_key, trend_installed FROM portal_sw_obs WHERE source = 'SCRIPT' AND asset_key = ANY(%s) ORDER BY asset_key, obs_id DESC", [keys])}
    bad = []
    for m in ms:
        t = pcy.get(g["ak"].get(m["ci"]))
        want = (CMP[0], "Full suite") if t is True else (CMP[1], "Not installed") if t is False else None
        if want is None:
            if m["_server"] or m["_console"]:
                want = (CMP[2], None)
            else:
                want = (CMP[3], "Not determined")
        got = (m["cmp"], m["final"] if want[1] else None)
        if t is True and m["final"] in ("Sensor only",):
            want = (CMP[0], "Sensor only")                    # the PC saw only the sensor: the component rule, not an error
        if got != want:
            bad.append(f"{m['ci']}: report says {m['cmp']} / {m['final']}, recomputed {want[0]} / {want[1]}")
    return len(ms), bad


# ---------------------------------------------------------------- the whole report as data
def build(run_id=None):
    """The whole report as data (every machine). Use `scoped` for one engineer's machines."""
    g = gather(run_id)
    allm = machines(g)
    listed = [m for m in allm if m["listed"]]
    checked, bad = selfcheck(g, listed)
    return {"g": g, "all": allm, "listed": listed, "machines": listed, "scope": None, "made": dt.datetime.now(), "checked": checked, "bad": bad, "verified": not bad}


def scoped(d, engineer_key):
    """The same report for the machines assigned to one engineer (None when there are none). An administrator / team lead passes no engineer and gets everything."""
    if not engineer_key:
        return d
    ms = [m for m in d["listed"] if m["engineer"] == engineer_key]
    return {**d, "machines": ms, "scope": engineer_key} if ms else None


def build_safe(run_id):
    """For the automatic e-mail: the report, or None when it cannot be made (it must never stop the investigation mail)."""
    try:
        return build(run_id)
    except (EndpointReportError, ti.InvError):
        return None


def file(d, engineer_key, by):
    """(file name, bytes, mime) for one person, or None when they have no machine in the report."""
    d2 = scoped(d, engineer_key)
    if d2 is None:
        return None
    data, mime, _ = render(d2, by)
    return filename(d2), data, mime


def _pct(a, b):
    return f"{100.0 * a / b:.1f}%" if b else "-"


# ---------------------------------------------------------------- tables
MAIN_COLS = [("file_name", "Endpoint name (in the analysed file)"), ("file_type", "Object type (file)"), ("file_os", "Operating system (file)"), ("file_mgr", "Protection manager (file)"),
             ("ci", "Asset (CI) in register"), ("class", "Class"), ("make", "Make"), ("model", "Model"), ("serial", "Serial no"), ("user", "User"), ("cpf", "CPF no"), ("location", "Location"),
             ("engineer", "Engineer"), ("status", "Asset status"), ("final", "SENSOR ONLY / FULL SUITE"), ("basis", "Based on"), ("conf", "Confidence"),
             ("cmp", "Endpoint report compared with software inventory"), ("todo", "What to do"), ("notes", "Notes for the central team"), ("view", "Console view: protection working"),
             ("serverlist", "In an Apex One server list"), ("lists", "Also in these centre reports"), ("bf", "BigFix last report"), ("pc_checked", "PC checked (software inventory)"), ("pc_date", "PC check date"),
             ("pc_agent", "Trend Micro on the PC"), ("pc_product", "Product on the PC"), ("pc_version", "Agent version on the PC"), ("pc_services", "Trend services on the PC"),
             ("pc_sensor", "Endpoint sensor (Basecamp) running on the PC"), ("other_sec", "Other security software on the PC"), ("pc_os", "Windows on the PC")]
ALL_COLS = [("ci", "Asset (CI)"), ("class", "Class"), ("status", "Asset status"), ("user", "User"), ("location", "Location"), ("engineer", "Engineer"), ("model", "Model"), ("final", "SENSOR ONLY / FULL SUITE"),
            ("basis", "Based on"), ("conf", "Confidence"), ("view", "Console view: protection working"), ("listed", "In the analysed endpoint report"), ("serverlist", "In an Apex One server list"),
            ("console", "Vision One console says"), ("console_conn", "Console connection"), ("pc_checked", "PC checked"), ("pc_date", "PC check date"), ("pc_agent", "Trend Micro on the PC"),
            ("pc_product", "Product on the PC"), ("pc_version", "Agent version on the PC"), ("pc_services", "Trend services on the PC"), ("notes", "Notes for the central team")]
T = lambda ttl, cols, rws: {"title": ttl, "columns": cols, "rows": rws}              # noqa: E731


def _verify_rows(ms, allm):
    rows = []
    for m in ms:
        if m["pc_agent"] == "No" and m["listed"]:
            rows.append({"issue": "Listed by the centre, but no Trend Micro on the PC", "m": m, "detail": f"{m['cmp']}. PC checked {m['pc_date']}." + (f" Other security software: {m['other_sec']}." if m["other_sec"] else ""),
                         "todo": "Check the PC; reinstall or correct the list."})
    for m in allm:
        if m["view"].startswith(("EDR sensor only", "Neither")):
            pcs = f"PC check {m['pc_date']}: {m['pc_product']} {m['pc_version']}, {m['pc_services']}" if m["pc_agent"] == "Yes" else "Not checked on the PC"
            rows.append({"issue": "Console shows anti-malware switched OFF (" + m["view"].split(" (")[0] + ")", "m": m, "detail": f"Console: {m['console']}. {pcs}.",
                         "todo": "Central team: confirm whether anti-malware is switched off by policy; the PC runs the full agent." if m["pc_agent"] == "Yes" else "Central team: confirm; run ankcheck.bat on the PC."})
        elif m["pc_agent"] == "Yes" and not (m["listed"] or m["serverlist"] != "No" or m["_console"] or m["lists"]):
            rows.append({"issue": "Agent on the PC but in none of the centre lists we hold", "m": m, "detail": f"{m['pc_product']} {m['pc_version']}, PC checked {m['pc_date']}.",
                         "todo": "Central team: confirm which console manages it."})
    return sorted(({"issue": r["issue"], "ci": r["m"]["ci"], "engineer": r["m"]["engineer"].title(), "location": r["m"]["location"], "status": r["m"]["status"], "detail": r["detail"], "todo": r["todo"]} for r in rows),
                  key=lambda x: (x["issue"], x["ci"]))


def tables(d, full=True):
    g, ms = d["g"], d["machines"]
    prim = g["primary"]
    ps = prim["summary"]
    fname = "; ".join(f["name"] for f in ps.get("files", [])) or prim["filename"]
    show = [{**m, "engineer": m["engineer"].title(), "listed": "Yes" if m["listed"] else "No"} for m in ms]
    order = {k: i for i, k in enumerate(CMP)}
    show.sort(key=lambda m: (order[m["cmp"]], m["engineer"], m["ci"]))
    n = len(ms)
    cnt = C(m["cmp"] for m in ms)
    determined = cnt[CMP[0]] + cnt[CMP[1]] + cnt[CMP[2]]
    need85 = max(0, -(-85 * n // 100) - determined)
    both = [m for m in d["all"] if m["pc_agent"] == "Yes" and m["_suite"] and m["_cs"]]
    cross = C((m["_suite"], m["_cs"]) for m in both)
    agree = sum(v for (a, b), v in cross.items() if a == b)
    am_on = sum(1 for m in both if not m["view"].startswith(("EDR sensor only", "Neither")))
    cons = [m for m in d["all"] if m["_console"] and m["pc_agent"] in ("Yes", "No")]
    srv = [m for m in d["all"] if m["_server"] and m["pc_agent"] in ("Yes", "No")]
    own = [m for m in d["listed"] if m["pc_agent"] in ("Yes", "No")]
    pcdates = [m["pc_date"] for m in ms if m["pc_date"]]
    verify = _verify_rows(ms, [m for m in d["all"] if m["engineer"] == d["scope"]] if d["scope"] else d["all"])
    check = sorted((m for m in show if m["cmp"] == CMP[3]), key=lambda m: (m["status"] != "In use", m["location"], m["ci"]))
    about = [("Purpose", "Endpoint report for the central team to verify: every endpoint of the analysed report, compared with what the software inventory (the ankcheck.bat PC check) found on the machines, with the Trend Micro state of each machine (Sensor only / Full suite) in its own column."),
             ("The analysed report", f"Inventory match analysis of {fname} ({ps.get('tool', '')}), report date {ps.get('report_as_of') or ps.get('as_of', '')}. {n} endpoints matched to the register" + ("" if d["scope"] else f", {len(prim['unregistered'])} names are not in the register") +
              ". A listing in a directory-style report (Active Directory / inventory without a connection status) does NOT by itself prove an agent is installed: the PC check is what proves it."),
             ("Software inventory", f"The ankcheck.bat PC check: Trend Micro product, version and the Trend services running on the PC itself. PC checks exist for {sum(1 for m in ms if m['pc_checked'] == 'Yes')} of the {n} machines."),
             ("Other centre reports used only as supporting evidence", "; ".join(ti._label(r) for r, _ in g["others"]) or "none analysed"),
             ("Full suite", "The anti-malware agent is installed: the real-time scan service (ntrtscan) is on the PC, or the console shows an anti-malware component, or an Apex One server list shows the Security Agent. Anti-malware may still be switched off by policy: see 'Console view'."),
             ("Sensor only", "What is INSTALLED: only the Vision One endpoint sensor (EndpointBasecamp), without the anti-malware agent. Shown only where the PC or the console says so."),
             ("Console view", "What is SWITCHED ON in the Vision One console ('EDR sensor only' = anti-malware disabled while the EDR sensor is on). This is a different question from what is installed; those machines are on sheet 'Verify first' for the central team to confirm."),
             ("Not determined", "Nothing says what is installed (no PC check, no other report). It is NOT guessed. Run ankcheck.bat on those PCs: sheet 'Check these PCs'."),
             ("Confidence", "High = seen on the PC itself. Medium = a centre report (console columns or an Apex One server list) but not seen on the PC. A dash = no evidence."),
             ("Age of the evidence", f"PC checks of these machines are dated {min(pcdates) if pcdates else '-'} to {max(pcdates) if pcdates else '-'}. A PC check is usually newer than the centre lists, so a difference can also mean the machine changed since."),
             ("Important limit", f"{determined} of the {n} endpoints ({_pct(determined, n)}) can be classified from evidence; {need85} more PC checks are needed to reach 85%." +
              (f" The component seen on the PC agrees with the console in {agree} of {len(both)} machines ({_pct(agree, len(both))})." if both else "")),
             ("Checked before release", ("Every conclusion was recomputed a second way: all " + str(d["checked"]) + " agree." if d["verified"] else "DO NOT CIRCULATE: " + str(len(d["bad"])) + " conclusions differ when recomputed (" + "; ".join(d["bad"][:3]) + ").")),
             ("Made", f"{d['made']:%d %b %Y %H:%M} from the portal's data" + (f", for the machines assigned to {d['scope'].title()}" if d["scope"] else "") + ".")]
    t = [T("About this report", [("k", "Topic"), ("v", "Detail")], [{"k": a, "v": b} for a, b in about]), T("Endpoint report", MAIN_COLS, show),
         T("Verify first", [("issue", "Issue"), ("ci", "Asset (CI)"), ("engineer", "Engineer"), ("location", "Location"), ("status", "Asset status"), ("detail", "Detail"), ("todo", "What to do")], verify),
         T("Check these PCs", [("ci", "Asset (CI)"), ("engineer", "Engineer"), ("user", "User"), ("location", "Location"), ("status", "Asset status"), ("model", "Model"), ("serial", "Serial no"), ("todo", "What to do")], check)]
    if full and not d["scope"]:
        unreg = [{"name": u["name"], "os": u["os"], "ip": u["ip"], "kind": {"unregistered": "Not in the asset register", "mismatch": "Probably a register machine under a slightly different name", "outside": "In the register, but as another class"}.get(u["kind"], u["kind"]),
                  "files": u.get("files", ""), "todo": u["action"]} for u in prim["unregistered"]]
        checks = [{"k": "Installed component seen on the PC compared with the console's anti-malware component", "n": len(both), "ok": agree, "pct": _pct(agree, len(both)), "note": "Machines checked on the PC that a console also lists. A component counts as present even if switched off."},
                  {"k": "Same machines: console shows anti-malware switched ON", "n": len(both), "ok": am_on, "pct": _pct(am_on, len(both)), "note": f"The other {len(both) - am_on} run the agent on the PC but the console shows anti-malware disabled: sheet 'Verify first'."},
                  {"k": "Machines a console lists, checked on the PC: agent really there", "n": len(cons), "ok": sum(1 for m in cons if m["pc_agent"] == "Yes"), "pct": _pct(sum(1 for m in cons if m["pc_agent"] == "Yes"), len(cons)), "note": "Tests that 'listed in the console' means 'installed'."},
                  {"k": "Machines an Apex One server list shows, checked on the PC: agent really there", "n": len(srv), "ok": sum(1 for m in srv if m["pc_agent"] == "Yes"), "pct": _pct(sum(1 for m in srv if m["pc_agent"] == "Yes"), len(srv)), "note": "Basis for 'Full suite - Apex One server list'."},
                  {"k": "Machines of the analysed report, checked on the PC: agent really there", "n": len(own), "ok": sum(1 for m in own if m["pc_agent"] == "Yes"), "pct": _pct(sum(1 for m in own if m["pc_agent"] == "Yes"), len(own)), "note": "Shows why a listing alone is not proof: the 'contradicted' machines."},
                  {"k": "Endpoints of the analysed report that can be classified from evidence", "n": n, "ok": determined, "pct": _pct(determined, n), "note": f"{cnt[CMP[0]]} confirmed on the PC, {cnt[CMP[2]]} supported by a centre report, {cnt[CMP[1]]} contradicted. {cnt[CMP[3]]} not verifiable."},
                  {"k": "More PC checks needed to classify 85% of the analysed report", "n": need85, "ok": "", "pct": "", "note": "Machines = number of PCs still to check (sheet 'Check these PCs')."}]
        t += [T("Not in register", [("name", "Endpoint name (in the file)"), ("os", "Operating system"), ("ip", "IP address"), ("kind", "Finding"), ("files", "Found in file"), ("todo", "What to do")], unreg),
              T("Accuracy checks", [("k", "Check"), ("n", "Machines"), ("ok", "Agreeing"), ("pct", "Rate"), ("note", "Note")], checks),
              T("All register machines", ALL_COLS, [{**m, "engineer": m["engineer"].title(), "listed": "Yes" if m["listed"] else "No"} for m in sorted(d["all"], key=lambda m: (m["engineer"], m["ci"]))])]
    return t, {"n": n, "determined": determined, "need85": need85, "cnt": cnt, "agree": agree, "both": len(both), "am_on": am_on}


def kpis(d, info):
    ms, cnt = d["machines"], info["cnt"]
    fin = C(m["final"] for m in ms)
    k = [("Endpoints matched to the register", info["n"]), ("Confirmed on the PC", cnt[CMP[0]]), ("Supported by a centre report", cnt[CMP[2]]), ("Contradicted by the PC", cnt[CMP[1]]),
         ("Not verifiable (run ankcheck.bat)", cnt[CMP[3]]), ("Classified from evidence", f"{info['determined']} ({_pct(info['determined'], info['n'])})"), ("Full suite", fin["Full suite"]),
         ("Sensor only (installed)", fin["Sensor only"]), ("Not installed (PC check)", fin["Not installed"]), ("Not determined", fin["Not determined"])]
    if info["both"]:
        k.append(("Installed component agrees with the console", f"{info['agree']} of {info['both']} ({_pct(info['agree'], info['both'])})"))
    k.append(("Checked before release", "all agree" if d["verified"] else "DO NOT CIRCULATE"))
    return k


def render(d, by):
    t, info = tables(d)
    sub = f"Prepared {d['made']:%d %b %Y} - for verification by the central team" + (f" - machines assigned to {d['scope'].title()}" if d["scope"] else "")
    return export.xlsx_bytes(t, "Detailed endpoint report" + (f" - {d['scope'].title()}" if d["scope"] else ""), kpis(d, info), sub, generated_by=by), export.MIME["xlsx"], "xlsx"


def filename(d):
    return export.safe_name("Endpoint_Report" + (f"_{d['scope']}" if d["scope"] else "") + f"_{dt.date.today():%Y%m%d}") + ".xlsx"
