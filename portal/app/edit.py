"""Validated, audited editing of the registers.

Rules that keep the data trustworthy:
  * only whitelisted fields can be edited; every value is validated (type, allowed list, existence of referenced records, date order);
  * compare-and-set: the browser sends the values it saw, a changed record is refused with 409 (no silent overwrite of a colleague's edit);
  * one transaction per save, the row is locked (FOR UPDATE), derived columns are recomputed with tools/itam_rules.py - the same rules the
    loaders use - so an edited row is indistinguishable from a freshly loaded one;
  * every change is written to portal_audit (who, when, from where, old -> new, reason) in the same transaction;
  * each edited field is remembered in portal_lock so the next loader run does not overwrite it (see tools/itam_locks.py);
  * nothing is deleted: "archive" retires a record (is_current = 0) and can be undone.
"""
import contextlib
import datetime as dt
import decimal
import json
import re
import sys
from pathlib import Path

from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import itam_locks as L  # noqa: E402
import itam_rules as R  # noqa: E402

from . import db  # noqa: E402
from .datasets import DATASETS  # noqa: E402


class Invalid(Exception):
    """400 - the request is wrong. `fields` maps field -> message for inline display."""

    def __init__(self, message, fields=None):
        super().__init__(message)
        self.fields = fields or {}


class Conflict(Exception):
    """409 - the record changed since the browser loaded it (or a uniqueness rule)."""

    def __init__(self, message, current=None):
        super().__init__(message)
        self.current = current or {}


class NotFound(Exception):
    pass


# ---------------------------------------------------------------- field specifications
def F(label, kind="text", group="Details", **kw):
    return {"label": label, "kind": kind, "group": group, **kw}


STATUS_ASSET = ["IN_USE", "IN_STORE", "STANDBY", "NOT_IN_USE", "NOT_ON_NETWORK", "SURPLUS", "TRANSFERRED", "REMOVED_FROM_AMC"]
CLASSES = ["DESKTOP", "LAPTOP", "WORKSTATION", "SERVER", "ROUTER", "SWITCH", "PRINTER", "SCANNER", "UPS", "MEDIA_CONVERTER"]
OS_FAMILY = ["WINDOWS", "WINDOWS SERVER", "LINUX", "MACOS", "OTHER"]
# Operating system suggestions, per OS family (offline - no lookup, no internet call at runtime; researched once and hardcoded, since
# the production server has no internet access). "OTHER" has no suggestion list - it's always a free typed value.
OS_EDITIONS = {
    "WINDOWS": ["WINDOWS 11 HOME", "WINDOWS 11 PRO", "WINDOWS 11 PRO FOR WORKSTATIONS", "WINDOWS 11 ENTERPRISE", "WINDOWS 11 EDUCATION",
                "WINDOWS 10 HOME", "WINDOWS 10 PRO", "WINDOWS 10 ENTERPRISE", "WINDOWS 10 EDUCATION", "WINDOWS 10 LTSC", "WINDOWS 8.1", "WINDOWS 7"],
    "WINDOWS SERVER": ["WINDOWS SERVER 2025 STANDARD", "WINDOWS SERVER 2025 DATACENTER", "WINDOWS SERVER 2022 STANDARD", "WINDOWS SERVER 2022 DATACENTER",
                        "WINDOWS SERVER 2019 STANDARD", "WINDOWS SERVER 2019 DATACENTER", "WINDOWS SERVER 2016 STANDARD", "WINDOWS SERVER 2016 DATACENTER",
                        "WINDOWS SERVER 2012 R2 STANDARD", "WINDOWS SERVER 2012 R2 DATACENTER"],
    "LINUX": ["UBUNTU SERVER 24.04 LTS", "UBUNTU SERVER 22.04 LTS", "UBUNTU DESKTOP 24.04 LTS", "RED HAT ENTERPRISE LINUX 9", "RED HAT ENTERPRISE LINUX 8",
              "ROCKY LINUX 9", "ROCKY LINUX 8", "CENTOS STREAM 9", "CENTOS 7", "DEBIAN 12", "DEBIAN 11", "SUSE LINUX ENTERPRISE SERVER 15", "FEDORA"],
    "MACOS": ["MACOS SEQUOIA 15", "MACOS SONOMA 14", "MACOS VENTURA 13", "MACOS MONTEREY 12", "MACOS BIG SUR 11", "MACOS CATALINA 10.15"],
    "OTHER": [],
}
UNIFORM_SIZES = ["S", "M", "L", "XL", "XXL", "XXXL"]     # suggestions only - the field still accepts a typed inch/cm measurement instead

