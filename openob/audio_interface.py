from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class AudioInterface:
    """
    Per-node audio I/O configuration. Not shared across the network — each
    node picks its input (TX) or output (RX) type independently.

    Construct via :meth:`from_argparse` from the openob.cli ``Namespace``.
    Mode-specific fields default to ``None`` when not applicable: e.g. an RX
    interface has ``test_wave=None``, a TX interface has ``wav_file=None``.
    """

    node_name: str
    mode: Literal["tx", "rx"]
    type: str

    # TX-only
    samplerate: int = 0
    channels: int = 0  # 0 = let the source/audioconvert negotiate; pin via --channels
    test_wave: str | None = None

    # ALSA (TX or RX)
    alsa_device: str | None = None

    # JACK (TX or RX)
    jack_auto: bool | None = None
    jack_name: str | None = None
    jack_port_pattern: str | None = None

    # RX-only
    wav_file: str | None = None

    @classmethod
    def from_argparse(cls, opts: argparse.Namespace) -> AudioInterface:
        """Build an :class:`AudioInterface` from a parsed CLI Namespace."""
        if opts.mode == "tx":
            kind = opts.audio_input
            return cls(
                node_name=opts.node_name,
                mode="tx",
                type=kind,
                samplerate=opts.samplerate,
                channels=opts.channels,
                test_wave=opts.test_wave if kind == "test" else None,
                alsa_device=opts.alsa_device if kind == "alsa" else None,
                jack_auto=opts.jack_auto if kind == "jack" else None,
                jack_name=opts.jack_name if kind == "jack" else None,
                jack_port_pattern=opts.jack_port_pattern if kind == "jack" else None,
            )
        if opts.mode == "rx":
            kind = opts.audio_output
            return cls(
                node_name=opts.node_name,
                mode="rx",
                type=kind,
                wav_file=opts.wav_file if kind == "wav" else None,
                alsa_device=opts.alsa_device if kind == "alsa" else None,
                jack_auto=opts.jack_auto if kind == "jack" else None,
                jack_name=opts.jack_name if kind == "jack" else None,
                jack_port_pattern=opts.jack_port_pattern if kind == "jack" else None,
            )
        raise ValueError("Unknown mode %r" % opts.mode)
