from kazoo.client import KazooClient, KazooState
import logging
import json
import os
import socket
import psutil
import time


class Node(object):
    """Node is the core class and entry point for an OpenOB daemon or program

    A node wraps and manages all links associated with its configuration,
    and ensures they stay running.
    """
    def __init__(self, node_id, config_hosts, audio_interfaces, log_level):
        """Set up the node"""
        logging.basicConfig(level=log_level,
                            format='%(asctime)s:%(levelname)s: %(message)s')
        self.node_id = node_id
        self.audio_interfaces = audio_interfaces
        logging.info('Starting OpenOB node ID "%s"' % self.node_id)
        self.zk = KazooClient(hosts=config_hosts)
        self.zk_base = "/openob/nodes/%s" % self.node_id

    def run(self):
        """Run the node; this method blocks and will run until interrupted"""
        self.zk.start()
        self.zk.add_listener(self.zk_state_change_handler)

        self.zk.ensure_path(self.zk_base)

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
        # look at what Links are configured and set up our connection ends
        self.update_links()

    def update_links(self):
        self.links = {}
        # TODO: This probably isn't the way to go.
        # /openob/nodes/:id/encoders/:eid/destinations/:did and same for rx
        links = self.zk.get_children("/openob/links")
        for link_path in links:
            logging.debug("Inspecting %s for node involvement")
            link_config_data = self.zk.get("link_path")
            link_config = json.loads(str(link_config_data.decode('utf8')))

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
