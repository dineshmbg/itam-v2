"""Engineer dashboard: workload, PM completion and onboarding, joined through the verified portal_engineer mapping."""
from . import db


def overview():
    rows = db.query("""
        WITH a AS (
          SELECT engineer_name AS k, count(*) FILTER (WHERE record_level='ASSET') AS assets,
                 count(*) FILTER (WHERE pm_status IN ('DONE','DONE_OUTSIDE_QUARTER')) AS pm_done,
                 count(*) FILTER (WHERE pm_status = 'PENDING') AS pm_pending
          FROM asset WHERE is_current=1 AND engineer_name IS NOT NULL GROUP BY 1),
        c AS (
          SELECT engineer AS k, count(*) AS calls_total, count(*) FILTER (WHERE call_status='OPEN') AS calls_open,
                 count(*) FILTER (WHERE call_status='CLOSED') AS calls_closed,
                 round(avg(tat_days) FILTER (WHERE call_status='CLOSED')::numeric, 1) AS avg_tat,
                 max(ageing_days) FILTER (WHERE call_status='OPEN') AS oldest_open
          FROM svc_call WHERE is_current=1 AND engineer IS NOT NULL GROUP BY 1),
        pm AS (SELECT pm_done_by AS k, count(*) AS pm_performed FROM asset WHERE is_current=1 AND pm_done_by IS NOT NULL GROUP BY 1)
        SELECT e.engineer_key AS id, e.display_name, e.ecode, e.source,
               r.designation, r.level, r.employment_status, r.gender, r.onboarding_status, r.onboarding_pending_items,
               r.date_of_joining_ongc, r.skill_category, r.deployed_at,
               coalesce(a.assets,0) AS assets, coalesce(a.pm_done,0) AS pm_done, coalesce(a.pm_pending,0) AS pm_pending,
               coalesce(pm.pm_performed,0) AS pm_performed,
               coalesce(c.calls_total,0) AS calls_total, coalesce(c.calls_open,0) AS calls_open, coalesce(c.calls_closed,0) AS calls_closed,
               c.avg_tat, c.oldest_open
        FROM portal_engineer e
        LEFT JOIN cipl_employee r ON r.ecode = e.ecode
        LEFT JOIN a ON a.k = e.engineer_key LEFT JOIN c ON c.k = e.engineer_key LEFT JOIN pm ON pm.k = e.engineer_key
        ORDER BY coalesce(c.calls_total,0) + coalesce(a.assets,0) DESC, e.display_name""")
    for r in rows:
        tracked = r["pm_done"] + r["pm_pending"]
        r["pm_pct"] = round(100.0 * r["pm_done"] / tracked, 1) if tracked else None
        r["on_roster"] = r["ecode"] is not None
    onboarding = db.query("SELECT coalesce(onboarding_status,'-') AS label, count(*) AS n FROM cipl_employee WHERE employment_status='ACTIVE' GROUP BY 1 ORDER BY 2 DESC")
    roster = db.one("SELECT count(*) FILTER (WHERE employment_status='ACTIVE') AS active, max(snapshot_date) AS as_of FROM cipl_employee")
    kpi = {
        "engineers": roster["active"],
        "with_workload": sum(1 for r in rows if r["assets"] or r["calls_total"]),
        "open_calls": sum(r["calls_open"] for r in rows),
        "assets": sum(r["assets"] for r in rows),
        "pm_pending": sum(r["pm_pending"] for r in rows),
        "pm_pct": round(100.0 * sum(r["pm_done"] for r in rows) / max(1, sum(r["pm_done"] + r["pm_pending"] for r in rows)), 1),
        "onboarding_open": sum(1 for r in rows if r["onboarding_status"] and r["onboarding_status"] != "COMPLETE"),
        "as_of": roster["as_of"],
    }
    return {"kpi": kpi, "rows": rows, "onboarding": onboarding}


def detail(key, admin=False):
    personal = ", r.mobile_no, r.personal_email" if admin else ""
    e = db.one(f"""SELECT e.engineer_key AS id, e.display_name, e.ecode, e.source, r.designation, r.level, r.employment_status, r.gender, r.company_email{personal},
                         r.onboarding_status, r.onboarding_pending_items, r.date_of_joining_ongc, r.skill_category, r.deployed_at
                  FROM portal_engineer e LEFT JOIN cipl_employee r ON r.ecode = e.ecode WHERE e.engineer_key = %s""", [key])
    if not e:
        return None
    e["open_calls"] = db.query("SELECT sr_id AS id, cipl_call_date, asset_key, problem_description, priority, ageing_days, spare_status FROM svc_call WHERE is_current=1 AND engineer=%s AND call_status='OPEN' ORDER BY ageing_days DESC NULLS LAST", [key])
    e["recent_closed"] = db.query("SELECT sr_id AS id, cipl_call_date, closed_date, asset_key, problem_description, tat_days FROM svc_call WHERE is_current=1 AND engineer=%s AND call_status='CLOSED' ORDER BY closed_date DESC NULLS LAST LIMIT 8", [key])
    e["assets_by_class"] = db.query("SELECT asset_class AS label, count(*) AS n, count(*) FILTER (WHERE pm_status='PENDING') AS pm_pending FROM asset WHERE is_current=1 AND record_level='ASSET' AND engineer_name=%s GROUP BY 1 ORDER BY 2 DESC", [key])
    e["checklist"] = db.query("SELECT item, status, item_value FROM cipl_onboarding WHERE ecode=%s ORDER BY item", [e["ecode"]]) if e["ecode"] else []
    return e
