"""Integration tests: run against the real PostgreSQL database (read-only) and the built static files.

  cd portal && .venv\\Scripts\\python -m pytest -q
"""
import re
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from portal.app import config, db, web  # noqa: E402
from portal.app.datasets import DATASETS  # noqa: E402
from portal.app.main import app  # noqa: E402


FAKE_ADMIN = {"user_id": 0, "username": "TEST", "display_name": "TEST", "role": "ADMIN", "state": "ok", "email": None, "active": True, "totp_enabled": False,
              "must_change": False, "locked_until": None, "last_login_at": None, "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc), "engineer_key": None}


@pytest.fixture(scope="module")
def client():
    async def fake_user(request):
        return FAKE_ADMIN
    mp = pytest.MonkeyPatch()
    mp.setattr(web, "current_user", fake_user)     # these tests are about the data API; sign-in itself is covered in test_auth.py
    with TestClient(app) as c:
        yield c
    mp.undo()


def sql_count(sql, params=None):
    return db.one(sql, params)["n"]


# ---------------------------------------------------------------- availability
@pytest.mark.parametrize("path", ["/api/health", "/api/meta", "/api/dash/assets", "/api/dash/calls", "/api/dash/engineers", "/api/integrity", "/api/status", "/api/search?q=elite"])
def test_endpoints_ok(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")


def test_index_and_assets_exist(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', r.text)
    assert refs, "index.html references no static files"
    for ref in refs:
        assert client.get(ref).status_code == 200, ref
    assert "cdn" not in r.text.lower() and "googleapis" not in r.text.lower(), "portal must be fully offline"


# ---------------------------------------------------------------- data integrity
def test_register_totals_match_database(client):
    expected = {
        "assets": sql_count("SELECT count(*) n FROM asset WHERE is_current=1 AND record_level='ASSET'"),
        "calls": sql_count("SELECT count(*) n FROM svc_call WHERE is_current=1"),
        "inward": sql_count("SELECT count(*) n FROM spare_inward WHERE is_current=1"),
        "outward": sql_count("SELECT count(*) n FROM spare_outward WHERE is_current=1"),
        "rma": sql_count("SELECT count(*) n FROM oem_rma WHERE is_current=1"),
    }
    for name, n in expected.items():
        assert client.get(f"/api/registers/{name}?limit=1").json()["total"] == n, name


def test_filter_matches_sql(client):
    total = client.get("/api/registers/assets?f.asset_class=DESKTOP&limit=1").json()["total"]
    assert total == sql_count("SELECT count(*) n FROM asset WHERE is_current=1 AND record_level='ASSET' AND asset_class='DESKTOP'")
    both = client.get("/api/registers/assets?f.asset_class=DESKTOP|LAPTOP&f.cover_status=EXPIRED&limit=1").json()["total"]
    assert both == sql_count("SELECT count(*) n FROM asset WHERE is_current=1 AND record_level='ASSET' AND asset_class IN ('DESKTOP','LAPTOP') AND cover_status='EXPIRED'")


def test_facet_counts_sum_to_total(client):
    d = client.get("/api/registers/calls?limit=1&facets=1").json()
    for key in ("call_status", "priority", "spare_status"):
        assert sum(i["n"] for i in d["facets"][key]) == d["total"], key


def test_facet_counts_follow_other_filters(client):
    d = client.get("/api/registers/calls?limit=1&facets=1&f.call_status=OPEN").json()
    assert d["total"] == sql_count("SELECT count(*) n FROM svc_call WHERE is_current=1 AND call_status='OPEN'")
    # the status facet itself ignores its own selection (so the user can switch), other facets shrink to the open calls
    assert sum(i["n"] for i in d["facets"]["priority"]) == d["total"]


def test_search_is_conjunctive_and_case_insensitive(client):
    a = client.get("/api/registers/assets?q=elite&limit=1").json()["total"]
    b = client.get("/api/registers/assets?q=ELITE hitesh&limit=1").json()["total"]
    assert a >= b > 0
    assert client.get("/api/registers/assets?q=zzzz-no-such-thing&limit=1").json()["total"] == 0


def test_dashboard_kpis_match_database(client):
    a = client.get("/api/dash/assets").json()["kpi"]
    assert a["assets"] == sql_count("SELECT count(*) n FROM asset WHERE is_current=1 AND record_level='ASSET'")
    assert a["cover_expired"] == sql_count("SELECT count(*) n FROM asset WHERE is_current=1 AND cover_status='EXPIRED'")
    c = client.get("/api/dash/calls").json()["kpi"]
    assert c["open"] == sql_count("SELECT count(*) n FROM svc_call WHERE is_current=1 AND call_status='OPEN'")
    assert c["total"] == sql_count("SELECT count(*) n FROM svc_call WHERE is_current=1")


def test_engineer_totals_are_consistent(client):
    e = client.get("/api/dash/engineers").json()
    assert sum(r["calls_open"] for r in e["rows"]) == e["kpi"]["open_calls"]
    # every call and every asset is attributed to a registered engineer (no orphans)
    assert sum(r["calls_total"] for r in e["rows"]) == sql_count("SELECT count(*) n FROM svc_call WHERE is_current=1")
    assert e["kpi"]["assets"] == sql_count("SELECT count(*) n FROM asset WHERE is_current=1 AND record_level='ASSET'")


def test_masters_files_reconcile_with_database(client):
    rep = client.get("/api/integrity").json()
    assert rep["summary"]["fail"] == 0, [c for c in rep["checks"] if c["status"] == "fail"]
    assert all(m["status"] == "pass" for m in rep["masters"]), [m for m in rep["masters"] if m["status"] != "pass"]


# ---------------------------------------------------------------- security
@pytest.mark.parametrize("evil", ["'; DROP TABLE asset; --", "\" OR 1=1 --", "%' OR '1'='1", "\\", "a" * 500, "%_%"])
def test_search_is_injection_safe(client, evil):
    assert client.get("/api/registers/assets", params={"q": evil, "limit": 1}).status_code == 200
    assert client.get("/api/search", params={"q": evil}).status_code == 200
    assert sql_count("SELECT count(*) n FROM asset") > 0


def test_unknown_inputs_are_rejected(client):
    assert client.get("/api/registers/nonsense").status_code == 400
    assert client.get("/api/registers/assets?f.unknown=1").status_code == 400
    assert client.get("/api/registers/assets?sort=id;drop").status_code == 400
    assert client.get("/api/registers/assets?limit=abc").status_code == 400
    assert client.get("/api/registers/assets/DOES-NOT-EXIST").status_code == 404


def test_limit_is_capped(client):
    assert len(client.get("/api/registers/assets?limit=100000").json()["rows"]) <= config.MAX_PAGE


def test_connections_are_read_only():
    import psycopg
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        db.query("UPDATE portal_engineer SET display_name = display_name")
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        db.query("CREATE TEMP TABLE x AS SELECT 1")


def test_personal_data_is_admin_only_on_registers(client, monkeypatch):
    """Personal data (mobile, personal e-mail, DOB) is included in a register's detail JSON for an administrator - matching the hover
    card (cards.py), which already showed the same fields to admins - but stripped for a plain user and never present on dashboards."""
    row = db.one("SELECT asset_key AS n, user_mobile AS m, engineer_name AS eng FROM asset WHERE is_current=1 AND user_mobile IS NOT NULL AND engineer_name IS NOT NULL LIMIT 1")
    key, mob = row["n"], row["m"]
    d = client.get(f"/api/registers/assets/{key}").json()      # client is signed in as an administrator
    assert d["row"].get("user_mobile") == mob
    assert mob not in client.get("/api/dash/assets").text + client.get("/api/dash/engineers").text
    # a plain user linked to the SAME engineer as this asset (so row-level scoping still lets them open it) must not see the personal data
    plain_user = {**FAKE_ADMIN, "role": "USER", "username": "PLAINUSER", "engineer_key": row["eng"]}

    async def fake_plain_user(request):
        return plain_user
    monkeypatch.setattr(web, "current_user", fake_plain_user)
    d2 = client.get(f"/api/registers/assets/{key}").json()
    assert "user_mobile" not in d2["row"]


def test_security_headers(client):
    h = client.get("/").headers
    assert "default-src 'self'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"


# ---------------------------------------------------------------- performance-related behaviour
def test_etag_revalidation_returns_304(client):
    r = client.get("/api/dash/assets")
    r2 = client.get("/api/dash/assets", headers={"If-None-Match": r.headers["etag"]})
    assert r2.status_code == 304 and not r2.content


def test_responses_are_compressed(client):
    r = client.get("/api/registers/assets?limit=200", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"


def test_static_assets_are_immutable_and_html_revalidates(client):
    html = client.get("/").text
    css = re.search(r'href="(/static/assets/app-[A-Z0-9]+\.css)"', html).group(1)
    assert "immutable" in client.get(css).headers["cache-control"]
    assert client.get("/").headers["cache-control"] == "no-cache"


def test_hot_queries_are_fast(client):
    import time
    for path in ["/api/registers/assets?limit=100&facets=1&q=elite", "/api/registers/calls?limit=100&facets=1&f.call_status=OPEN", "/api/search?q=hitesh"]:
        t = time.perf_counter()
        assert client.get(path).status_code == 200
        assert (time.perf_counter() - t) < 0.5, path


# ---------------------------------------------------------------- live updates
def test_hub_flush_invalidates_cache_and_bumps_version():
    import asyncio
    from portal.app.live import Hub

    async def scenario():
        hub = Hub()
        hub.loop = asyncio.get_running_loop()
        q = asyncio.Queue()
        hub.clients.add(q)
        hub.cache_put("k", ("v",))
        assert hub.cache_get("k") is not None
        v0 = hub.version
        hub._on_notify("asset")
        hub._on_notify("svc_call")
        hub._on_notify("asset")            # burst is collapsed into ONE event
        ev = await asyncio.wait_for(q.get(), 2)
        assert ev["tables"] == ["asset", "svc_call"] and ev["version"] == v0 + 1
        assert hub.cache_get("k") is None and q.empty()
    asyncio.run(scenario())


def test_every_dataset_column_and_facet_is_queryable(client):
    for name, ds in DATASETS.items():
        r = client.get(f"/api/registers/{name}?limit=2&facets=1")
        assert r.status_code == 200, (name, r.text)
        assert set(r.json()["facets"]) == {f["key"] for f in ds["facets"]}
        for col in ds["columns"]:
            assert client.get(f"/api/registers/{name}?limit=1&sort={col['key']}").status_code == 200, (name, col["key"])
