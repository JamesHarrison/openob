"""Logging setup for OpenOB.

Two formatters are available:

- ``text`` (default) — human-readable line including the link/node/role
  context where present, e.g.::

      2026-05-03 12:00:01,234 INFO [studio/stl-main/tx] openob.cli — pipeline.started

  Trailing ``key=value`` pairs are appended for any structured ``extra``
  fields the call site supplied, so ``grep`` users still see them.

- ``json`` — one JSON object per line (stdlib only — no extra dependency).
  All ``extra`` fields are promoted to top-level keys so a downstream
  aggregator (Loki, ELK, an OTel logs receiver) gets structured fields.

Code call sites use a :class:`LinkLoggerAdapter` returned by
:meth:`LoggerFactory.getLinkLogger` so every record automatically carries
``link``, ``node``, ``role``. Loggers obtained via plain :meth:`getLogger`
get safe defaults (``-``) for those fields.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Literal


_LINK_ATTRS = ('link', 'node', 'role')
# Standard LogRecord attributes — everything else on the record is
# treated as user-supplied ``extra`` and promoted in JSON output / appended
# in text output.
_RESERVED = frozenset({
    'name', 'msg', 'args', 'levelname', 'levelno', 'pathname', 'filename',
    'module', 'exc_info', 'exc_text', 'stack_info', 'lineno', 'funcName',
    'created', 'msecs', 'relativeCreated', 'thread', 'threadName',
    'processName', 'process', 'message', 'asctime', 'taskName',
})


class _TextFormatter(logging.Formatter):
    """Format: timestamp level [node/link/role] logger — message extra=...

    Pulls link/node/role via getattr-with-default so records that came
    from a plain ``logger.info(...)`` (without going through a
    LinkLoggerAdapter) format cleanly with ``-`` placeholders. We can't
    rely on the percent-style ``%(link)s`` because stdlib's
    ``Logger.makeRecord`` rejects ``extra`` keys that collide with
    pre-existing record attributes — so injecting link/node/role via a
    LogRecord factory would crash adapter call sites.
    """
    DATEFMT = '%Y-%m-%d %H:%M:%S'

    def __init__(self) -> None:
        super().__init__(datefmt=self.DATEFMT)

    def format(self, record: logging.LogRecord) -> str:
        # Build the base line manually so we don't need percent-style
        # access to link/node/role.
        ts = self.formatTime(record, self.datefmt)
        link = getattr(record, 'link', '-')
        node = getattr(record, 'node', '-')
        role = getattr(record, 'role', '-')
        base = (
            f'{ts} {record.levelname} [{node}/{link}/{role}] '
            f'{record.name} — {record.getMessage()}'
        )
        extras = _extract_extras(record)
        if extras:
            kv = ' '.join(f'{k}={_textify(v)}' for k, v in extras.items())
            base = f'{base} {kv}'
        if record.exc_info:
            base = f'{base}\n{self.formatException(record.exc_info)}'
        return base


class _JSONFormatter(logging.Formatter):
    """One JSON object per line, link/node/role + extras at top level.

    Stdlib-only on purpose — pulling in ``python-json-logger`` for ~30
    lines of formatter logic is a poor trade for the Pi audience.
    """

    def format(self, record: logging.LogRecord) -> str:
        # %f isn't portable through time.strftime (Windows rejects it),
        # so build the ISO-ish timestamp by hand: stdlib formatTime
        # without datefmt yields "YYYY-MM-DD HH:MM:SS,mmm". Replace the
        # comma-millis with dot and the space with T so the output is
        # ISO-8601-ish for log aggregators.
        ts = self.formatTime(record).replace(',', '.').replace(' ', 'T', 1)
        payload: dict[str, Any] = {
            'ts': ts,
            'level': record.levelname,
            'logger': record.name,
            'msg': record.getMessage(),
        }
        for attr in _LINK_ATTRS:
            value = getattr(record, attr, None)
            if value is not None and value != '-':
                payload[attr] = value
        # Two ways a record can carry an event tag:
        #  1. The call site passed extra={'event': 'pipeline.restart'}
        #     — explicit and preferred.
        #  2. The message itself looks like a dotted-lowercase event tag
        #     (covers call sites that just did logger.info('foo.bar')).
        # Either way the JSON formatter promotes it to a top-level
        # `event` field that aggregators key on for grouping.
        for k, v in _extract_extras(record).items():
            payload[k] = _jsonify(v)
        if 'event' not in payload and _looks_like_event(payload['msg']):
            payload['event'] = payload['msg']
        if record.exc_info:
            payload['exc_info'] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def _extract_extras(record: logging.LogRecord) -> dict[str, Any]:
    """Pull everything off the record that isn't a stdlib field or our
    link/node/role context. ``extra={'foo': 1}`` sets ``record.foo = 1``,
    so we just enumerate ``__dict__`` and skip the known names.
    """
    extras: dict[str, Any] = {}
    for k, v in record.__dict__.items():
        if k in _RESERVED:
            continue
        if k in _LINK_ATTRS:
            continue
        if k.startswith('_'):
            continue
        extras[k] = v
    return extras


def _textify(value: Any) -> str:
    """Render a value for the trailing ``key=value`` text segment.

    Whitespace and equals signs in the value are minimally quoted so a
    downstream ``grep | awk -F=`` is still parseable.
    """
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    s = str(value)
    if any(c in s for c in (' ', '\t', '"', '=')):
        return json.dumps(s, ensure_ascii=False)
    return s


def _jsonify(value: Any) -> Any:
    """Convert a value to something json.dumps can serialise.

    Falls back to ``str()`` via the ``default`` arg in :meth:`_JSONFormatter.format`,
    but we pre-convert containers so nested data round-trips cleanly.
    """
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    return str(value)


def _looks_like_event(msg: str) -> bool:
    """Heuristic: a message string is an event tag iff it's a dotted
    lowercase identifier (e.g. ``pipeline.restart``) with no whitespace.

    Lets call sites use either ``logger.info('pipeline.restart', extra={...})``
    (event-style) or ``logger.info('Some sentence', extra={...})``
    (message-style) without ceremony.
    """
    if not msg or ' ' in msg or '\n' in msg:
        return False
    if not all(c.islower() or c == '.' or c == '_' or c.isdigit() for c in msg):
        return False
    return '.' in msg


class LinkLoggerAdapter(logging.LoggerAdapter):
    """LoggerAdapter that injects ``link``, ``node``, ``role`` into ``extra``
    on every record.

    Matches the stdlib LoggerAdapter contract: ``self.extra`` is merged
    into the per-call ``extra`` dict so call sites can still pass their
    own structured fields.
    """

    def process(self, msg, kwargs):
        # Pull out any caller-supplied extra; merge ours in beneath it
        # so explicit per-call values win (e.g. an override of ``role``
        # in a one-off log line).
        kwargs = dict(kwargs)
        merged = dict(self.extra) if self.extra else {}
        merged.update(kwargs.get('extra') or {})
        kwargs['extra'] = merged
        return msg, kwargs


class LoggerFactory:
    """Process-wide logger factory.

    The first instantiation installs the formatter + handler on the
    ``openob`` root; subsequent instantiations are no-ops aside from
    returning loggers from :meth:`getLogger`. Re-entry is safe because
    everything routes through the gated class-level setup state.
    """

    _isSetup: bool = False
    _fmt: Literal['text', 'json'] = 'text'

    def __init__(
        self,
        level: int = logging.DEBUG,
        *,
        fmt: Literal['text', 'json'] | None = None,
    ) -> None:
        if fmt is None:
            env_fmt = os.environ.get('OPENOB_LOG_FORMAT', '').lower()
            fmt = 'json' if env_fmt == 'json' else 'text'
        if LoggerFactory._isSetup is False:
            logger = logging.getLogger('openob')
            logger.setLevel(level)
            handler = logging.StreamHandler()
            handler.setLevel(level)
            handler.setFormatter(self._make_formatter(fmt))
            logger.addHandler(handler)
            LoggerFactory._isSetup = True
            LoggerFactory._fmt = fmt

    @staticmethod
    def _make_formatter(fmt: Literal['text', 'json']) -> logging.Formatter:
        if fmt == 'json':
            return _JSONFormatter()
        return _TextFormatter()

    @classmethod
    def reconfigure(
        cls,
        *,
        level: int | None = None,
        fmt: Literal['text', 'json'] | None = None,
    ) -> None:
        """Mutate the live handler in place (level + formatter).

        Used by the CLI when ``--telemetry-log-format`` / ``--telemetry-log-level``
        flags arrive AFTER the bootstrap LoggerFactory has run. Prefer this
        over re-instantiating because handlers are already wired to the
        openob hierarchy.
        """
        logger = logging.getLogger('openob')
        if level is not None:
            logger.setLevel(level)
            for h in logger.handlers:
                h.setLevel(level)
        if fmt is not None:
            for h in logger.handlers:
                h.setFormatter(cls._make_formatter(fmt))
            cls._fmt = fmt

    def getLogger(
        self, name: str, level: int = logging.DEBUG,
    ) -> logging.Logger:
        logger = logging.getLogger('openob.%s' % name)
        logger.setLevel(level)
        return logger

    def getLinkLogger(
        self,
        *,
        node: str,
        link: str,
        role: str,
        suffix: str | None = None,
        level: int = logging.DEBUG,
    ) -> LinkLoggerAdapter:
        """Return an adapter that auto-tags every record with link/node/role.

        ``suffix`` is appended to the logger name so subsystems can still
        filter by hierarchy (e.g. ``link.<name>.caps_signal.tx``) — pass
        e.g. ``suffix='caps_signal.tx'``.
        """
        base_name = f'node.{node}.link.{link}.{role}'
        if suffix:
            base_name = f'{base_name}.{suffix}'
        base = self.getLogger(base_name, level=level)
        return LinkLoggerAdapter(
            base,
            extra={'link': link, 'node': node, 'role': role},
        )
