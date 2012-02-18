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

class RTPReceiver:
  def __init__(self):
    config = redis.Redis("REDIS_HOST")
    self.rx = gst.Pipeline("rx")
    bus = self.rx.get_bus()
    # Audio pipeline elements
    audioconvert = gst.element_factory_make("audioconvert")
    audioresample = gst.element_factory_make("audioresample")
    audioresample.set_property('quality', 9)
    audiorate = gst.element_factory_make("audiorate")
    sink = gst.element_factory_make("alsasink")
    decoder = gst.element_factory_make("celtdec","celt-decode")
    self.depayloader = gst.element_factory_make("rtpceltdepay","celt-depayload")
    level = gst.element_factory_make("level")
    level.set_property('message', True)
    level.set_property('interval', 1000000000)

    # Our RTP receive manager
    self.rtpbin = gst.element_factory_make('gstrtpbin')
    self.rtpbin.set_property('latency', 150)
    self.rtpbin.set_property('autoremove', True)
    self.rtpbin.set_property('do-lost', True)
    # Where audio comes in
    udpsrc_rtpin = gst.element_factory_make('udpsrc')
    udpsrc_rtpin.set_property('port', 3000)
    # Set our caps from redis
    caps = config.get("caps").replace('\\', '')
    udpsrc_caps = gst.caps_from_string(caps)
    udpsrc_rtpin.set_property('caps', udpsrc_caps)
    udpsrc_rtpin.set_property('timeout', 5000000)
    # Where our RTCP control messages come in
    udpsrc_rtcpin = gst.element_factory_make('udpsrc')
    udpsrc_rtcpin.set_property('port', 3001)
    udpsrc_rtcpin.set_property('timeout', 5000000)
    # And where we'll send RTCP Sender Reports
    udpsink_rtcpout = gst.element_factory_make('udpsink')
    udpsink_rtcpout.set_property('host', "0.0.0.0")
    udpsink_rtcpout.set_property('port', 3002)


    # Add the elements to the pipeline
    self.rx.add(audiorate, audioresample, audioconvert, sink, decoder, self.depayloader, self.rtpbin, udpsrc_rtpin, udpsrc_rtcpin, udpsink_rtcpout, level)
    # Link anything with static pads
    gst.element_link_many(self.depayloader, decoder, audioconvert, level, audioresample, audiorate, sink)
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
    print "PAD ADDED"
    print "  obj", obj
    print "  pad", pad
    # Relink
    self.rtpbin.link(self.depayloader)

  def on_bus_message(self, bus, message):
    print "BUS MESSAGE"
    print "  bus", bus
    print "  message", message

  def start(self):
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

