#!/bin/bash
# Tells the portal's Software update page that the updater is alive: only when it could actually do its job (podman present, release key installed).
DIR="${ITAM_UPDATE_DIR:-/var/lib/itam-updates}"
command -v podman >/dev/null && [ -s "${ITAM_RELEASE_KEY:-/etc/itam/release-public.pem}" ] && [ -d "$DIR" ] && date -Is > "$DIR/agent.heartbeat.tmp" && mv -f "$DIR/agent.heartbeat.tmp" "$DIR/agent.heartbeat" && chmod 644 "$DIR/agent.heartbeat"
exit 0
