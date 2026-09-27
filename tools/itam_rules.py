"""Derived-field rules shared by the portal (edits) and the loaders (re-applying manual overrides).

Pure functions - no database access. Each `derive_*` takes the row as a dict (column -> value, lower-case keys), a small context dict with facts
that live in other tables, and the as-of date, and returns the derived columns that must be set. The rules mirror the converters
(tools/inventory_to_master.py, tools/call_tracking.py) so an edited row is indistinguishable from a freshly converted one.
"""
import datetime as dt
import re

MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
IPV4 = re.compile(r"^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$")

# Flags each dataset's rules own. Flags outside this list (cross-row checks such as DUPLICATE_SERIAL) are preserved untouched.
MANAGED_FLAGS = {
    "assets": {"COVER_EXPIRED", "COVER_EXPIRING_90D", "REMOVED_FROM_AMC_RATE_PRESENT", "PM_PENDING", "PM_OUTSIDE_QUARTER", "MISSING_ASSET_ID", "MISSING_SERIAL",
               "CPF_NOT_IN_HR_MASTER", "NO_CPF", "USER_RETIRED_OR_EXITED", "IP_NOT_STANDARD"},
    "calls": {"CLOSED_WITHOUT_DATE", "OPEN_WITH_CLOSED_DATE", "NEGATIVE_TAT", "MISSING_ONGC_TICKET", "PRIORITY_INVALID", "STATUS_INVALID", "CIPL_DATE_BEFORE_ONGC_DATE",
              "SLOW_LOGGING_5D_PLUS", "CLOSED_PART_NOT_RECEIVED", "OPEN_AWAITING_PART", "FAULTY_SPARE_DATE_IN_FUTURE", "ASSET_NOT_IN_MASTER"},
    "inward": {"NOT_RECEIVED_YET", "RECEIVED_BEFORE_DISPATCH", "SR_NOT_IN_CALLS"},
    "outward": {"NO_GATEPASS", "NOT_SENT_YET", "SR_NOT_IN_CALLS", "ASSET_NOT_IN_MASTER"},
    "rma": {"FAULTY_PART_NOT_RETURNED", "MISSING_RMA_NO", "RETURN_BEFORE_RECEIPT", "DEVICE_NOT_LINKED_TO_ASSET", "MISSING_CALL_DATE"},
}


NO_UPPER = {"COMPANY_EMAIL", "PERSONAL_EMAIL", "EMAIL"}      # e-mail addresses keep their case; all other text is stored in upper case


def upper_text(v):
    return v.upper() if isinstance(v, str) else v


def upper_record(rec):
    return {k: (v if str(k).upper() in NO_UPPER else upper_text(v)) for k, v in rec.items()}


def has(v):
    """A real value: not None, not empty text, not NaN (pandas hands NaN over for empty cells)."""
    return v is not None and v != "" and not (isinstance(v, float) and v != v)


def to_date(v):
    if not has(v):
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v)[:10])


def days(a, b):
    a, b = to_date(a), to_date(b)
    return (a - b).days if a and b else None


def quarter_window(label):
    """'Q2 JUL-SEP 2026' -> (2026-07-01, 2026-09-30)."""
    m = re.match(r"Q\d\s+([A-Z]{3})-([A-Z]{3})\s+(\d{4})", (label or "").upper())
    if not m or m[1] not in MONTHS or m[2] not in MONTHS:
        return None, None
    y, m1, m2 = int(m[3]), MONTHS[m[1]], MONTHS[m[2]]
    end = dt.date(y + (m2 == 12), (m2 % 12) + 1, 1) - dt.timedelta(days=1)
    return dt.date(y, m1, 1), end


def merge_flags(dataset, old, wanted):
    """Keep flags this module does not own, replace the ones it does. Returns 'A; B' or None."""
    owned = MANAGED_FLAGS[dataset]
    kept = [f for f in (old if isinstance(old, str) else "").split("; ") if f and f not in owned]
    return "; ".join(kept + sorted(wanted)) or None


