"""Best-effort hostname for a client IP address, for the activity log.

Why: on this LAN, addresses are handed out by DHCP, so the same PC can carry a different IP on a different day (or after a
reconnect) - the IP alone is not enough to trace an action back to a specific machine later. The hostname is far more stable, so
the activity log records both, captured at the moment the action happened (a hostname looked up after the fact could reflect
whatever PC now holds that IP, not the one that held it then).

Two independent ways to ask "who are you", tried in order, because neither is available everywhere:
  1. NetBIOS name service (UDP port 137, "node status" query) - every Windows PC answers this by default, even with no DNS
     registration at all, which is the common case on a plain DHCP network with no dynamic DNS updates.
  2. Reverse DNS (PTR record) - works for anything else (Linux hosts, printers, phones) when the network's DNS is set up to
     register DHCP leases; otherwise this simply returns nothing, same as leaving the field blank.
Both are bounded to a few hundred milliseconds and cached, so a slow or unreachable client never meaningfully delays the
request being logged, and the same address is not re-queried on every single action.
"""
import logging
import re
import socket
import struct
import threading
import time

log = logging.getLogger("itam")

_NBT_TIMEOUT_S = 0.3
_DNS_TIMEOUT_S = 0.3
_CACHE_TTL_S = 15 * 60          # a resolved hostname is reused for this long
_NEGATIVE_TTL_S = 2 * 60        # a failed lookup is retried after this long, not on every single request

_cache = {}
_cache_lock = threading.Lock()
_dns_pool = None                # created on first use - a reverse DNS call cannot be interrupted, so it is bounded by abandoning it, not cancelling it


def _cache_get(ip):
    with _cache_lock:
        hit = _cache.get(ip)
    if not hit:
        return None, False
    hostname, expires = hit
    return (hostname, True) if time.monotonic() < expires else (None, False)


def _cache_put(ip, hostname):
    ttl = _CACHE_TTL_S if hostname else _NEGATIVE_TTL_S
    with _cache_lock:
        _cache[ip] = (hostname, time.monotonic() + ttl)


def _nbt_name_query(ip):
    """NBT-NS node status query (RFC 1002 4.2.18) - the wildcard name '*' asks the target to list its own NetBIOS names.
    Returns the unique "workstation service" name (suffix 0x00), which is the computer's own name, or None."""
    def encode(raw16):
        out = bytearray()
        for b in raw16:
            out.append(0x41 + (b >> 4))
            out.append(0x41 + (b & 0x0F))
        return bytes([len(out)]) + bytes(out) + b"\x00"

    query = struct.pack("!HHHHHH", 0x1234, 0x0000, 1, 0, 0, 0) + encode(b"*" + b"\x00" * 15) + struct.pack("!HH", 0x0021, 0x0001)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(_NBT_TIMEOUT_S)
        sock.sendto(query, (ip, 137))
        data, _ = sock.recvfrom(2048)
    except OSError:
        return None
    finally:
        sock.close()
    return _parse_nbt_response(data)


def _skip_dns_name(buf, offset):
    while True:
        length = buf[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:              # a compression pointer, always 2 bytes
            return offset + 2
        offset += 1 + length


_VALID_NAME = re.compile(r"[A-Za-z0-9_-]{1,15}")


def _parse_nbt_response(buf):
    try:
        qdcount, ancount = struct.unpack("!HH", buf[4:8])
        if not ancount:
            return None
        offset = 12
        for _ in range(qdcount):                   # a NODE STATUS reply normally has QDCOUNT 0 (nothing to skip here);
            offset = _skip_dns_name(buf, offset)    # this loop only runs at all against a non-standard responder
            offset += 4                             # qtype + qclass
        offset = _skip_dns_name(buf, offset)        # the answer's own name
        offset += 8                                 # type(2) + class(2) + ttl(4)
        rdlen = struct.unpack("!H", buf[offset:offset + 2])[0]
        offset += 2
        num_names = buf[offset]
        p = offset + 1
        for _ in range(num_names):
            raw, suffix, flags = buf[p:p + 15], buf[p + 15], struct.unpack("!H", buf[p + 16:p + 18])[0]
            p += 18
            if suffix == 0x00 and not (flags & 0x8000):     # the "workstation service" name, not a group name
                m = _VALID_NAME.match(raw.decode("ascii", "replace").strip())    # untrusted network bytes: only ordinary name characters are accepted
                if m and m.group() != "*":
                    return m.group().upper()
        return None
    except (IndexError, struct.error):
        return None


def _reverse_dns(ip):
    try:
        name = socket.gethostbyaddr(ip)[0]
        return name.split(".")[0].upper() or None       # the short name is what people recognise; drop a fully-qualified domain suffix
    except (socket.herror, socket.gaierror, OSError):
        return None


def resolve_hostname(ip):
    """-> the client's hostname, or None if neither method answers in time. Never raises, never blocks the caller for long."""
    if not ip:
        return None
    hostname, cached = _cache_get(ip)
    if cached:
        return hostname
    try:
        hostname = _nbt_name_query(ip)
        if not hostname:
            global _dns_pool
            if _dns_pool is None:
                import concurrent.futures
                _dns_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="hostname-lookup")
            future = _dns_pool.submit(_reverse_dns, ip)
            try:
                hostname = future.result(timeout=_DNS_TIMEOUT_S)
            except concurrent.futures.TimeoutError:
                hostname = None          # the lookup may still finish in the background; this request just does not wait for it
    except Exception:  # noqa: BLE001 - a lookup failure must never break the action being logged
        log.exception("hostname lookup failed for %s", ip)
        hostname = None
    _cache_put(ip, hostname)
    return hostname
