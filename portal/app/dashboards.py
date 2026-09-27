"""Aggregations for the Assets and Call-tracker dashboards. Every figure is computed live in SQL from the current (is_current = 1) rows.

`eng`: when given (a non-admin engineer's own `engineer_key`), every figure is scoped to that engineer's own assets/calls only -
the dashboard becomes "my assets" / "my calls" rather than the whole fleet. None (the default, used for an administrator) means unscoped.
"""
from . import db


def assets(eng=None):
    p = {"eng": eng}
    A = "asset WHERE is_current = 1" + (" AND engineer_name = %(eng)s" if eng else "")
    k = db.one(f"""
        SELECT count(*) FILTER (WHERE record_level='ASSET') AS assets,
               count(*) FILTER (WHERE record_level='COMPONENT') AS components,
               count(*) FILTER (WHERE record_level='ASSET' AND asset_status='IN_USE') AS in_use,
               count(*) FILTER (WHERE record_level='ASSET' AND asset_status IN ('IN_STORE','SURPLUS','NOT_IN_USE','NOT_ON_NETWORK','STANDBY')) AS idle,
               count(*) FILTER (WHERE record_level='ASSET' AND asset_status='REMOVED_FROM_AMC') AS removed,
               count(*) FILTER (WHERE cover_status='EXPIRED') AS cover_expired,
               count(*) FILTER (WHERE cover_status='EXPIRING_90D') AS cover_expiring,
               count(*) FILTER (WHERE cover_status='ACTIVE') AS cover_active,
               count(*) FILTER (WHERE pm_status='PENDING') AS pm_pending,
               count(*) FILTER (WHERE pm_status IN ('DONE','DONE_OUTSIDE_QUARTER')) AS pm_done,
               count(*) FILTER (WHERE record_level='ASSET' AND user_hr_status NOT IN ('ACTIVE')) AS users_inactive,
               count(*) FILTER (WHERE dq_flags IS NOT NULL) AS dq_rows,
               max(snapshot_date) AS as_of, max(pm_quarter) AS pm_quarter
        FROM {A}""", p)
    by_class = db.query(f"""
        SELECT asset_class AS label, count(*) AS total,
               count(*) FILTER (WHERE asset_status='IN_USE') AS in_use,
               count(*) FILTER (WHERE asset_status IN ('IN_STORE','SURPLUS','NOT_IN_USE','NOT_ON_NETWORK','STANDBY')) AS idle,
               count(*) FILTER (WHERE asset_status IN ('REMOVED_FROM_AMC','TRANSFERRED')) AS other
        FROM {A} AND record_level='ASSET' GROUP BY 1 ORDER BY 2 DESC""", p)
    status = db.query(f"SELECT asset_status AS label, count(*) AS n FROM {A} AND record_level='ASSET' GROUP BY 1 ORDER BY 2 DESC", p)
    cover = db.query(f"SELECT cover_status AS label, count(*) AS n FROM {A} GROUP BY 1 ORDER BY 2 DESC", p)
    pm = db.query(f"""
        SELECT asset_class AS label,
               count(*) FILTER (WHERE pm_status='DONE') AS done,
               count(*) FILTER (WHERE pm_status='DONE_OUTSIDE_QUARTER') AS stale,
               count(*) FILTER (WHERE pm_status='PENDING') AS pending
        FROM {A} AND record_level='ASSET' AND pm_status <> 'NOT_TRACKED' GROUP BY 1 ORDER BY 2 DESC""", p)
    loc = db.query(f"SELECT location_code AS label, count(*) AS n FROM {A} AND record_level='ASSET' AND location_code IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 10", p)
    osf = db.query(f"SELECT os_family AS label, count(*) AS n FROM {A} AND record_level='ASSET' AND os_family IS NOT NULL GROUP BY 1 ORDER BY 2 DESC", p)
    attention = db.query(f"""
        SELECT asset_key AS id, asset_class, trim(coalesce(make,'') || ' ' || coalesce(model,'')) AS model, user_name, cover_type, cover_expiry_date, cover_status
        FROM {A} AND cover_status IN ('EXPIRED','EXPIRING_90D') ORDER BY cover_expiry_date, asset_key LIMIT 10""", p)
    flags = db.query(f"""SELECT flag AS label, count(*) AS n FROM asset, regexp_split_to_table(dq_flags, '; ') AS flag
                        WHERE is_current=1{' AND engineer_name = %(eng)s' if eng else ''} AND dq_flags IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 8""", p)
    by_engineer = db.query(f"""SELECT coalesce(engineer_name, 'UNASSIGNED') AS label, count(*) AS total,
                                     count(*) FILTER (WHERE cover_status IN ('EXPIRED','EXPIRING_90D')) AS cover_risk, count(*) FILTER (WHERE pm_status = 'PENDING') AS pm_pending
                              FROM {A} AND record_level='ASSET' GROUP BY 1 ORDER BY 2 DESC""", p)
    by_make = db.query(f"SELECT coalesce(make,'UNKNOWN') AS label, count(*) AS n FROM {A} AND record_level='ASSET' GROUP BY 1 ORDER BY 2 DESC LIMIT 10", p)
    age = db.query(f"""SELECT coalesce(to_char(install_date, 'YYYY'), 'UNKNOWN') AS label, count(*) AS n FROM {A} AND record_level='ASSET' AND asset_class IN ('DESKTOP','LAPTOP','WORKSTATION','SERVER','PRINTER','SCANNER','SWITCH','ROUTER','UPS')
                       GROUP BY 1 ORDER BY 1""", p)
    expiry = db.query(f"""SELECT to_char(cover_expiry_date, 'YYYY-MM') AS label, count(*) AS n FROM {A} AND record_level='ASSET' AND cover_expiry_date BETWEEN current_date AND current_date + interval '12 months'
                          GROUP BY 1 ORDER BY 1""", p)
    cover_type = db.query(f"SELECT coalesce(cover_type,'NONE') AS label, count(*) AS n FROM {A} AND record_level='ASSET' GROUP BY 1 ORDER BY 2 DESC", p)
    hr = db.query(f"SELECT coalesce(user_hr_status,'NO USER') AS label, count(*) AS n FROM {A} AND record_level='ASSET' GROUP BY 1 ORDER BY 2 DESC", p)
    changes = db.query(f"""SELECT snapshot_date AS day, change_type AS label, count(*) AS n FROM asset_change_log WHERE change_type IN ('ADDED','REMOVED') AND snapshot_date >= current_date - 90
                          {"AND asset_key IN (SELECT asset_key FROM asset WHERE engineer_name = %(eng)s)" if eng else ""} GROUP BY 1, 2 ORDER BY 1 DESC, 2""", p)
    network = db.one(f"""SELECT count(*) FILTER (WHERE ip_address IS NOT NULL) AS with_ip, count(*) FILTER (WHERE ip_address IS NULL AND asset_class IN ('DESKTOP','WORKSTATION','SERVER','PRINTER','SWITCH','ROUTER')) AS missing_ip,
                                count(*) FILTER (WHERE asset_status = 'NOT_ON_NETWORK') AS off_network FROM {A} AND record_level='ASSET'""", p)
    return {"kpi": k, "by_class": by_class, "status": status, "cover": cover, "pm": pm, "locations": loc, "os": osf, "attention": attention, "flags": flags,
            "by_engineer": by_engineer, "by_make": by_make, "age": age, "expiry": expiry, "cover_type": cover_type, "hr": hr, "changes": changes, "network": network}