# ---------------------------------------------------------------- assets
def cover_status(asset_status, expiry, as_of):
    if asset_status == "REMOVED_FROM_AMC":
        return "REMOVED"
    e = to_date(expiry)
    if e is None:
        return "UNKNOWN"
    if e < as_of:
        return "EXPIRED"
    if e <= as_of + dt.timedelta(days=90):
        return "EXPIRING_90D"
    return "ACTIVE"


def pm_status(asset_class, record_level, pm_date, pm_quarter):
    if asset_class == "LAPTOP":
        return "NOT_TRACKED"
    if record_level == "COMPONENT":
        return "NA"
    d = to_date(pm_date)
    if d is None:
        return "PENDING"
    start, end = quarter_window(pm_quarter)
    if start and end and not (start <= d <= end):
        return "DONE_OUTSIDE_QUARTER"
    return "DONE"


def derive_asset(row, ctx, as_of):
    out = {}
    out["cover_status"] = cover_status(row.get("asset_status"), row.get("cover_expiry_date"), as_of)
    out["pm_status"] = pm_status(row.get("asset_class"), row.get("record_level"), row.get("pm_date"), row.get("pm_quarter"))
    if ctx.get("resolve_user"):
        hr = ctx.get("hr")
        cpf = row.get("cpf_no")
        if hr:
            out.update(user_name=hr["employee_name"], user_designation=hr["designation"], user_level=hr["level"], user_mobile=hr["mobile_no"],
                       user_retirement_date=hr["date_of_retirement"], user_hr_status=hr["record_status"])
        else:
            out["user_hr_status"] = "NO_CPF" if cpf is None else "NOT_IN_HR_MASTER"
    merged = {**row, **out}
    flags = set()
    is_asset = merged.get("record_level") == "ASSET"
    if merged["cover_status"] == "EXPIRED":
        flags.add("COVER_EXPIRED")
    elif merged["cover_status"] == "EXPIRING_90D":
        flags.add("COVER_EXPIRING_90D")
    if merged.get("asset_status") == "REMOVED_FROM_AMC" and has(merged.get("rate_value")):
        flags.add("REMOVED_FROM_AMC_RATE_PRESENT")
    if merged["pm_status"] == "PENDING":
        flags.add("PM_PENDING")
    elif merged["pm_status"] == "DONE_OUTSIDE_QUARTER":
        flags.add("PM_OUTSIDE_QUARTER")
    flags_now = row.get("dq_flags")
    invalid_id_moved = "ASSET_ID_INVALID_MOVED" in (flags_now if isinstance(flags_now, str) else "")
    if is_asset and not has(merged.get("ongc_asset_id")) and not invalid_id_moved:
        flags.add("MISSING_ASSET_ID")
    if is_asset and not has(merged.get("serial_no")) and merged.get("asset_class") != "SERVER":
        flags.add("MISSING_SERIAL")
    st = merged.get("user_hr_status")
    if st == "NOT_IN_HR_MASTER":
        flags.add("CPF_NOT_IN_HR_MASTER")
    elif st == "NO_CPF":
        flags.add("NO_CPF")
    elif st in ("RETIRED", "EXITED"):
        flags.add("USER_RETIRED_OR_EXITED")
    ip = merged.get("ip_address")
    if has(ip) and not IPV4.match(str(ip)):
        flags.add("IP_NOT_STANDARD")
    out["dq_flags"] = merge_flags("assets", row.get("dq_flags"), flags)
    return out


