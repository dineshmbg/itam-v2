"""mkpkg.py OUTDIR VERSION HEALTHY(yes|no) [tamper|badsig|badload] - builds a release package like deploy/build-release.ps1, signed with /sim/keys/release.key"""
import hashlib
import json
import subprocess
import sys
import zipfile

out, version, healthy = sys.argv[1], sys.argv[2], sys.argv[3]
mode = sys.argv[4] if len(sys.argv) > 4 else ""
fake = f"ID={hashlib.sha256(version.encode()).hexdigest()}\nLABEL={version}\nHEALTHY={healthy}\n" + ("BAD=load\n" if mode == "badload" else "") + "padding " * 500
tar = fake.encode()
m = {"product": "itam-portal", "version": version, "built_at": "2026-09-26T10:00:00", "image": "itam-portal.tar", "size": len(tar), "sha256": hashlib.sha256(tar).hexdigest(), "notes": ""}
open("/tmp/manifest.json", "w").write(json.dumps(m, indent=2))
key = "/sim/keys/attacker.key" if mode == "badsig" else "/sim/keys/release.key"
subprocess.run(["openssl", "dgst", "-sha256", "-sign", key, "-out", "/tmp/manifest.sig", "/tmp/manifest.json"], check=True)
with zipfile.ZipFile(f"{out}/itam-release-{version}.itamrel", "w", zipfile.ZIP_STORED) as z:
    z.write("/tmp/manifest.json", "manifest.json")
    z.write("/tmp/manifest.sig", "manifest.sig")
    z.writestr("itam-portal.tar", tar + (b"TAMPER" if mode == "tamper" else b""))