SPEC = {
    "assets": {
        "fields": {
            "asset_key": F("Asset (CI)", upper=True, max=40, group="Identity", create_only=True, required=True),
            "asset_class": F("Class", "enum", "Identity", values=CLASSES, create_only=True, required=True),
            "asset_type": F("Type", "suggest", "Identity", upper=True, max=60, create_only=True, required=True, lookup="asset_type", depends_on="asset_class"),
            "hostname": F("Hostname", upper=True, max=60, group="Identity"),
            "asset_description": F("Description", max=200, group="Identity"),
            "make": F("Make", "suggest", "Hardware", upper=True, max=60, lookup="make"),
            "model": F("Model", "suggest", "Hardware", upper=True, max=80, lookup="model", depends_on="make"),
            "serial_no": F("Serial", upper=True, max=60, group="Hardware"),
            "ongc_asset_id": F("ONGC asset ID", upper=True, max=40, group="Hardware"),
            "ongc_census_no": F("ONGC census no.", upper=True, max=40, group="Hardware"),
            "os_family": F("OS family", "enum", "Hardware", values=OS_FAMILY),
            "os": F("Operating system", "suggest", "Hardware", upper=True, max=80, values_by=OS_EDITIONS, depends_on="os_family"),
            "firmware_version": F("Firmware", max=60, group="Hardware"),
            "ip_address": F("IP address", "ip", "Hardware"),
            "mgmt_ip": F("Management IP", "ip", "Hardware"),
            "install_date": F("Installed on", "date", "Hardware"),
            "sub_type": F("Sub type", upper=True, max=60, group="Hardware details"),
            "processor": F("Processor", upper=True, max=80, group="Hardware details"),
            "ram": F("RAM", upper=True, max=40, group="Hardware details"),
            "storage": F("Storage", upper=True, max=60, group="Hardware details"),
            "attached_monitor_model": F("Attached monitor model", upper=True, max=80, group="Hardware details"),
            "attached_monitor_sn": F("Attached monitor serial", upper=True, max=60, group="Hardware details"),
            "ports": F("Ports", upper=True, max=60, group="Hardware details"),
            "network_port_available": F("Network ports available", upper=True, max=40, group="Hardware details"),
            "capacity": F("Capacity", upper=True, max=60, group="Hardware details"),
            "battery_qty": F("Battery quantity", upper=True, max=20, group="Hardware details"),
            "battery_spec": F("Battery spec", upper=True, max=80, group="Hardware details"),
            "cpf_no": F("User CPF no.", "cpf", "Ownership"),
            "engineer_name": F("Engineer", "engineer", "Location and care"),
            "location_code": F("Location", upper=True, max=40, group="Location and care"),
            "floor_area": F("Floor / area", max=60, group="Location and care"),
            "room": F("Room", max=60, group="Location and care"),
            "cover_type": F("Cover type", "enum", "Contract", values=["AMC", "WRTY"]),
            "cover_expiry_date": F("Cover ends", "date", "Contract"),
            "rate_component": F("Rate component", "suggest", "Contract", upper=True, max=20),
            "rate_value": F("Rate value", "number", "Contract", max=99999999.99),
            "purchase_date": F("Purchased on", "date", "Lifecycle"),
            "purchase_cost": F("Purchase cost", "number", "Lifecycle", max=99999999.99),
            "vendor_name": F("Vendor", upper=True, max=80, group="Lifecycle"),
            "po_no": F("Purchase order no.", upper=True, max=40, group="Lifecycle"),
            "refresh_due_date": F("Refresh due on", "date", "Lifecycle", max_years=15),
            "pm_date": F("PM done on", "date", "Preventive maintenance"),
            "pm_done_by": F("PM done by", "engineer", "Preventive maintenance"),
            "pm_signed_by": F("PM signed by", max=80, group="Preventive maintenance"),
            "asset_status": F("Status", "enum", "Status", values=STATUS_ASSET, required=True),
            "remarks": F("Remarks", "longtext", "Status", max=500),
        },
        "table": "asset", "pk": "asset_key", "defaults": {"record_level": "ASSET", "asset_status": "IN_USE", "os_family": "WINDOWS", "os": "WINDOWS 11 PRO"},
        "block_fields": {"record_level"},
    },
    "calls": {
        "fields": {
            "sr_id": F("SR ID", upper=True, max=40, group="Call", create_only=True, required=True),
            "ongc_ticket_no": F("ONGC ticket no.", upper=True, max=40, group="Call"),
            "ongc_call_date": F("Logged by ONGC", "date", "Call"),
            "cipl_call_date": F("Logged by CIPL", "date", "Call", required=True),
            "asset_key": F("Asset (CI)", "asset", "Call", required=True, fill_from_asset=True),
            "cpf_no": F("User CPF no.", "cpf", "Call"),
            "zone": F("Zone", upper=True, max=60, group="Call"),
            "site": F("Site", upper=True, max=80, group="Call"),
            "site_incharge": F("Site in-charge", upper=True, max=80, group="Call"),
            "engineer": F("Engineer", "engineer", "Call", required=True),
            "priority": F("Priority", "enum", "Call", values=["P1", "P2", "P3"], required=True),
            "problem_description": F("Problem", "longtext", "Call", max=500, required=True),
            "call_status": F("Status", "enum", "Resolution", values=["OPEN", "CLOSED"], required=True),
            "closed_date": F("Closed on", "date", "Resolution"),
            "spare_required": F("Spare required", "longtext", "Spare", max=300),
            "part_required": F("Part no. required", "suggest", "Spare", upper=True, max=80, lookup="part_no", depends_on="asset_key"),
            "spare_issue_note": F("Spare note", "longtext", "Spare", max=300),
            "faulty_spare_sent_date": F("Faulty spare sent on", "date", "Spare"),
            "oem_rma_no": F("OEM RMA no.", "rma_no", "Spare"),
        },
        "table": "svc_call", "pk": "sr_id", "defaults": {"call_status": "OPEN", "priority": "P2"},
    },
    "inward": {
        "fields": {
            "inward_date": F("Inward date", "date", "Part", required=True),
            "sr_id": F("SR ID", "call", "Part", required=True),
            "asset_key": F("Asset (CI)", "asset", "Part"),
            "part_description": F("Part", "longtext", "Part", max=200, required=True),
            "part_no": F("Part no.", "suggest", "Part", upper=True, max=80, lookup="part_no", depends_on="asset_key"),
            "bill_no": F("Bill no.", upper=True, max=60, group="Receipt"),
            "courier_awb": F("Courier AWB", upper=True, max=60, group="Receipt"),
            "location": F("Location", upper=True, max=80, group="Receipt"),
            "received_by": F("Received by", upper=True, max=80, group="Receipt"),
            "received_date": F("Received on", "date", "Receipt"),
            "remarks": F("Remarks", "longtext", "Receipt", max=500),
        },
        "table": "spare_inward", "pk": "inward_id", "defaults": {"location": "ONGC ANKLESHWAR"}, "id_prefix": "IN",
    },
    "outward": {
        "fields": {
            "outward_date": F("Outward date", "date", "Part", required=True),
            "sr_id": F("SR ID", "call", "Part", required=True),
            "asset_key": F("Asset (CI)", "asset", "Part"),
            "device_serial_no": F("Device serial", upper=True, max=60, group="Part"),
            "part_description": F("Part", "longtext", "Part", max=200, required=True),
            "part_serial_no": F("Part serial", upper=True, max=60, group="Part"),
            "gatepass_no": F("Gate pass no.", upper=True, max=60, group="Dispatch"),
            "sent_date": F("Sent on", "date", "Dispatch"),
            "sent_location": F("Sent to", upper=True, max=80, group="Dispatch"),
            "courier": F("Courier", upper=True, max=80, group="Dispatch"),
            "location": F("Location", upper=True, max=80, group="Dispatch"),
            "remarks": F("Remarks", "longtext", "Dispatch", max=500),
        },
        "table": "spare_outward", "pk": "outward_id", "defaults": {}, "id_prefix": "OUT",
    },
    "rma": {
        "fields": {
            "rma_no": F("RMA no.", upper=True, max=60, group="Case"),
            "vendor_case_id": F("Vendor case ID", upper=True, max=60, group="Case"),
            "vendor": F("Vendor", "enum", "Case", values=["JUNIPER", "CISCO"], required=True),
            "device_model": F("Device model", upper=True, max=80, group="Device", required=True),
            "device_serial_no": F("Device serial", upper=True, max=60, group="Device"),
            "asset_key": F("Asset (CI)", "asset", "Device"),
            "location": F("Location", upper=True, max=80, group="Device"),
            "call_sr_id": F("Linked call (SR ID)", "call", "Case"),
            "fault_item": F("Fault", "longtext", "Fault", max=200, required=True),
            "fault_category": F("Fault category", "enum", "Fault", values=["WHOLE_UNIT", "PSU", "PORT", "POWER_ON", "BOOT", "MODULE", "OPTIC", "SOFTWARE", "OTHER"]),
            "faulty_part_serial": F("Faulty part serial", upper=True, max=60, group="Fault"),
            "call_log_date": F("Logged on", "date", "Timeline", required=True),
            "replacement_received_date": F("Replacement received on", "date", "Timeline"),
            "replacement_part_serial": F("Replacement serial", upper=True, max=60, group="Timeline"),
            "faulty_return_date": F("Faulty part returned on", "date", "Timeline"),
            "dc_no": F("Delivery challan no.", upper=True, max=60, group="Timeline"),
            "gatepass_no": F("Gate pass no.", upper=True, max=60, group="Timeline"),
            "remarks": F("Remarks", "longtext", "Timeline", max=500),
        },
        "table": "oem_rma", "pk": "rma_line_id", "defaults": {}, "id_prefix": "RMA",
    },
    "engineers": {
        "fields": {
            "employee_name": F("Full name", "text", "Identity", upper=True, max=120, create_only=True, required=True),
            "gender": F("Gender", "enum", "Identity", values=["M", "F"]),
            "mobile_no": F("Mobile number", "text", "Contact", max=20),
            "company_email": F("Company e-mail", "email", "Contact", max=120),
            "personal_email": F("Personal e-mail", "email", "Contact", max=120),
            "designation": F("Designation", "text", "Role", upper=True, max=80),
            "level": F("Level", "text", "Role", upper=True, max=20),
            "skill_category": F("Skill category", "text", "Role", upper=True, max=80),
            "deployed_at": F("Deployed at", "text", "Role", upper=True, max=80),
            "duty_pattern": F("Duty pattern", "text", "Role", upper=True, max=40),
            "education": F("Education", "text", "Background", upper=True, max=120),
            "certifications": F("Certifications", "longtext", "Background", max=300),
            "bank_name": F("Bank name", "text", "Banking", upper=True, max=120),
            "bank_account_no": F("Account number", "text", "Banking", upper=True, max=30),
            "bank_ifsc": F("IFSC code", "text", "Banking", upper=True, max=11),
            "epfo_no": F("EPFO / UAN number", "text", "Statutory", upper=True, max=30),
            "esic_no": F("ESIC number", "text", "Statutory", upper=True, max=30),
            "uniform_shirt_size": F("Shirt size", "suggest", "Uniform", upper=True, max=20, values=UNIFORM_SIZES),
            "uniform_trouser_size": F("Trouser size", "suggest", "Uniform", upper=True, max=20, values=UNIFORM_SIZES),
            "uniform_jacket_size": F("Jacket size", "suggest", "Uniform", upper=True, max=20, values=UNIFORM_SIZES),
            "remarks": F("Remarks", "longtext", "Notes", max=500),
        },
        # register identity (engineer_key) differs from the underlying table's key (ecode) - see _resolve_key(); the roster's own
        # lifecycle fields (ECODE, employment status) are not here: employment status changes go through a roster event, not a generic
        # edit. employee_name IS here (create_only) - an admin-created engineer (see create_engineer()) needs to set it once; a
        # roster-sourced engineer's name still only ever changes via the next roster sync, never through this form (the field simply
        # never appears once create_only fields are filtered out of the edit view).
        "table": "cipl_employee", "pk": "ecode", "defaults": {}, "active_col": "is_on_roster",
    },
}

