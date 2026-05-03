"""
In-band caps signalling for OpenOB v6.

Carries the negotiated RTP caps from TX to RX as RFC 3550 §6.7 RTCP APP
packets MUXED ON THE SAME UDP PORT as the RTP audio (RFC 5761, RTCP-mux).
This means a single inbound port at the receiver — no extra firewall hole
to punch.

Demux is by RTP payload type: byte 1 of each packet is the PT (RTP) or
PT+marker (RTCP). Standard RTCP packet types occupy 200..207, while RTP
sessions in the wild use PT 0..127 (we use dynamic PT 96+ for Opus/L16),
so there's no overlap. (RFC 5761 explicitly carves out PT 64..95 as
"forbidden in RTP" for muxed sessions; we stay clear of those.)

Two halves:

- :class:`CapsSignalSender` — TX-side. Uses stdlib :func:`socket.sendto`
  via a :class:`GLib.timeout_add_seconds` callback. No GStreamer pipeline.
- :func:`wait_for_caps` — RX-side bootstrap. Binds a UDP socket, reads
  until the first OPOB APP packet arrives, then closes the socket so the
  GStreamer pipeline can take the port.
- :func:`attach_rtcp_demux_probe` — RX-side runtime. Pad probe on the main
  udpsrc that drops RTCP packets (so rtpbin only sees RTP) and optionally
  refreshes caps from late-arriving APP packets.

This module deliberately doesn't touch ``rtpbin``'s own RTCP machinery:
PyGObject's ``RTCPPacket.app_get_data`` binding is broken, OpenOB doesn't
currently wire ``rtpbin`` RTCP at all, and owning the channel ourselves
keeps the wire format under our control.
"""
from __future__ import annotations

import logging
import socket
import struct
import time
from typing import Callable

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib  # noqa: E402

from cryptography.exceptions import InvalidTag  # noqa: E402

from openob.security import (  # noqa: E402
    decrypt_caps_payload, encrypt_caps_payload,
)

# Idempotent — safe alongside other modules that init Gst.
Gst.init(None)


APP_NAME = b'OPOB'                # 4 ASCII bytes, our APP packet identifier
SUBTYPE_CAPS = 0                  # subtype 0 = RTP caps payload
RTCP_PT_SR = 200                  # RFC 3550 §6.4.1 (Sender Report)
RTCP_PT_RR = 201                  # RFC 3550 §6.4.2 (Receiver Report)
RTCP_PT_APP = 204                 # RFC 3550 §6.7
DEFAULT_INTERVAL = 2.0            # seconds between sender retransmits

# NTP epoch is 1900-01-01; Unix epoch is 1970-01-01. Difference in seconds.
_NTP_UNIX_EPOCH_OFFSET = 2208988800

# RFC 5761 §4 demux: byte 1 of each packet is either the RTP marker+PT
# (range 0..255 with PT 0..127) or the RTCP packet type (range 200..207
# for the standard SR/RR/SDES/BYE/APP/RTPFB/PSFB/XR/RSI). To make this
# unambiguous we MUST avoid RTP PTs in 64..95 (RFC 5761 §4 — those collide
# with RTCP after the marker bit is OR'd in). OpenOB uses dynamic PT 96
# (rtpopuspay / rtpL16pay defaults), well clear of that range.
_RTCP_PT_LOW = 200
_RTCP_PT_HIGH = 207

# Reasonable cap so a hostile peer can't push us into allocating large
# buffers on parsing. RTP caps strings in the wild are a few hundred bytes;
# anything beyond a few KB is almost certainly malformed.
_MAX_CAPS_BYTES = 4096
_MAX_RECV_BYTES = _MAX_CAPS_BYTES + 64  # +slack for the RTCP framing overhead


# --------------------------------------------------------------------- helpers


def _ssrc_for_link(link_name: str) -> int:
    """Derive a stable 32-bit SSRC from the link name, for log readability."""
    h = 0
    for b in link_name.encode('utf-8'):
        h = (h * 31 + b) & 0xFFFFFFFF
    return h or 0xDEADBEEF  # avoid SSRC 0; some implementations treat it specially


