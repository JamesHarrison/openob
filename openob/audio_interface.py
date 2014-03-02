from openob.logger import LoggerFactory


class AudioInterface(object):

    """
        The LinkConfig class encapsulates link configuration. It's genderless;
        a TX node should be able to set up a new link and an RX node should be
        able (once the TX node has specified the port caps) to configure itself
        to receive the stream using the data and methods in this config.
    """

    def __init__(self, node_name, interface_name='default'):
        self.interface_name = interface_name
        self.node_name = node_name
        self.logger_factory = LoggerFactory()
        self.logger = self.logger_factory.getLogger('node.%s.audio_interface.%s' 
                                                    % (self.node_name, self.interface_name))
        self.config = dict()

    def set(self, key, value):
        """Set a config value"""
        self.logger.debug("Set %s to %s" % (key, value))
        self.config[key] = value

    def get(self, key):
        """Get a config value"""
        value = self.config[key]
        self.logger.debug("Fetched %s, got %s" % (key, value))
        return value

    def __getattr__(self, key):
        """Convenience method to access get"""
        return self.get(key)

    def set_from_argparse(self, opts):
        """Set up the audio interface from argparse options"""
        self.set("mode", opts.mode)
        self.set("type", opts.audio_input)
        self.set("samplerate", opts.samplerate)
        if opts.audio_input == 'alsa':
            self.set("device", opts.device)
        elif opts.audio_input == 'jack':
            if opts.jack_auto is not False:
                self.set("jack_auto", opts.jack_auto)
            if opts.jack_name is not None:
                self.set("jack_name", opts.jack_name)
            else:
                self.set("jack_name", "openob")
