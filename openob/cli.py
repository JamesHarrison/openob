"""
OpenOB v6 command-line interface.

Three subcommands:

- ``openob tx <link_name> [opts]`` — start a transmitter.
- ``openob rx <link_name> [opts]`` — start a receiver.
- ``openob migrate-v5 <redis_host> [<link_name>...]`` — print v6 TOML
  equivalents of v5 Redis configs (one-shot migration helper).

Configuration comes from a TOML file at ``~/.openob/links/<link_name>.toml``
by default; ``--config FILE`` overrides the path. CLI flags override file
values. See :mod:`openob.link_config` for the full layering rules.

Compared to v5: no more ``<config_host>`` positional (Redis is gone), and
``<node_name>`` is now an optional ``--node-name`` (defaults to the
machine's hostname; only used for log labelling).
"""
from __future__ import annotations

import argparse
import logging
import socket
import sys
from typing import Any

from openob.audio_interface import AudioInterface
from openob.link_config import (
    ConfigError,
    link_config_from_resolved,
    resolve_link_settings,
)
from openob.logger import LoggerFactory


_OPUS_BITRATES = [
    16, 24, 32, 48, 64, 96, 128, 192, 256, 384,
    # qext / high-rate territory (Opus quality extensions, libopus 1.3+)
    512, 768, 1024, 1536, 2048,
]
_OPUS_FRAMESIZES = [2, 5, 10, 20, 40, 60]
_TEST_WAVES = [
    'sine', 'square', 'saw', 'triangle', 'silence',
    'white-noise', 'pink-noise', 'sine-table', 'ticks',
    'gaussian-noise', 'red-noise', 'blue-noise', 'violet-noise',
]


# --- argparse glue ---------------------------------------------------------


class _HelpAction(argparse._HelpAction):
    """Help action that recursively prints subparser help, like v5."""

    def __call__(self, parser, namespace, values, option_string=None):
        parser.print_help()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for choice, sub in action.choices.items():
                    print(f"\nSubparser '{choice}'")
                    print(sub.format_help())
        parser.exit()