DERIVED_COLS = {
    "assets": ["cover_status", "pm_status", "user_name", "user_designation", "user_level", "user_mobile", "user_retirement_date", "user_hr_status", "dq_flags"],
    "calls": ["tat_days", "ageing_days", "logging_lag_days", "faulty_spare_status", "spare_status", "dq_flags"],
    "inward": ["transit_days", "dq_flags"], "outward": ["dq_flags"],
    "rma": ["return_status", "days_to_return", "days_to_replacement", "dq_flags"],
}
# Columns no importer or loader ever writes - portal edits to them need no manual-override lock.
PORTAL_ONLY = {"assets": {"purchase_date", "purchase_cost", "vendor_name", "po_no", "refresh_due_date"},
               "engineers": {"bank_name", "bank_account_no", "bank_ifsc", "epfo_no", "esic_no", "uniform_shirt_size", "uniform_trouser_size", "uniform_jacket_size"}}
ASSET_COPY = ["asset_class", "asset_type", "make", "model", "serial_no"]      # copied onto a call from its asset
TODAY = lambda: dt.date.today()  # noqa: E731


def _team_lead_si_name():
    """The active engineer whose designation is Team Leader/SI - used as the default Site in-charge / Received by, since that
    role is who is actually meant on those forms, not the literal designation text."""
    r = db.one("""SELECT employee_name FROM cipl_employee WHERE employment_status = 'ACTIVE'
                 AND designation ILIKE '%team lead%' AND designation ILIKE '%si%' ORDER BY employee_name LIMIT 1""")
    return r["employee_name"] if r else None


