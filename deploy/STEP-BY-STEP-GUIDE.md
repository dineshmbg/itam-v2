# ITAM Portal - step-by-step guide: deploy and update on the production servers

Every step is tagged with **where you run it**:

| Tag | Machine | How you work on it |
|---|---|---|
| **[DEV PC]** | Your development PC (this project, Windows 11) | PowerShell, in `C:\Users\dines\Documents\project-claude` |
| **[SERVER]** | The physical production server - Windows Server 2019, **10.205.64.46** (PostgreSQL 18 runs here, and Hyper-V hosts the VM) | Remote Desktop (RDP), **PowerShell as Administrator** |
| **[VM]** | The Ubuntu VM `itam-ubuntu-production` on that server - **10.205.64.25** (Podman runs the portal here) | SSH from the DEV PC: `ssh <vm-user>@10.205.64.25` |
| **[BROWSER]** | Any PC on the LAN | Chrome / Edge, address `https://10.205.64.25/` |

Replace `<vm-user>` with the Linux user you use on the VM.

---

## 0. The picture, and what lives where

```
 [DEV PC]                        [SERVER] 10.205.64.46                          [VM] 10.205.64.25
 build + sign a release   -->    PostgreSQL 18  <-- port 5432, this VM only -- Podman: container "itam-portal"  <-- HTTPS 443 -- PCs on the LAN
 (Docker Desktop)                database ongc_ank (ALL the data lives here)    update service (systemd, root)
                                 Hyper-V hosts the VM                            shared folder /var/lib/itam-updates
```

| Thing | Where it is | Notes |
|---|---|---|
| **The data (database `ongc_ank`)** | **[SERVER]**, in PostgreSQL | The master copy. Updates never replace it. |
| **The portal program** | **[VM]**, in the container `itam-portal` | This is what an update replaces. |
| **The update service** | **[VM]**, systemd units `itam-updater.path` + `itam-updater-heartbeat.timer`, scripts in `/opt/itam/` | Installed once by `vm-install.sh`. Does the safe steps of an update. |
| **Release signing key (PRIVATE)** | **[DEV PC]** `C:\Users\dines\.itam-release\release-private.pem` | **Back it up (USB stick, safe place). Never copy it to a server.** |
| **Release public key** | `deploy\release-public.pem` -> **[VM]** `/etc/itam/release-public.pem` | The VM only installs packages signed by your private key. |

**Passwords you will meet - keep them in your password manager, not in files:**

| Password | Used for | Where set |
|---|---|---|
| PostgreSQL `postgres` password | Administering the database on the server | [SERVER] - set when PostgreSQL was installed |
| PostgreSQL `ank_app` password | The portal container logging in to the database | Created by `Prepare-Database.ps1` (shown once); stored on the VM as a Podman secret |
| Portal logins (`A003541`, `ADMIN`, ...) | People signing in to the portal | Inside the database (see Part 5) |
| VM Linux user (sudo) password | Working on the VM | The VM |

---

## 1. Before you start (checklist)

- [ ] You can RDP to **[SERVER]** as an Administrator, and SSH to **[VM]** (`ssh <vm-user>@10.205.64.25`).
- [ ] On **[DEV PC]**: Docker Desktop is installed and starts ("Engine running"), and Git for Windows is installed (it provides `openssl`).
- [ ] The release key exists on the DEV PC: `Test-Path $env:USERPROFILE\.itam-release\release-private.pem` -> `True`. (If not: `.\deploy\make-release-key.ps1`, then **back it up**.)
- [ ] Choose a **quiet time**: the portal is unavailable for about a minute during Part 4, and PostgreSQL restarts briefly in Part 2 *only if* a setting has to change.
- [ ] Nobody is in the middle of a big data import.

---

## Part 1 - Build a fresh release  **[DEV PC]**

Do this every time - it always builds from the current project files.

1. Start **Docker Desktop** and wait until it shows *Engine running*.
2. Open PowerShell and run:
   ```powershell
   cd C:\Users\dines\Documents\project-claude
   .\deploy\build-release.ps1 -Notes "what changed in this version"
   ```
   It rebuilds the front end, **runs all tests (if one fails, it stops and builds nothing)**, builds the container image, and signs it. It takes about 3-5 minutes.
