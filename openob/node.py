"""
Link supervisor for OpenOB v6.

A :class:`Node` runs one link's TX or RX side and restarts it when the
GStreamer pipeline crashes or stalls. The supervisor stays in a tight
restart loop so an unattended link recovers from transient failures
without operator intervention.

v6 changes vs v5:

- No more Redis. The TX→RX caps handshake is in-band via
  :mod:`openob.caps_signal` (RFC 5761 RTCP-mux on the RTP port).
- TX side spins up a :class:`CapsSignalSender` after the GStreamer pipeline
  reaches PLAYING and we've captured the negotiated caps string.
- RX side either bootstrap-waits for an in-band caps signal
  (:func:`wait_for_caps`) before building its pipeline, or — if
  ``caps_from_config`` is set — derives the caps string from local config
  and starts immediately.
- v6.1: optional RTP retransmission (rtprtxsend/rtprtxreceive). When TX has
  ``retransmission_time_limit > 0`` it shares one Python UDP socket between
  the audio udpsink, an RTCP udpsrc (for inbound NACKs), and the OPOB caps
  sender — so OPOB and audio share a single NAT mapping and RX can target
  the back-channel using the OPOB packet's source address.
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import time

from openob._log_events import EVT_PIPELINE_RESTART
from openob.audio_interface import AudioInterface
from openob.caps_signal import CapsSignalSender, wait_for_caps
from openob.link_config import LinkConfig, TxPath
from openob.logger import LoggerFactory
from openob.security import resolve_caps_key
from openob.telemetry import TelemetryRegistry, get_openob_version


# IP_UNICAST_IF lives in glibc's headers but the symbol isn't always exposed
# on Python's `socket` module (it depends on Python build / kernel headers).
# 50 is the canonical Linux value; falling back to the literal lets us run on
# stock interpreters without a "module 'socket' has no attribute" surprise.
_IP_UNICAST_IF = getattr(socket, 'IP_UNICAST_IF', 50)


# Bootstrap timeout for the RX caps wait. Generous so an RX started long
# before TX still picks up the eventual caps. None would work but a finite
# timeout means a misconfigured link surfaces in logs rather than blocking
# silently forever.
_CAPS_WAIT_TIMEOUT_SECONDS = 24 * 60 * 60


class Node:
    """A node runs and supervises one link's TX or RX side."""

    def __init__(self, node_name: str) -> None:
        self.node_name = node_name
        self.logger_factory = LoggerFactory()
        self.logger = self.logger_factory.getLogger(f'node.{self.node_name}')

    def run_link(
        self,
        link_config: LinkConfig,
        audio_interface: AudioInterface,
    ) -> None:
        """Run the link TX or RX side, restarting on failure."""
        # Imports deferred so Gst.init only fires when we actually need it.
        try:
            from openob.rtp.rx import RTPReceiver
            from openob.rtp.tx import RTPTransmitter
        except ImportError as e:
            self.logger.critical(
                'Failed to load GStreamer pipeline modules (%s). '
                'Check that python3-gi, python3-gst-1.0, gir1.2-gstreamer-1.0, '
                'gstreamer1.0-plugins-base and gstreamer1.0-plugins-good are '
                'installed and that this Python venv was created with '
                '--system-site-packages. SRTP also needs gstreamer1.0-plugins-bad.',
                e,
            )
            raise

        self.logger.info(
            'Link %s initial setup start on %s', link_config.name, self.node_name,
        )
        role = audio_interface.mode  # 'tx' or 'rx'
        link_logger = self.logger_factory.getLinkLogger(
            node=self.node_name, link=link_config.name, role=role,
        )

        # One TelemetryRegistry per run_link call. The audio path calls
        # public methods on it unconditionally — when telemetry is off the
        # disabled() singleton makes every call a no-op, so the audio path
        # has zero awareness of whether telemetry is configured.
        telemetry = TelemetryRegistry.from_link(
            link_config,
            node_name=self.node_name,
            role=role,
            version=get_openob_version(),
        )
        telemetry.start()
        try:
            if audio_interface.mode == 'tx':
                self._run_tx_loop(
                    link_config, audio_interface, RTPTransmitter,
                    link_logger, telemetry,
                )
            elif audio_interface.mode == 'rx':
                self._run_rx_loop(
                    link_config, audio_interface, RTPReceiver,
                    link_logger, telemetry,
                )
            else:
                link_logger.critical(
                    'Unknown audio interface mode: %r', audio_interface.mode,
                )
                sys.exit(1)
        finally:
            telemetry.stop()

    def _run_tx_loop(self, link_config, audio_interface, RTPTransmitter,
                     link_logger, telemetry):
        first_iteration = True
        while True:
            caps_senders: list[CapsSignalSender] = []
            shared_pysock: socket.socket | None = None
            path_sockets: list[socket.socket] = []
            try:
                if not first_iteration:
                    # We only get here after the previous iteration's except
                    # block fired. Bump the restart counter and reset delta
                    # tracking so the post-restart counters establish a
                    # fresh baseline.
                    telemetry.reset_counter_deltas()
                first_iteration = False
                link_logger.info('Starting up transmitter')

                multipath = bool(link_config.tx_paths)
                if multipath:
                    # One Python-owned UDP socket per egress, with iface
                    # (IP_UNICAST_IF) or bind applied as configured. Path 0
                    # additionally carries the RTCP NACK return path and the
                    # OPOB caps signal source-port that RX targets for both;
                    # paths 1..N also send caps so bootstrap works regardless
                    # of which path is up first.
                    for p in link_config.tx_paths:
                        path_sockets.append(_build_path_socket(p))
                    transmitter = RTPTransmitter(
                        self.node_name, link_config, audio_interface,
                        path_sockets=path_sockets,
                        telemetry=telemetry,
                    )
                else:
                    # When RTX is enabled the audio udpsink, an RTCP udpsrc, and
                    # the OPOB caps sender all need to share a single source
                    # port — that's how RTCP NACKs from RX hairpin back through
                    # TX's NAT into the same socket the audio went out on.
                    if link_config.retransmission_time_limit > 0:
                        shared_pysock = _build_shared_tx_socket(link_config)
                    transmitter = RTPTransmitter(
                        self.node_name, link_config, audio_interface,
                        shared_socket=shared_pysock,
                        telemetry=telemetry,
                    )
                transmitter.run()  # blocks until pipeline reaches PLAYING and caps captured
                caps = transmitter.get_caps()
                if caps is None:
                    raise RuntimeError(
                        'transmitter reached PLAYING but produced no caps string'
                    )
                link_logger.info('Captured caps from transmitter: %s', caps)

                # In PSK mode the caps signal is AEAD-encrypted with a
                # separate AES-256-GCM key derived from the same PSK so a
                # spoofed caps packet can't reconfigure the receiver.
                # Cleartext when security="none" (no shared secret).
                caps_key = (
                    resolve_caps_key(
                        link_name=link_config.name,
                        psk=link_config.psk,
                        root_psk=link_config.root_psk,
                    ) if link_config.security == 'psk' else None
                )

                # Start broadcasting caps in-band so RX can pick them up.
                # Same UDP port as the audio (RTCP-mux per RFC 5761). Multipath
                # spawns one sender per path so caps egress every interface.
                if multipath:
                    for i, (path, sock) in enumerate(zip(link_config.tx_paths, path_sockets)):
                        cs = CapsSignalSender(
                            host=path.host,
                            port=path.port if path.port is not None else link_config.port,
                            link_name=link_config.name,
                            caps=caps,
                            caps_key=caps_key,
                            sock=sock,
                            logger=self.logger_factory.getLogger(
                                f'link.{link_config.name}.caps_signal.tx.'
                                f'{path.label or f"path{i}"}'
                            ),
                        )
                        cs.start()
                        caps_senders.append(cs)
                else:
                    cs = CapsSignalSender(
                        host=link_config.receiver_host,
                        port=link_config.port,
                        link_name=link_config.name,
                        caps=caps,
                        caps_key=caps_key,
                        sock=shared_pysock,
                        logger=self.logger_factory.getLogger(
                            f'link.{link_config.name}.caps_signal.tx'
                        ),
                    )
                    cs.start()
                    caps_senders.append(cs)

                transmitter.loop()
                if transmitter.shutdown_requested:
                    link_logger.info('Transmitter shut down cleanly')
                    return
            except Exception as exc:
                link_logger.exception(
                    'Transmitter pipeline failed; restarting in 0.5s'
                )
                telemetry.set_link_up(False)
                telemetry.record_pipeline_restart(reason=type(exc).__name__)
                link_logger.info(
                    'pipeline restart scheduled in 0.5s',
                    extra={
                        'event': EVT_PIPELINE_RESTART,
                        'reason': type(exc).__name__,
                    },
                )
                time.sleep(0.5)
            finally:
                for cs in caps_senders:
                    cs.stop()
                if shared_pysock is not None:
                    try:
                        shared_pysock.close()
                    except OSError:
                        pass
                for sock in path_sockets:
                    try:
                        sock.close()
                    except OSError:
                        pass

    def _run_rx_loop(self, link_config, audio_interface, RTPReceiver,
                     link_logger, telemetry):
        # Caps-signal authentication key (derived once; same for the
        # bootstrap wait and the runtime demux probe inside RTPReceiver).
        caps_key = (
            resolve_caps_key(
                link_name=link_config.name,
                psk=link_config.psk,
                root_psk=link_config.root_psk,
            ) if link_config.security == 'psk' else None
        )

        first_iteration = True
        while True:
            try:
                if not first_iteration:
                    telemetry.reset_counter_deltas()
                first_iteration = False
                peer_addr: tuple[str, int] | None = None
                if link_config.caps_from_config:
                    caps = link_config.derived_rtp_caps()
                    link_logger.info(
                        'caps_from_config=true; using locally-derived caps: %s', caps,
                    )
                    # caps_from_config skips the in-band recvfrom, so the
                    # only way to get a back-channel target for RTX NACKs
                    # is the explicit tx_host config field. When unset we
                    # quietly run without RTX even if TX has it on.
                    if link_config.tx_host:
                        peer_addr = (link_config.tx_host, link_config.port)
                    else:
                        link_logger.info(
                            'caps_from_config + no tx_host: RTX disabled on RX '
                            '(set tx_host to enable NACK back-channel)',
                        )
                else:
                    link_logger.info(
                        'Waiting for in-band caps signal on UDP %d (link=%s, auth=%s)',
                        link_config.port, link_config.name,
                        'aes-256-gcm' if caps_key else 'none',
                    )
                    caps, peer_addr = wait_for_caps(
                        port=link_config.port,
                        link_name=link_config.name,
                        timeout=_CAPS_WAIT_TIMEOUT_SECONDS,
                        caps_key=caps_key,
                        logger=self.logger_factory.getLogger(
                            f'link.{link_config.name}.caps_signal.rx'
                        ),
                        telemetry=telemetry,
                    )
                    link_logger.info('Got caps from in-band signal: %s', caps)

                link_logger.info('Starting up receiver')
                receiver = RTPReceiver(
                    self.node_name, link_config, audio_interface, caps,
                    peer_addr=peer_addr,
                    telemetry=telemetry,
                )
                receiver.run()
                receiver.loop()
                if receiver.shutdown_requested:
                    link_logger.info('Receiver shut down cleanly')
                    return
            except Exception as exc:
                link_logger.exception(
                    'Receiver pipeline failed; restarting in 0.1s'
                )
                telemetry.set_link_up(False)
                telemetry.record_pipeline_restart(reason=type(exc).__name__)
                link_logger.info(
                    'pipeline restart scheduled in 0.1s',
                    extra={
                        'event': EVT_PIPELINE_RESTART,
                        'reason': type(exc).__name__,
                    },
                )
                time.sleep(0.1)


