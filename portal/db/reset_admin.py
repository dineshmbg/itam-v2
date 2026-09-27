"""Get administrator access back when nobody can sign in. Run on the server that runs the portal (needs the same database settings as the portal).

  python portal/db/reset_admin.py              list every account that can administer (read-only) - shows who is locked / inactive / never signed in
  python portal/db/reset_admin.py ADMIN        give ADMIN a NEW temporary password (printed once), reactivate and unlock it, sign it out everywhere
  python portal/db/reset_admin.py A003541 --password '*ongc123'     ...or set a starting password you choose (still forced to change at next sign-in)

In the container:   sudo podman exec -w /app itam-portal python portal/db/reset_admin.py ADMIN
The person must then sign in with the temporary password and choose a real one at once. Every recovery is written to the activity log.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from portal.app import auth, db  # noqa: E402


def main(argv):
    if len(argv) < 2:
        rows = db.query("""SELECT username, display_name, active, must_change, failed_attempts, locked_until > now() AS locked, last_login_at
                           FROM portal_user WHERE role = 'ADMIN' AND NOT read_only ORDER BY user_id""")
        print(f"{'USER':10} {'NAME':28} {'ACTIVE':6} {'LOCKED':6} {'MUST CHANGE PW':14} LAST SIGN-IN")
        for r in rows:
            print(f"{r['username']:10} {r['display_name'][:28]:28} {str(r['active']):6} {str(bool(r['locked'])):6} {str(r['must_change']):14} {r['last_login_at'] or 'never'}")
        print("\nTo recover one:  python portal/db/reset_admin.py <USERNAME>")
        return 0
    password = argv[argv.index("--password") + 1] if "--password" in argv and argv.index("--password") + 1 < len(argv) else None
    try:
        out = auth.recover_account(argv[1], password=password)
    except auth.AuthError as e:
        print(f"reset_admin: {e}", file=sys.stderr)
        return 1
    print(f"Account {out['username']} is active and unlocked.\nPassword (shown once): {out['temporary_password']}\nIt must be changed at the next sign-in.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