def schema():
    """What the browser needs to build forms. Engineer list is added by the caller."""
    out = {}
    team_lead_si = _team_lead_si_name()
    for name, sp in SPEC.items():
        defaults = dict(sp["defaults"])          # a copy - SPEC's own dict must never be mutated, it is shared across every request
        if name == "calls":
            defaults.setdefault("cipl_call_date", dt.date.today().isoformat())
            defaults.setdefault("zone", "WEST")
            defaults.setdefault("site", "ONGC ANKLESHWAR")
            if team_lead_si:
                defaults.setdefault("site_incharge", team_lead_si)
        elif name == "inward":
            defaults.setdefault("inward_date", dt.date.today().isoformat())
            defaults.setdefault("received_date", dt.date.today().isoformat())
            if team_lead_si:
                defaults.setdefault("received_by", team_lead_si)
        elif name == "outward":
            defaults.setdefault("outward_date", dt.date.today().isoformat())
            defaults.setdefault("sent_date", dt.date.today().isoformat())
            defaults.setdefault("sent_location", "CIPL WARE HOUSE")
            defaults.setdefault("location", "NOIDA")
        out[name] = {"pk": sp["pk"], "id_auto": bool(sp.get("id_prefix")), "defaults": defaults, "label": DATASETS[name]["label"],
                     "fields": [{"key": k, **{a: b for a, b in v.items() if a in ("label", "kind", "group", "values", "max", "required", "create_only", "upper", "lookup", "depends_on", "fill_from_asset")}} for k, v in sp["fields"].items()]}
    return out


# ---------------------------------------------------------------- validation
def _rows(con, sql, params=()):
    with con.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _exists(con, sql, params):
    with con.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone() is not None


def clean(con, dataset, field, raw):
    """-> normalised value (None for blank). Raises ValueError with a user-readable message."""
    spec = SPEC[dataset]["fields"][field]
    kind = spec["kind"]
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if kind == "cpf":
        s = str(raw).strip()
        if not re.fullmatch(r"\d{4,8}", s):
            raise ValueError("CPF number must be 4 to 8 digits")
        v = int(s)
        if not _exists(con, "SELECT 1 FROM employee WHERE cpf_no = %s", (v,)):
            raise ValueError("this CPF number is not in the HR master")
        return v
    if not isinstance(raw, (str, int, float)):
        raise ValueError("invalid value")
    s = re.sub(r"\s+", " ", str(raw).strip()) if kind != "longtext" else str(raw).strip()
    if spec.get("upper") or kind in ("text", "longtext", "suggest"):        # text rule: everything is stored in upper case
        s = s.upper()
    if kind in ("text", "longtext", "suggest"):
        if len(s) > spec.get("max", 200):
            raise ValueError(f"at most {spec.get('max', 200)} characters")
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", s):
            raise ValueError("contains control characters")
        return s
    if kind == "enum":
        s = s.upper()
        if s not in spec["values"]:
            raise ValueError("choose one of: " + ", ".join(spec["values"]))
        return s
    if kind == "date":
        try:
            d = dt.date.fromisoformat(s[:10])
        except ValueError:
            raise ValueError("enter a valid date (YYYY-MM-DD)") from None
        if not (dt.date(2000, 1, 1) <= d <= TODAY() + dt.timedelta(days=366 * spec.get("max_years", 1))):
            raise ValueError("date is outside 2000-01-01 .. " + (f"{spec['max_years']} years from today" if spec.get("max_years") else "one year from today"))
        return d
    if kind == "number":
        try:
            d = decimal.Decimal(s.replace(",", ""))
        except decimal.InvalidOperation:
            raise ValueError("enter a number, e.g. 295.50") from None
        if not d.is_finite() or d < 0 or d > decimal.Decimal(str(spec.get("max", 99999999.99))):
            raise ValueError("enter a positive amount")
        return d.quantize(decimal.Decimal("0.01"))
    if kind == "ip":
        if not R.IPV4.match(s):
            raise ValueError("enter a valid IPv4 address such as 10.1.2.3")
        return s
    if kind == "email":
        s = s.lower()          # e-mail addresses are exempt from the upper-case text rule, same as everywhere else in the portal
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s):
            raise ValueError("enter a valid e-mail address")
        return s
    if kind == "engineer":
        s = s.upper()
        if not _exists(con, "SELECT 1 FROM portal_engineer WHERE engineer_key = %s", (s,)):
            raise ValueError("not a known engineer - pick one from the list")
        return s
    if kind == "asset":
        s = s.upper()
        if not _exists(con, "SELECT 1 FROM asset WHERE asset_key = %s AND is_current = 1", (s,)):
            raise ValueError("no such asset (CI) in the register")
        return s
    if kind == "call":
        s = s.upper()
        if not _exists(con, "SELECT 1 FROM svc_call WHERE sr_id = %s AND is_current = 1", (s,)):
            raise ValueError("no such call (SR ID) in the tracker")
        return s
    if kind == "rma_no":
        s = s.upper()
        if not _exists(con, "SELECT 1 FROM oem_rma WHERE rma_no = %s AND is_current = 1", (s,)):
            raise ValueError("no such OEM RMA number")
        return s
    raise ValueError("unsupported field")


def _clean_all(con, dataset, values, allow_create_only):
    fields = SPEC[dataset]["fields"]
    out, errors = {}, {}
    for f, raw in values.items():
        if f not in fields:
            errors[f] = "this field cannot be edited"
            continue
        if fields[f].get("create_only") and not allow_create_only:
            errors[f] = "cannot be changed after creation"
            continue
        try:
            out[f] = clean(con, dataset, f, raw)
        except ValueError as e:
            errors[f] = str(e)
    if errors:
        raise Invalid("Some values are not valid.", errors)
    return out


