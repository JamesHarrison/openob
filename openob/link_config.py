"""
Link configuration for OpenOB v6.

Replaces the v5 Redis-backed ``LinkConfig`` with a frozen dataclass populated
from a per-link TOML file plus optional CLI overrides. No Redis, no shared
state at runtime — each side reads its own config file at startup; the only
runtime cross-side communication is the RTP+RTCP stream itself.

Layout::

    ~/.openob/
    ├── secrets.toml          # optional, [defaults] table for root_psk etc.
    └── links/
        ├── flypack-1.toml    # one file per link, per machine
        ├── flypack-2.toml
        └── stl-main.toml

Loader precedence (lowest to highest)::

    dataclass defaults
    → OPENOB_PSK / OPENOB_ROOT_PSK environment vars (fallback if PSK unset)
    → ~/.openob/secrets.toml [defaults]
    → ~/.openob/links/<link_name>.toml top-level
    → CLI overrides

Envvar substitution (``"${OPENOB_PSK_FOO}"`` → ``os.environ["OPENOB_PSK_FOO"]``)
is applied to ``psk`` and ``root_psk`` so secrets can live outside the file
when ops uses systemd-creds, ``pass``, or a sealed-secrets workflow.

The TOML is FLAT — every key lives at the top level. Each TOML file
represents one machine's view of one link, so there's no need to split
``[tx]`` and ``[rx]`` sections (each end has its own file). Audio I/O
fields (``audio_input``, ``audio_output``, ``alsa_device``, …) are loaded
by :mod:`openob.audio_interface` from the same flat dict.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Literal, Mapping

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


DEFAULT_CONFIG_DIR = Path.home() / '.openob'
DEFAULT_LINKS_SUBDIR = 'links'
DEFAULT_SECRETS_FILENAME = 'secrets.toml'

_ENV_SUBSTITUTABLE_FIELDS = ('psk', 'root_psk')
_ENV_PATTERN = re.compile(r'^\$\{([A-Z_][A-Z0-9_]*)\}$')


class ConfigError(ValueError):
    """Raised when link configuration is missing, inconsistent, or unreadable."""


@dataclass(frozen=True)
class LinkConfig:
    """Resolved configuration for a single OpenOB link.

    Constructed by :func:`load_link_config` after layering defaults, the
    optional secrets file, the per-link TOML, and CLI overrides. Both ends
    of a link must arrive at matching values for the *shared* fields
    (encoding, samplerate, bitrate, port, security, …) for audio to flow.

    Per-side fields (``mode``, ``caps_from_config``) differ by side.

    The dataclass is frozen so accidental mutation in pipeline-construction
    code surfaces as an :class:`AttributeError` rather than silently
    desynchronising state.
    """

    # --- identity / mode ---
    name: str
    mode: Literal['tx', 'rx']

    # --- transport ---
    port: int = 3000
    receiver_host: str | None = None  # required for TX; multicast group on RX if multicast=True
    multicast: bool = False
    jitter_buffer: int = 40

    # --- codec ---
    encoding: Literal['pcm', 'opus'] = 'opus'
    samplerate: int = 0           # 0 = let GStreamer negotiate; pin via the CLI/TOML
    channels: int = 0             # 0 = negotiate; 1=mono, 2=stereo
    bitrate: int = 128            # kbps; opus only

    # --- opus extras ---
    opus_framesize: int = 20
    opus_complexity: int = 9
    opus_fec: bool = True
    opus_dtx: bool = False
    opus_qext: bool = False
    opus_loss_expectation: int = 0

    # --- security (v6) ---
    security: Literal['none', 'psk'] = 'none'
    psk: str | None = None
    root_psk: str | None = None

    # --- caps signalling (v6) ---
    caps_from_config: bool = False  # if True, RX skips the in-band wait and derives caps from this config

    # --- packet-loss recovery (v6.1+) ---
    # 0 disables RTX entirely; >0 enables rtprtxsend (TX) + signals RX via the
    # in-band caps to flip its jitter-buffer's do-retransmission. The value is
    # rtprtxsend's max-size-time in milliseconds — i.e. how long TX keeps each
    # sent packet around for resend on NACK. 200 ms is a sensible STL default.
    retransmission_time_limit: int = 0
    # RX-side override for caps_from_config + RTX: where to send RTCP NACKs
    # when there's no in-band caps signal to learn the peer address from.
    # Ignored on the standard in-band path (peer is sniffed from recvfrom).
    tx_host: str | None = None

    def derived_rtp_caps(self) -> str:
        """Build a deterministic caps string from the codec parameters.

        Used by the ``--caps-from-config`` fallback path on RX, and by tests
        that want to know what the caps "should" be without spinning up the
        full pipeline. Type tags (``(string)``, ``(int)``) make the string
        stable across GStreamer versions.

        For ``security='psk'`` the media type is ``application/x-srtp`` so
        srtpdec can consume the udpsrc directly; for ``security='none'``
        it's the usual ``application/x-rtp``.
        """
        media_type = (
            'application/x-srtp' if self.security == 'psk'
            else 'application/x-rtp'
        )
        if self.encoding == 'opus':
            encoding_name = 'OPUS'
            clock_rate = 48000  # libopus is always 48 kHz internally
        elif self.encoding == 'pcm':
            encoding_name = 'L16'
            if self.samplerate <= 0:
                raise ConfigError(
                    "encoding=pcm + caps_from_config requires an explicit "
                    "samplerate (cannot use 'negotiate' here)"
                )
            clock_rate = self.samplerate
        else:
            raise ConfigError(f'unknown encoding: {self.encoding!r}')

        parts = [
            media_type,
            'media=(string)audio',
            f'encoding-name=(string){encoding_name}',
            f'clock-rate=(int){clock_rate}',
            'payload=(int)96',  # dynamic PT range; matches our payloader default
        ]
        if self.channels > 0:
            parts.append(f'channels=(int){self.channels}')
        return ','.join(parts)


# Caps-string fields to strip before broadcasting via the in-band caps
# signal. srtpenc embeds the master key in its output caps so a same-process
# srtpdec can pick it up — we MUST NOT ship those over the wire.
_CAPS_SECRET_FIELDS = {
    'srtp-key', 'srtcp-key', 'srtp-mki', 'srtcp-mki', 'mki',
    'srtp-cipher', 'srtcp-cipher', 'srtp-auth', 'srtcp-auth',
    # Per-session counters / sync state — receiver derives its own.
    'roc',
}


def sanitize_caps_for_wire(caps_str: str) -> str:
    """Strip secret-bearing fields from a caps string before broadcast.

    SRTP key material lives in the ``srtp-key`` caps field (and friends);
    leaking it over the in-band caps signal would defeat the encryption.
    The receiver derives its own key from the shared PSK, so we don't need
    to send the key — just the codec parameters.
    """
    parts = caps_str.split(',')
    keep: list[str] = []
    for p in parts:
        field = p.split('=', 1)[0].strip().lower()
        if field in _CAPS_SECRET_FIELDS:
            continue
        keep.append(p)
    return ','.join(keep)


# --------------------------------------------------------------- TOML loader


def _resolve_envvar(value: Any) -> Any:
    """Replace ``"${ENVVAR}"`` strings with the env value; pass anything
    else through unchanged. Raises :class:`ConfigError` if the envvar is
    referenced but not set in the environment.
    """
    if not isinstance(value, str):
        return value
    m = _ENV_PATTERN.match(value)
    if not m:
        return value
    name = m.group(1)
    if name not in os.environ:
        raise ConfigError(
            f'config references envvar {name!r} but it is not set'
        )
    return os.environ[name]


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open('rb') as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f'failed to parse {path}: {exc}') from exc
    except OSError as exc:
        raise ConfigError(f'failed to read {path}: {exc}') from exc


def _layer(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Shallow merge: keys in ``overlay`` overwrite ``base``. Nested tables
    (``[tx]``, ``[rx]``) are also merged shallowly per-section so a CLI
    flag can override one TOML key without wiping the rest of the section.
    """
    result = dict(base)
    for k, v in overlay.items():
        if isinstance(v, Mapping) and isinstance(result.get(k), Mapping):
            result[k] = _layer(result[k], v)
        else:
            result[k] = v
    return result


