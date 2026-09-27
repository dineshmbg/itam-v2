#!/bin/bash
# Installs the ITAM Portal (one Podman container, managed by systemd) on the Ubuntu VM. The DATABASE is NOT part of this: it is the PostgreSQL
# installed on the physical server, which the container reaches over the network (prepare it first with host/Prepare-Database.ps1).
#
#     sudo DB_HOST=192.168.1.10 ./vm-install.sh
#
# Settings (environment variables; DB_HOST is required, the rest have defaults; the password is asked for unless DB_PASSWORD is set):
#     DB_HOST      address of the physical server as the VM sees it (required)
#     DB_PORT      5432          DB_NAME  ongc_ank          DB_USER  ank_app
#     DB_SSLMODE   prefer       (use "require" if the server's PostgreSQL has SSL switched on)
#     TLS_NAMES    names/addresses the portal's HTTPS certificate must be valid for (default: this VM's addresses)
#
# Loads the image (from ./images/itam-portal.tar if present - offline), stores the database password as a Podman secret, checks that the
# database can be reached and holds the inventory, installs the systemd unit (so the portal starts again after every reboot) and starts it.
# Safe to run again.
set -euo pipefail
cd "$(dirname "$0")"
say() { printf '\n==> %s\n' "$*"; }
die() { printf 'vm-install: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run this with sudo"
command -v podman >/dev/null || die "podman is not installed"
command -v systemctl >/dev/null || die "systemd is required"
ver=$(podman --version | awk '{print $NF}')
[ "$(printf '%s\n4.5.0\n' "$ver" | sort -V | head -1)" = "4.5.0" ] || die "Podman $ver is too old: the systemd (Quadlet) unit needs Podman 4.5 or newer (Ubuntu 24.04 has 4.9)"
DB_HOST="${DB_HOST:-}"; [ -n "$DB_HOST" ] || die "set DB_HOST to the physical server's address, e.g.  sudo DB_HOST=192.168.1.10 ./vm-install.sh"
DB_PORT="${DB_PORT:-5432}"; DB_NAME="${DB_NAME:-ongc_ank}"; DB_USER="${DB_USER:-ank_app}"; DB_SSLMODE="${DB_SSLMODE:-prefer}"

say "Image"
if [ -s images/itam-portal.tar ]; then podman load -i images/itam-portal.tar; else
  podman image exists localhost/itam-portal:latest || { [ -d ../portal ] && podman build -f Containerfile -t localhost/itam-portal:latest .. ; } \
    || die "no image: put images/itam-portal.tar next to this script (podman save), or run from the full project folder to build it"
fi

say "Database password (Podman secret 'itam-db-password')"
if [ -z "${DB_PASSWORD:-}" ]; then
  if podman secret exists itam-db-password && [ "${CHANGE_PASSWORD:-}" != yes ]; then
    echo "  already stored - kept (set CHANGE_PASSWORD=yes to replace it)"
  else
    read -rsp "  password of database role $DB_USER (from Prepare-Database.ps1): " DB_PASSWORD; echo
  fi
fi
if [ -n "${DB_PASSWORD:-}" ]; then
  podman secret rm itam-db-password >/dev/null 2>&1 || true
  printf '%s' "$DB_PASSWORD" | podman secret create itam-db-password - >/dev/null
  echo "  stored"
fi

say "Can this VM reach the database?"
set +e
out=$(podman run --rm -i --secret itam-db-password,type=env,target=PGPASSWORD -e PGHOST="$DB_HOST" -e PGPORT="$DB_PORT" -e PGDATABASE="$DB_NAME" -e PGUSER="$DB_USER" -e PGSSLMODE="$DB_SSLMODE"   --entrypoint python localhost/itam-portal:latest - 2>&1 <<'PY'
import os, sys
import psycopg
try:
    c = psycopg.connect(host=os.environ["PGHOST"], port=os.environ["PGPORT"], dbname=os.environ["PGDATABASE"], user=os.environ["PGUSER"],
                        password=os.environ["PGPASSWORD"], connect_timeout=8)
except Exception as e:
    m = str(e)
    if "password authentication failed" in m:
        why = "the server answered but rejected the password for role " + os.environ["PGUSER"]
    elif "no pg_hba.conf entry" in m:
        why = "the server does not allow connections from this VM's address (pg_hba.conf)"
    elif "does not exist" in m:
        why = "the database or role does not exist on that server"
    else:
        why = "cannot reach %s port %s (not listening, or a firewall blocks it)" % (os.environ["PGHOST"], os.environ["PGPORT"])
    print("FAILED:", why)
    sys.exit(2)
ver = c.execute("select split_part(version(), ' ', 2)").fetchone()[0]
tz = c.execute("show timezone").fetchone()[0]
print(f"CONNECTED: PostgreSQL {ver}, time zone {tz}")
if c.execute("select to_regclass('public.asset')").fetchone()[0]:
    print("INVENTORY:", c.execute("select count(*) from asset where is_current = 1").fetchone()[0], "current assets")
else:
    print("INVENTORY: none - the database is empty (restore the backup with host/Prepare-Database.ps1)")
PY
)
rc=$?
set -e
echo "  $out"
if [ $rc -ne 0 ] || printf '%s' "$out" | grep -q '^FAILED'; then
  cat >&2 <<EOF

The database could not be reached. Common causes:
  * "timeout" / "refused": PostgreSQL is not listening on that address, or the physical server's firewall blocks port $DB_PORT
      -> on the server run  host\\Prepare-Database.ps1 -VmAddress <this VM's address> -ConfigureNetwork
  * "no pg_hba.conf entry": the server does not allow this VM's address (same script fixes it)
  * "password authentication failed": wrong password for role $DB_USER
Nothing has been installed yet. Fix that, then run this script again.
EOF
  exit 1
fi

say "Folders and systemd unit"
install -d -m 755 /var/backups/itam-second-copy /etc/containers/systemd
TLS_NAMES="${TLS_NAMES:-$( { hostname -I 2>/dev/null | tr ' ' '\n'; hostname -f 2>/dev/null; hostname 2>/dev/null; } | awk 'NF && !seen[$0]++' | paste -sd, - )}"
echo "  HTTPS certificate will be valid for: $TLS_NAMES"
install -m 644 quadlet/itam-data.volume /etc/containers/systemd/itam-data.volume
sed -e "s#@TLS_NAMES@#${TLS_NAMES}#" -e "s#@DB_HOST@#${DB_HOST}#" -e "s#@DB_PORT@#${DB_PORT}#" -e "s#@DB_NAME@#${DB_NAME}#" -e "s#@DB_USER@#${DB_USER}#" -e "s#@DB_SSLMODE@#${DB_SSLMODE}#" \
  quadlet/itam-portal.container > /etc/containers/systemd/itam-portal.container
chmod 644 /etc/containers/systemd/itam-portal.container
systemctl daemon-reload

say "Software-update service (used by the portal's Software update page)"
command -v python3 >/dev/null || die "python3 is required on the VM (sudo apt install python3)"
command -v openssl >/dev/null || die "openssl is required on the VM (sudo apt install openssl)"
install -d -m 755 /opt/itam /etc/itam /var/lib/itam-updates /var/lib/itam-updates/incoming
if [ -s release-public.pem ]; then
  install -m 644 release-public.pem /etc/itam/release-public.pem
  install -m 644 release-public.pem /var/lib/itam-updates/release-public.pem
  install -m 755 updater/itam-updater.sh updater/itam-heartbeat.sh /opt/itam/
  for u in itam-updater.path itam-updater.service itam-updater-heartbeat.service itam-updater-heartbeat.timer; do install -m 644 "updater/$u" "/etc/systemd/system/$u"; done
  systemctl daemon-reload
  systemctl enable --now itam-updater.path itam-updater-heartbeat.timer >/dev/null 2>&1
  echo "  update service installed; only packages signed with the key in release-public.pem will ever be installed"
else
  echo "  WARNING: release-public.pem is missing next to this script, so the update service was NOT installed (updates from the portal stay off)."
  echo "           On the development PC run deploy\make-release-key.ps1 once, copy deploy/release-public.pem here, and run this installer again."
fi

say "Starting the portal"
systemctl restart itam-portal
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 443/tcp >/dev/null; ufw allow 80/tcp >/dev/null; echo "  firewall: opened 443 and 80"; fi
ok=""
for i in $(seq 1 40); do podman healthcheck run itam-portal >/dev/null 2>&1 && ok=1 && break; sleep 3; done
ip=$(hostname -I | awk '{print $1}')
if [ -n "$ok" ]; then say "The portal is running: https://$ip/"; else
  say "The portal container is starting but is not healthy yet - look at:  journalctl -u itam-portal -n 50"
fi
cat <<EOF

Next:
  * Browsers warn about the certificate until it is trusted. Copy it to the PCs and import it into "Trusted Root Certification Authorities":
        podman cp itam-portal:/data/tls/portal.crt ./itam-portal.crt
  * The portal starts by itself after a reboot of the VM:  systemctl status itam-portal      (if the physical server's PostgreSQL is still
    starting it simply waits and retries)
  * Logs:  journalctl -u itam-portal -f          Backups: podman exec itam-portal ls /data/backups   (copies in /var/backups/itam-second-copy)
EOF
