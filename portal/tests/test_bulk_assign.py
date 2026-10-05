"""Bulk assign / hand over assets between engineers.

  cd portal && .venv\Scripts\python -m pytest -q tests/test_bulk_assign.py
"""
import pytest

from portal.app import edit
from tests.test_ux_batch3 import sandbox  # noqa: F401  (rolled-back transaction fixture)


def _pick(con):
    engs = [r[0] for r in con.execute("SELECT engineer_key FROM portal_engineer ORDER BY 1 LIMIT 2").fetchall()]
    keys = [r[0] for r in con.execute("SELECT asset_key FROM asset WHERE is_current = 1 AND record_level = 'ASSET' ORDER BY 1 LIMIT 3").fetchall()]
    if len(engs) < 2 or len(keys) < 3:
        pytest.skip("needs two engineers and three assets")
    return engs, keys


def test_assign_then_hand_over(sandbox):
    (a, b), keys = _pick(sandbox)
    r = edit.reassign_assets(keys, a, None, "TESTER", "127.0.0.1")
    assert r["changed"] + r["skipped"] == 3
    assert all(sandbox.execute("SELECT engineer_name FROM asset WHERE asset_key=%s AND is_current=1", (k,)).fetchone()[0] == a for k in keys)
    r = edit.reassign_assets(keys, b, a, "TESTER", "127.0.0.1")
    assert r["changed"] == 3
    assert all(sandbox.execute("SELECT engineer_name FROM asset WHERE asset_key=%s AND is_current=1", (k,)).fetchone()[0] == b for k in keys)


def test_from_filter_leaves_others(sandbox):
    (a, b), keys = _pick(sandbox)
    edit.reassign_assets(keys[:1], a, None, "T", "x")
    edit.reassign_assets(keys[1:], b, None, "T", "x")
    r = edit.reassign_assets(keys, a, b, "T", "x")           # only the ones with b move
    assert r["changed"] == 2 and r["skipped"] == 1


def test_rejects_bad_input(sandbox):
    (a, _), keys = _pick(sandbox)
    with pytest.raises(edit.Invalid):
        edit.reassign_assets([], a, None, "T", "x")
    with pytest.raises(edit.Invalid):
        edit.reassign_assets(keys, a, a, "T", "x")
    with pytest.raises(edit.Invalid):
        edit.reassign_assets(keys, "NOT A REAL ENGINEER", None, "T", "x")
    with pytest.raises(edit.NotFound):
        edit.reassign_assets(["NO-SUCH-ASSET"], a, None, "T", "x")
