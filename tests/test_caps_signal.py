"""Unit tests for the caps-signal wire format and helpers."""
from __future__ import annotations

import pytest

from openob.caps_signal import (
    APP_NAME,
    RTCP_PT_SR,
    SUBTYPE_CAPS,
    build_rtcp_app,
    build_rtcp_sender_report,
    find_opob_caps,
    is_rtcp,
    parse_encoding_from_caps,
    parse_rtcp_app,
    parse_rtx_fields,
    sanitize_caps_from_wire,
)


# --- build / parse round-trip --------------------------------------------


def test_round_trip_simple_payload():
    msg = build_rtcp_app(0xcafef00d, APP_NAME, SUBTYPE_CAPS, b'hello world')
    parsed = list(parse_rtcp_app(msg))
    assert len(parsed) == 1
    subtype, name, ssrc, payload = parsed[0]
    assert subtype == SUBTYPE_CAPS
    assert name == APP_NAME
    assert ssrc == 0xcafef00d
    # Payload is zero-padded to 32-bit boundary.
    assert payload.rstrip(b'\x00') == b'hello world'


def test_find_opob_caps_round_trip():
    caps = 'application/x-rtp,media=(string)audio,clock-rate=(int)48000'
    msg = build_rtcp_app(0xdeadbeef, APP_NAME, SUBTYPE_CAPS, caps.encode())
    assert find_opob_caps(msg) == caps


def test_find_opob_caps_returns_none_for_non_app():
    # PT 200 (SR), not 204 (APP). Build manually.
    import struct
    sr = struct.pack('!BBH', 0x80, 200, 1) + b'\x00\x00\x00\x01' * 2
    assert find_opob_caps(sr) is None


def test_find_opob_caps_returns_none_for_rtp():
    # An RTP-looking byte 1 (PT 96 = 0x60), not RTCP.
    rtp = b'\x80\x60\x00\x01' + b'\x00' * 12
    assert find_opob_caps(rtp) is None


def test_is_rtcp_demux():
    # PT in 200..207 → RTCP
    assert is_rtcp(b'\x80\xc8\x00\x00')  # 200
    assert is_rtcp(b'\x80\xcc\x00\x00')  # 204 APP
    assert is_rtcp(b'\x80\xcf\x00\x00')  # 207
    # Outside range → not RTCP
    assert not is_rtcp(b'\x80\x60\x00\x00')   # 96 — typical RTP
    assert not is_rtcp(b'\x80\x7f\x00\x00')   # 127 — RTP max
    assert not is_rtcp(b'\x80\xd0\x00\x00')   # 208 — outside
    # Too short
    assert not is_rtcp(b'\x80')


def test_build_rejects_bad_name():
    with pytest.raises(ValueError, match='4 bytes'):
        build_rtcp_app(0, b'ABC', 0, b'')
    with pytest.raises(ValueError, match='4 bytes'):
        build_rtcp_app(0, b'ABCDE', 0, b'')


def test_build_rejects_bad_subtype():
    with pytest.raises(ValueError, match='5-bit'):
        build_rtcp_app(0, APP_NAME, 32, b'')


def test_parser_skips_non_app_in_compound():
    """Compound RTCP buffer with SR + APP should yield only the APP."""
    import struct
    # Sender Report: V=2, PT=200, length=6 (length-in-words MINUS 1, so 7 words = 28 bytes total).
    # The 4-byte header is INCLUDED in those 28 bytes, so we add 24 bytes of body.
    sr = struct.pack('!BBH', 0x80, 200, 6) + b'\x00' * 24
    app = build_rtcp_app(0x12345678, APP_NAME, SUBTYPE_CAPS, b'hi')
    compound = sr + app
    parsed = list(parse_rtcp_app(compound))
    assert len(parsed) == 1
    assert parsed[0][1] == APP_NAME


def test_parser_handles_truncated_tail():
    """A malformed packet at the end of a compound buffer is silently skipped."""
    msg = build_rtcp_app(0, APP_NAME, SUBTYPE_CAPS, b'ok') + b'\x80\xcc'  # truncated
    parsed = list(parse_rtcp_app(msg))
    assert len(parsed) == 1  # the valid one before the truncated tail


# --- parse_encoding_from_caps -------------------------------------------


def test_parse_encoding_opus():
    caps = (
        'application/x-rtp,media=(string)audio,encoding-name=(string)OPUS,'
        'clock-rate=(int)48000,payload=(int)96,channels=(int)2'
    )
    assert parse_encoding_from_caps(caps) == 'opus'


