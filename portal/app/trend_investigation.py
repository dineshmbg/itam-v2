"""Detailed Trend Micro investigation: for every machine in the register, is the agent there, is the sensor there, is anti-malware working - and how sure are we.

It is built on demand from what the portal already holds, so it is always current and nothing is stored twice:
  - the newest analysed Trend Micro console report (Vision One endpoint inventory; it carries anti-malware, EDR sensor, sensor connectivity, package),
  - the newest analysed older centre lists (Apex One server list, old Apex One inventory ...): a machine in one of them is *listed*, which does not prove an agent,
  - the newest PC check of each machine (software observations, source SCRIPT): the only evidence taken on the machine itself,
  - the newest BigFix analysis (is the machine reachable at all).
It is generated automatically after every analysis of a Trend Micro related report (routes_tools.match_run), e-mailed to each engineer (their own machines) and to the person
who ran it (all machines), and downloadable as Excel and PDF next to the software report (engineers get their own machines only).
"""
import datetime as dt
import json
import re
from collections import Counter

from . import db, export
from . import inventory_match as im

RULE = "TREND_INVESTIGATION"          # notify_log.rule_key
TREND_RE = re.compile(r"apex|trend|officescan|worry|vision|endpoint", re.I)
CATS = ["Not installed - confirmed on the PC", "Sensor only - anti-malware disabled (console)", "Agent registered - all protection disabled (console)", "Installed - confirmed on the PC",
        "Installed - per Vision One console, not checked on the PC", "Only in older centre lists - not verified", "Not found in any centre report, not checked on the PC"]
PROT = ["Both (anti-malware + sensor)", "Anti-malware only (no sensor)", "Sensor only (anti-malware off)", "Neither (all disabled)", "Not in Vision One", "Not known (report analysed before this was kept)"]
PCTXT = {"Y": "Installed", "N": "Not installed", "-": "Not checked"}
VER_HINT = {"newer": "Information: the agent was probably upgraded after the console export.", "older": "The PC has an older agent than the console shows: check that the agent updates itself."}
NEED = (CATS[0], CATS[1], CATS[2], CATS[5], CATS[6])          # statuses that need someone to act


class InvError(Exception):
    http_error = True

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- which analyses count
def _is_trend(r):
    s = r["summary"] or {}
    if s.get("product") == "bigfix":
        return False
    names = [r["tool"] or "", r["filename"] or ""] + [f.get("name", "") for f in s.get("files", [])]
    return s.get("product") == "trendmicro" or bool(TREND_RE.search(" ".join(names)))


def is_trend_run(run_id):
    r = db.one("SELECT run_id, tool, filename, summary FROM portal_match_run WHERE run_id = %s", [run_id])
    return bool(r) and _is_trend(r)


def _stem(r):
    """The same centre list uploaded again (a newer copy, or an 'ank-' filtered copy) is one list: file name without extension, digits, punctuation and a leading 'ank'."""
    first = ((r["summary"] or {}).get("files") or [{"name": r["filename"] or ""}])[0].get("name", "")
    s = re.sub(r"[^a-z]", "", re.sub(r"\.[a-z0-9]+$", "", first.lower()))
    return s[3:] if s.startswith("ank") and len(s) > 6 else s


def _label(r):
    return ((r["summary"] or {}).get("files") or [{"name": r["filename"]}])[0].get("name", r["filename"])


def _pick():
    """Newest Trend Micro console analysis, the newest of each older centre list, and the newest BigFix analysis."""
    rows = db.query("SELECT run_id, at, username, tool, filename, summary FROM portal_match_run ORDER BY at DESC LIMIT 80")
    vision = next((r for r in rows if (r["summary"] or {}).get("product") == "trendmicro"), None)
    bigfix = next((r for r in rows if (r["summary"] or {}).get("product") == "bigfix"), None)
    lists, seen = [], set()
    for r in rows:
        if r is vision or (r["summary"] or {}).get("product") in ("trendmicro", "bigfix") or not _is_trend(r):
            continue
        if _stem(r) not in seen:
            seen.add(_stem(r))
            lists.append(r)
    return vision, lists, bigfix


# ---------------------------------------------------------------- gathering the evidence
def _day(v):
    return v.strftime("%d-%b-%Y") if isinstance(v, (dt.datetime, dt.date)) else (v or "")


