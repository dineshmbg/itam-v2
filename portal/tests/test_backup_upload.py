"""Uploading a dump made on another machine (e.g. the production portal's Back up now)."""
import shutil

import pytest
from starlette.testclient import TestClient

from portal.app import backup
from portal.app.main import app
from test_scoping import as_user, sandbox  # noqa: F401  (sandbox is a fixture)

UP = {"X-Requested-With": "itam-portal", "Content-Type": "application/octet-stream", "X-Filename": "prod.dump"}


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path)
    return tmp_path


def post(c, body, headers=UP):
    return c.post("/api/admin/backups/upload", content=body, headers=headers)


def test_refusals_leave_nothing_behind(sandbox, folder, monkeypatch):
    as_user(monkeypatch, "ADMIN", username="BKU_A")
    with TestClient(app) as c:
        assert "not a PostgreSQL" in post(c, b"select 1;").json()["error"]       # plain SQL / junk
        assert post(c, b"PGDMP-truncated").status_code in (400, 422)             # magic ok, archive unreadable
    assert list(folder.iterdir()) == []


def test_only_a_full_administrator_may_upload(sandbox, folder, monkeypatch):
    as_user(monkeypatch, "USER", username="BKU_U")
    with TestClient(app) as c:
        assert post(c, b"PGDMP").status_code == 403
    assert list(folder.iterdir()) == []


def test_real_dump_is_listed_as_uploaded(sandbox, folder, monkeypatch):
    src = sorted(backup.config.PROJECT_ROOT.joinpath("backups").glob("*_auto.dump"))
    if not src:
        pytest.skip("no portal backup on this machine")
    as_user(monkeypatch, "ADMIN", username="BKU_B")
    with TestClient(app) as c:
        r = post(c, src[-1].read_bytes())
        assert r.status_code == 200, r.text
        name = r.json()["file"]
        assert name.endswith("_uploaded.dump") and (folder / name).exists()
        assert [b["kind"] for b in c.get("/api/admin/backups").json()["backups"] if b["file"] == name] == ["UPLOADED"]
        assert backup.verify(name)["ok"]