def is_rtcp(packet: bytes) -> bool:
    """RFC 5761 demux: byte 1 in 200..207 → RTCP, else assume RTP."""
    return len(packet) >= 2 and _RTCP_PT_LOW <= packet[1] <= _RTCP_PT_HIGH


def _ntp_now() -> tuple[int, int]:
    """Current time as the (integer-seconds, fraction) NTP timestamp pair
    used by RFC 3550 §6.4.1 — 32-bit seconds since 1900-01-01 plus a 32-bit
    fraction-of-a-second."""
    import time
    now = time.time() + _NTP_UNIX_EPOCH_OFFSET
    seconds = int(now) & 0xFFFFFFFF
    fraction = int((now - int(now)) * (1 << 32)) & 0xFFFFFFFF
    return seconds, fraction


def build_rtcp_sender_report(ssrc: int) -> bytes:
    """Build a minimal RFC 3550 §6.4.1 Sender Report with no report blocks.

    Wire format (28 bytes total)::

        0..3   V=2, P=0, RC=0, PT=200, length=6 (7 words minus 1)
        4..7   sender SSRC
        8..15  NTP timestamp (8 bytes: 32-bit seconds + 32-bit fraction)
        16..19 RTP timestamp (we don't track an RTP clock here, so 0)
        20..23 sender's packet count (0 — we send RTCP, not RTP, on this
               session; the audio RTP session has its own ssrc)
        24..27 sender's octet count (0)

    Used as the leading packet of the compound RTCP buffer required by
    RFC 3550 §6.1 — every RTCP transmission must be a compound packet
    starting with SR or RR. We're the sender of these caps packets, so
    SR is the semantically-correct choice (vs RR which is for receivers
    reporting on streams they're listening to). Real NTP timestamp;
    counters zeroed since OpenOB doesn't track per-session stats on the
    caps channel.
    """
    # V=2, P=0, RC=0 → 0x80; PT=200 (SR); length = 7 words MINUS 1
    header = struct.pack('!BBH', 0x80, RTCP_PT_SR, 6)
    ntp_sec, ntp_frac = _ntp_now()
    sender_info = struct.pack(
        '!IIIII',
        ntp_sec, ntp_frac,
        0,  # RTP timestamp — no RTP session on this channel
        0,  # sender's packet count
        0,  # sender's octet count
    )
    return header + struct.pack('!I', ssrc) + sender_info


def build_rtcp_app(ssrc: int, name: bytes, subtype: int, data: bytes) -> bytes:
    """Build a single RTCP APP packet per RFC 3550 §6.7.

    Wire format (bytes)::

        0..1   V|P|subtype, PT=204
        2..3   length in 32-bit words MINUS 1
        4..7   SSRC
        8..11  name (4 ASCII bytes)
        12..n  application data (with RFC 3550 §6.4.1 padding if needed)

    When the data length isn't already a 32-bit multiple, the P bit is set
    and the trailing bytes carry padding whose last byte is the padding
    count (so the receiver can strip the exact number of bytes). This is
    necessary so opaque payloads (e.g. AES-GCM ciphertext + tag) survive
    the wire format intact — naive zero-fill would corrupt the auth tag.
    """
    if len(name) != 4:
        raise ValueError("APP name must be exactly 4 bytes")
    if not 0 <= subtype <= 31:
        raise ValueError("APP subtype is a 5-bit field (0-31)")

    pad_len = (-len(data)) & 3
    if pad_len == 0:
        data_padded = data
        p_bit = 0
    else:
        # RFC 3550 §6.4.1: padding occupies pad_len bytes; the last byte
        # gives the count (including itself). Receivers strip `pad_len`
        # bytes when the P bit is set.
        data_padded = data + b'\x00' * (pad_len - 1) + bytes([pad_len])
        p_bit = 1

    data_words = len(data_padded) // 4
    length_words_minus_1 = 2 + data_words  # SSRC (1 word) + name (1 word) + data

    first_byte = (2 << 6) | (p_bit << 5) | (subtype & 0x1f)
    header = struct.pack('!BBH', first_byte, RTCP_PT_APP, length_words_minus_1)
    return header + struct.pack('!I', ssrc) + name + data_padded


