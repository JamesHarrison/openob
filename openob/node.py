from __future__ import annotations

import sys
import time

from openob.audio_interface import AudioInterface
from openob.link_config import LinkConfig
from openob.logger import LoggerFactory


class Node:

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

    def __init__(self, node_name: str) -> None:
        """Set up a new node."""
        self.node_name = node_name
        self.logger_factory = LoggerFactory()
        self.logger = self.logger_factory.getLogger('node.%s' % self.node_name)

    def run_link(
        self,
        link_config: LinkConfig,
        audio_interface: AudioInterface,
    ) -> None:
        """
          Run a new TX or RX node.
        """
        # Imports deferred so Gst.init only fires when we actually need it,
        # which keeps `openob --help` fast and avoids initialising GStreamer
        # in the test process when it just needs to import openob.cli.
        try:
            from openob.rtp.rx import RTPReceiver
            from openob.rtp.tx import RTPTransmitter
        except ImportError as e:
            self.logger.critical(
                "Failed to load GStreamer pipeline modules (%s). "
                "Check that python3-gi, python3-gst-1.0, gir1.2-gstreamer-1.0, "
                "gstreamer1.0-plugins-base and gstreamer1.0-plugins-good are "
                "installed and that this Python venv was created with "
                "--system-site-packages.",
                e,
            )
            raise

        # We're now entering the realm where we should desperately try and
        # maintain a link under all circumstances forever.
        self.logger.info("Link %s initial setup start on %s" % (link_config.name, self.node_name))
        link_logger = self.logger_factory.getLogger('node.%s.link.%s' % (self.node_name, link_config.name))
        while True:
            try:
                if audio_interface.mode == 'tx':
                    try:
                        link_logger.info("Starting up transmitter")
                        # Clear any stale caps left in Redis by a previous TX
                        # run so an RX currently in blocking_get keeps waiting
                        # until we publish fresh caps for this pipeline.
                        link_config.unset("caps")
                        transmitter = RTPTransmitter(self.node_name, link_config, audio_interface)
                        transmitter.run()
                        caps = transmitter.get_caps()
                        link_logger.debug("Got caps from transmitter, setting config")
                        link_config.set("caps", caps)
                        transmitter.loop()
                        if transmitter.shutdown_requested:
                            link_logger.info("Transmitter shut down cleanly")
                            return
                    except Exception:
                        link_logger.exception("Transmitter pipeline failed; restarting in 0.5s")
                        time.sleep(0.5)
                elif audio_interface.mode == 'rx':
                    link_logger.info("Waiting for transmitter capabilities...")
                    link_config.blocking_get("caps")
                    link_logger.info("Got caps from transmitter")
                    try:
                        link_logger.info("Starting up receiver")
                        receiver = RTPReceiver(self.node_name, link_config, audio_interface)
                        receiver.run()
                        receiver.loop()
                        if receiver.shutdown_requested:
                            link_logger.info("Receiver shut down cleanly")
                            return
                    except Exception:
                        link_logger.exception("Receiver pipeline failed; restarting in 0.1s")
                        time.sleep(0.1)
                else:
                    link_logger.critical("Unknown audio interface mode (%s)!" % audio_interface.mode)
                    sys.exit(1)
            except Exception:
                link_logger.exception(
                    "Unhandled exception in run_link supervisor; please report this as a bug"
                )
                raise