def _check_cross(dataset, row):
    """Field-relationship rules on the complete new row. Raises Invalid."""
    e = {}
    d = R.to_date
    if dataset == "calls":
        if not d(row.get("cipl_call_date")):
            e["cipl_call_date"] = "required"
        if row.get("call_status") == "CLOSED":
            if not d(row.get("closed_date")):
                e["closed_date"] = "required when the call is closed"
            elif d(row["closed_date"]) < d(row.get("cipl_call_date") or row["closed_date"]):
                e["closed_date"] = "cannot be before the date the call was logged"
        if d(row.get("cipl_call_date")) and d(row.get("ongc_call_date")) and d(row["cipl_call_date"]) < d(row["ongc_call_date"]):
            e["cipl_call_date"] = "cannot be before the date ONGC logged the call"
    elif dataset == "inward":
        if d(row.get("received_date")) and d(row.get("inward_date")) and d(row["received_date"]) < d(row["inward_date"]):
            e["received_date"] = "cannot be before the inward date"
    elif dataset == "outward":
        if d(row.get("sent_date")) and d(row.get("outward_date")) and d(row["sent_date"]) < d(row["outward_date"]):
            e["sent_date"] = "cannot be before the outward date"
    elif dataset == "rma":
        if d(row.get("replacement_received_date")) and d(row.get("call_log_date")) and d(row["replacement_received_date"]) < d(row["call_log_date"]):
            e["replacement_received_date"] = "cannot be before the date the case was logged"
        if d(row.get("faulty_return_date")) and d(row.get("replacement_received_date")) and d(row["faulty_return_date"]) < d(row["replacement_received_date"]):
            e["faulty_return_date"] = "cannot be before the replacement was received"
    if e:
        raise Invalid("Some values do not fit together.", e)


def _linked(con, dataset, row, changed):
    """Copies that must follow an edit (returned as extra column changes)."""
    extra = {}
    if dataset == "calls":
        if "asset_key" in changed:
            a = (_rows(con, "SELECT asset_class, asset_type, make, model, serial_no FROM asset WHERE asset_key = %s AND is_current = 1", (row["asset_key"],)) or [{}])[0]
            extra.update({c: a.get(c) for c in ASSET_COPY})
        if "cpf_no" in changed:
            extra["user_name"] = (_rows(con, "SELECT employee_name FROM employee WHERE cpf_no = %s", (row["cpf_no"],)) or [{}])[0].get("employee_name") if row.get("cpf_no") else None
        if changed.get("call_status") == "OPEN" and row.get("closed_date"):
            extra["closed_date"] = None
    if dataset == "rma" and "asset_key" in changed:
        extra["link_basis"] = "MANUAL" if row.get("asset_key") else None
    return extra


def _derive(con, dataset, row, changed_fields, as_of):
    ctx = L.build_ctx(con, dataset, row, resolve_user="cpf_no" in changed_fields, spare=True)
    return R.DERIVE[dataset](row, ctx, as_of)


# ---------------------------------------------------------------- helpers
def _jd(v):
    return L.jsonable(v)


def _eq(a, b):
    return _jd(a) == _jd(b)


def _audit(con, editor, ip, dataset, key, action, changes, reason):
    con.execute("INSERT INTO portal_audit (editor, client_ip, dataset, record_key, action, changes, reason) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)",
                (editor, ip, dataset, key, action, json.dumps(changes, default=str), reason))


def _lock(con, dataset, key, field, value, editor, source):
    con.execute("""INSERT INTO portal_lock (dataset, record_key, field, value, editor, source_value) VALUES (%s,%s,%s,%s::jsonb,%s,%s::jsonb)
                   ON CONFLICT (dataset, record_key, field) DO UPDATE SET value = EXCLUDED.value, editor = EXCLUDED.editor, edited_at = now()""",
                (dataset, key, field, json.dumps(_jd(value)), editor, json.dumps(_jd(source))))


def _resolve_key(con, dataset, key):
    """The engineers register is identified by engineer_key, but its underlying table (cipl_employee) is keyed by ecode."""
    if dataset != "engineers":
        return key
    r = _rows(con, "SELECT ecode FROM portal_engineer WHERE engineer_key = %s", (key,))
    if not r or not r[0]["ecode"]:
        raise NotFound("This engineer is not on the CIPL roster - there is no record to edit.")
    return r[0]["ecode"]


def _fetch(con, dataset, key, lock=True):
    sp = SPEC[dataset]
    real_key = _resolve_key(con, dataset, key)
    col = sp.get("active_col", "is_current")
    rows = _rows(con, f"SELECT * FROM {sp['table']} WHERE {sp['pk']} = %s AND {col} = 1" + (" FOR UPDATE" if lock else ""), (real_key,))
    if not rows:
        raise NotFound(f"{DATASETS[dataset]['label']} record '{key}' was not found (it may have been archived).")
    return rows[0]


def _update_row(con, dataset, key, cols):
    if not cols:
        return
    sp = SPEC[dataset]
    real_key = _resolve_key(con, dataset, key)
    names = list(cols)
    con.execute(f"UPDATE {sp['table']} SET " + ", ".join(f"{c} = %s" for c in names) + f" WHERE {sp['pk']} = %s", [cols[c] for c in names] + [real_key])


def _cascade_calls(con, sr_ids, editor, ip, as_of, reason):
    """Part receipt changed -> the call's spare status and flags follow."""
    for sr in {s for s in sr_ids if s}:
        rows = _rows(con, "SELECT * FROM svc_call WHERE sr_id = %s AND is_current = 1 FOR UPDATE", (sr,))
        if not rows:
            continue
        row = rows[0]
        new = _derive(con, "calls", row, set(), as_of)
        diff = {c: v for c, v in new.items() if not _eq(row.get(c), v)}
        if diff:
            _update_row(con, "calls", sr, diff)
            _audit(con, editor, ip, "calls", sr, "CASCADE", {c: {"old": _jd(row.get(c)), "new": _jd(v)} for c, v in diff.items()}, reason or "part receipt changed")