def parse_rtcp_app(buf: bytes):
    """Walk a (possibly compound) RTCP buffer and yield APP packets only.

    Yields ``(subtype, name, ssrc, payload)`` tuples with the padding
    bytes (per the P bit, RFC 3550 §6.4.1) already stripped from
    ``payload``. Silently skips non-APP packets and malformed tails.
    """
    offset = 0
    n = len(buf)
    while offset + 4 <= n:
        first_byte, pt, length_words_minus_1 = struct.unpack_from('!BBH', buf, offset)
        if first_byte >> 6 != 2:  # version must be 2
            return
        pkt_bytes = (length_words_minus_1 + 1) * 4
        if pkt_bytes < 4 or offset + pkt_bytes > n:
            return
        if pt == RTCP_PT_APP and pkt_bytes >= 12:
            subtype = first_byte & 0x1f
            ssrc, = struct.unpack_from('!I', buf, offset + 4)
            name = bytes(buf[offset + 8:offset + 12])
            payload_end = offset + pkt_bytes
            # Strip RFC 3550 padding if the P bit is set; the last byte is
            # the count of padding bytes (including itself).
            if (first_byte >> 5) & 1:
                pad_count = buf[payload_end - 1]
                if 0 < pad_count <= pkt_bytes - 12:
                    payload_end -= pad_count
            payload = bytes(buf[offset + 12:payload_end])
            yield subtype, name, ssrc, payload
        offset += pkt_bytes


def parse_encoding_from_caps(caps_str: str) -> str:
    """Extract the OpenOB-recognised encoding name from a caps string.

    Used by RX to pick the right depayloader/decoder when the codec
    parameters arrive in-band (so the RX TOML doesn't need to repeat them).
    Returns ``'opus'`` or ``'pcm'``.
    """
    for part in caps_str.split(','):
        key, _, value = part.strip().partition('=')
        if key.strip().lower() != 'encoding-name':
            continue
        # Strip GStreamer type tags and quoting: '(string)OPUS' → 'OPUS'.
        v = value.strip()
        if v.startswith('(string)'):
            v = v[len('(string)'):]
        v = v.strip().strip('"').upper()
        if v == 'OPUS':
            return 'opus'
        if v == 'L16':
            return 'pcm'
        raise ValueError(
            f'caps encoding-name {v!r} is not a recognised OpenOB codec '
            f'(expected OPUS or L16)'
        )
    raise ValueError(f'no encoding-name field in caps: {caps_str!r}')


def parse_rtx_fields(caps_str: str) -> tuple[int, dict[int, int]] | None:
    """Extract OpenOB's RTX advertisement from a caps string.

    Returns ``(rtx_time_ms, pt_map)`` where ``pt_map`` is original-PT →
    RTX-PT, or ``None`` when the caps don't carry the RTX hint (i.e. the
    sender either disabled RTX or is a v6.0 peer that pre-dates this
    feature). Only ``rtx-time`` is required to enable RX-side RTX; ``pt-map``
    falls back to the OpenOB default ``{96: 97}`` if missing or malformed.
    """
    rtx_time: int | None = None
    pt_map: dict[int, int] = {}
    for part in caps_str.split(','):
        key, _, value = part.strip().partition('=')
        k = key.strip().lower()
        v = value.strip()
        # Strip type tag. We look only at (uint), (int), (string).
        for tag in ('(uint)', '(int)', '(string)'):
            if v.startswith(tag):
                v = v[len(tag):]
                break
        v = v.strip().strip('"')
        if k == 'rtx-time':
            try:
                rtx_time = int(v)
            except ValueError:
                continue
        elif k == 'rtx-pt-map':
            # Encoded as 'orig=rtx,orig=rtx,...'; we get the WHOLE field
            # value here because Gst caps don't quote it back. But our
            # comma-split above already broke on commas — so we only see
            # the first 'orig=rtx' fragment in v. That's fine; OpenOB only
            # ever uses one mapping (96→97). Parsing more would mean
            # replacing the comma-split with a structure-aware tokeniser.
            try:
                orig_str, _, rtx_str = v.partition('=')
                pt_map[int(orig_str)] = int(rtx_str)
            except ValueError:
                continue
    if rtx_time is None or rtx_time <= 0:
        return None
    if not pt_map:
        pt_map = {96: 97}
    return rtx_time, pt_map


