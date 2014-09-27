import gobject
import pygst
pygst.require("0.10")
import gst
import re
from openob.logger import LoggerFactory


class RTPReceiver(object):

    def __init__(self, node_name, link_config, audio_interface):
        """Sets up a new RTP receiver"""
        self.started = False
        self.pipeline = gst.Pipeline("rx")
        self.bus = self.pipeline.get_bus()
        self.bus.connect("message", self.on_message)
        self.link_config = link_config
        self.audio_interface = audio_interface
        self.logger_factory = LoggerFactory()
        self.logger = self.logger_factory.getLogger('node.%s.link.%s.%s' % (node_name, self.link_config.name, self.audio_interface.mode))
        caps = self.link_config.get("caps")
        # Audio output
        if self.audio_interface.type == 'auto':
            self.sink = gst.element_factory_make("autoaudiosink")
        elif self.audio_interface.type == 'alsa':
            self.sink = gst.element_factory_make("alsasink")
            self.sink.set_property('device', self.audio_interface.alsa_device)
        elif self.audio_interface.type == 'jack':
            self.sink = gst.element_factory_make("jackaudiosink")
            if self.audio_interface.jack_auto:
                self.sink.set_property('connect', 'auto')
            else:
                self.sink.set_property('connect', 'none')
            self.sink.set_property('name', self.audio_interface.jack_name)
            self.sink.set_property('client-name', self.audio_interface.jack_name)

        # Audio conversion and resampling
        self.audioconvert = gst.element_factory_make("audioconvert")
        self.audioresample = gst.element_factory_make("audioresample")
        self.audioresample.set_property('quality', 6)
        self.audiorate = gst.element_factory_make("audiorate")

        # Decoding and depayloading
        if self.link_config.encoding == 'opus':
            self.decoder = gst.element_factory_make("opusdec", "decoder")
            self.decoder.set_property('use-inband-fec', True)  # FEC
            self.decoder.set_property('plc', True)  # Packet loss concealment
            self.depayloader = gst.element_factory_make(
                "rtpopusdepay", "depayloader")
        elif self.link_config.encoding == 'pcm':
            self.depayloader = gst.element_factory_make(
                "rtpL16depay", "depayloader")

        # RTP stuff
        self.rtpbin = gst.element_factory_make('gstrtpbin')
        self.rtpbin.set_property('latency', self.link_config.jitter_buffer)
        self.rtpbin.set_property('autoremove', True)
        self.rtpbin.set_property('do-lost', True)
        #self.rtpbin.set_property('buffer-mode', 1)
        # Where audio comes in
        self.udpsrc_rtpin = gst.element_factory_make('udpsrc')
        self.udpsrc_rtpin.set_property('port', self.link_config.port)
        if self.link_config.multicast:
            self.udpsrc_rtpin.set_property('auto_multicast', True)
            self.udpsrc_rtpin.set_property('multicast_group', self.link_config.receiver_host)
        caps = caps.replace('\\', '')
        # Fix for gstreamer bug in rtpopuspay fixed in GST-plugins-bad
        # 50140388d2b62d32dd9d0c071e3051ebc5b4083b, bug 686547
        if self.link_config.encoding == 'opus':
            caps = re.sub(r'(caps=.+ )', '', caps)
        udpsrc_caps = gst.caps_from_string(caps)
        self.udpsrc_rtpin.set_property('caps', udpsrc_caps)
        self.udpsrc_rtpin.set_property('timeout', 3000000)

        # Our level monitor, also used for continuous audio
        self.level = gst.element_factory_make("level")
        self.level.set_property('message', True)
        self.level.set_property('interval', 1000000000)

        # And now we've got it all set up we need to add the elements
        self.pipeline.add(
            self.audiorate, self.audioresample, self.audioconvert, self.sink,
            self.level, self.depayloader, self.rtpbin, self.udpsrc_rtpin)
        if self.link_config.encoding != 'pcm':
            self.pipeline.add(self.decoder)
            gst.element_link_many(
                self.depayloader, self.decoder, self.audioresample)
        else:
            gst.element_link_many(self.depayloader, self.audioresample)
        gst.element_link_many(
            self.audioresample, self.audiorate, self.audioconvert, self.level,
            self.sink)
        self.logger.debug(self.sink)
        # Now the RTP pads
        self.udpsrc_rtpin.link_pads('src', self.rtpbin, 'recv_rtp_sink_0')

        # Attach callbacks for dynamic pads (RTP output) and busses
        self.rtpbin.connect('pad-added', self.rtpbin_pad_added)
        self.bus.add_signal_watch()

    # Our RTPbin won't give us an audio pad till it receives, so we need to
    # attach it here
    def rtpbin_pad_added(self, obj, pad):
        # Unlink first.
        self.rtpbin.unlink(self.depayloader)
        # Relink
        self.rtpbin.link(self.depayloader)

    def on_message(self, bus, message):
        if message.type == gst.MESSAGE_ELEMENT:
            if message.structure.get_name() == 'level':
                if self.started is False:
                    self.started = True
                    #gst.DEBUG_BIN_TO_DOT_FILE(self.pipeline, gst.DEBUG_GRAPH_SHOW_ALL, 'rx-graph')
                    if len(message.structure['peak']) == 1:
                        self.logger.info("Receiving mono audio transmission")
                    else:
                        self.logger.info("Receiving stereo audio transmission")

            if message.structure.get_name() == 'GstUDPSrcTimeout':
                # Only UDP source configured to emit timeouts is the audio
                # input
                self.logger.critical("No data received for 3 seconds!")
                if self.started:
                    self.logger.critical("Shutting down receiver for restart")
                    self.pipeline.set_state(gst.STATE_NULL)
                    self.loop.quit()
        return True

    def run(self):
        self.pipeline.set_state(gst.STATE_PLAYING)

    def loop(self):
        self.loop = gobject.MainLoop()
        self.loop.run()