def _add_link_options(p: argparse.ArgumentParser) -> None:
    """Options that apply to both ``tx`` and ``rx`` (link-level config)."""
    p.add_argument('--config', type=str, default=None,
                   help='Path to a specific link config TOML (overrides the default lookup)')
    p.add_argument('--config-dir', type=str, default=None,
                   help='Override the config directory '
                        '(default: $OPENOB_CONFIG_DIR or ~/.openob)')
    p.add_argument('--node-name', type=str, default=None,
                   help='Identifier for this end of the link in logs '
                        '(defaults to the machine hostname)')

    p.add_argument('--port', '-p', type=int, default=None,
                   help='UDP port for RTP+RTCP-mux audio')
    p.add_argument('--receiver-host', type=str, default=None,
                   help='[tx] destination host. [rx] multicast group address (when --multicast)')
    p.add_argument('--multicast', dest='multicast', action='store_true', default=None,
                   help='Use IP multicast')
    p.add_argument('--no-multicast', dest='multicast', action='store_false',
                   help='Use unicast (default)')
    p.add_argument('--jitter-buffer', type=int, default=None,
                   help='Jitter buffer size in milliseconds')

    p.add_argument('--encoding', '-e', type=str, choices=['pcm', 'opus'], default=None,
                   help='Audio encoding')
    p.add_argument('--samplerate', '-r', type=int, default=None,
                   help='Sample rate (Hz). 0 = let GStreamer negotiate')
    p.add_argument('--channels', '-c', type=int, default=None,
                   help='Channel count (1 mono, 2 stereo, 0 = negotiate)')
    p.add_argument('--bitrate', '-b', type=int, choices=_OPUS_BITRATES, default=None,
                   help='Opus bitrate (kbps)')

    p.add_argument('--opus-framesize', type=int, choices=_OPUS_FRAMESIZES, default=None,
                   help='Opus frame size (ms)')
    p.add_argument('--opus-complexity', type=int, choices=range(0, 11), default=None,
                   metavar='N', help='Opus computational complexity (0-10)')
    p.add_argument('--opus-loss-expectation', type=int, choices=range(0, 101),
                   default=None, metavar='PCT',
                   help='Expected packet loss percentage for Opus (0-100)')
    p.add_argument('--opus-fec', dest='opus_fec', action='store_true', default=None,
                   help='Enable Opus inband FEC (default true)')
    p.add_argument('--no-opus-fec', dest='opus_fec', action='store_false',
                   help='Disable Opus inband FEC')
    p.add_argument('--opus-dtx', dest='opus_dtx', action='store_true', default=None,
                   help='Enable Opus DTX')
    p.add_argument('--no-opus-dtx', dest='opus_dtx', action='store_false',
                   help='Disable Opus DTX (default)')
    p.add_argument('--opus-qext', dest='opus_qext', action='store_true', default=None,
                   help='Enable Opus quality extensions (libopus 1.3+, requires '
                        'opusenc with the qext property)')
    p.add_argument('--no-opus-qext', dest='opus_qext', action='store_false',
                   help='Disable Opus qext (default)')

    p.add_argument('--security', type=str, choices=['none', 'psk'], default=None,
                   help="Security mode. 'psk' enables SRTP using a PSK loaded from "
                        "the config file. PSK is never accepted on the CLI.")

    p.add_argument('--caps-from-config', dest='caps_from_config',
                   action='store_true', default=None,
                   help='[rx] derive caps from local config instead of waiting '
                        'for an in-band caps signal from TX')
    p.add_argument('--no-caps-from-config', dest='caps_from_config',
                   action='store_false',
                   help='[rx] always wait for an in-band caps signal (default)')

    p.add_argument('--retransmission-time-limit', type=int, default=None,
                   metavar='MS',
                   help='[tx] enable RTP retransmission (rtprtxsend max-size-time '
                        'in ms). 0 disables. RX picks RTX up from the in-band '
                        'caps signal — no RX config needed in the normal case')
    p.add_argument('--tx-host', type=str, default=None,
                   help='[rx] only used with --caps-from-config + RTX: the TX '
                        'address to send RTCP NACKs to (the in-band path sniffs '
                        'this automatically)')

    # --- telemetry (v6.2+) -------------------------------------------------
    # OTLP-only; off by default. Bearer tokens go in TOML / env, never on
    # the CLI (same discipline as PSK).
    p.add_argument('--telemetry', dest='telemetry_enabled',
                   action='store_true', default=None,
                   help='Enable OTLP metrics + structured event logging '
                        '(requires `pip install openob[telemetry]`)')
    p.add_argument('--no-telemetry', dest='telemetry_enabled',
                   action='store_false',
                   help='Disable telemetry (default)')
    p.add_argument('--telemetry-otlp-endpoint', type=str, default=None,
                   metavar='URL',
                   help='OTLP HTTP/protobuf endpoint, e.g. '
                        'https://otlp.example.com:4318 or '
                        'http://prom:9090/api/v1/otlp/v1/metrics')
    p.add_argument('--telemetry-otlp-interval-seconds', type=int, default=None,
                   metavar='N',
                   help='OTLP export interval (seconds, min 5; default 30)')
    p.add_argument('--telemetry-log-format', type=str,
                   choices=['text', 'json'], default=None,
                   help='Log line format. JSON promotes structured event '
                        'fields to top-level keys for log aggregators. '
                        'Also settable via OPENOB_LOG_FORMAT=json')
    p.add_argument('--telemetry-log-level', type=str,
                   choices=['debug', 'info', 'warning', 'error'], default=None,
                   help='Override log level for the telemetry-aware loggers')

    # --- hooks (v6.3+) -----------------------------------------------------
    # Lets hardware integrators wire OpenOB to a relay, LED, or dashboard
    # without standing up an OTel collector. See openob/hooks.py for the
    # JSON envelope schema.
    p.add_argument('--hook-command', type=str, default=None, metavar='CMD',
                   help='Shell command to invoke on link state changes '
                        'and (optionally) on a heartbeat. Receives event/'
                        'link/role/status/prev_status as positional argv '
                        'plus a JSON envelope on stdin')
    p.add_argument('--hook-heartbeat-seconds', type=int, default=None,
                   metavar='N',
                   help='Run the hook every N seconds with current link '
                        'state and audio levels. 0 disables heartbeats; '
                        'status changes still fire (default 0)')
    p.add_argument('--hook-timeout-seconds', type=int, default=None,
                   metavar='N',
                   help='Per-invocation timeout for the hook command in '
                        'seconds (default 5). The worker SIGKILLs the '
                        'child if it exceeds this')


