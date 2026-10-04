"""Dataset registry: the ONLY place that defines which tables / columns / facets the API can expose.
Every identifier used in SQL comes from this file (never from the request), so the query builder is injection-proof by construction.
"""


def search_expr(cols):
    """Immutable lower-cased haystack. The same text is used for the trigram index (db/setup.py) and the queries."""
    return "lower(" + " || ' ' || ".join(f"coalesce({c}::text,'')" for c in cols) + ")"


# Registers a plain User account cannot open at all (not just row-scoped) - Calls, Inward, Outward and OEM RMA are administrator
# territory now; a User's own Call tracker dashboard (scoped to their calls) is the only window they get onto that data. Shared
# by queries.py (register access) and auth.py (edit/create/archive) - kept here, not in either, to avoid a circular import.
ADMIN_ONLY_DATASETS = {"calls", "inward", "outward", "rma"}


def col(key, label, kind="text", expr=None, sort=None, align=None, ref=None, ref_id=None):
    """ref: hover-card kind (asset | call | cpf | engineer) for this column; ref_id: SQL expression for the id when it differs from the shown value."""
    return {"key": key, "label": label, "kind": kind, "expr": expr or key, "sort": sort or expr or key, "align": align, "ref": ref, "ref_id": ref_id}


def facet(key, label, expr=None, kind="single", limit=30, order="count", labels=None, blank=True):
    return {"key": key, "label": label, "expr": expr or key, "kind": kind, "limit": limit, "order": order, "labels": labels or {}, "blank": blank}


_MONTH = lambda c: f"to_char({c}, 'YYYY-MM')"  # noqa: E731

ASSET_SEARCH = ["asset_key", "ci_no", "hostname", "serial_no", "ongc_asset_id", "ip_address", "make", "model", "asset_type", "asset_class", "user_name",
                "cpf_no", "location_code", "room", "engineer_name", "remarks", "asset_description"]
CALL_SEARCH = ["sr_id", "ongc_ticket_no", "asset_key", "user_name", "engineer", "problem_description", "part_required", "make", "model", "serial_no", "oem_rma_no"]
INWARD_SEARCH = ["inward_id", "sr_id", "asset_key", "part_description", "part_no", "bill_no", "courier_awb", "received_by"]
OUTWARD_SEARCH = ["outward_id", "sr_id", "asset_key", "part_description", "part_serial_no", "device_serial_no", "gatepass_no", "sent_location"]
RMA_SEARCH = ["rma_line_id", "rma_no", "vendor_case_id", "vendor", "device_model", "location", "device_serial_no", "asset_key", "fault_item",
              "faulty_part_serial", "replacement_part_serial", "dc_no", "gatepass_no", "call_sr_id"]

EMPLOYEE_SEARCH = ["cpf_no", "employee_name", "designation", "level", "discipline", "sub_discipline", "org_unit_name", "position_name", "location", "qualification"]
ENGINEER_SEARCH = ["engineer_key", "display_name", "ecode", "designation", "skill_category", "deployed_at", "company_email"]