def test_parse_encoding_pcm():
    caps = (
        'application/x-rtp,media=(string)audio,encoding-name=(string)L16,'
        'clock-rate=(int)48000,payload=(int)96,channels=(int)2'
    )
    assert parse_encoding_from_caps(caps) == 'pcm'


def test_parse_encoding_srtp_variant():
    """The application/x-srtp variant emitted in PSK mode is also handled."""
    caps = (
        'application/x-srtp,media=(string)audio,encoding-name=(string)OPUS,'
        'clock-rate=(int)48000,payload=(int)96'
    )
    assert parse_encoding_from_caps(caps) == 'opus'


def test_parse_encoding_unquoted():
    """Some payloaders may emit unquoted encoding-name without the (string) tag."""
    assert parse_encoding_from_caps('application/x-rtp,encoding-name=OPUS') == 'opus'


def test_parse_encoding_unknown_raises():
    caps = 'application/x-rtp,encoding-name=(string)PCMA'
    with pytest.raises(ValueError, match='not a recognised'):
        parse_encoding_from_caps(caps)


def test_parse_encoding_missing_raises():
    caps = 'application/x-rtp,clock-rate=(int)48000'
    with pytest.raises(ValueError, match='no encoding-name'):
        parse_encoding_from_caps(caps)


# --- compound SR + APP (RFC 3550 §6.1 compliance) -------------------------


def test_sender_report_wire_format():
    """The Sender Report is exactly 28 bytes: header + SSRC + sender info."""
    import struct
    sr = build_rtcp_sender_report(0xdeadbeef)
    assert len(sr) == 28
    first, pt, length = struct.unpack_from('!BBH', sr, 0)
    assert first == 0x80          # V=2, P=0, RC=0
    assert pt == RTCP_PT_SR       # 200
    assert length == 6            # 7 words minus 1
    ssrc, = struct.unpack_from('!I', sr, 4)
    assert ssrc == 0xdeadbeef
    # Sender info: NTP-sec, NTP-frac, RTP ts, packet count, octet count
    ntp_sec, ntp_frac, rtp_ts, pkt_count, oct_count = struct.unpack_from('!IIIII', sr, 8)
    assert ntp_sec > 3_800_000_000  # well past 2020 in NTP epoch
    assert rtp_ts == 0
    assert pkt_count == 0
    assert oct_count == 0


def test_compound_sr_then_app_parses_only_app():
    """A compound SR + APP buffer (the on-the-wire format the sender
    actually emits) yields just the APP packet through parse_rtcp_app."""
    sr = build_rtcp_sender_report(0x12345678)
    app = build_rtcp_app(0x12345678, APP_NAME, SUBTYPE_CAPS, b'opus-caps-go-here')
    compound = sr + app

    parsed = list(parse_rtcp_app(compound))
    assert len(parsed) == 1
    subtype, name, ssrc, payload = parsed[0]
    assert name == APP_NAME
    assert ssrc == 0x12345678
    assert payload.rstrip(b'\x00') == b'opus-caps-go-here'


def test_find_opob_caps_through_compound():
    """find_opob_caps must work on the compound packet too."""
    sr = build_rtcp_sender_report(1)
    app = build_rtcp_app(
        1, APP_NAME, SUBTYPE_CAPS,
        b'application/x-rtp,clock-rate=(int)48000',
    )
    assert find_opob_caps(sr + app) == 'application/x-rtp,clock-rate=(int)48000'


def test_is_rtcp_compound_byte1_is_sr_pt():
    """An on-wire OpenOB caps packet looks like RTCP from byte 1 (PT=200,
    SR — leads the compound) — so the demux probe correctly classifies it."""
    sr = build_rtcp_sender_report(1)
    app = build_rtcp_app(1, APP_NAME, SUBTYPE_CAPS, b'caps')
    compound = sr + app
    assert is_rtcp(compound)        # leading byte 1 = 200 = SR
    assert compound[1] == RTCP_PT_SR


# --- find_opob_caps with AEAD auth ---------------------------------------


def test_find_opob_caps_auth_round_trip():
    """An encrypted OPOB CAPS packet decrypts cleanly with the matching key."""
    from openob.security import derive_caps_key, encrypt_caps_payload
    caps_str = 'application/x-srtp,encoding-name=(string)OPUS,clock-rate=(int)48000'
    key = derive_caps_key('the-psk', 'mylink')
    encrypted = encrypt_caps_payload(key, caps_str.encode('utf-8'))
    sr = build_rtcp_sender_report(1)
    app = build_rtcp_app(1, APP_NAME, SUBTYPE_CAPS, encrypted)
    assert find_opob_caps(sr + app, key) == caps_str


