"""Permission matrix: every kind of account x every sensitive action, through the REAL HTTP routes (2026-10-03).

Four overlapping grants (group, extended access, calls/parts access, asset access) mean a new permission has to be right in three places
- the gate (auth.check_*), the Manage-user save allowlist and the edit-form schema - and we have shipped it wrong twice. One table of
"who may do what" makes the whole picture visible in one place and fails loudly when any route disagrees with it. To change a rule, change
the table; to add a grant, add a persona row.

True = the request is allowed through (it may still be answered 400/404 because the test sends minimal input - that is not a denial);
False = 403. Each case runs in its own rolled-back transaction, so nothing real is touched, even for archive / create.

  cd portal && .venv\\Scripts\\python -m pytest -q tests/test_permission_matrix.py
"""
import pytest
from starlette.testclient import TestClient

from portal.app.main import app
from test_scoping import HDR, as_user, one, sandbox  # noqa: F401  (sandbox is a fixture)

OWNER = "MATRIX-OWNER-ENGINEER"      # the engineer the test asset is assigned to (made up: ownership is a plain string comparison)
ELSE = "MATRIX-SOMEONE-ELSE"

# persona -> how the account looks to the request (the same shape auth.session_user produces)
PERSONAS = {
    "admin": dict(role="ADMIN"),
    "user": dict(role="USER", engineer_key=OWNER),
    "user+parts_read": dict(role="USER", engineer_key=OWNER, call_parts_access="READ"),
    "user+parts_full": dict(role="USER", engineer_key=OWNER, call_parts_access="FULL"),
    "user+asset_read": dict(role="USER", engineer_key=ELSE, asset_access="READ"),
    "user+asset_full": dict(role="USER", engineer_key=ELSE, asset_access="FULL"),
    "user+extended": dict(role="ADMIN", group="USER", engineer_key=ELSE, extended_access=True),     # effective role ADMIN, real group USER
    "user+lead": dict(role="USER", engineer_key=ELSE, lead_tools=True),                               # plain User, designation Team Leader/SI
    "user_no_engineer": dict(role="USER", engineer_key=None),
    "demo_readonly": dict(role="ADMIN", read_only=True),
}


def who(*allowed):
    return {p: p in allowed for p in PERSONAS}


READ_ALL = who(*PERSONAS)
ADMINISH = who("admin", "user+extended", "demo_readonly")                 # administrator-level routes (admin=True): effective role ADMIN
FULL_ADMIN = who("admin", "demo_readonly")                                # admin="strict": Control, Data tools, Administration - not extended access
LEAD = who("admin", "user+lead", "demo_readonly")                         # admin="lead_strict": as strict, plus a Team Leader/SI User (Inventory match, Users, Activity log)
LEAD_PM = who("admin", "user+extended", "user+lead", "demo_readonly")     # admin="lead": as admin=True, plus a Team Leader/SI User (PM cycles and snapshots)
CALLS_REGISTER = who("admin", "user+parts_read", "user+parts_full", "user+extended", "demo_readonly")
WRITE_ADMINISH = who("admin", "user+extended")                            # a write only a real/effective administrator may make (demo is read-only -> 403)

