"""One-time (idempotent, non-destructive) database preparation for the ITAM portal.

  python portal/db/setup.py

Creates only: the pg_trgm extension, trigram search indexes, plain lookup indexes, the NOTIFY trigger that makes the dashboards live,
the portal_engineer table (verified mapping between roster names and the names used in asset / call data), and the
editing tables portal_lock (manual overrides that survive loader runs) and portal_audit (who changed what, when).
Nothing is dropped and no inventory row is touched.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import itam_locks  # noqa: E402
from portal.app import auth, backup, db, importer, inventory_match, lifecycle, mailer, pm, reports, views_pref  # noqa: E402
from portal.app.datasets import ASSET_SEARCH, CALL_SEARCH, INWARD_SEARCH, OUTWARD_SEARCH, RMA_SEARCH, search_expr  # noqa: E402

# Roster name (cipl_employee.ecode) <-> working name used in asset / call data. Verified by hand against both name lists.
WORKING_NAME_TO_ECODE = {
    "DINESH GADARIA": "A003541", "SHAHRUKH KHAN": "A003542", "TITUS JOSEPH": "A003543", "RINKAL PARMAR": "A003545",
    "BHAVESH PATEL": "A003546", "HIREN PATEL": "A003547", "HITESH PARMAR": "A003991", "MINHAZ PATEL": "A004018",
    "VASUDEV CHITTE": "A004064", "RAVILAL": "A004542", "MAYANK PATEL": "A005832", "PARSAN LAWANGARE": "A005864",
    "PRIYESH PANCHAL": "A006476", "SAGAR JOSHI": "A006496",
}

DDL = [
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    f"CREATE INDEX IF NOT EXISTS ix_portal_asset_search ON asset USING gin ({search_expr(ASSET_SEARCH)} gin_trgm_ops)",
    f"CREATE INDEX IF NOT EXISTS ix_portal_call_search ON svc_call USING gin ({search_expr(CALL_SEARCH)} gin_trgm_ops)",
    f"CREATE INDEX IF NOT EXISTS ix_portal_inward_search ON spare_inward USING gin ({search_expr(INWARD_SEARCH)} gin_trgm_ops)",
    f"CREATE INDEX IF NOT EXISTS ix_portal_outward_search ON spare_outward USING gin ({search_expr(OUTWARD_SEARCH)} gin_trgm_ops)",
    f"CREATE INDEX IF NOT EXISTS ix_portal_rma_search ON oem_rma USING gin ({search_expr(RMA_SEARCH)} gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS ix_portal_asset_cur ON asset (is_current, record_level, asset_class)",
    "CREATE INDEX IF NOT EXISTS ix_portal_asset_eng ON asset (engineer_name) WHERE is_current = 1",
    "CREATE INDEX IF NOT EXISTS ix_portal_asset_parent ON asset (parent_asset_key) WHERE parent_asset_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_portal_call_cur ON svc_call (is_current, call_status)",
    "CREATE INDEX IF NOT EXISTS ix_portal_call_eng ON svc_call (engineer) WHERE is_current = 1",
    "CREATE INDEX IF NOT EXISTS ix_portal_inward_sr ON spare_inward (sr_id)",
    "CREATE INDEX IF NOT EXISTS ix_portal_outward_sr ON spare_outward (sr_id)",
    "CREATE INDEX IF NOT EXISTS ix_portal_inward_asset ON spare_inward (asset_key)",
    "CREATE INDEX IF NOT EXISTS ix_portal_outward_asset ON spare_outward (asset_key)",
    "CREATE INDEX IF NOT EXISTS ix_portal_rma_asset ON oem_rma (asset_key)",
    """CREATE TABLE IF NOT EXISTS portal_engineer (
         engineer_key TEXT PRIMARY KEY,          -- working name exactly as used in asset / call data (upper case)
         display_name TEXT NOT NULL,
         ecode        TEXT,                      -- CIPL roster code, when the person is on the roster
         source       TEXT NOT NULL,             -- MAPPED / ROSTER_ONLY / DATA_ONLY / PORTAL (added manually by an administrator)
         created_at   TIMESTAMPTZ DEFAULT now())""",
    # Portal-only engineer fields (2026-09-23): banking, EPFO, ESIC, uniform sizes. Never touched by the CIPL roster loader
    # (tools/cipl_roster.py names its own fixed column list) - once entered here, a value is permanent across every future roster sync.
    "ALTER TABLE cipl_employee ADD COLUMN IF NOT EXISTS bank_name TEXT",
    "ALTER TABLE cipl_employee ADD COLUMN IF NOT EXISTS bank_account_no TEXT",
    "ALTER TABLE cipl_employee ADD COLUMN IF NOT EXISTS bank_ifsc TEXT",
    "ALTER TABLE cipl_employee ADD COLUMN IF NOT EXISTS epfo_no TEXT",
    "ALTER TABLE cipl_employee ADD COLUMN IF NOT EXISTS esic_no TEXT",
    "ALTER TABLE cipl_employee ADD COLUMN IF NOT EXISTS uniform_shirt_size TEXT",
    "ALTER TABLE cipl_employee ADD COLUMN IF NOT EXISTS uniform_trouser_size TEXT",
    "ALTER TABLE cipl_employee ADD COLUMN IF NOT EXISTS uniform_jacket_size TEXT",
    # Asset lifecycle fields (2026-09-23): portal-only columns - the asset importer writes its own fixed template columns and never touches these.
    "ALTER TABLE asset ADD COLUMN IF NOT EXISTS purchase_date DATE",
    "ALTER TABLE asset ADD COLUMN IF NOT EXISTS purchase_cost NUMERIC(12,2)",
    "ALTER TABLE asset ADD COLUMN IF NOT EXISTS vendor_name TEXT",
    "ALTER TABLE asset ADD COLUMN IF NOT EXISTS po_no TEXT",
    "ALTER TABLE asset ADD COLUMN IF NOT EXISTS refresh_due_date DATE",
    # Physical verification (stocktake): one row per check, so the full history of who confirmed what, when, is kept.
    """CREATE TABLE IF NOT EXISTS asset_verification (
         verification_id BIGSERIAL PRIMARY KEY, asset_key TEXT NOT NULL, verified_on DATE NOT NULL DEFAULT current_date,
         result TEXT NOT NULL CHECK (result IN ('FOUND','NOT_FOUND')), verified_by TEXT NOT NULL, note TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS ix_asset_verification_key ON asset_verification (asset_key, verified_on DESC)",
    # OS family (2026-09-23): "WINDOWS 11"/"WINDOWS 10" merged into one "WINDOWS" family - the edition (11 vs 10 vs Pro/Home/...)
    # now lives in the Operating system field itself. Harmless to rerun.
    "UPDATE asset SET os_family = 'WINDOWS' WHERE os_family IN ('WINDOWS 11', 'WINDOWS 10')",
    """CREATE OR REPLACE VIEW v_engineer AS
       SELECT e.engineer_key, e.display_name, e.ecode, e.source, r.designation, r.level, r.gender, r.employment_status, r.onboarding_status, r.skill_category, r.deployed_at,
              r.date_of_joining_ongc, r.company_email, r.mobile_no, r.personal_email, r.date_of_birth, r.education, r.certifications, r.experience_years, r.location, r.duty_pattern,
              r.date_current_contract, r.resignation_date, r.last_working_date, r.exit_reason, r.remarks,
              (SELECT count(*) FROM asset a WHERE a.engineer_name = e.engineer_key AND a.is_current = 1 AND a.record_level = 'ASSET') AS assets,
              (SELECT count(*) FROM svc_call c WHERE c.engineer = e.engineer_key AND c.is_current = 1 AND c.call_status = 'OPEN') AS open_calls,
              (SELECT count(*) FROM svc_call c WHERE c.engineer = e.engineer_key AND c.is_current = 1) AS calls,
              r.bank_name, r.bank_account_no, r.bank_ifsc, r.epfo_no, r.esic_no, r.uniform_shirt_size, r.uniform_trouser_size, r.uniform_jacket_size
       FROM portal_engineer e LEFT JOIN cipl_employee r ON r.ecode = e.ecode""",
    """CREATE OR REPLACE FUNCTION portal_notify() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN PERFORM pg_notify('portal_changes', TG_TABLE_NAME); RETURN NULL; END $$""",
]
UPPER_TABLES = ["asset", "asset_snapshot", "svc_call", "spare_inward", "spare_outward", "oem_rma", "oem_serial_history", "employee", "employee_history",
                "cipl_employee", "cipl_employee_snapshot", "cipl_onboarding", "cipl_event_log", "portal_engineer", "asset_verification"]
