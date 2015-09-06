import logging
import multiprocessing as mp
from system_config import SystemConfig


class Node(object):
    """Node is the core class and entry point for an OpenOB daemon or program

    A node wraps and manages all links associated with its configuration,
    and ensures they stay running.
    """
    def __init__(self):
        self.encoders = {}
        self.decoders = {}

    def setup(self, node_id, config_hosts, audio_interfaces, log_level):
        """Set up the node's basic configuration"""
        logging.basicConfig(level=log_level,
                            format='%(asctime)s:%(levelname)s: %(message)s')
        self.node_id = node_id
        self.system = SystemConfig(config_hosts)
        self.system.connect()
        self.config = self.system.node(self.node_id)
        self.config.setup()
        self.config.set_audio_interfaces(audio_interfaces)
        self.config.set_status_data()

    def run(self):
        """Run the node; this method blocks and will run until interrupted"""
        logging.info('Starting OpenOB node ID "%s"' % self.node_id)

        self.update_encoders()
        self.update_decoders()

        # At this point we've got a bunch of processes representing our
        # configured encoders and decoders, all started.
        self.monitor_processes()

    def monitor_processes(self):
        logging.warn("Would now start monitoring running encoders/decoders.")
        logging.fatal("But I'm not going to because it's not implemented yet.")
        # TODO: Intelligently watch for processes dying, log exit codes
        # TODO: Restart dead processes from current config
        for eid, enc in self.encoders.items():
            enc.join()
        for did, dec in self.decoders.items():
            dec.join()

    def setup_encoder(self, eid, config):
        """Set up a new encoder, or re-create an existing one with a new
        configuration"""
        if self.encoders[eid]:
            # FIXME: Probably should be nicer about this. Maybe.
            self.encoders[eid].terminate()
        self.encoders[eid] = mp.Process(target=self.run_encoder(config))
        self.encoders[eid].start()

    def setup_decoder(self, did, config):
        pass

    def update_encoders(self):
        pass

    def update_decoders(self):
        pass

    def shutdown(self, code=0):
        """Shut down the node gracefully"""
        self.config.stop()