def _short_day(s):
    """'2026-10-06 10:39' -> '6 Oct'."""
    try:
        return dt.datetime.strptime(s[:10], "%Y-%m-%d").strftime("%d %b").lstrip("0")
    except (TypeError, ValueError):
        return ""


def _vkey(v):
    """'14.0.21008' -> (14, 0, 21008), so versions compare by number; None when it is not a plain dotted number."""
    try:
        return tuple(int(x) for x in (v or "").strip().split("."))
    except ValueError:
        return None


def version_gap(pc_ver, console_ver):
    """'newer' / 'older' when the PC agent differs from the console's registered one, '' when equal or either is unknown."""
    a, b = _vkey(pc_ver), _vkey(console_ver)
    if not a or not b or a == b:
        return ""
    return "newer" if a > b else "older"


def gather():
    vision, lists, bigfix = _pick()
    if not vision and not lists:
        raise InvError("No Trend Micro report has been analysed yet. Run Inventory match with the centre's Trend Micro report first.", 404)
    vrun = im.get(vision["run_id"]) if vision else None
    lruns = [im.get(r["run_id"]) for r in lists]
    brun = im.get(bigfix["run_id"]) if bigfix else None
    newest = vrun or lruns[0]
    prefixes = (newest["summary"].get("prefixes") or ["ANK"])
    classes = (newest["summary"].get("classes") or im.DEFAULT_CLASSES)
    register = im._assets(prefixes, classes)                    # today's register, so engineers and users are current
    keys = {im.key_of(a["ci_no"]): a["ci_no"] for a in register}
    asset_key = {r["ci_no"]: r["asset_key"] for r in db.query("SELECT asset_key, ci_no FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND ci_no = ANY(%s)", [list(keys.values())])}
    obs = {r["asset_key"]: r for r in db.query("SELECT DISTINCT ON (asset_key) asset_key, trend_installed, trend_version, trend_status, inspected_at FROM portal_sw_obs WHERE source = 'SCRIPT' AND asset_key = ANY(%s) "
                                                "ORDER BY asset_key, obs_id DESC", [list(asset_key.values())])}
    by = lambda run: {im.key_of(a["ci"]): a for a in run["assets"]} if run else {}      # noqa: E731
    dups = {im.key_of(x["name"]): x for x in (vrun or {}).get("duplicates", [])}          # names the console lists more than once
    return {"dups": dups, "v1_day": _short_day(((vrun or {}).get("summary", {}).get("files") or [{}])[0].get("report_time")), "register": register, "vision": (vrun, by(vrun)), "lists": [(r, by(r)) for r in lruns], "bigfix": (brun, by(brun)), "pc": {k: obs.get(asset_key.get(ci)) for k, ci in keys.items()}, "prefixes": prefixes}


# ---------------------------------------------------------------- one row per machine
def _protection(t):
    """Both / anti-malware only / sensor only / neither, from the console's own columns (the sensor is the EDR (XDR) feature)."""
    if "t_am" not in t:
        return PROT[5]
    am_on, edr_on = (t["t_am"] or "").lower() != "disabled", (t["t_edr"] or "").lower() in ("enabled", "enabling")
    return {(True, True): PROT[0], (True, False): PROT[1], (False, True): PROT[2], (False, False): PROT[3]}[(am_on, edr_on)]


def _missing(p, t):
    if p == PROT[0]:
        return "Nothing - but the sensor shows Disconnected" if (t.get("t_sensor_conn") or "").lower() == "disconnected" else ""
    if p == PROT[1]:
        return "EDR sensor (installed but disabled)" if t.get("t_sensor_ver") else "EDR sensor (not installed)"
    if p == PROT[2]:
        return "Anti-malware"
    if p == PROT[3]:
        return "Anti-malware and EDR sensor"
    return "Unknown - the machine is not in the Vision One report" if p == PROT[4] else "Unknown - upload the Vision One report again"


