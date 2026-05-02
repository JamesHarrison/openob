from __future__ import annotations

import signal
from typing import TYPE_CHECKING

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib  # noqa: E402
Gst.init(None)

from openob.logger import LoggerFactory  # noqa: E402
from openob.rtp._gst_helpers import (  # noqa: E402
    channel_descriptor,
    channels_from_pad,
    format_peaks,
    make_element,
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
    ) -> None:
        """Sets up a new RTP receiver"""

        self.link_config = link_config
        self.audio_interface = audio_interface
        self.shutdown_requested = False

        self.logger_factory = LoggerFactory()
        self.logger = self.logger_factory.getLogger('node.%s.link.%s.%s' % (node_name, self.link_config.name, self.audio_interface.mode))
        self.logger.info('Creating reception pipeline')

        self.build_pipeline()

    def run(self) -> None:
        self.pipeline.set_state(Gst.State.PLAYING)
        self.logger.info('Listening for stream on %s:%i' % (self.link_config.receiver_host, self.link_config.port))

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
        self.logger.debug('Building decoder bin')
        bin = Gst.Bin.new('decoder')

        decoder = None

        # Decoding and depayloading
        if self.link_config.encoding == 'opus':
            decoder = make_element('opusdec', 'decoder')
            decoder.set_property('use-inband-fec', True)  # FEC
            decoder.set_property('plc', True)  # Packet loss concealment
            depayloader = make_element('rtpopusdepay', 'depayloader')
        elif self.link_config.encoding == 'pcm':
            depayloader = make_element('rtpL16depay', 'depayloader')
        else:
            self.logger.critical('Unknown encoding type %s' % self.link_config.encoding)
            raise ValueError('Unknown encoding type %s' % self.link_config.encoding)

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
        self.logger.debug('Building RTP transport bin')
        bin = Gst.Bin.new('transport')

        caps = self.link_config.get('caps').replace('\\', '')
        udpsrc_caps = Gst.Caps.from_string(caps)

        # Where audio comes in
        udpsrc = make_element('udpsrc', 'udpsrc')
        udpsrc.set_property('port', self.link_config.port)
        udpsrc.set_property('caps', udpsrc_caps)
        udpsrc.set_property('timeout', 3000000000)
        if self.link_config.multicast:
            udpsrc.set_property('auto-multicast', True)
            udpsrc.set_property('multicast-group', self.link_config.receiver_host)
            self.logger.info('Multicast mode enabled')
        bin.add(udpsrc)

        rtpbin = make_element('rtpbin', 'rtpbin')
        rtpbin.set_property('latency', self.link_config.jitter_buffer)
        rtpbin.set_property('autoremove', True)
        rtpbin.set_property('do-lost', True)
        bin.add(rtpbin)

        udpsrc.link_pads('src', rtpbin, 'recv_rtp_sink_0')

        valve = make_element('valve', 'valve')
        bin.add(valve)

        bin.add_pad(Gst.GhostPad.new('src', valve.get_static_pad('src')))
        # Attach callbacks for dynamic pads (RTP output) and busses
        rtpbin.connect('pad-added', self.rtpbin_pad_added)

        return bin

    # Our RTPbin won't give us an audio pad till it receives, so we need to
    # attach it here
    def rtpbin_pad_added(self, obj, pad) -> None:
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
