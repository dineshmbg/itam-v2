# deploy/

**Container / VM deployment** - Podman Quadlet unit, `vm-install.sh`, `new-certificate.sh`; `make-release-key.ps1` + `build-release.ps1` (dev PC: tests + build + SIGN a `.itamrel` package); `updater/` (VM update service behind the portal's Data tools > Software update page; verifies signature, backs up, health-checks, auto-rollback); `host/Prepare-Database.ps1` (on the physical server: creates role/database, restores a backup). The database is NOT containerised. Offline image transfer: `images/itam-portal.tar`. Detail: `README.md`.
