"""Live updates: PostgreSQL LISTEN/NOTIFY -> debounced fan-out to every browser over Server-Sent Events.

The triggers installed by db/setup.py fire whenever a loader touches an inventory table. A burst of statements (a bulk load) is collapsed into
one event after a short quiet period, the server-side caches are dropped, and each open dashboard re-fetches its own data.
"""
import asyncio
import threading
import time

import orjson
import psycopg

from . import db

DEBOUNCE_S = 0.35


class Hub:
    def __init__(self):
        self.version = int(time.time())          # changes on every data change and on every server start (safe ETag base)
        self.clients = set()
        self.connected = False
        self.last_change = None
        self.loop = None
        self._pending = set()
        self._timer = None
        self._stop = threading.Event()
        self._cache = {}

    # ---- cache (dropped on every data change)
    def cache_get(self, key):
        hit = self._cache.get(key)
        return hit if hit and hit[0] == self.version else None

    def cache_put(self, key, value):
        if len(self._cache) > 600:
            self._cache.clear()
        self._cache[key] = (self.version, value)

    # ---- lifecycle
    def start(self, loop):
        self.loop = loop
        threading.Thread(target=self._listen, daemon=True, name="pg-listen").start()

    def stop(self):
        self._stop.set()

    def _listen(self):
        while not self._stop.is_set():
            try:
                with psycopg.connect(db.conninfo(), autocommit=True) as con:
                    con.execute("LISTEN portal_changes")
                    self.connected = True
                    while not self._stop.is_set():
                        for n in con.notifies(timeout=5):
                            self.loop.call_soon_threadsafe(self._on_notify, n.payload)
            except Exception:
                self.connected = False
                time.sleep(2)
        self.connected = False

    # ---- runs on the event loop
    def _on_notify(self, payload):
        self._pending.add(payload)
        if self._timer is None:
            self._timer = self.loop.call_later(DEBOUNCE_S, self._flush)

    def _flush(self):
        self._timer = None
        tables = sorted(self._pending)
        self._pending.clear()
        self.version += 1
        self.last_change = time.time()
        self._cache.clear()
        ev = {"type": "change", "tables": tables, "version": self.version, "ts": self.last_change}
        for q in list(self.clients):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                self.clients.discard(q)

    def snapshot(self):
        return {"version": self.version, "live": self.connected, "clients": len(self.clients), "last_change": self.last_change}


hub = Hub()


def sse(event, data):
    return b"event: " + event.encode() + b"\ndata: " + orjson.dumps(data) + b"\n\n"