def resolve_link_settings(
    link_name: str,
    *,
    config_dir: Path | str | None = None,
    config_file: Path | str | None = None,
    secrets_file: Path | str | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the layered config for a link, returning the merged dict.

    Performs envvar substitution on PSK fields and applies the
    ``OPENOB_PSK`` / ``OPENOB_ROOT_PSK`` env-var fallback (only used when
    no other layer provided a PSK). Used by both :func:`load_link_config`
    and :func:`openob.audio_interface.load_audio_interface`, so we read
    the TOML once.

    The merged dict has all link-level + audio-side keys at the top level
    (the v6 TOML is flat — no ``[tx]``/``[rx]`` subsections).

    Config dir precedence: explicit ``config_dir`` argument (typically the
    ``--config-dir`` CLI flag) > ``OPENOB_CONFIG_DIR`` env var > default
    ``~/.openob``.
    """
    if config_dir is not None:
        base_dir = Path(config_dir)
    elif os.environ.get('OPENOB_CONFIG_DIR'):
        base_dir = Path(os.environ['OPENOB_CONFIG_DIR'])
    else:
        base_dir = DEFAULT_CONFIG_DIR

    # --- secrets file (optional, top-level [defaults] only) ---
    if secrets_file is not None:
        secrets_path = Path(secrets_file)
    else:
        secrets_path = base_dir / DEFAULT_SECRETS_FILENAME
    secrets_doc = _read_toml(secrets_path)
    secrets_defaults = secrets_doc.get('defaults', {})
    if not isinstance(secrets_defaults, Mapping):
        raise ConfigError(
            f'{secrets_path}: [defaults] must be a table, got {type(secrets_defaults).__name__}'
        )

    # --- per-link file ---
    if config_file is not None:
        link_path = Path(config_file)
    else:
        link_path = base_dir / DEFAULT_LINKS_SUBDIR / f'{link_name}.toml'
    link_doc = _read_toml(link_path)

    # Reject legacy [tx] / [rx] subsections with a helpful migration message.
    for subsection in ('tx', 'rx'):
        if isinstance(link_doc.get(subsection), Mapping):
            raise ConfigError(
                f'{link_path}: v6 flattened the TOML — drop the [{subsection}] '
                f'subsection and move its keys to the top level. Each TOML '
                f'represents one machine\'s view of one link, so there\'s no '
                f'need for per-mode subsections.'
            )

    # --- layer: secrets defaults < link file < CLI ---
    merged: dict[str, Any] = {}
    merged = _layer(merged, secrets_defaults)
    merged = _layer(merged, link_doc)
    if cli_overrides:
        merged = _layer(merged, {k: v for k, v in cli_overrides.items() if v is not None})

    # --- envvar substitution on PSK fields (TOML "${OPENOB_PSK_X}" form) ---
    for field_name in _ENV_SUBSTITUTABLE_FIELDS:
        if field_name in merged:
            merged[field_name] = _resolve_envvar(merged[field_name])

    # --- env-var FALLBACK: only used if nothing above provided a PSK.
    # Lets `openob tx mylink --security psk` work without a TOML when the
    # operator has set OPENOB_PSK in the environment (typical for systemd
    # via EnvironmentFile or LoadCredentialEncrypted). Stays out of CLI
    # args entirely so ps / shell history don't capture it.
    if 'psk' not in merged and os.environ.get('OPENOB_PSK'):
        merged['psk'] = os.environ['OPENOB_PSK']
    if 'root_psk' not in merged and os.environ.get('OPENOB_ROOT_PSK'):
        merged['root_psk'] = os.environ['OPENOB_ROOT_PSK']

    return merged


def load_link_config(
    link_name: str,
    mode: Literal['tx', 'rx'],
    *,
    cli_overrides: Mapping[str, Any] | None = None,
    config_dir: Path | str | None = None,
    config_file: Path | str | None = None,
    secrets_file: Path | str | None = None,
) -> LinkConfig:
    """Build a :class:`LinkConfig` from the layered TOML + CLI sources.

    See module docstring for the file layout and precedence.
    """
    merged = resolve_link_settings(
        link_name,
        config_dir=config_dir,
        config_file=config_file,
        secrets_file=secrets_file,
        cli_overrides=cli_overrides,
    )
    return link_config_from_resolved(link_name, mode, merged)


def link_config_from_resolved(
    link_name: str,
    mode: Literal['tx', 'rx'],
    merged: Mapping[str, Any],
) -> LinkConfig:
    """Build a :class:`LinkConfig` from an already-resolved settings dict.

    Lets callers share a single :func:`resolve_link_settings` call across
    LinkConfig + AudioInterface construction.
    """
    valid_fields = {f.name for f in fields(LinkConfig)} - {'name', 'mode'}
    # Pick only the link-level keys; AudioInterface fields share the
    # top-level namespace in v6 and are validated separately.
    link_only = {k: v for k, v in merged.items() if k in valid_fields}

    # Reject keys we don't recognise from EITHER namespace — typos here
    # silently wreck a deployment otherwise.
    unknown = set(merged) - valid_fields - _AUDIO_INTERFACE_FIELDS
    if unknown:
        raise ConfigError(
            f'unknown link-config keys for {link_name!r}: {sorted(unknown)}. '
            f'Valid keys: {sorted(valid_fields | _AUDIO_INTERFACE_FIELDS)}'
        )

    try:
        cfg = LinkConfig(name=link_name, mode=mode, **link_only)
    except TypeError as exc:
        raise ConfigError(
            f'failed to build LinkConfig for {link_name!r}: {exc}'
        ) from exc

    _validate(cfg)
    return cfg


# Audio-interface fields that share the top-level TOML namespace. Listed
# here (rather than imported from openob.audio_interface) so this module
# stays a pure leaf — audio_interface.py imports from us, not vice versa.
_AUDIO_INTERFACE_FIELDS = frozenset({
    'audio_input', 'audio_output',
    'alsa_device', 'jack_name', 'jack_auto', 'jack_port_pattern',
    'test_wave', 'wav_file',
})


def _validate(cfg: LinkConfig) -> None:
    """Sanity-check resolved config. Raises :class:`ConfigError` on issues."""
    if cfg.security not in ('none', 'psk'):
        raise ConfigError(
            f"security must be 'none' or 'psk', got {cfg.security!r}"
        )
    if cfg.security == 'psk' and not (cfg.psk or cfg.root_psk):
        raise ConfigError(
            "security='psk' requires either 'psk' or 'root_psk' to be set "
            "(per-link in this file, or root_psk via [defaults] in "
            "~/.openob/secrets.toml)"
        )
    if cfg.encoding not in ('pcm', 'opus'):
        raise ConfigError(
            f"encoding must be 'pcm' or 'opus', got {cfg.encoding!r}"
        )
    if cfg.mode == 'tx' and not cfg.receiver_host:
        raise ConfigError(
            "TX mode requires receiver_host to be set "
            "(in the link config or via --receiver-host)"
        )
    if cfg.mode == 'rx' and cfg.multicast and not cfg.receiver_host:
        raise ConfigError(
            "RX in multicast mode requires receiver_host (the multicast "
            "group address) to be set"
        )
    if not 1 <= cfg.port <= 65535:
        raise ConfigError(f"port must be 1..65535, got {cfg.port}")
    if cfg.encoding == 'pcm' and cfg.samplerate <= 0 and cfg.caps_from_config:
        raise ConfigError(
            "encoding=pcm + caps_from_config requires an explicit samplerate"
        )
    if cfg.retransmission_time_limit < 0:
        raise ConfigError(
            f"retransmission_time_limit must be >= 0 (ms), "
            f"got {cfg.retransmission_time_limit}"
        )
