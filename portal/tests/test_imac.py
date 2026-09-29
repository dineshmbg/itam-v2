"""Tests for Fill IMAC: creating a record (with and without an asset (CI) change), the permission model, the
generated PDF, and the history that shows on both the old and new asset's detail page. Every test runs inside
one rolled-back database transaction - the real data is never changed.

  cd portal && .venv\\Scripts\\python -m pytest -q tests/test_imac.py
"""
import pytest
from starlette.testclient import TestClient

from portal.app import auth, edit, imac, queries
from portal.app.main import app
from test_scoping import HDR, as_user, one, sandbox  # noqa: F401  (sandbox is a fixture)

ED = {"user_id": 0, "username": "Test Editor", "role": "ADMIN", "engineer_key": None}


def an_asset(con, engineer=None):
    if engineer:
        return one(con, "SELECT asset_key, engineer_name FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND engineer_name = %s ORDER BY asset_key LIMIT 1", (engineer,))
    return one(con, "SELECT asset_key, engineer_name FROM asset WHERE is_current = 1 AND record_level = 'ASSET' ORDER BY asset_key LIMIT 1")


def test_create_installation_snapshots_the_asset_and_stores_a_pdf(sandbox):
    a = an_asset(sandbox)
    r = imac.create(ED, a["asset_key"], {"change_type": "installation", "ticket_no": "SR-TEST-1", "feasible": True, "requester_name": "A User"}, "127.0.0.1")
    assert r["asset_key"] == a["asset_key"] and r["prev_asset_key"] is None
    row = one(sandbox, "SELECT * FROM imac_record WHERE imac_id = %s", (r["id"],))
    assert row["change_type"] == "INSTALLATION" and row["ticket_no"] == "SR-TEST-1" and row["asset_type"] and bytes(row["pdf"])[:4] == b"%PDF"
    hist = imac.for_asset(a["asset_key"])
    assert hist and hist[0]["id"] == r["id"]


def test_infeasible_requires_a_reason(sandbox):
    a = an_asset(sandbox)
    with pytest.raises(edit.Invalid):
        imac.create(ED, a["asset_key"], {"change_type": "ADDITION", "feasible": False}, "127.0.0.1")
    imac.create(ED, a["asset_key"], {"change_type": "ADDITION", "feasible": False, "infeasible_reason": "Part not in stock"}, "127.0.0.1")


def test_unknown_change_type_and_asset_are_rejected(sandbox):
    a = an_asset(sandbox)
    with pytest.raises(edit.Invalid):
        imac.create(ED, a["asset_key"], {"change_type": "REPLACEMENT"}, "127.0.0.1")
    with pytest.raises(edit.NotFound):
        imac.create(ED, "NO-SUCH-ASSET", {"change_type": "INSTALLATION"}, "127.0.0.1")


def test_change_with_a_different_asset_records_old_to_new_and_shows_on_both(sandbox):
    old = one(sandbox, "SELECT asset_key FROM asset WHERE is_current = 1 AND record_level = 'ASSET' ORDER BY asset_key LIMIT 1")
    new = one(sandbox, "SELECT asset_key FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND asset_key <> %s ORDER BY asset_key DESC LIMIT 1", (old["asset_key"],))
    r = imac.create(ED, old["asset_key"], {"change_type": "CHANGE", "new_asset_key": new["asset_key"], "ticket_no": "SR-TEST-2"}, "127.0.0.1")
    assert r["asset_key"] == new["asset_key"] and r["prev_asset_key"] == old["asset_key"]
    row = one(sandbox, "SELECT asset_type, make, model, serial_no FROM asset WHERE asset_key = %s", (new["asset_key"],))
    saved = one(sandbox, "SELECT asset_type, make, model, serial_no FROM imac_record WHERE imac_id = %s", (r["id"],))
    assert saved["asset_type"] == row["asset_type"] and saved["serial_no"] == row["serial_no"]      # snapshot is of the NEW asset, not the one the button was pressed on
    assert {h["id"] for h in imac.for_asset(old["asset_key"])} & {r["id"]}
    assert {h["id"] for h in imac.for_asset(new["asset_key"])} & {r["id"]}
    assert one(sandbox, "SELECT count(*) n FROM portal_audit WHERE record_key = %s AND action = 'EVENT'", (new["asset_key"],))["n"] == 1


def test_engineer_can_only_fill_imac_for_their_own_asset(sandbox):
    mine = one(sandbox, "SELECT engineer_name, min(asset_key) k FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND engineer_name IS NOT NULL GROUP BY 1 ORDER BY 1 LIMIT 1")
    theirs = one(sandbox, "SELECT asset_key k FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND engineer_name <> %s LIMIT 1", (mine["engineer_name"],))
    user = {"user_id": 0, "username": "ENGY", "role": "USER", "engineer_key": mine["engineer_name"]}
    imac.create(user, mine["k"], {"change_type": "INSTALLATION"}, "127.0.0.1")      # must not raise
    with pytest.raises(auth.AuthError):
        imac.create(user, theirs["k"], {"change_type": "INSTALLATION"}, "127.0.0.1")


def test_pdf_download_permission_and_content(sandbox):
    a = an_asset(sandbox)
    r = imac.create(ED, a["asset_key"], {"change_type": "INSTALLATION"}, "127.0.0.1")
    data, name = imac.get_pdf(ED, r["id"])
    assert data[:4] == b"%PDF" and name.endswith(".pdf")
    stranger = {"user_id": 1, "username": "STRANGER", "role": "USER", "engineer_key": "NOBODY"}
    with pytest.raises(auth.AuthError):
        imac.get_pdf(stranger, r["id"])
    with pytest.raises(edit.NotFound):
        imac.get_pdf(ED, 999999999)


def test_http_create_and_download(sandbox, monkeypatch):
    a = an_asset(sandbox)
    as_user(monkeypatch, "ADMIN", username="IMAC_HTTP")
    with TestClient(app) as c:
        r = c.post("/api/edit/assets/imac", json={"key": a["asset_key"], "change_type": "ADDITION", "ticket_no": "SR-TEST-3"}, headers=HDR)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["detail"]["related"]["imac"][0]["ticket_no"] == "SR-TEST-3"
        pdf = c.get(f"/api/imac/{body['id']}/pdf")
        assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content[:4] == b"%PDF"