ACTIONS = {
    # name: (method, path, body or None, who is allowed)
    "dashboard: assets": ("GET", "/api/dash/assets", None, READ_ALL),
    "dashboard: calls": ("GET", "/api/dash/calls", None, READ_ALL),
    "dashboard: engineers": ("GET", "/api/dash/engineers", None, ADMINISH),
    "register: calls": ("GET", "/api/registers/calls", None, CALLS_REGISTER),
    "register: inward": ("GET", "/api/registers/inward", None, CALLS_REGISTER),
    "register: assets": ("GET", "/api/registers/assets", None, READ_ALL),
    "pm: cycles page": ("GET", "/api/pm/cycles", None, LEAD_PM),
    "pm: roll over": ("POST", "/api/pm/rollover", {}, who("admin", "user+extended", "user+lead")),
    "control: change log": ("GET", "/api/audit", None, FULL_ADMIN),
    "control: data integrity": ("GET", "/api/integrity", None, FULL_ADMIN),
    "admin: users list": ("GET", "/api/admin/users", None, LEAD),
    "admin: users update": ("POST", "/api/admin/users/update", {"user_id": 0}, who("admin", "user+lead")),
    "admin: sync from roster": ("POST", "/api/admin/users/sync", {}, who("admin")),                      # roster-derived groups: administrators only
    "admin: security settings": ("POST", "/api/admin/settings", {"settings": {}}, who("admin")),
    "admin: activity log": ("GET", "/api/admin/activity", None, LEAD),
    "tools: inventory match": ("GET", "/api/match/meta", None, LEAD),
    "tools: data import": ("GET", "/api/admin/import", None, FULL_ADMIN),                                # Data import / Backup / Software update / E-mail stay administrator-only
    "admin: e-mail and alerts": ("GET", "/api/admin/email", None, FULL_ADMIN),
    "admin: backups list": ("GET", "/api/admin/backups", None, FULL_ADMIN),
    # Replace / Redeploy re-key an asset across the whole database: real administrators only - not even extended access
    "lifecycle: preflight": ("GET", "/api/lifecycle/preflight?key=MATRIX-NONE", None, FULL_ADMIN),
    "lifecycle: candidates": ("GET", "/api/lifecycle/candidates?q=ab", None, FULL_ADMIN),
    "lifecycle: replace": ("POST", "/api/lifecycle/replace", {}, who("admin")),
    "lifecycle: redeploy": ("POST", "/api/lifecycle/redeploy", {}, who("admin")),
    # bulk (re)assignment of up to thousands of assets to an engineer in one click
    "assets: bulk assign to an engineer": ("POST", "/api/edit/assets/reassign", {}, who("admin", "user+asset_full", "user+extended")),
    "calls: create": ("POST", "/api/edit/calls/create", {"values": {}}, who("admin", "user+parts_full", "user+extended")),
    "assets: create": ("POST", "/api/edit/assets/create", {"values": {}}, who("admin", "user+asset_full", "user+extended")),
    "assets: archive": ("POST", "/api/edit/assets/archive", "OWN_ARCHIVE", who("admin", "user+asset_full", "user+extended")),
    "assets: edit hostname, own asset": ("POST", "/api/edit/assets/update", "OWN_HOSTNAME", who("admin", "user", "user+parts_read", "user+parts_full", "user+asset_full", "user+extended")),
    "assets: edit hostname, someone else's": ("POST", "/api/edit/assets/update", "OTHER_HOSTNAME", who("admin", "user+asset_full", "user+extended")),
    "assets: edit contract field, own asset": ("POST", "/api/edit/assets/update", "OWN_CONTRACT", who("admin", "user+asset_full", "user+extended")),
    "assets: edit lifecycle field, own asset": ("POST", "/api/edit/assets/update", "OWN_LIFECYCLE", who("admin", "user+asset_full", "user+extended")),
}


@pytest.fixture()
def assets(sandbox):
    """Two assets with no calls/inward/outward (so archive is allowed), owned by two different made-up engineers."""
    rows = sandbox.execute("""SELECT asset_key FROM asset a WHERE is_current = 1 AND record_level = 'ASSET' AND NOT EXISTS (SELECT 1 FROM svc_call c WHERE c.asset_key = a.asset_key)
                              AND NOT EXISTS (SELECT 1 FROM spare_inward i WHERE i.asset_key = a.asset_key) AND NOT EXISTS (SELECT 1 FROM spare_outward o WHERE o.asset_key = a.asset_key)
                              ORDER BY asset_key LIMIT 2""").fetchall()
    own, other = rows[0][0], rows[1][0]
    sandbox.execute("UPDATE asset SET engineer_name = %s WHERE asset_key = %s", (OWNER, own))
    sandbox.execute("UPDATE asset SET engineer_name = %s WHERE asset_key = %s", (ELSE + "-2", other))
    return {"own": own, "other": other}


def body_for(spec, assets):
    if spec == "OWN_ARCHIVE":
        return {"key": assets["own"], "reason": "permission matrix test"}
    if spec == "OWN_HOSTNAME":
        return {"key": assets["own"], "changes": {"hostname": "MATRIX-HOST"}, "expected": {}}
    if spec == "OTHER_HOSTNAME":
        return {"key": assets["other"], "changes": {"hostname": "MATRIX-HOST"}, "expected": {}}
    if spec == "OWN_CONTRACT":
        return {"key": assets["own"], "changes": {"cover_type": "AMC"}, "expected": {}}
    if spec == "OWN_LIFECYCLE":
        return {"key": assets["own"], "changes": {"vendor_name": "MATRIX VENDOR"}, "expected": {}}
    return spec


CASES = [(persona, action) for action in ACTIONS for persona in PERSONAS]


@pytest.mark.parametrize("persona,action", CASES, ids=[f"{p} | {a}" for p, a in CASES])
def test_permission_matrix(sandbox, monkeypatch, assets, persona, action):
    method, path, spec, allowed = ACTIONS[action]
    u = as_user(monkeypatch, username="MX_" + persona.upper().replace("+", "_"), **{k: v for k, v in PERSONAS[persona].items() if k in ("role", "engineer_key", "read_only")})
    u.update({k: v for k, v in PERSONAS[persona].items() if k in ("group", "extended_access", "asset_access", "call_parts_access", "lead_tools")})
    body = body_for(spec, assets)
    with TestClient(app) as c:
        r = c.get(path) if method == "GET" else c.post(path, json=body, headers=HDR)
    denied = r.status_code in (401, 403)
    assert denied != allowed[persona], f"{persona} -> {method} {path}: got {r.status_code} {r.text[:120]!r}, the matrix says {'allowed' if allowed[persona] else 'forbidden'}"


