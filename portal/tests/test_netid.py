"""Hostname resolution for the activity log (app/netid.py) and its wiring into auth.log(). No network calls are made in these
tests except test_a_real_lan_address_resolves_via_netbios, which talks to this machine's own address and is skipped if that
does not work in the test environment (a firewall, or a network namespace with no NetBIOS responder)."""
import socket
import struct
import time

import pytest

from portal.app import auth, netid
from test_scoping import one, sandbox  # noqa: F401  (sandbox is a fixture)

ED = "Test Editor"


@pytest.fixture(autouse=True)
def _clear_cache():
    netid._cache.clear()
    yield
    netid._cache.clear()


# ---------------------------------------------------------------- packet parsing (no network - built by hand)
def _nbt_name_entry(name, suffix, group):
    return name.encode("ascii").ljust(15, b" ") + bytes([suffix]) + struct.pack("!H", 0x8000 if group else 0x0000)


def _nbt_reply(names, qdcount=0):
    header = struct.pack("!HHHHHH", 0x1234, 0x8400, qdcount, 1, 0, 0)
    body = b""
    if qdcount:
        body += b"\x01*\x00" + struct.pack("!HH", 0x0021, 0x0001)                    # a minimal (non-standard) echoed question
    body += b"\xc0\x0c" + struct.pack("!HHIH", 0x0021, 0x0001, 0, 1 + 18 * len(names))
    body += bytes([len(names)]) + b"".join(names)
    return header + body


def test_parses_a_genuine_windows_node_status_reply_with_no_question_section():
    # this is the real shape Windows sends (QDCOUNT 0) - the bug this test guards against: assuming the question is always echoed
    reply = _nbt_reply([_nbt_name_entry("MYPC", 0x00, False), _nbt_name_entry("WORKGROUP", 0x00, True)])
    assert netid._parse_nbt_response(reply) == "MYPC"


def test_parses_a_reply_that_does_echo_the_question_section():
    reply = _nbt_reply([_nbt_name_entry("OTHERPC", 0x00, False)], qdcount=1)
    assert netid._parse_nbt_response(reply) == "OTHERPC"


def test_ignores_group_names_and_service_suffixes_other_than_workstation():
    reply = _nbt_reply([_nbt_name_entry("AGROUP", 0x00, True), _nbt_name_entry("FILESERV", 0x20, False), _nbt_name_entry("REALNAME", 0x00, False)])
    assert netid._parse_nbt_response(reply) == "REALNAME"


def test_rejects_garbage_bytes_as_a_name_instead_of_crashing():
    junk = bytes([0xff, 0xfe, 0x00, 0x01] * 4)                                        # not valid ASCII hostname characters
    reply = _nbt_reply([junk[:15] + bytes([0x00]) + b"\x00\x00"])
    assert netid._parse_nbt_response(reply) is None


@pytest.mark.parametrize("bad", [b"", b"\x00" * 4, b"\x00" * 20])
def test_never_raises_on_truncated_or_empty_packets(bad):
    assert netid._parse_nbt_response(bad) is None


def test_no_answers_returns_none():
    reply = struct.pack("!HHHHHH", 0x1234, 0x8400, 0, 0, 0, 0)
    assert netid._parse_nbt_response(reply) is None


# ---------------------------------------------------------------- resolve_hostname: caching and bounded time
def test_an_unreachable_address_returns_none_quickly_and_is_cached(monkeypatch):
    monkeypatch.setattr(netid, "_NBT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(netid, "_DNS_TIMEOUT_S", 0.05)
    t = time.monotonic()
    assert netid.resolve_hostname("192.0.2.1") is None                                 # TEST-NET-1, reserved, never routes
    assert time.monotonic() - t < 2.0
    hit, cached = netid._cache_get("192.0.2.1")
    assert cached and hit is None                                                      # a failure is cached too (short TTL), not retried on every request


def test_a_resolved_hostname_is_served_from_cache_without_a_second_lookup(monkeypatch):
    calls = []
    monkeypatch.setattr(netid, "_nbt_name_query", lambda ip: calls.append(ip) or "SOMEPC")
    assert netid.resolve_hostname("10.9.9.9") == "SOMEPC"
    assert netid.resolve_hostname("10.9.9.9") == "SOMEPC"
    assert calls == ["10.9.9.9"]                                                       # the second call was answered from cache


def test_falls_back_to_reverse_dns_when_netbios_answers_nothing(monkeypatch):
    monkeypatch.setattr(netid, "_nbt_name_query", lambda ip: None)
    monkeypatch.setattr(netid, "_reverse_dns", lambda ip: "dns-name.example.com".split(".")[0].upper())
    assert netid.resolve_hostname("10.9.9.8") == "DNS-NAME"


def test_empty_ip_resolves_to_none_without_any_lookup(monkeypatch):
    monkeypatch.setattr(netid, "_nbt_name_query", lambda ip: pytest.fail("must not query an empty address"))
    assert netid.resolve_hostname("") is None
    assert netid.resolve_hostname(None) is None


@pytest.mark.skipif(not socket.gethostname(), reason="no local hostname to resolve against")
def test_a_real_lan_address_resolves_via_netbios():
    """Talks to this machine's own address over the real network - the one live-network test in this file. Skipped, not failed, if
    this environment does not answer (a container with NetBIOS blocked, for example) - the parsing itself is already covered above."""
    try:
        own_ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        pytest.skip("could not determine this machine's own LAN address")
    name = netid._nbt_name_query(own_ip)
    if name is None:
        pytest.skip("no NetBIOS response on this network - nothing further to check here")
    assert name.isascii() and name.replace("-", "").replace("_", "").isalnum()


# ---------------------------------------------------------------- wiring into auth.log()
def test_log_stores_the_hostname_resolved_at_the_time(sandbox, monkeypatch):
    monkeypatch.setattr(netid, "resolve_hostname", lambda ip: "ENGINEER-PC-07" if ip == "10.1.2.3" else None)
    auth.log(ED, "10.1.2.3", "LOGIN")
    row = one(sandbox, "SELECT ip, hostname FROM portal_activity WHERE username = %s ORDER BY activity_id DESC LIMIT 1", (ED,))
    assert row["ip"] == "10.1.2.3" and row["hostname"] == "ENGINEER-PC-07"


def test_log_still_writes_the_row_even_if_hostname_resolution_blows_up(sandbox, monkeypatch):
    def boom(ip):
        raise RuntimeError("network is unreachable")
    monkeypatch.setattr(netid, "resolve_hostname", boom)
    auth.log(ED, "10.1.2.4", "LOGIN")           # must not raise, and must not lose the audit row just because the hostname lookup failed
    row = one(sandbox, "SELECT ip, hostname FROM portal_activity WHERE username = %s AND ip = %s", (ED, "10.1.2.4"))
    assert row["ip"] == "10.1.2.4" and row["hostname"] is None
