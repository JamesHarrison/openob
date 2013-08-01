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
        self.redis = redis.Redis(opts.config_host)
        break
      except Exception, e:
        time.sleep(0.5)

  def get(self, key, block=False):
    """Get a configuration value

    Can block on a non-existent key if desired with the block kwarg; in this mode the get will block until the key has a value (that is not False)
    """
    ns_key = "openob3:%s:%s" % (self.link_name, key)
    if block:
      val = False
      while not val:
        val = self.redis.get(ns_key)
      return val
    else:
      return self.redis.get(ns_key)
    return val

  def set(self, key, value):
    """
    Set a configuration value
    """
    ns_key = "openob3:%s:%s" % (self.link_name, key)
    self.redis.set(ns_key, value)
    pass
