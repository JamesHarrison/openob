from __future__ import annotations

import signal
import time
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


class RTPTransmitter:

    def __init__(
        self,
        node_name: str,
        link_config: LinkConfig,
        audio_interface: AudioInterface,
    ) -> None:
        """Sets up a new RTP transmitter"""

        self.link_config = link_config
        self.audio_interface = audio_interface

        self.logger_factory = LoggerFactory()
        self.logger = self.logger_factory.getLogger('node.%s.link.%s.%s' % (node_name, self.link_config.name, self.audio_interface.mode))
        self.logger.info('Creating transmission pipeline')

        self.build_pipeline()

    def run(self) -> None:
        self.pipeline.set_state(Gst.State.PLAYING)
        # Gst.debug_bin_to_dot_file(self.pipeline, Gst.DebugGraphDetails.ALL, 'tx-graph')

        while self.caps is None:
            caps = self.transport.get_by_name('udpsink').get_static_pad('sink').get_property('caps')

            if caps is None:
                self.logger.warning('Waiting for audio interface/caps')
                time.sleep(0.1)
            else:
                self.caps = caps.to_string()

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

        if self.audio_interface.samplerate != 0:
            caps.set_value('rate', self.audio_interface.samplerate)
        if self.audio_interface.channels != 0:
            caps.set_value('channels', self.audio_interface.channels)

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
        bin.add(udpsink)

        bin.add_pad(Gst.GhostPad.new('sink', rtpbin.request_pad_simple('send_rtp_sink_0')))

        rtpbin.link_pads('send_rtp_src_0', udpsink, 'sink')

        return bin

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
