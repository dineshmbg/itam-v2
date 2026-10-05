#!/bin/bash
# Registers the always-on LaunchAgent (equivalent of service/install.ps1). Run after the portal works via run_portal.sh.
set -euo pipefail
P="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$P/portal/service/logs" ~/Library/LaunchAgents
sed "s#__PROJECT__#$P#g" "$P/portal/service/com.ongc.itam-portal.plist.template" > ~/Library/LaunchAgents/com.ongc.itam-portal.plist
launchctl bootout "gui/$(id -u)/com.ongc.itam-portal" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.ongc.itam-portal.plist
echo "Installed. Portal: http://127.0.0.1:8420"
