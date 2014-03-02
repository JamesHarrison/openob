import logging


class LoggerFactory(object):
    _isSetup = False

    def __init__(self, level=logging.DEBUG):
        # Set up the top level logger ONCE
        if LoggerFactory._isSetup is False:
            logger = logging.getLogger("openob")
            logger.setLevel(level)
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            ch = logging.StreamHandler()
            ch.setLevel(level)
            ch.setFormatter(formatter)
            logger.addHandler(ch)
            LoggerFactory._isSetup = True

    def getLogger(self, name, level=logging.DEBUG):
        logger = logging.getLogger("openob.%s" % name)
        logger.setLevel(level)
        return logger
