"""Does the domain controller answer LDAP, and does it accept NTLM? Sends one bind with a made-up account, so no real password is used.

    python tools/ldap_probe.py [host] [port] [DOMAIN]

invalidCredentials = the controller answered and understood the method (what we want to see). A timeout or 'connection closed' = the
controller or a firewall is not letting this machine talk LDAP. Any other result = that method is refused.
"""
import sys
import time

import ldap3

host = sys.argv[1] if len(sys.argv) > 1 else "vdainf01adc01.ongc.ongcgroup.co.in"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 389
dom = sys.argv[3] if len(sys.argv) > 3 else "ONGC"
user = dom + chr(92) + "0000001"
for label, how in (("NTLM", ldap3.NTLM), ("SIMPLE", ldap3.SIMPLE)):
    t = time.time()
    try:
        c = ldap3.Connection(ldap3.Server(host, port=port, get_info=ldap3.NONE, connect_timeout=10), user=user, password="not-a-real-password",
                             authentication=how, receive_timeout=25, raise_exceptions=False)
        c.bind()
        print(f"{label:7}{(c.result or {}).get('description')}  ({time.time() - t:.1f}s)")
    except Exception as ex:                                   # noqa: BLE001 - the point is to show what happened
        print(f"{label:7}NO ANSWER: {type(ex).__name__}: {str(ex)[:90]}  ({time.time() - t:.1f}s)")
