# Deploying the ITAM Portal: portal in a Podman container on the Ubuntu VM, database = PostgreSQL on the physical server

**The database is NOT in a container.** PostgreSQL stays a normal installation on the physical server (fresh install, only the `postgres` password set).
The portal runs as one container inside the Ubuntu VM (which runs on that same server) and reaches the database over the network.

```
 PCs on the LAN ──HTTPS 443──▶  Ubuntu VM ─ Podman ─ itam-portal container   (HTTP 80 just redirects to 443)
                                                        │  PostgreSQL protocol, port 5432, allowed for the VM's address only
                                                        ▼
                                     physical server:  PostgreSQL 18 (database ongc_ank, role ank_app)
```

Two halves, in this order:

| | Where | What |
|---|---|---|
| **A** | on the physical server | `host\Prepare-Database.ps1` creates the role + database, **restores your backup** (the "copy of the database with its data"), and lets the VM connect |
| **B** | on the Ubuntu VM | `vm-install.sh` loads the portal image, checks it can reach the database, installs the systemd unit and starts the portal |

**What was tested** (on the development PC, with Docker; a throw-away PostgreSQL 18 stood in for "a fresh server install"): Part A run under Windows PowerShell 5.1
against a server that had only the `postgres` user - role, database, restore of a real backup (57 tables, 4,781 assets), a second run changing nothing (existing data and
the role's password untouched), the config-file edits with backups; Part B's image against that external database - HTTPS, redirect, sign-in, register, export, QR, a backup made by the portal
over the network, and the connection check with every failure message. **Not testable on the PC:** the Podman/systemd unit (`quadlet/`) and the Windows-only pieces of
`-ConfigureNetwork` (service restart, firewall rule). Read the output of the first run on the real machines.

## A. On the physical server (Windows, PowerShell as Administrator)

You need: PostgreSQL 18 installed (only the `postgres` password set), and the backup file, e.g. `ongc_ank_20260923_231351_manual.dump` (**it contains personal data** - copy it privately).

```powershell
cd <folder with deploy\host>
.\Prepare-Database.ps1 -DumpFile D:\itam\ongc_ank_20260923_231351_manual.dump -VmAddress 192.168.1.50 -ConfigureNetwork
```
`-VmAddress` is the address of the **Ubuntu VM** as the server will see it (or a range like `192.168.1.0/24`). The script asks for the `postgres` password, then:

1. checks the server (version 18, time zone, collation);
2. creates the role **`ank_app`** (login; *not* a superuser) with a generated password, and the database **`ongc_ank`** owned by it - the password is **shown once**: write it down, the VM installer asks for it;
3. restores the backup **only if the database is empty** - it never overwrites data, and running the script again is harmless (the role's password is left alone unless you pass `-AppPassword`);
4. with `-ConfigureNetwork`: sets `listen_addresses`, adds **one** line to `pg_hba.conf` (`host ongc_ank ank_app <VM address>/32 scram-sha-256`), opens **one** Windows-firewall rule for the PostgreSQL port from that address only,
   sets the server time zone to Asia/Kolkata (the portal shows times in the server's time zone), and restarts the PostgreSQL service. `postgresql.conf` and `pg_hba.conf` are backed up first (`*.bak-<time>`).
   Use `-NoRestart` / `-NoFirewall` to do those yourself.

It ends by listing the server's IP addresses - the one the VM can reach is the `DB_HOST` for part B.

**Settings copied from the development database** (everything else was default): role `ank_app` (not superuser), database `ongc_ank` owned by it, UTF8, extension `pg_trgm` (created by the restore),
time zone Asia/Calcutta (= Kolkata), `max_connections` 100. The portal's own settings (security policy, e-mail rules, users) are inside the database and come with the backup.
If the new server's collation differs from the original `English_India.1252` the script warns - text sorting can then differ slightly.

**Linux server instead?** The same steps by hand: create the role and database (`CREATE ROLE ank_app LOGIN PASSWORD '...'; CREATE DATABASE ongc_ank OWNER ank_app;`), `pg_restore -U postgres -d ongc_ank --no-owner --role=ank_app file.dump`,
`listen_addresses = '*'`, one `pg_hba.conf` line as above, firewall for the VM's address, time zone Asia/Kolkata, restart.

**How the VM reaches the server.** With Hyper-V the VM must be able to reach the server's address: an *External* virtual switch (VM on the LAN, the server reachable at its LAN address) is simplest. If the VM is on an *Internal/Default* switch, use the
server's address on that switch as `DB_HOST` and that switch's VM address as `-VmAddress`.

## B. On the Ubuntu VM

Copy the `deploy/` folder to the VM (with `images/itam-portal.tar`). It needs **Podman 4.5+** and systemd (`podman --version`; Ubuntu 24.04 has 4.9), a fixed IP, correct time, and free ports 80/443.

```bash
cd deploy
sudo DB_HOST=192.168.1.10 ./vm-install.sh          # asks for the ank_app password from part A
```
(`DB_HOST` = the physical server's address as seen from the VM. Optional: `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_SSLMODE=require`, `TLS_NAMES="192.168.1.50,itam.local"`.)

It loads the image, stores the password as a Podman secret, **tests the database connection first** (and stops with a plain explanation if it fails - wrong password / not allowed by `pg_hba.conf` / cannot reach it),
installs the systemd unit so the portal starts again after every reboot of the VM, opens ports 80/443 in `ufw` if active, and starts the portal. If the server's PostgreSQL is still starting when the VM boots, the portal simply waits and retries.

Check: `systemctl status itam-portal`, `podman ps`, `curl -k https://localhost/api/health` (→ `{"ok":true}`), then open `https://<VM address>/`.

**First sign-in.** The database is a copy of the live one, so the accounts are the same: the ADMIN account still has its *must change password* state (temporary password from testing) and the 20 roster accounts still have starting passwords - sign in as ADMIN, set a real password, and use *Users and security* to see who has never signed in.
The demo login is switched off on this server (`PORTAL_DEMO_USER=off`).

## Trusting the certificate on the PCs

The portal creates its own HTTPS certificate (valid 10 years) the first time it starts; browsers warn until a PC trusts it (the connection is encrypted either way).
```bash
podman cp itam-portal:/data/tls/portal.crt ./itam-portal.crt        # on the VM, then copy the file to a PC
```
Windows (Administrator PowerShell): `Import-Certificate -FilePath .\itam-portal.crt -CertStoreLocation Cert:\LocalMachine\Root`; for many PCs use Group Policy (*Trusted Root Certification Authorities > Import*). Firefox has its own certificate list.
If the VM's address or name changes, make a new certificate: `sudo ./new-certificate.sh 192.168.10.20,itam.ank.local`, then trust the new file.

## Backups (two places, both matter)

* **The database lives on the physical server**, so the portal's own backups (*Data tools > Backup and restore*, daily at 02:00, newest 14 kept) are made **from the VM over the network** and stored in the VM (`podman exec itam-portal ls /data/backups`).
  A second copy goes to `/var/backups/itam-second-copy` on the VM - mount a second virtual disk or a network share there.
* Also back up the server's PostgreSQL the way you back up the server (and the VM's virtual disk), so a lost VM or a lost server never means lost data. **Test a restore once**: a backup file can be restored with `Prepare-Database.ps1 -DumpFile` into an empty database (e.g. `-DbName ongc_ank_test`).

## Updating - the "Software update" page (like a firmware update)

**Rule: the code moves forward from the development PC; the data never does.** The production database is the master copy, so an update never replaces it.

**How it is made safe.** A release is ONE signed file, `itam-release-<version>.itamrel`. Only files signed with **your private release key** (kept on the development PC, never on a server) are ever installed - an administrator account
alone cannot make the server run other code. The portal (inside its container) only receives and checks the file; a small **update service on the VM** (outside the container) does the real work and re-checks everything itself:
1. verifies the signature and the checksum of the image, 2. **backs the database up**, 3. loads the new image, 4. restarts the portal, 5. waits until the *new* version is healthy - and if it is not, **restores the previous version by itself**.
Each step is shown live on the page and written to a log; nothing is ever half-done silently.

### Each release (three steps, nothing to type on a server)
1. **Development PC:** `.\deploy\build-release.ps1 -Notes "what changed"` - rebuilds, runs the tests (a failing test stops the release), builds the image, signs it. Result: `deploy\releases\itam-release-<version>.itamrel`.
2. **In the portal** (as an administrator): **Data tools > Software update > Choose file > Upload package**. The page checks the file (signature, checksum) and lists it. Alternatively copy the file to `/var/lib/itam-updates/incoming/` on the VM and it appears in the same list.
3. Press **Install...**, read what will happen, type `UPDATE`. Watch the progress on the same page. The portal is unavailable for about a minute; the page reconnects by itself and shows "Updated". If the new version fails, it says "Rolled back" and you are on the old version again.
   *Go back later:* the page has a **Go back to <previous version>** button (typed confirmation, database backed up first).

The same update service can be driven from the VM's command line if the portal is down: `sudo ./update-portal.sh releases/itam-release-<version>.itamrel` (or `--rollback`).

### One-time setup (existing VM: do this once to switch over)
1. **Development PC:** `.\deploy\make-release-key.ps1` (already done on this PC: private key in `%USERPROFILE%\.itam-release\release-private.pem`, public key in `deploy\release-public.pem`). **Back the private key up** (USB stick, safe place). If it is lost, new packages cannot be signed until a new key is made and installed on the VM again.
2. `.\deploy\build-release.ps1` once, and copy the whole `deploy/` folder to the VM (with `release-public.pem`, `updater/`, and `images/itam-portal.tar`).
3. **On the VM, once:** `cd deploy && sudo DB_HOST=10.205.64.46 ./vm-install.sh` - it installs the update service (systemd path unit + heartbeat timer), the release public key, the shared folder `/var/lib/itam-updates`, and loads this first image. The VM needs `python3` and `openssl` (Ubuntu has them).
   After this, every later update is done from the portal page. The page shows *"Update service running"* when the VM side is alive; if it says *"not answering"*, nothing can be installed until it is (`systemctl status itam-updater.path itam-updater-heartbeat.timer`).

### What happens to the database
| Kind of change | What you do |
|---|---|
| **New tables, columns, indexes, triggers, views** (the usual case) | Nothing. They are written into `portal/db/setup.py` (idempotent: only adds, never drops or rewrites) and it runs automatically every time the new version starts. |
| **New data entered on the development PC** | Do **not** restore it over production. Repeat the work on production: *Data tools > Data import* (it takes a safety backup first), or edit in the portal. |
| **A one-off data fix that a release needs** | It ships as a script in the image; I will tell you the exact command, e.g. `sudo podman exec -w /app itam-portal python portal/db/<script>.py`. (None is pending.) |
| **Refreshing a TEST system from a development copy** (never live) | Use an *empty* database: `Prepare-Database.ps1 -DbName ongc_ank_test -DumpFile ...`. The script refuses to restore into a database that already holds data. |

**Safety net on the physical server:** before a big update also run `host\Backup-Database.ps1 -To D:\itam-db-backups` (it makes a dated `pg_dump` and keeps the newest 30) and schedule it daily; the portal's own backups live inside the VM.
Going back to the previous version never needs a database restore, because updates only add to the schema and the previous version ignores new columns.

### Testing the updater (development PC)
The update service is tested against a simulated Podman with real signed packages: `docker run --rm -u 0 -v <project>\deploy\updater:/t --entrypoint bash localhost/itam-portal:latest /t/test/run-tests.sh`
(35 checks: good update, unhealthy new version rolled back, wrong signature, tampered image, same version, failing backup, unloadable image, portal request handled once, path tricks refused, rollback, interrupted run). The portal side is covered by `portal/tests/test_update.py`.

## Moving the VM to another server

`sudo systemctl stop itam-portal && sudo poweroff`, export/import the VM, give it its new IP, then `sudo DB_HOST=<server address> ./vm-install.sh` again (keeps the stored password, rewrites the unit) and `./new-certificate.sh` if the address changed. Run part A on that server for the VM's new address.

## Everyday

```bash
journalctl -u itam-portal -f          # portal log
sudo systemctl restart itam-portal
podman exec itam-portal ls /data/backups
```
If the log says *"reachable but has no inventory tables yet"*, the backup has not been restored on the server (part A) - the portal keeps waiting and starts by itself afterwards.

## Security notes

* Only the VM's address can reach PostgreSQL (`pg_hba.conf` line + firewall rule); the portal connects as the ordinary role `ank_app`, never as `postgres`. Use `DB_SSLMODE=require` (and enable `ssl = on` in `postgresql.conf`) if you want that link encrypted too.
* The database password is a Podman secret - not in the image, not in the unit file. Ports opened on the VM: 443 and 80 (redirect only).
* The portal records every PC's real address in the activity log (rootful Podman does not mask them).
