#!/usr/bin/env python

# OpenOB RX
# James Harrison <james@talkunafraid.co.uk>
# Many thanks to http://blog.abourget.net/2009/6/14/gstreamer-rtp-and-live-streaming/
#  and the GStreamer developers.
import gobject
gobject.threads_init()
import pygst
pygst.require("0.10")
import gst
import redis
import yaml
import datetime
import calendar

static_conf = yaml.load(open("config.yml", 'r'))
config = redis.Redis(static_conf['configuration_host'])

class RTPReceiver:
  def __init__(self):
    # Configure ourselves from the TX keys
    caps = config.get((static_conf['caps_key']+static_conf['rx']['configuration_name']))
    base_port = int(config.get((static_conf['port_key']+static_conf['rx']['configuration_name'])))
    buffer_size = int(config.get((static_conf['buffer_size_key']+static_conf['rx']['configuration_name'])))
    depayloader_name = config.get((static_conf['depayloader_key']+static_conf['rx']['configuration_name']))
    decoder_name = config.get((static_conf['decoder_key']+static_conf['rx']['configuration_name']))
    self.rx = gst.Pipeline("rx")
    bus = self.rx.get_bus()
    # Audio pipeline elements
    audioconvert = gst.element_factory_make("audioconvert")
    audioresample = gst.element_factory_make("audioresample")
    audioresample.set_property('quality', 9)
    audiorate = gst.element_factory_make("audiorate")
    if static_conf['rx']['audio_connection'] == 'alsa':
      self.sink = gst.element_factory_make("alsasink")
      self.sink.set_property('device', static_conf['rx']['alsa_device'])
    elif static_conf['rx']['audio_connection'] == 'jack':
      self.sink = gst.element_factory_make("jackaudiosink")
      self.sink.set_property('connect', 'auto')
    elif static_conf['rx']['audio_connection'] == 'pulseaudio':
      self.source = gst.element_factory_make("pulsesink")
    self.decoder = gst.element_factory_make(decoder_name,"decoder")
    self.depayloader = gst.element_factory_make(depayloader_name,"depayloader")
    level = gst.element_factory_make("level")
    level.set_property('message', True)
    level.set_property('interval', 1000000000)

    # Our RTP receive manager
    self.rtpbin = gst.element_factory_make('gstrtpbin')
    self.rtpbin.set_property('latency', buffer_size)
    self.rtpbin.set_property('autoremove', True)
    self.rtpbin.set_property('do-lost', True)
    # Where audio comes in
    udpsrc_rtpin = gst.element_factory_make('udpsrc')
    udpsrc_rtpin.set_property('port', base_port)
    # Set up our caps from redis
    caps = caps.replace('\\', '')
    udpsrc_caps = gst.caps_from_string(caps)
    udpsrc_rtpin.set_property('caps', udpsrc_caps)
    udpsrc_rtpin.set_property('timeout', 5000000)
    # Where our RTCP control messages come in
    udpsrc_rtcpin = gst.element_factory_make('udpsrc')
    udpsrc_rtcpin.set_property('port', base_port+1)
    # And where we'll get RTCP Sender Reports
    udpsink_rtcpout = gst.element_factory_make('udpsink')
    udpsink_rtcpout.set_property('host', "0.0.0.0")
    udpsink_rtcpout.set_property('port', base_port+2)


    # Add the elements to the pipeline and link 'em up
    if depayloader_name == 'rtpL16depay':
      self.rx.add(audiorate, audioresample, audioconvert, self.sink, self.depayloader, self.rtpbin, udpsrc_rtpin, udpsrc_rtcpin, udpsink_rtcpout, level)
      gst.element_link_many(self.depayloader, audioconvert, level, audioresample, audiorate, self.sink)
    else:
      self.rx.add(audiorate, audioresample, audioconvert, self.sink, self.decoder, self.depayloader, self.rtpbin, udpsrc_rtpin, udpsrc_rtcpin, udpsink_rtcpout, level)
      gst.element_link_many(self.depayloader, self.decoder, audioconvert, level, audioresample, audiorate, self.sink)
    for p in udpsrc_rtpin.pads():
      print p
      print p.get_caps()
    # Now the RTP pads
    udpsrc_rtpin.link_pads('src', self.rtpbin, 'recv_rtp_sink_0')
    udpsrc_rtcpin.link_pads('src', self.rtpbin, 'recv_rtcp_sink_0')
    # RTCP SRs
    self.rtpbin.link_pads('send_rtcp_src_0', udpsink_rtcpout, 'sink')

    # Attach callbacks for dynamic pads (RTP output) and busses
    self.rtpbin.connect('pad-added', self.rtpbin_pad_added)
    bus.add_signal_watch()
    bus.connect('message', self.on_bus_message)

  # Our RTPbin won't give us an audio pad till it receives, so we need to attach it here
  def rtpbin_pad_added(self, obj, pad):
    # Unlink first.
    self.rtpbin.unlink(self.depayloader)
    # Relink
    self.rtpbin.link(self.depayloader)

  def on_bus_message(self, bus, message):
    if message.type == gst.MESSAGE_ELEMENT:
      if message.structure.get_name() == 'level':
        # This is an audio level update, which the 'level' element emits once a second.
        
        # We're storing lists here in redis to let us do historical graphing in the webUI.

        # First, push a timestamp value
        config.lpush((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":utc_timestamps"), calendar.timegm(datetime.datetime.utcnow().timetuple()))

        # Push the latest values to the Redis server
        config.lpush((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":rms:left"), message.structure['rms'][0])
        config.lpush((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":rms:right"), message.structure['rms'][1])
        config.lpush((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":peak:left"), message.structure['peak'][0])
        config.lpush((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":peak:right"), message.structure['peak'][1])
        
        # Trim the lists down to 3600 seconds (1 hour) of data to stop them getting too huge while remaining useful for level graphs
        config.ltrim((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":utc_timestamps"), 0, 3600)
        config.ltrim((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":rms:left"), 0, 3600)
        config.ltrim((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":rms:right"), 0, 3600)
        config.ltrim((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":peak:left"), 0, 3600)
        config.ltrim((static_conf['rx_level_info_key']+static_conf['rx']['configuration_name']+":peak:right"), 0, 3600)
        print message.structure['peak']
      elif message.structure.get_name() == 'GstUDPSrcTimeout':
        # Only UDP source configured to emit timeouts is the audio input
        print "No data received in 5 seconds!"
      else:
        print message
        print message.type
        print message.structure
        print message.structure.get_name()
    else:
      print message
      print message.type
      print message.structure
      print message.structure.get_name()
    return gst.BUS_PASS

  def start(self):
    print "OpenOB RX Mode Starting"
    self.rx.set_state(gst.STATE_PLAYING)
    #udpsink_rtcpout.set_locked_state(gst.STATE_PLAYING)
    print "Started..."

  def loop(self):
    print "Running..."
    gobject.MainLoop().run()

if __name__ == '__main__':
  rtprx = RTPReceiver()
  rtprx.start()
  rtprx.loop()

