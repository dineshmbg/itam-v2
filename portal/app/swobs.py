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
