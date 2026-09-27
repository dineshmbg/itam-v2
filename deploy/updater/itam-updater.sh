#!/bin/bash
# ITAM Portal updater - runs on the VM as root, OUTSIDE the container. Installed as /opt/itam/itam-updater.sh by vm-install.sh.
#
#   itam-updater.sh --from-request           started by systemd when the portal writes request.json (Software update page)
#   itam-updater.sh --package FILE.itamrel   the same update from the command line (update-portal.sh calls this)
#   itam-updater.sh --rollback               go back to the previous version
#
# It never trusts the portal: whatever the request says, it re-verifies the package (signature with the owner's public key, then the checksum of the image), backs the
# database up, loads the image, restarts the portal, waits until the NEW container is healthy, and rolls back by itself if it is not. Every step is written to
# status.json / update.log in the shared folder, which is what the Software update page shows.
set -uo pipefail

DIR="${ITAM_UPDATE_DIR:-/var/lib/itam-updates}"
PUB="${ITAM_RELEASE_KEY:-/etc/itam/release-public.pem}"
UNIT="${ITAM_UNIT:-itam-portal}"
IMAGE="${ITAM_IMAGE:-localhost/itam-portal}"
HEALTH_WAIT="${ITAM_HEALTH_WAIT:-300}"
STATUS="$DIR/status.json"; LOG="$DIR/update.log"; WORK="$DIR/work"
umask 022

log() { printf '%s  %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG" >&2; }

# ---- status file (JSON edited with python3, atomically)
st() {   # st PHASE "message" [RESULT]
  python3 - "$STATUS" "$1" "$2" "${3:-}" <<'PY'
import json, os, sys, time
path, phase, msg, result = sys.argv[1:5]
try:
    s = json.load(open(path))
except Exception:
    s = {"steps": []}
now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
s.update(phase=phase, message=msg, updated_at=now)
if result:
    s["result"] = result
    if result in ("success", "failed", "rolled_back"):
        s["finished_at"] = now
elif s.get("result") in (None, "queued"):
    s["result"] = "running"
s.setdefault("steps", []).append({"name": phase, "at": now, "note": msg})
tmp = path + ".tmp"
json.dump(s, open(tmp, "w"), indent=2)
os.replace(tmp, path)
PY
}
st_init() {   # st_init ACTION VERSION_FROM VERSION_TO BY   (only when nobody wrote a status yet, i.e. command-line use)
  python3 - "$STATUS" "$@" <<'PY'
import json, os, sys, time
path, action, vfrom, vto, by = sys.argv[1:6]
now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
try:
    s = json.load(open(path))
except Exception:
    s = {}
if s.get("result") not in ("queued", "running") or not s.get("id"):
    s = {"id": os.urandom(6).hex(), "steps": [], "requested_by": by, "requested_at": now}
s.setdefault("requested_by", by)
s.setdefault("requested_at", now)
s.update(result="running", action=action, version_from=vfrom, version_to=vto, started_at=s.get("started_at") or now)
tmp = path + ".tmp"
json.dump(s, open(tmp, "w"), indent=2)
os.replace(tmp, path)
PY
}
history_add() {   # history_add RESULT MESSAGE
  python3 - "$STATUS" "$DIR/history.jsonl" "$1" "$2" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
rec = {k: s.get(k) for k in ("id", "action", "version_from", "version_to", "requested_by", "requested_at", "started_at", "finished_at")}
rec.update(result=sys.argv[3], message=sys.argv[4])
open(sys.argv[2], "a").write(json.dumps(rec) + "\n")
PY
}

label() { podman image inspect --format '{{index .Config.Labels "itam.version"}}' "$1" 2>/dev/null || echo ""; }
running_version() { label "$IMAGE:latest"; }
healthy() {   # healthy IMAGE_ID - the container must be healthy AND started from that image
  local end=$((SECONDS + HEALTH_WAIT))
  while [ $SECONDS -lt $end ]; do
    if podman healthcheck run "$UNIT" >/dev/null 2>&1 && [ "$(podman inspect --format '{{.Image}}' "$UNIT" 2>/dev/null)" = "$1" ]; then return 0; fi
    sleep 3
  done
  return 1
}
fail() {   # fail MESSAGE  (nothing was changed yet)
  log "FAILED: $1"; st failed "$1" failed; history_add failed "$1"; rm -f "$DIR/request.processing.json"; exit 1
}

backup_database() {
  if ! podman container exists "$UNIT"; then log "portal container is not running - no pre-update backup possible"; return 1; fi
  podman exec -w /app "$UNIT" python -c "
from portal.app import backup
r = backup.run_backup('MANUAL', by='updater', note='before software update')
print('backup made:', r['file'], r['size_bytes'], 'bytes')" 2>&1 | tee -a "$LOG" >&2
  return "${PIPESTATUS[0]}"
}

# ---- verify a package and unpack it into $WORK/<id>; sets PKG_TAR, PKG_VERSION
verify_package() {
  local pkg="$1" out
  [ -f "$pkg" ] || fail "package file not found: $pkg"
  [ -s "$PUB" ] || fail "no release key is installed on this VM ($PUB) - updates cannot be trusted; run vm-install.sh with deploy/release-public.pem present"
  local id; id="$(python3 -c 'import json;print(json.load(open("'"$STATUS"'")).get("id",""))' 2>/dev/null)"; id="${id:-cli$$}"
  PKG_DIR="$WORK/$id"; rm -rf "$PKG_DIR"; mkdir -p "$PKG_DIR"
  st verifying "Unpacking and checking the package"
  out="$(python3 - "$pkg" "$PKG_DIR" <<'PY' 2>&1
import sys, zipfile
pkg, dest = sys.argv[1:3]
want = {"manifest.json", "manifest.sig", "itam-portal.tar"}
with zipfile.ZipFile(pkg) as z:
    names = {i.filename for i in z.infolist()}
    if names != want:
        sys.exit("unexpected contents: " + ", ".join(sorted(names ^ want)))
    for n in sorted(want):
        with z.open(n) as src, open(f"{dest}/{n}", "wb") as dst:
            while True:
                b = src.read(1 << 20)
                if not b:
                    break
                dst.write(b)
PY
)" || fail "the package cannot be unpacked ($out)"
  openssl dgst -sha256 -verify "$PUB" -signature "$PKG_DIR/manifest.sig" "$PKG_DIR/manifest.json" >/dev/null 2>&1 \
    || fail "the signature is NOT valid - this package was not made with your release key; nothing was changed"
  log "signature OK"
  out="$(python3 - "$PKG_DIR" <<'PY' 2>&1
import hashlib, json, re, sys
d = sys.argv[1]
m = json.load(open(f"{d}/manifest.json"))
if m.get("product") != "itam-portal":
    sys.exit("wrong product")
if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", str(m.get("version", ""))):
    sys.exit("invalid version label")
h = hashlib.sha256()
n = 0
with open(f"{d}/itam-portal.tar", "rb") as f:
    for b in iter(lambda: f.read(1 << 20), b""):
        h.update(b); n += len(b)
if h.hexdigest() != m.get("sha256") or n != m.get("size"):
    sys.exit("the image does not match the signed manifest (damaged or altered)")
print(m["version"])
PY
)" || fail "checksum check failed: $out"
  PKG_VERSION="$out"; PKG_TAR="$PKG_DIR/itam-portal.tar"
  log "checksum OK - version $PKG_VERSION"
}

