import sys
import time
from openob.logger import LoggerFactory
from openob.rtp.tx import RTPTransmitter
from openob.rtp.rx import RTPReceiver
from openob.link_config import LinkConfig
from gst import ElementNotFoundError


class Node(object):

    """
        OpenOB node instance.

        Nodes run links. Each Node looks after its end of a link, ensuring
        that it remains running and tries to recover from failures, as well as
        responding to configuration changes.

        Nodes have a name; everything else is link specific.

        For instance, a node might be the 'studio' node, which would run a
        'tx' end for the 'stl' link.

        Nodes have a config host which is where they store their inter-Node
        data and communicate with other Nodes.
    """

    def __init__(self, node_name):
        """Set up a new node."""
        self.node_name = node_name
        self.logger_factory = LoggerFactory()
        self.logger = self.logger_factory.getLogger('node.%s' % self.node_name)

    def run_link(self, link_config, audio_interface):
        """
          Run a new TX or RX node.
        """
        # We're now entering the realm where we should desperately try and
        # maintain a link under all circumstances forever.
        self.logger.info("Link %s initial setup start on %s" % (link_config.name, self.node_name))
        link_logger = self.logger_factory.getLogger('node.%s.link.%s.%s' % (self.node_name, link_config.name, audio_interface.mode))
        while True:
            try:
                if audio_interface.mode == 'tx':
                    try:
                        transmitter = RTPTransmitter(self.node_name, link_config, audio_interface)
                        link_logger.info("Starting up transmitter")
                        transmitter.run()
                        caps = transmitter.get_caps()
                        link_logger.info("Got caps, setting config - %s" % caps)
                        link_config.set("caps", caps)
                        transmitter.loop()
                    except ElementNotFoundError as e:
                        link_logger.critical("GStreamer element missing: %s - will now exit" % e)
                        sys.exit(1)
                    except Exception as e:
                        link_logger.exception("Transmitter crashed for some reason! Restarting...")
                        time.sleep(0.5)
                elif audio_interface.mode == 'rx':
                    link_logger.info("Waiting for transmitter capabilities...")
                    caps = link_config.blocking_get("caps")
                    link_logger.info("Got caps from transmitters - %s" % caps)
                    try:
                        link_logger.info("Starting up receiver")
                        receiver = RTPReceiver(self.node_name, link_config, audio_interface)
                        receiver.run()
                        receiver.loop()
                    except ElementNotFoundError as e:
                        link_logger.critical("GStreamer element missing: %s - will now exit" % e)
                        sys.exit(1)
                    except Exception as e:
                        link_logger.exception("Receiver crashed for some reason! Restarting...")
                        time.sleep(0.1)
                else:
                    link_logger.critical("Unknown audio interface mode (%s)!" % audio_interface.mode)
                    sys.exit(1)
            except Exception as e:
                link_logger.exception("Unknown exception thrown - please report this as a bug! %s" % e)
                raise