# ---------------------------------------------------------------- what each account SEES (row scoping), not just whether it may call
def test_register_rows_seen_by_each_kind_of_account(sandbox, monkeypatch, assets):
    def total(persona):
        u = as_user(monkeypatch, username="MXS_" + persona.upper().replace("+", "_"), **{k: v for k, v in PERSONAS[persona].items() if k in ("role", "engineer_key", "read_only")})
        u.update({k: v for k, v in PERSONAS[persona].items() if k in ("group", "extended_access", "asset_access", "call_parts_access", "lead_tools")})
        with TestClient(app) as c:
            return c.get("/api/registers/assets", params={"limit": 1}).json()["total"]
    everything = one(sandbox, "SELECT count(*) n FROM asset WHERE is_current = 1 AND record_level = 'ASSET'")["n"]
    assert total("admin") == everything
    assert total("user+extended") == everything                      # extended access: the whole register
    assert total("user+asset_read") == everything and total("user+asset_full") == everything
    assert total("user") == one(sandbox, "SELECT count(*) n FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND engineer_name = %s", (OWNER,))["n"]
    assert total("user_no_engineer") == 0                            # not linked to an engineer: sees nothing, not an error


def test_reports_offer_only_the_datasets_an_account_may_open(sandbox, monkeypatch):
    expected_calls = {"admin": True, "user+extended": True, "user+parts_read": True, "user+parts_full": True, "user": False, "user+asset_read": False,
                      "user+asset_full": False, "user_no_engineer": False}
    for persona, has_calls in expected_calls.items():
        u = as_user(monkeypatch, username="MXR_" + persona.upper().replace("+", "_"), **{k: v for k, v in PERSONAS[persona].items() if k in ("role", "engineer_key", "read_only")})
        u.update({k: v for k, v in PERSONAS[persona].items() if k in ("group", "extended_access", "asset_access", "call_parts_access", "lead_tools")})
        with TestClient(app) as c:
            datasets = c.get("/api/reports/meta").json()["datasets"]
        assert ("calls" in datasets) == has_calls, persona
        assert "assets" in datasets, persona                          # everyone can report on the assets they are allowed to see


# ---------------------------------------------------------------- a Team Leader/SI cannot use Users and security to climb out of the User group
def test_team_leader_cannot_escalate_through_manage_user(sandbox, monkeypatch):
    mk = lambda name, role, ext=False: sandbox.execute(
        "INSERT INTO portal_user (username, display_name, role, extended_access, password_hash, must_change, created_by) VALUES (%s,%s,%s,%s,'x',FALSE,'test') RETURNING user_id",
        (name, name, role, ext)).fetchone()[0]
    plain, boss, ext_user = mk("MXL_PLAIN", "USER"), mk("MXL_BOSS", "ADMIN"), mk("MXL_EXT", "USER", True)
    me = mk("MXL_ME", "USER")
    u = as_user(monkeypatch, username="MXL_ME", role="USER", engineer_key=ELSE)
    u.update(lead_tools=True, group="USER", user_id=me)
    with TestClient(app) as c:
        post = lambda path, body: c.post(path, json=body, headers=HDR).status_code
        assert post("/api/admin/users/update", {"user_id": plain, "display_name": "RENAMED"}) == 200
        assert post("/api/admin/users/update", {"user_id": plain, "role": "ADMIN"}) == 403          # cannot make anyone an administrator
        assert post("/api/admin/users/update", {"user_id": plain, "extended_access": True}) == 403   # nor give extended access
        assert post("/api/admin/users/update", {"user_id": boss, "display_name": "X"}) == 403        # nor touch an administrator
        assert post("/api/admin/users/update", {"user_id": ext_user, "reset_2fa": True}) == 403      # nor an extended-access account
        assert post("/api/admin/users/reset-password", {"user_id": boss}) == 403                     # nor reset an administrator's password
        assert post("/api/admin/users/update", {"user_id": me, "extended_access": True}) == 403      # nor themselves
        assert post("/api/admin/users/create", {"username": "MXL_NEW", "display_name": "New Person", "role": "ADMIN"}) == 403
        assert post("/api/admin/users/create", {"username": "MXL_NEW", "display_name": "New Person", "role": "USER"}) == 200
    assert one(sandbox, "SELECT role FROM portal_user WHERE user_id = %s", (plain,))["role"] == "USER"
    assert one(sandbox, "SELECT role FROM portal_user WHERE user_id = %s", (boss,))["role"] == "ADMIN"


def test_lead_designation_rule():
    from portal.app import auth
    assert auth.is_lead_designation("TEAM LEADER/SI") and auth.is_lead_designation("Team Leader")
    assert not any(auth.is_lead_designation(d) for d in ("SR SERVER ENGINEER", "NETWORK ENGINEER", "SITE IN-CHARGE", "", None))