def machines(g):
    vrun, vby = g["vision"]
    brun, bby = g["bigfix"]
    out = []
    for a in g["register"]:
        k = im.key_of(a["ci_no"])
        v = vby.get(k)
        in_v = bool(v and v["installed"])
        lists = [(im.key_of(a["ci_no"]) in lby and lby[im.key_of(a["ci_no"])]["installed"]) for _, lby in g["lists"]]
        any_list = any(lists)
        o = g["pc"].get(k)
        pc = "-" if o is None or o["trend_installed"] is None else ("Y" if o["trend_installed"] else "N")
        t = v if in_v else {}
        prot = _protection(t) if in_v else PROT[4]
        sensor_only, all_off = prot == PROT[2], prot == PROT[3]
        if pc == "N": cat = CATS[0]
        elif sensor_only: cat = CATS[1]
        elif all_off: cat = CATS[2]
        elif pc == "Y": cat = CATS[3]
        elif in_v: cat = CATS[4]
        elif any_list: cat = CATS[5]
        else: cat = CATS[6]
        notes = []
        if pc == "Y" and not in_v and not any_list: notes.append("PC check found Trend Micro, but the machine is in no centre report")
        if pc == "Y" and in_v and (t.get("t_conn") or "").lower() == "not communicating": notes.append("PC has the agent; the centre console shows 'Not communicating'")
        if pc == "N" and (in_v or any_list): notes.append("PC check found NO Trend Micro, but a centre report lists the machine")
        if pc == "Y" and not in_v and any_list: notes.append("PC check found Trend Micro; the centre lists it only in an older report")
        if pc == "Y" and (sensor_only or all_off): notes.append("PC has the agent; the console shows protection disabled")
        gap = version_gap((o or {}).get("trend_version") if pc == "Y" else "", t.get("t_pm_ver"))
        if gap: notes.append(f"Agent version on PC ({o['trend_version']}) differs from console ({t['t_pm_ver']}{', ' + g['v1_day'] if g.get('v1_day') else ''})")
        if t.get("t_action"): notes.append("Console has an install/upgrade error pending: " + t["t_action"])
        if in_v and k in g.get("dups", {}): notes.append("Console had two records for this name; the newest was used")
        if vrun and k not in vby: notes.append("Not in the register when the Vision One report was analysed")
        out.append({"ci": a["ci_no"], "class": a["asset_class"].title(), "status": (a["asset_status"] or "").replace("_", " ").capitalize(), "engineer": a["engineer_name"] or "UNASSIGNED", "user": a["user_name"] or "",
                    "location": a["location_code"] or "", "make": a["make"] or "", "model": a["model"] or "", "final": cat, "prot": prot, "missing": _missing(prot, t) if in_v else _missing(prot, {}),
                    "pc": PCTXT[pc], "pc_product": (o or {}).get("trend_status") or "" if pc != "-" else "", "pc_version": (o or {}).get("trend_version") or "" if pc != "-" else "",
                    "pc_date": _day(o["inspected_at"]) if o and pc != "-" else "", "v1": "Yes" if in_v else "No", "v1_conn": t.get("t_conn", ""), "v1_ver": t.get("t_pm_ver", ""), "ver_gap": gap, "v1_day": g.get("v1_day", ""), "v1_dup": bool(in_v and k in g.get("dups", {})), "v1_seen": (v or {}).get("r_seen") or "" if in_v else "",
                    "v1_sensor": (t.get("t_sensor_conn") or "No sensor record") if in_v and "t_am" in t else "", "v1_xdr": t.get("t_edr", ""), "v1_am": t.get("t_am", ""), "v1_pkg": t.get("t_pkg", "") or "Not shown" if in_v and "t_am" in t else "",
                    "lists": ["Yes" if x else "No" for x in lists], "bigfix": "Yes" if (a["ci_no"] and im.key_of(a["ci_no"]) in bby and bby[im.key_of(a["ci_no"])]["installed"]) else "No", "notes": "; ".join(notes),
                    "in_use": a["asset_status"] == "IN_USE", "sensor_disc": prot == PROT[0] and (t.get("t_sensor_conn") or "").lower() == "disconnected"})
    return out


# ---------------------------------------------------------------- numbers and tables (for everyone, or for one engineer's machines)
def _pct(a, b):
    return f"{100.0 * a / b:.1f}%" if b else "-"


def build(engineer_key=None):
    """The whole investigation as data. `engineer_key` keeps only that engineer's machines (and leaves out the fleet-wide parts)."""
    g = gather()
    allm = machines(g)
    ms = [m for m in allm if m["engineer"] == engineer_key] if engineer_key else allm
    if engineer_key and not ms:
        raise InvError("No machine is assigned to you, so there is nothing to investigate.", 404)
    return {"g": g, "machines": ms, "all": allm, "scope": engineer_key, "made": dt.datetime.now()}


def _count(ms, f):
    return sum(1 for m in ms if f(m))