def calls(eng=None):
    p = {"eng": eng}
    C = "svc_call WHERE is_current=1" + (" AND engineer = %(eng)s" if eng else "")
    k = db.one(f"""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE call_status='OPEN') AS open,
               count(*) FILTER (WHERE call_status='OPEN' AND spare_status='PART_PENDING') AS awaiting_part,
               max(ageing_days) FILTER (WHERE call_status='OPEN') AS oldest_open,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY ageing_days) FILTER (WHERE call_status='OPEN') AS median_open_age,
               round(avg(tat_days) FILTER (WHERE call_status='CLOSED')::numeric, 1) AS avg_tat,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY tat_days) FILTER (WHERE call_status='CLOSED') AS median_tat,
               count(*) FILTER (WHERE date_trunc('month', cipl_call_date) = date_trunc('month', current_date)) AS this_month,
               count(*) FILTER (WHERE date_trunc('month', cipl_call_date) = date_trunc('month', current_date - interval '1 month')) AS last_month,
               count(*) FILTER (WHERE priority='P1') AS p1,
               max(snapshot_date) AS as_of
        FROM {C}""", p)
    repeat = db.query(f"SELECT asset_key AS id, asset_class, model, count(*) AS n FROM {C} AND asset_key IS NOT NULL GROUP BY 1,2,3 HAVING count(*) >= 3 ORDER BY n DESC, 1 LIMIT 8", p)
    k["repeat_assets"] = db.one(f"SELECT count(*) AS n FROM (SELECT asset_key FROM {C} AND asset_key IS NOT NULL GROUP BY 1 HAVING count(*) >= 3) x", p)["n"]
    monthly = db.query(f"""
        SELECT to_char(date_trunc('month', cipl_call_date), 'YYYY-MM') AS label, count(*) AS total,
               count(*) FILTER (WHERE call_status='CLOSED') AS closed, count(*) FILTER (WHERE call_status='OPEN') AS open,
               round(avg(tat_days)::numeric, 1) AS avg_tat
        FROM {C} AND cipl_call_date IS NOT NULL GROUP BY 1 ORDER BY 1""", p)
    ageing = db.query(f"""
        SELECT bucket AS label, count(*) AS n FROM (
          SELECT CASE WHEN ageing_days<=7 THEN '0-7 days' WHEN ageing_days<=14 THEN '8-14 days' WHEN ageing_days<=30 THEN '15-30 days'
                      WHEN ageing_days<=60 THEN '31-60 days' ELSE '61+ days' END AS bucket
          FROM {C} AND call_status='OPEN') x GROUP BY 1""", p)
    order = ["0-7 days", "8-14 days", "15-30 days", "31-60 days", "61+ days"]
    have = {r["label"]: r["n"] for r in ageing}
    ageing = [{"label": b, "n": have.get(b, 0)} for b in order]
    problems = db.query(f"""
        SELECT problem_description AS label, count(*) AS n FROM {C} AND problem_description IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 10""", p)
    by_class = db.query(f"SELECT coalesce(asset_class,'UNKNOWN') AS label, count(*) AS n FROM {C} GROUP BY 1 ORDER BY 2 DESC", p)
    priority = db.query(f"SELECT coalesce(priority,'-') AS label, count(*) AS n FROM {C} GROUP BY 1 ORDER BY 1", p)
    oldest = db.query(f"""
        SELECT sr_id AS id, cipl_call_date, asset_key, problem_description, engineer, ageing_days, spare_status
        FROM {C} AND call_status='OPEN' ORDER BY ageing_days DESC NULLS LAST, sr_id LIMIT 10""", p)
    inward_scope = f"WHERE is_current=1{' AND sr_id IN (SELECT sr_id FROM svc_call WHERE is_current=1 AND engineer = %(eng)s)' if eng else ''}"
    outward_scope = inward_scope
    spares = db.one(f"""
        SELECT (SELECT count(*) FROM spare_inward {inward_scope}) AS inward,
               (SELECT count(*) FROM spare_inward {inward_scope} AND received_date IS NULL) AS inward_pending,
               (SELECT round(avg(transit_days)::numeric, 1) FROM spare_inward {inward_scope} AND transit_days IS NOT NULL) AS avg_transit_days,
               (SELECT count(*) FROM spare_outward {outward_scope}) AS outward,
               (SELECT count(*) FROM spare_outward {outward_scope} AND gatepass_no IS NULL) AS outward_no_gatepass,
               (SELECT count(*) FROM spare_outward {outward_scope} AND sent_date IS NULL) AS outward_not_sent""", p)
    spares["top_parts"] = db.query(f"""SELECT part_description AS label, count(*) AS n FROM spare_inward {inward_scope} AND part_description IS NOT NULL
                                      GROUP BY 1 ORDER BY 2 DESC LIMIT 8""", p)
    rma_scope = f"WHERE is_current=1{' AND call_sr_id IN (SELECT sr_id FROM svc_call WHERE is_current=1 AND engineer = %(eng)s)' if eng else ''}"
    rma = db.one(f"""
        SELECT count(*) AS total, count(*) FILTER (WHERE return_status='PENDING') AS pending_return,
               count(*) FILTER (WHERE asset_key IS NULL) AS unlinked, round(avg(days_to_replacement)::numeric, 1) AS avg_days_to_replace
        FROM oem_rma {rma_scope}""", p)
    rma["by_year"] = db.query(f"SELECT to_char(call_log_date,'YYYY') AS label, count(*) AS n FROM oem_rma {rma_scope} AND call_log_date IS NOT NULL GROUP BY 1 ORDER BY 1", p)
    by_engineer = db.query(f"""SELECT coalesce(engineer, 'UNASSIGNED') AS label, count(*) AS total, count(*) FILTER (WHERE call_status='OPEN') AS open, count(*) FILTER (WHERE call_status='CLOSED') AS closed,
                                    round(avg(tat_days) FILTER (WHERE call_status='CLOSED')::numeric, 1) AS avg_tat, max(ageing_days) FILTER (WHERE call_status='OPEN') AS oldest
                             FROM {C} GROUP BY 1 ORDER BY 2 DESC""", p)
    tat_class = db.query(f"""SELECT coalesce(asset_class,'UNKNOWN') AS label, count(*) FILTER (WHERE call_status='CLOSED') AS closed, round(avg(tat_days) FILTER (WHERE call_status='CLOSED')::numeric, 1) AS avg_tat,
                                  count(*) FILTER (WHERE call_status='OPEN') AS open FROM {C} GROUP BY 1 ORDER BY 2 DESC""", p)
    sla = db.one(f"""SELECT count(*) FILTER (WHERE call_status='CLOSED') AS closed,
                           count(*) FILTER (WHERE call_status='CLOSED' AND tat_days <= 1) AS within_1d, count(*) FILTER (WHERE call_status='CLOSED' AND tat_days <= 3) AS within_3d,
                           count(*) FILTER (WHERE call_status='CLOSED' AND tat_days <= 7) AS within_7d FROM {C}""", p)
    eng_cond = " AND c.engineer = %(eng)s" if eng else ""
    daily = db.query(f"""SELECT d::date AS day, (SELECT count(*) FROM svc_call c WHERE c.is_current=1 AND c.cipl_call_date = d::date{eng_cond}) AS opened,
                               (SELECT count(*) FROM svc_call c WHERE c.is_current=1 AND c.closed_date = d::date{eng_cond}) AS closed
                        FROM generate_series(current_date - 29, current_date, interval '1 day') d ORDER BY 1""", p)
    by_site = db.query(f"SELECT coalesce(site,'UNKNOWN') AS label, count(*) AS n FROM {C} GROUP BY 1 ORDER BY 2 DESC LIMIT 10", p)
    logging_lag = db.one(f"SELECT round(avg(logging_lag_days)::numeric, 1) AS avg_lag, count(*) FILTER (WHERE logging_lag_days >= 5) AS slow FROM {C}", p)
    return {"kpi": k, "monthly": monthly, "ageing": ageing, "problems": problems, "by_class": by_class, "priority": priority, "oldest": oldest,
            "repeat": repeat, "spares": spares, "rma": rma, "by_engineer": by_engineer, "tat_class": tat_class, "sla": sla, "daily": daily, "by_site": by_site, "logging_lag": logging_lag}