3. Expected ending:
   ```
   ==> Ready: version 20260926-1015
       ...\deploy\releases\itam-release-20260926-1015.itamrel  (117 MB)
   ```
   Two files matter:
   - `deploy\releases\itam-release-<version>.itamrel` - the signed package for the portal's **Software update** page (all later updates).
   - `deploy\images\itam-portal.tar` - the plain image, used **only once**, by `vm-install.sh` in Part 4.
4. (Optional) close Docker Desktop.

---

## Part 2 - Prepare the database and its network access  **[SERVER]**

The database is already installed and in use. This part checks it, backs it up, and makes sure the VM is allowed to connect. Everything here is safe to repeat.

### 2.1 Put the helper scripts on the server  **[DEV PC] -> [SERVER]**
Copy the folder `deploy\host` (two `.ps1` files) to the server, e.g. to `C:\itam\host`. Use RDP copy/paste, or from the DEV PC:
```powershell
robocopy C:\Users\dines\Documents\project-claude\deploy\host \\10.205.64.46\c$\itam\host /E
```

### 2.2 Open PowerShell as Administrator on the server  **[SERVER]**
```powershell
cd C:\itam\host
Get-Service postgresql*            # Status must be: Running
```

### 2.3 Take a fresh database backup first  **[SERVER]**
```powershell
.\Backup-Database.ps1 -To D:\itam-db-backups -Keep 30
```
It asks for the `postgres` password and prints `Backup written: D:\itam-db-backups\ongc_ank_<date>_server.dump`. **Do not continue if this fails.**

### 2.4 Check / set the network access for the VM  **[SERVER]**
```powershell
.\Prepare-Database.ps1 -VmAddress 10.205.64.25 -ConfigureNetwork
```
It asks for the `postgres` password. Because the database already holds your data, expect these lines (that is the *safe* behaviour):
```
role 'ank_app' already exists - its password was left unchanged
the database already holds the inventory (NNNN current assets) - restore skipped, nothing was overwritten
listen_addresses is already '*'                         (or: it is set and PostgreSQL is restarted)
pg_hba.conf already allows ank_app@ongc_ank from 10.205.64.25/32      (or: a line is added, with a backup of the file)
firewall rule already exists                            (or: TCP 5432 is allowed from 10.205.64.25 only)
```
If it *changes* `listen_addresses` it restarts the PostgreSQL service (a few seconds; the portal waits and reconnects by itself). It never touches your data.

### 2.5 Verify  **[SERVER]**
```powershell
netstat -ano | findstr :5432                                   # expect 0.0.0.0:5432 ... LISTENING
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -h localhost -U ank_app -d ongc_ank -c "SELECT count(*) FROM asset WHERE is_current=1;"
```
The second command asks for the `ank_app` password and should print the number of current assets (about 4,781).

### 2.6 Make the VM start with the server  **[SERVER]**  (once)
```powershell
Get-VM itam-ubuntu-production | Format-List Name,State,AutomaticStartAction,AutomaticStartDelay
Set-VM -Name itam-ubuntu-production -AutomaticStartAction Start -AutomaticStartDelay 90
```
The 90-second delay lets PostgreSQL come up before the portal container starts.

---

## Part 3 - Copy the deploy folder to the VM  **[DEV PC] -> [VM]**

From PowerShell on the DEV PC (SSH/`scp` are built into Windows 10/11):
```powershell
scp -r C:\Users\dines\Documents\project-claude\deploy <vm-user>@10.205.64.25:/home/<vm-user>/
```
This copies scripts, `updater/`, `quadlet/`, `release-public.pem`, `images/itam-portal.tar` and `releases/`. It takes a minute or two (about 250 MB).

---

## Part 4 - Install the update service and the new version  **[VM]**  (one-time switch-over)

Log in: `ssh <vm-user>@10.205.64.25`

### 4.1 Pre-flight checks  **[VM]**
```bash
podman --version                    # 4.5 or newer (Ubuntu 24.04 has 4.9)
python3 --version && openssl version
sudo timedatectl set-timezone Asia/Kolkata ; timedatectl | grep -i "time zone"
timeout 3 bash -c '</dev/tcp/10.205.64.46/5432' && echo "database port reachable"
sudo podman ps                      # the old itam-portal container is listed (if it was installed before)
```
Everything must succeed; "database port reachable" proves Part 2 worked.

