"""Tests for Fill IMAC: the auto-filled context (asset snapshot + requester), the CPF/name search fallback when
an asset has no registered user, creating a record with its generated PDF, the permission model, and the history
that shows on the asset's own detail page. Every test runs inside one rolled-back database transaction - the
real data is never changed.

  cd portal && .venv\\Scripts\\python -m pytest -q tests/test_imac.py
"""
import pytest
from starlette.testclient import TestClient

from portal.app import auth, edit, imac, queries
from portal.app.main import app
from test_scoping import HDR, as_user, one, sandbox  # noqa: F401  (sandbox is a fixture)

ED = {"user_id": 0, "username": "Test Editor", "display_name": "Test Editor", "role": "ADMIN", "engineer_key": None}


def an_asset_with_user(con):
    return one(con, """SELECT a.asset_key, a.cpf_no FROM asset a JOIN employee e ON e.cpf_no = a.cpf_no AND e.record_status = 'ACTIVE'
                       WHERE a.is_current = 1 AND a.record_level = 'ASSET' AND a.cpf_no IS NOT NULL LIMIT 1""")


def an_asset_without_user(con):
    return one(con, """SELECT asset_key FROM asset WHERE is_current = 1 AND record_level = 'ASSET'
                       AND (cpf_no IS NULL OR cpf_no NOT IN (SELECT cpf_no FROM employee WHERE record_status = 'ACTIVE')) LIMIT 1""")


def test_context_pulls_the_registered_user_automatically(sandbox):
    a = an_asset_with_user(sandbox)
    ctx = imac.context(ED, a["asset_key"])
    assert ctx["requester"]["cpf"] == str(a["cpf_no"]) and ctx["requester"]["name"] and ctx["engineer_name"] == "Test Editor"
    assert ctx["asset_type"] is not None


def test_context_requester_is_none_when_asset_has_no_registered_user(sandbox):
    a = an_asset_without_user(sandbox)
    if not a:
        pytest.skip("every current asset in this dataset has a resolvable user - nothing to test here")
    ctx = imac.context(ED, a["asset_key"])
    assert ctx["requester"] is None


def test_person_lookup_resolves_a_real_cpf_and_returns_none_for_an_unknown_one(sandbox):
    a = an_asset_with_user(sandbox)
    p = imac.person(a["cpf_no"])
    assert p["cpf"] == str(a["cpf_no"]) and p["name"]
    assert imac.person(a["cpf_no"] + 9000000) is None    # well-formed but not a real CPF
    assert imac.person("NOT-A-NUMBER") is None            # non-numeric input must not raise
    assert imac.person("") is None and imac.person(None) is None


def test_create_snapshots_the_asset_and_stores_a_valid_pdf(sandbox):
    a = an_asset_with_user(sandbox)
    r = imac.create(ED, a["asset_key"], {"requester_cpf": a["cpf_no"]}, "127.0.0.1")
    assert r["asset_key"] == a["asset_key"]
    row = one(sandbox, "SELECT * FROM imac_record WHERE imac_id = %s", (r["id"],))
    assert row["requester_cpf"] == str(a["cpf_no"]) and row["engineer_name"] == "Test Editor" and row["asset_type"]
    assert row["call_closure_date"] is not None and bytes(row["pdf"])[:4] == b"%PDF"
    hist = imac.for_asset(a["asset_key"])
    assert hist and hist[0]["id"] == r["id"]


def test_create_without_a_requester_still_works(sandbox):
    a = an_asset_with_user(sandbox)
    r = imac.create(ED, a["asset_key"], {}, "127.0.0.1")      # no requester_cpf at all - e.g. nobody was found and nobody picked
    row = one(sandbox, "SELECT requester_cpf, requester_name FROM imac_record WHERE imac_id = %s", (r["id"],))
    assert row["requester_cpf"] is None and row["requester_name"] is None


def test_unknown_asset_is_rejected(sandbox):
    with pytest.raises(edit.NotFound):
        imac.create(ED, "NO-SUCH-ASSET", {}, "127.0.0.1")
    with pytest.raises(edit.NotFound):
        imac.context(ED, "NO-SUCH-ASSET")


def test_engineer_can_only_fill_imac_for_their_own_asset(sandbox):
    mine = one(sandbox, "SELECT engineer_name, min(asset_key) k FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND engineer_name IS NOT NULL GROUP BY 1 ORDER BY 1 LIMIT 1")
    theirs = one(sandbox, "SELECT asset_key k FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND engineer_name <> %s LIMIT 1", (mine["engineer_name"],))
    user = {"user_id": 0, "username": "ENGY", "display_name": "Engy", "role": "USER", "engineer_key": mine["engineer_name"]}
    imac.create(user, mine["k"], {}, "127.0.0.1")      # must not raise
    with pytest.raises(auth.AuthError):
        imac.create(user, theirs["k"], {}, "127.0.0.1")


def test_pdf_download_permission_and_content(sandbox):
    a = an_asset_with_user(sandbox)
    r = imac.create(ED, a["asset_key"], {}, "127.0.0.1")
    data, name = imac.get_pdf(ED, r["id"])
    assert data[:4] == b"%PDF" and name.endswith(".pdf")
    stranger = {"user_id": 1, "username": "STRANGER", "role": "USER", "engineer_key": "NOBODY"}
    with pytest.raises(auth.AuthError):
        imac.get_pdf(stranger, r["id"])
    with pytest.raises(edit.NotFound):
        imac.get_pdf(ED, 999999999)


def test_http_full_round_trip(sandbox, monkeypatch):
    a = an_asset_with_user(sandbox)
    as_user(monkeypatch, "ADMIN", username="IMAC_HTTP")
    with TestClient(app) as c:
        ctx = c.get(f"/api/assets/{a['asset_key']}/imac/context")
        assert ctx.status_code == 200 and ctx.json()["requester"]["cpf"] == str(a["cpf_no"])

        person = c.get(f"/api/assets/{a['asset_key']}/imac/person", params={"cpf": a["cpf_no"]})
        assert person.status_code == 200 and person.json()["person"]["cpf"] == str(a["cpf_no"])

        r = c.post("/api/edit/assets/imac", json={"key": a["asset_key"], "requester_cpf": a["cpf_no"]}, headers=HDR)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["detail"]["related"]["imac"][0]["requester_cpf"] == str(a["cpf_no"])

        pdf = c.get(f"/api/imac/{body['id']}/pdf")
        assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content[:4] == b"%PDF"
