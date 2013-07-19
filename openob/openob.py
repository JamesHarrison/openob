
import logging
from link_manager import LinkManager
class OpenOB(object):
    """The base OpenOB class

    Provides basic interfaces onto shared objects and manages logging setup
    """
    def __init__(self):
        self.logger = logging.getLogger('openob')
        self.logger.setLevel(logging.DEBUG)
        fh = logging.FileHandler('debug.log')
        ch = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        ch.setFormatter(formatter)
        fh.setLevel(logging.DEBUG)
        ch.setLevel(logging.DEBUG)
        self.logger.addHandler(fh)
        self.logger.addHandler(ch)
        self.logger.info("OpenOB starting up")

    def link_manager(self):
        self.logger.info("Setting up new link manager")
        return LinkManager()
