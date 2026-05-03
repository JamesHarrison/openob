"""
Per-node audio I/O configuration.

Loaded from the same flat TOML as :mod:`openob.link_config`. The mode
(``tx`` or ``rx``) determines which audio fields are meaningful:

- TX uses ``audio_input``, plus ``alsa_device`` / ``jack_*`` / ``test_wave``
  as appropriate to the chosen input type.
- RX uses ``audio_output``, plus ``alsa_device`` / ``jack_*`` / ``wav_file``
  as appropriate to the chosen output type.

Fields meant for the other side are silently ignored (so a single TOML
deployed to both ends still works), but in v6 each TOML normally describes
one machine, so most files only carry the side they need.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from openob.link_config import ConfigError, resolve_link_settings


@dataclass(frozen=True)
class AudioInterface:
    """Per-node audio I/O configuration.

    Mode-specific fields default to ``None`` when not applicable: e.g. an RX
    interface has ``test_wave=None``, a TX interface has ``wav_file=None``.

    Construct via :meth:`from_resolved` from the merged TOML+CLI dict.
    """

    node_name: str
    mode: Literal['tx', 'rx']
    type: str

    # ALSA (TX or RX)
    alsa_device: str | None = None

    # JACK (TX or RX)
    jack_auto: bool | None = None
    jack_name: str | None = None
    jack_port_pattern: str | None = None

    # TX-only
    test_wave: str | None = None

    # RX-only
    wav_file: str | None = None

    @classmethod
    def from_resolved(
        cls,
        node_name: str,
        mode: Literal['tx', 'rx'],
        settings: Mapping[str, Any],
    ) -> 'AudioInterface':
        """Build from a flat settings dict (CLI overrides already merged in).

        Falls back to ``audio_input='auto'`` / ``audio_output='auto'`` if
        the relevant key isn't set, so a totally bare TOML still works on a
        machine with a single sound card.
        """
        if mode == 'tx':
            kind = str(settings.get('audio_input', 'auto'))
            return cls(
                node_name=node_name,
                mode='tx',
                type=kind,
                alsa_device=_get_optional_str(settings, 'alsa_device') if kind == 'alsa' else None,
                jack_auto=_get_optional_bool(settings, 'jack_auto') if kind == 'jack' else None,
                jack_name=_get_optional_str(settings, 'jack_name') if kind == 'jack' else None,
                jack_port_pattern=_get_optional_str(settings, 'jack_port_pattern') if kind == 'jack' else None,
                test_wave=_get_optional_str(settings, 'test_wave') if kind == 'test' else None,
            )
        if mode == 'rx':
            kind = str(settings.get('audio_output', 'auto'))
            return cls(
                node_name=node_name,
                mode='rx',
                type=kind,
                alsa_device=_get_optional_str(settings, 'alsa_device') if kind == 'alsa' else None,
                jack_auto=_get_optional_bool(settings, 'jack_auto') if kind == 'jack' else None,
                jack_name=_get_optional_str(settings, 'jack_name') if kind == 'jack' else None,
                jack_port_pattern=_get_optional_str(settings, 'jack_port_pattern') if kind == 'jack' else None,
                wav_file=_get_optional_str(settings, 'wav_file') if kind == 'wav' else None,
            )
        raise ConfigError(f'unknown mode: {mode!r}')


def load_audio_interface(
    link_name: str,
    node_name: str,
    mode: Literal['tx', 'rx'],
    *,
    cli_overrides: Mapping[str, Any] | None = None,
    config_dir: Path | str | None = None,
    config_file: Path | str | None = None,
    secrets_file: Path | str | None = None,
) -> AudioInterface:
    """Load AudioInterface from the same TOML+CLI sources as LinkConfig."""
    settings = resolve_link_settings(
        link_name,
        config_dir=config_dir,
        config_file=config_file,
        secrets_file=secrets_file,
        cli_overrides=cli_overrides,
    )
    iface = AudioInterface.from_resolved(node_name, mode, settings)
    _validate(iface)
    return iface


def _get_optional_str(section: Mapping[str, Any], key: str) -> str | None:
    val = section.get(key)
    if val is None:
        return None
    return str(val)


def _get_optional_bool(section: Mapping[str, Any], key: str) -> bool | None:
    val = section.get(key)
    if val is None:
        return None
    return bool(val)


def _validate(iface: AudioInterface) -> None:
    """Sanity-check resolved audio settings."""
    if iface.mode == 'tx' and iface.type not in ('auto', 'alsa', 'jack', 'test'):
        raise ConfigError(
            f'TX audio_input must be one of auto/alsa/jack/test, got {iface.type!r}'
        )
    if iface.mode == 'rx' and iface.type not in ('auto', 'alsa', 'jack', 'test', 'wav'):
        raise ConfigError(
            f'RX audio_output must be one of auto/alsa/jack/test/wav, got {iface.type!r}'
        )
    if iface.mode == 'rx' and iface.type == 'wav' and not iface.wav_file:
        raise ConfigError(
            "RX audio_output='wav' requires wav_file to be set "
            "(in the TOML or via --wav-file)"
        )
