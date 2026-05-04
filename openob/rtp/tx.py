from __future__ import annotations

import os
import signal
import socket as pysocket
import time
from typing import TYPE_CHECKING

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gio, Gst, GLib  # noqa: E402
Gst.init(None)

from openob._log_events import (  # noqa: E402
    EVT_LEVEL,
    EVT_PATH_STATS,
    EVT_PIPELINE_STARTED,
    EVT_RTX_STATS,
)
from openob.link_config import sanitize_caps_for_wire  # noqa: E402
from openob.logger import LoggerFactory  # noqa: E402
from openob.rtp._gst_helpers import (  # noqa: E402
    channel_descriptor,
    channels_from_pad,
    format_peaks,
    make_element,
    request_pad,
)
from openob.security import (  # noqa: E402
    SRTP_RTCP_AUTH, SRTP_RTCP_CIPHER, SRTP_RTP_AUTH, SRTP_RTP_CIPHER,
    resolve_srtp_master,
)
from openob.hooks import LinkHookRunner  # noqa: E402
from openob.telemetry import TelemetryRegistry  # noqa: E402

if TYPE_CHECKING:
    from openob.audio_interface import AudioInterface
    from openob.link_config import LinkConfig


# RTX uses a second RTP payload type to carry retransmissions alongside the
# primary stream. OpenOB hardcodes the mapping primary=96 → rtx=97 because
# we only run one stream per link.
_RTX_ORIGINAL_PT = 96
_RTX_PT = 97