def _add_tx_audio_options(p: argparse.ArgumentParser) -> None:
    p.add_argument('--audio-input', '-a', type=str,
                   choices=['auto', 'alsa', 'jack', 'pulse', 'coreaudio', 'wasapi', 'test'],
                   default=None,
                   help='Audio source type')
    p.add_argument('--alsa-device', '-d', type=str, default=None,
                   help='ALSA device (e.g. hw:0)')
    p.add_argument('--jack-name', type=str, default=None, help='JACK port name root')
    p.add_argument('--jack-auto', dest='jack_auto', action='store_true', default=None,
                   help='Enable JACK auto-connect (default)')
    p.add_argument('--no-jack-auto', dest='jack_auto', action='store_false',
                   help='Disable JACK auto-connect')
    p.add_argument('--jack-port-pattern', type=str, default=None,
                   help='JACK port pattern')
    p.add_argument('--audio-device', type=str, default=None,
                   help='Display-name substring (case-insensitive) or native '
                        'device id for coreaudio/wasapi/pulse. List candidates '
                        'with `openob list-devices`.')
    p.add_argument('--test-wave', type=str, choices=_TEST_WAVES, default=None,
                   help='audiotestsrc waveform (when --audio-input=test)')


def _add_rx_audio_options(p: argparse.ArgumentParser) -> None:
    p.add_argument('--audio-output', '-a', type=str,
                   choices=['auto', 'alsa', 'jack', 'pulse', 'coreaudio', 'wasapi', 'test', 'wav'],
                   default=None,
                   help='Audio sink type')
    p.add_argument('--alsa-device', '-d', type=str, default=None,
                   help='ALSA device (e.g. hw:0)')
    p.add_argument('--jack-name', type=str, default=None, help='JACK port name root')
    p.add_argument('--jack-auto', dest='jack_auto', action='store_true', default=None,
                   help='Enable JACK auto-connect (default)')
    p.add_argument('--no-jack-auto', dest='jack_auto', action='store_false',
                   help='Disable JACK auto-connect')
    p.add_argument('--jack-port-pattern', type=str, default=None,
                   help='JACK port pattern')
    p.add_argument('--audio-device', type=str, default=None,
                   help='Display-name substring (case-insensitive) or native '
                        'device id for coreaudio/wasapi/pulse. List candidates '
                        'with `openob list-devices`.')
    p.add_argument('--wav-file', type=str, default=None,
                   help='Output WAV path (when --audio-output=wav)')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='openob',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        add_help=False,
    )
    parser.add_argument('-v', '--verbose', action='store_const',
                        help='Increase logging verbosity to DEBUG',
                        const=logging.DEBUG, default=logging.INFO)
    parser.add_argument('-h', '--help', action=_HelpAction, help='Show help')

    subs = parser.add_subparsers(dest='mode', help='Subcommand')

    # tx
    tx = subs.add_parser('tx', formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                         help='Run a transmitter')
    tx.add_argument('link_name', type=str, help='Link name (matches both ends)')
    _add_link_options(tx)
    _add_tx_audio_options(tx)

    # rx
    rx = subs.add_parser('rx', formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                         help='Run a receiver')
    rx.add_argument('link_name', type=str, help='Link name (matches both ends)')
    _add_link_options(rx)
    _add_rx_audio_options(rx)

    # migrate-v5
    mig = subs.add_parser('migrate-v5', formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                          help='Print v6 TOML for one or more v5 Redis-stored links')
    mig.add_argument('redis_host', type=str,
                     help='v5 Redis host (e.g. studio.example.com or 127.0.0.1:6379)')
    mig.add_argument('link_names', type=str, nargs='+', help='v5 link names to migrate')

    # list-devices
    lsd = subs.add_parser('list-devices', formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                          help='List audio devices visible on this machine')
    lsd.add_argument('--backend', type=str,
                     choices=['coreaudio', 'wasapi', 'pulse', 'alsa', 'all'],
                     default='all',
                     help='Filter to one audio backend')
    lsd.add_argument('--direction', type=str,
                     choices=['source', 'sink', 'both'], default='both',
                     help='Filter to inputs (source), outputs (sink), or both')
    lsd.add_argument('--raw', action='store_true', default=False,
                     help='Tab-separated output for piping into shell scripts')

    return parser


