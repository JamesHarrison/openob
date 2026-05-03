from __future__ import annotations

import os
import signal
import socket as pysocket
from typing import TYPE_CHECKING

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gio, Gst, GLib  # noqa: E402
Gst.init(None)


# RTX payload-type pairing — primary stream is always PT 96, retransmissions
# arrive on PT 97. Hardcoded both ends; OpenOB only carries one stream per link.
_RTX_ORIGINAL_PT = 96
_RTX_PT = 97

from openob.caps_signal import (  # noqa: E402
    attach_rtcp_demux_probe, parse_encoding_from_caps, parse_rtx_fields,
)
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


class RTPReceiver:

    def __init__(
        self,
        node_name: str,
        link_config: LinkConfig,
        audio_interface: AudioInterface,
        caps: str,
        peer_addr: tuple[str, int] | None = None,
    ) -> None:
        """Sets up a new RTP receiver.

        ``caps`` is the RTP caps string the TX has told us to expect (via
        the in-band caps signal, or derived locally if --caps-from-config).
        For ``security='psk'`` it should be ``application/x-srtp,...``;
        otherwise ``application/x-rtp,...``.

        ``peer_addr`` is ``(ip, port)`` of the TX side, captured from the
        OPOB caps packet's source address (in-band path) or from the
        ``tx_host`` config field (caps_from_config path). When ``None``,
        RTX is disabled even if the caps advertise it — there's nowhere
        to send NACKs.
        """
        self.link_config = link_config
        self.audio_interface = audio_interface
        self.caps = caps
        self.peer_addr = peer_addr
        self.shutdown_requested = False
        # Set in build_transport when RTX is enabled; kept on self so
        # loop()'s finally block can close it on shutdown.
        self._rx_shared_pysock: pysocket.socket | None = None

        self.logger_factory = LoggerFactory()
        self.logger = self.logger_factory.getLogger(
            'node.%s.link.%s.%s'
            % (node_name, self.link_config.name, self.audio_interface.mode)
        )

        # Pick the depayloader/decoder based on the encoding-name in the
        # caps the TX is actually sending — so the RX TOML doesn't have to
        # repeat what TX already declares. Falls back to link_config.encoding
        # only if caps parse fails (e.g. malformed caps, very old TX).
        try:
            self.encoding = parse_encoding_from_caps(self.caps)
            self.logger.info(
                'Creating reception pipeline (caps=%s, encoding=%s — derived from caps)',
                self.caps, self.encoding,
            )
        except ValueError as exc:
            self.encoding = self.link_config.encoding
            self.logger.warning(
                'Could not derive encoding from caps (%s); falling back to '
                'configured encoding=%s', exc, self.encoding,
            )
            self.logger.info('Creating reception pipeline (caps=%s)', self.caps)

        # Decide whether RTX is on for this session. All three required:
        # TX is advertising (rtx_fields parsed from caps), we know where
        # to send NACKs back (peer_addr), and we're not in multicast mode
        # (RTX needs a unicast back-channel).
        rtx_fields = parse_rtx_fields(self.caps)
        if (
            rtx_fields is not None
            and self.peer_addr is not None
            and not self.link_config.multicast
        ):
            self.rtx_time_ms, self.rtx_pt_map = rtx_fields
            self.rtx_enabled = True
            self.logger.info(
                'RTX enabled (rtx-time=%dms, peer=%s:%d, pt-map=%s)',
                self.rtx_time_ms, self.peer_addr[0], self.peer_addr[1],
                self.rtx_pt_map,
            )
        else:
            self.rtx_time_ms = 0
            self.rtx_pt_map = {}
            self.rtx_enabled = False
            if rtx_fields is not None and self.peer_addr is None:
                self.logger.info(
                    'TX advertised RTX but no peer address available '
                    '(caps_from_config without tx_host); RTX disabled',
                )
            elif rtx_fields is not None and self.link_config.multicast:
                self.logger.info(
                    'TX advertised RTX but multicast mode is on '
                    '(no unicast back-channel); RTX disabled',
                )

        self.build_pipeline()

    def run(self) -> None:
        self.pipeline.set_state(Gst.State.PLAYING)
        self.logger.info(
            'Listening for stream on %s:%i',
            self.link_config.receiver_host or '0.0.0.0',
            self.link_config.port,
        )

    def _request_shutdown(self) -> bool:
        """GLib-aware signal handler: send EOS so wavenc/filesink finalise cleanly."""
        if self.shutdown_requested:
            return GLib.SOURCE_REMOVE
        self.shutdown_requested = True
        self.logger.info('Shutdown signal received, sending EOS')
        self.pipeline.send_event(Gst.Event.new_eos())
        return GLib.SOURCE_REMOVE

    def loop(self) -> None:
        self.main_loop = GLib.MainLoop()
        # Register signal handlers via GLib so they actually fire while the
        # mainloop is blocked in C. Plain signal.signal() doesn't.
        sigterm_id = GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, self._request_shutdown)
        sigint_id = GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, self._request_shutdown)
        try:
            self.main_loop.run()
        except Exception as e:
            self.logger.exception('Encountered a problem in the MainLoop: %s' % e)
        finally:
            # set_state(NULL) drives wavenc through PAUSED→READY→NULL, which
            # rewrites the RIFF header with the final data length. Without
            # this the captured WAV is unparseable.
            self.pipeline.set_state(Gst.State.NULL)
            GLib.source_remove(sigterm_id)
            GLib.source_remove(sigint_id)
            # The dup'd fds inside Gio.Sockets are closed by GStreamer
            # (close-socket=False on the elements but the GIO wrappers own
            # those fds). Our Python socket still owns its original fd.
            if self._rx_shared_pysock is not None:
                try:
                    self._rx_shared_pysock.close()
                except OSError:
                    pass

    def build_pipeline(self):
        self.pipeline = Gst.Pipeline.new('rx')

        self.started = False
        bus = self.pipeline.get_bus()

        self.transport = self.build_transport()
        self.decoder = self.build_decoder()
        self.output = self.build_audio_interface()

        self.pipeline.add(self.transport)
        self.pipeline.add(self.decoder)
        self.pipeline.add(self.output)
        self.transport.link(self.decoder)
        self.decoder.link(self.output)

        bus.add_signal_watch()
        bus.connect('message', self.on_message)

    def build_audio_interface(self):
        self.logger.debug('Building audio output bin')
        bin = Gst.Bin.new('audio')

        # Audio output. The 'wav' type is a test/debug helper that captures the
        # decoded stream to a WAV file via wavenc ! filesink. We pin the format
        # to S16LE via a capsfilter so the captured WAV is predictable across
        # GStreamer versions (wavenc otherwise picks whatever audioconvert
        # negotiates upstream).
        if self.audio_interface.type == 'wav':
            wavenc = make_element('wavenc')
            filesink = make_element('filesink')
            filesink.set_property('location', self.audio_interface.wav_file)
            filesink.set_property('sync', False)

            resample = make_element('audioresample')
            resample.set_property('quality', 9)
            convert = make_element('audioconvert')
            self.level_element = make_element('level')
            self.level_element.set_property('post-messages', True)
            self.level_element.set_property('interval', 1000000000)

            capsfilter = make_element('capsfilter')
            capsfilter.set_property(
                'caps', Gst.Caps.from_string('audio/x-raw,format=S16LE'),
            )

            for el in (resample, convert, capsfilter, self.level_element, wavenc, filesink):
                bin.add(el)

            resample.link(convert)
            convert.link(capsfilter)
            capsfilter.link(self.level_element)
            self.level_element.link(wavenc)
            wavenc.link(filesink)

            bin.add_pad(Gst.GhostPad.new('sink', resample.get_static_pad('sink')))
            self.logger.info('WAV capture mode, writing to %s' % self.audio_interface.wav_file)
            return bin

        if self.audio_interface.type == 'auto':
            sink = make_element('autoaudiosink')
        elif self.audio_interface.type == 'alsa':
            sink = make_element('alsasink')
            sink.set_property('device', self.audio_interface.alsa_device)
        elif self.audio_interface.type == 'jack':
            sink = make_element('jackaudiosink')
            if self.audio_interface.jack_auto:
                sink.set_property('connect', 'auto')
            else:
                sink.set_property('connect', 'none')
            sink.set_property('name', self.audio_interface.jack_name)
            sink.set_property('client-name', self.audio_interface.jack_name)
            if self.audio_interface.jack_port_pattern:
                sink.set_property('port-pattern', self.audio_interface.jack_port_pattern)
        elif self.audio_interface.type == 'test':
            sink = make_element('fakesink')
        else:
            raise ValueError('Unknown audio output type %s' % self.audio_interface.type)

        bin.add(sink)

        # Audio resampling and conversion
        resample = make_element('audioresample')
        resample.set_property('quality', 9)
        bin.add(resample)

        convert = make_element('audioconvert')
        bin.add(convert)

        # Our level monitor, also used for continuous audio
        self.level_element = make_element('level')
        self.level_element.set_property('post-messages', True)
        self.level_element.set_property('interval', 1000000000)
        bin.add(self.level_element)

        resample.link(convert)
        convert.link(self.level_element)
        self.level_element.link(sink)

        bin.add_pad(Gst.GhostPad.new('sink', resample.get_static_pad('sink')))

        return bin

    def build_decoder(self):
        self.logger.debug('Building decoder bin (encoding=%s)', self.encoding)
        bin = Gst.Bin.new('decoder')

        decoder = None

        # Decoding and depayloading. self.encoding is derived from the caps
        # signal in __init__ (so RX adapts to whatever TX is sending),
        # falling back to link_config.encoding only if caps parse fails.
        if self.encoding == 'opus':
            decoder = make_element('opusdec', 'decoder')
            decoder.set_property('use-inband-fec', True)  # FEC
            decoder.set_property('plc', True)  # Packet loss concealment
            depayloader = make_element('rtpopusdepay', 'depayloader')
        elif self.encoding == 'pcm':
            depayloader = make_element('rtpL16depay', 'depayloader')
        else:
            self.logger.critical('Unknown encoding type %s' % self.encoding)
            raise ValueError('Unknown encoding type %s' % self.encoding)

        bin.add(depayloader)

        bin.add_pad(Gst.GhostPad.new('sink', depayloader.get_static_pad('sink')))

        if decoder is not None:
            bin.add(decoder)
            depayloader.link(decoder)
            bin.add_pad(Gst.GhostPad.new('src', decoder.get_static_pad('src')))
        else:
            bin.add_pad(Gst.GhostPad.new('src', depayloader.get_static_pad('src')))

        return bin

    def build_transport(self):
        self.logger.debug('Building RTP transport bin (security=%s)', self.link_config.security)
        bin = Gst.Bin.new('transport')

        # Strip any escaping that crept in (legacy v5 caps had backslashes from
        # Redis storage; v6 wire-derived caps don't, but the helper is cheap).
        caps_str = self.caps.replace('\\', '')
        udpsrc_caps = Gst.Caps.from_string(caps_str)

        # Where audio comes in
        udpsrc = make_element('udpsrc', 'udpsrc')
        udpsrc.set_property('caps', udpsrc_caps)
        udpsrc.set_property('timeout', 3000000000)

        # RTX path needs a unicast back-channel: build a Python socket bound
        # to the listen port, share it (via dup'd Gio.Sockets) with both the
        # udpsrc above AND the udpsink that emits RTCP NACKs. That way our
        # NACKs leave from the same local port the audio arrived on, which
        # keeps things symmetric end-to-end and lets TX's NAT route them.
        # Default path (RTX off, or multicast) keeps the original
        # GStreamer-managed bind by setting the `port` property.
        if self.rtx_enabled:
            shared = self._bind_shared_rx_socket()
            self._rx_shared_pysock = shared
            gsock_in = Gio.Socket.new_from_fd(os.dup(shared.fileno()))
            udpsrc.set_property('socket', gsock_in)
            udpsrc.set_property('close-socket', False)
        else:
            udpsrc.set_property('port', self.link_config.port)

        if self.link_config.multicast:
            udpsrc.set_property('auto-multicast', True)
            udpsrc.set_property('multicast-group', self.link_config.receiver_host)
            self.logger.info('Multicast mode enabled')
        bin.add(udpsrc)

        # RFC 5761 §4 demux: RTCP packets (incl. our muxed OPOB CAPS APP)
        # are dropped before they reach rtpbin / srtpdec; we'd otherwise
        # confuse them. Caps-refresh callback is intentionally None — v6
        # doesn't rebuild the pipeline mid-flight on caps changes; the
        # link supervisor restarts on data timeout (3 s).
        attach_rtcp_demux_probe(udpsrc, on_caps_refresh=None, logger=self.logger)

        rtpbin = make_element('rtpbin', 'rtpbin')
        rtpbin.set_property('latency', self.link_config.jitter_buffer)
        rtpbin.set_property('autoremove', True)
        rtpbin.set_property('do-lost', True)
        if self.rtx_enabled:
            # Propagates to each rtpjitterbuffer rtpbin spawns; jitter
            # buffers then emit upstream RTX request events that rtpbin
            # converts to RTCP NACKs on send_rtcp_src_0. Must be set
            # BEFORE pad request triggers request-aux-receiver.
            rtpbin.set_property('do-retransmission', True)
            rtpbin.connect(
                'request-aux-receiver', self._on_request_aux_receiver,
            )
            # rtprtxreceive sits between udpsrc and rtpbin's session, and
            # the caps event coming out of it loses the clock-rate field
            # the jitter buffer needs. Both rtpbin and the jitter buffer
            # itself emit request-pt-map when they hit a buffer with no
            # known clock-rate — handling both gives the buffer a direct
            # lookup keyed by the PT in each packet.
            rtpbin.connect('request-pt-map', self._on_request_pt_map)
            rtpbin.connect('new-jitterbuffer', self._on_new_jitterbuffer)
        bin.add(rtpbin)

        if self.link_config.security == 'psk':
            srtpdec = make_element('srtpdec', 'srtpdec')
            bin.add(srtpdec)

            # Provide the SRTP master key when srtpdec asks (per-SSRC). The
            # signal returns a Gst.Caps with all the security parameters.
            master = resolve_srtp_master(
                link_name=self.link_config.name,
                psk=self.link_config.psk,
                root_psk=self.link_config.root_psk,
            )
            self._srtp_master = master  # keep alive for the GLib.Bytes lifecycle

            # Build the key-caps via string with the canonical (buffer)<hex>
            # encoding so GStreamer's caps parser produces a real GstBuffer
            # rather than the GLib.Bytes that `set_value` ends up creating
            # (which srtpdec rejects).
            key_hex = master.hex()
            key_caps_str = (
                f'application/x-srtp,'
                f'srtp-key=(buffer){key_hex},'
                f'srtp-cipher=(string){SRTP_RTP_CIPHER},'
                f'srtcp-cipher=(string){SRTP_RTCP_CIPHER},'
                f'srtp-auth=(string){SRTP_RTP_AUTH},'
                f'srtcp-auth=(string){SRTP_RTCP_AUTH}'
            )

            def on_request_key(_elem, ssrc):
                self.logger.debug(
                    'srtpdec requested key for ssrc=0x%08x', ssrc & 0xffffffff,
                )
                return Gst.Caps.from_string(key_caps_str)

            srtpdec.connect('request-key', on_request_key)
            self.logger.info(
                'SRTP enabled (cipher=%s/%s auth=%s/%s, %d-byte master key+salt)',
                SRTP_RTP_CIPHER, SRTP_RTCP_CIPHER,
                SRTP_RTP_AUTH, SRTP_RTCP_AUTH, len(master),
            )

            udpsrc.link_pads('src', srtpdec, 'rtp_sink')
            srtpdec.link_pads('rtp_src', rtpbin, 'recv_rtp_sink_0')
        else:
            udpsrc.link_pads('src', rtpbin, 'recv_rtp_sink_0')

        valve = make_element('valve', 'valve')
        bin.add(valve)

        bin.add_pad(Gst.GhostPad.new('src', valve.get_static_pad('src')))

        # RTCP back-channel: rtpbin emits NACKs on send_rtcp_src_0 once
        # do-retransmission is set; we send them out the same socket the
        # audio arrived on, targeted at the OPOB peer. sync/async=False
        # because RTCP isn't on the audio clock and we don't want udpsink
        # to block waiting on segments before forwarding.
        if self.rtx_enabled and self._rx_shared_pysock is not None:
            assert self.peer_addr is not None
            gsock_out = Gio.Socket.new_from_fd(
                os.dup(self._rx_shared_pysock.fileno())
            )
            udpsink_rtcp = make_element('udpsink', 'udpsink-rtcp')
            udpsink_rtcp.set_property('socket', gsock_out)
            udpsink_rtcp.set_property('close-socket', False)
            udpsink_rtcp.set_property('host', self.peer_addr[0])
            udpsink_rtcp.set_property('port', self.peer_addr[1])
            udpsink_rtcp.set_property('sync', False)
            udpsink_rtcp.set_property('async', False)
            bin.add(udpsink_rtcp)
            ok = rtpbin.link_pads(
                'send_rtcp_src_0', udpsink_rtcp, 'sink',
            )
            if not ok:
                raise RuntimeError(
                    'failed to link rtpbin.send_rtcp_src_0 → RTCP udpsink'
                )

        # Attach pad-added LAST. rtpbin emits this for every pad addition
        # (incl. our own send_rtcp_src_0 request above). Connecting after
        # all the pad requests we make ourselves means rtpbin_pad_added
        # only fires for the dynamic recv_rtp_src_* pad rtpbin creates
        # when the first RTP packet arrives — by which time self.transport
        # is set.
        rtpbin.connect('pad-added', self.rtpbin_pad_added)

        return bin

    def _bind_shared_rx_socket(self) -> pysocket.socket:
        """Bind a Python socket on the listen port for shared use between
        ``udpsrc`` and the RTCP NACK ``udpsink``.

        wait_for_caps closes its own socket before this runs; the brief
        unbound window is acceptable for STL — TX retransmits OPOB caps
        every 2 s anyway.
        """
        sock = pysocket.socket(pysocket.AF_INET, pysocket.SOCK_DGRAM)
        sock.setsockopt(pysocket.SOL_SOCKET, pysocket.SO_REUSEADDR, 1)
        sock.bind(('', self.link_config.port))
        return sock

    def _on_new_jitterbuffer(self, _rtpbin, jitterbuffer, _session_id, _ssrc):
        """Hook each freshly-spawned rtpjitterbuffer to our pt-map handler.

        rtpbin's own request-pt-map only covers the session level; once
        the buffer is created it asks for its own caps via the same
        signal name on its own object, and that's where the missing
        clock-rate ends up biting us.
        """
        jitterbuffer.connect('request-pt-map', self._on_request_pt_map_jb)

    def _on_request_pt_map_jb(self, _jitterbuffer, pt):
        """Adapt the rtpbin-style 3-arg callback to the jitterbuffer's
        2-arg version. Same lookup, just a different arity."""
        return self._on_request_pt_map(None, 0, pt)

    def _on_request_pt_map(self, _rtpbin, session_id, pt):
        """Hand rtpbin/jitterbuffer a caps structure for ``pt``.

        Built from ``self.caps`` (the in-band caps signal) — for the audio
        PT we keep all the original fields; for the RTX PT we synthesise
        matching caps with the same clock-rate so the jitter buffer's
        RTX path has the timing info it needs.
        """
        del session_id
        try:
            base = Gst.Caps.from_string(self.caps.replace('\\', ''))
        except Exception as exc:
            self.logger.warning('request-pt-map: caps parse failed: %s', exc)
            return None
        if base is None or base.get_size() == 0:
            self.logger.warning('request-pt-map: empty caps from self.caps=%r', self.caps)
            return None
        struct = base.get_structure(0)
        ok, base_pt = struct.get_int('payload')
        if ok and pt == base_pt:
            self.logger.debug('request-pt-map: pt=%d → audio caps', pt)
            return base
        rtx_caps = base.copy()
        rtx_struct = rtx_caps.get_structure(0)
        rtx_struct.set_value('payload', pt)
        self.logger.debug('request-pt-map: pt=%d → rtx caps', pt)
        return rtx_caps

    def _on_request_aux_receiver(self, _rtpbin, session_id):
        """Build the rtprtxreceive bin that rtpbin inserts between the
        transport input (udpsrc/srtpdec) and its recv RTP pad. Called
        once per session when recv_rtp_sink_<sid> is requested.
        """
        aux = Gst.Bin.new(f'rtx-aux-receiver-{session_id}')
        rtx = make_element('rtprtxreceive', 'rtprtxreceive')
        # rtprtxreceive's payload-type-map is keyed by ORIGINAL PT and
        # values are RTX PTs — same convention as rtprtxsend (the element
        # internally figures out which side it's on). With map {96: 97},
        # incoming packets with PT 97 are treated as RTX-of-PT-96.
        orig_to_rtx = (
            self.rtx_pt_map if self.rtx_pt_map
            else {_RTX_ORIGINAL_PT: _RTX_PT}
        )
        pt_map_parts = ','.join(
            f'{orig}=(uint){rtx_pt}' for orig, rtx_pt in orig_to_rtx.items()
        )
        pt_map_struct = Gst.Structure.from_string(
            f'application/x-rtp-pt-map,{pt_map_parts}'
        )[0]
        rtx.set_property('payload-type-map', pt_map_struct)
        aux.add(rtx)
        aux.add_pad(Gst.GhostPad.new(
            f'sink_{session_id}', rtx.get_static_pad('sink'),
        ))
        aux.add_pad(Gst.GhostPad.new(
            f'src_{session_id}', rtx.get_static_pad('src'),
        ))
        self.logger.debug(
            'aux-receiver bin built for session %d (rtprtxreceive orig→rtx %s)',
            session_id, orig_to_rtx,
        )
        return aux

    # Our RTPbin won't give us an audio pad till it receives, so we need to
    # attach it here. rtpbin emits pad-added for any pad change, so filter
    # to the dynamic recv_rtp_src that only appears once packets flow.
    def rtpbin_pad_added(self, _obj, pad) -> None:
        if not pad.get_name().startswith('recv_rtp_src_'):
            return
        valve = self.transport.get_by_name('valve')
        rtpbin = self.transport.get_by_name('rtpbin')

        # Unlink first.
        rtpbin.unlink(valve)
        # Relink
        rtpbin.link(valve)

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
            if struct is not None:
                name = struct.get_name()
                if name == 'level':
                    peaks = struct.get_value('peak')
                    if not self.started:
                        self.started = True
                        self._log_pipeline_started(peaks)
                    else:
                        self._log_level(peaks)
                elif name == 'GstUDPSrcTimeout':
                    # Only UDP source configured to emit timeouts is the audio input
                    self.logger.critical('No data received for 3 seconds!')
                    if self.started:
                        self.logger.critical('Shutting down receiver for restart')
                        self.pipeline.set_state(Gst.State.NULL)
                        self.main_loop.quit()
        return True

    def _log_pipeline_started(self, peaks) -> None:
        # On RX the level is downstream of the decoder so its caps already
        # match what was negotiated end-to-end. Fall back to peak count if
        # the pad has no current caps yet.
        caps_str, n_channels = channels_from_pad(self.level_element.get_static_pad('sink'))
        if n_channels is None:
            n_channels = len(peaks)
        descriptor = channel_descriptor(n_channels)
        if caps_str is not None:
            self.logger.info(
                'Receiving %s audio transmission (%s)', descriptor, caps_str
            )
        else:
            self.logger.info('Receiving %s audio transmission', descriptor)

    def _log_level(self, peaks) -> None:
        self.logger.debug('Levels: %s', format_peaks(peaks))