# ---------------------------------------------------------------- public operations
def update(dataset, key, changes, expected, editor, ip, reason=None, con=None):
    """con: run inside the caller's transaction (used by bulk operations such as recording PM for many assets)."""
    if dataset not in SPEC:
        raise Invalid("unknown register")
    if not isinstance(changes, dict) or not changes:
        raise Invalid("Nothing to save.")
    as_of = TODAY()
    with (contextlib.nullcontext(con) if con is not None else db.write()) as con:
        row = _fetch(con, dataset, key)
        norm = _clean_all(con, dataset, changes, allow_create_only=False)
        stale = {f: {"current": _jd(row.get(f))} for f in norm if f in (expected or {}) and not _eq(row.get(f), (expected[f] if expected[f] != "" else None))}
        if stale:
            raise Conflict("Someone changed this record after you opened it.", stale)
        actual = {f: v for f, v in norm.items() if not _eq(row.get(f), v)}
        if not actual:
            return {"changed": [], "message": "No changes."}
        new = {**row, **actual}
        _check_cross(dataset, new)
        extra = _linked(con, dataset, new, actual)
        new.update(extra)
        derived = _derive(con, dataset, new, set(actual), as_of)
        new.update(derived)
        write = {c: v for c, v in {**actual, **extra, **derived}.items() if not _eq(row.get(c), v)}
        _update_row(con, dataset, key, write)
        locked = {**actual, **{c: v for c, v in extra.items() if c in row}}
        for f, v in locked.items():
            if f not in PORTAL_ONLY.get(dataset, ()):      # a loader never writes these, so there is nothing for a lock to protect them from
                _lock(con, dataset, key, f, v, editor, row.get(f))
        _audit(con, editor, ip, dataset, key, "UPDATE", {f: {"old": _jd(row.get(f)), "new": _jd(v)} for f, v in locked.items()}, reason)
        if dataset in ("inward", "outward"):
            _cascade_calls(con, [row.get("sr_id"), new.get("sr_id")], editor, ip, as_of, f"{dataset} {key} changed")
        return {"changed": sorted(locked), "derived": sorted(c for c in write if c not in locked)}


def reassign_assets(keys, engineer, from_engineer, editor, ip, reason=None):
    """Assign many assets to one engineer in a single transaction (engineer=None/'' clears the assignment). `from_engineer`, when
    given, restricts the move to assets currently with that engineer - the "old engineer -> new engineer" hand-over. Each asset goes
    through update(), so it is validated, locked against re-import overwrite and audited exactly like a hand edit."""
    keys = [str(k).strip().upper() for k in dict.fromkeys(keys or []) if str(k).strip()][:1000]
    if not keys:
        raise Invalid("Select at least one asset.")
    engineer = (engineer or "").strip().upper() or None
    from_engineer = (from_engineer or "").strip().upper() or None
    if engineer and engineer == from_engineer:
        raise Invalid("The old and new engineer are the same.")
    moved, skipped = [], 0
    with db.write() as con:
        for k in keys:
            row = _rows(con, "SELECT engineer_name FROM asset WHERE asset_key = %s AND is_current = 1", (k,))
            if not row:
                raise NotFound(f"{k} is not in the asset register.")
            cur_eng = row[0]["engineer_name"]
            if (from_engineer and cur_eng != from_engineer) or cur_eng == engineer:
                skipped += 1
                continue
            update("assets", k, {"engineer_name": engineer}, {}, editor, ip, reason or "bulk engineer assignment", con=con)
            moved.append(k)
    return {"changed": len(moved), "skipped": skipped, "keys": moved}


def _next_id(con, dataset):
    sp = SPEC[dataset]
    pre, col, table = sp["id_prefix"], sp["pk"], sp["table"]
    con.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (table,))
    with con.cursor() as cur:
        cur.execute(f"SELECT coalesce(max(substring({col} from %s)::int), 0) FROM {table} WHERE {col} ~ %s", (rf"^{pre}-(\d+)$", rf"^{pre}-\d+$"))
        n = cur.fetchone()[0]
    return f"{pre}-{n + 1:04d}"


def create(dataset, values, editor, ip, reason=None):
    if dataset not in SPEC:
        raise Invalid("unknown register")
    sp = SPEC[dataset]
    as_of = TODAY()
    with db.write() as con:
        values = dict(values or {})
        pk_value = None
        if not sp.get("id_prefix"):
            raw = values.pop(sp["pk"], None)
            if raw in (None, ""):
                raise Invalid("Some required values are missing.", {sp["pk"]: "required"})
            pk_value = re.sub(r"\s+", "", str(raw)).upper()
            if not re.fullmatch(r"[A-Z0-9][A-Z0-9._/-]{1,39}", pk_value):
                raise Invalid("Some values are not valid.", {sp["pk"]: "use letters, digits and - . / _ (2 to 40 characters)"})
        norm = _clean_all(con, dataset, values, allow_create_only=True)
        row = {**sp["defaults"], **norm}
        missing = {f: "required" for f, s in sp["fields"].items() if s.get("required") and not row.get(f) and f != sp["pk"]}
        if missing:
            raise Invalid("Some required values are missing.", missing)
        _check_cross(dataset, row)
        if dataset == "assets":
            snap = _rows(con, "SELECT max(snapshot_date) AS d, (SELECT pm_quarter FROM asset WHERE pm_quarter IS NOT NULL LIMIT 1) AS q FROM asset")[0]
            row.update(pm_quarter=snap["q"], source_sheet="PORTAL")
            table_snap = snap["d"]
        else:
            table_snap = _rows(con, f"SELECT max(snapshot_date) AS d FROM {sp['table']}")[0]["d"]
        if not sp.get("id_prefix"):
            if _exists(con, f"SELECT 1 FROM {sp['table']} WHERE {sp['pk']} = %s", (pk_value,)):
                raise Conflict(f"{pk_value} already exists (it may be archived or retired).")
            key = pk_value
        else:
            key = _next_id(con, dataset)
        row[sp["pk"]] = key
        if dataset == "assets":
            row["record_level"] = "ASSET"
        row.update(_linked(con, dataset, row, {"asset_key": 1, "cpf_no": 1, **row}))
        row.update(_derive(con, dataset, row, set(norm) | {"cpf_no"}, as_of))
        row.update(snapshot_date=table_snap or as_of, is_current=1, first_seen_date=as_of, last_seen_date=as_of)
        cols = {c: v for c, v in row.items() if v is not None}
        con.execute(f"INSERT INTO {sp['table']} (" + ", ".join(cols) + ") VALUES (" + ", ".join(["%s"] * len(cols)) + ")", list(cols.values()))
        con.execute("INSERT INTO portal_lock (dataset, record_key, field, value, editor) VALUES (%s,%s,%s,'true'::jsonb,%s)", (dataset, key, L.CREATED, editor))
        _audit(con, editor, ip, dataset, key, "CREATE", {f: {"new": _jd(v)} for f, v in norm.items()}, reason)
        if dataset in ("inward", "outward"):
            _cascade_calls(con, [row.get("sr_id")], editor, ip, as_of, f"{dataset} {key} created")
        return {"id": key}