def find_opob_caps(
    packet: bytes,
    caps_key: bytes | None = None,
    *,
    logger: logging.Logger | None = None,
) -> str | None:
    """If ``packet`` is an RTCP buffer containing an OPOB CAPS APP packet,
    return the caps string. Otherwise ``None``.

    When ``caps_key`` is set, the APP payload is treated as an AES-256-GCM
    envelope (12-byte nonce + ciphertext + 16-byte tag) and the returned
    caps come from the verified plaintext. An auth-tag mismatch causes the
    packet to be silently dropped (logged at warning level if a logger is
    given) — this is the wire path that prevents a spoofed caps signal
    from reconfiguring the receiver.
    """
    if not is_rtcp(packet):
        return None
    for subtype, name, _ssrc, payload in parse_rtcp_app(packet):
        if name != APP_NAME or subtype != SUBTYPE_CAPS:
            continue
        if caps_key is not None:
            try:
                plaintext = decrypt_caps_payload(caps_key, payload)
            except (InvalidTag, ValueError) as exc:
                if logger is not None:
                    logger.warning(
                        'caps signal failed authentication (%s); '
                        'dropping packet (likely spoofed or PSK mismatch)',
                        type(exc).__name__,
                    )
                continue
        else:
            plaintext = payload
        try:
            return plaintext.rstrip(b'\x00').decode('utf-8')
        except UnicodeDecodeError:
            if logger is not None:
                logger.warning(
                    'caps payload decoded but is not UTF-8; dropping'
                )
            return None
    return None


# ----------------------------------------------------------------------- TX


