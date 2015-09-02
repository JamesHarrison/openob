from kazoo.client import KazooClient, KazooState
import logging
import json
import os
import socket
import psutil
import time
import multiprocessing as mp


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
        self.audio_interfaces = audio_interfaces
        logging.info('Connecting to Zookeeper on "%s"' % config_hosts)
        self.zk = KazooClient(hosts=config_hosts)
        self.zk_base = "/openob/nodes/%s" % self.node_id

    def run(self):
        """Run the node; this method blocks and will run until interrupted"""
        logging.info('Starting OpenOB node ID "%s"' % self.node_id)
        self.zk.start()
        self.zk.add_listener(self.zk_state_change_handler)

        self.zk.ensure_path(self.zk_base)
        self.zk.ensure_path("%s/encoders" % self.zk_base)
        self.zk.ensure_path("%s/decoders" % self.zk_base)

        # Clean up any existing audio interface records
        self.zk.delete("%s/audio_interfaces" % self.zk_base,
                       recursive=True)

        # Ensure the root node exists
        self.zk.ensure_path("%s/audio_interfaces" % self.zk_base)
        for iid, config in self.audio_interfaces.items():
            # Write out each available audio interface with its config in JSON
            path = "%s/audio_interfaces/%s" % (self.zk_base, iid)
            self.zk.create(path, json.dumps(dict(config)).encode('utf8'))

        self.set_status_data()

        # Now we've configured all the info about this node, it's time to
        # configure our encoders/decoders etc
        encoder_ids = self.zk.get_children("%s/encoders" % self.zk_base,
                                           watch=self.update_encoders)
        for eid in encoder_ids:
            data = self.zk.get('%s/encoders/%s' % (self.zk_base, eid))
            config = json.loads(str(data.decode('utf8')))
            self.setup_encoder(eid, config)

        decoder_ids = self.zk.get_children("%s/decoders" % self.zk_base,
                                           watch=self.update_decoders)
        for did in decoder_ids:
            data = self.zk.get('%s/decoders/%s' % (self.zk_base, did))
            config = json.loads(str(data.decode('utf8')))
            self.setup_decoder(did, config)

        # At this point we've got a bunch of processes representing our
        # configured encoders and decoders, all started.
        self.monitor_processes()

    def monitor_processes(self):
        logging.warn("Would now start monitoring running encoders/decoders.")
        logging.fatal("But I'm not going to because it's not implemented yet.")
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

    def add_encoder(self, eid, config):
        logging.warn("Adding encoder with ID %s and config %s" % (eid, str(config)))

    def delete_encoder(self, eid):
        logging.warn("Deleting encoder with ID %s" % eid)

    def set_status_data(self):
        # thisproc = psutil.Process(os.getpid())
        status = {
            'pid': os.getpid(),
            'hostname': socket.gethostname(),
            'network_addrs': psutil.net_if_addrs(),
            # 'host_cpu': psutil.cpu_percent(interval=1.0),
            # 'self_cpu': thisproc.cpu_percent(interval=1.0),
            'report_at': time.time(),
            }
        status_data = json.dumps(dict(status)).encode('utf8')
        self.zk.ensure_path("%s/status" % self.zk_base)
        self.zk.set("%s/status" % self.zk_base, status_data)

    def shutdown(self):
        """Shut down the node gracefully"""
        self.zk.stop()

    def zk_state_change_handler(self, state):
        if state == KazooState.LOST:
            logging.info('No connection to Zookeeper')
        elif state == KazooState.SUSPENDED:
            logging.warn('Connection to Zookeeper failed')
        else:
            logging.info('Connection established to Zookeeper')