### 4.2 Run the installer  **[VM]**
```bash
cd ~/deploy
sudo DB_HOST=10.205.64.46 TLS_NAMES=10.205.64.25 bash ./vm-install.sh
```
- If the database password is already stored on the VM it says *"already stored - kept"*; otherwise it asks for the **`ank_app` password**. (To replace it: `sudo CHANGE_PASSWORD=yes DB_HOST=10.205.64.46 bash ./vm-install.sh`.)
- It **tests the database connection first**. Expected: `CONNECTED: PostgreSQL 18.x, time zone Asia/Kolkata` and `INVENTORY: NNNN current assets`. If it says `FAILED: ...` it tells you why (wrong password / not allowed by pg_hba / cannot reach it) and installs nothing - fix that on the server (Part 2) and run it again.
- It then installs the update service, the release public key and the systemd unit, and restarts the portal. **The portal is unavailable for about a minute.**
- The portal's HTTPS certificate is kept if one already exists (it is only made when missing), so PCs that already trust it keep trusting it. `TLS_NAMES` only matters the first time, or after `new-certificate.sh`.
- The portal's HTTPS certificate is kept if one already exists (it is only made when missing), so PCs that already trust it keep trusting it. `TLS_NAMES` only matters the first time, or after `new-certificate.sh`.

### 4.3 Verify  **[VM]**
```bash
systemctl status itam-portal itam-updater.path itam-updater-heartbeat.timer --no-pager | grep -E "Active|●"
sudo podman ps                                         # itam-portal: Up ... (healthy)
curl -k https://localhost/api/health                   # {"ok":true}
sudo podman image inspect --format '{{index .Config.Labels "itam.version"}}' localhost/itam-portal:latest   # the version you built in Part 1
cat /var/lib/itam-updates/agent.heartbeat              # a time from the last minute
```
If `itam-portal` is not healthy after two minutes: `journalctl -u itam-portal -n 60 --no-pager` (the usual cause is the database connection - see Troubleshooting).

---

## Part 5 - First sign-in, certificate, admin access  **[BROWSER] + [VM]**

### 5.1 Trust the portal's certificate (once per PC)
The portal makes its own HTTPS certificate; browsers warn until a PC trusts it (the connection is encrypted either way).
1. **[VM]**: `sudo podman cp itam-portal:/data/tls/portal.crt ~/itam-portal.crt`
2. **[DEV PC]**: `scp <vm-user>@10.205.64.25:~/itam-portal.crt .`
3. On each PC (PowerShell as Administrator): `Import-Certificate -FilePath .\itam-portal.crt -CertStoreLocation Cert:\LocalMachine\Root`. For many PCs use Group Policy (*Computer Configuration > Windows Settings > Security Settings > Public Key Policies > Trusted Root Certification Authorities > Import*).

### 5.2 Sign in  **[BROWSER]**
Open `https://10.205.64.25/`. Sign in with your administrator account. New accounts made by the roster sync start with a starting password and must choose a new one at first sign-in (12+ characters, upper + lower case, a digit and a symbol).

