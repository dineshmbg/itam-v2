"""Tests for the can_reassign_assets permission grant (2026-10-01): a User-group account may change CPF No. and
Engineer - and only those two fields - on an asset already assigned to them. This is for the hand-off scenario
(an asset's owner moves department, the engineer holding the asset reassigns it to the new owner/engineer) and is
deliberately narrower than asset_access=FULL: it widens WHICH FIELDS are editable, never WHICH ASSETS. The existing
ownership check in check_edit (the asset must already be assigned to the caller's engineer_key) is untouched.
Every test runs inside one rolled-back database transaction - the real data is never changed.

  cd portal && .venv\\Scripts\\python -m pytest -q tests/test_reassign_access.py
"""
import pytest
from starlette.testclient import TestClient

from portal.app import auth
from portal.app.main import app
from test_scoping import HDR, as_user, one, sandbox  # noqa: F401  (sandbox is a fixture)


def an_asset(con, engineer=None):
    if engineer:
        return one(con, "SELECT asset_key, engineer_name FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND engineer_name = %s ORDER BY asset_key LIMIT 1", (engineer,))
    return one(con, "SELECT asset_key, engineer_name FROM asset WHERE is_current = 1 AND record_level = 'ASSET' ORDER BY asset_key LIMIT 1")


def not_their_asset(con, engineer):
    return one(con, "SELECT asset_key FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND engineer_name <> %s LIMIT 1", (engineer,))


# ---------------------------------------------------------------- check_edit: field widening
def test_reassign_off_still_refuses_cpf_and_engineer(sandbox):
    a = an_asset(sandbox)
    user = {"role": "USER", "engineer_key": a["engineer_name"], "asset_access": "NONE", "can_reassign_assets": False}
    with pytest.raises(auth.AuthError):
        auth.check_edit(user, "assets", ["cpf_no"], a["asset_key"])
    with pytest.raises(auth.AuthError):
        auth.check_edit(user, "assets", ["engineer_name"], a["asset_key"])


def test_reassign_on_allows_cpf_and_engineer_on_their_own_asset(sandbox):
    a = an_asset(sandbox)
    user = {"role": "USER", "engineer_key": a["engineer_name"], "asset_access": "NONE", "can_reassign_assets": True}
    auth.check_edit(user, "assets", ["cpf_no", "engineer_name"], a["asset_key"])
    auth.check_edit(user, "assets", ["asset_status", "cpf_no"], a["asset_key"])          # combined with an ordinary field


def test_reassign_on_still_refuses_an_asset_not_assigned_to_them(sandbox):
    a = an_asset(sandbox)
    other = not_their_asset(sandbox, a["engineer_name"])
    user = {"role": "USER", "engineer_key": a["engineer_name"], "asset_access": "NONE", "can_reassign_assets": True}
    with pytest.raises(auth.AuthError):
        auth.check_edit(user, "assets", ["cpf_no"], other["asset_key"])


def test_reassign_on_still_refuses_unrelated_fields(sandbox):
    a = an_asset(sandbox)
    user = {"role": "USER", "engineer_key": a["engineer_name"], "asset_access": "NONE", "can_reassign_assets": True}
    with pytest.raises(auth.AuthError):
        auth.check_edit(user, "assets", ["make", "model"], a["asset_key"])


