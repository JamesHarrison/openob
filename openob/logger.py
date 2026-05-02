from __future__ import annotations

import logging


class LoggerFactory:
    _isSetup: bool = False

    def __init__(self, level: int = logging.DEBUG) -> None:
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

    def getLogger(self, name: str, level: int = logging.DEBUG) -> logging.Logger:
        logger = logging.getLogger("openob.%s" % name)
        logger.setLevel(level)
        return logger