class RTPTransmitter:

    def __init__(
        self,
        node_name: str,
        link_config: LinkConfig,
        audio_interface: AudioInterface,
        shared_socket: pysocket.socket | None = None,
        path_sockets: list[pysocket.socket] | None = None,
        telemetry: TelemetryRegistry | None = None,
        hooks: LinkHookRunner | None = None,
    ) -> None:
        """Sets up a new RTP transmitter.

        ``shared_socket`` is supplied by ``Node._run_tx_loop`` only when
        retransmission is enabled (and multipath is OFF), so the audio
        udpsink, the RTCP udpsrc (for inbound NACKs), and the OPOB caps
        sender all share one local UDP source port. That single source port
        is what RX targets when sending NACKs — TX's NAT then routes them
        back to the same socket the audio is leaving on.

        ``path_sockets`` is supplied when ``link_config.tx_paths`` is
        non-empty (multipath). One pre-bound UDP socket per egress path,
        with iface (IP_UNICAST_IF) and/or source-IP bind already applied.
        Path 0's socket plays the same RTX/caps role that
        ``shared_socket`` plays in the single-path case; paths 1..N are
        write-only audio.
        """

        self.link_config = link_config
        self.audio_interface = audio_interface
        self.shared_socket = shared_socket
        self.path_sockets = path_sockets
        self.telemetry = telemetry or TelemetryRegistry.disabled()
        self.hooks = hooks or LinkHookRunner.disabled()
        self.multipath = bool(link_config.tx_paths)
        if self.multipath:
            if path_sockets is None or len(path_sockets) != len(link_config.tx_paths):
                raise RuntimeError(
                    f'multipath needs {len(link_config.tx_paths)} path sockets, '
                    f'got {0 if path_sockets is None else len(path_sockets)}'
                )
        # In multipath, the primary path (index 0) is the RTX socket; in
        # single-path, the shared_socket is. Either way, RTX is on iff
        # we have a primary socket AND the config requests it.
        if self.multipath:
            assert path_sockets is not None  # validated above
            primary_socket: pysocket.socket | None = path_sockets[0]
        else:
            primary_socket = shared_socket
        self._primary_socket = primary_socket
        self.rtx_enabled = (
            primary_socket is not None and link_config.retransmission_time_limit > 0
        )
        self._rtprtxsend = None  # populated by aux-sender when RTX is on
        self._path_udpsinks: list[Gst.Element] = []  # for per-path stats
        self._rtx_stats_timeout_id: int | None = None
        self._path_stats_timeout_id: int | None = None
        self._path_prev_bytes: list[int] = []
        # Test-only hook: insert a netsim element with the configured
        # drop-probability AFTER rtprtxsend (so RTX has buffered the
        # packet before it gets dropped). Set via OPENOB_TEST_PACKET_LOSS_PERCENT
        # for loss-injection tests; not part of the user-facing config.
        loss_pct_str = os.environ.get('OPENOB_TEST_PACKET_LOSS_PERCENT')
        try:
            self._test_loss_fraction = (
                float(loss_pct_str) / 100.0 if loss_pct_str else 0.0
            )
        except ValueError:
            self._test_loss_fraction = 0.0

        self.logger_factory = LoggerFactory()
        self.logger = self.logger_factory.getLogger('node.%s.link.%s.%s' % (node_name, self.link_config.name, self.audio_interface.mode))
        self.logger.info('Creating transmission pipeline')

        self.build_pipeline()

    def run(self) -> None:
        self.pipeline.set_state(Gst.State.PLAYING)
        # Gst.debug_bin_to_dot_file(self.pipeline, Gst.DebugGraphDetails.ALL, 'tx-graph')

        # Poll the caps-source pad for the negotiated caps. With SRTP the
        # caps will include srtp-key etc., which sanitize_caps_for_wire
        # strips before the in-band caps signal goes out. In multipath we
        # read from the primary udpsink (path 0); caps are identical across
        # paths because they all branch from the same upstream chain.
        caps_source_name = 'udpsink-primary' if self.multipath else 'udpsink'
        while self.caps is None:
            caps = self.transport.get_by_name(caps_source_name).get_static_pad('sink').get_property('caps')

            if caps is None:
                self.logger.warning('Waiting for audio interface/caps')
                time.sleep(0.1)
            else:
                raw = caps.to_string()
                self.caps = sanitize_caps_for_wire(raw)
                if self.caps != raw:
                    self.logger.debug(
                        'Stripped secret fields from caps for in-band signal '
                        '(raw=%s, sanitized=%s)', raw, self.caps,
                    )

        # Advertise RTX support to the RX via the in-band caps. RX parses
        # rtx-time / rtx-pt-map and only enables its do-retransmission +
        # rtprtxreceive when present (so v6.0 receivers stay passive).
        # The pt-map value is quoted because GStreamer's caps parser
        # otherwise chokes on the nested '=' inside an unquoted string.
        if self.rtx_enabled:
            self.caps = (
                f'{self.caps},'
                f'rtx-time=(uint){self.link_config.retransmission_time_limit},'
                f'rtx-pt-map=(string)"{_RTX_ORIGINAL_PT}={_RTX_PT}"'
            )
            # Periodic RTX stats so operators (and tests) can confirm
            # retransmission is actually happening. Once a second is
            # plenty — these counters move on the order of NACKs/sec.
            if self._rtx_stats_timeout_id is None:
                self._rtx_stats_timeout_id = GLib.timeout_add_seconds(
                    1, self._poll_tx_stats,
                )
        else:
            # No RTX, but we still want the rtpsession poll for sent-packet
            # counters and jitter — operators care about throughput on
            # vanilla unicast/multicast links too.
            if self._rtx_stats_timeout_id is None:
                self._rtx_stats_timeout_id = GLib.timeout_add_seconds(
                    1, self._poll_tx_stats,
                )

        # Per-path stats are how operators on Pi-grade hardware (no tcpdump
        # in the field) confirm both modems are actually carrying traffic.
        if self.multipath and self._path_stats_timeout_id is None:
            self._path_prev_bytes = [0] * len(self._path_udpsinks)
            self._path_stats_timeout_id = GLib.timeout_add_seconds(
                1, self._log_path_stats,
            )

    def _request_shutdown(self) -> bool:
        if self.shutdown_requested:
            return GLib.SOURCE_REMOVE
        self.shutdown_requested = True
        self.logger.info('Shutdown signal received, sending EOS')
        self.pipeline.send_event(Gst.Event.new_eos())
        return GLib.SOURCE_REMOVE

    def loop(self) -> None:
        self.main_loop = GLib.MainLoop()
        sigterm_id = GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, self._request_shutdown)
        sigint_id = GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, self._request_shutdown)
        try:
            self.main_loop.run()
        except Exception as e:
            self.logger.exception('Encountered a problem in the MainLoop: %s' % e)
        finally:
            self.pipeline.set_state(Gst.State.NULL)
            GLib.source_remove(sigterm_id)
            GLib.source_remove(sigint_id)
            if self._rtx_stats_timeout_id is not None:
                GLib.source_remove(self._rtx_stats_timeout_id)
                self._rtx_stats_timeout_id = None
            if self._path_stats_timeout_id is not None:
                GLib.source_remove(self._path_stats_timeout_id)
                self._path_stats_timeout_id = None

    def build_pipeline(self):
        self.pipeline = Gst.Pipeline.new('tx')

        self.started = False
        self.shutdown_requested = False
        self.caps: str | None = None

        bus = self.pipeline.get_bus()

        self.source = self.build_audio_interface()
        self.encoder = self.build_encoder()
        self.transport = self.build_transport()

        self.pipeline.add(self.source)
        self.pipeline.add(self.encoder)
        self.pipeline.add(self.transport)
        self.source.link(self.encoder)
        self.encoder.link(self.transport)

        # Connect our bus up
        bus.add_signal_watch()
        bus.connect('message', self.on_message)

    def build_audio_interface(self):
        self.logger.debug('Building audio input bin')
        bin = Gst.Bin.new('audio')

        # Audio input
        if self.audio_interface.type == 'auto':
            source = make_element('autoaudiosrc')
        elif self.audio_interface.type == 'alsa':
            source = make_element('alsasrc')
            source.set_property('device', self.audio_interface.alsa_device)
        elif self.audio_interface.type == 'jack':
            source = make_element('jackaudiosrc')
            if self.audio_interface.jack_auto:
                source.set_property('connect', 'auto')
            else:
                source.set_property('connect', 'none')
            source.set_property('buffer-time', 50000)
            source.set_property('name', self.audio_interface.jack_name)
            source.set_property('client-name', self.audio_interface.jack_name)
            if self.audio_interface.jack_port_pattern:
                source.set_property('port-pattern', self.audio_interface.jack_port_pattern)
        elif self.audio_interface.type in ('coreaudio', 'wasapi', 'pulse'):
            # Native OS audio: CoreAudio / WASAPI2 / PulseAudio. Gst.Device.create_element()
            # returns an element with the right backend's `device` property pre-set
            # (uint for CoreAudio, string for wasapi2/pulse), so one branch covers all
            # three. None is returned by resolve_audio_device when no audio_device was
            # specified — we then build a bare element so the backend default kicks in.
            from openob.audio_device import resolve_audio_device
            resolved = resolve_audio_device(
                backend=self.audio_interface.type,
                direction='source',
                name_or_id=self.audio_interface.audio_device,
                logger=self.logger,
            )
            if resolved is not None:
                source = resolved.gst_device.create_element('audio-source')
                if source is None:
                    raise RuntimeError(
                        f'Gst.Device.create_element() returned None for '
                        f'{self.audio_interface.type} device {resolved.display_name!r} '
                        f'(id={resolved.native_id})'
                    )
            else:
                factory = {
                    'coreaudio': 'osxaudiosrc',
                    'wasapi':    'wasapi2src',
                    'pulse':     'pulsesrc',
                }[self.audio_interface.type]
                source = make_element(factory)
                self.logger.info(
                    '%s source: backend default (no audio_device set)',
                    self.audio_interface.type,
                )
            if self.audio_interface.type == 'pulse':
                # client-name lets `pavucontrol` identify the link by name —
                # mirrors the JACK pattern below.
                source.set_property('client-name', f'openob-{self.link_config.name}')
        elif self.audio_interface.type == 'test':
            source = make_element('audiotestsrc')
            source.set_property('is-live', True)
            wave = self.audio_interface.test_wave or 'sine'
            source.set_property('wave', wave)
            self.logger.info('audiotestsrc wave=%s' % wave)
        else:
            raise ValueError('Unknown audio input type %s' % self.audio_interface.type)

        bin.add(source)

        # Level monitor: kept right after the source — moving it downstream
        # of the capsfilter caused rtpL16pay caps negotiation to stall at
        # startup. The descriptor logic in _log_pipeline_started reads
        # post-capsfilter caps from the bin's src ghost pad to get the
        # transmitted channel count, so a --channels upmix is still reflected
        # accurately in the "Started X audio transmission (...)" line even
        # though level peak metering reflects source-native channels.
        self.level_element = make_element('level')
        self.level_element.set_property('post-messages', True)
        self.level_element.set_property('interval', 1000000000)
        bin.add(self.level_element)

        # Audio resampling and conversion
        resample = make_element('audioresample')
        resample.set_property('quality', 9)  # SRC
        bin.add(resample)

        convert = make_element('audioconvert')
        bin.add(convert)

        # Add a capsfilter to allow specification of input sample rate / channels
        capsfilter = make_element('capsfilter')

        caps = Gst.Caps.new_empty_simple('audio/x-raw')

        if self.link_config.samplerate != 0:
            caps.set_value('rate', self.link_config.samplerate)
        if self.link_config.channels != 0:
            caps.set_value('channels', self.link_config.channels)

        self.logger.debug('TX capsfilter: %s', caps.to_string())
        capsfilter.set_property('caps', caps)
        bin.add(capsfilter)

        source.link(self.level_element)
        self.level_element.link(resample)
        resample.link(convert)
        convert.link(capsfilter)

        bin.add_pad(Gst.GhostPad.new('src', capsfilter.get_static_pad('src')))

        return bin

    def build_encoder(self):
        self.logger.debug('Building encoder bin')
        bin = Gst.Bin.new('encoder')

        encoder = None

        # Encoding and payloading
        if self.link_config.encoding == 'opus':
            encoder = make_element('opusenc', 'encoder')
            encoder.set_property('bitrate', int(self.link_config.bitrate) * 1000)
            encoder.set_property('frame-size', self.link_config.opus_framesize)
            encoder.set_property('complexity', int(self.link_config.opus_complexity))
            encoder.set_property('inband-fec', self.link_config.opus_fec)
            encoder.set_property('packet-loss-percentage', int(self.link_config.opus_loss_expectation))
            encoder.set_property('dtx', self.link_config.opus_dtx)

            # Opus Quality Extensions (libopus >=1.3). Only touch the encoder
            # if explicitly enabled, so older GStreamer builds without the
            # 'enable-qext' property keep working unchanged.
            if self.link_config.opus_qext:
                self._apply_opus_qext(encoder)

            payloader = make_element('rtpopuspay', 'payloader')
        elif self.link_config.encoding == 'pcm':
            # we have no encoder for PCM operation
            payloader = make_element('rtpL16pay', 'payloader')
        else:
            self.logger.critical('Unknown encoding type %s' % self.link_config.encoding)
            raise ValueError('Unknown encoding type %s' % self.link_config.encoding)

        bin.add(payloader)

        if encoder is not None:
            bin.add(encoder)
            encoder.link(payloader)
            bin.add_pad(Gst.GhostPad.new('sink', encoder.get_static_pad('sink')))
        else:
            bin.add_pad(Gst.GhostPad.new('sink', payloader.get_static_pad('sink')))

        bin.add_pad(Gst.GhostPad.new('src', payloader.get_static_pad('src')))

        return bin

    def build_transport(self):
        self.logger.debug('Building RTP transport bin')
        bin = Gst.Bin.new('transport')

        # Our RTP manager
        rtpbin = make_element('rtpbin', 'rtpbin')
        rtpbin.set_property('latency', 0)
        bin.add(rtpbin)
        # Stashed so the stats poll timer can reach into rtpsession's
        # stats struct (packets-sent, jitter, RTT) without a dictionary
        # lookup on every poll.
        self._rtpbin = rtpbin

        if self.multipath:
            # Multipath: identical SRTP/RTP packets fan out to N egresses.
            # The variable `udpsink` below is the tee — SRTP/netsim/direct
            # linking lands on the tee. Each tee.src_<i> → queue_<i> →
            # udpsink_<i> with its own pre-bound, iface-pinned UDP socket.
            udpsink = make_element('tee', 'tx-tee')
            bin.add(udpsink)
            assert self.path_sockets is not None  # validated in __init__

            for i, (path, sock) in enumerate(zip(
                self.link_config.tx_paths, self.path_sockets,
            )):
                label = path.label or f'path{i}'
                # Per-path queue is the backpressure isolator: leaky=downstream
                # + small max-size-time means a wedged modem drops its OWN
                # oldest queued packets, never blocks the tee. See plan's
                # Backpressure isolation section.
                queue = make_element('queue', f'tx-queue-{label}')
                queue.set_property('leaky', 2)  # GST_QUEUE_LEAK_DOWNSTREAM
                queue.set_property('max-size-time', 100 * Gst.MSECOND)
                queue.set_property('max-size-buffers', 0)
                queue.set_property('max-size-bytes', 0)
                bin.add(queue)

                is_primary = (i == 0)
                # Path 0 is named 'udpsink-primary' for run()'s caps poll.
                udpsink_i = make_element(
                    'udpsink',
                    'udpsink-primary' if is_primary else f'udpsink-{label}',
                )
                udpsink_i.set_property('host', path.host)
                udpsink_i.set_property(
                    'port',
                    path.port if path.port is not None else self.link_config.port,
                )
                # Small SO_SNDBUF: keeps the kernel send buffer shallow so
                # a wedged interface can't silently absorb seconds of stale
                # audio. ~100 ms of 128 kbps OPUS.
                udpsink_i.set_property('buffer-size', 65536)
                udpsink_i.set_property('sync', False)
                udpsink_i.set_property('async', False)
                gsock_i = Gio.Socket.new_from_fd(os.dup(sock.fileno()))
                udpsink_i.set_property('socket', gsock_i)
                udpsink_i.set_property('close-socket', False)
                bin.add(udpsink_i)
                self._path_udpsinks.append(udpsink_i)

                tee_src = request_pad(udpsink, 'src_%u')
                if tee_src is None:
                    raise RuntimeError(
                        f'tee src request pad failed for path {label}'
                    )
                link_ret = tee_src.link(queue.get_static_pad('sink'))
                if link_ret != Gst.PadLinkReturn.OK:
                    raise RuntimeError(
                        f'failed to link tee → queue for path {label}: {link_ret}'
                    )
                if not queue.link(udpsink_i):
                    raise RuntimeError(
                        f'failed to link queue → udpsink for path {label}'
                    )

                self.logger.info(
                    'tx_path[%d] label=%s host=%s:%d iface=%s bind=%s '
                    'socket-fd=%d%s',
                    i, label, path.host,
                    path.port if path.port is not None else self.link_config.port,
                    path.iface, path.bind, sock.fileno(),
                    ' (primary)' if is_primary else '',
                )

            if self.rtx_enabled:
                self.logger.info(
                    'RTX enabled (max-size-time=%dms) — primary path (label=%s) '
                    'carries RTCP NACKs back; caps go on every path',
                    self.link_config.retransmission_time_limit,
                    self.link_config.tx_paths[0].label or 'path0',
                )
                # MUST be connected BEFORE the pad request below.
                rtpbin.connect('request-aux-sender', self._on_request_aux_sender)
        else:
            udpsink = make_element('udpsink', 'udpsink')
            udpsink.set_property('host', self.link_config.receiver_host)
            udpsink.set_property('port', self.link_config.port)
            self.logger.info('Set receiver to %s:%i' % (self.link_config.receiver_host, self.link_config.port))

            if self.link_config.multicast:
                udpsink.set_property('auto-multicast', True)
                self.logger.info('Multicast mode enabled')

            # When RTX is enabled, audio + OPOB caps + inbound RTCP NACKs all
            # ride one Python-owned UDP socket so they share a NAT mapping. We
            # dup the fd so the GIO socket has its own lifecycle from the
            # Python socket — close-socket=False keeps the GIO side from
            # nuking the underlying fd when the pipeline goes to NULL.
            if self.rtx_enabled and self.shared_socket is not None:
                gsock = Gio.Socket.new_from_fd(os.dup(self.shared_socket.fileno()))
                udpsink.set_property('socket', gsock)
                udpsink.set_property('close-socket', False)
                self.logger.info(
                    'RTX enabled (max-size-time=%dms) — sharing UDP socket fd=%d for '
                    'audio + OPOB caps + RTCP NACKs',
                    self.link_config.retransmission_time_limit,
                    self.shared_socket.fileno(),
                )

                # Inject rtprtxsend via rtpbin's request-aux-sender. MUST be
                # connected BEFORE we request 'send_rtp_sink_0' below, since
                # the pad request triggers the signal.
                rtpbin.connect('request-aux-sender', self._on_request_aux_sender)
            bin.add(udpsink)

        bin.add_pad(Gst.GhostPad.new('sink', request_pad(rtpbin, 'send_rtp_sink_0')))

        if self.link_config.security == 'psk':
            # Insert srtpenc between rtpbin and udpsink. srtpenc has a
            # request sink (rtp_sink_%u) and the matching src (rtp_src_%u)
            # is created paired with it at request time — so we must
            # connect pad-added BEFORE requesting, AND also handle the case
            # where the pad already exists by the time control returns.
            srtpenc = make_element('srtpenc', 'srtpenc')
            master = resolve_srtp_master(
                link_name=self.link_config.name,
                psk=self.link_config.psk,
                root_psk=self.link_config.root_psk,
            )
            srtpenc.set_property('key', Gst.Buffer.new_wrapped(master))
            srtpenc.set_property('rtp-cipher', SRTP_RTP_CIPHER)
            srtpenc.set_property('rtcp-cipher', SRTP_RTCP_CIPHER)
            srtpenc.set_property('rtp-auth', SRTP_RTP_AUTH)
            srtpenc.set_property('rtcp-auth', SRTP_RTCP_AUTH)
            bin.add(srtpenc)
            self.logger.info(
                'SRTP enabled (cipher=%s/%s auth=%s/%s, %d-byte master key+salt)',
                SRTP_RTP_CIPHER, SRTP_RTCP_CIPHER,
                SRTP_RTP_AUTH, SRTP_RTCP_AUTH, len(master),
            )

            def link_srtp_src_to_udpsink(pad) -> None:
                if not pad.get_name().startswith('rtp_src_'):
                    return
                udpsink_pad = udpsink.get_static_pad('sink')
                if udpsink_pad is None or udpsink_pad.is_linked():
                    return
                ret = pad.link(udpsink_pad)
                if ret != Gst.PadLinkReturn.OK:
                    self.logger.error('srtpenc src → udpsink link failed: %s', ret)
                else:
                    self.logger.debug('srtpenc src → udpsink linked')

            srtpenc.connect(
                'pad-added',
                lambda _elem, pad: link_srtp_src_to_udpsink(pad),
            )

            srtpenc_sink = request_pad(srtpenc, 'rtp_sink_0')
            assert srtpenc_sink is not None, 'srtpenc.rtp_sink_0 request failed'
            ok = rtpbin.link_pads('send_rtp_src_0', srtpenc, srtpenc_sink.get_name())
            if not ok:
                raise RuntimeError('failed to link rtpbin → srtpenc')

            # Belt-and-suspenders: if pad-added fired between bin.add and
            # the connect above (paired-pad creation can happen synchronously),
            # walk srtpenc's existing pads and link any rtp_src_X we missed.
            it = srtpenc.iterate_src_pads()
            while True:
                ok, pad = it.next()
                if ok != Gst.IteratorResult.OK:
                    break
                link_srtp_src_to_udpsink(pad)
        else:
            # Optional test-only loss injection. netsim sits between
            # rtpbin's send_rtp_src and udpsink; rtprtxsend (inside rtpbin
            # via aux-sender) has already buffered each packet at this
            # point, so dropped packets can still be retransmitted on
            # NACK. Gated behind an env var so production runs never
            # accidentally enable it.
            if self._test_loss_fraction > 0:
                netsim = make_element('netsim', 'netsim')
                netsim.set_property('drop-probability', self._test_loss_fraction)
                bin.add(netsim)
                ok = (
                    rtpbin.link_pads('send_rtp_src_0', netsim, 'sink')
                    and netsim.link(udpsink)
                )
                if not ok:
                    raise RuntimeError(
                        'failed to splice netsim between rtpbin and udpsink'
                    )
                self.logger.info(
                    'OPENOB_TEST_PACKET_LOSS_PERCENT=%.2f → netsim drop-probability=%.4f',
                    self._test_loss_fraction * 100, self._test_loss_fraction,
                )
            else:
                rtpbin.link_pads('send_rtp_src_0', udpsink, 'sink')

        # Inbound RTCP path (NACKs from RX). Only built when RTX is on; we
        # share the audio udpsink's socket so RX can target it by replying
        # to OPOB's source address. In multipath this is the primary path's
        # socket (path 0); paths 1..N are write-only audio. udpsrc gets caps
        # tagged x-rtcp so rtpbin's recv_rtcp_sink_0 takes them on the right
        # internal session.
        if self.rtx_enabled and self._primary_socket is not None:
            gsock_in = Gio.Socket.new_from_fd(os.dup(self._primary_socket.fileno()))
            udpsrc_rtcp = make_element('udpsrc', 'udpsrc-rtcp')
            udpsrc_rtcp.set_property('socket', gsock_in)
            udpsrc_rtcp.set_property('close-socket', False)
            udpsrc_rtcp.set_property(
                'caps', Gst.Caps.from_string('application/x-rtcp'),
            )
            bin.add(udpsrc_rtcp)
            ok = udpsrc_rtcp.link_pads('src', rtpbin, 'recv_rtcp_sink_0')
            if not ok:
                raise RuntimeError(
                    'failed to link RTCP udpsrc → rtpbin.recv_rtcp_sink_0'
                )

        return bin

    def _poll_tx_stats(self) -> bool:
        """1 Hz: read rtprtxsend + rtpsession stats, log + push to telemetry.

        Combined into one timer callback so a polling tick reads each
        element exactly once per second. Counter resets across pipeline
        restarts are absorbed by ``TelemetryRegistry``'s delta tracker.

        Returns ``GLib.SOURCE_CONTINUE`` to stay subscribed; the timer
        is removed by ``loop()``'s finally block on shutdown.
        """
        # rtprtxsend stats — only present when RTX is enabled.
        if self._rtprtxsend is not None:
            try:
                requests = self._rtprtxsend.get_property('num-rtx-requests')
                packets = self._rtprtxsend.get_property('num-rtx-packets')
            except (TypeError, AttributeError):
                # Stats unreadable → don't disable the whole timer; the
                # session stats below may still be readable.
                requests = packets = None
            else:
                self.logger.info(
                    'RTX stats: nack-requests=%d packets-resent=%d',
                    requests, packets,
                    extra={
                        'event': EVT_RTX_STATS,
                        'nack_requests': requests,
                        'packets_resent': packets,
                    },
                )
                self.telemetry.update_rtx_send_stats(
                    requests=requests, packets=packets,
                )

        # rtpsession stats — packets-sent, jitter, etc. Available even
        # without RTX. emit('get-internal-session', 0) returns None until
        # the session has seen its first packet; tolerate that.
        try:
            session = self._rtpbin.emit('get-internal-session', 0)
        except (TypeError, AttributeError):
            session = None
        if session is not None:
            try:
                stats = session.get_property('stats')
            except (TypeError, AttributeError):
                stats = None
            if stats is not None:
                self.telemetry.update_rtp_session_stats(
                    stats, clock_rate=self._rtp_clock_rate(),
                )
        return GLib.SOURCE_CONTINUE

    def _rtp_clock_rate(self) -> int:
        """Return the negotiated RTP clock-rate for jitter conversion.

        Opus is always 48 kHz internally regardless of input samplerate;
        PCM uses the configured samplerate. Falls back to 0 (which the
        telemetry layer treats as "skip the gauge") when neither known.
        """
        if self.link_config.encoding == 'opus':
            return 48000
        if self.link_config.encoding == 'pcm':
            return self.link_config.samplerate or 0
        return 0

    def _log_path_stats(self) -> bool:
        """Periodic per-path bytes-served / rate logging.

        On Pi-grade hardware in the field operators have no tcpdump; this
        log line is the only way to see "modem A is alive, modem B has
        gone dark". 1 s cadence — same as RTX stats.
        """
        for i, (path, udpsink) in enumerate(zip(
            self.link_config.tx_paths, self._path_udpsinks,
        )):
            try:
                served = udpsink.get_property('bytes-served')
            except (TypeError, AttributeError):
                # bytes-served only appeared in newer GStreamers; if we
                # can't read it, drop the timer rather than spam errors.
                return GLib.SOURCE_REMOVE
            delta = served - self._path_prev_bytes[i]
            self._path_prev_bytes[i] = served
            label = path.label or f'path{i}'
            self.logger.info(
                'tx_path %s: %.1f KB/s (bytes-served=%d)',
                label, delta / 1024.0, served,
                extra={
                    'event': EVT_PATH_STATS,
                    'path': label,
                    'kb_per_sec': round(delta / 1024.0, 1),
                    'bytes_served': served,
                },
            )
            self.telemetry.add_path_bytes(label=label, bytes_served=served)
        return GLib.SOURCE_CONTINUE

    def _on_request_aux_sender(self, _rtpbin, session_id):
        """Build the rtprtxsend bin that rtpbin inserts between its send
        session and the transport. Called once per session when the first
        send_rtp_sink_<sid> pad is requested.
        """
        aux = Gst.Bin.new(f'rtx-aux-sender-{session_id}')
        rtx = make_element('rtprtxsend', 'rtprtxsend')
        # max-size-time is in ns at the GObject level. Our config knob is ms
        # — the small unit users actually think in for STL link tuning.
        rtx.set_property(
            'max-size-time',
            self.link_config.retransmission_time_limit * Gst.MSECOND,
        )
        # PT mapping is original → rtx. String parse is the most portable
        # PyGObject form for a Gst.Structure with uint values.
        pt_map_struct = Gst.Structure.from_string(
            f'application/x-rtp-pt-map,{_RTX_ORIGINAL_PT}=(uint){_RTX_PT}'
        )[0]
        rtx.set_property('payload-type-map', pt_map_struct)
        aux.add(rtx)
        aux.add_pad(Gst.GhostPad.new(
            f'sink_{session_id}', rtx.get_static_pad('sink'),
        ))
        aux.add_pad(Gst.GhostPad.new(
            f'src_{session_id}', rtx.get_static_pad('src'),
        ))
        # Stash the rtprtxsend element so the stats timer can poll its
        # num-rtx-requests / num-rtx-packets counters. Only one session
        # per OpenOB link, so a single attribute is enough.
        self._rtprtxsend = rtx
        self.logger.debug(
            'aux-sender bin built for session %d (rtprtxsend %d→%d)',
            session_id, _RTX_ORIGINAL_PT, _RTX_PT,
        )
        return aux

    def on_message(self, bus, message) -> bool:
        del bus
        if message.type == Gst.MessageType.EOS:
            self.logger.info('Pipeline reached EOS, exiting main loop')
            self.telemetry.set_link_up(False)
            # 'down' on EOS is correct: a clean shutdown will overwrite
            # this with 'shutdown' from Node's finally-block once stop()
            # propagates. A failure-driven EOS leaves 'down' set, which
            # is what the operator wants to see.
            self.hooks.set_status('down', reason='eos')
            if hasattr(self, 'main_loop'):
                self.main_loop.quit()
            return True
        if message.type == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            self.logger.error('Pipeline error: %s (%s)' % (err, dbg))
            self.telemetry.set_link_up(False)
            self.hooks.set_status('down', reason='pipeline_error')
            if hasattr(self, 'main_loop'):
                self.main_loop.quit()
            return True
        if message.type == Gst.MessageType.ELEMENT:
            struct = message.get_structure()
            if struct is not None and struct.get_name() == 'level':
                peaks = struct.get_value('peak')
                rms = struct.get_value('rms')
                if not self.started:
                    self.started = True
                    self.telemetry.set_link_up(True)
                    self.hooks.set_status('up')
                    self._log_pipeline_started(peaks)
                else:
                    self._log_level(peaks)
                self._push_audio_levels(peaks, rms)
        return True

    def _log_pipeline_started(self, peaks) -> None:
        # Prefer post-capsfilter caps (the bin's src ghost) so the descriptor
        # reflects what's actually being transmitted, including any
        # --channels upmix. Falls back to peak count if caps aren't yet
        # readable on the pad.
        caps_str, n_channels = channels_from_pad(self.source.get_static_pad('src'))
        if n_channels is None:
            n_channels = len(peaks)
        descriptor = channel_descriptor(n_channels)
        # Keep the human-readable message stable — operators (and the
        # e2e test harness) grep for "Started X audio transmission".
        # The event= extra is what JSON consumers key on.
        if caps_str is not None:
            self.logger.info(
                'Started %s audio transmission (%s)',
                descriptor, caps_str,
                extra={
                    'event': EVT_PIPELINE_STARTED,
                    'channels': n_channels,
                    'descriptor': descriptor,
                    'caps': caps_str,
                },
            )
        else:
            self.logger.info(
                'Started %s audio transmission',
                descriptor,
                extra={
                    'event': EVT_PIPELINE_STARTED,
                    'channels': n_channels,
                    'descriptor': descriptor,
                },
            )

    def _log_level(self, peaks) -> None:
        self.logger.debug(
            'Levels: %s', format_peaks(peaks),
            extra={'event': EVT_LEVEL, 'peaks': format_peaks(peaks)},
        )

    def _push_audio_levels(self, peaks, rms) -> None:
        """Push level-element peak/rms arrays to telemetry as gauges.

        Tolerates None / non-iterable values that some GStreamer builds
        produce when the source hasn't decided on a channel count yet.
        """
        if peaks:
            for ch, value in enumerate(peaks):
                self.telemetry.set_audio_peak(ch, float(value))
        if rms:
            for ch, value in enumerate(rms):
                self.telemetry.set_audio_rms(ch, float(value))
        # Mirror the levels into the hook runner so heartbeat snapshots
        # carry recent values. update_audio_levels is a no-op on the
        # disabled singleton; cheap (one lock + list-copy) when active.
        self.hooks.update_audio_levels(peaks, rms)

    def _apply_opus_qext(self, encoder) -> None:
        """
        Enable Opus Quality Extensions on an opusenc element. Emits warnings
        when the configured bitrate is below the qext target range, or when
        the installed opusenc doesn't expose the qext property.
        """
        bitrate_kbps = int(self.link_config.bitrate)
        if bitrate_kbps < 1024:
            self.logger.warning(
                'Opus qext enabled at %d kbps; qext is designed for high-rate '
                'music (>=1024 kbps) and is unlikely to help below that.',
                bitrate_kbps,
            )
        if encoder.find_property('qext') is not None:
            encoder.set_property('qext', True)
            self.logger.info('Opus qext mode enabled on opusenc')
        else:
            self.logger.warning(
                "Opus qext requested but the installed opusenc has no "
                "'qext' property — needs a GStreamer/libopus build "
                "with qext support. Encoding will continue without qext."
            )

    def get_caps(self) -> str | None:
        return self.caps