def _next_ecode(con):
    """Synthetic ECODE for an engineer added directly in the portal, not from the CIPL roster - PORTAL-0001, PORTAL-0002, ..."""
    con.execute("SELECT pg_advisory_xact_lock(hashtext('cipl_employee_portal_ecode'))")
    with con.cursor() as cur:
        cur.execute(r"SELECT coalesce(max(substring(ecode from 'PORTAL-(\d+)')::int), 0) FROM cipl_employee WHERE ecode ~ '^PORTAL-\d+$'")
        n = cur.fetchone()[0]
    return f"PORTAL-{n + 1:04d}"


def create_engineer(values, editor, ip, reason=None):
    """A new engineer added directly by an administrator - not sourced from the CIPL roster (auth.check_create restricts this to
    admins). Gets a synthetic ECODE (PORTAL-nnnn) so it fits the same cipl_employee/portal_engineer join every roster-sourced
    engineer uses, and is tagged source='PORTAL' so a later roster sync of the same real person is never auto-merged into it -
    that reconciliation, if it is ever needed, is a manual step."""
    with db.write() as con:
        norm = _clean_all(con, "engineers", dict(values or {}), allow_create_only=True)
        name = re.sub(r"\s+", " ", (norm.get("employee_name") or "").strip())
        if not name:
            raise Invalid("Enter the engineer's full name.", {"employee_name": "required"})
        engineer_key = name.upper()
        if _exists(con, "SELECT 1 FROM portal_engineer WHERE engineer_key = %s", (engineer_key,)):
            raise Conflict(f"An engineer named {engineer_key} already exists. Use a fuller name to tell them apart.")
        ecode = _next_ecode(con)
        as_of = TODAY()
        row = {**norm, "employee_name": name, "ecode": ecode, "employment_status": "ACTIVE", "onboarding_status": "NOT STARTED",
               "is_on_roster": 1, "first_seen_date": as_of, "last_seen_date": as_of, "snapshot_date": as_of}
        cols = {c: v for c, v in row.items() if v is not None}
        con.execute("INSERT INTO cipl_employee (" + ", ".join(cols) + ") VALUES (" + ", ".join(["%s"] * len(cols)) + ")", list(cols.values()))
        con.execute("INSERT INTO portal_engineer (engineer_key, display_name, ecode, source) VALUES (%s,%s,%s,'PORTAL')", (engineer_key, name, ecode))
        con.execute("INSERT INTO portal_lock (dataset, record_key, field, value, editor) VALUES ('engineers',%s,%s,'true'::jsonb,%s)", (engineer_key, L.CREATED, editor))
        _audit(con, editor, ip, "engineers", engineer_key, "CREATE", {f: {"new": _jd(v)} for f, v in norm.items()}, reason)
    return {"id": engineer_key}


VERIFY_RESULTS = ("FOUND", "NOT_FOUND")


def verify_asset(key, result, note, editor, ip):
    """Record a physical check of an asset (stocktake): who confirmed it, when, and whether it was found where the register says.
    Each check is its own row - the history is never overwritten."""
    result = str(result or "").strip().upper()
    if result not in VERIFY_RESULTS:
        raise Invalid("Choose whether the asset was found.", {"result": "required"})
    note = re.sub(r"\s+", " ", str(note or "").strip())[:300] or None
    with db.write() as con:
        if not _exists(con, "SELECT 1 FROM asset WHERE asset_key = %s AND is_current = 1", (key,)):
            raise NotFound(f"Asset '{key}' was not found (it may have been archived).")
        con.execute("INSERT INTO asset_verification (asset_key, verified_on, result, verified_by, note) VALUES (%s, %s, %s, %s, %s)", (key, TODAY(), result, editor, note))
        _audit(con, editor, ip, "assets", key, "VERIFY", {"result": {"old": None, "new": result}, **({"note": {"old": None, "new": note}} if note else {})}, None)
    return {"asset": key, "result": result, "verified_on": TODAY().isoformat()}


def _dependents(con, dataset, key):
    if dataset == "assets":
        q = [("calls", "SELECT count(*) FROM svc_call WHERE asset_key=%s AND is_current=1"), ("inward lines", "SELECT count(*) FROM spare_inward WHERE asset_key=%s AND is_current=1"),
             ("outward lines", "SELECT count(*) FROM spare_outward WHERE asset_key=%s AND is_current=1"), ("OEM RMA cases", "SELECT count(*) FROM oem_rma WHERE asset_key=%s AND is_current=1"),
             ("components", "SELECT count(*) FROM asset WHERE parent_asset_key=%s AND is_current=1")]
    elif dataset == "calls":
        q = [("inward lines", "SELECT count(*) FROM spare_inward WHERE sr_id=%s AND is_current=1"), ("outward lines", "SELECT count(*) FROM spare_outward WHERE sr_id=%s AND is_current=1"),
             ("OEM RMA cases", "SELECT count(*) FROM oem_rma WHERE call_sr_id=%s AND is_current=1")]
    else:
        return []
    out = []
    with con.cursor() as cur:
        for label, sql in q:
            cur.execute(sql, (key,))
            n = cur.fetchone()[0]
            if n:
                out.append(f"{n} {label}")
    return out