### 5.3 If nobody can sign in as administrator  **[VM]**
```bash
sudo podman exec -w /app itam-portal python portal/db/reset_admin.py                       # lists the administrators (locked / inactive / never signed in)
sudo podman exec -w /app itam-portal python portal/db/reset_admin.py A003541               # new temporary password (shown once), unlocks and reactivates
sudo podman exec -w /app itam-portal python portal/db/reset_admin.py A003541 --password '*ongc123'    # or a starting password you choose (still must be changed at first sign-in)
```
Then sign in with that password. (The recovery is written to the portal's activity log.)

### 5.4 Confirm the update page is ready  **[BROWSER]**
Menu **Data tools -> Software update**. It must show *Running version* = the version from Part 1 and **"Update service running"** (green). If it says *not answering*, see Troubleshooting.

**The one-time setup is finished. From now on every update is done from the portal (Part 6).**

---

## Part 6 - Every later update ("firmware update")

### 6.1 Build and sign  **[DEV PC]**
Same as Part 1: start Docker Desktop, then
```powershell
cd C:\Users\dines\Documents\project-claude
.\deploy\build-release.ps1 -Notes "what changed"
```
Result: `deploy\releases\itam-release-<newversion>.itamrel`.

### 6.2 Upload  **[BROWSER]**
1. Sign in as an administrator -> **Data tools -> Software update**.
2. **Choose file** -> select the `.itamrel` file -> **Upload package** (about 10-30 seconds; a progress bar shows).
3. The page checks it and lists it with **"Signature valid"** (green). If it says *NOT valid* or *checksum does not match*, the file is wrong or damaged - rebuild (Part 6.1) and upload again; nothing was installed.
   *(Alternative for a very large file or a slow connection: copy the file to `/var/lib/itam-updates/incoming/` on the VM with `scp` - it appears in the same list.)*

### 6.3 Install  **[BROWSER]**
1. Click **Install...**. Read the dialog: version from -> to, and the four things that will happen.
2. Type **UPDATE** and click **Install**.
3. Stay on the page. The steps tick off live: *Check the package -> Back up the database -> Load the new version -> Restart the portal -> Wait until it is healthy*. For about a minute the page shows *"The portal is restarting"* - **do not close it**; it reconnects by itself.
4. It ends with **"Updated"** (green). If the new version does not start correctly, the page shows **"Rolled back"** and you are back on the old version automatically - nothing was lost. Send me the *Detailed log* shown on the page.

### 6.4 Check afterwards  **[BROWSER]**
- Software update page: *Running version* is the new one.
- Open the Call tracker dashboard and one register; open **Data tools -> Backup and restore**: there is a backup made "before software update".

### Behind the scenes (so you know what runs where)
| Step | Runs on | By |
|---|---|---|
| Upload and first check of the package | [VM], inside the container | the portal |
| Signature + checksum check again, database backup, load image, restart, health check, rollback | [VM], outside the container, as root | the update service (`/opt/itam/itam-updater.sh`) |
| The database itself, new tables/columns added when the new version starts | [SERVER] | PostgreSQL (schema changes are added automatically and only ever *added*) |

---

## Part 7 - Going back to the previous version

**From the portal (normal case)  [BROWSER]:** *Data tools -> Software update -> "Go back to <previous version>"*, type **ROLLBACK**. The database is backed up first; your data is kept.

**If the portal itself is down  [VM]:**
```bash
sudo /opt/itam/itam-updater.sh --rollback
```
Going back never needs a database restore: updates only add to the database, and the older version ignores what it does not know.

**Update from the VM command line (portal down)  [VM]:** copy a `.itamrel` to the VM and run `sudo /opt/itam/itam-updater.sh --package /path/to/itam-release-<version>.itamrel`.

---

## Part 8 - Routine schedule (keep these running)

| What | Where | How often | How |
|---|---|---|---|
| Portal's own database backup | [VM] -> `/data/backups` (+ second copy folder `/var/backups/itam-second-copy`) | Daily 02:00 (automatic) | *Data tools -> Backup and restore* |
| PostgreSQL `pg_dump` + restore test + copy to the NAS | [SERVER] | Daily 02:00 | your existing scheduled task |
| Hyper-V export of the VM to the NAS | [SERVER] | Weekly, Sunday 03:00 | your existing scheduled task |
| Extra backup **before every update** (recommended) | [SERVER] | Before Part 4 / 6 | `.\Backup-Database.ps1 -To D:\itam-db-backups` |
| Back up the private release key | [DEV PC] | Once (and after any change) | copy `%USERPROFILE%\.itam-release\` to a USB stick in a safe place |
| **Test a restore** into a spare database | [SERVER] | Once a quarter | `.\Prepare-Database.ps1 -DbName ongc_ank_test -DumpFile D:\itam-db-backups\<file>.dump` (never on the live database - it refuses a database that already holds data) |

---

## Part 9 - Troubleshooting  (what you see -> where to look -> what to do)

| Symptom | Where | What to do |
|---|---|---|
| `vm-install.sh` prints `FAILED: cannot reach 10.205.64.46 port 5432` | [VM] then [SERVER] | On the VM: `timeout 3 bash -c '</dev/tcp/10.205.64.46/5432'`. On the server: Part 2.4 (listen/pg_hba/firewall), `Get-Service postgresql*`. Both machines must be on the same network/virtual switch. |
| `FAILED: the server answered but rejected the password` | [SERVER] / [VM] | Wrong `ank_app` password. Set a known one on the server: `.\Prepare-Database.ps1 -AppPassword (Read-Host -AsSecureString "new ank_app password")`, then on the VM `sudo CHANGE_PASSWORD=yes DB_HOST=10.205.64.46 bash ./vm-install.sh`. |
| `FAILED: ... does not allow connections from this VM's address` | [SERVER] | Run Part 2.4 again with the VM's real address. |
| Portal page does not open | [VM] | `sudo podman ps`; `journalctl -u itam-portal -n 60 --no-pager`. A message *"waiting for PostgreSQL"* means the database is unreachable; *"no inventory tables yet"* means the database is empty (restore a backup with `Prepare-Database.ps1 -DumpFile`). |
| Browser says "connection not private" | [BROWSER] | Normal until the certificate is trusted (Part 5.1). |
| Cannot sign in | [BROWSER]/[VM] | Read the message: *wrong password*, *locked for 15 minutes*, *too many sign-ins from this PC (wait 10 min)*. Recover access with Part 5.3. |
| Software update page: *"Update service not answering"* | [VM] | `systemctl status itam-updater.path itam-updater-heartbeat.timer --no-pager`; `sudo systemctl enable --now itam-updater.path itam-updater-heartbeat.timer`; `ls -l /etc/itam/release-public.pem`. If the key file is missing, run `vm-install.sh` again with `release-public.pem` next to it. |
| Upload rejected: *signature is NOT valid* | [DEV PC] | The package was not signed with the key installed on the VM. Rebuild with the right key (`build-release.ps1`); if the key was replaced, install the new `release-public.pem` with `vm-install.sh`. |
| Upload rejected: *not enabled on this server* | [VM] | The shared folder is missing: run `vm-install.sh` again (Part 4). |
| Update ended **"Rolled back"** | [BROWSER] | Nothing was lost. Open *Detailed log* on the page and send it to me. |
| Update ended **"Failed"** before installing | [BROWSER] | The message says why (most often: the pre-update backup failed - check the database connection). Nothing was changed. |
| Update log on the VM | [VM] | `tail -50 /var/lib/itam-updates/update.log` |
| After a power cut, the portal is not up | [SERVER]/[VM] | Start order is server -> PostgreSQL -> VM -> portal (Part 2.6). `sudo systemctl restart itam-portal` on the VM is safe at any time. |

---

## Appendix A - Command cheat sheet by machine

**[DEV PC]**
```powershell
cd C:\Users\dines\Documents\project-claude
.\deploy\make-release-key.ps1                          # ONCE: creates the signing key (then back it up)
.\deploy\build-release.ps1 -Notes "what changed"       # every release: tests + build + sign
scp -r .\deploy <vm-user>@10.205.64.25:/home/<vm-user>/   # first-time copy to the VM
```

**[SERVER]** (PowerShell as Administrator, in `C:\itam\host`)
```powershell
.\Backup-Database.ps1 -To D:\itam-db-backups -Keep 30
.\Prepare-Database.ps1 -VmAddress 10.205.64.25 -ConfigureNetwork          # safe to repeat
.\Prepare-Database.ps1 -DbName ongc_ank_test -DumpFile D:\itam-db-backups\<file>.dump   # restore TEST only
Get-Service postgresql* ; Get-VM itam-ubuntu-production
```

**[VM]**
```bash
sudo DB_HOST=10.205.64.46 TLS_NAMES=10.205.64.25 bash ./vm-install.sh     # one-time switch-over / repair
sudo podman ps ; journalctl -u itam-portal -f                             # status / live log
sudo systemctl restart itam-portal                                        # restart the portal
sudo podman exec -w /app itam-portal python portal/db/reset_admin.py      # administrator recovery (see Part 5.3)
sudo /opt/itam/itam-updater.sh --rollback                                 # back to the previous version
tail -50 /var/lib/itam-updates/update.log                                 # update log
```

**[BROWSER]**  `https://10.205.64.25/` -> *Data tools -> Software update* (upload, Install, Go back) and *Backup and restore*.

---

## Appendix B - If you ever set up a brand-new server and VM from nothing

1. **[SERVER]** install PostgreSQL 18 (set the `postgres` password only). Copy `deploy\host` and a backup file (e.g. `D:\itam\ongc_ank_<date>_manual.dump`).
2. **[SERVER]** `.\Prepare-Database.ps1 -DumpFile D:\itam\<file>.dump -VmAddress <VM address> -ConfigureNetwork` - creates the role and database, **restores the backup**, opens access for the VM. It prints the **`ank_app` password once - write it down**.
3. **[SERVER]** create the VM (Ubuntu 24.04, External virtual switch, fixed IP); install Podman: `sudo apt install podman python3 openssl`; set the time zone (Part 4.1).
4. **[DEV PC]** Part 1, then Part 3 (copy `deploy` to the VM).
5. **[VM]** Part 4.2 with the new server address as `DB_HOST`, then Parts 5 and 2.6.
