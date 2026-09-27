"""Software update: package checks, the hand-over folder, and the routes. Uses a temporary update folder and a throw-away signing key; nothing real is touched."""
import hashlib
import json
import shutil
import subprocess
import time
import zipfile

import pytest
from starlette.testclient import TestClient

from portal.app import update
from portal.app.main import app
from test_scoping import HDR, as_user, sandbox  # noqa: F401  (sandbox is a fixture)

OPENSSL = shutil.which("openssl")
needs_openssl = pytest.mark.skipif(not OPENSSL, reason="openssl is needed to sign a test package")


def make_key(folder, name):
    priv, pub = folder / f"{name}.key", folder / f"{name}.pub"
    subprocess.run([OPENSSL, "genrsa", "-out", str(priv), "2048"], check=True, capture_output=True)
    subprocess.run([OPENSSL, "rsa", "-in", str(priv), "-pubout", "-out", str(pub)], check=True, capture_output=True)
    return priv, pub


def make_package(folder, version="20260926-1010", key=None, tamper=False, extra=None, product="itam-portal"):
    """Builds a release package exactly like deploy/build-release.ps1 does."""
    tar = b"pretend this is a container image " * 1000
    manifest = {"product": product, "version": version, "built_at": "2026-09-26T10:10:00", "image": "itam-portal.tar", "size": len(tar), "sha256": hashlib.sha256(tar).hexdigest(), "notes": "test"}
    mbytes = json.dumps(manifest, indent=2).encode()
    mf = folder / "manifest.json"
    mf.write_bytes(mbytes)
    sig = b"no-key"
    if key:
        sf = folder / "manifest.sig"
        subprocess.run([OPENSSL, "dgst", "-sha256", "-sign", str(key), "-out", str(sf), str(mf)], check=True, capture_output=True)
        sig = sf.read_bytes()
    path = folder / f"itam-release-{version}.itamrel"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        z.writestr("manifest.json", mbytes)
        z.writestr("manifest.sig", sig)
        z.writestr("itam-portal.tar", tar + (b"X" if tamper else b""))
        for n in (extra or []):
            z.writestr(n, b"x")
    return path


@pytest.fixture()
def upd(tmp_path, monkeypatch):
    d = tmp_path / "updates"
    (d / "incoming").mkdir(parents=True)
    monkeypatch.setattr(update, "UPDATES", d)
    monkeypatch.setattr(update, "_inspect_cache", {})
    monkeypatch.setenv("ITAM_VERSION", "20260925-2256")
    return d


def heartbeat(d):
    (d / "agent.heartbeat").write_text("alive")


@needs_openssl
def test_a_correctly_signed_package_is_accepted_and_described(upd, tmp_path):
    priv, pub = make_key(tmp_path, "release")
    shutil.copy(pub, upd / "release-public.pem")
    info = update.inspect_package(make_package(tmp_path, key=priv))
    assert info["ok"] and info["version"] == "20260926-1010" and info["signature"] is True and info["problems"] == []


@needs_openssl
def test_a_package_signed_with_another_key_is_rejected(upd, tmp_path):
    _, pub = make_key(tmp_path, "release")
    other_priv, _ = make_key(tmp_path, "attacker")
    shutil.copy(pub, upd / "release-public.pem")
    info = update.inspect_package(make_package(tmp_path, key=other_priv))
    assert not info["ok"] and info["signature"] is False and any("signature" in p for p in info["problems"])


def test_a_tampered_image_fails_the_checksum(upd, tmp_path):
    info = update.inspect_package(make_package(tmp_path, tamper=True))
    assert not info["ok"] and any("checksum" in p for p in info["problems"])


def test_a_package_with_extra_files_or_the_wrong_product_is_rejected(upd, tmp_path):
    assert not update.inspect_package(make_package(tmp_path, extra=["evil.sh"]))["ok"]
    assert not update.inspect_package(make_package(tmp_path, product="something-else"))["ok"]
    junk = tmp_path / "itam-release-1.itamrel"
    junk.write_bytes(b"not a zip")
    assert not update.inspect_package(junk)["ok"]


def test_upload_rejects_bad_names_and_bad_content_and_keeps_good_packages(upd, tmp_path):
    with pytest.raises(update.UpdateError):
        update.save_upload("../../etc/passwd", [b"x"])
    with pytest.raises(update.UpdateError):
        update.save_upload("itam-release-1.itamrel", [b"not a zip"])
    assert list((upd / "incoming").iterdir()) == []                                 # nothing half-written is left behind
    good = make_package(tmp_path)
    out = update.save_upload(good.name, [good.read_bytes()[i:i + 4096] for i in range(0, good.stat().st_size, 4096)])
    assert out["ok"] and (upd / "incoming" / good.name).exists()
    with pytest.raises(update.UpdateError):                                          # the same name twice
        update.save_upload(good.name, [good.read_bytes()])