def test_find_opob_caps_auth_rejects_wrong_key():
    """A packet encrypted under one key is dropped (returns None) when
    parsed with a different key — exactly how a spoofed packet from a
    bad-PSK source gets rejected."""
    from openob.security import derive_caps_key, encrypt_caps_payload
    key_a = derive_caps_key('alice', 'mylink')
    key_b = derive_caps_key('bob',   'mylink')
    encrypted = encrypt_caps_payload(key_a, b'application/x-rtp,foo=1')
    sr = build_rtcp_sender_report(1)
    app = build_rtcp_app(1, APP_NAME, SUBTYPE_CAPS, encrypted)
    assert find_opob_caps(sr + app, key_b) is None


def test_find_opob_caps_auth_rejects_cleartext_when_key_set():
    """If the receiver expects authenticated payloads (caps_key set) but
    the packet payload is cleartext, the AEAD decrypt fails and the
    packet is dropped. Prevents downgrade attacks."""
    from openob.security import derive_caps_key
    key = derive_caps_key('the-psk', 'mylink')
    sr = build_rtcp_sender_report(1)
    app = build_rtcp_app(
        1, APP_NAME, SUBTYPE_CAPS,
        b'application/x-rtp,...',  # cleartext, no nonce/tag
    )
    assert find_opob_caps(sr + app, key) is None


def test_find_opob_caps_no_key_still_works_for_cleartext():
    """Without a caps_key the parser falls back to cleartext UTF-8 — the
    security="none" mode."""
    sr = build_rtcp_sender_report(1)
    app = build_rtcp_app(
        1, APP_NAME, SUBTYPE_CAPS,
        b'application/x-rtp,clock-rate=(int)48000',
    )
    assert (
        find_opob_caps(sr + app, caps_key=None)
        == 'application/x-rtp,clock-rate=(int)48000'
    )


# --- sanitize_caps_from_wire ---------------------------------------------


def test_sanitize_keeps_allowed_fields():
    caps = (
        'application/x-rtp,media=(string)audio,encoding-name=(string)OPUS,'
        'clock-rate=(int)48000,payload=(int)96,channels=(int)2'
    )
    assert sanitize_caps_from_wire(caps) == caps


def test_sanitize_strips_unknown_fields():
    """Attacker-injected fields are dropped before they reach Gst.Caps."""
    caps = (
        'application/x-rtp,media=(string)audio,clock-rate=(int)48000,'
        'attacker-field=(string)oops,encoding-name=(string)OPUS'
    )
    cleaned = sanitize_caps_from_wire(caps)
    assert cleaned == (
        'application/x-rtp,media=(string)audio,clock-rate=(int)48000,'
        'encoding-name=(string)OPUS'
    )


def test_sanitize_strips_srtp_key_field():
    """A wire-supplied srtp-key MUST be stripped — RX derives its own from
    the PSK; trusting a wire-supplied key would defeat encryption entirely."""
    caps = (
        'application/x-srtp,media=(string)audio,'
        'srtp-key=(buffer)deadbeef,encoding-name=(string)OPUS'
    )
    cleaned = sanitize_caps_from_wire(caps)
    assert cleaned is not None
    assert 'srtp-key' not in cleaned
    assert 'deadbeef' not in cleaned


def test_sanitize_rejects_bad_media_type():
    """An unrecognised media type returns None (the packet gets dropped)."""
    assert sanitize_caps_from_wire(
        'application/x-something-else,clock-rate=(int)48000'
    ) is None
    assert sanitize_caps_from_wire('') is None


def test_sanitize_accepts_srtp_media_type():
    """application/x-srtp — the security=psk path — must pass."""
    caps = 'application/x-srtp,encoding-name=(string)OPUS'
    assert sanitize_caps_from_wire(caps) == caps


def test_find_opob_caps_drops_bad_media_type():
    """End-to-end: a packet with an attacker-chosen media type returns None
    (packet dropped) rather than feeding it to Gst.Caps.from_string."""
    sr = build_rtcp_sender_report(1)
    app = build_rtcp_app(
        1, APP_NAME, SUBTYPE_CAPS,
        b'application/x-evil,clock-rate=(int)48000',
    )
    assert find_opob_caps(sr + app) is None


