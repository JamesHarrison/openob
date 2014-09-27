from openob.logger import LoggerFactory


class AudioInterface(object):

    """
        The AudioInterface class describes an audio interface on a Node.
        The configuration is not shared across the network. The type property of
        an AudioInterface should define the mode of link operation.
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

        if opts.mode == "tx":
            self.set("type", opts.audio_input)
            self.set("samplerate", opts.samplerate)
        elif opts.mode == "rx":
            self.set("type", opts.audio_output)
        if self.get("type") == "alsa":
            self.set("alsa_device", opts.alsa_device)
        elif self.get("type") == "jack":
            self.set("jack_auto", opts.jack_auto)
            if opts.jack_name is not None:
                self.set("jack_name", opts.jack_name)
            else:
                self.set("jack_name", "openob")