def tables(d, full=True):
    g, ms = d["g"], d["machines"]
    N = len(ms)
    vrun = g["vision"][0]
    n_v1 = _count(ms, lambda m: m["v1"] == "Yes")
    cn = Counter(m["final"] for m in ms)
    pn = Counter(m["prot"] for m in ms)
    pcy, pcn = _count(ms, lambda m: m["pc"] == "Installed"), _count(ms, lambda m: m["pc"] == "Not installed")
    iu = lambda f: _count(ms, lambda m: f(m) and m["in_use"])      # noqa: E731
    present = sum(cn[c] for c in CATS[1:5])
    nf = [m for m in ms if m["final"] == CATS[6]]
    nf_bf = _count(nf, lambda m: m["bigfix"] == "Yes")
    both_disc = _count(ms, lambda m: m["sensor_disc"])
    list_labels = [_label(r) for r, _ in g["lists"]]
    rel = []
    for name, cond in (("In the Vision One console", lambda m: m["v1"] == "Yes"), ("Only in older centre lists (not in Vision One)", lambda m: m["v1"] == "No" and "Yes" in m["lists"]),
                       ("In no centre report at all", lambda m: m["v1"] == "No" and "Yes" not in m["lists"])):
        c = [m for m in ms if cond(m) and m["pc"] != "Not checked"]
        y = _count(c, lambda m: m["pc"] == "Installed")
        rel.append({"where": name, "checked": len(c), "yes": f"{y} ({_pct(y, len(c))})", "no": f"{len(c) - y} ({_pct(len(c) - y, len(c))})"})
    stat = [{"status": c, "machines": cn[c], "pct": _pct(cn[c], N), "in_use": iu(lambda m, c=c: m["final"] == c)} for c in CATS]
    stat.append({"status": "TOTAL machines", "machines": N, "pct": "100.0%", "in_use": iu(lambda m: True)})
    prot = [{"state": p, "machines": pn[p], "pct": _pct(pn[p], n_v1) if p in PROT[:4] else "", "in_use": iu(lambda m, p=p: m["prot"] == p),
             "todo": {PROT[0]: "Nothing missing in the console (see the sensors shown as Disconnected).", PROT[1]: "The EDR sensor is not working: ask the centre to install / enable it.",
                      PROT[2]: "Anti-malware is disabled: ask the centre to enable it.", PROT[3]: "Anti-malware and the sensor are both disabled: ask the centre why; re-enable or reinstall.",
                      PROT[4]: "No console record: see the status table.", PROT[5]: "Upload the Trend Micro console report again so the sensor state is kept."}[p]} for p in PROT if pn[p] or p in PROT[:4]]
    prot.append({"state": "of the 'Both' machines, the sensor shows Disconnected", "machines": both_disc, "pct": _pct(both_disc, n_v1), "in_use": iu(lambda m: m["sensor_disc"]), "todo": "Listed on the Sensor catch-up sheet."})
    am_only_no, am_only_off = _count(ms, lambda m: m["missing"] == "EDR sensor (not installed)"), _count(ms, lambda m: m["missing"] == "EDR sensor (installed but disabled)")
    ni = [m for m in ms if m["final"] == CATS[0]]
    find = [
        f"Trend Micro agent present on {present:,} of {N:,} machines ({_pct(present, N)}): {pcy:,} proven on the PC, {present - pcy:,} registered in the centre's Vision One console. Only {pcy + pcn:,} machines have been checked on the PC "
        f"so far{', so most of the figure rests on the centre console' if pcy + pcn < N / 2 else ''}.",
        f"{cn[CATS[0]]} machines have NO Trend Micro (PC check; {iu(lambda m: m['final'] == CATS[0])} in use). Install the agent." + (f" {_count(ni, lambda m: m['v1'] == 'Yes' or 'Yes' in m['lists'])} of them are listed by the centre, which shows a listing does not prove an agent."
                                                                                                                               if ni else ""),
        f"{cn[CATS[1]] + cn[CATS[2]]} machines have the agent but protection is off in the console: {cn[CATS[1]]} sensor only (anti-malware disabled) and {cn[CATS[2]]} with both disabled. Ask the centre to re-enable it.",
        f"Sensor only or both ({n_v1} machines in Vision One): {pn[PROT[0]]} both anti-malware and sensor; {pn[PROT[1]]} anti-malware only ({am_only_no} no sensor installed, {am_only_off} sensor disabled); {pn[PROT[2]]} sensor only; "
        f"{pn[PROT[3]]} neither; and {both_disc} of the 'both' machines show the sensor as Disconnected. The Sensor catch-up sheet lists them.",
        f"{cn[CATS[6]]} machines are in no centre report and not checked on the PC ({iu(lambda m: m['final'] == CATS[6])} in use): {nf_bf} report to BigFix (run the check script there first) and {len(nf) - nf_bf} have not been seen by BigFix, "
        f"Trend Micro or the check script (locate them).",
        f"{cn[CATS[5]]} machines are only in older centre lists ({iu(lambda m: m['final'] == CATS[5])} in use): run the check script to confirm and ask the centre to move them to Vision One.",
    ]
    for r in rel[2:]:
        if r["checked"]:
            find.append(f"The centre reports miss machines that do have Trend Micro: of {r['checked']} PCs in no centre report, {r['yes']} had the agent.")
    tabs = [{"title": "Findings", "columns": [("n", "#"), ("t", "Finding")], "rows": [{"n": i, "t": t} for i, t in enumerate(find, 1)]},
            {"title": "Where machines stand", "columns": [("status", "Status"), ("machines", "Machines"), ("pct", "% of all"), ("in_use", "In use")], "rows": stat},
            {"title": "Sensor only or both", "columns": [("state", "Protection working"), ("machines", "Machines"), ("pct", "% of Vision One"), ("in_use", "In use"), ("todo", "What is missing and what to do")], "rows": prot},
            {"title": "Centre report reliability", "columns": [("where", "Where the machine appears"), ("checked", "PC checked"), ("yes", "Agent found on PC"), ("no", "No agent on PC")], "rows": rel}]
    if full and not d["scope"]:
        eng = Counter(m["engineer"] for m in ms)
        tabs.append({"title": "By engineer", "columns": [("e", "Engineer"), ("m", "Machines"), ("a", "Need attention"), ("u", "Need attention, in use"), ("p", "Anti-malware only"), ("s", "Sensor only"), ("n", "Neither"), ("d", "Both, sensor disconnected")],
                     "rows": [{"e": e, "m": eng[e], "a": _count(ms, lambda m, e=e: m["engineer"] == e and m["final"] in NEED), "u": _count(ms, lambda m, e=e: m["engineer"] == e and m["final"] in NEED and m["in_use"]),
                               "p": _count(ms, lambda m, e=e: m["engineer"] == e and m["prot"] == PROT[1]), "s": _count(ms, lambda m, e=e: m["engineer"] == e and m["prot"] == PROT[2]),
                               "n": _count(ms, lambda m, e=e: m["engineer"] == e and m["prot"] == PROT[3]), "d": _count(ms, lambda m, e=e: m["engineer"] == e and m["sensor_disc"])} for e in sorted(eng, key=lambda e: -eng[e])]})
    order = {c: i for i, c in enumerate([CATS[0], CATS[1], CATS[2], CATS[6], CATS[5], CATS[4], CATS[3]])}
    todo = sorted((m for m in ms if m["final"] in NEED), key=lambda m: (order[m["final"]], not m["in_use"], m["engineer"], m["ci"]))
    step = {CATS[0]: "Install the Trend Micro agent, then run the check script again.", CATS[1]: "Ask the centre to enable anti-malware for this machine.", CATS[2]: "Ask the centre why protection is disabled; re-enable or reinstall the agent.",
            CATS[5]: "Run the check script on the PC to confirm; ask the centre to move the agent to Vision One.", CATS[6]: "Run the check script on the PC first; if no agent, install it." }
    for m in todo:
        m["next"] = ("Locate the machine: no tool has seen it. Confirm it is in use / on the network, then run the check script." if m["final"] == CATS[6] and m["bigfix"] == "No" else step[m["final"]])
    acols = [("ci", "Asset (CI)"), ("class", "Class"), ("status", "Asset status"), ("engineer", "Engineer"), ("user", "User"), ("location", "Location"), ("final", "Trend Micro status"), ("prot", "Protection working"), ("next", "Suggested next step")]
    tabs.append({"title": "Action list", "columns": acols, "rows": todo})
    if full:
        sc = [m for m in ms if m["prot"] in (PROT[1], PROT[2], PROT[3]) or m["sensor_disc"]]
        so = {PROT[3]: 0, PROT[2]: 1, PROT[1]: 2, PROT[0]: 3}
        sc.sort(key=lambda m: (so[m["prot"]], not m["in_use"], m["engineer"], m["ci"]))
        for m in sc:
            m["next"] = {PROT[3]: "Ask the centre why anti-malware and the sensor are disabled; re-enable or reinstall the agent.", PROT[2]: "Ask the centre to enable anti-malware for this machine.",
                         PROT[1]: ("EDR sensor installed but disabled: ask the centre to enable EDR (XDR)." if m["missing"].endswith("disabled)") else "No EDR sensor installed: ask the centre to deploy it."),
                         PROT[0]: "Sensor enabled but Disconnected: check the machine's connection to the centre (network, proxy) and the sensor service."}[m["prot"]]
        tabs.append({"title": "Sensor catch-up", "columns": [("ci", "Asset (CI)"), ("class", "Class"), ("status", "Asset status"), ("engineer", "Engineer"), ("user", "User"), ("location", "Location"), ("prot", "Protection working"),
                                                          ("missing", "What is missing"), ("v1_sensor", "Sensor connectivity"), ("v1_conn", "Agent connection"), ("v1_seen", "Last status reported"), ("pc", "PC check"), ("next", "Suggested next step")], "rows": sc})
        lc = [(f"l{i}", f"In {lab}") for i, lab in enumerate(list_labels)]
        rows = [{**m, **{f"l{i}": x for i, x in enumerate(m["lists"])}} for m in sorted(ms, key=lambda m: (m["final"] not in NEED, order[m["final"]], not m["in_use"], m["engineer"], m["ci"]))]
        tabs.append({"title": "Machine status", "columns": [("ci", "Asset (CI)"), ("class", "Class"), ("status", "Asset status"), ("engineer", "Engineer"), ("user", "User"), ("location", "Location"), ("make", "Make"), ("model", "Model"),
                                                         ("final", "Trend Micro status"), ("prot", "Protection working"), ("missing", "What is missing (console)"), ("pc", "PC check"), ("pc_product", "PC: product found"),
                                                         ("pc_version", "PC: agent version"), ("pc_date", "PC check recorded"), ("v1", "In Vision One"), ("v1_conn", "Console: agent connection"), ("v1_seen", "Console: last status"),
                                                         ("v1_sensor", "Console: sensor connectivity"), ("v1_xdr", "Console: EDR sensor"), ("v1_am", "Console: anti-malware"), ("v1_pkg", "Console: package"), ("v1_ver", "Console: agent version"), *lc,
                                                         ("bigfix", "In BigFix report"), ("notes", "Notes")], "rows": rows})
    diff = []
    for m in ms:
        if m["pc"] == "Not installed" and (m["v1"] == "Yes" or "Yes" in m["lists"]): diff.append(("PC found no agent, a centre report lists the machine", m))
        if m["pc"] == "Installed" and m["v1"] == "No" and "Yes" not in m["lists"]: diff.append(("PC has the agent, no centre report lists the machine", m))
        if m["pc"] == "Installed" and m["v1"] == "No" and "Yes" in m["lists"]: diff.append(("PC has the agent, only an older centre list has the machine", m))
        if m["pc"] == "Installed" and m["final"] in (CATS[1], CATS[2]): diff.append(("PC has the agent, console shows protection disabled", m))
        if m["pc"] == "Installed" and (m["v1_conn"] or "").lower() == "not communicating": diff.append(("PC has the agent, console shows 'Not communicating'", m))
        if m["ver_gap"]: diff.append(("Agent version differs between PC and console", m))
    if full and diff:
        tabs.append({"title": "PC vs centre", "columns": [("t", "Difference"), ("ci", "Asset (CI)"), ("engineer", "Engineer"), ("user", "User"), ("status", "Asset status"), ("pc_version", "PC: agent version"), ("v1_ver", "Console: agent version"), ("hint", "Note")],
                     "rows": [{"t": t, **m, "hint": VER_HINT.get(m["ver_gap"], "") if t.startswith("Agent version") else ""} for t, m in sorted(diff, key=lambda x: (x[0], x[1]["engineer"], x[1]["ci"]))]})
    if full and not d["scope"]:
        names = []
        for r, _ in ([g["vision"]] if g["vision"][0] else []) + g["lists"]:
            for u in r.get("unregistered", []):
                names.append({"name": u["name"], "src": _label(r), "why": u["why"]})
        if names:
            tabs.append({"title": "Centre names not in register", "columns": [("name", "Name in the centre report"), ("src", "Report"), ("why", "Why it is not matched")], "rows": sorted(names, key=lambda x: (x["src"], x["name"]))})
    src = []
    if g["vision"][0]:
        r = g["vision"][0]
        src.append({"s": "Trend Micro console (Vision One endpoint inventory)", "f": r["filename"], "d": _day(r["at"]), "m": _count(d["all"], lambda m: m["v1"] == "Yes")})
    for i, (r, _) in enumerate(g["lists"]):
        src.append({"s": "Older centre list", "f": _label(r), "d": _day(r["at"]), "m": _count(d["all"], lambda m, i=i: m["lists"][i] == "Yes")})
    if g["bigfix"][0]:
        src.append({"s": "BigFix", "f": g["bigfix"][0]["filename"], "d": _day(g["bigfix"][0]["at"]), "m": _count(d["all"], lambda m: m["bigfix"] == "Yes")})
    src.append({"s": "PC check (software observations, source SCRIPT)", "f": "newest check of each machine", "d": "", "m": _count(d["all"], lambda m: m["pc"] != "Not checked")})
    tabs.append({"title": "Sources and method", "columns": [("s", "Source"), ("f", "File"), ("d", "Analysed on"), ("m", "Register machines found")], "rows": src})
    return tabs


