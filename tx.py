#!/usr/bin/env python

# OpenOB TX
# James Harrison <james@talkunafraid.co.uk>
# Many thanks to http://blog.abourget.net/2009/6/14/gstreamer-rtp-and-live-streaming/
#  and the GStreamer developers.

import gobject
gobject.threads_init()
import pygst
pygst.require("0.10")
import gst
import redis
config = redis.Redis("REDIS_HOST")
REMOTE_HOST="ENDPOINT_ADDRESS"

class RTPTransmitter():
  def __init__(self):
    self.tx = gst.Pipeline("tx")
    bus = self.tx.get_bus()
    # Audio pipeline elements
    source = gst.element_factory_make("alsasrc")
    source.set_property('device', 'hw:0,0')
    audioconvert = gst.element_factory_make("audioconvert")
    audioresample = gst.element_factory_make("audioresample")
    audioresample.set_property('quality', 9)
    audiorate = gst.element_factory_make("audiorate")
    encoder = gst.element_factory_make("celtenc","celt-encode")
    #encoder.set_property('max-bitrate', 320000)
    encoder.set_property('bitrate', 128000)
    payloader = gst.element_factory_make("rtpceltpay","celt-payload")
    level = gst.element_factory_make("level")
    level.set_property('message', True)
    level.set_property('interval', 1000000000)
    # We'll send audio out on this
    self.udpsink_rtpout = gst.element_factory_make("udpsink", "udpsink0")
    self.udpsink_rtpout.set_property('host', REMOTE_HOST)
    self.udpsink_rtpout.set_property('port', 3000)
    # And send our control packets out on this
    self.udpsink_rtcpout = gst.element_factory_make("udpsink", "udpsink1")
    self.udpsink_rtcpout.set_property('host', REMOTE_HOST)
    self.udpsink_rtcpout.set_property('port', 3001)
    # And the receiver will send us RTCP Sender Reports on this
    udpsrc_rtcpin = gst.element_factory_make("udpsrc", "udpsrc0")
    udpsrc_rtcpin.set_property('port', 3002)
    # Our RTP manager
    rtpbin = gst.element_factory_make("gstrtpbin","gstrtpbin")

    # Add the elements to the pipeline
    self.tx.add(source, audioconvert, audioresample, audiorate, encoder, payloader, rtpbin, self.udpsink_rtpout, self.udpsink_rtcpout, udpsrc_rtcpin, level)

    # Now we link them together, pad to pad
    gst.element_link_many(source, audioconvert, audioresample, audiorate, level, encoder, payloader)

    # Now the RTP pads
    payloader.link_pads('src', rtpbin, 'send_rtp_sink_0')
    rtpbin.link_pads('send_rtp_src_0', self.udpsink_rtpout, 'sink')
    rtpbin.link_pads('send_rtcp_src_0', self.udpsink_rtcpout, 'sink')
    udpsrc_rtcpin.link_pads('src', rtpbin, 'recv_rtcp_sink_0')

    bus.add_signal_watch()
    bus.enable_sync_message_emission()
    bus.connect('message', self.on_bus_message)

  def on_bus_message(self, bus, message):
    if message.type == gst.MESSAGE_ELEMENT:
      if message.structure.get_name() == 'level':
        # This is an audio level update
        print "@ %s PEAK: %s DECAY: %s RMS: %s" % (message.structure['stream-time'],message.structure['peak'],message.structure['decay'],message.structure['rms'])
    return gst.BUS_PASS


  def start(self):
    print "Setting locked state for udpsink"
    print self.udpsink_rtcpout.set_locked_state(gst.STATE_PLAYING)
    print "Setting pipeline to PLAYING"
    print self.tx.set_state(gst.STATE_PLAYING)
    print "Waiting pipeline to settle"
    print self.tx.get_state()
    print "Final caps written to redis"
    config.set("caps",str(self.udpsink_rtpout.get_pad('sink').get_property('caps')))

  def loop(self):
    gobject.MainLoop().run()

rtp = RTPTransmitter()
rtp.start()
rtp.loop()

