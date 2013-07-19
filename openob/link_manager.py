import logging
import sys
import time
import redis
from rtp.tx import RTPTransmitter
from rtp.rx import RTPReceiver
import gst
# OpenOB Link Manager
# One of these runs at each end and negotiates everything (RX pushes config info to TX), reconnects when links fail, and so on.
class LinkManager(object):
  '''OpenOB Manager. Handles management of links, mostly recovery from failures.'''
  def __init__(self):
    self.logger = logging.getLogger('openob.link_manager')
    self.logger.info("LinkManager established")
  def process_levels(self, message):
    if len(message.structure['peak']) == 2:
      self.logger.info("Peak levels: L %d R %d" % (message.structure['peak'][0], message.structure['peak'][1]))
    else:
      self.logger.info("Peak levels: M %d" % message.structure['peak'][0])
    # L, R, L, R (RMS, RMS, Peak, Peak)
    # message.structure['rms'][0], message.structure['rms'][1], message.structure['peak'][0], message.structure['peak'][1]
  def run(self, opts):
    self.logger = logging.getLogger('openob.link_manager.%s' % opts.link_name)
    self.logger.info("Starting link '%s'" % opts.link_name)
    # We're now entering the realm where we should desperately try and maintain a link under all circumstances forever.
    while True:
      try:
        # Set up redis and connect
        config = None
        while True:
          try:
            config = redis.Redis(opts.config_host)
            break
          except Exception, e:
            time.sleep(0.5)

        # So if we're a transmitter, let's set the options the receiver needs to know about
        link_key = "openob2:"+opts.link_name+":"
        if opts.mode == 'tx':
          if opts.encoding == 'celt' and int(opts.bitrate) > 192:
            self.logger.warn("Can't use bitrates higher than 192kbps for CELT, limiting from %dkbps" % opts.bitrate)
            opts.bitrate = 192
          # We're a transmitter!
          config.set(link_key+"port", opts.port)
          config.set(link_key+"jitter_buffer", opts.jitter_buffer)
          config.set(link_key+"encoding", opts.encoding)
          config.set(link_key+"bitrate", opts.bitrate)
          self.logger.info("Configured receiver with:")
          self.logger.info("Base Port:     %s" % config.get(link_key+"port"))
          self.logger.info("Jitter Buffer: %s ms" % config.get(link_key+"jitter_buffer"))
          self.logger.info("Encoding:      %s" % config.get(link_key+"encoding"))
          self.logger.info("Bitrate:       %s kbit/s" % config.get(link_key+"bitrate"))
          # Okay, we can't set caps yet - we need to configure ourselves first.
          opus_opts = {'audio': True, 'bandwidth': -1000, 'frame-size': opts.framesize, 'complexity': opts.complexity, 'constrained-vbr': True, 'inband-fec': opts.fec, 'packet-loss-percentage': opts.loss, 'dtx': opts.dtx}
          try:
            transmitter = RTPTransmitter(audio_input=opts.audio_input, audio_device=opts.device, base_port=opts.port, encoding=opts.encoding, bitrate=opts.bitrate, jack_name=("openob_tx_%s" % opts.link_name), receiver_address=opts.receiver_host, opus_options=opus_opts)
            # Set it up, get caps
            try:
              transmitter.run()
              config.set(link_key+"caps", transmitter.get_caps())
              self.logger.info("Caps:          %s" % config.get(link_key+"caps"))
              transmitter.loop()
            except Exception, e:
              self.logger.error("Lost connection or otherwise had the transmitter fail on us, restarting (%s)" % e)
              time.sleep(0.3)
          except gst.ElementNotFoundError, e:
            self.logger.critical("Couldn't fulfill our gstreamer module dependencies! You don't have the following element available on this system: %s" % e)
            sys.exit(1)
        else:
          # We're a receiver!
          # Default values.
          port = 3000
          caps = ''
          jitter_buffer = 150
          encoding = 'opus'
          bitrate = '96'
          while True:
            try:
              if config.get(link_key+"port") == None:
                self.logger.warn("Unable to configure myself from the configuration host; has the transmitter been started yet, and have you got the same link name on each end? Waiting half a second to recheck")
                time.sleep(0.5)
              port = int(config.get(link_key+"port"))
              caps = config.get(link_key+"caps")
              jitter_buffer = int(config.get(link_key+"jitter_buffer"))
              encoding = config.get(link_key+"encoding")
              bitrate = int(config.get(link_key+"bitrate"))
              self.logger.info("Configured receiver from transmitter with:")
              self.logger.info("Base Port:     %s" % port)
              self.logger.info("Jitter Buffer: %s ms" % caps)
              self.logger.info("Encoding:      %s" % encoding)
              self.logger.info("Bitrate:       %s kbit/s" % bitrate)
              self.logger.info("Caps:          %s" % caps)
              break
            except Exception, e:
              self.logger.warn("Unable to configure myself from the configuration host; has the transmitter been started yet, and have you got the same link name on each end? Waiting half a second to recheck")
              time.sleep(0.5)
              #raise
          # Okay, we can now configure ourself
          receiver = RTPReceiver(audio_output=opts.audio_output, audio_device=opts.device, base_port=port, encoding=encoding, caps=caps, bitrate=bitrate, jitter_buffer=jitter_buffer, jack_name=("openob_tx_%s" % opts.link_name) )
          try:
            receiver.run()
            receiver.loop()
          except Exception, e:
            self.logger.error("Lost connection or otherwise had the transmitter fail on us, restarting (%s)" % e)
            time.sleep(0.5)

      except Exception, e:
        self.logger.critical("Unhandled exception occured, please report me as a bug! (%s)" % e)
        raise
