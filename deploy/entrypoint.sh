#!/bin/sh
# ITAM Portal container start-up:
#   1. make the /data folders, 2. create a TLS certificate the first time, 3. wait for PostgreSQL and for the restored database,
#   4. run the (idempotent) database preparation, 5. start the portal over HTTPS.
set -eu
D=/data
mkdir -p "$D/home" "$D/tls" "$D/backups" "$D/masters" "$D/uploads"

# ---- TLS certificate (kept in /data/tls; delete the two files and restart to make a new one, e.g. after the server's address changes)
if [ ! -s "$D/tls/portal.crt" ] || [ ! -s "$D/tls/portal.key" ]; then
  NAMES="${PORTAL_TLS_NAMES:-localhost}"
  echo "entrypoint: creating a TLS certificate for: $NAMES"
  san=""; cn=""
  OLDIFS=$IFS; IFS=','
  for n in $NAMES; do
    n=$(printf '%s' "$n" | tr -d ' ')
    [ -z "$n" ] && continue
    [ -z "$cn" ] && cn="$n"
    case "$n" in
      *:*) san="${san}IP:$n," ;;
      *[!0-9.]*) san="${san}DNS:$n," ;;
      *) san="${san}IP:$n," ;;
    esac
  done
  IFS=$OLDIFS
  san="${san%,}"
  openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 3650 \
    -keyout "$D/tls/portal.key" -out "$D/tls/portal.crt" \
    -subj "/O=ITAM Portal/CN=${cn:-localhost}" \
    -addext "subjectAltName=${san:-DNS:localhost}" -addext "keyUsage=digitalSignature,keyEncipherment" -addext "extendedKeyUsage=serverAuth" 2>/dev/null
  chmod 600 "$D/tls/portal.key"
fi

# ---- wait for PostgreSQL and for the restored inventory tables (the asset/call tables come from a restored backup, see deploy/README.md)
python - <<'PY'
import os, sys, time
import psycopg
info = dict(host=os.environ.get("PGHOST", "localhost"), port=os.environ.get("PGPORT", "5432"), dbname=os.environ.get("PGDATABASE", "ongc_ank"),
            user=os.environ.get("PGUSER", "ank_app"), password=os.environ.get("PGPASSWORD"), connect_timeout=5)
deadline = time.time() + 60 * 30
said = None
while True:
    try:
        with psycopg.connect(**info) as con:
            if con.execute("SELECT to_regclass('public.asset')").fetchone()[0]:
                print("entrypoint: database is ready", flush=True)
                break
            msg = "PostgreSQL is reachable but has no inventory tables yet - restore the backup on the server (deploy/host/Prepare-Database.ps1 -DumpFile ...). Waiting..."
    except Exception as e:  # noqa: BLE001
        msg = f"waiting for PostgreSQL at {info['host']}: {str(e).splitlines()[0][:120]}"
    if msg != said:
        print("entrypoint:", msg, flush=True)
        said = msg
    if time.time() > deadline:
        sys.exit("entrypoint: gave up waiting for the database after 30 minutes")
    time.sleep(5)
PY

python /app/portal/db/setup.py
exec python /app/portal/serve.py --host "${PORTAL_HOST:-0.0.0.0}" --port "${PORTAL_PORT:-8443}" \
  --redirect-port "${PORTAL_REDIRECT_PORT:-0}" --tls-cert "$D/tls/portal.crt" --tls-key "$D/tls/portal.key"
