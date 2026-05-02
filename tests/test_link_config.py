"""Unit tests for the redis_host parser in openob.link_config."""
from __future__ import annotations

import pytest

from openob.link_config import _parse_redis_host


@pytest.mark.parametrize(
    "given, expected",
    [
        # Bare hostname / IPv4 → default port
        ("redis.example.com", ("redis.example.com", 6379)),
        ("127.0.0.1", ("127.0.0.1", 6379)),
        ("localhost", ("localhost", 6379)),
        # host:port — IPv4 / hostname
        ("redis.example.com:6380", ("redis.example.com", 6380)),
        ("127.0.0.1:6380", ("127.0.0.1", 6380)),
        # Bare IPv6 literal (more than one colon, no brackets) → default port
        ("::1", ("::1", 6379)),
        ("fe80::1", ("fe80::1", 6379)),
        ("2001:db8::1", ("2001:db8::1", 6379)),
        # Bracketed IPv6 — with and without port
        ("[::1]", ("::1", 6379)),
        ("[::1]:6380", ("::1", 6380)),
        ("[fe80::1]:6380", ("fe80::1", 6380)),
        ("[2001:db8::1]:6380", ("2001:db8::1", 6380)),
    ],
)
def test_parse_redis_host_valid(given, expected):
    assert _parse_redis_host(given) == expected


@pytest.mark.parametrize(
    "given, frag",
    [
        ("[::1", "no closing"),
        ("[::1]junk", "unexpected text"),
        ("[::1]:abc", "non-integer port"),
        ("redis.example.com:abc", "non-integer port"),
    ],
)
def test_parse_redis_host_rejects(given, frag):
    with pytest.raises(ValueError, match=frag):
        _parse_redis_host(given)
