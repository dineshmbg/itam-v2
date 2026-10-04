"""Dashboard packs: the same live figures a dashboard shows, arranged for Excel, PDF and e-mail (share with management)."""
import datetime as dt

from . import dashboards, engineers, pm


def _kpis(d, spec):
    return [(label, d.get(key)) for key, label in spec if key in d]


def _n(rows, key="n", label="label"):
    return [{"label": r[label], "n": r[key]} for r in rows]


def _t(title, columns, rows):
    return {"title": title, "columns": columns, "rows": rows}


def assets():
    d = dashboards.assets()
    k = d["kpi"]
    kp = _kpis(k, [("assets", "Assets"), ("components", "Components"), ("in_use", "Deployed"), ("idle", "Idle / in stock"), ("removed", "Out of AMC"), ("cover_active", "Cover active"),
                   ("cover_expiring", "Cover renewal due (90 days)"), ("cover_expired", "Cover lapsed"), ("pm_done", "PM completed"), ("pm_pending", "PM scheduled"), ("users_inactive", "Users not active in HR"), ("dq_rows", "Rows with data-quality flags")])
    ch = [{"title": "Assets by class", "rows": [{"label": r["label"], "n": r["total"]} for r in d["by_class"]]}, {"title": "Assets by status", "rows": _n(d["status"])},
          {"title": "Cover status", "rows": _n(d["cover"])}, {"title": "Assets per engineer", "rows": [{"label": r["label"], "n": r["total"]} for r in d["by_engineer"]]},
          {"title": "Top makes", "rows": _n(d["by_make"])}, {"title": "Cover ending, next 12 months", "rows": _n(d["expiry"])}]
    tb = [_t("Attention: cover expired or ending", [("id", "Asset"), ("asset_class", "Class"), ("model", "Model"), ("user_name", "User"), ("cover_type", "Cover"), ("cover_expiry_date", "Cover ends"), ("cover_status", "Status")], d["attention"]),
          _t("By class", [("label", "Class"), ("total", "Total"), ("in_use", "Deployed"), ("idle", "Idle"), ("other", "Other")], d["by_class"]),
          _t("By engineer", [("label", "Engineer"), ("total", "Assets"), ("cover_risk", "Cover at risk"), ("pm_pending", "PM scheduled")], d["by_engineer"]),
          _t("Preventive maintenance by class", [("label", "Class"), ("done", "Completed"), ("stale", "Completed late"), ("pending", "Scheduled")], d["pm"]),
          _t("Locations", [("label", "Location"), ("n", "Assets")], d["locations"]), _t("Data-quality flags", [("label", "Flag"), ("n", "Rows")], d["flags"]),
          _t("Added / removed, last 90 days", [("day", "Date"), ("label", "Change"), ("n", "Assets")], d["changes"])]
    return {"title": "Asset dashboard", "subtitle": f"As of {k['as_of']:%d %b %Y} - PM quarter {k['pm_quarter']}", "kpis": kp, "charts": ch, "tables": tb}


def calls():
    d = dashboards.calls()
    k = d["kpi"]
    kp = _kpis(k, [("total", "Calls logged"), ("open", "Calls raised, unresolved"), ("awaiting_part", "Awaiting a part"), ("oldest_open", "Oldest open (days)"), ("median_open_age", "Median open age (days)"),
                   ("avg_tat", "Average TAT (days)"), ("median_tat", "Median TAT (days)"), ("this_month", "Calls this month"), ("last_month", "Calls last month"), ("p1", "P1 calls"), ("repeat_assets", "Assets with 3+ calls")])
    s = d["sla"]
    if s["closed"]:
        kp += [("Closed within 1 day (%)", round(100 * s["within_1d"] / s["closed"], 1)), ("Closed within 3 days (%)", round(100 * s["within_3d"] / s["closed"], 1)), ("Closed within 7 days (%)", round(100 * s["within_7d"] / s["closed"], 1))]
    ch = [{"title": "Calls per month", "rows": [{"label": r["label"], "n": r["total"]} for r in d["monthly"]]}, {"title": "Age of open calls", "rows": _n(d["ageing"])},
          {"title": "Priority", "rows": _n(d["priority"])}, {"title": "Calls per engineer", "rows": [{"label": r["label"], "n": r["total"]} for r in d["by_engineer"]]},
          {"title": "Top problems", "rows": _n(d["problems"])}, {"title": "Calls by asset class", "rows": _n(d["by_class"])}]
    tb = [_t("Oldest open calls", [("id", "SR ID"), ("cipl_call_date", "Logged"), ("asset_key", "Asset"), ("problem_description", "Problem"), ("engineer", "Engineer"), ("ageing_days", "Age (d)"), ("spare_status", "Spare")], d["oldest"]),
          _t("By engineer", [("label", "Engineer"), ("total", "Calls"), ("open", "Raised"), ("closed", "Resolved"), ("avg_tat", "Avg TAT (d)"), ("oldest", "Oldest open (d)")], d["by_engineer"]),
          _t("Turnaround by asset class", [("label", "Class"), ("closed", "Resolved"), ("open", "Raised"), ("avg_tat", "Avg TAT (d)")], d["tat_class"]),
          _t("Monthly", [("label", "Month"), ("total", "Calls"), ("closed", "Resolved"), ("open", "Raised"), ("avg_tat", "Avg TAT (d)")], d["monthly"]),
          _t("Assets with repeat calls", [("id", "Asset"), ("asset_class", "Class"), ("model", "Model"), ("n", "Calls")], d["repeat"]), _t("Calls by site", [("label", "Site"), ("n", "Calls")], d["by_site"])]
    return {"title": "Call tracker dashboard", "subtitle": f"As of {k['as_of']:%d %b %Y}", "kpis": kp, "charts": ch, "tables": tb}