DATASETS = {
    "assets": {
        "table": "asset", "pk": "asset_key", "base": "is_current = 1", "label": "Assets", "search": ASSET_SEARCH,
        "sort": ("asset_key", "asc"), "defaults": {"record_level": ["ASSET"]},
        "columns": [
            col("asset_key", "Asset (CI)", "mono", ref="asset"),
            col("asset_class", "Class", "text"),
            col("asset_type", "Type", "text"),
            col("make_model", "Make / model", "text", expr="trim(coalesce(make,'') || ' ' || coalesce(model,''))"),
            col("serial_no", "Serial", "mono"),
            col("user_name", "User", "name", ref="cpf", ref_id="cpf_no"),
            col("location_code", "Location", "text"),
            col("engineer_name", "Engineer", "name", ref="engineer"),
            col("asset_status", "Status", "badge"),
            col("cover_status", "Cover", "badge"),
            col("cover_expiry_date", "Cover ends", "date"),
            col("pm_status", "PM", "badge"),
            col("rate_component", "Rate component", "mono"),
            col("rate_value", "Rate value", "money", align="right"),
            col("last_verified", "Last verified", "date", expr="(SELECT max(v.verified_on) FROM asset_verification v WHERE v.asset_key = asset.asset_key)"),
        ],
        "facets": [
            facet("record_level", "Record", kind="radio", labels={"ASSET": "Assets", "COMPONENT": "Components"}),
            facet("asset_class", "Class"), facet("asset_type", "Type"), facet("asset_status", "Status"),
            facet("cover_status", "Cover"), facet("pm_status", "PM this quarter"),
            facet("verified", "Physical check", expr="CASE WHEN (SELECT max(v.verified_on) FROM asset_verification v WHERE v.asset_key = asset.asset_key) IS NULL THEN 'NEVER' WHEN (SELECT max(v.verified_on) FROM asset_verification v WHERE v.asset_key = asset.asset_key) < current_date - 365 THEN 'OVER_A_YEAR' ELSE 'WITHIN_A_YEAR' END",
                  labels={"NEVER": "Never checked", "OVER_A_YEAR": "Last checked over a year ago", "WITHIN_A_YEAR": "Checked in the last year"}, blank=False),
            facet("engineer_name", "Engineer"), facet("make", "Make", limit=20), facet("os_family", "OS family"),
            facet("user_hr_status", "User in HR master"), facet("location_code", "Location", limit=25),
            facet("dq_flags", "Data quality", expr="dq_flags", kind="flags"),
        ],
    },
    "calls": {
        "table": "svc_call", "pk": "sr_id", "base": "is_current = 1", "label": "Calls", "search": CALL_SEARCH,
        "sort": ("cipl_call_date", "desc"), "defaults": {},
        "columns": [
            col("sr_id", "SR ID", "mono", ref="call"),
            col("cipl_call_date", "Logged", "date"),
            col("asset_key", "Asset (CI)", "mono", ref="asset"),
            col("user_name", "User", "name", ref="cpf", ref_id="cpf_no"),
            col("problem_description", "Problem", "text"),
            col("engineer", "Engineer", "name", ref="engineer"),
            col("priority", "Priority", "badge"),
            col("call_status", "Status", "badge"),
            col("age_days", "Age / TAT (d)", "int", expr="coalesce(ageing_days, tat_days)", align="right"),
            col("spare_status", "Spare", "badge"),
        ],
        "facets": [
            facet("call_status", "Status", kind="radio"),
            facet("priority", "Priority"), facet("engineer", "Engineer"), facet("spare_status", "Spare"),
            facet("asset_class", "Asset class"),
            facet("age_bucket", "Age of open calls", order=["0-7 days", "8-14 days", "15-30 days", "31-60 days", "61+ days"],
                  expr="CASE WHEN call_status='OPEN' THEN CASE WHEN ageing_days<=7 THEN '0-7 days' WHEN ageing_days<=14 THEN '8-14 days' "
                       "WHEN ageing_days<=30 THEN '15-30 days' WHEN ageing_days<=60 THEN '31-60 days' ELSE '61+ days' END END", blank=False),
            facet("month", "Month logged", expr=_MONTH("cipl_call_date"), order="value_desc"),
            facet("dq_flags", "Data quality", expr="dq_flags", kind="flags"),
        ],
    },
    "inward": {
        "table": "spare_inward", "pk": "inward_id", "base": "is_current = 1", "label": "Inward", "search": INWARD_SEARCH,
        "sort": ("inward_date", "desc"), "defaults": {},
        "columns": [
            col("inward_id", "ID", "mono"), col("inward_date", "Date", "date"), col("sr_id", "SR ID", "mono", ref="call"), col("asset_key", "Asset (CI)", "mono", ref="asset"),
            col("part_description", "Part", "text"), col("bill_no", "Bill", "text"), col("courier_awb", "AWB", "mono"),
            col("received_by", "Received by", "name"), col("received_date", "Received", "date"), col("transit_days", "Transit (d)", "int", align="right"),
        ],
        "facets": [
            facet("receipt", "Receipt", kind="bool", expr="CASE WHEN received_date IS NULL THEN 'Pending' ELSE 'Received' END"),
            facet("month", "Month", expr=_MONTH("inward_date"), order="value_desc"),
            facet("received_by", "Received by"), facet("location", "Location"),
            facet("dq_flags", "Data quality", expr="dq_flags", kind="flags"),
        ],
    },
    "outward": {
        "table": "spare_outward", "pk": "outward_id", "base": "is_current = 1", "label": "Outward", "search": OUTWARD_SEARCH,
        "sort": ("outward_date", "desc"), "defaults": {},
        "columns": [
            col("outward_id", "ID", "mono"), col("outward_date", "Date", "date"), col("sr_id", "SR ID", "mono", ref="call"), col("asset_key", "Asset (CI)", "mono", ref="asset"),
            col("part_description", "Part", "text"), col("part_serial_no", "Part serial", "mono"), col("gatepass_no", "Gate pass", "mono"),
            col("sent_date", "Sent", "date"), col("sent_location", "Sent to", "text"),
        ],
        "facets": [
            facet("gatepass", "Gate pass", kind="bool", expr="CASE WHEN gatepass_no IS NULL THEN 'Missing' ELSE 'Recorded' END"),
            facet("dispatch", "Dispatch", kind="bool", expr="CASE WHEN sent_date IS NULL THEN 'Not sent' ELSE 'Sent' END"),
            facet("sent_location", "Sent to"), facet("month", "Month", expr=_MONTH("outward_date"), order="value_desc"),
            facet("dq_flags", "Data quality", expr="dq_flags", kind="flags"),
        ],
    },
    "pm": {
        "table": "asset", "pk": "asset_key", "base": "is_current = 1 AND record_level = 'ASSET' AND pm_status <> 'NOT_TRACKED'", "label": "PM worklist", "search": ASSET_SEARCH,
        "detail": "assets", "sort": ("asset_key", "asc"), "defaults": {"pm_status": ["PENDING"]},
        "columns": [
            col("asset_key", "Asset (CI)", "mono", ref="asset"), col("asset_class", "Class", "text"), col("make_model", "Make / model", "text", expr="trim(coalesce(make,'') || ' ' || coalesce(model,''))"),
            col("location_code", "Location", "text"), col("engineer_name", "Engineer", "name", ref="engineer"), col("user_name", "User", "name", ref="cpf", ref_id="cpf_no"),
            col("pm_status", "PM", "badge"), col("pm_date", "PM date", "date"), col("pm_done_by", "Done by", "name"), col("pm_signed_by", "Signed by", "name"), col("cover_status", "Cover", "badge"),
        ],
        "facets": [
            facet("pm_status", "PM this quarter", kind="radio"), facet("asset_class", "Class"), facet("engineer_name", "Engineer"), facet("location_code", "Location", limit=25),
            facet("pm_done_by", "Done by"), facet("cover_status", "Cover"), facet("asset_status", "Status"),
        ],
    },
    "employees": {
        "table": "employee", "pk": "cpf_no", "base": "record_status = 'ACTIVE'", "label": "Employees", "search": EMPLOYEE_SEARCH, "readonly": True,
        "sort": ("employee_name", "asc"), "defaults": {},
        "columns": [
            col("cpf_no", "CPF no.", "mono", ref="cpf"), col("employee_name", "Name", "name"), col("designation", "Designation", "text"), col("level", "Level", "text"),
            col("discipline", "Discipline", "text"), col("org_unit_name", "Section", "text"), col("location", "Location", "text"),
            col("date_of_join_ongc", "Joined ONGC", "date"), col("date_of_retirement", "Retires", "date"),
        ],
        "facets": [
            facet("designation", "Designation", limit=20), facet("level", "Level"), facet("discipline", "Discipline"), facet("org_unit_name", "Section", limit=20),
            facet("location", "Location"), facet("tech_non_tech", "Technical / non-technical"), facet("gender", "Gender", kind="gender", labels={"M": "Male", "F": "Female"}),
            facet("retire_year", "Retirement year", expr="to_char(date_of_retirement, 'YYYY')", order="value_desc"),
        ],
    },
    "engineers": {
        "table": "v_engineer", "pk": "engineer_key", "base": "true", "label": "Engineers", "search": ENGINEER_SEARCH, "readonly": True,
        "sort": ("display_name", "asc"), "defaults": {},
        "columns": [
            col("ecode", "ECODE", "mono", ref="engineer", ref_id="engineer_key"), col("display_name", "Name", "name", ref="engineer", ref_id="engineer_key"), col("designation", "Designation", "text"),
            col("employment_status", "Roster status", "badge"), col("skill_category", "Skill", "text"), col("deployed_at", "Deployed at", "text"),
            col("assets", "Assets", "int", align="right"), col("open_calls", "Open calls", "int", align="right"), col("onboarding_status", "Onboarding", "badge"),
        ],
        "facets": [
            facet("employment_status", "Roster status"), facet("source", "Mapping"), facet("skill_category", "Skill"), facet("deployed_at", "Deployed at"),
            facet("gender", "Gender", kind="gender", labels={"M": "Male", "F": "Female"}), facet("onboarding_status", "Onboarding"),
        ],
    },
    "rma": {
        "table": "oem_rma", "pk": "rma_line_id", "base": "is_current = 1", "label": "OEM RMA", "search": RMA_SEARCH,
        "sort": ("call_log_date", "desc"), "defaults": {},
        "columns": [
            col("rma_line_id", "Line", "mono"), col("rma_no", "RMA no.", "mono"), col("vendor", "Vendor", "text"), col("device_model", "Model", "text"),
            col("asset_key", "Asset (CI)", "mono", ref="asset"), col("fault_item", "Fault", "text"), col("fault_category", "Category", "text"),
            col("call_log_date", "Logged", "date"), col("replacement_received_date", "Replaced", "date"), col("faulty_return_date", "Returned", "date"),
            col("return_status", "Return", "badge"), col("days_to_replacement", "Days to replace", "int", align="right"),
        ],
        "facets": [
            facet("return_status", "Faulty part return", kind="radio"),
            facet("vendor", "Vendor"), facet("fault_category", "Fault category"), facet("device_model", "Model"),
            facet("year", "Year logged", expr="to_char(call_log_date, 'YYYY')", order="value_desc"),
            facet("link", "Asset link", kind="bool", expr="CASE WHEN asset_key IS NULL THEN 'Not linked' ELSE 'Linked' END"),
            facet("dq_flags", "Data quality", expr="dq_flags", kind="flags"),
        ],
    },
}

# columns hidden from the detail view (internal bookkeeping)
HIDDEN_DETAIL = {"is_current", "first_seen_date", "last_seen_date", "snapshot_date", "source_row", "source_sheet"}
# personal data that the portal never sends to the browser
NEVER_EXPOSE = {"user_mobile", "mobile_no", "personal_email", "date_of_birth",
                "bank_name", "bank_account_no", "bank_ifsc", "epfo_no", "esic_no"}     # hover cards show these to administrators only
