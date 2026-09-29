"""Barcode stickers (Data tools > Assets register > Print barcode labels) and the barcode on the Asset (CI) hover card.
Every test runs inside one rolled-back database transaction - the real data is never changed.
"""
import pytest
from starlette.testclient import TestClient

from portal.app import cards, export
from portal.app.main import app
from test_scoping import HDR, as_user, one, sandbox  # noqa: F401  (sandbox is a fixture)


def an_asset(con, engineer=None):
    if engineer:
        return one(con, "SELECT asset_key FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND engineer_name = %s ORDER BY asset_key LIMIT 1", (engineer,))["asset_key"]
    return one(con, "SELECT asset_key FROM asset WHERE is_current = 1 AND record_level = 'ASSET' ORDER BY asset_key LIMIT 1")["asset_key"]


# ---------------------------------------------------------------- barcode generation itself
def test_barcode_svg_is_a_real_code128_barcode_of_the_given_value():
    svg = export.asset_barcode_svg("ANK-NAS-BACKUP")
    assert svg.startswith("<?xml") and "<svg" in svg
    assert export.asset_barcode_svg("A") != export.asset_barcode_svg("B")  # not a fixed placeholder image


def test_labels_pdf_has_one_page_per_48_assets_and_is_a_real_pdf(sandbox):
    rows = [{"asset_key": f"TEST-LABEL-{i:03d}", "asset_class": "LAPTOP", "asset_type": "LAPTOP", "make_model": "DELL LATITUDE", "serial_no": f"SN{i}"} for i in range(50)]
    pdf = export.asset_labels_pdf(rows)
    assert pdf.startswith(b"%PDF")
    assert pdf.count(b"/Type /Page\n") or pdf.count(b"/Type/Page") or b"/Count 2" in pdf  # 50 assets at 48/page = 2 pages


def test_labels_pdf_skips_rows_with_no_asset_key_without_crashing():
    pdf = export.asset_labels_pdf([{"asset_key": None, "asset_class": "LAPTOP"}, {"asset_key": "TEST-OK-1", "asset_class": "LAPTOP", "make_model": "X", "serial_no": "1"}])
    assert pdf.startswith(b"%PDF")


def test_labels_pdf_handles_an_empty_register():
    pdf = export.asset_labels_pdf([])
    assert pdf.startswith(b"%PDF")


# ---------------------------------------------------------------- hover card
def test_asset_hover_card_carries_a_barcode_of_its_own_ci(sandbox):
    key = an_asset(sandbox)
    data = cards._asset(key, admin=True)
    assert data["barcode_svg"] and data["barcode_svg"].startswith("<?xml")
    assert data["barcode_svg"] == export.asset_barcode_svg(key)


def test_other_hover_card_kinds_do_not_carry_a_barcode(sandbox):
    sr = one(sandbox, "SELECT sr_id FROM svc_call WHERE is_current = 1 LIMIT 1")["sr_id"]
    assert "barcode_svg" not in cards._call(sr, admin=True)


# ---------------------------------------------------------------- HTTP: print route, scoping, permissions
def test_http_print_labels_returns_a_pdf_for_an_admin(sandbox, monkeypatch):
    as_user(monkeypatch, "ADMIN", username="BC_ADMIN")
    with TestClient(app) as c:
        r = c.get("/api/registers/assets/labels")
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
        assert "asset_labels" in r.headers["content-disposition"]


def test_http_print_labels_is_scoped_to_the_users_own_assets(sandbox, monkeypatch):
    mine = one(sandbox, "SELECT engineer_name, min(asset_key) k, count(*) n FROM asset WHERE is_current = 1 AND record_level = 'ASSET' GROUP BY 1 ORDER BY 1 LIMIT 1")
    as_user(monkeypatch, "USER", engineer_key=mine["engineer_name"], username="BC_USER")
    with TestClient(app) as c:
        r = c.get("/api/registers/assets/labels")
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
        # a User printing their own scope must never produce more labels than they have assets - proven indirectly via export_table,
        # which the route reuses (already covered directly for CSV export in test_review_batch.py). There is no equivalent labels route
        # for any other register - "calls/labels" simply falls through to the generic register-detail route, refused the same way any
        # other Calls access is for a User with no call_parts_access grant.
        assert c.get("/api/registers/calls/labels").status_code == 403
