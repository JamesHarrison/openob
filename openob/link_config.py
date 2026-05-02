from __future__ import annotations

import time
from typing import Any

import redis

from openob.logger import LoggerFactory

DEFAULT_REDIS_PORT = 6379


def _parse_redis_host(redis_host: str) -> tuple[str, int]:
    """
    Parse a redis_host string into (host, port). Accepts:

    - bare hostname or IPv4: ``"redis.example.com"`` or ``"127.0.0.1"`` →
      default port
    - hostname/IPv4 with port: ``"redis.example.com:6380"`` or ``"127.0.0.1:6380"``
    - bare IPv6 literal: ``"::1"`` or ``"fe80::1"`` → default port
      (heuristic: any unbracketed string with more than one ``:`` is IPv6)
    - bracketed IPv6, optionally with port: ``"[::1]"`` or ``"[::1]:6380"``

    Raises ``ValueError`` for malformed input (unclosed bracket, junk after
    the closing bracket, non-integer port).
    """
    # Bracketed form: [host] or [host]:port
    if redis_host.startswith('['):
        end = redis_host.find(']')
        if end == -1:
            raise ValueError(
                "redis_host %r starts with '[' but has no closing ']'" % redis_host
            )
        host = redis_host[1:end]
        suffix = redis_host[end + 1:]
        if suffix == "":
            return host, DEFAULT_REDIS_PORT
        if not suffix.startswith(':'):
            raise ValueError(
                "redis_host %r has unexpected text after ']': %r" % (redis_host, suffix)
            )
        try:
            port = int(suffix[1:])
        except ValueError:
            raise ValueError(
                "redis_host %r has a non-integer port" % redis_host
            ) from None
        return host, port

    # Unbracketed multi-colon: assume it's a bare IPv6 literal at the default port.
    if redis_host.count(':') > 1:
        return redis_host, DEFAULT_REDIS_PORT

    # Single colon: host:port (IPv4 / hostname).
    if ':' in redis_host:
        host, _, port_str = redis_host.rpartition(':')
        try:
            port = int(port_str)
        except ValueError:
            raise ValueError(
                "redis_host %r has a non-integer port" % redis_host
            ) from None
        return host, port

    # Bare hostname / IPv4 / unbracketed simple form.
    return redis_host, DEFAULT_REDIS_PORT


class LinkConfig:
    """
        The LinkConfig class encapsulates link configuration. It's genderless;
        a TX node should be able to set up a new link and an RX node should be
        able (once the TX node has specified the port caps) to configure itself
        to receive the stream using the data and methods in this config.
    """

    int_properties: tuple[str, ...] = (
        'port', 'jitter_buffer', 'opus_framesize', 'opus_complexity',
        'bitrate', 'opus_loss_expectation',
    )
    bool_properties: tuple[str, ...] = ('opus_dtx', 'opus_fec', 'opus_qext', 'multicast')

    def __init__(self, link_name: str, redis_host: str) -> None:
        """
            Set up a new LinkConfig instance - needs to know the link name and
            configuration host. `redis_host` accepts:

            - bare hostname or IPv4 (default port 6379)
            - ``host:port`` for non-default ports
            - bare IPv6 literal (default port), e.g. ``::1`` or ``fe80::1``
            - bracketed IPv6 with port, e.g. ``[::1]:6380``
        """
        self.link_name = link_name
        self.redis_host = redis_host
        host_part, redis_port = _parse_redis_host(redis_host)
        self.logger_factory = LoggerFactory()
        self.logger = self.logger_factory.getLogger('link.%s.config' % self.link_name)
        self.logger.info("Connecting to configuration host %s:%d" % (host_part, redis_port))
        self.redis: redis.Redis = redis.Redis(
            host=host_part, port=redis_port, decode_responses=True,
        )
        # Retry until Redis answers ping. Use exponential backoff capped at 5s
        # and rate-limit the warning log (first failure + every 10th attempt)
        # so a long outage doesn't fill the log with one line per 100ms.
        attempts = 0
        backoff = 0.1
        while True:
            try:
                self.redis.ping()
                if attempts == 0:
                    self.logger.info("Connected to Redis at %s:%d", host_part, redis_port)
                else:
                    self.logger.info(
                        "Connected to Redis at %s:%d after %d retries",
                        host_part, redis_port, attempts,
                    )
                break
            except Exception as e:
                attempts += 1
                if attempts == 1 or attempts % 10 == 0:
                    self.logger.warning(
                        "Redis ping failed (attempt %d): %s; retrying in %.1fs",
                        attempts, e, backoff,
                    )
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 5.0)

    def blocking_get(self, key: str) -> Any:
        """Get a value, blocking until it's not None if needed"""
        while True:
            value = self.get(key)
            if value is not None:
                self.logger.debug("Fetched (blocking) %s, got %s" % (key, value))
                return value
            time.sleep(0.1)

    def set(self, key: str, value: Any) -> Any:
        """Set a value in the config store"""
        scoped_key = self.scoped_key(key)

        if key in self.bool_properties:
            value = '1' if value else '0'

        self.redis.set(scoped_key, value)
        self.logger.debug("Set %s to %s" % (scoped_key, value))
        return value

    def get(self, key: str) -> Any:
        """Get a value from the config store"""
        scoped_key = self.scoped_key(key)
        value = self.redis.get(scoped_key)

        if value is None:
            self.logger.debug("Fetched %s, got None" % scoped_key)
            return None

        if key in self.int_properties:
            value = int(value)
        elif key in self.bool_properties:
            value = (value == '1')
        self.logger.debug("Fetched %s, got %s" % (scoped_key, value))
        return value

    def unset(self, key: str) -> None:
        scoped_key = self.scoped_key(key)
        self.redis.delete(scoped_key)
        self.logger.debug("Unset %s" % scoped_key)

    def __getattr__(self, key: str) -> Any:
        """Convenience method to access get"""
        return self.get(key)

    def scoped_key(self, key: str) -> str:
        """Return an appropriate key name scoped to a link"""
        return "openob:%s:%s" % (self.link_name, key)

    def set_from_argparse(self, opts) -> None:
        """Given an argparse Namespace from openob.cli, configure this link"""
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
            self.set("opus_qext", opts.qext)

    def commit_changes(self, restart: bool = False) -> None:
        """
            To be called after calls to set() on a running link to signal
            a reconfiguration event for that link. If restart is True, the link
            should simply terminate itself so it can be restarted with the new
            parameters. If restart is False, the link should set all parameters
            it can which do not involve a restart.
        """
        raise NotImplementedError("Link reconfiguration is not yet implemented")
