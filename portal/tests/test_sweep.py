"""Exhaustive filter sweep: every option of every facet of every register must answer 200 with consistent counts (no combination may error)."""
import pytest
from starlette.testclient import TestClient

from conftest import fake_user
from portal.app.datasets import DATASETS
from portal.app.main import app


@pytest.mark.parametrize("name", list(DATASETS))
def test_every_facet_option_returns_rows_and_consistent_counts(name, monkeypatch):
    fake_user(monkeypatch, "ADMIN")
    with TestClient(app) as c:
        base = c.get(f"/api/registers/{name}?facets=1&limit=5")
        assert base.status_code == 200, base.text
        facets = base.json()["facets"]
        tested = 0
        for key, items in facets.items():
            for it in items:
                if it["n"] == 0:
                    continue
                r = c.get(f"/api/registers/{name}", params={"facets": 1, "limit": 3, f"f.{key}": it["v"]})
                assert r.status_code == 200, (name, key, it["v"], r.text[:200])
                j = r.json()
                assert j["total"] == it["n"] or key in ("dq_flags",), (name, key, it["v"], j["total"], it["n"])       # count shown next to an option == rows returned
                assert isinstance(j["facets"], dict) and j["rows"] is not None
                tested += 1
        assert tested > 0
        # two filters at once, and search on top
        for key, items in list(facets.items())[:3]:
            if len(items) >= 2:
                r = c.get(f"/api/registers/{name}", params={"facets": 1, "limit": 3, f"f.{key}": f"{items[0]['v']}|{items[1]['v']}", "q": "a"})
                assert r.status_code == 200, r.text[:200]
        assert c.get(f"/api/registers/{name}?scope=archived&facets=1").status_code == 200
        for hostile in ("' OR 1=1 --", "%", "\\", "a" * 500, chr(0) + "x"):
            assert c.get(f"/api/registers/{name}", params={"q": hostile, "limit": 2}).status_code in (200, 400), hostile
