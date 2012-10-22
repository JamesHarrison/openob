import gobject
import pygst
pygst.require("0.10")
import gst
import re
from colorama import Fore, Back, Style
class RTPReceiver:
  def __init__(self, caps='', audio_output='alsa', audio_device='hw:0', base_port=3000, encoding='celt', bitrate=96, jitter_buffer=150, jack_name='openob_rx'):
    """Sets up a new RTP receiver"""
    self.started = False
    self.pipeline = gst.Pipeline("rx")
    self.bus = self.pipeline.get_bus()
    self.bus.connect("message", self.on_message)
    # Audio output
    if audio_output == 'alsa':
      self.sink = gst.element_factory_make("alsasink")
      self.sink.set_property('device', audio_device)
    elif audio_output == 'jack':
      self.sink = gst.element_factory_make("jackaudiosink")
      self.sink.set_property('connect', 'auto')
      self.sink.set_property('name', jack_name)
    elif audio_output == 'pulseaudio':
      self.source = gst.element_factory_make("pulsesink")
    # Audio conversion and resampling
    self.audioconvert = gst.element_factory_make("audioconvert")
    self.audioresample = gst.element_factory_make("audioresample")
    self.audioresample.set_property('quality', 9)
    self.audiorate = gst.element_factory_make("audiorate")

    # Decoding and depayloading
    if encoding == 'celt':
      self.decoder = gst.element_factory_make("celtdec","decoder")
      self.depayloader = gst.element_factory_make("rtpceltdepay","depayloader")
    elif encoding == 'opus':
      self.decoder = gst.element_factory_make("opusdec","decoder")
      self.decoder.set_property('use-inband-fec', True) # FEC
      self.decoder.set_property('plc', True) # Packet loss concealment
      self.depayloader = gst.element_factory_make("rtpopusdepay","depayloader")
    elif encoding == 'pcm':
      self.depayloader = gst.element_factory_make("rtpL16depay","depayloader")

    # RTP stuff
    self.rtpbin = gst.element_factory_make('gstrtpbin')
    self.rtpbin.set_property('latency', jitter_buffer)
    self.rtpbin.set_property('autoremove', True)
    self.rtpbin.set_property('do-lost', True)
    # Where audio comes in
    self.udpsrc_rtpin = gst.element_factory_make('udpsrc')
    self.udpsrc_rtpin.set_property('port', base_port)
    caps = caps.replace('\\', '')
    # Fix for gstreamer bug in rtpopuspay fixed in GST-plugins-bad 50140388d2b62d32dd9d0c071e3051ebc5b4083b, bug 686547
    if encoding == 'opus':
      caps = re.sub(r'(caps=.+ )', '', caps)
    udpsrc_caps = gst.caps_from_string(caps)
    self.udpsrc_rtpin.set_property('caps', udpsrc_caps)
    self.udpsrc_rtpin.set_property('timeout', 5000000)
    # Where our RTCP control messages come in
    self.udpsrc_rtcpin = gst.element_factory_make('udpsrc')
    self.udpsrc_rtcpin.set_property('port', base_port+1)
    # And where we'll send RTCP Sender Reports (a black hole - we assume we can't contact the sender, and this is optional)
    self.udpsink_rtcpout = gst.element_factory_make('udpsink')
    self.udpsink_rtcpout.set_property('host', "0.0.0.0")
    self.udpsink_rtcpout.set_property('port', base_port+2)

    # Our level monitor, also used for continuous audio
    self.level = gst.element_factory_make("level")
    self.level.set_property('message', True)
    self.level.set_property('interval', 1000000000)

    # And now we've got it all set up we need to add the elements
    self.pipeline.add(self.audiorate, self.audioresample, self.audioconvert, self.sink, self.level, self.depayloader, self.rtpbin, self.udpsrc_rtpin, self.udpsrc_rtcpin, self.udpsink_rtcpout)
    if encoding != 'pcm':
      self.pipeline.add(self.decoder)
      gst.element_link_many(self.depayloader, self.decoder, self.audioconvert)
    else:
      gst.element_link_many(self.depayloader, self.audioconvert)
    gst.element_link_many(self.audioconvert, self.audioresample, self.audiorate, self.level, self.sink)
    for p in self.udpsrc_rtpin.pads():
      print p
      print p.get_caps()
    # Now the RTP pads
    self.udpsrc_rtpin.link_pads('src', self.rtpbin, 'recv_rtp_sink_0')
    self.udpsrc_rtcpin.link_pads('src', self.rtpbin, 'recv_rtcp_sink_0')
    # RTCP SRs
    self.rtpbin.link_pads('send_rtcp_src_0', self.udpsink_rtcpout, 'sink')

    # Attach callbacks for dynamic pads (RTP output) and busses
    self.rtpbin.connect('pad-added', self.rtpbin_pad_added)
    self.bus.add_signal_watch()

  # Our RTPbin won't give us an audio pad till it receives, so we need to attach it here
  def rtpbin_pad_added(self, obj, pad):
    # Unlink first.
    self.rtpbin.unlink(self.depayloader)
    # Relink
    self.rtpbin.link(self.depayloader)
  def on_message (self, bus, message):
    if message.type == gst.MESSAGE_ELEMENT:
      if message.structure.get_name() == 'level':
        self.started = True
        if int(message.structure['peak'][0]) > -1 or int(message.structure['peak'][1]) > -1:
          print(Fore.BLACK + Back.RED + (" -- Receiving: L %3.2f R %3.2f (Peak L %3.2f R %3.2f) !!! CLIP  !!!" % (message.structure['rms'][0], message.structure['rms'][1], message.structure['peak'][0], message.structure['peak'][1])) + Fore.RESET + Back.RESET + Style.RESET_ALL)
        elif int(message.structure['peak'][0]) > -5 or int(message.structure['peak'][1]) > -5:
          print(Fore.BLACK + Back.YELLOW + (" -- Receiving: L %3.2f R %3.2f (Peak L %3.2f R %3.2f) !!! LEVEL !!!" % (message.structure['rms'][0], message.structure['rms'][1], message.structure['peak'][0], message.structure['peak'][1])) + Fore.RESET + Back.RESET + Style.RESET_ALL)
        else:
          print(Fore.BLACK + Back.GREEN + (" -- Receiving: L %3.2f R %3.2f (Peak L %3.2f R %3.2f) (Level OK)" % (message.structure['rms'][0], message.structure['rms'][1], message.structure['peak'][0], message.structure['peak'][1])) + Fore.RESET + Back.RESET + Style.RESET_ALL)
      if message.structure.get_name() == 'GstUDPSrcTimeout':
        # Only UDP source configured to emit timeouts is the audio input
        print " -- No data for 5 seconds!"
        if self.started:
          print " -- Shutting down receiver for restart of link"
          self.pipeline.set_state(gst.STATE_NULL)
          self.loop.quit()
    #if message.structure:
    #  print("MSG " + bus.get_name() + ": " + message.structure.to_string())
    return True
  def run(self):
    self.pipeline.set_state(gst.STATE_PLAYING)
  def loop(self):
    self.loop = gobject.MainLoop()
    self.loop.run()