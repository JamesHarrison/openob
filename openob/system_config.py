import logging
from kazoo.client import KazooClient, KazooState
from node_config import NodeConfig


class SystemConfig(object):
    """SystemConfig holds interfaces relating to the configuration store.

    It should be used to interact with the config store, and to get a
    NodeConfig. The SystemConfig class holds the Zookeeper connection.
    """
    def __init__(self, config_hosts):
        self.zk = KazooClient(hosts=config_hosts)
        self.zk_base = "/openob"
        self.connected = False

    def connect(self):
        if self.connected:
            return True
        self.connected = True
        self.zk.start()
        self.zk.add_listener(self.zk_state_change_handler)
        self.zk.ensure_path(self.zk_base)
        return True

    def stop(self):
        self.zk.stop()
        self.connected = False

    def node(self, node_id):
        # Ensure connected
        self.connect()
        # Set up our node config 
        return NodeConfig(self, node_id)

    def zk_state_change_handler(self, state):
        if state == KazooState.LOST:
            logging.info('No connection to Zookeeper')
        elif state == KazooState.SUSPENDED:
            logging.warn('Connection to Zookeeper failed')
        else:
            logging.info('Connection established to Zookeeper')
