#!/bin/bash
# Runs itam-updater.sh against the fake podman/systemctl in ./shims, with real signed packages. Meant to run inside a Linux container:
#   docker run --rm -u 0 -v <project>/deploy/updater:/t --entrypoint bash localhost/itam-portal:latest /t/test/run-tests.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; UPD="$HERE/../itam-updater.sh"; SIMPY="python3 $HERE/sim.py"
export SIM=/sim ITAM_UPDATE_DIR=/sim/updates ITAM_RELEASE_KEY=/sim/keys/release.pub ITAM_LOCK=/sim/lock ITAM_HEALTH_WAIT=7 PATH="$HERE/shims:$PATH"
pass=0; failn=0
ok()  { pass=$((pass+1)); echo "  PASS  $1"; }
bad() { failn=$((failn+1)); echo "  FAIL  $1"; }
check() { if eval "$2"; then ok "$1"; else bad "$1   [$2]"; fi; }
field() { $SIMPY field "$1"; }
mk() { python3 "$HERE/mkpkg.py" "$@"; }
run() { "$UPD" "$@" >/dev/null 2>&1; }

echo "1. a good, signed, healthy package is installed"
$SIMPY reset; mk /sim/pkgs 20260926-1010 yes
run --package /sim/pkgs/itam-release-20260926-1010.itamrel; rc=$?
check "exit code 0" "[ $rc -eq 0 ]"
check "status is success" "[ \"\$(field result)\" = success ]"
check "image :latest is the new version" "[ \"\$($SIMPY running)\" = 20260926-1010 ]"
check "the container runs the new version" "[ \"\$($SIMPY container)\" = 20260926-1010 ]"
check ":previous is the old version" "[ \"\$($SIMPY previous)\" = 20260925-2256 ]"
check "previous.json written" "[ \"\$($SIMPY prev_json)\" = 20260925-2256 ]"
check "history has a success entry" "grep -q success $ITAM_UPDATE_DIR/history.jsonl"
check "the log shows signature OK and the backup" "grep -q 'signature OK' $ITAM_UPDATE_DIR/update.log && grep -q 'backup made' $ITAM_UPDATE_DIR/update.log"

echo "2. a new version that is not healthy is rolled back automatically"
$SIMPY reset; mk /sim/pkgs 20260926-1010 no
run --package /sim/pkgs/itam-release-20260926-1010.itamrel; rc=$?
check "exit code non-zero" "[ $rc -ne 0 ]"
check "status is rolled_back" "[ \"\$(field result)\" = rolled_back ]"
check "the OLD version is running again" "[ \"\$($SIMPY container)\" = 20260925-2256 ]"
check ":latest points to the old version" "[ \"\$($SIMPY running)\" = 20260925-2256 ]"

echo "3. a package signed with the wrong key is refused and nothing changes"
$SIMPY reset; mk /sim/pkgs 20260926-1010 yes badsig
run --package /sim/pkgs/itam-release-20260926-1010.itamrel
check "status is failed" "[ \"\$(field result)\" = failed ]"
check "the message mentions the signature" "field message | grep -qi signature"
check "old version still running" "[ \"\$($SIMPY container)\" = 20260925-2256 ] && [ \"\$($SIMPY running)\" = 20260925-2256 ]"
check "the backup was not even attempted" "! grep -q 'backup made' $ITAM_UPDATE_DIR/update.log"

echo "4. a tampered image fails the checksum"
$SIMPY reset; mk /sim/pkgs 20260926-1010 yes tamper
run --package /sim/pkgs/itam-release-20260926-1010.itamrel
check "status is failed" "[ \"\$(field result)\" = failed ]"
check "old version still running" "[ \"\$($SIMPY running)\" = 20260925-2256 ]"

echo "5. the version that is already running is refused"
$SIMPY reset; mk /sim/pkgs 20260925-2256 yes
run --package /sim/pkgs/itam-release-20260925-2256.itamrel
check "status is failed" "[ \"\$(field result)\" = failed ]"
check "the message says already running" "field message | grep -q 'already the one running'"

echo "6. a failing database backup stops the update before anything is touched"
$SIMPY reset; touch /sim/backup_fail; mk /sim/pkgs 20260926-1010 yes
run --package /sim/pkgs/itam-release-20260926-1010.itamrel
check "status is failed" "[ \"\$(field result)\" = failed ]"
check "the message says the backup failed" "field message | grep -qi backup"
check "old version still running, no :previous created" "[ \"\$($SIMPY running)\" = 20260925-2256 ] && [ \"\$($SIMPY previous)\" = none ]"

echo "6b. an image that cannot be loaded leaves the running version untouched"
$SIMPY reset; mk /sim/pkgs 20260926-1010 yes badload
run --package /sim/pkgs/itam-release-20260926-1010.itamrel
check "status is failed" "[ \"\$(field result)\" = failed ]"
check "old version still running" "[ \"\$($SIMPY running)\" = 20260925-2256 ] && [ \"\$($SIMPY container)\" = 20260925-2256 ]"

echo "7. the request from the portal is processed exactly once and the package is cleaned up"
$SIMPY reset; mk /sim/updates/incoming 20260926-1010 yes
echo '{"id":"req123456789","action":"install","package":"itam-release-20260926-1010.itamrel","version":"20260926-1010","requested_by":"A003541"}' > /sim/updates/request.json
echo '{"id":"req123456789","result":"queued","steps":[]}' > /sim/updates/status.json
run --from-request; rc=$?
check "exit code 0" "[ $rc -eq 0 ]"
check "status is success and keeps the request id" "[ \"\$(field result)\" = success ] && [ \"\$(field id)\" = req123456789 ]"
check "requested_by is recorded" "[ \"\$(field requested_by)\" = A003541 ]"
check "request files are gone" "[ ! -e /sim/updates/request.json ] && [ ! -e /sim/updates/request.processing.json ]"
check "the package was removed after success" "[ ! -e /sim/updates/incoming/itam-release-20260926-1010.itamrel ]"
echo '{"id":"x","action":"install","package":"../../etc/passwd","requested_by":"A003541"}' > /sim/updates/request.json
run --from-request
check "a request naming a path outside incoming/ is refused" "[ \"\$(field result)\" = failed ] && [ ! -e /sim/updates/request.processing.json ]"

echo "8. rollback swaps the two versions; a cut-off run is never repeated automatically"
$SIMPY reset; mk /sim/pkgs 20260926-1010 yes; run --package /sim/pkgs/itam-release-20260926-1010.itamrel
echo '{"id":"rb1","action":"rollback","requested_by":"A003541"}' > /sim/updates/request.json
run --from-request
check "rollback succeeded" "[ \"\$(field result)\" = success ]"
check "the container runs the old version again" "[ \"\$($SIMPY container)\" = 20260925-2256 ]"
check "previous.json now names the version we came from" "[ \"\$($SIMPY prev_json)\" = 20260926-1010 ]"
echo '{"id":"rb2","action":"rollback","requested_by":"A003541"}' > /sim/updates/request.json; echo '{}' > /sim/updates/request.processing.json
run --from-request
check "a leftover 'processing' marker is reported as interrupted, not re-run" "[ \"\$(field result)\" = failed ] && field message | grep -qi interrupted"

echo; echo "updater tests: $pass passed, $failn failed"; [ $failn -eq 0 ]
