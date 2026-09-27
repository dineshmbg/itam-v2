#!/bin/bash
# Command-line way to install a release package (the Software update page in the portal does the same thing). Both use the same updater, which
# checks the signature and checksum, backs the database up, installs, health-checks and rolls back by itself.
#
#     sudo ./update-portal.sh [releases/itam-release-<version>.itamrel]     (default: the newest .itamrel in ./releases or ./images)
#     sudo ./update-portal.sh --rollback                                     go back to the previous version
set -euo pipefail
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo "update-portal: run this with sudo" >&2; exit 1; }
[ -x /opt/itam/itam-updater.sh ] || { echo "update-portal: the update service is not installed - run vm-install.sh first (with release-public.pem next to it)" >&2; exit 1; }
if [ "${1:-}" = "--rollback" ]; then exec /opt/itam/itam-updater.sh --rollback; fi
PKG="${1:-$(ls -t releases/itam-release-*.itamrel images/itam-release-*.itamrel 2>/dev/null | head -1)}"
[ -n "$PKG" ] && [ -f "$PKG" ] || { echo "update-portal: no release package found - give the path of an itam-release-*.itamrel file" >&2; exit 1; }
case "$PKG" in *.tar) echo "update-portal: raw image files are no longer installed - use the signed .itamrel package made by build-release.ps1" >&2; exit 1;; esac
exec /opt/itam/itam-updater.sh --package "$PKG"
