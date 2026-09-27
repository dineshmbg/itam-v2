#!/bin/bash
# Make a new HTTPS certificate - needed when the server's address or name changes (for example after the VM is moved to the production LAN).
#
#     sudo ./new-certificate.sh 192.168.10.20,itam.ank.local
#
# The names/addresses are what browsers will type. After this, import the new certificate on the PCs again (see README).
set -euo pipefail
NAMES="${1:?usage: new-certificate.sh <address-or-name>[,<address-or-name>...]}"
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
UNIT=/etc/containers/systemd/itam-portal.container
[ -f "$UNIT" ] || { echo "$UNIT not found - run vm-install.sh first" >&2; exit 1; }
systemctl stop itam-portal
podman run --rm --entrypoint sh -v itam-data:/data localhost/itam-portal:latest -c 'rm -f /data/tls/portal.crt /data/tls/portal.key'
sed -i "s#^Environment=PORTAL_TLS_NAMES=.*#Environment=PORTAL_TLS_NAMES=${NAMES}#" "$UNIT"
systemctl daemon-reload
systemctl start itam-portal
echo "new certificate is being created for: $NAMES"
echo "when the portal is up, copy it out with:  podman cp itam-portal:/data/tls/portal.crt ./itam-portal.crt"
