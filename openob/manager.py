import sys
import time
import redis
from openob.rtp.tx import RTPTransmitter
from openob.rtp.rx import RTPReceiver
import gst
from colorama import Fore, Back, Style
# OpenOB Link Manager
# One of these runs at each end and negotiates everything (RX pushes config info to TX), reconnects when links fail, and so on.
class Manager:
  '''OpenOB Manager. Handles management of links, mostly recovery from failures.'''
  def run(self, opts):
    print("-- OpenOB Audio Link")
    print(" -- Starting Up")
    print(" -- Parameters: %s" % opts)
    # We're now entering the realm where we should desperately try and maintain a link under all circumstances forever.
    while True:
      try:
        # Set up redis and connect
        config = None
        while True:
          try:
            config = redis.Redis(opts.config_host)
            print(" -- Connected to configuration server")
            break
          except Exception, e:
            print(Fore.BLACK + Back.RED + " -- Couldn't connect to Redis! Ensure your configuration host is set properly, and you can connect to the default Redis port on that host from here (%s)." % e)
            print("    Waiting half a second and attempting to connect again." + Fore.RESET + Back.RESET)
            time.sleep(0.5)

        # So if we're a transmitter, let's set the options the receiver needs to know about
        link_key = "openob2:"+opts.link_name+":"
        if opts.mode == 'tx':
          if opts.encoding == 'celt' and int(opts.bitrate) > 192:
            print(Fore.BLACK + Back.YELLOW + " -- WARNING: Can't use bitrates higher than 192kbps for CELT, limiting" + Fore.RESET + Back.RESET)
            opts.bitrate = 192
          # We're a transmitter!
          config.set(link_key+"port", opts.port)
          config.set(link_key+"jitter_buffer", opts.jitter_buffer)
          config.set(link_key+"encoding", opts.encoding)
          config.set(link_key+"bitrate", opts.bitrate)
          print(" -- Configured receiver with:")
          print("   - Base Port:     %s" % config.get(link_key+"port"))
          print("   - Jitter Buffer: %s ms" % config.get(link_key+"jitter_buffer"))
          print("   - Encoding:      %s" % config.get(link_key+"encoding"))
          print("   - Bitrate:       %s kbit/s" % config.get(link_key+"bitrate"))
          # Okay, we can't set caps yet - we need to configure ourselves first.
          try:
            transmitter = RTPTransmitter(audio_input=opts.audio_input, audio_device=opts.device, base_port=opts.port, encoding=opts.encoding, bitrate=opts.bitrate, jack_name=("openob_tx_%s" % opts.link_name), receiver_address=opts.receiver_host)
            # Set it up, get caps
            try:
              transmitter.run()
              config.set(link_key+"caps", transmitter.get_caps())
              print("   - Caps:          %s" % config.get(link_key+"caps"))
              transmitter.loop()
            except Exception, e:
              print(Fore.BLACK + Back.RED + " -- Lost connection or otherwise had the transmitter fail on us, restarting (%s)" % e)
              time.sleep(0.5)
          except gst.ElementNotFoundError, e:
            print(Fore.BLACK + Back.RED + (" -- Couldn't fulfill our gstreamer module dependencies! You don't have the following element available: %s" % e) + Fore.RESET + Back.RESET)
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
                print(Fore.BLACK + Back.YELLOW + " -- Unable to configure myself from the configuration host; has the transmitter been started yet, and have you got the same link name on each end?")
                print("    Waiting half a second and attempting to reconfigure myself." + Fore.RESET + Back.RESET)
                time.sleep(0.5)
              port = int(config.get(link_key+"port"))
              caps = config.get(link_key+"caps")
              jitter_buffer = int(config.get(link_key+"jitter_buffer"))
              encoding = config.get(link_key+"encoding")
              bitrate = int(config.get(link_key+"bitrate"))
              print(" -- Configured from transmitter with:")
              print("   - Base Port:     %s" % port)
              print("   - Jitter Buffer: %s ms" % caps)
              print("   - Encoding:      %s" % encoding)
              print("   - Bitrate:       %s kbit/s" % bitrate)
              print("   - Caps:          %s" % caps)
              break
            except Exception, e:
              print(Fore.BLACK + Back.YELLOW + " -- Unable to configure myself from the configuration host; has the transmitter been started yet? (%s)" % e)
              print("    Waiting half a second and attempting to reconfigure myself." + Fore.RESET + Back.RESET)
              time.sleep(0.5)
              #raise
          # Okay, we can now configure ourself
          receiver = RTPReceiver(audio_output=opts.audio_output, audio_device=opts.device, base_port=port, encoding=encoding, caps=caps, bitrate=bitrate, jitter_buffer=jitter_buffer, jack_name=("openob_tx_%s" % opts.link_name) )
          try:
            receiver.run()
            receiver.loop()
          except Exception, e:
            print(Fore.BLACK + Back.RED + (" -- Lost connection or otherwise had the receiver fail on us, restarting (%s)" % e) + Fore.RESET + Back.RESET)
            time.sleep(0.5)

      except Exception, e:
        print(Fore.BLACK + Back.RED + " -- Unhandled exception occured, please report this as a bug!" + Fore.RESET + Back.RESET)
        raise