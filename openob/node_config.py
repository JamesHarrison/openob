import json
import psutil
import logging
import time
import socket
import os


# TODO: Implement this stuff
class NodeConfig(object):
    """NodeConfig contains the interface to the config store

    All writing and reading from the configuration server relating to a Node
    should be done through this class."""
    def __init__(self, system_config, node_id):
        self.zk_base = "/openob/nodes/%s" % self.node_id

    def setup(self):
        """Do any initial configuration required. Call before other calls."""
        self.zk.ensure_path("%s/encoders" % self.zk_base)
        self.zk.ensure_path("%s/decoders" % self.zk_base)
        self.zk.ensure_path("%s/audio_interfaces" % self.zk_base)
        self.zk.ensure_path("%s/network_interfaces" % self.zk_base)

    def add_encoder(self, eid, config):
        self.encoders[eid] = dict(config)
        logging.warn("Adding encoder with ID %s and config %s" % (eid, str(config)))

    def delete_encoder(self, eid):
        logging.warn("Deleting encoder with ID %s" % eid)

    def add_decoder(self, did, config):
        logging.warn("Adding decoder with ID %s and config %s" % (did, str(config)))

    def delete_decoder(self, did):
        logging.warn("Deleting decoder with ID %s" % did)

    def add_transmitter(self, tid, config):
        logging.warn("Adding transmitter with ID %s and config %s" % (tid, str(config)))

    def delete_transmitter(self, tid):
        logging.warn("Deleting transmitter with ID %s" % tid)

    def add_receiver(self, rid, config):
        logging.warn("Adding receiver with ID %s and config %s" % (rid, str(config)))

    def delete_receiver(self, rid):
        logging.warn("Deleting receiver with ID %s" % rid)

    def set_audio_interfaces(self, audio_interfaces):
        self.zk.delete("%s/audio_interfaces" % self.zk_base,
                       recursive=True)
        self.zk.ensure_path("%s/audio_interfaces" % self.zk_base)
        for iid, config in self.audio_interfaces.items():
            # Write out each available audio interface with its config in JSON
            path = "%s/audio_interfaces/%s" % (self.zk_base, iid)
            self.zk.create(path, json.dumps(dict(config)).encode('utf8'))

    def get_audio_interfaces(self):
        pass

    def set_status_data(self):
        # TODO: Break out network addresses on this machine as config nodes
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