do_install() {
  local pkg="$1" by="${2:-console}"
  local cur; cur="$(running_version)"; cur="${cur:-none}"
  st_init install "$cur" "?" "$by"
  verify_package "$pkg"
  st_init install "$cur" "$PKG_VERSION" "$by"
  if [ "$PKG_VERSION" = "$cur" ]; then fail "version $cur is already the one running"; fi

  st backup "Backing up the database first"
  backup_database || fail "the pre-update backup failed - nothing was changed. Is the portal healthy? See the log."

  local old_id; old_id="$(podman image inspect --format '{{.Id}}' "$IMAGE:latest" 2>/dev/null || true)"
  st loading "Loading the new image"
  if [ -n "$old_id" ]; then podman tag "$IMAGE:latest" "$IMAGE:previous"; printf '{"version": "%s"}\n' "$cur" > "$DIR/previous.json"; fi
  if ! podman load -i "$PKG_TAR" >>"$LOG" 2>&1; then
    [ -n "$old_id" ] && podman tag "$IMAGE:previous" "$IMAGE:latest"
    fail "the image could not be loaded - the running version was not touched"
  fi
  local new_id new_label; new_id="$(podman image inspect --format '{{.Id}}' "$IMAGE:latest" 2>/dev/null)"; new_label="$(label "$IMAGE:latest")"
  if [ "$new_label" != "$PKG_VERSION" ]; then
    [ -n "$old_id" ] && podman tag "$IMAGE:previous" "$IMAGE:latest"
    fail "the loaded image says it is version '$new_label', not '$PKG_VERSION' - not installed"
  fi

  st restarting "Restarting the portal on version $PKG_VERSION"
  systemctl restart "$UNIT"
  st health "Waiting for the new version to become healthy (up to ${HEALTH_WAIT}s)"
  if healthy "$new_id"; then
    st done "Updated $cur -> $PKG_VERSION; the portal is healthy" success
    history_add success "Updated $cur -> $PKG_VERSION"
    log "SUCCESS: $cur -> $PKG_VERSION"
    rm -rf "$PKG_DIR"
    [ -n "${INCOMING_PKG:-}" ] && rm -f "$INCOMING_PKG"
    podman image prune -f >/dev/null 2>&1 || true
    return 0
  fi

  log "the new version did not become healthy - rolling back"; journalctl -u "$UNIT" -n 30 --no-pager >>"$LOG" 2>&1 || true
  st rollback "The new version is not healthy - going back to $cur"
  if [ -n "$old_id" ]; then
    podman tag "$IMAGE:previous" "$IMAGE:latest"; systemctl restart "$UNIT"
    if healthy "$old_id"; then
      printf '{"version": "%s"}\n' "$PKG_VERSION" > "$DIR/previous.json"
      st done "Rolled back to $cur: version $PKG_VERSION did not start correctly (see the log). Nothing was lost." rolled_back
      history_add rolled_back "Version $PKG_VERSION failed its health check; back on $cur"
      log "ROLLED BACK to $cur"; return 1
    fi
  fi
  st done "The update failed and the previous version did not come back healthy either - use: journalctl -u $UNIT" failed
  history_add failed "update failed and rollback did not restore a healthy portal"
  return 1
}