# ---------------------------------------------------------------- calls
def derive_call(row, ctx, as_of):
    out = {}
    cd, od, closed = to_date(row.get("cipl_call_date")), to_date(row.get("ongc_call_date")), to_date(row.get("closed_date"))
    status = row.get("call_status")
    flags = set()
    out["tat_days"] = out["ageing_days"] = None
    if status == "CLOSED":
        if closed is None:
            flags.add("CLOSED_WITHOUT_DATE")
        else:
            t = days(closed, cd)
            if t is not None and t < 0:
                flags.add("NEGATIVE_TAT")
            else:
                out["tat_days"] = t
    elif status == "OPEN":
        if closed is not None:
            flags.add("OPEN_WITH_CLOSED_DATE")
        out["ageing_days"] = days(as_of, cd)
    else:
        flags.add("STATUS_INVALID")
    lag = days(cd, od)
    out["logging_lag_days"] = lag
    if lag is not None and lag < 0:
        flags.add("CIPL_DATE_BEFORE_ONGC_DATE")
    elif lag is not None and lag >= 5:
        flags.add("SLOW_LOGGING_5D_PLUS")
    if not row.get("ongc_ticket_no"):
        flags.add("MISSING_ONGC_TICKET")
    if row.get("priority") not in ("P1", "P2", "P3"):
        flags.add("PRIORITY_INVALID")
    fs = to_date(row.get("faulty_spare_sent_date"))
    if fs:
        out["faulty_spare_status"] = "SENT"
        if fs > as_of:
            flags.add("FAULTY_SPARE_DATE_IN_FUTURE")
    elif row.get("faulty_spare_status") == "SENT":
        out["faulty_spare_status"] = "NOT_SENT"
    if "inward_received" in ctx:
        if row.get("part_required"):
            out["spare_status"] = "PART_RECEIVED" if ctx["inward_received"] else "PART_PENDING"
        else:
            out["spare_status"] = "NO_SPARE_NEEDED"
    spare = out.get("spare_status", row.get("spare_status"))
    if row.get("part_required") and spare == "PART_PENDING":
        flags.add("CLOSED_PART_NOT_RECEIVED" if status == "CLOSED" else "OPEN_AWAITING_PART")
    if ctx.get("asset_known") is False:
        flags.add("ASSET_NOT_IN_MASTER")
    out["dq_flags"] = merge_flags("calls", row.get("dq_flags"), flags)
    return out


# ---------------------------------------------------------------- spares and RMA
def derive_inward(row, ctx, as_of):
    flags = set()
    out = {"transit_days": None}
    rec, sent = to_date(row.get("received_date")), to_date(row.get("inward_date"))
    if rec is None:
        flags.add("NOT_RECEIVED_YET")
    else:
        t = days(rec, sent)
        if t is not None and t < 0:
            flags.add("RECEIVED_BEFORE_DISPATCH")
        else:
            out["transit_days"] = t
    if ctx.get("call_known") is False:
        flags.add("SR_NOT_IN_CALLS")
    out["dq_flags"] = merge_flags("inward", row.get("dq_flags"), flags)
    return out


def derive_outward(row, ctx, as_of):
    flags = set()
    if not row.get("gatepass_no"):
        flags.add("NO_GATEPASS")
    if not row.get("sent_date"):
        flags.add("NOT_SENT_YET")
    if ctx.get("call_known") is False:
        flags.add("SR_NOT_IN_CALLS")
    if ctx.get("asset_known") is False:
        flags.add("ASSET_NOT_IN_MASTER")
    return {"dq_flags": merge_flags("outward", row.get("dq_flags"), flags)}


def derive_rma(row, ctx, as_of):
    flags = set()
    ret, recv, call = to_date(row.get("faulty_return_date")), to_date(row.get("replacement_received_date")), to_date(row.get("call_log_date"))
    out = {"return_status": "RETURNED" if ret else "PENDING", "days_to_return": None, "days_to_replacement": None}
    if not ret:
        flags.add("FAULTY_PART_NOT_RETURNED")
    if not row.get("rma_no"):
        flags.add("MISSING_RMA_NO")
    if not call:
        flags.add("MISSING_CALL_DATE")
    t2 = days(ret, recv)
    if t2 is not None and t2 < 0:
        flags.add("RETURN_BEFORE_RECEIPT")
    else:
        out["days_to_return"] = t2
    t = days(recv, call)
    out["days_to_replacement"] = t if t is not None and t >= 0 else None
    if not row.get("asset_key"):
        flags.add("DEVICE_NOT_LINKED_TO_ASSET")
    out["dq_flags"] = merge_flags("rma", row.get("dq_flags"), flags)
    return out


DERIVE = {"assets": derive_asset, "calls": derive_call, "inward": derive_inward, "outward": derive_outward, "rma": derive_rma,
          "engineers": lambda row, ctx, as_of: {}}      # cipl_employee has no computed/flag columns that a contact-detail edit needs to recompute