def test_find_opob_caps_strips_unknown_fields_in_packet():
    """End-to-end: unknown fields in a wire packet are stripped before
    return so the caller can't accidentally feed them to GStreamer."""
    sr = build_rtcp_sender_report(1)
    app = build_rtcp_app(
        1, APP_NAME, SUBTYPE_CAPS,
        b'application/x-rtp,clock-rate=(int)48000,attacker=(string)x',
    )
    result = find_opob_caps(sr + app)
    assert result == 'application/x-rtp,clock-rate=(int)48000'


# --- _PerSourceRateLimiter ----------------------------------------------


def test_rate_limiter_allows_initial_burst():
    """A fresh source gets the full bucket immediately — legitimate TX
    that fires its first caps packet on startup is never rate-limited."""
    from openob.caps_signal import _PerSourceRateLimiter
    rl = _PerSourceRateLimiter(capacity=5, refill_per_sec=1.0)
    for _ in range(5):
        assert rl.allow('10.0.0.1', now=0.0) is True
    assert rl.allow('10.0.0.1', now=0.0) is False  # bucket drained


def test_rate_limiter_refills_over_time():
    """Tokens refill at the configured rate."""
    from openob.caps_signal import _PerSourceRateLimiter
    rl = _PerSourceRateLimiter(capacity=2, refill_per_sec=1.0)
    assert rl.allow('10.0.0.1', now=0.0) is True
    assert rl.allow('10.0.0.1', now=0.0) is True
    assert rl.allow('10.0.0.1', now=0.0) is False  # drained
    assert rl.allow('10.0.0.1', now=1.5) is True   # 1.5 tokens refilled


def test_rate_limiter_isolates_sources():
    """One noisy source can't deny service to a quiet one."""
    from openob.caps_signal import _PerSourceRateLimiter
    rl = _PerSourceRateLimiter(capacity=2, refill_per_sec=1.0)
    assert rl.allow('10.0.0.1', now=0.0) is True
    assert rl.allow('10.0.0.1', now=0.0) is True
    assert rl.allow('10.0.0.1', now=0.0) is False
    # Different source — own bucket, full.
    assert rl.allow('10.0.0.2', now=0.0) is True
    assert rl.allow('10.0.0.2', now=0.0) is True


def test_rate_limiter_caps_tracked_sources():
    """Source-IP spraying can't OOM the dict — LRU eviction kicks in."""
    from openob.caps_signal import _PerSourceRateLimiter
    rl = _PerSourceRateLimiter(capacity=1, refill_per_sec=1.0, max_sources=4)
    # Fill the source dict.
    for i in range(4):
        assert rl.allow(f'10.0.0.{i}', now=float(i)) is True
    # New source forces eviction; dict size stays at the cap.
    assert rl.allow('10.0.99.99', now=10.0) is True
    assert len(rl._buckets) == 4
    # The oldest entry (10.0.0.0 at t=0.0) should be the one evicted.
    assert '10.0.0.0' not in rl._buckets


# --- parse_rtx_fields ----------------------------------------------------


def test_parse_rtx_fields_present():
    """Caps with rtx-time + rtx-pt-map round-trip into the parsed tuple."""
    caps = (
        'application/x-rtp,media=(string)audio,encoding-name=(string)OPUS,'
        'clock-rate=(int)48000,payload=(int)96,'
        'rtx-time=(uint)200,rtx-pt-map=(string)96=97'
    )
    result = parse_rtx_fields(caps)
    assert result is not None
    rtx_time, pt_map = result
    assert rtx_time == 200
    assert pt_map == {96: 97}


def test_parse_rtx_fields_absent_returns_none():
    """v6.0-style caps without RTX fields parse to None."""
    caps = (
        'application/x-rtp,media=(string)audio,encoding-name=(string)OPUS,'
        'clock-rate=(int)48000,payload=(int)96'
    )
    assert parse_rtx_fields(caps) is None


def test_parse_rtx_fields_zero_rtx_time_treated_as_off():
    """rtx-time=0 means RTX is explicitly off (don't enable on RX)."""
    caps = (
        'application/x-rtp,encoding-name=(string)OPUS,'
        'rtx-time=(uint)0,rtx-pt-map=(string)96=97'
    )
    assert parse_rtx_fields(caps) is None


def test_parse_rtx_fields_pt_map_default_when_missing():
    """rtx-time alone with no pt-map falls back to OpenOB's default 96→97."""
    caps = 'application/x-rtp,rtx-time=(uint)150'
    result = parse_rtx_fields(caps)
    assert result is not None
    assert result == (150, {96: 97})
