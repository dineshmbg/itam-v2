"""Engineer documents: who may see/upload, file checks, replace/remove keep the old bytes, rename follows. Rolled back (see conftest.box)."""
import pytest
from starlette.testclient import TestClient

from conftest import HDR, fake_user
from portal.app import edit, engdocs
from portal.app.main import app

PDF = b"%PDF-1.4\n" + b"x" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"y" * 50
UP = {"X-Requested-With": "itam-portal"}


@pytest.fixture()
def eng(box):
    keys = [r[0] for r in box.execute("SELECT engineer_key FROM portal_engineer ORDER BY engineer_key LIMIT 2").fetchall()]
    assert len(keys) == 2
    box.execute("DELETE FROM portal_engineer_doc")
    return keys


def up(c, key, data=PDF, typ="CANCELLED_CHEQUE", **h):
    return c.post(f"/api/engineers/{key}/docs/upload", content=data, headers={**UP, "X-Doc-Type": typ, "X-Filename": "scan.pdf", **h})


def test_own_only_for_a_plain_engineer(monkeypatch, eng):
    mine, other = eng
    fake_user(monkeypatch, role="USER", username="E1", engineer_key=mine)
    c = TestClient(app)
    assert up(c, mine).status_code == 200
    assert up(c, other).status_code == 403
    assert c.get(f"/api/engineers/{other}/docs").status_code == 403
    d = c.get(f"/api/engineers/{mine}/docs").json()
    assert len(d["docs"]) == 1 and d["can_remove"] is False
    r = c.get(f"/api/engineers/{mine}/docs/{d['docs'][0]['doc_id']}/file")
    assert r.status_code == 200 and r.content == PDF and "attachment" in r.headers["content-disposition"]
    assert c.post(f"/api/engineers/{mine}/docs/remove", json={"doc_id": d["docs"][0]["doc_id"]}, headers=HDR).status_code == 403


def test_admin_and_team_lead_see_everyone(monkeypatch, eng):
    mine, other = eng
    u = fake_user(monkeypatch, role="USER", username="E1", engineer_key=mine)
    c = TestClient(app)
    assert up(c, other).status_code == 403
    u["lead_tools"] = True
    assert up(c, other).status_code == 200
    u.update(lead_tools=False, role="ADMIN")
    assert c.get(f"/api/engineers/{other}/docs").status_code == 200


def test_read_only_demo_account_is_refused(monkeypatch, eng):
    u = fake_user(monkeypatch, role="ADMIN")
    u["read_only"] = True
    c = TestClient(app)
    assert c.get(f"/api/engineers/{eng[0]}/docs").status_code == 403
    assert up(c, eng[0]).status_code == 403


def test_file_checks(monkeypatch, eng):
    fake_user(monkeypatch, role="ADMIN")
    c = TestClient(app)
    k = eng[0]
    assert up(c, k, b"MZ\x90 not a document").status_code == 415
    assert up(c, k, b"").status_code == 400
    assert up(c, k, PDF + b"z" * engdocs.MAX_BYTES).status_code in (400, 413)
    assert up(c, k, typ="OTHER").status_code == 400                     # a label is required
    assert up(c, k, typ="NOPE").status_code == 400
    assert up(c, k, X_Expiry="x").status_code in (200, 400)
    assert up(c, k, **{"X-Expiry": "not-a-date"}).status_code == 400
    assert up(c, "NO SUCH PERSON").status_code == 404


def test_replace_and_remove_keep_the_old_file(monkeypatch, eng, box):
    fake_user(monkeypatch, role="ADMIN")
    c = TestClient(app)
    k = eng[0]
    up(c, k)
    up(c, k, PNG, **{"X-Filename": "new.png", "X-Expiry": "2020-01-01"})
    d = c.get(f"/api/engineers/{k}/docs").json()["docs"]
    cur = [x for x in d if not x["superseded_at"]]
    assert len(d) == 2 and len(cur) == 1 and cur[0]["mime"] == "image/png" and cur[0]["expiry_status"] == "EXPIRED"
    assert box.execute("SELECT count(*) FROM portal_engineer_doc WHERE content IS NOT NULL").fetchone()[0] == 2
    assert c.post(f"/api/engineers/{k}/docs/remove", json={"doc_id": cur[0]["doc_id"]}, headers=HDR).status_code == 200
    assert not [x for x in c.get(f"/api/engineers/{k}/docs").json()["docs"] if not x["superseded_at"]]
    up(c, k, typ="OTHER", **{"X-Label": "EPFO"}); up(c, k, typ="OTHER", **{"X-Label": "ID PROOF"})
    assert len([x for x in c.get(f"/api/engineers/{k}/docs").json()["docs"] if not x["superseded_at"]]) == 2   # different labels coexist


def test_rename_moves_the_documents(monkeypatch, eng, box):
    fake_user(monkeypatch, role="ADMIN")
    c = TestClient(app)
    k = eng[0]
    up(c, k)
    new = "ZZ DOC TEST PERSON"
    edit.rename_engineer(k, new, "T", "127.0.0.1")
    assert box.execute("SELECT count(*) FROM portal_engineer_doc WHERE engineer_key = %s", (new,)).fetchone()[0] == 1
