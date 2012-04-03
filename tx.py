#!/usr/bin/env python

# OpenOB TX
# James Harrison <james@talkunafraid.co.uk>
# Many thanks to http://blog.abourget.net/2009/6/14/gstreamer-rtp-and-live-streaming/
#  and the GStreamer developers.

import gobject
import pygst
pygst.require("0.10")
import gst
import redis
import yaml
import datetime
import calendar
import sys

gobject.threads_init()

static_conf = yaml.load(open("config.yml", 'r'))
config = redis.Redis(static_conf['configuration_host'])


class RTPTransmitter():
  def __init__(self):
    self.started = False
    self.tx = gst.Pipeline("tx")
    bus = self.tx.get_bus()
    # Audio pipeline elements
    if static_conf['tx']['audio_connection'] == 'alsa':
      self.source = gst.element_factory_make('alsasrc')
      self.source.set_property('device', static_conf['tx']['alsa_device'])
    elif static_conf['tx']['audio_connection'] == 'jack':
      self.source = gst.element_factory_make("jackaudiosrc")
      self.source.set_property('connect', 'auto')
      self.source.set_property('name', "openob_"+static_conf['tx']['configuration_name'])
    elif static_conf['tx']['audio_connection'] == 'pulseaudio':
      self.source = gst.element_factory_make("pulsesrc")
    # Audio conversion/sample rate conversion/resampling magic to tie everything together.
    audioconvert = gst.element_factory_make("audioconvert")
    audioresample = gst.element_factory_make("audioresample")
    audioresample.set_property('quality', 9) # SRC
    audiorate = gst.element_factory_make("audiorate")
    self.encoder = gst.element_factory_make(static_conf['tx']['encoder']['tx'],"encoder")
    if static_conf['tx']['encoder']['tx'] != 'identity':
      self.encoder.set_property('bitrate', static_conf['tx']['bitrate'])
    self.payloader = gst.element_factory_make(static_conf['tx']['payloader']['tx'],"payloader")
    level = gst.element_factory_make("level")
    level.set_property('message', True)
    level.set_property('interval', 1000000000)
    # We'll send audio out on this
    self.udpsink_rtpout = gst.element_factory_make("udpsink", "udpsink0")
    self.udpsink_rtpout.set_property('host', static_conf['tx']['receiver_address'])
    self.udpsink_rtpout.set_property('port', int(static_conf['tx']['base_port']))
    # And send our control packets out on this
    self.udpsink_rtcpout = gst.element_factory_make("udpsink", "udpsink1")
    self.udpsink_rtcpout.set_property('host', static_conf['tx']['receiver_address'])
    self.udpsink_rtcpout.set_property('port', int(static_conf['tx']['base_port'])+1)
    # And the receiver will send us RTCP Sender Reports on this
    udpsrc_rtcpin = gst.element_factory_make("udpsrc", "udpsrc0")
    udpsrc_rtcpin.set_property('port', int(static_conf['tx']['base_port'])+2)
    # Our RTP manager
    rtpbin = gst.element_factory_make("gstrtpbin","gstrtpbin")

    # Add the elements to the pipeline and glue it all together neatly.
    self.tx.add(self.source, audioconvert, audioresample, audiorate, self.payloader, rtpbin, self.udpsink_rtpout, self.udpsink_rtcpout, udpsrc_rtcpin, level)
    # Explicitly tell the jackaudiosrc we want stereo via a capsfilter, forces JACK client to grab two ports
    if static_conf['tx']['audio_connection'] == 'jack':
      caps = gst.Caps('audio/x-raw-float, channels=2')
      capsfilter =  gst.element_factory_make("capsfilter", "filter")
      capsfilter.set_property("caps", caps)
      self.tx.add(capsfilter)
      gst.element_link_many(self.source, capsfilter, audioresample, audiorate, level, audioconvert)
    else:
      gst.element_link_many(self.source, audioresample, audiorate, level, audioconvert)
    # If we're payloading raw audio, let's just deal with the payloader.
    # We won't need an encoder.
    if static_conf['tx']['payloader']['tx'] == 'rtpL16pay':
      gst.element_link_many(audioconvert, self.payloader)
    else:
      # Add an encoder if we need one.
      self.tx.add(self.encoder)
      gst.element_link_many(audioconvert, self.encoder, self.payloader)
    

    # Now the RTP pads, which are a little trickier.
    self.payloader.link_pads('src', rtpbin, 'send_rtp_sink_0')
    rtpbin.link_pads('send_rtp_src_0', self.udpsink_rtpout, 'sink')
    rtpbin.link_pads('send_rtcp_src_0', self.udpsink_rtcpout, 'sink')
    udpsrc_rtcpin.link_pads('src', rtpbin, 'recv_rtcp_sink_0')
    # We need to have some stuff on busses to report level information
    bus.add_signal_watch()
    bus.enable_sync_message_emission()
    bus.connect('message', self.on_bus_message)

  def on_bus_message(self, bus, message):
    if message.type == gst.MESSAGE_ELEMENT:
      if message.structure.get_name() == 'level':
        self.started = True
        # This is an audio level update, which the 'level' element emits once a second.
        
        # We're storing lists here in redis to let us do historical graphing in the webUI.

        # First, push a timestamp value
        config.lpush((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":utc_timestamps"), calendar.timegm(datetime.datetime.utcnow().timetuple()))

        # Push the latest values to the Redis server
        config.lpush((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":rms:left"), message.structure['rms'][0])
        config.lpush((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":rms:right"), message.structure['rms'][1])
        config.lpush((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":peak:left"), message.structure['peak'][0])
        config.lpush((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":peak:right"), message.structure['peak'][1])
        
        # Trim the lists down to 3600 seconds (1 hour) of data to stop them getting too huge while remaining useful for level graphs
        config.ltrim((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":utc_timestamps"), 0, 3600)
        config.ltrim((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":rms:left"), 0, 3600)
        config.ltrim((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":rms:right"), 0, 3600)
        config.ltrim((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":peak:left"), 0, 3600)
        config.ltrim((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":peak:right"), 0, 3600)
        print message.structure['peak']
        
        # If the rx_level_info_key+configname+utc_timestamp last value from the receiver is more than 3 seconds behind us, then chances are the receiver's died a death. Kill ourself to restart.
        now_timestamp = calendar.timegm(datetime.datetime.utcnow().timetuple())
        rx_now_timestamp = config.lindex((static_conf['tx_level_info_key']+static_conf['tx']['configuration_name']+":utc_timestamps"),0)
        if now_timestamp < (rx_now_timestamp - 3) or now_timestamp > (rx_now_timestamp + 3):
          sys.exit("Receiver appears to have crashed or isn't running - start before starting the transmitter!")
      if message.structure.get_name() == 'udpsink':
        print message
        print message.type
        print message.structure
    else:
      print message
      print message.type
      print message.structure
    return gst.BUS_PASS

  def start(self):
    print "OpenOB TX Mode Starting"
    print "Writing initial shared configuration values"
    config.set((static_conf['port_key']+static_conf['tx']['configuration_name']),str(static_conf['tx']['base_port']))
    config.set((static_conf['buffer_size_key']+static_conf['tx']['configuration_name']),str(static_conf['tx']['jitter_buffer_size']))
    config.set((static_conf['depayloader_key']+static_conf['tx']['configuration_name']),str(static_conf['tx']['payloader']['rx']))
    config.set((static_conf['decoder_key']+static_conf['tx']['configuration_name']),str(static_conf['tx']['encoder']['rx']))
    print "Setting locked state for udpsink"
    print self.udpsink_rtcpout.set_locked_state(gst.STATE_PLAYING)
    print "Setting pipeline to PLAYING"
    print self.tx.set_state(gst.STATE_PLAYING)
    print "Waiting pipeline to settle"
    print self.tx.get_state()
    print "Writing shared configuration for RX caps"
    config.set((static_conf['caps_key']+static_conf['tx']['configuration_name']),str(self.udpsink_rtpout.get_pad('sink').get_property('caps')))
    print "Done writing configuration, all good to go now!"

  def loop(self):
    gobject.MainLoop().run()

rtp = RTPTransmitter()
rtp.start()
rtp.loop()

