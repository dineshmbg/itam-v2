"""Tests for the 2026-09-23 review batch: physical verification, QR labels, CSV export, lifecycle and hardware fields, integrity checks.
Every test runs inside one rolled-back database transaction - the real data is never changed.
"""
import pytest
from starlette.testclient import TestClient

from portal.app import auth, edit, integrity, queries
from portal.app.main import app
from test_scoping import HDR, as_user, one, sandbox  # noqa: F401  (sandbox is a fixture)

ED = "Test Editor"


def an_asset(con, engineer=None):
    if engineer:
        return one(con, "SELECT asset_key, engineer_name FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND engineer_name = %s ORDER BY asset_key LIMIT 1", (engineer,))
    return one(con, "SELECT asset_key, engineer_name FROM asset WHERE is_current = 1 AND record_level = 'ASSET' ORDER BY asset_key LIMIT 1")


# ---------------------------------------------------------------- physical verification
def test_verify_records_each_check_and_keeps_history(sandbox):
    a = an_asset(sandbox)["asset_key"]
    edit.verify_asset(a, "found", "in rack 3", ED, "127.0.0.1")
    edit.verify_asset(a, "NOT_FOUND", None, ED, "127.0.0.1")
    rows = queries.detail("assets", a, user={"role": "ADMIN"})["related"]["verifications"]
    assert [r["result"] for r in rows] == ["NOT_FOUND", "FOUND"] or {r["result"] for r in rows} == {"FOUND", "NOT_FOUND"}
    assert one(sandbox, "SELECT count(*) n FROM portal_audit WHERE record_key = %s AND action = 'VERIFY'", (a,))["n"] == 2
    listing = queries.list_rows("assets", {"q": a}, user={"role": "ADMIN"})["rows"][0]
    assert listing["last_verified"] is not None


def test_verify_rejects_bad_result_and_unknown_asset(sandbox):
    a = an_asset(sandbox)["asset_key"]
    with pytest.raises(edit.Invalid):
        edit.verify_asset(a, "maybe", None, ED, "127.0.0.1")
    with pytest.raises(edit.NotFound):
        edit.verify_asset("NO-SUCH-ASSET", "FOUND", None, ED, "127.0.0.1")


def test_engineer_can_verify_only_their_own_assets(sandbox):
    mine = one(sandbox, "SELECT engineer_name, min(asset_key) k FROM asset WHERE is_current = 1 AND record_level = 'ASSET' GROUP BY 1 ORDER BY 1 LIMIT 1")
    theirs = one(sandbox, "SELECT asset_key k FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND engineer_name <> %s LIMIT 1", (mine["engineer_name"],))
    user = {"role": "USER", "engineer_key": mine["engineer_name"]}
    auth.check_verify(user, mine["k"])                          # must not raise
    with pytest.raises(auth.AuthError):
        auth.check_verify(user, theirs["k"])
    auth.check_verify({"role": "ADMIN"}, theirs["k"])           # an administrator may verify anything


def test_http_verify_and_read_only_account_is_blocked(sandbox, monkeypatch):
    a = an_asset(sandbox)["asset_key"]
    as_user(monkeypatch, "ADMIN", username="REVIEW_ADMIN")
    with TestClient(app) as c:
        r = c.post("/api/edit/assets/verify", json={"key": a, "result": "FOUND"}, headers=HDR)
        assert r.status_code == 200, r.text
        assert r.json()["detail"]["related"]["verifications"][0]["result"] == "FOUND"
    as_user(monkeypatch, "ADMIN", username="REVIEW_DEMO", read_only=True)
    with TestClient(app) as c:
        assert c.post("/api/edit/assets/verify", json={"key": a, "result": "FOUND"}, headers=HDR).status_code == 403


