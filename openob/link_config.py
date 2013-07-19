import redis
class LinkConfig(object):
  '''OpenOB LinkConfig object. Abstracts all the relevant configuration parameters for an OpenOB link, setting/retrieving etc.'''
  def __init__(self, link_name):
    """
    Set up a new link config object.
    """
    self.link_name = link_name
    self.logger = logging.getLogger('openob.link_config.%s' % self.link_name)
    # Set up redis and connect
    config = None
    while True:
      try:
        config = redis.Redis(opts.config_host)
        break
      except Exception, e:
        time.sleep(0.5)

  def get(self, key, block=False):
    """
    Get a configuration value
    """
    pass

  def set(self, key, value):
    """
    Set a configuration value
    """
    pass