def kpis(d):
    ms = d["machines"]
    cn = Counter(m["final"] for m in ms)
    present = sum(cn[c] for c in CATS[1:5])
    return [("Machines", len(ms)), ("Trend Micro agent present", f"{present} ({_pct(present, len(ms))})"), ("Confirmed on the PC", _count(ms, lambda m: m["pc"] == "Installed")),
            ("Not installed (PC check)", cn[CATS[0]]), ("Sensor only / protection disabled", cn[CATS[1]] + cn[CATS[2]]), ("Only in older lists", cn[CATS[5]]), ("Not found anywhere", cn[CATS[6]]),
            ("Checked on the PC", f"{_count(ms, lambda m: m['pc'] != 'Not checked')} of {len(ms)}")]


def render(d, fmt, by):
    scope = f" - {d['scope'].title()}" if d["scope"] else ""
    title = "Trend Micro detailed investigation" + scope
    sub = (f"Register machines {'assigned to you' if d['scope'] else 'of the site'} checked against the centre's Trend Micro reports and the PC check - made {d['made']:%d %b %Y %H:%M}. "
           "The newest PC check of each machine and the newest analysed centre reports are used.")
    if fmt == "pdf":
        short = ["Not installed (PC check)", "Sensor only (anti-malware off)", "Agent, all protection disabled", "Installed - confirmed on the PC", "Installed - per console, unchecked", "Only in older lists", "Not found anywhere, unchecked"]
        ch = [{"title": "Machines by Trend Micro status", "rows": [{"label": s, "n": _count(d["machines"], lambda m, c=c: m["final"] == c)} for s, c in zip(short, CATS)]}]
        t = [x for x in tables(d, full=False)]
        for x in t:
            if x["title"] == "Action list":
                x["rows"] = x["rows"][:60]
                x["title"] = "Action list - first 60 (the Excel file has them all)"
        return export.pdf_bytes(title, sub, kpis(d), t, ch, by), "application/pdf", "pdf"
    return export.xlsx_bytes(tables(d, full=True), title, kpis(d), sub, generated_by=by), export.MIME["xlsx"], "xlsx"


