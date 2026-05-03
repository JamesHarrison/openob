"""
One-shot helper that prints the v6 TOML equivalent of a v5 link's
Redis-stored configuration. Used by the ``openob migrate-v5`` subcommand.

v5 stored each link's config as ``openob:<link_name>:<field>`` keys in a
shared Redis instance. This helper reads those, maps them to the v6 field
names, and prints a TOML document the user can drop into
``~/.openob/links/<link_name>.toml``.

The ``redis`` package is imported lazily so OpenOB v6 doesn't require it
as a runtime dependency — only the user actually performing a migration
needs to ``pip install redis`` (or run from a v5 environment).
"""
from __future__ import annotations

import logging
import re
import sys
from typing import IO


# v5 LinkConfig int / bool field names (from openob/link_config.py in v5).
_V5_INT_FIELDS = {
    'port', 'jitter_buffer', 'opus_framesize', 'opus_complexity',
    'bitrate', 'opus_loss_expectation',
}
_V5_BOOL_FIELDS = {'opus_dtx', 'opus_fec', 'opus_qext', 'multicast'}

# Map v5 Redis field name → v6 TOML key. Most are identical; we rename
# input_samplerate → samplerate to match the v6 CLI flag.
_V5_TO_V6 = {
    'name': None,                  # v6 uses the file basename
    'port': 'port',
    'jitter_buffer': 'jitter_buffer',
    'encoding': 'encoding',
    'bitrate': 'bitrate',
    'multicast': 'multicast',
    'input_samplerate': 'samplerate',
    'receiver_host': 'receiver_host',
    'opus_framesize': 'opus_framesize',
    'opus_complexity': 'opus_complexity',
    'opus_fec': 'opus_fec',
    'opus_loss_expectation': 'opus_loss_expectation',
    'opus_dtx': 'opus_dtx',
    'opus_qext': 'opus_qext',
    # 'caps' is intentionally dropped — v6 derives or signals it at runtime.
}


def _parse_redis_host(redis_host: str) -> tuple[str, int]:
    """Same parsing logic as v5 LinkConfig._parse_redis_host but standalone
    so we don't have to import the v5 codebase here."""
    if redis_host.startswith('['):
        end = redis_host.find(']')
        if end == -1:
            raise ValueError(f'unclosed bracket in redis_host {redis_host!r}')
        host = redis_host[1:end]
        suffix = redis_host[end + 1:]
        if not suffix:
            return host, 6379
        if not suffix.startswith(':'):
            raise ValueError(f'unexpected text after ] in redis_host {redis_host!r}')
        return host, int(suffix[1:])
    if redis_host.count(':') > 1:  # bare IPv6
        return redis_host, 6379
    if ':' in redis_host:
        host, _, port = redis_host.rpartition(':')
        return host, int(port)
    return redis_host, 6379


def _coerce_v5_value(field: str, raw: str) -> object:
    if field in _V5_INT_FIELDS:
        return int(raw)
    if field in _V5_BOOL_FIELDS:
        return raw == '1'
    return raw


def _toml_value(value: object) -> str:
    """Best-effort TOML literal for the simple types we hand it."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        # Conservative: always quote, escape backslashes and quotes.
        escaped = value.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    raise TypeError(f'unsupported TOML value type: {type(value).__name__}')


def dump_v5_link_as_toml(redis_client, link_name: str, out: IO[str]) -> bool:
    """Render the v6 TOML for a single link from a v5 Redis instance.

    Returns True if any v5 keys were found for the link, False otherwise.
    """
    prefix = f'openob:{link_name}:'
    settings: dict[str, object] = {}
    for v5_field, v6_key in _V5_TO_V6.items():
        raw = redis_client.get(prefix + v5_field)
        if raw is None:
            continue
        value = _coerce_v5_value(v5_field, raw)
        if v6_key is None:
            continue
        settings[v6_key] = value

    if not settings:
        return False

    out.write(f'# v6 config migrated from v5 Redis link {link_name!r}\n')
    out.write(f'# Save as ~/.openob/links/{link_name}.toml\n\n')
    for k, v in settings.items():
        out.write(f'{k} = {_toml_value(v)}\n')
    out.write('\n')
    out.write('# v5 stored no security config; default is cleartext RTP.\n')
    out.write('# To enable SRTP, set:\n')
    out.write('#   security = "psk"\n')
    out.write('#   psk = "your-shared-secret"\n')
    out.write('\n')
    out.write('# v5 stored no audio-interface config — that lived in CLI args.\n')
    out.write('# Add a [tx] or [rx] section here, or pass --audio-input/--audio-output on the CLI.\n')
    return True


def dump_v5_links_as_toml(
    redis_host: str,
    link_names: list[str],
    out: IO[str],
    logger: logging.Logger,
) -> None:
    try:
        import redis as redis_pkg
    except ImportError:
        logger.error(
            'migrate-v5 requires the redis package. Install with: pip install "redis>=3"'
        )
        sys.exit(1)

    host, port = _parse_redis_host(redis_host)
    logger.info('Connecting to v5 Redis at %s:%d', host, port)
    client = redis_pkg.Redis(host=host, port=port, decode_responses=True)
    try:
        client.ping()
    except Exception as exc:
        logger.error('Cannot reach Redis at %s:%d: %s', host, port, exc)
        sys.exit(1)

    found_any = False
    for i, link_name in enumerate(link_names):
        if i > 0:
            out.write('# ' + '=' * 70 + '\n\n')
        if not _is_safe_link_name(link_name):
            logger.warning(
                'skipping suspicious link name %r (only [A-Za-z0-9._-] allowed)',
                link_name,
            )
            continue
        if not dump_v5_link_as_toml(client, link_name, out):
            logger.warning('no v5 keys found for link %r at %s:%d', link_name, host, port)
            continue
        found_any = True

    if not found_any:
        sys.exit(1)


_LINK_NAME_RE = re.compile(r'^[A-Za-z0-9._-]+$')


def _is_safe_link_name(name: str) -> bool:
    return bool(name) and bool(_LINK_NAME_RE.match(name))
