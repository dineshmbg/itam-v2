"""Helpers for run-tests.sh:  sim.py reset | field KEY | running | previous | container | prev_json"""
import json
import os
import shutil
import subprocess
import sys

SIM = "/sim"
UPD = os.environ.get("ITAM_UPDATE_DIR", "/sim/updates")


def image(tag):
    try:
        return dict(line.split("=", 1) for line in open(f"{SIM}/images/localhost_itam-portal:{tag}").read().split())
    except OSError:
        return {}


def reset():
    shutil.rmtree(SIM, ignore_errors=True)
    for d in ("keys", "updates/incoming", "pkgs", "container", "images"):
        os.makedirs(f"{SIM}/{d}")
    subprocess.run(["openssl", "genrsa", "-out", f"{SIM}/keys/release.key", "2048"], capture_output=True, check=True)
    subprocess.run(["openssl", "rsa", "-in", f"{SIM}/keys/release.key", "-pubout", "-out", f"{SIM}/keys/release.pub"], capture_output=True, check=True)
    subprocess.run(["openssl", "genrsa", "-out", f"{SIM}/keys/attacker.key", "2048"], capture_output=True, check=True)
    open(f"{SIM}/images/localhost_itam-portal:latest", "w").write("id=AAAA1111\nlabel=20260925-2256\nhealthy=yes\n")
    open(f"{SIM}/container/image", "w").write("AAAA1111")


def container_label():
    cid = open(f"{SIM}/container/image").read().strip()
    for f in os.listdir(f"{SIM}/images"):
        d = dict(line.split("=", 1) for line in open(f"{SIM}/images/{f}").read().split())
        if d["id"] == cid:
            return d["label"]
    return "?"


cmd = sys.argv[1]
if cmd == "reset":
    reset()
elif cmd == "field":
    print(json.load(open(f"{UPD}/status.json")).get(sys.argv[2], ""))
elif cmd == "running":
    print(image("latest").get("label", ""))
elif cmd == "previous":
    print(image("previous").get("label", "none"))
elif cmd == "container":
    print(container_label())
elif cmd == "prev_json":
    print(json.load(open(f"{UPD}/previous.json")).get("version", ""))