def filename(d, ext):
    return export.safe_name("Trend_Micro_Investigation" + (f"_{d['scope']}" if d["scope"] else "") + f"_{dt.date.today():%Y%m%d}") + "." + ext


# ---------------------------------------------------------------- automatic: after every Trend Micro related analysis
def _engineer_name(key):
    r = db.one("SELECT display_name FROM portal_engineer WHERE engineer_key = %s", [key])
    return (r["display_name"] if r else key).title()


def on_run(run_id, user):
    """Called after an analysis. Does nothing unless it was a Trend Micro related report. Mails each engineer their own investigation (Excel + PDF) and the person who ran it the full one.
    A mail problem (e-mail off, a server error) never loses anything: the investigation stays downloadable and the outcome is returned."""
    from . import mailer
    if not is_trend_run(run_id):
        return None
    try:
        full = build()
    except InvError as e:
        return {"built": False, "error": str(e)}
    from . import endpoint_report as er       # the detailed endpoint report travels in the same e-mail (imported here: it imports this module)
    erd = er.build_safe(run_id)
    attached = 0
    cfg = mailer.get_smtp()
    plan = []
    people = sorted({m["engineer"] for m in full["all"] if m["engineer"] != im.UNASSIGNED})
    sent = {r["event_key"]: r["status"] for r in db.query("SELECT event_key, status FROM notify_log WHERE rule_key = %s AND event_key LIKE %s", [RULE, run_id + ":%"])}
    targets = [(None, user.get("email") or (db.one("SELECT email FROM portal_user WHERE username = %s", [user["username"]]) or {}).get("email"), user["username"])] + [(e, mailer.engineer_email(e), e) for e in people]
    for eng, to, who in targets:
        item = {"engineer": eng or "ALL (the person who ran the analysis)", "email": to, "status": "READY"}
        if not to:
            item["status"] = "NO_ADDRESS"
        elif sent.get(f"{run_id}:{who}") == "SENT":
            item["status"] = "ALREADY_SENT"
        elif not cfg["enabled"]:
            item["status"] = "MAIL_OFF"
        plan.append((item, eng, who))
    for item, eng, who in plan:
        if item["status"] != "READY":
            continue
        try:
            d = full if eng is None else build(eng)
            n_need = _count(d["machines"], lambda m: m["final"] in NEED)
            subject = f"Trend Micro detailed investigation{'' if eng is None else ' - your machines'}: {n_need} of {len(d['machines'])} machines need attention - {dt.date.today():%d %b %Y}"
            intro = ("Dear " + (_engineer_name(eng) if eng else "colleague") + ", a Trend Micro report was analysed. The attached detailed investigation shows, for "
                     + ("every machine" if eng is None else "each machine assigned to you") + ", whether the agent and the sensor are there and working, what is missing and what to do. The most urgent are below. "
                     "You can download it again at any time under Inventory match / My inventory report, next to the software report.")
            if erd and erd["verified"]:
                intro += (" A second attachment, the detailed endpoint report, compares the machines of the analysed endpoint report with what the PC check found on each PC and says, for each, whether it has the full suite"
                          " or the sensor only, and what is still to be checked.")
            urgent = next(t for t in tables(d, full=False) if t["title"].startswith("Action list"))["rows"][:12]
            body, text = mailer.page(subject, intro, urgent, [("ci", "Asset (CI)"), ("status", "Asset status"), ("final", "Trend Micro status"), ("next", "Suggested next step")],
                                     footer=f"Automatic message from the ITAM Portal (Trend Micro investigation, analysis run by {user['username']})." + (f" {cfg['portal_url']}" if cfg["portal_url"] else ""))
            files = []
            for fmt in ("xlsx", "pdf"):
                data, mime, ext = render(d, fmt, user["username"])
                files.append((filename(d, ext), data, mime))
            extra = er.file(erd, eng, user["username"]) if erd and erd["verified"] else None
            if extra:
                files.append(extra)
                attached += 1
            mailer.send(item["email"], subject, text, body, files, cfg=cfg)
            status, err = "SENT", None
        except Exception as e:      # noqa: BLE001 - one person's failure never stops the rest
            status, err = "FAILED", str(e)[:300]
        item["status"], item["error"] = status, err
        with db.write() as con:
            con.execute("""INSERT INTO notify_log (rule_key, event_key, recipient, subject, status, error) VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (rule_key, event_key, recipient) DO UPDATE SET status = EXCLUDED.status, error = EXCLUDED.error, at = now(), attempts = notify_log.attempts + 1""",
                        (RULE, f"{run_id}:{who}", item["email"], "Trend Micro detailed investigation", status, err))
    out = {"built": True, "machines": len(full["all"]), "mail_enabled": cfg["enabled"], "plan": [p[0] for p in plan],
           "endpoint_report": ({"built": True, "verified": erd["verified"], "machines": len(erd["listed"]), "differences": erd["bad"][:5], "attached": attached} if erd else {"built": False})}
    with db.write() as con:          # kept with the analysis, so reopening it later still says what was done
        con.execute("UPDATE portal_match_run SET result = result || jsonb_build_object('investigation', %s::jsonb) WHERE run_id = %s", (json.dumps(out, default=str), run_id))
    return out