class CapsSignalSender:
    """TX-side periodic OPOB CAPS APP packet sender.

    Sends one packet immediately on :meth:`start` so an already-listening RX
    gets caps within milliseconds, then every :attr:`interval_seconds`.

    Uses a stdlib UDP socket (no GStreamer machinery — overkill for a
    100-byte packet every 2s) and integrates with the existing
    :class:`GLib.MainLoop` via :func:`GLib.timeout_add_seconds`.
    """

    def __init__(
        self,
        host: str,
        port: int,
        link_name: str,
        caps: str,
        interval_seconds: float = DEFAULT_INTERVAL,
        logger: logging.Logger | None = None,
        caps_key: bytes | None = None,
        sock: socket.socket | None = None,
    ) -> None:
        self.host = host
        self.port = port  # SAME port as the RTP audio — RTCP-mux per RFC 5761
        self.link_name = link_name
        self.caps = caps
        self.interval_seconds = interval_seconds
        self.logger = logger or logging.getLogger(
            f'openob.link.{link_name}.caps_signal.tx'
        )
        self.ssrc = _ssrc_for_link(link_name)
        # If caps_key is set, the APP packet payload is AES-256-GCM encrypted
        # with that key (12-byte nonce || ciphertext || 16-byte tag). If
        # None, the payload is plain UTF-8 caps bytes (security="none" mode).
        self.caps_key = caps_key

        # When sock is supplied (RTX mode) the caller owns the socket and we
        # share its source port with the audio udpsink so OPOB and RTP carry
        # one NAT mapping. When None we create our own ephemeral socket on
        # start() and close it on stop().
        self._sock: socket.socket | None = sock
        self._sock_owned = sock is None
        self._timeout_id: int | None = None
        self._counter = 0

    def start(self) -> None:
        if self._timeout_id is not None:
            return
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Allow multicast: the OS will route via the default interface;
        # explicit IP_MULTICAST_IF can be added later if a node needs to pin
        # which NIC the caps signal goes out of.
        self._push_one()
        self._timeout_id = GLib.timeout_add_seconds(
            int(self.interval_seconds), self._push_one,
        )
        self.logger.info(
            'Caps-signal sender active: %s:%d every %.1fs (RTCP-mux on RTP port)',
            self.host, self.port, self.interval_seconds,
        )

    def stop(self) -> None:
        if self._timeout_id is not None:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = None
        if self._sock is not None and self._sock_owned:
            self._sock.close()
        if self._sock_owned:
            self._sock = None

    def _push_one(self) -> bool:
        if self._sock is None:
            return GLib.SOURCE_REMOVE
        plaintext = self.caps.encode('utf-8')
        if len(plaintext) > _MAX_CAPS_BYTES:
            self.logger.error(
                'caps string is %d bytes (>%d cap); refusing to send',
                len(plaintext), _MAX_CAPS_BYTES,
            )
            return GLib.SOURCE_REMOVE

        # When PSK security is in effect, AEAD-encrypt the payload so the
        # caps signal has the same authenticity guarantee as the audio
        # stream (RFC 5761 §7 spirit). Without a caps_key we send cleartext
        # — security="none" mode, where there's no shared secret to use.
        if self.caps_key is not None:
            payload = encrypt_caps_payload(self.caps_key, plaintext)
        else:
            payload = plaintext

        # RFC 3550 §6.1 requires every RTCP transmission to be a compound
        # packet starting with SR or RR. We're the sender of these caps
        # packets so SR is semantically correct. Counters are zeroed
        # because OpenOB doesn't track per-session stats on this channel.
        # Receivers' parse_rtcp_app walks the compound and picks out the
        # APP transparently.
        compound = (
            build_rtcp_sender_report(self.ssrc)
            + build_rtcp_app(self.ssrc, APP_NAME, SUBTYPE_CAPS, payload)
        )
        try:
            self._sock.sendto(compound, (self.host, self.port))
        except OSError as exc:
            self.logger.warning('caps-signal sendto failed: %s', exc)
        else:
            self._counter += 1
            self.logger.debug(
                'caps-signal #%d sent (ssrc=0x%08x, %d bytes compound SR+APP)',
                self._counter, self.ssrc, len(compound),
            )
        return GLib.SOURCE_CONTINUE


# ----------------------------------------------------------------------- RX