def engineers_pack():
    d = engineers.overview()
    k = d["kpi"]
    kp = _kpis(k, [("engineers", "Engineers on roster"), ("with_workload", "With workload"), ("open_calls", "Calls raised, unresolved"), ("assets", "Assets under care"), ("pm_pending", "PM scheduled"), ("pm_pct", "PM completion (%)"), ("onboarding_open", "Onboarding open")])
    rows = d["rows"]
    ch = [{"title": "Open calls per engineer", "rows": [{"label": r["display_name"], "n": r["calls_open"]} for r in rows if r["calls_open"]]},
          {"title": "Assets per engineer", "rows": [{"label": r["display_name"], "n": r["assets"]} for r in rows if r["assets"]]}]
    tb = [_t("Engineers", [("display_name", "Engineer"), ("ecode", "ECODE"), ("designation", "Designation"), ("employment_status", "Roster status"), ("assets", "Assets"), ("calls_open", "Calls raised, unresolved"), ("calls_closed", "Resolved calls"),
                           ("avg_tat", "Avg TAT (d)"), ("oldest_open", "Oldest open (d)"), ("pm_done", "PM completed"), ("pm_pending", "PM scheduled"), ("pm_pct", "PM %"), ("onboarding_status", "Onboarding")], rows)]
    return {"title": "Engineer dashboard", "subtitle": f"Roster as of {k['as_of']:%d %b %Y}" if k.get("as_of") else "", "kpis": kp, "charts": ch, "tables": tb}


def pm_pack():
    d = pm.dashboard()
    k, c = d["kpi"], d["cycle"]
    kp = [("Cycle", c["label"]), ("Kick-off date", f"{c['kickoff']:%d %b %Y}"), ("Quarter ends", f"{c['end']:%d %b %Y}"), ("Days left", k["days_left"]), ("Assets in scope", k["scope"]), ("PM completed", k["done"]),
          ("Completed late", k["stale"]), ("PM scheduled", k["pending"]), ("Completion (%)", k["pct_done"]), ("Time elapsed (%)", k["expected_pct"]), ("Scheduled, no engineer", k["unassigned"])]
    ch = [{"title": "Pending PM per engineer", "rows": [{"label": r["label"], "n": r["pending"]} for r in d["by_engineer"] if r["pending"]]}, {"title": "Pending PM by class", "rows": [{"label": r["label"], "n": r["pending"]} for r in d["by_class"] if r["pending"]]},
          {"title": "Pending PM by location", "rows": [{"label": r["label"], "n": r["pending"]} for r in d["by_location"] if r["pending"]]}]
    tb = [_t("By engineer", [("label", "Engineer"), ("scope", "In scope"), ("done", "Completed"), ("stale", "Completed late"), ("pending", "Scheduled")], d["by_engineer"]),
          _t("By class", [("label", "Class"), ("scope", "In scope"), ("done", "Completed"), ("stale", "Completed late"), ("pending", "Scheduled")], d["by_class"]),
          _t("By location", [("label", "Location"), ("scope", "In scope"), ("done", "Completed"), ("pending", "Scheduled")], d["by_location"]),
          _t("Previous quarters (snapshots)", [("label", "Quarter"), ("as_of", "As of"), ("scope", "In scope"), ("done", "Completed"), ("pending", "Scheduled")], d["quarters"]),
          _t("Recently recorded", [("asset_key", "Asset"), ("pm_date", "PM date"), ("done_by", "Done by"), ("signed_by", "Signed by"), ("recorded_by", "Recorded by")], d["recent"])]
    return {"title": "Preventive maintenance dashboard", "subtitle": f"{c['label']} - as of {dt.date.today():%d %b %Y}", "kpis": kp, "charts": ch, "tables": tb}


PACKS = {"assets": assets, "calls": calls, "engineers": engineers_pack, "pm": pm_pack}