# ---------------------------------------------------------------- QR label
def test_qr_label_returns_svg_pointing_at_the_asset(sandbox, monkeypatch):
    a = an_asset(sandbox)["asset_key"]
    as_user(monkeypatch, "ADMIN", username="REVIEW_QR")
    with TestClient(app) as c:
        r = c.get(f"/api/assets/{a}/qr", headers={"Host": "192.168.1.75:8420"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["svg"].startswith("<svg") and f"open={a}" in j["url"] and j["url"].startswith("http://192.168.1.75:8420/#/registers/assets")
        assert c.get("/api/assets/NO-SUCH-ASSET/qr").status_code == 404


# ---------------------------------------------------------------- CSV export
def test_csv_export_matches_filter_and_is_scoped_to_the_user(sandbox, monkeypatch):
    as_user(monkeypatch, "ADMIN", username="REVIEW_EXP_A")
    with TestClient(app) as c:
        r = c.get("/api/registers/assets/export", params={"q": "printer"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        lines = r.content.decode("utf-8-sig").strip().split("\r\n")
        total = queries.list_rows("assets", {"q": "printer"}, user={"role": "ADMIN"})["total"]
        assert len(lines) == total + 1 and lines[0].startswith("Asset (CI)")
    eng = one(sandbox, "SELECT engineer_name, count(*) n FROM asset WHERE is_current = 1 GROUP BY 1 ORDER BY 2 LIMIT 1")
    as_user(monkeypatch, "USER", engineer_key=eng["engineer_name"], username="REVIEW_EXP_U")
    with TestClient(app) as c:
        lines = c.get("/api/registers/assets/export").content.decode("utf-8-sig").strip().split("\r\n")
        assert len(lines) - 1 <= eng["n"]                                     # only their own assets, never the whole fleet
        assert c.get("/api/registers/calls/export").status_code == 403       # and no access to registers they cannot open


# ---------------------------------------------------------------- lifecycle and hardware fields
def test_lifecycle_and_hardware_fields_validate_and_save(sandbox):
    a = an_asset(sandbox)["asset_key"]
    edit.update("assets", a, {"purchase_date": "2024-04-01", "purchase_cost": "45,000.5", "vendor_name": "test vendor", "po_no": "po-1",
                              "refresh_due_date": "2031-04-01", "ram": "16 gb", "processor": "i5"}, {}, ED, "127.0.0.1")
    row = one(sandbox, "SELECT purchase_cost, vendor_name, po_no, ram, refresh_due_date FROM asset WHERE asset_key = %s", (a,))
    assert float(row["purchase_cost"]) == 45000.50 and row["vendor_name"] == "TEST VENDOR" and row["ram"] == "16 GB" and str(row["refresh_due_date"]) == "2031-04-01"
    # portal-only fields need no manual-override lock, fields the importer writes still do
    locked = {r[0] for r in sandbox.execute("SELECT field FROM portal_lock WHERE dataset = 'assets' AND record_key = %s", (a,)).fetchall()}
    assert "ram" in locked and "purchase_cost" not in locked
    with pytest.raises(edit.Invalid):                                          # a purchase date cannot be in the future ...
        edit.update("assets", a, {"purchase_date": "2099-01-01"}, {}, ED, "127.0.0.1")
    with pytest.raises(edit.Invalid):                                          # ... and a refresh date cannot be absurdly far away
        edit.update("assets", a, {"refresh_due_date": "2099-01-01"}, {}, ED, "127.0.0.1")


# ---------------------------------------------------------------- integrity checks
def test_new_integrity_checks_exist_and_list_examples(sandbox):
    got = {c["id"]: c for c in integrity.checks()}
    for cid in ("ip_format", "ip_dup", "host_dup", "install_date", "cover_expired_use", "amc_removed_rate", "amc_no_rate", "user_retired",
                "user_retiring", "component_parent", "not_verified"):
        assert cid in got, cid
        c = got[cid]
        assert c["status"] in ("pass", "warn") and (not c["bad"] or c["sample"]), cid
    assert got["ip_format"]["bad"] > 0 and any("/" in (one(sandbox, "SELECT ip_address FROM asset WHERE asset_key = %s", (k,))["ip_address"] or "") for k in got["ip_format"]["sample"])


# ---------------------------------------------------------------- optional second backup location
def test_backup_is_copied_to_the_second_location_when_configured(sandbox, monkeypatch, tmp_path):
    from portal.app import backup
    src = tmp_path / "main"
    dst = tmp_path / "second"
    monkeypatch.setattr(backup, "BACKUP_DIR", src)
    monkeypatch.setattr(backup, "COPY_DIR", str(dst))
    r = backup.run_backup("MANUAL", by="test")
    assert (dst / r["file"]).exists() and (src / r["file"]).exists()
    assert "also copied" in one(sandbox, "SELECT note FROM portal_backup ORDER BY backup_id DESC LIMIT 1")["note"]