do_rollback() {
  local by="${1:-console}"
  local cur; cur="$(running_version)"; cur="${cur:-none}"
  podman image exists "$IMAGE:previous" || { st_init rollback "$cur" "?" "$by"; fail "there is no previous version to go back to"; }
  local prev_label; prev_label="$(label "$IMAGE:previous")"
  st_init rollback "$cur" "$prev_label" "$by"
  st backup "Backing up the database first"
  backup_database || fail "the pre-rollback backup failed - nothing was changed"
  local cur_id prev_id; cur_id="$(podman image inspect --format '{{.Id}}' "$IMAGE:latest")"; prev_id="$(podman image inspect --format '{{.Id}}' "$IMAGE:previous")"
  st restarting "Going back to version $prev_label"
  podman tag "$IMAGE:latest" "$IMAGE:swap"; podman tag "$IMAGE:previous" "$IMAGE:latest"; podman tag "$IMAGE:swap" "$IMAGE:previous"; podman rmi "$IMAGE:swap" >/dev/null 2>&1 || true
  systemctl restart "$UNIT"
  st health "Waiting for version $prev_label to become healthy"
  if healthy "$prev_id"; then
    printf '{"version": "%s"}\n' "$cur" > "$DIR/previous.json"
    st done "Rolled back $cur -> $prev_label; the portal is healthy" success
    history_add success "Rolled back $cur -> $prev_label"; log "ROLLED BACK $cur -> $prev_label"; return 0
  fi
  podman tag "$IMAGE:previous" "$IMAGE:swap"; podman tag "$IMAGE:latest" "$IMAGE:previous"; podman tag "$IMAGE:swap" "$IMAGE:latest"; podman rmi "$IMAGE:swap" >/dev/null 2>&1 || true
  systemctl restart "$UNIT"
  st done "Going back to $prev_label did not work; restored $cur" failed
  history_add failed "rollback to $prev_label did not become healthy"; return 1
}

# ---- main
mkdir -p "$DIR/incoming" "$WORK"
exec 9>"${ITAM_LOCK:-/run/itam-updater.lock}"
flock -n 9 || { echo "another update is already running" >&2; sleep 20; exit 0; }   # the pause keeps systemd's path unit from re-triggering in a tight loop
INCOMING_PKG=""
case "${1:-}" in
  --from-request)
    REQ="$DIR/request.json"; PROC="$DIR/request.processing.json"
    if [ -f "$PROC" ]; then   # a previous run was cut off (VM restarted mid-update): never re-run it automatically
      rm -f "$PROC" "$REQ"; st_init interrupted "?" "?" "system"
      st done "An earlier update was interrupted (the VM restarted). Check the portal and run the update again if needed." failed
      history_add failed "interrupted by a restart"; exit 1
    fi
    [ -f "$REQ" ] || exit 0
    mv "$REQ" "$PROC"
    ACTION="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("action",""))' "$PROC")"
    BY="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("requested_by","portal"))' "$PROC")"
    if [ "$ACTION" = install ]; then
      NAME="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("package",""))' "$PROC")"
      case "$NAME" in itam-release-*.itamrel) ;; *) rm -f "$PROC"; fail "the request names an invalid package";; esac
      case "$NAME" in */*|*..*) rm -f "$PROC"; fail "the request names an invalid package";; esac
      INCOMING_PKG="$DIR/incoming/$NAME"
      do_install "$INCOMING_PKG" "$BY"; rc=$?
    elif [ "$ACTION" = rollback ]; then do_rollback "$BY"; rc=$?
    else rm -f "$PROC"; fail "unknown request '$ACTION'"; fi
    rm -f "$PROC"; exit $rc ;;
  --package) [ -n "${2:-}" ] || { echo "usage: $0 --package FILE.itamrel" >&2; exit 2; }; do_install "$2" "console"; exit $? ;;
  --rollback) do_rollback "console"; exit $? ;;
  *) echo "usage: $0 --from-request | --package FILE.itamrel | --rollback" >&2; exit 2 ;;
esac
