import gobject
import pygst
pygst.require("0.10")
import gst
import time
import re
from openob.logger import LoggerFactory


class RTPTransmitter(object):

    def __init__(self, node_name, link_config, audio_interface):
        """Sets up a new RTP transmitter"""
        self.logger_factory = LoggerFactory()
        self.logger = self.logger_factory.getLogger('node.%s.link.%s.%s' % (node_name, self.link_config.name, self.audio_interface.mode))
        self.logger.info("Creating RTP transmission pipeline")

        self.started = False
        self.caps = 'None'

        self.build_pipeline(link_config, audio_interface)

    def run(self):
        self.pipeline.set_state(gst.STATE_PLAYING)
        while self.caps == 'None':
            self.logger.debug(udpsink_rtpout.get_state())
            self.caps = str(
                udpsink_rtpout.get_pad('sink').get_property('caps'))
            # Fix for gstreamer bug in rtpopuspay fixed in GST-plugins-bad
            # 50140388d2b62d32dd9d0c071e3051ebc5b4083b, bug 686547
            if self.link_config.encoding == 'opus':
                self.caps = re.sub(r'(caps=.+ )', '', self.caps)

            if self.caps == 'None':
                self.logger.warn("Waiting for audio interface/caps")

            time.sleep(0.1)

    def loop(self):
        try:
            self.loop = gobject.MainLoop()
            self.loop.run()
        except Exception as e:
            self.logger.exception("Encountered a problem in the MainLoop, tearing down the pipeline: %s" % e)
            self.pipeline.set_state(gst.STATE_NULL)

    def build_pipeline(self, link_config, audio_interface):
        self.pipeline = gst.Pipeline("tx")
        bus = self.pipeline.get_bus()
        bus.connect("message", self.on_message)

        self.link_config = link_config
        self.audio_interface = audio_interface

        source = self.build_audio_interface()
        encoder = self.build_encoder()
        transport = self.build_transport()
        
        self.pipeline.add(source, encoder, transport)
        gst.element_link_many(source, encoder, transport)

        # Connect our bus up
        bus.add_signal_watch()
        bus.connect('message', self.on_message)

    def build_audio_interface(self):
        bin = gst.Bin('audio')

        # Audio input
        if self.audio_interface.type == 'auto':
            source = gst.element_factory_make('autoaudiosrc')
        elif self.audio_interface.type == 'alsa':
            source = gst.element_factory_make('alsasrc')
            source.set_property('device', self.audio_interface.alsa_device)
        elif self.audio_interface.type == 'jack':
            source = gst.element_factory_make("jackaudiosrc")
            if self.audio_interface.jack_auto:
                source.set_property('connect', 'auto')
            else:
                source.set_property('connect', 'none')
            source.set_property('buffer-time', 50000)
            source.set_property('name', self.audio_interface.jack_name)
            source.set_property('client-name', self.audio_interface.jack_name)
        # Audio resampling and conversion
        audioresample = gst.element_factory_make("audioresample")
        audioconvert = gst.element_factory_make("audioconvert")
        audioresample.set_property('quality', 9)  # SRC

        # Add a capsfilter to allow specification of input sample rate
        capsfilter = gst.element_factory_make("capsfilter")

        # Decide which format to apply to the capsfilter (Jack uses float)
        if self.audio_interface.type == 'jack':
            data_type = 'audio/x-raw-float'
        else:
            data_type = 'audio/x-raw-int'

        # if audio_rate has been specified, then add that to the capsfilter
        if self.audio_interface.samplerate != 0:
            capsfilter.set_property(
                "caps", gst.Caps('%s, channels=2, rate=%d' % (data_type, self.audio_interface.samplerate)))
        else:
            capsfilter.set_property(
                "caps", gst.Caps('%s, channels=2' % data_type))

        # Our level monitor
        level = gst.element_factory_make("level")
        level.set_property('message', True)
        level.set_property('interval', 1000000000)

        bin.add(
            source, capsfilter, level, audioresample, audioconvert
        )

        gst.element_link_many(
            source, capsfilter, level, audioresample, audioconvert
        )

        return bin

    def build_encoder(self):
        bin = gst.Bin('encoder')

        # Encoding and payloading
        if self.link_config.encoding == 'opus':
            encoder = gst.element_factory_make("opusenc", "encoder")
            encoder.set_property('bitrate', int(self.link_config.bitrate) * 1000)
            encoder.set_property('tolerance', 80000000)
            encoder.set_property('frame-size', self.link_config.opus_framesize)
            encoder.set_property('complexity', int(self.link_config.opus_complexity))
            encoder.set_property('inband-fec', self.link_config.opus_fec)
            encoder.set_property('packet-loss-percentage', int(self.link_config.opus_loss_expectation))
            encoder.set_property('dtx', self.link_config.opus_dtx)
            print(encoder.get_properties('bitrate', 'dtx', 'inband-fec'))
            payloader = gst.element_factory_make("rtpopuspay", "payloader")
        elif self.link_config.encoding == 'pcm':
            # we have no encoder for PCM operation
            payloader = gst.element_factory_make("rtpL16pay", "payloader")
        else:
            self.logger.critical("Unknown encoding type %s" % self.link_config.encoding)

        if self.link_config.encoding != 'pcm':
            # Only add the encoder if we're not in PCM mode
            bin.add(encoder)
            gst.element_link_many(encoder, payloader)
        
        bin.add(payloader)

        return bin

    def build_transport(self):
        bin = gst.Bin('transport')

        # TODO: Add a tee here, and sort out creating multiple UDP sinks for multipath
        # Now the RTP bits
        # We'll send audio out on this
        udpsink_rtpout = gst.element_factory_make("udpsink", "udpsink_rtp")
        udpsink_rtpout.set_property('host', self.link_config.receiver_host)
        udpsink_rtpout.set_property('port', self.link_config.port)
        self.logger.info('Set receiver to %s:%i' % (self.link_config.receiver_host, self.link_config.port))

        if self.link_config.multicast:
            udpsink_rtpout.set_property('auto_multicast', True)
            self.logger.info('Multicast mode enabled')

        # Our RTP manager
        rtpbin = gst.element_factory_make("gstrtpbin", "gstrtpbin")
        rtpbin.set_property('latency', 0)

        # And now the RTP bits
        payloader.link_pads('src', rtpbin, 'send_rtp_sink_0')
        rtpbin.link_pads('send_rtp_src_0', udpsink_rtpout, 'sink')
        # self.udpsrc_rtcpin.link_pads('src', rtpbin, 'recv_rtcp_sink_0')
        # # RTCP SRs
        # rtpbin.link_pads('send_rtcp_src_0', self.udpsink_rtcpout, 'sink')

        bin.add(udpsink_rtpout, rtpbin)

    def on_message(self, bus, message):
        if message.type == gst.MESSAGE_ELEMENT:
            if message.structure.get_name() == 'level':
                if self.started is False:
                    self.started = True
                    #gst.DEBUG_BIN_TO_DOT_FILE(self.pipeline, gst.DEBUG_GRAPH_SHOW_ALL, 'tx-graph')
                    #self.logger.debug(source.get_property('actual-buffer-time'))
                    if len(message.structure['peak']) == 1:
                        self.logger.info("Started mono audio transmission")
                    else:
                        self.logger.info("Started stereo audio transmission")
        return True

    def get_caps(self):
        return self.caps