def _link_overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """Extract LinkConfig CLI overrides from the parsed args, dropping Nones
    (so file/defaults remain in effect for unspecified flags)."""
    keys = (
        'port', 'receiver_host', 'multicast', 'jitter_buffer',
        'encoding', 'samplerate', 'channels', 'bitrate',
        'opus_framesize', 'opus_complexity', 'opus_loss_expectation',
        'opus_fec', 'opus_dtx', 'opus_qext',
        'security', 'caps_from_config',
        'retransmission_time_limit', 'tx_host',
        'telemetry_enabled', 'telemetry_otlp_endpoint',
        'telemetry_otlp_interval_seconds',
        'telemetry_log_format', 'telemetry_log_level',
        'hook_command', 'hook_heartbeat_seconds', 'hook_timeout_seconds',
    )
    return {k: getattr(args, k) for k in keys if getattr(args, k, None) is not None}


def _audio_overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """Extract audio CLI overrides as a flat dict that merges into the same
    top-level namespace as link config (v6 TOML is flat — no [tx]/[rx])."""
    keys = (
        'audio_input', 'audio_output',
        'alsa_device', 'jack_name', 'jack_auto', 'jack_port_pattern',
        'audio_device',
        'test_wave', 'wav_file',
    )
    return {k: getattr(args, k) for k in keys if getattr(args, k, None) is not None}


def _resolve_node_name(args: argparse.Namespace) -> str:
    if getattr(args, 'node_name', None):
        return args.node_name
    return socket.gethostname() or 'openob'


def _run_link(args: argparse.Namespace, logger: logging.Logger) -> None:
    """Common path for tx and rx subcommands."""
    # Single layered TOML resolution shared between LinkConfig and AudioInterface.
    link_overrides = _link_overrides_from_args(args)
    audio_overrides = _audio_overrides_from_args(args)
    overrides = dict(link_overrides)
    overrides.update(audio_overrides)  # adds the {mode: {...}} subdict

    try:
        merged = resolve_link_settings(
            args.link_name,
            config_dir=args.config_dir,
            config_file=args.config,
            cli_overrides=overrides,
        )
        link_config = link_config_from_resolved(args.link_name, args.mode, merged)
        node_name = _resolve_node_name(args)
        audio_interface = AudioInterface.from_resolved(node_name, args.mode, merged)
    except ConfigError as exc:
        logger.error('config: %s', exc)
        sys.exit(2)

    try:
        from importlib.metadata import version
        ob_version = version('OpenOB')
    except Exception:
        ob_version = 'unknown'

    # Reconfigure logging now that we know the resolved config. The first
    # LoggerFactory() in main() seeded text/env-default; this honours the
    # --telemetry-log-format CLI flag or telemetry_log_format TOML key.
    _LEVEL_MAP = {
        'debug': logging.DEBUG, 'info': logging.INFO,
        'warning': logging.WARNING, 'error': logging.ERROR,
    }
    LoggerFactory.reconfigure(
        fmt=link_config.telemetry_log_format,
        level=_LEVEL_MAP.get(link_config.telemetry_log_level)
        if link_config.telemetry_log_level else None,
    )

    logger.info(
        'OpenOB %s starting: node=%s link=%s mode=%s encoding=%s port=%d security=%s '
        'audio_%s=%s',
        ob_version, node_name, link_config.name, link_config.mode,
        link_config.encoding, link_config.port, link_config.security,
        'input' if args.mode == 'tx' else 'output', audio_interface.type,
    )

    # Defer Node import so `openob --help` doesn't pull in GStreamer.
    from openob.node import Node
    node = Node(node_name)
    node.run_link(link_config, audio_interface)


