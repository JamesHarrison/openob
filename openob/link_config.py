import redis
import time
from openob.logger import LoggerFactory


class LinkConfig(object):

    """
        The LinkConfig class encapsulates link configuration. It's genderless;
        a TX node should be able to set up a new link and an RX node should be
        able (once the TX node has specified the port caps) to configure itself
        to receive the stream using the data and methods in this config.
    """

    def __init__(self, link_name, redis_host):
        """
            Set up a new LinkConfig instance - needs to know the link name and
            configuration host.
        """
        self.link_name = link_name
        self.redis_host = redis_host
        self.logger_factory = LoggerFactory()
        self.logger = self.logger_factory.getLogger('link.%s.config' % self.link_name)
        self.logger.info("Connecting to configuration host %s" % self.redis_host)
        self.redis = None
        while True:
            try:
                self.redis = redis.StrictRedis(self.redis_host)
                break
            except Exception as e:
                self.logger.error(
                    "Unable to connect to configuration host! Retrying. (%s)"
                    % e
                )
                time.sleep(0.1)

    def blocking_get(self, key):
        """Get a value, blocking until it's not None if needed"""
        while True:
            value = self.get(key)
            if value is not None:
                self.logger.debug("Fetched (blocking) %s, got %s" % (key, value))
                return value
            time.sleep(0.1)

    def set(self, key, value):
        """Set a value in the config store"""
        scoped_key = self.scoped_key(key)
        self.redis.set(scoped_key, value)
        self.logger.debug("Set %s to %s" % (scoped_key, value))
        return value

    def get(self, key):
        """Get a value from the config store"""
        scoped_key = self.scoped_key(key)
        value = self.redis.get(scoped_key)
        # Do some typecasting
        if key == 'port' or key == 'jitter_buffer' or key == 'opus_framesize':
            value = int(value)
        self.logger.debug("Fetched %s, got %s" % (scoped_key, value))
        return value

    def unset(self, key):
        scoped_key = self.scoped_key(key)
        self.redis.delete(scoped_key)
        self.logger.debug("Unset %s" % scoped_key)

    def __getattr__(self, key):
        """Convenience method to access get"""
        return self.get(key)

    def scoped_key(self, key):
        """Return an appropriate key name scoped to a link"""
        return ("openob:%s:%s" % (self.link_name, key))

    def set_from_argparse(self, opts):
        """Given an optparse object from bin/openob, configure this link"""
        self.set("name", opts.link_name)
        if opts.mode == "tx":
            self.set("port", opts.port)
            self.set("jitter_buffer", opts.jitter_buffer)
            self.set("encoding", opts.encoding)
            self.set("bitrate", opts.bitrate)
            self.set("multicast", opts.multicast)
            self.set("input_samplerate", opts.samplerate)
            self.set("receiver_host", opts.receiver_host)
            self.set("opus_framesize", opts.framesize)
            self.set("opus_complexity", opts.complexity)
            self.set("opus_fec", opts.fec)
            self.set("opus_loss_expectation", opts.loss)
            self.set("opus_dtx", opts.dtx)

    def commit_changes(self, restart=False):
        """
            To be called after calls to set() on a running link to signal
            a reconfiguration event for that link. If restart is True, the link
            should simply terminate itself so it can be restarted with the new
            parameters. If restart is False, the link should set all parameters
            it can which do not involve a restart.
        """
        raise(NotImplementedError, "Link reconfiguration is not yet implemented")
