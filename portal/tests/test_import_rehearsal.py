"""The import page's CHECK now rehearses the load (2026-10-03), so a load that would be refused is flagged before anyone presses Load.

Background: the 1 Oct call tracker passed the check and was only refused when Load was pressed ("30 of 113 rows missing >20%"). The loaders now
accept --dry-run (ITAM_DRY_RUN=1): same reads, same safety stops, same change counts, but nothing is ever committed.
"""
import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
try:
    import master_db
except ModuleNotFoundError:      # the loaders need pandas, which the portal's own venv does not ship: borrow the base interpreter's copy
    sys.path.append(str(Path(sys.base_prefix) / "Lib" / "site-packages"))
    import master_db

from portal.app import importer  # noqa: E402

PROBE = "zz_dryrun_probe"


def test_a_dry_run_connection_never_commits(monkeypatch):
    monkeypatch.setenv("ITAM_DRY_RUN", "1")
    con = master_db.connect()
    try:
        con.execute(f"CREATE TABLE {PROBE} (x int)")
        con.execute(f"INSERT INTO {PROBE} VALUES (1)")
        con.commit()                                     # what every loader calls at the end of a real load
        assert con.execute(f"SELECT count(*) FROM {PROBE}").fetchone()[0] == 1        # visible inside the rehearsal
    finally:
        con.close()                                      # rolls back
    monkeypatch.delenv("ITAM_DRY_RUN")
    real = master_db.connect()
    try:
        assert real.execute("SELECT to_regclass(%s) IS NULL", (PROBE,)).fetchone()[0] is True, "the rehearsal leaked a committed change"
    finally:
        real.execute(f"DROP TABLE IF EXISTS {PROBE}")
        real.commit()
        real.close()


def test_a_normal_connection_still_commits(monkeypatch):
    monkeypatch.delenv("ITAM_DRY_RUN", raising=False)
    con = master_db.connect()
    try:
        assert type(con) is psycopg.Connection
    finally:
        con.close()


def test_the_check_step_uses_the_rehearsal_and_the_load_step_does_not():
    for kind in ("assets", "cipl", "calls", "rma"):
        assert "--dry-run" in importer._cmd(kind, "f.xlsx", "out", None, load=False, force=False)
        assert "--dry-run" not in importer._cmd(kind, "f.xlsx", "out", None, load=True, force=False)
        assert "--no-load-db" not in importer._cmd(kind, "f.xlsx", "out", None, load=False, force=False)


@pytest.fixture()
def job(box, monkeypatch, tmp_path):
    from portal.app import importer as imp
    monkeypatch.setattr(imp, "UPLOADS", tmp_path)
    up = imp.save_upload("calls", "tracker.xlsx", b"PK\x03\x04 not really a workbook", "TESTER")
    return up["job_id"]


def test_an_overridable_safety_stop_becomes_a_warning_not_a_failure(job, monkeypatch):
    blocked = ("Wrote x.xlsx: 275 calls\n  CALLS: 275 rows loaded | changes: {'CHANGED': 28}\n"
               "Load blocked (SPARE_OUTWARD): 30 of 113 current rows are missing (>20%). Check the file or use --force.")
    monkeypatch.setattr(importer, "_run", lambda cmd, timeout=600, extra_env=None: (1, blocked))
    r = importer.check(job)
    assert r["ok"] is True and "30 of 113 current rows are missing" in r["warning"] and "use --force" not in r["warning"]
    assert r["log"].startswith("!! THE LOAD WOULD BE REFUSED") and "nothing has been changed" in r["log"]
    assert importer.detail(job)["status"] == "CHECKED"


def test_a_stop_that_cannot_be_overridden_still_fails_the_check(job, monkeypatch):
    monkeypatch.setattr(importer, "_run", lambda cmd, timeout=600, extra_env=None: (1, "Load blocked: snapshot 2026-09-01 is older than already loaded 2026-10-01."))
    r = importer.check(job)
    assert r["ok"] is False and r["warning"] is None
    assert importer.detail(job)["status"] == "CHECK_FAILED"


def test_an_ordinary_failure_still_fails_the_check(job, monkeypatch):
    monkeypatch.setattr(importer, "_run", lambda cmd, timeout=600, extra_env=None: (1, "Sheet 'Outward' not found in tracker.xlsx"))
    assert importer.check(job)["ok"] is False


def test_a_clean_rehearsal_passes_with_no_warning(job, monkeypatch):
    monkeypatch.setattr(importer, "_run", lambda cmd, timeout=600, extra_env=None: (0, "DRY RUN: nothing was saved"))
    r = importer.check(job)
    assert r["ok"] is True and r["warning"] is None
