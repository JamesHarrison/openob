import gobject
import pygst
pygst.require("0.10")
import gst
import time
import re
from colorama import Fore, Back, Style
class RTPTransmitter:
  def __init__(self, audio_input='alsa', audio_device='hw:0', base_port=3000, encoding='opus', bitrate=96, jack_name='openob_tx', receiver_address='localhost', opus_options={'audio': True, 'bandwidth': -1000, 'frame-size': 20, 'complexity': 5, 'constrained-vbr': True, 'inband-fec': True, 'packet-loss-percentage': 1}):
    """Sets up a new RTP transmitter"""
    self.started = False
    self.pipeline = gst.Pipeline("tx")
    self.bus = self.pipeline.get_bus()
    self.bus.connect("message", self.on_message)
    self.caps = 'None'
    self.encoding = encoding
    # Audio input
    if audio_input == 'alsa':
      self.source = gst.element_factory_make('alsasrc')
      self.source.set_property('device', audio_device)
    elif audio_input == 'jack':
      self.source = gst.element_factory_make("jackaudiosrc")
      self.source.set_property('connect', 'auto')
      self.source.set_property('name', jack_name)
    elif audio_input == 'pulseaudio':
      self.source = gst.element_factory_make("pulsesrc")

    # Audio conversion and resampling
    self.audioconvert = gst.element_factory_make("audioconvert")
    self.audioresample = gst.element_factory_make("audioresample")
    self.audioresample.set_property('quality', 9) # SRC
    self.audiorate = gst.element_factory_make("audiorate")

    # Encoding and payloading
    if encoding == 'celt':
      self.encoder = gst.element_factory_make("celtenc", "encoder")
      self.encoder.set_property('bitrate', bitrate*1000)
      self.payloader = gst.element_factory_make("rtpceltpay", "payloader")
    elif encoding == 'opus':
      self.encoder = gst.element_factory_make("opusenc", "encoder")
      self.encoder.set_property('bitrate', bitrate*1000)
      for key, value in opus_options.iteritems():
        self.encoder.set_property(key, value)
      self.payloader = gst.element_factory_make("rtpopuspay", "payloader")
    elif encoding == 'pcm':
      # we have no encoder for PCM operation
      self.payloader = gst.element_factory_make("rtpL16pay", "payloader")

    # Now the RTP bits
    # We'll send audio out on this
    self.udpsink_rtpout = gst.element_factory_make("udpsink", "udpsink_rtp")
    self.udpsink_rtpout.set_property('host', receiver_address)
    self.udpsink_rtpout.set_property('port', base_port)
    # And send our control packets out on this
    self.udpsink_rtcpout = gst.element_factory_make("udpsink", "udpsink_rtcp")
    self.udpsink_rtcpout.set_property('host', receiver_address)
    self.udpsink_rtcpout.set_property('port', base_port+1)
    # And the receiver will send us RTCP Sender Reports on this
    self.udpsrc_rtcpin = gst.element_factory_make("udpsrc", "udpsrc_rtcp")
    self.udpsrc_rtcpin.set_property('port', base_port+2)
    # (but we'll ignore them/operate fine without them because we assume we're stuck behind a firewall)
    # Our RTP manager
    self.rtpbin = gst.element_factory_make("gstrtpbin","gstrtpbin")

    # Our level monitor, also used for continuous audio
    self.level = gst.element_factory_make("level")
    self.level.set_property('message', True)
    self.level.set_property('interval', 1000000000)

    # Add to the pipeline
    self.pipeline.add(self.source, self.audioconvert, self.audioresample, self.audiorate, self.payloader, self.udpsink_rtpout, self.udpsink_rtcpout, self.udpsrc_rtcpin, self.rtpbin, self.level)
    if encoding != 'pcm':
      # Only add an encoder if we're not in PCM mode
      self.pipeline.add(self.encoder)

    # Add a capsfilter to set JACK up right if we're using JACK for input
    # Then link our input section
    if audio_input == 'jack':
      caps = gst.Caps('audio/x-raw-float, channels=2')
      self.capsfilter =  gst.element_factory_make("capsfilter", "filter")
      self.capsfilter.set_property("caps", caps)
      self.pipeline.add(self.capsfilter)
      gst.element_link_many(self.source, self.capsfilter, self.level, self.audioresample, self.audiorate, self.audioconvert)
    else:
      gst.element_link_many(self.source, self.level, self.audioresample, self.audiorate, self.audioconvert)
    # Now we get to link this up to our encoder/payloader

    if encoding != 'pcm':
      gst.element_link_many(self.audioconvert, self.encoder, self.payloader)
    else:
      gst.element_link_many(self.audioconvert, self.payloader)

    # And now the RTP bits
    self.payloader.link_pads('src', self.rtpbin, 'send_rtp_sink_0')
    self.rtpbin.link_pads('send_rtp_src_0', self.udpsink_rtpout, 'sink')
    self.rtpbin.link_pads('send_rtcp_src_0', self.udpsink_rtcpout, 'sink')
    self.udpsrc_rtcpin.link_pads('src', self.rtpbin, 'recv_rtcp_sink_0')

    # Connect our bus up
    self.bus.add_signal_watch()
    self.bus.connect('message', self.on_message)

  def run(self):
    self.udpsink_rtcpout.set_locked_state(gst.STATE_PLAYING)
    self.pipeline.set_state(gst.STATE_PLAYING)
    print self.pipeline.get_state()
    while self.caps == 'None':
      self.pipeline.get_state()
      print(" -- Waiting for caps - if you get this a lot, you probably can't access the requested audio device.")
      self.caps = str(self.udpsink_rtpout.get_pad('sink').get_property('caps'))
      # Fix for gstreamer bug in rtpopuspay fixed in GST-plugins-bad 50140388d2b62d32dd9d0c071e3051ebc5b4083b, bug 686547
      if self.encoding == 'opus':
        self.caps = re.sub(r'(caps=.+ )', '', self.caps)
      time.sleep(0.1)
  def loop(self):
    try:
      self.loop = gobject.MainLoop()
      self.loop.run()
    except Exception, e:
      print("Exception encountered in transmitter - %s" % e)
      self.pipeline.set_state(gst.STATE_NULL)
  def on_message (self, bus, message):
    if message.type == gst.MESSAGE_ELEMENT:
      if message.structure.get_name() == 'level':
        self.started = True
        if int(message.structure['peak'][0]) > -1 or int(message.structure['peak'][1]) > -1:
          print(Fore.BLACK + Back.RED + (" -- Transmitting: L %3.2f R %3.2f (Peak L %3.2f R %3.2f) !!! CLIP  !!!" % (message.structure['rms'][0], message.structure['rms'][1], message.structure['peak'][0], message.structure['peak'][1])) + Fore.RESET + Back.RESET + Style.RESET_ALL)
        elif int(message.structure['peak'][0]) > -5 or int(message.structure['peak'][1]) > -5:
          print(Fore.BLACK + Back.YELLOW + (" -- Transmitting: L %3.2f R %3.2f (Peak L %3.2f R %3.2f) !!! LEVEL !!!" % (message.structure['rms'][0], message.structure['rms'][1], message.structure['peak'][0], message.structure['peak'][1])) + Fore.RESET + Back.RESET + Style.RESET_ALL)
        else:
          print(Fore.BLACK + Back.GREEN + (" -- Transmitting: L %3.2f R %3.2f (Peak L %3.2f R %3.2f) (Level OK)" % (message.structure['rms'][0], message.structure['rms'][1], message.structure['peak'][0], message.structure['peak'][1])) + Fore.RESET + Back.RESET + Style.RESET_ALL)
    return True
  def get_caps(self):
    return self.caps
