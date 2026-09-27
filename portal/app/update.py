"""Software update: the portal's half of "firmware-style" updates.

The portal runs inside a container and cannot (and must not) restart itself, so an update is a hand-over through one shared folder:

    portal (this module)                                  updater on the VM (deploy/updater/*, runs as root, outside the container)
    ----------------------------------------------        -------------------------------------------------------------------
    keeps uploaded release packages in incoming/          watches request.json
    checks a package (structure, checksum, signature)     re-checks EVERYTHING itself (the portal is not trusted with the last word)
    writes request.json when an administrator confirms    backs the database up, loads the image, restarts, waits for health,
    shows status.json / update.log live                   rolls back by itself if the new version does not come up

A release package (*.itamrel, made by deploy/build-release.ps1) is an uncompressed zip of exactly three files:
    manifest.json   {"product","version","built_at","image","size","sha256","notes"}
    manifest.sig    RSA/SHA-256 signature of manifest.json made with the owner's private release key (never on any server)
    itam-portal.tar the container image; its SHA-256 is in the manifest, which the signature covers
Only packages signed by the owner's key are ever installed - an administrator account alone cannot make the server run other code.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

UPDATES = Path(os.environ.get("PORTAL_UPDATE_DIR", "/updates"))
MAX_PACKAGE_BYTES = 2 * 1024 ** 3
PACKAGE_RE = re.compile(r"^itam-release-[A-Za-z0-9._-]{1,40}\.itamrel$")
MEMBERS = {"manifest.json", "manifest.sig", "itam-portal.tar"}
HEARTBEAT_MAX_AGE_S = 180
ACTIVE = ("queued", "running")


class UpdateError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def current_version():
    return os.environ.get("ITAM_VERSION") or "dev"


def enabled():
    return UPDATES.is_dir() and os.access(UPDATES, os.W_OK)


def _incoming():
    d = UPDATES / "incoming"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json_atomic(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- inspecting a package
_inspect_cache = {}


def _sha256_stream(f):
    h = hashlib.sha256()
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
    return h.hexdigest()


def _verify_signature(manifest_bytes, sig_bytes):
    """True/False when a public key is available to the portal (early feedback only - the updater verifies again), None when it is not."""
    pub = UPDATES / "release-public.pem"
    if not pub.is_file() or not shutil.which("openssl"):
        return None
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "m").write_bytes(manifest_bytes)
        (Path(d) / "s").write_bytes(sig_bytes)
        r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(Path(d) / "s"), str(Path(d) / "m")], capture_output=True, timeout=30)
    return r.returncode == 0


def inspect_package(path):
    """Look inside a package without unpacking it. -> {"ok": bool, "problems": [...], "version", "built_at", "size", "notes", "signature": True/False/None}."""
    info = {"ok": False, "problems": [], "version": None, "built_at": None, "size": path.stat().st_size, "notes": None, "signature": None}
    try:
        with zipfile.ZipFile(path) as z:
            names = {i.filename for i in z.infolist()}
            if names != MEMBERS:
                info["problems"].append("this is not a release package (unexpected contents)")
                return info
            m_raw = z.read("manifest.json") if z.getinfo("manifest.json").file_size < 65536 else b""
            manifest = json.loads(m_raw or b"{}")
            for k in ("product", "version", "sha256", "size"):
                if k not in manifest:
                    info["problems"].append(f"manifest lacks '{k}'")
            if manifest.get("product") != "itam-portal":
                info["problems"].append("this package is for a different product")
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", str(manifest.get("version", ""))):
                info["problems"].append("the version label is not valid")
            info.update(version=manifest.get("version"), built_at=manifest.get("built_at"), notes=manifest.get("notes"))
            with z.open("itam-portal.tar") as f:
                actual = _sha256_stream(f)
            if actual != manifest.get("sha256"):
                info["problems"].append("checksum of the image does not match the manifest - the file is damaged or was altered")
            if z.getinfo("itam-portal.tar").file_size != manifest.get("size"):
                info["problems"].append("size of the image does not match the manifest")
            info["signature"] = _verify_signature(m_raw, z.read("manifest.sig"))
            if info["signature"] is False:
                info["problems"].append("the signature is NOT valid - this package was not made with your release key")
    except (zipfile.BadZipFile, ValueError, KeyError, OSError) as e:
        info["problems"].append(f"the file cannot be read as a release package ({type(e).__name__})")
    info["ok"] = not info["problems"]
    return info


def _inspect_cached(path):
    st = path.stat()
    key = (path.name, st.st_size, st.st_mtime_ns)
    if key not in _inspect_cache:
        _inspect_cache.clear() if len(_inspect_cache) > 20 else None
        _inspect_cache[key] = inspect_package(path)
    return _inspect_cache[key]


# ---------------------------------------------------------------- state shown on the page
def _agent():
    hb = UPDATES / "agent.heartbeat"
    try:
        age = time.time() - hb.stat().st_mtime
    except OSError:
        return {"alive": False, "seen_ago_s": None}
    return {"alive": age <= HEARTBEAT_MAX_AGE_S, "seen_ago_s": int(age)}


def _tail(path, n=80):
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


def state():
    if not enabled():
        return {"enabled": False, "current_version": current_version()}
    status = _read_json(UPDATES / "status.json")
    packages = []
    for p in sorted(_incoming().glob("*.itamrel"), key=lambda x: x.stat().st_mtime, reverse=True):
        if PACKAGE_RE.match(p.name):
            packages.append({"name": p.name, **_inspect_cached(p), "uploaded_at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")})
    history = []
    try:
        for line in (UPDATES / "history.jsonl").read_text(encoding="utf-8").splitlines()[-10:]:
            history.append(json.loads(line))
    except (OSError, ValueError):
        pass
    prev = _read_json(UPDATES / "previous.json")
    return {"enabled": True, "current_version": current_version(), "agent": _agent(), "packages": packages, "status": status,
            "request_pending": (UPDATES / "request.json").exists(), "history": list(reversed(history)), "log": _tail(UPDATES / "update.log"),
            "rollback_available": bool(prev and prev.get("version")), "previous_version": (prev or {}).get("version")}


def busy():
    if (UPDATES / "request.json").exists():
        return True
    s = _read_json(UPDATES / "status.json")
    return bool(s and s.get("result") in ACTIVE)


# ---------------------------------------------------------------- actions
class Upload:
    """A package being received. Use as:  u = Upload(name); u.write(chunk) ...; info = u.finish()   (or u.abort())."""

    def __init__(self, filename):
        if not enabled():
            raise UpdateError("Software update is not enabled on this server (the update folder is missing).", 409)
        self.name = Path(filename or "").name
        if not PACKAGE_RE.match(self.name):
            raise UpdateError("That is not a release package. It should be named like itam-release-20260926-1010.itamrel.")
        self.dest = _incoming() / self.name
        if self.dest.exists():
            raise UpdateError("A package with this name is already on the server. Discard it first, or use it as it is.", 409)
        self.free = shutil.disk_usage(_incoming()).free
        self.tmp = _incoming() / (self.name + ".part")
        self.size = 0
        self.f = open(self.tmp, "wb")

    def write(self, chunk):
        self.size += len(chunk)
        if self.size > MAX_PACKAGE_BYTES or self.size > self.free - 300 * 1024 * 1024:
            self.abort()
            raise UpdateError("The file is too large for the free space on the server.", 413)
        self.f.write(chunk)

    def abort(self):
        try:
            self.f.close()
        finally:
            self.tmp.unlink(missing_ok=True)

    def finish(self):
        self.f.close()
        try:
            info = inspect_package(self.tmp)
            if not info["ok"]:
                raise UpdateError("Rejected: " + "; ".join(info["problems"]))
            os.replace(self.tmp, self.dest)
        finally:
            self.tmp.unlink(missing_ok=True)
        return {"name": self.name, **inspect_package(self.dest)}


def save_upload(filename, chunks):
    u = Upload(filename)
    try:
        for c in chunks:
            u.write(c)
    except Exception:
        u.abort()
        raise
    return u.finish()


def _package_path(name):
    if not PACKAGE_RE.match(name or ""):
        raise UpdateError("Unknown package.")
    p = _incoming() / name
    if not p.is_file():
        raise UpdateError("That package is no longer on the server.", 404)
    return p


def discard(name):
    if busy():
        raise UpdateError("An update is in progress.", 409)
    _package_path(name).unlink()
    _inspect_cache.clear()


def request_install(name, user, allow_older=False):
    if not enabled():
        raise UpdateError("Software update is not enabled on this server.", 409)
    if not _agent()["alive"]:
        raise UpdateError("The update service on the VM is not answering, so nothing would happen. Check it (systemctl status itam-updater.path) - see deploy/README.md.", 409)
    if busy():
        raise UpdateError("Another update is already in progress.", 409)
    p = _package_path(name)
    info = inspect_package(p)
    if not info["ok"]:
        raise UpdateError("This package failed its checks: " + "; ".join(info["problems"]))
    cur = current_version()
    if info["version"] == cur:
        raise UpdateError(f"Version {cur} is already the one running.", 409)
    if cur != "dev" and str(info["version"]) < cur and not allow_older:
        raise UpdateError(f"Version {info['version']} is OLDER than the one running ({cur}). Tick the box to install it anyway.", 409)
    rid = uuid.uuid4().hex[:12]
    _write_json_atomic(UPDATES / "status.json", {"id": rid, "result": "queued", "phase": "queued", "message": "Waiting for the update service to pick this up", "version_from": cur,
                                                   "version_to": info["version"], "requested_by": user, "requested_at": _now(), "steps": []})
    _write_json_atomic(UPDATES / "request.json", {"id": rid, "action": "install", "package": name, "version": info["version"], "requested_by": user, "requested_at": _now()})
    return {"id": rid, "version": info["version"]}


def request_rollback(user):
    if not enabled():
        raise UpdateError("Software update is not enabled on this server.", 409)
    if not _agent()["alive"]:
        raise UpdateError("The update service on the VM is not answering, so nothing would happen.", 409)
    if busy():
        raise UpdateError("Another update is already in progress.", 409)
    prev = _read_json(UPDATES / "previous.json")
    if not prev or not prev.get("version"):
        raise UpdateError("There is no earlier version to go back to.", 409)
    rid = uuid.uuid4().hex[:12]
    _write_json_atomic(UPDATES / "status.json", {"id": rid, "result": "queued", "phase": "queued", "message": "Waiting for the update service to pick this up", "version_from": current_version(),
                                                   "version_to": prev["version"], "requested_by": user, "requested_at": _now(), "steps": []})
    _write_json_atomic(UPDATES / "request.json", {"id": rid, "action": "rollback", "version": prev["version"], "requested_by": user, "requested_at": _now()})
    return {"id": rid, "version": prev["version"]}
