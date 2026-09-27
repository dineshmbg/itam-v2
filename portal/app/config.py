"""Runtime configuration. Everything can be overridden with environment variables."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # .../portal
PROJECT_ROOT = ROOT.parent                             # .../project-claude
STATIC_DIR = ROOT / "static"
MASTERS_DIR = PROJECT_ROOT / "masters"

PG = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": int(os.environ.get("PGPORT", "5432")),
    "dbname": os.environ.get("PGDATABASE", "ongc_ank"),
    "user": os.environ.get("PGUSER", "ank_app"),
}
POOL_MIN = int(os.environ.get("PORTAL_POOL_MIN", "2"))
POOL_MAX = int(os.environ.get("PORTAL_POOL_MAX", "10"))
STATEMENT_TIMEOUT_MS = int(os.environ.get("PORTAL_STATEMENT_TIMEOUT_MS", "15000"))
HOST = os.environ.get("PORTAL_HOST", "127.0.0.1")       # local only unless the operator opts in
PORT = int(os.environ.get("PORTAL_PORT", "8420"))
CACHE_TTL_S = int(os.environ.get("PORTAL_CACHE_TTL_S", "300"))
MAX_PAGE = 200
ALLOW_REMOTE_EDIT = os.environ.get("PORTAL_ALLOW_REMOTE_EDIT") == "1"   # edits are accepted from this PC only unless the operator opts in