def _run_migrate(args: argparse.Namespace, logger: logging.Logger) -> None:
    from openob.migrate import dump_v5_links_as_toml
    dump_v5_links_as_toml(args.redis_host, args.link_names, sys.stdout, logger)


def _run_list_devices(args: argparse.Namespace, logger: logging.Logger) -> None:
    """List audio devices visible to Gst.DeviceMonitor on this machine.

    Deferred import so `openob --help` doesn't drag in GStreamer.
    """
    del logger  # human output goes to stdout, not the structured logger
    from openob.audio_device import list_audio_devices

    backend_filter = None if args.backend == 'all' else args.backend
    devices = list_audio_devices(backend=backend_filter, direction=args.direction)

    if args.raw:
        for d in devices:
            sys.stdout.write(
                f'{d.backend}\t{d.direction}\t{d.display_name}\t{d.native_id}\t'
                f'{"default" if d.is_default else ""}\n'
            )
        return

    # Human-readable: group by backend, direction, with copy-pasteable example
    # TOML lines for each entry.
    if not devices:
        sys.stdout.write('No audio devices visible to Gst.DeviceMonitor.\n')
        return

    by_backend: dict[str, list] = {}
    for d in devices:
        by_backend.setdefault(d.backend, []).append(d)

    for backend in ('coreaudio', 'wasapi', 'pulse', 'alsa'):
        if backend_filter is not None and backend != backend_filter:
            continue
        backend_devices = by_backend.get(backend, [])
        if not backend_devices:
            sys.stdout.write(
                f'{_BACKEND_DISPLAY[backend]}: 0 devices visible.\n\n'
            )
            continue
        sys.stdout.write(
            f'{_BACKEND_DISPLAY[backend]}: {len(backend_devices)} device(s)\n'
        )
        for d in backend_devices:
            marker = ' [default]' if d.is_default else ''
            direction_label = 'Source' if d.direction == 'source' else 'Sink  '
            sys.stdout.write(
                f'  {direction_label}{marker:11}  {d.display_name!r}  id={d.native_id}\n'
            )
            if backend != 'alsa':
                # ALSA still uses the legacy alsa_device key, not audio_device.
                sys.stdout.write(
                    f'                       audio_input/audio_output = "{backend}"\n'
                    f'                       audio_device = "{d.display_name}"\n'
                )
            else:
                sys.stdout.write(
                    f'                       audio_input/audio_output = "alsa"\n'
                    f'                       alsa_device = "{d.native_id}"\n'
                )
        sys.stdout.write('\n')


_BACKEND_DISPLAY: dict[str, str] = {
    'coreaudio': 'CoreAudio (macOS)',
    'wasapi':    'WASAPI (Windows)',
    'pulse':     'PulseAudio (Linux)',
    'alsa':      'ALSA (Linux)',
}


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, 'mode', None):
        parser.print_help()
        parser.exit(2)

    LoggerFactory(level=args.verbose)
    logger = LoggerFactory().getLogger('cli')

    if args.mode in ('tx', 'rx'):
        _run_link(args, logger)
    elif args.mode == 'migrate-v5':
        _run_migrate(args, logger)
    elif args.mode == 'list-devices':
        _run_list_devices(args, logger)
    else:  # pragma: no cover — argparse rejects others
        parser.error(f'unknown subcommand: {args.mode}')


if __name__ == '__main__':
    main()