# ---------------------------------------------------------------- HTTP: real reassignment + persistence
def test_http_reassign_updates_ownership_on_own_asset(sandbox, monkeypatch):
    a = an_asset(sandbox)
    other_eng_row = one(sandbox, "SELECT DISTINCT engineer_name FROM asset WHERE is_current = 1 AND engineer_name <> %s AND engineer_name IS NOT NULL LIMIT 1", (a["engineer_name"],))
    new_owner = one(sandbox, "SELECT cpf_no FROM employee WHERE cpf_no IS NOT NULL LIMIT 1")
    u = as_user(monkeypatch, "USER", engineer_key=a["engineer_name"], username="RA_HTTP")
    u["can_reassign_assets"] = True
    with TestClient(app) as c:
        r = c.post("/api/edit/assets/update", json={"key": a["asset_key"], "changes": {"cpf_no": new_owner["cpf_no"], "engineer_name": other_eng_row["engineer_name"]}, "expected": {}}, headers=HDR)
        assert r.status_code == 200, r.text
    row = one(sandbox, "SELECT cpf_no, engineer_name FROM asset WHERE asset_key = %s AND is_current = 1", (a["asset_key"],))
    assert row["cpf_no"] == new_owner["cpf_no"] and row["engineer_name"] == other_eng_row["engineer_name"]


def test_http_reassign_refused_without_the_flag(sandbox, monkeypatch):
    a = an_asset(sandbox)
    u = as_user(monkeypatch, "USER", engineer_key=a["engineer_name"], username="RA_HTTP_OFF")
    u["can_reassign_assets"] = False
    with TestClient(app) as c:
        r = c.post("/api/edit/assets/update", json={"key": a["asset_key"], "changes": {"cpf_no": 1}, "expected": {}}, headers=HDR)
        assert r.status_code == 403


def test_update_user_persists_can_reassign_assets(sandbox):
    created = auth.create_user("RA_TARGET", "Reassign Access Target", None, "USER", "SETUP")
    row = one(sandbox, "SELECT user_id FROM portal_user WHERE username = %s", (created["username"],))
    out = auth.update_user(row["user_id"], {"can_reassign_assets": True}, "TESTER")
    assert out["can_reassign_assets"] is True
    out2 = auth.update_user(row["user_id"], {"can_reassign_assets": False}, "TESTER")
    assert out2["can_reassign_assets"] is False


# ---------------------------------------------------------------- schema: the edit FORM must actually offer the fields
def asset_fields(schema_json):
    return {f["key"]: f for f in schema_json["datasets"]["assets"]["fields"]}


def test_schema_marks_cpf_and_engineer_readonly_without_the_flag(sandbox, monkeypatch):
    """check_edit allowing a field is not enough - record.js's edit form only renders fields the schema says are NOT
    readonly (`spec.fields.filter(f => !f.readonly)`). Caught as a real bug on 2026-10-01: check_edit was fixed to
    allow cpf_no/engineer_name under can_reassign_assets, but routes_edit.py's schema() still always computed
    readonly from USER_ASSET_FIELDS alone - the field simply never appeared in the edit dialog, so the grant was
    unusable from the UI even though the backend permission was correct."""
    as_user(monkeypatch, "USER", engineer_key="SOME-ENGINEER", username="SCHEMA_OFF")
    with TestClient(app) as c:
        fields = asset_fields(c.get("/api/edit/schema").json())
    assert fields["cpf_no"]["readonly"] is True
    assert fields["engineer_name"]["readonly"] is True
    assert fields["asset_status"]["readonly"] is False


def test_schema_unlocks_cpf_and_engineer_with_the_flag(sandbox, monkeypatch):
    u = as_user(monkeypatch, "USER", engineer_key="SOME-ENGINEER", username="SCHEMA_ON")
    u["can_reassign_assets"] = True
    with TestClient(app) as c:
        fields = asset_fields(c.get("/api/edit/schema").json())
    assert fields["cpf_no"]["readonly"] is False
    assert fields["engineer_name"]["readonly"] is False
    assert fields["make"]["readonly"] is True          # still not a blanket unlock of every field


def test_schema_unlocks_every_field_with_asset_access_full(sandbox, monkeypatch):
    u = as_user(monkeypatch, "USER", engineer_key="SOME-ENGINEER", username="SCHEMA_FULL")
    u["asset_access"] = "FULL"
    with TestClient(app) as c:
        fields = asset_fields(c.get("/api/edit/schema").json())
    assert fields["make"]["readonly"] is False
    assert fields["cpf_no"]["readonly"] is False