def _build_shared_tx_socket(link_config: LinkConfig) -> socket.socket:
    """Build the TX-side UDP socket that audio + OPOB caps + RTCP feedback
    all share when RTX is enabled.

    The audio udpsink, RTCP udpsrc, and CapsSignalSender each get a handle
    to this single socket so they share one local source port. That's what
    lets RX target the NACK back-channel by replying to the OPOB packet's
    source address — TX's NAT then routes the NACKs to the same socket the
    audio is leaving on.

    ``link_config`` is intentionally unused today (we bind ephemerally so
    multiple TXes can coexist on one host without colliding); it's part of
    the signature so a future ``bind_port`` knob can be added without
    rippling through the call sites.
    """
    del link_config  # reserved for future bind_port support
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', 0))  # ephemeral local port — NAT mapping is what RX targets
    return sock


def _build_path_socket(path: TxPath) -> socket.socket:
    """Build a TX socket pinned to one egress (interface and/or source IP).

    ``iface`` (Linux only) uses ``IP_UNICAST_IF`` — egress-interface pinning
    that does NOT require ``CAP_NET_RAW`` and lets the kernel re-pick the
    source IP each send (so 4G modems with dynamic IPs Just Work). ``bind``
    pins the source IP and is portable but doesn't survive IP rebinds. Both
    can be set together — useful when an interface has multiple IPs and you
    want a specific one.

    The socket is bound to an ephemeral local port so multiple TXes can
    coexist on one host without colliding.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if path.iface is not None:
        if sys.platform != 'linux':  # config validation should have caught this
            raise RuntimeError(
                f'tx_paths.iface is Linux-only (got {sys.platform})'
            )
        try:
            idx = socket.if_nametoindex(path.iface)
        except OSError as exc:
            raise RuntimeError(
                f'tx_paths.iface={path.iface!r} not found: {exc}'
            ) from exc
        # IP_UNICAST_IF takes the interface index in NETWORK byte order.
        # See ip(7): "The argument is a 4-byte integer ... in network byte
        # order". Easy to get this wrong — host-byte-order index "works"
        # locally but selects iface 0 (default) under any non-trivial setup.
        sock.setsockopt(
            socket.IPPROTO_IP, _IP_UNICAST_IF, struct.pack('!I', idx),
        )
    bind_ip = path.bind if path.bind is not None else ''
    sock.bind((bind_ip, 0))
    return sock
