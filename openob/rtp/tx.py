import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
Gst.init(None)

import time
import re
import sys
from openob.logger import LoggerFactory


class RTPTransmitter(object):

    def __init__(self, node_name, link_config, audio_interface):
        """Sets up a new RTP transmitter"""
        
        self.link_config = link_config
        self.audio_interface = audio_interface

        self.logger_factory = LoggerFactory()
        self.logger = self.logger_factory.getLogger('node.%s.link.%s.%s' % (node_name, self.link_config.name, self.audio_interface.mode))
        self.logger.info('Creating transmission pipeline')

        self.build_pipeline()

    def run(self):
        self.pipeline.set_state(Gst.State.PLAYING)
        while self.caps == 'None':
            self.caps = str(
                self.transport.get_static_pad('sink').get_property('caps'))

            if self.caps == 'None':
                self.logger.warn('Waiting for audio interface/caps')

            time.sleep(0.1)

    def loop(self):
        try:
            self.loop = gobject.MainLoop()
            self.loop.run()
        except Exception as e:
            self.logger.exception('Encountered a problem in the MainLoop, tearing down the pipeline: %s' % e)
            self.pipeline.set_state(Gst.State.NULL)

    def build_pipeline(self):
        self.pipeline = Gst.Pipeline.new('tx')

        self.started = False
        self.caps = 'None'

        bus = self.pipeline.get_bus()
        bus.connect('message', self.on_message)

        self.source = self.build_audio_interface()
        self.encoder = self.build_encoder()
        self.transport = self.build_transport()
        
        self.pipeline.add(self.source, self.encoder, self.transport)
        self.source.link(self.encoder)
        self.encoder.link(self.transport)

        # Connect our bus up
        bus.add_signal_watch()
        bus.connect('message', self.on_message)

    def build_audio_interface(self):
        self.logger.debug('Building audio input bin')
        bin = Gst.Bin('audio')

        # Audio input
        if self.audio_interface.type == 'auto':
            source = Gst.ElementFactory.make('autoaudiosrc')
        elif self.audio_interface.type == 'alsa':
            source = Gst.ElementFactory.make('alsasrc')
            source.set_property('device', self.audio_interface.alsa_device)
        elif self.audio_interface.type == 'jack':
            source = Gst.ElementFactory.make('jackaudiosrc')
            if self.audio_interface.jack_auto:
                source.set_property('connect', 'auto')
            else:
                source.set_property('connect', 'none')
            source.set_property('buffer-time', 50000)
            source.set_property('name', self.audio_interface.jack_name)
            source.set_property('client-name', self.audio_interface.jack_name)
        elif self.audio_interface.type == 'test':
            source = Gst.ElementFactory.make('audiotestsrc')

        bin.add(source)

        # Audio resampling and conversion
        resample = Gst.ElementFactory.make('audioresample')
        resample.set_property('quality', 9)  # SRC
        bin.add(resample)

        convert = Gst.ElementFactory.make('audioconvert')
        bin.add(convert)

        # Add a capsfilter to allow specification of input sample rate
        capsfilter = Gst.ElementFactory.make('capsfilter')

        # Decide which format to apply to the capsfilter (Jack uses float)
        if self.audio_interface.type == 'jack':
            data_type = 'audio/x-raw-float'
        else:
            data_type = 'audio/x-raw-int'

        # if audio_rate has been specified, then add that to the capsfilter
        if self.audio_interface.samplerate != 0:
            capsfilter.set_property(
                'caps', Gst.Caps('%s, channels=2, rate=%d' % (data_type, self.audio_interface.samplerate)))
        else:
            capsfilter.set_property(
                'caps', Gst.Caps('%s, channels=2' % data_type))
        bin.add(capsfilter)

        # Our level monitor
        level = Gst.ElementFactory.make('level')
        level.set_property('message', True)
        level.set_property('interval', 1000000000)
        bin.add(level)

        source.link(resample)
        resample.link(convert)
        convert.link(capsfilter)
        capsfilter.link(level)

        bin.add_pad(Gst.GhostPad.new('src', level.get_static_pad('src')))

        return bin

    def build_encoder(self):
        self.logger.debug('Building encoder bin')
        bin = Gst.Bin('encoder')

        # Encoding and payloading
        if self.link_config.encoding == 'opus':
            encoder = Gst.ElementFactory.make('opusenc', 'encoder')
            encoder.set_property('bitrate', int(self.link_config.bitrate) * 1000)
            encoder.set_property('tolerance', 80000000)
            encoder.set_property('frame-size', self.link_config.opus_framesize)
            encoder.set_property('complexity', int(self.link_config.opus_complexity))
            encoder.set_property('inband-fec', self.link_config.opus_fec)
            encoder.set_property('packet-loss-percentage', int(self.link_config.opus_loss_expectation))
            encoder.set_property('dtx', self.link_config.opus_dtx)

            payloader = Gst.ElementFactory.make('rtpopuspay', 'payloader')
        elif self.link_config.encoding == 'pcm':
            # we have no encoder for PCM operation
            payloader = Gst.ElementFactory.make('rtpL16pay', 'payloader')
        else:
            self.logger.critical('Unknown encoding type %s' % self.link_config.encoding)

        bin.add(payloader)

        if 'encoder' in locals():
            bin.add(encoder)
            encoder.link(payloader)
            bin.add_pad(Gst.GhostPad.new('sink', encoder.get_static_pad('sink')))
        else:
            bin.add_pad(Gst.GhostPad.new('sink', payloader.get_static_pad('sink')))

        bin.add_pad(Gst.GhostPad.new('src', payloader.get_static_pad('src')))

        return bin

    def build_transport(self):
        self.logger.debug('Building RTP transport bin')
        bin = Gst.Bin('transport')

        # Our RTP manager
        rtpbin = Gst.ElementFactory.make('rtpbin')
        rtpbin.set_property('latency', 0)
        bin.add(rtpbin)

        # TODO: Add a tee here, and sort out creating multiple UDP sinks for multipath
        udpsink = Gst.ElementFactory.make('udpsink')
        udpsink.set_property('host', self.link_config.receiver_host)
        udpsink.set_property('port', self.link_config.port)
        self.logger.info('Set receiver to %s:%i' % (self.link_config.receiver_host, self.link_config.port))

        if self.link_config.multicast:
            udpsink.set_property('auto_multicast', True)
            self.logger.info('Multicast mode enabled')
        bin.add(udpsink)
        rtpbin.link_pads('send_rtp_src_0', udpsink, 'sink')

        bin.add_pad(Gst.GhostPad.new('sink', rtpbin.get_request_pad('send_rtp_sink_0')))
        bin.add_pad(Gst.GhostPad.new('capspad', udpsink.get_static_pad('sink')))

        return bin

    def on_message(self, bus, message):
        if message.type == Gst.Message.ELEMENT:
            if message.structure.get_name() == 'level':
                if self.started is False:
                    self.started = True
                    #Gst.DEBUG_BIN_TO_DOT_FILE(self.pipeline, Gst.DEBUG_GRAPH_SHOW_ALL, 'tx-graph')
                    #self.logger.debug(source.get_property('actual-buffer-time'))
                    if len(message.structure['peak']) == 1:
                        self.logger.info('Started mono audio transmission')
                    else:
                        self.logger.info('Started stereo audio transmission')
        return True

    def get_caps(self):
        return self.caps
