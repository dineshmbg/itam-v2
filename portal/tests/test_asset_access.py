"""Tests for the asset_access permission grant (2026-09-30): a User-group account can be given READ (see every asset,
unscoped, in the dashboard/register/reports - no editing beyond the usual USER_ASSET_FIELDS on their own assets) or
FULL (also edit/create/archive/verify any asset, as if it were assigned to them). Deliberately narrower than
extended_access, which conflates "sees everything" with "can edit everything" across every register, not just Assets.
Every test runs inside one rolled-back database transaction - the real data is never changed.

  cd portal && .venv\\Scripts\\python -m pytest -q tests/test_asset_access.py
"""
import pytest
from starlette.testclient import TestClient

from portal.app import auth, queries
from portal.app.main import app
from test_scoping import HDR, as_user, one, sandbox  # noqa: F401  (sandbox is a fixture)


def an_asset(con, engineer=None):
    if engineer:
        return one(con, "SELECT asset_key, engineer_name FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND engineer_name = %s ORDER BY asset_key LIMIT 1", (engineer,))
    return one(con, "SELECT asset_key, engineer_name FROM asset WHERE is_current = 1 AND record_level = 'ASSET' ORDER BY asset_key LIMIT 1")


def not_their_asset(con, engineer):
    return one(con, "SELECT asset_key FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND engineer_name <> %s LIMIT 1", (engineer,))


# ---------------------------------------------------------------- scope_for (dashboard + register)
def test_asset_access_read_and_full_unscope_the_assets_register(sandbox):
    base = {"role": "USER", "engineer_key": None}
    assert queries.scope_for("assets", queries.dataset("assets"), {**base, "asset_access": "NONE"}) == ("false", [])
    for grant in ("READ", "FULL"):
        assert queries.scope_for("assets", queries.dataset("assets"), {**base, "asset_access": grant}) is None


def test_asset_access_does_not_unscope_other_registers(sandbox):
    """A narrower grant than extended_access on purpose - PM worklist and Employees stay scoped to the user's own engineer_key."""
    user = {"role": "USER", "engineer_key": None, "asset_access": "FULL"}
    assert queries.scope_for("pm", queries.dataset("pm"), user) == ("false", [])
    assert queries.scope_for("employees", queries.dataset("employees"), user) == ("false", [])


# ---------------------------------------------------------------- editing: only FULL bypasses ownership
def test_asset_access_read_grants_no_editing_beyond_the_usual_rule(sandbox):
    a = an_asset(sandbox)
    other = not_their_asset(sandbox, a["engineer_name"])
    user = {"role": "USER", "engineer_key": None, "asset_access": "READ"}
    with pytest.raises(auth.AuthError):
        auth.check_edit(user, "assets", ["asset_status"], a["asset_key"])
    with pytest.raises(auth.AuthError):
        auth.check_verify(user, a["asset_key"])
    with pytest.raises(auth.AuthError):
        auth.check_create(user, "assets")
    with pytest.raises(auth.AuthError):
        auth.check_archive(user, "assets")


def test_asset_access_full_can_edit_verify_create_and_archive_any_asset(sandbox):
    a = an_asset(sandbox)
    other = not_their_asset(sandbox, a["engineer_name"])
    user = {"role": "USER", "engineer_key": "NOBODYS-ENGINEER-KEY", "asset_access": "FULL"}
    auth.check_edit(user, "assets", ["asset_status", "make", "model"], a["asset_key"])   # any field, not just USER_ASSET_FIELDS
    auth.check_edit(user, "assets", ["asset_status"], other["asset_key"])                # any asset, not just their own
    auth.check_verify(user, other["asset_key"])
    auth.check_create(user, "assets")
    auth.check_archive(user, "assets")


def test_asset_access_full_still_does_not_grant_other_registers(sandbox):
    """Deliberately not extended_access: FULL on assets says nothing about Engineers, PM, or Administration."""
    user = {"role": "USER", "engineer_key": None, "asset_access": "FULL"}
    with pytest.raises(auth.AuthError):
        auth.check_create(user, "engineers")
    with pytest.raises(auth.AuthError):
        auth.check_edit(user, "engineers", ["mobile_no"], "SOME-ENGINEER-KEY")


# ---------------------------------------------------------------- HTTP: dashboard, register, update_user
def test_http_asset_dashboard_is_unscoped_with_read_access(sandbox, monkeypatch):
    u = as_user(monkeypatch, "USER", engineer_key="NOBODY-IN-PARTICULAR-AA-TEST", username="AA_SCOPED")
    u["asset_access"] = "NONE"
    with TestClient(app) as c:
        scoped = c.get("/api/dash/assets")
        assert scoped.status_code == 200 and scoped.json()["kpi"]["assets"] == 0     # no assets assigned to this made-up engineer key
    # a different username: the response cache (live.py's Hub) is keyed per-username, so reusing "AA_SCOPED" here would just
    # replay its cached (scoped) response instead of exercising the new asset_access value
    u2 = as_user(monkeypatch, "USER", engineer_key="NOBODY-IN-PARTICULAR-AA-TEST", username="AA_READ")
    u2["asset_access"] = "READ"
    with TestClient(app) as c:
        unscoped = c.get("/api/dash/assets")
        assert unscoped.status_code == 200 and unscoped.json()["kpi"]["assets"] > 100    # same engineer_key, now sees the whole fleet


def test_http_register_update_respects_asset_access(sandbox, monkeypatch):
    a = an_asset(sandbox)
    other = not_their_asset(sandbox, a["engineer_name"])
    u = as_user(monkeypatch, "USER", engineer_key="NOBODY-IN-PARTICULAR", username="AA_FULL")
    u["asset_access"] = "FULL"
    with TestClient(app) as c:
        r = c.post("/api/edit/assets/update", json={"key": other["asset_key"], "changes": {"remarks": "touched by asset_access FULL"}, "expected": {}}, headers=HDR)
        assert r.status_code == 200, r.text


def test_update_user_validates_and_persists_asset_access(sandbox):
    uid = auth.create_user("AA_TARGET", "Asset Access Target", None, "USER", "SETUP")["username"]
    row = one(sandbox, "SELECT user_id FROM portal_user WHERE username = %s", (uid,))
    with pytest.raises(auth.AuthError):
        auth.update_user(row["user_id"], {"asset_access": "SOMETHING"}, "TESTER")
    out = auth.update_user(row["user_id"], {"asset_access": "READ"}, "TESTER")
    assert out["asset_access"] == "READ"