def test_install_needs_a_live_updater_and_refuses_the_running_or_an_older_version(upd, tmp_path):
    pkg = make_package(tmp_path, version="20260926-1010")
    update.save_upload(pkg.name, [pkg.read_bytes()])
    with pytest.raises(update.UpdateError) as e:
        update.request_install(pkg.name, "ADMIN")
    assert "not answering" in str(e.value)                                           # no heartbeat -> nothing would happen, so say so
    heartbeat(upd)
    older = make_package(tmp_path, version="20260901-0900")
    update.save_upload(older.name, [older.read_bytes()])
    with pytest.raises(update.UpdateError) as e:
        update.request_install(older.name, "ADMIN")
    assert "OLDER" in str(e.value)
    same = make_package(tmp_path, version="20260925-2256")
    update.save_upload(same.name, [same.read_bytes()])
    with pytest.raises(update.UpdateError) as e:
        update.request_install(same.name, "ADMIN")
    assert "already the one running" in str(e.value)


def test_install_writes_one_request_and_blocks_a_second_until_it_finishes(upd, tmp_path):
    heartbeat(upd)
    pkg = make_package(tmp_path)
    update.save_upload(pkg.name, [pkg.read_bytes()])
    out = update.request_install(pkg.name, "A003541")
    req = json.loads((upd / "request.json").read_text())
    assert req["action"] == "install" and req["package"] == pkg.name and req["version"] == out["version"] and req["requested_by"] == "A003541"
    assert json.loads((upd / "status.json").read_text())["result"] == "queued"
    with pytest.raises(update.UpdateError):
        update.request_install(pkg.name, "A003541")
    with pytest.raises(update.UpdateError):
        update.discard(pkg.name)                                                     # cannot delete the package while it is being installed
    (upd / "request.json").unlink()                                                  # the updater took it ...
    (upd / "status.json").write_text(json.dumps({"result": "success"}))              # ... and finished
    update.discard(pkg.name)
    assert not (upd / "incoming" / pkg.name).exists()


def test_rollback_needs_a_previous_version(upd):
    heartbeat(upd)
    with pytest.raises(update.UpdateError):
        update.request_rollback("ADMIN")
    (upd / "previous.json").write_text(json.dumps({"version": "20260901-0900"}))
    assert update.request_rollback("ADMIN")["version"] == "20260901-0900"
    assert json.loads((upd / "request.json").read_text())["action"] == "rollback"


def test_state_reports_agent_packages_history_and_log(upd, tmp_path):
    assert update.state()["agent"]["alive"] is False
    heartbeat(upd)
    pkg = make_package(tmp_path)
    update.save_upload(pkg.name, [pkg.read_bytes()])
    (upd / "history.jsonl").write_text(json.dumps({"result": "success", "version_to": "1"}) + "\n")
    (upd / "update.log").write_text("line one\nline two\n")
    st = update.state()
    assert st["enabled"] and st["agent"]["alive"] and st["packages"][0]["name"] == pkg.name and st["history"][0]["result"] == "success" and st["log"][-1] == "line two"
    assert st["current_version"] == "20260925-2256"


def test_disabled_when_the_folder_does_not_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "UPDATES", tmp_path / "nope")
    assert update.state() == {"enabled": False, "current_version": update.current_version()}
    with pytest.raises(update.UpdateError):
        update.Upload("itam-release-1.itamrel")


# ---------------------------------------------------------------- HTTP
def test_http_upload_start_and_permissions(sandbox, upd, tmp_path, monkeypatch):
    heartbeat(upd)
    pkg = make_package(tmp_path)
    raw = pkg.read_bytes()
    up = {"X-Requested-With": "itam-portal", "Content-Type": "application/octet-stream", "X-Filename": pkg.name}
    as_user(monkeypatch, "USER", username="UPD_USER")
    with TestClient(app) as c:
        assert c.post("/api/admin/update/upload", content=raw, headers=up).status_code == 403          # a plain User may not
        assert c.get("/api/admin/update").status_code == 403
    as_user(monkeypatch, "ADMIN", username="UPD_DEMO", read_only=True)
    with TestClient(app) as c:
        assert c.post("/api/admin/update/upload", content=raw, headers=up).status_code == 403          # the read-only demo account may not
    as_user(monkeypatch, "ADMIN", username="UPD_ADMIN")
    with TestClient(app) as c:
        r = c.post("/api/admin/update/upload", content=raw, headers=up)
        assert r.status_code == 200 and r.json()["version"] == "20260926-1010", r.text
        assert c.post("/api/admin/update/upload", content=b"junk", headers={**up, "X-Filename": "evil.exe"}).status_code == 400
        s = c.get("/api/admin/update").json()
        assert s["enabled"] and s["packages"][0]["name"] == pkg.name and s["agent"]["alive"]
        assert c.post("/api/admin/update/start", json={"package": pkg.name}, headers=HDR).status_code == 400   # the confirmation word is required
        r = c.post("/api/admin/update/start", json={"package": pkg.name, "confirm": "UPDATE"}, headers=HDR)
        assert r.status_code == 200, r.text
        assert (upd / "request.json").exists()
        assert c.post("/api/admin/update/start", json={"package": pkg.name, "confirm": "UPDATE"}, headers=HDR).status_code == 409
