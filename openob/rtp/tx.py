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
    ) -> None:
        """Sets up a new RTP transmitter.

        ``shared_socket`` is supplied by ``Node._run_tx_loop`` only when
        retransmission is enabled, so the audio udpsink, the RTCP udpsrc
        (for inbound NACKs), and the OPOB caps sender all share one local
        UDP source port. That single source port is what RX targets when
        sending NACKs — TX's NAT then routes them back to the same socket
        the audio is leaving on.
        """

        self.link_config = link_config
        self.audio_interface = audio_interface
        self.shared_socket = shared_socket
        self.rtx_enabled = (
            shared_socket is not None and link_config.retransmission_time_limit > 0
        )
        self._rtprtxsend = None  # populated by aux-sender when RTX is on
        self._rtx_stats_timeout_id: int | None = None
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

        # Poll the udpsink sink pad for the negotiated caps. With SRTP the
        # caps will include srtp-key etc., which sanitize_caps_for_wire
        # strips before the in-band caps signal goes out.
        while self.caps is None:
            caps = self.transport.get_by_name('udpsink').get_static_pad('sink').get_property('caps')

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
                    1, self._log_rtx_stats,
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

        # TODO: Add a tee here, and sort out creating multiple UDP sinks for multipath
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
        # share the same socket as the audio udpsink so RX can target it
        # by replying to OPOB's source address. udpsrc gets caps tagged
        # x-rtcp so rtpbin's recv_rtcp_sink_0 takes them on the right
        # internal session.
        if self.rtx_enabled and self.shared_socket is not None:
            gsock_in = Gio.Socket.new_from_fd(os.dup(self.shared_socket.fileno()))
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

    def _log_rtx_stats(self) -> bool:
        """Periodic dump of rtprtxsend's NACK/retransmit counters.

        Returns ``GLib.SOURCE_CONTINUE`` to stay subscribed; the timer
        is removed by ``loop()``'s finally block on shutdown.
        """
        if self._rtprtxsend is None:
            return GLib.SOURCE_CONTINUE
        try:
            requests = self._rtprtxsend.get_property('num-rtx-requests')
            packets = self._rtprtxsend.get_property('num-rtx-packets')
        except (TypeError, AttributeError):
            return GLib.SOURCE_REMOVE
        self.logger.info(
            'RTX stats: nack-requests=%d packets-resent=%d',
            requests, packets,
        )
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
        if message.type == Gst.MessageType.EOS:
            self.logger.info('Pipeline reached EOS, exiting main loop')
            if hasattr(self, 'main_loop'):
                self.main_loop.quit()
            return True
        if message.type == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            self.logger.error('Pipeline error: %s (%s)' % (err, dbg))
            if hasattr(self, 'main_loop'):
                self.main_loop.quit()
            return True
        if message.type == Gst.MessageType.ELEMENT:
            struct = message.get_structure()
            if struct is not None and struct.get_name() == 'level':
                peaks = struct.get_value('peak')
                if not self.started:
                    self.started = True
                    self._log_pipeline_started(peaks)
                else:
                    self._log_level(peaks)
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
        if caps_str is not None:
            self.logger.info(
                'Started %s audio transmission (%s)', descriptor, caps_str
            )
        else:
            self.logger.info('Started %s audio transmission', descriptor)

    def _log_level(self, peaks) -> None:
        self.logger.debug('Levels: %s', format_peaks(peaks))

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