UPPER_SKIP = ["company_email", "personal_email", "email"]      # e-mail addresses stay as typed
UPPER_FN = """CREATE OR REPLACE FUNCTION portal_uppercase() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE k text; v jsonb; o jsonb := '{}'::jsonb; skip text[] := TG_ARGV;
BEGIN
  FOR k, v IN SELECT * FROM jsonb_each(to_jsonb(NEW)) LOOP
    IF jsonb_typeof(v) = 'string' AND NOT (k = ANY(skip)) AND (v #>> '{}') <> upper(v #>> '{}') THEN
      o := o || jsonb_build_object(k, upper(v #>> '{}'));
    END IF;
  END LOOP;
  IF o <> '{}'::jsonb THEN NEW := jsonb_populate_record(NEW, o); END IF;
  RETURN NEW;
END $$"""
NOTIFY_TABLES = ["asset", "asset_verification", "svc_call", "spare_inward", "spare_outward", "oem_rma", "oem_serial_history", "employee", "cipl_employee", "cipl_onboarding", "cipl_event_log", "portal_engineer", "portal_lock", "portal_audit", "portal_activity", "pm_record", "pm_cycle", "portal_report"]


def title(name):
    return " ".join(w.capitalize() for w in name.split())


def main():
    with db.admin_connection() as con:
        itam_locks.ensure_tables(con)
        con.execute("CREATE INDEX IF NOT EXISTS ix_portal_audit_rec ON portal_audit (dataset, record_key, audit_id DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_portal_lock_rec ON portal_lock (dataset, record_key)")
        for stmt in DDL:
            con.execute(stmt)
        auth.ensure_tables(con)
        for stmt in backup.DDL + pm.DDL + mailer.DDL + importer.DDL + inventory_match.DDL + reports.DDL + views_pref.DDL + lifecycle.DDL:
            con.execute(stmt)
        pm.ensure_cycle(con, by="setup")
        con.execute(UPPER_FN)
        skip = ", ".join(f"'{c}'" for c in UPPER_SKIP)
        for t in UPPER_TABLES:       # every writer (portal, loaders, manual SQL) stores text in upper case
            con.execute(f"CREATE OR REPLACE TRIGGER portal_uppercase_{t} BEFORE INSERT OR UPDATE ON {t} FOR EACH ROW EXECUTE FUNCTION portal_uppercase({skip})")
        first = auth.bootstrap_admin(con)
        auth.ensure_demo_user(con)
        for t in NOTIFY_TABLES:
            con.execute(f"CREATE OR REPLACE TRIGGER portal_notify_{t} AFTER INSERT OR UPDATE OR DELETE ON {t} FOR EACH STATEMENT EXECUTE FUNCTION portal_notify()")
        roster = {r["ecode"]: r["employee_name"] for r in con.execute("SELECT ecode, employee_name FROM cipl_employee").fetchall()}
        data_names = {r["n"] for r in con.execute(
            "SELECT engineer_name n FROM asset WHERE is_current=1 AND engineer_name IS NOT NULL UNION SELECT pm_done_by FROM asset WHERE is_current=1 AND pm_done_by IS NOT NULL "
            "UNION SELECT engineer FROM svc_call WHERE is_current=1 AND engineer IS NOT NULL").fetchall()}
        rows, mapped = [], set()
        for wn, ec in WORKING_NAME_TO_ECODE.items():
            if ec not in roster:
                raise SystemExit(f"Roster code {ec} for {wn} not found in cipl_employee - fix WORKING_NAME_TO_ECODE.")
            rows.append((wn, title(wn), ec, "MAPPED"))
            mapped.add(ec)
        for ec, nm in roster.items():
            if ec not in mapped:
                rows.append((nm.upper(), nm, ec, "ROSTER_ONLY"))
        known = {r[0] for r in rows}
        for n in sorted(data_names - known):
            rows.append((n, title(n), None, "DATA_ONLY"))
        for r in rows:
            con.execute("INSERT INTO portal_engineer (engineer_key, display_name, ecode, source) VALUES (%s,%s,%s,%s) ON CONFLICT (engineer_key) DO NOTHING", r)
        n = con.execute("SELECT source, COUNT(*) c FROM portal_engineer GROUP BY 1 ORDER BY 1").fetchall()
        con.execute("ANALYZE asset"); con.execute("ANALYZE svc_call")
    print("Portal database preparation complete.")
    if first:
        note = Path.home() / ".itam_first_admin.txt"
        lines = ["ITAM Portal first administrator", f"User name: {first[0]}", f"Default password: {first[1]}", "You must change it at first sign-in. Delete this file afterwards."]
        note.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"  first administrator created: {first[0]} / {first[1]}  (also saved in {note} - delete it after the first sign-in)")
    print(f"  read-only demo login: {auth.DEMO_USERNAME} / {auth.DEMO_PASSWORD}  (fixed - never expires, never forces a password change)")
    print("  engineers:", {r["source"]: r["c"] for r in n})
    print("  triggers on:", ", ".join(NOTIFY_TABLES))


if __name__ == "__main__":
    main()