def wait_for_caps(
    port: int,
    link_name: str,
    logger: logging.Logger | None = None,
    timeout: float | None = None,
    expected_peer: str | None = None,
    caps_key: bytes | None = None,
) -> tuple[str, tuple[str, int]]:
    """Bootstrap-wait for the first OPOB CAPS APP packet on ``port``.

    Binds a stdlib UDP socket, reads until a matching packet arrives, then
    closes the socket so the main GStreamer udpsrc can rebind the same port
    when the receiver pipeline starts.

    Args:
        port: UDP port to bind. Same port as the RTP audio (RTCP-mux).
        link_name: for logging.
        logger: defaults to the link logger.
        timeout: seconds to wait, or None for indefinite.
        expected_peer: if given, ignore packets from any other source
            address. Useful for STL deployments where the TX address is
            known in advance.
        caps_key: AES-256-GCM key. If set, only AEAD-authenticated payloads
            are accepted; spoofed/forged packets are dropped silently.
            Required for any meaningful security guarantee in PSK mode.

    Returns:
        ``(caps_str, (peer_ip, peer_port))``. The peer address is the source
        of the OPOB packet — used by RX to target the RTCP NACK back-channel
        when RTX is enabled. When TX shares its OPOB+RTP socket (RTX mode),
        peer_port equals TX's RTP source port post-NAT, so NACKs sent there
        hairpin into TX's existing audio flow.

    Raises:
        TimeoutError: no matching packet within ``timeout`` seconds.
    """
    log = logger or logging.getLogger(f'openob.link.{link_name}.caps_signal.rx')

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(('', port))
        log.info(
            'Waiting for caps signal on UDP %d (link=%s, timeout=%s, peer=%s, auth=%s)',
            port, link_name, timeout, expected_peer or 'any',
            'aes-256-gcm' if caps_key else 'none',
        )

        deadline = (time.monotonic() + timeout) if timeout is not None else None
        n_received = 0
        n_ignored_peer = 0
        n_auth_failed = 0

        while True:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f'no OPOB CAPS APP packet on UDP {port} within {timeout}s '
                        f'(received {n_received} packets, ignored {n_ignored_peer} '
                        f'from non-peer, {n_auth_failed} auth failures)'
                    )
                sock.settimeout(remaining)

            try:
                data, addr = sock.recvfrom(_MAX_RECV_BYTES)
            except socket.timeout:
                continue  # loop will re-check deadline and raise

            n_received += 1

            if expected_peer is not None and addr[0] != expected_peer:
                n_ignored_peer += 1
                continue

            # Capture auth-failure count by sniffing the log via a counting
            # logger filter would be overkill; instead inline the parse so
            # we can count failures distinctly from "not OPOB" cases.
            opob_caps_str = find_opob_caps(data, caps_key, logger=log)
            if opob_caps_str is None:
                # Could be an RTP packet, a non-OPOB RTCP, OR an OPOB
                # packet that failed AEAD verification (already logged by
                # find_opob_caps in that case).
                if caps_key is not None and is_rtcp(data):
                    n_auth_failed += 1  # heuristic — overcounts a tiny bit
                if n_received % 100 == 0:
                    log.debug(
                        'still waiting for valid OPOB CAPS APP packet '
                        '(received %d packets so far, latest from %s)',
                        n_received, addr,
                    )
                continue

            log.info(
                'Got caps from %s after %d packet(s): %s',
                addr[0], n_received, opob_caps_str,
            )
            return (opob_caps_str, (addr[0], addr[1]))
    finally:
        sock.close()


def attach_rtcp_demux_probe(
    udpsrc,
    on_caps_refresh: Callable[[str], None] | None = None,
    logger: logging.Logger | None = None,
    caps_key: bytes | None = None,
) -> int:
    """Attach a pad probe to ``udpsrc.src`` that drops RTCP packets so
    rtpbin only sees RTP, and optionally calls ``on_caps_refresh`` whenever
    a fresh OPOB CAPS APP packet arrives.

    When ``caps_key`` is set, refresh callbacks fire only for AEAD-
    authenticated payloads (forged OPOB packets are dropped silently).

    The probe is the only thing standing between the receiver's udpsrc and
    rtpbin's RTP input pad — without it, rtpbin chokes on the muxed RTCP
    packets.

    Returns the probe id (caller can pass to ``pad.remove_probe`` on
    teardown if needed).
    """
    log = logger or logging.getLogger('openob.caps_signal.rx.probe')
    pad = udpsrc.get_static_pad('src')
    if pad is None:
        raise RuntimeError("udpsrc has no 'src' pad — has it been added to a bin?")

    def probe(_pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        size = buf.get_size()
        if size < 2:
            return Gst.PadProbeReturn.OK
        # Cheap demux: look at byte 1 of the packet.
        b1 = buf.extract_dup(1, 1)
        if not b1 or not (_RTCP_PT_LOW <= b1[0] <= _RTCP_PT_HIGH):
            return Gst.PadProbeReturn.OK  # RTP — pass through to rtpbin
        # RTCP: parse for OPOB CAPS, then drop so rtpbin doesn't see it.
        if on_caps_refresh is not None:
            data = buf.extract_dup(0, size)
            caps = find_opob_caps(data, caps_key, logger=log)
            if caps is not None:
                try:
                    on_caps_refresh(caps)
                except Exception:
                    log.exception('on_caps_refresh callback raised')
        return Gst.PadProbeReturn.DROP

    probe_id = pad.add_probe(Gst.PadProbeType.BUFFER, probe)
    log.debug('RTCP demux probe attached to udpsrc.src (id=%s)', probe_id)
    return probe_id
