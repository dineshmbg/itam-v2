"""Software / OS observations per asset: what a report, the check script or an engineer saw on a machine, kept as an append-only history.
Sources never overwrite each other (conflicts are shown side by side for the engineer to judge). The inspection date is stamped by the database
at insert - no caller can supply it - and a trigger refuses UPDATE and DELETE, so history cannot be rewritten."""
import datetime as dt

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