def archive(dataset, key, editor, ip, reason):
    if dataset not in SPEC:
        raise Invalid("unknown register")
    if not reason or len(reason.strip()) < 5:
        raise Invalid("Give a reason (at least 5 characters) for archiving.", {"reason": "required"})
    sp = SPEC[dataset]
    with db.write() as con:
        _fetch(con, dataset, key)
        dep = _dependents(con, dataset, key)
        if dep:
            raise Conflict("Cannot archive: still referenced by " + ", ".join(dep) + ". Archive or re-link those first.")
        con.execute(f"UPDATE {sp['table']} SET is_current = 0 WHERE {sp['pk']} = %s", (key,))
        con.execute("INSERT INTO portal_lock (dataset, record_key, field, value, editor) VALUES (%s,%s,%s,'true'::jsonb,%s) ON CONFLICT (dataset, record_key, field) DO UPDATE SET editor = EXCLUDED.editor, edited_at = now()",
                    (dataset, key, L.ARCHIVED, editor))
        _audit(con, editor, ip, dataset, key, "ARCHIVE", {}, reason.strip())
        if dataset in ("inward", "outward"):
            _cascade_calls(con, [_rows(con, f"SELECT sr_id FROM {sp['table']} WHERE {sp['pk']} = %s", (key,))[0]["sr_id"]], editor, ip, TODAY(), "record archived")
    return {"id": key}


def restore(dataset, key, editor, ip, reason=None):
    if dataset not in SPEC:
        raise Invalid("unknown register")
    sp = SPEC[dataset]
    with db.write() as con:
        rows = _rows(con, f"SELECT * FROM {sp['table']} WHERE {sp['pk']} = %s FOR UPDATE", (key,))
        if not rows or rows[0]["is_current"] == 1 or not _exists(con, "SELECT 1 FROM portal_lock WHERE dataset=%s AND record_key=%s AND field=%s", (dataset, key, L.ARCHIVED)):
            raise NotFound("This record is not in the archive.")
        if dataset == "assets" and rows[0].get("asset_status") == "REPLACED":
            raise Conflict("This machine was retired when it was replaced. Use Redeploy to put it back into service under a new name.")
        con.execute(f"UPDATE {sp['table']} SET is_current = 1 WHERE {sp['pk']} = %s", (key,))
        con.execute("DELETE FROM portal_lock WHERE dataset=%s AND record_key=%s AND field=%s", (dataset, key, L.ARCHIVED))
        _audit(con, editor, ip, dataset, key, "RESTORE", {}, reason)
        if dataset in ("inward", "outward"):
            _cascade_calls(con, [rows[0]["sr_id"]], editor, ip, TODAY(), "record restored")
    return {"id": key}


def reset(dataset, key, fields, editor, ip):
    """Drop manual overrides: the field returns to what the source file last said, and the next load may change it again."""
    if dataset not in SPEC:
        raise Invalid("unknown register")
    as_of = TODAY()
    with db.write() as con:
        row = _fetch(con, dataset, key)
        locks = _rows(con, "SELECT field, value, source_value FROM portal_lock WHERE dataset=%s AND record_key=%s AND field <> ALL(%s)", (dataset, key, [L.CREATED, L.ARCHIVED]))
        by = {r["field"]: r for r in locks}
        want = [f for f in (fields or by) if f in by]
        if not want:
            raise Invalid("There is no manual override to reset.")
        actual = {}
        for f in want:
            src = by[f]["source_value"]
            actual[f] = L.from_json(f, src)
        new = {**row, **actual}
        derived = _derive(con, dataset, new, set(actual), as_of)
        write = {c: v for c, v in {**actual, **derived}.items() if not _eq(row.get(c), v)}
        _update_row(con, dataset, key, write)
        con.execute("DELETE FROM portal_lock WHERE dataset=%s AND record_key=%s AND field = ANY(%s)", (dataset, key, want))
        _audit(con, editor, ip, dataset, key, "RESET", {f: {"old": _jd(row.get(f)), "new": _jd(actual[f])} for f in want}, "manual override removed")
        if dataset in ("inward", "outward"):
            _cascade_calls(con, [row.get("sr_id")], editor, ip, as_of, "override reset")
    return {"reset": want}


EXIT_TYPES = {"RESIGNED", "TERMINATED", "TRANSFERRED"}


def engineer_event(key, etype, date, reason, editor, ip):
    """Roster lifecycle event for an engineer - same effect as tools/cipl_roster.py event."""
    etype = (etype or "").upper()
    if etype not in EXIT_TYPES | {"REJOINED"}:
        raise Invalid("Choose Resigned, Terminated, Transferred or Rejoined.", {"type": "invalid"})
    try:
        d = dt.date.fromisoformat(str(date)[:10])
    except ValueError:
        raise Invalid("Enter a valid date.", {"date": "invalid"}) from None
    if not (dt.date(2000, 1, 1) <= d <= TODAY() + dt.timedelta(days=366)):
        raise Invalid("Date is out of range.", {"date": "out of range"})
    with db.write() as con:
        eng = _rows(con, "SELECT ecode FROM portal_engineer WHERE engineer_key = %s", (key,))
        if not eng or not eng[0]["ecode"]:
            raise NotFound("This engineer is not on the CIPL roster.")
        ec = eng[0]["ecode"]
        cur = _rows(con, "SELECT employment_status FROM cipl_employee WHERE ecode = %s FOR UPDATE", (ec,))
        if not cur:
            raise NotFound("Roster record not found.")
        old = cur[0]["employment_status"]
        if etype in EXIT_TYPES:
            if old != "ACTIVE":
                raise Conflict(f"Already {old.lower()}.")
            con.execute("UPDATE cipl_employee SET employment_status=%s, resignation_date=%s, last_working_date=%s, exit_reason=%s WHERE ecode=%s", (etype, d, d, reason, ec))
            new = etype
        else:
            if old == "ACTIVE":
                raise Conflict("Already active.")
            con.execute("UPDATE cipl_employee SET employment_status='ACTIVE', resignation_date=NULL, last_working_date=NULL, exit_reason=NULL WHERE ecode=%s", (ec,))
            new = "ACTIVE"
        con.execute("INSERT INTO cipl_event_log (snapshot_date,ecode,event_type,field,old_value,new_value,event_date,reason,source) VALUES (%s,%s,%s,'EMPLOYMENT_STATUS',%s,%s,%s,%s,'MANUAL')",
                    (TODAY(), ec, etype, old, new, d, reason))
        _audit(con, editor, ip, "engineers", key, "EVENT", {"employment_status": {"old": old, "new": new}, "date": d.isoformat()}, reason)
    return {"status": new}
