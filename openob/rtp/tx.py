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
        self.started = False
        self.caps = 'None'
        self.pipeline = gst.Pipeline("tx")
        self.bus = self.pipeline.get_bus()
        self.bus.connect("message", self.on_message)
        self.link_config = link_config
        self.audio_interface = audio_interface
        self.logger_factory = LoggerFactory()
        self.logger = self.logger_factory.getLogger('node.%s.link.%s.%s' % (node_name, self.link_config.name, self.audio_interface.mode))
        self.logger.info("Creating RTP transmission pipeline")
        # Audio input
        if self.audio_interface.type == 'auto':
            self.source = gst.element_factory_make('autoaudiosrc')
        elif self.audio_interface.type == 'alsa':
            self.source = gst.element_factory_make('alsasrc')
            self.source.set_property('device', self.audio_interface.alsa_device)
        elif self.audio_interface.type == 'jack':
            self.source = gst.element_factory_make("jackaudiosrc")
            if self.audio_interface.jack_auto:
                self.source.set_property('connect', 'auto')
            else:
                self.source.set_property('connect', 'none')
            self.source.set_property('buffer-time', 50000)
            self.source.set_property('name', self.audio_interface.jack_name)
            self.source.set_property('client-name', self.audio_interface.jack_name)
        # Audio resampling and conversion
        self.audioresample = gst.element_factory_make("audioresample")
        self.audioconvert = gst.element_factory_make("audioconvert")
        self.audioresample.set_property('quality', 9)  # SRC
        
        # Audio filtering
        
        if self.link_config.highpass:
            self.highpass_filter = gst.element_factory_make("audiocheblimit")
            self.highpass_filter.set_property('mode', 'high-pass')
            self.highpass_filter.set_property('cutoff', 175)
        
        if self.link_config.limiter:
            self.limiter = gst.element_factory_make("audiodynamic")
            self.limiter.set_property('mode', 'compressor')
            self.limiter.set_property('characteristics', 'hard-knee')
            self.limiter.set_property('ratio', 4.0)
            self.limiter.set_property('threshold', 0.8)
            

        # Encoding and payloading
        if self.link_config.encoding == 'opus':
            self.encoder = gst.element_factory_make("opusenc", "encoder")
            self.encoder.set_property('bitrate', int(self.link_config.bitrate) * 1000)
            self.encoder.set_property('tolerance', 80000000)
            self.encoder.set_property('frame-size', self.link_config.opus_framesize)
            self.encoder.set_property('complexity', int(self.link_config.opus_complexity))
            self.encoder.set_property('inband-fec', self.link_config.opus_fec)
            self.encoder.set_property('packet-loss-percentage', int(self.link_config.opus_loss_expectation))
            self.encoder.set_property('dtx', self.link_config.opus_dtx)
            print(self.encoder.get_properties('bitrate', 'dtx', 'inband-fec'))
            self.payloader = gst.element_factory_make("rtpopuspay", "payloader")
        elif self.link_config.encoding == 'pcm':
            # we have no encoder for PCM operation
            self.payloader = gst.element_factory_make("rtpL16pay", "payloader")
        else:
            self.logger.critical("Unknown encoding type %s" % self.link_config.encoding)
        # TODO: Add a tee here, and sort out creating multiple UDP sinks for multipath
        # Now the RTP bits
        # We'll send audio out on this
        self.udpsink_rtpout = gst.element_factory_make("udpsink", "udpsink_rtp")
        self.udpsink_rtpout.set_property('host', self.link_config.receiver_host)
        self.udpsink_rtpout.set_property('port', self.link_config.port)
        self.logger.info('Set receiver to %s:%i' % (self.link_config.receiver_host, self.link_config.port))

        if self.link_config.multicast:
            self.udpsink_rtpout.set_property('auto_multicast', True)
            self.logger.info('Multicast mode enabled')

        # Our RTP manager
        self.rtpbin = gst.element_factory_make("gstrtpbin", "gstrtpbin")
        self.rtpbin.set_property('latency', 0)
        # Our level monitor
        self.level = gst.element_factory_make("level")
        self.level.set_property('message', True)
        self.level.set_property('interval', 1000000000)

        # Add a capsfilter to allow specification of input sample rate
        self.capsfilter = gst.element_factory_make("capsfilter")

        # Add to the pipeline
        self.pipeline.add(
            self.source, self.capsfilter, self.audioresample, self.audioconvert,
            self.payloader, self.udpsink_rtpout, self.rtpbin,
            self.level)

        if self.link_config.encoding != 'pcm':
            # Only add the encoder if we're not in PCM mode
            self.pipeline.add(self.encoder)
        
        if self.link_config.highpass:
            # Only add the HPF if needed
            self.pipeline.add(self.highpass_filter)

        # Decide which format to apply to the capsfilter (Jack uses float)
        if self.audio_interface.type == 'jack':
            data_type = 'audio/x-raw-float'
        else:
            data_type = 'audio/x-raw-int'

        # if audio_rate has been specified, then add that to the capsfilter
        if self.audio_interface.samplerate != 0:
            self.capsfilter.set_property(
                "caps", gst.Caps('%s, channels=%d, rate=%d' % (data_type, self.audio_interface.channels, self.audio_interface.samplerate)))
        else:
            self.capsfilter.set_property(
                "caps", gst.Caps('%s, channels=%d' % (data_type, self.audio_interface.channels)))
        
        # This identity block just acts as a neat way to join the audio
        # pipeline to the coder
        self.audio_identity_block = gst.element_factory_make('identity')
        self.pipeline.add(self.audio_identity_block)
        
        # Then continue linking the pipeline together
        gst.element_link_many(
            self.source, self.capsfilter, self.level, self.audioresample, self.audioconvert)
            
        # work on our filter chain
        
        last_linked = self.audioconvert
        
        # Insert the highpass filter if configured
        if self.link_config.highpass:
            gst.element_link_many(last_linked, self.highpass_filter)
            last_linked = self.highpass_filter
        
        if self.link_config.limiter:
            gst.element_link_many(last_linked, self.limiter)
            last_linked = self.limiter
        
        gst.element_link_many(last_linked, self.audio_identity_block)

        # Now we get to link this up to our encoder/payloader
        if self.link_config.encoding != 'pcm':
            gst.element_link_many(
                self.audio_identity_block, self.encoder, self.payloader)
        else:
            gst.element_link_many(self.audio_identity_block, self.payloader)

        # And now the RTP bits
        self.payloader.link_pads('src', self.rtpbin, 'send_rtp_sink_0')
        self.rtpbin.link_pads('send_rtp_src_0', self.udpsink_rtpout, 'sink')
        # self.udpsrc_rtcpin.link_pads('src', self.rtpbin, 'recv_rtcp_sink_0')
        # # RTCP SRs
        # self.rtpbin.link_pads('send_rtcp_src_0', self.udpsink_rtcpout, 'sink')
        # Connect our bus up
        self.bus.add_signal_watch()
        self.bus.connect('message', self.on_message)

    def run(self):
        self.pipeline.set_state(gst.STATE_PLAYING)
        while self.caps == 'None':
            self.logger.debug(self.udpsink_rtpout.get_state())
            self.caps = str(
                self.udpsink_rtpout.get_pad('sink').get_property('caps'))
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

    def on_message(self, bus, message):
        if message.type == gst.MESSAGE_ELEMENT:
            if message.structure.get_name() == 'level':
                if self.started is False:
                    self.started = True
                    #gst.DEBUG_BIN_TO_DOT_FILE(self.pipeline, gst.DEBUG_GRAPH_SHOW_ALL, 'tx-graph')
                    #self.logger.debug(self.source.get_property('actual-buffer-time'))
                    if len(message.structure['peak']) == 1:
                        self.logger.info("Started mono audio transmission")
                    else:
                        self.logger.info("Started stereo audio transmission")
        return True

    def get_caps(self):
        return self.caps
