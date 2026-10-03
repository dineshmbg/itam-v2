"""The backup/restore pipeline really works: dump the live data, restore it into a scratch database, compare every table (db/restore_drill.py).

Skipped where the machine has no PostgreSQL login that may create databases (the portal's own login may not). The live database is only read;
the scratch database is dropped again.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
import restore_drill as rd  # noqa: E402


def test_a_fresh_backup_restores_into_a_scratch_database_and_matches_live():
    if not rd.admin_password("postgres"):
        pytest.skip("no PostgreSQL admin login available for the restore drill")
    try:
        res = rd.run()
    except rd.backup.BackupError:
        pytest.skip("PostgreSQL client tools (pg_dump / pg_restore) not found")
    assert res["ok"], res["mismatch"]
    assert res["tables"] >= 20


def test_the_drill_only_ever_drops_its_own_scratch_databases():
    assert rd.PREFIX == "ongc_ank_drill_" and not "ongc_ank".startswith(rd.PREFIX)
