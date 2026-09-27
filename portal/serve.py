"""Start the ITAM Portal.

  python serve.py [--host 127.0.0.1] [--port 8420] [--open]
                  [--tls-cert cert.pem --tls-key key.pem [--redirect-port 8080]]

With --tls-cert/--tls-key (or PORTAL_TLS_CERT / PORTAL_TLS_KEY) the portal itself speaks HTTPS - no reverse proxy needed, and every
client's own address still reaches the activity log. --redirect-port starts a tiny listener that sends plain-HTTP visitors to the HTTPS address.

Binds to localhost by default. Use --host 0.0.0.0 only if colleagues on the LAN should reach it
(sign-in is required, but the connection is plain HTTP - put an HTTPS proxy in front on an untrusted network).
"""
import argparse
import os
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))


def _start_redirect(host, port, https_port):
    """Plain-HTTP listener whose only job is to send visitors to the HTTPS address (same host name, HTTPS port)."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Redirect(BaseHTTPRequestHandler):
        def _go(self):
            name = (self.headers.get("Host") or "localhost").rsplit(":", 1)[0] if not (self.headers.get("Host") or "").startswith("[") else "localhost"
            self.send_response(308)
            self.send_header("Location", f"https://{name}{'' if https_port == 443 else f':{https_port}'}{self.path}")
            self.send_header("Content-Length", "0")
            self.end_headers()
        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _go

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer((host, port), Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def main():
    from portal.app import config, db

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=config.HOST)
    ap.add_argument("--port", type=int, default=config.PORT)
    ap.add_argument("--open", action="store_true", help="open the portal in the default browser")
    ap.add_argument("--tls-cert", default=os.environ.get("PORTAL_TLS_CERT"), help="certificate file (PEM) - turns on HTTPS")
    ap.add_argument("--tls-key", default=os.environ.get("PORTAL_TLS_KEY"), help="private key file (PEM)")
    ap.add_argument("--redirect-port", type=int, default=int(os.environ.get("PORTAL_REDIRECT_PORT", "0")), help="also listen here (plain HTTP) and redirect to the HTTPS address")
    a = ap.parse_args()
    if bool(a.tls_cert) != bool(a.tls_key):
        sys.exit("Give both --tls-cert and --tls-key (or neither).")

    if not (config.STATIC_DIR / "index.html").exists():
        sys.exit("Front end is not built. Run:  cd frontend && node build.mjs")
    try:
        with db.admin_connection() as con:
            row = con.execute("SELECT to_regclass('public.portal_engineer') AS t, to_regclass('public.portal_user') AS u").fetchone()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Cannot reach PostgreSQL ({config.PG['host']}:{config.PG['port']}/{config.PG['dbname']}): {e}")
    if not row["t"] or not row["u"]:
        sys.exit("Database is not prepared for the portal. Run:  python db/setup.py")

    import uvicorn
    tls = {"ssl_certfile": a.tls_cert, "ssl_keyfile": a.tls_key} if a.tls_cert else {}
    if tls and a.redirect_port:
        _start_redirect(a.host, a.redirect_port, int(os.environ.get("PORTAL_PUBLIC_HTTPS_PORT") or a.port))
    url = f"{'https' if tls else 'http'}://{'127.0.0.1' if a.host in ('0.0.0.0', '::') else a.host}:{a.port}/"
    print(f"ITAM Portal  ->  {url}   (Ctrl+C to stop)")
    if a.open:
        webbrowser.open(url)
    uvicorn.run("portal.app.main:app", host=a.host, port=a.port, log_level="warning", access_log=False, timeout_keep_alive=30, **tls)


if __name__ == "__main__":
    main()
