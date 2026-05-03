from __future__ import annotations

import os
import socket
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest


@pytest.fixture
def free_udp_port() -> int:
    """Allocate an unused UDP port. Brief race window; fine for local CI."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def free_udp_port2(free_udp_port) -> int:
    """A second unused UDP port — for tests that need two simultaneous links."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    if port == free_udp_port:
        # Almost-impossible collision; just bump.
        return port + 1
    return port


@pytest.fixture
def link_name() -> str:
    """Per-test link name. Files are isolated per-test via tmp_config_dir, so
    collisions are impossible — but a unique name still helps when reading
    failing-test logs."""
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def tmp_wav(tmp_path: Path) -> Path:
    return tmp_path / "rx.wav"


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    """Per-test ~/.openob equivalent. Tests write a TOML link file under
    ``<tmp>/links/<link_name>.toml`` and pass ``--config-dir <tmp>`` to the
    openob CLI."""
    (tmp_path / "links").mkdir()
    return tmp_path


def write_link_toml(
    config_dir: Path,
    link_name: str,
    *,
    encoding: str = "opus",
    port: int,
    receiver_host: str = "127.0.0.1",
    bitrate: int = 128,
    samplerate: int | None = None,
    channels: int | None = None,
    multicast: bool = False,
    security: str = "none",
    psk: str | None = None,
    root_psk: str | None = None,
    caps_from_config: bool = False,
    retransmission_time_limit: int | None = None,
    tx_host: str | None = None,
    extra: dict | None = None,
) -> Path:
    """Helper for tests: render a v6 (flat) link TOML at the canonical path
    inside ``config_dir`` and return it.

    All settings live at the top level; ``extra`` is a dict of additional
    flat keys (e.g. ``audio_input``, ``audio_output``, ``alsa_device``,
    ``test_wave``, ``wav_file``, ``jack_*``) that get appended.
    """
    path = config_dir / "links" / f"{link_name}.toml"
    fields: dict[str, Any] = {
        'encoding': encoding,
        'port': port,
        'receiver_host': receiver_host,
        'bitrate': bitrate,
        'multicast': multicast,
        'security': security,
    }
    if samplerate is not None:
        fields['samplerate'] = samplerate
    if channels is not None:
        fields['channels'] = channels
    if psk is not None:
        fields['psk'] = psk
    if root_psk is not None:
        fields['root_psk'] = root_psk
    if caps_from_config:
        fields['caps_from_config'] = True
    if retransmission_time_limit is not None:
        fields['retransmission_time_limit'] = retransmission_time_limit
    if tx_host is not None:
        fields['tx_host'] = tx_host
    if extra:
        fields.update(extra)

    lines: list[str] = []
    for k, v in fields.items():
        if isinstance(v, bool):
            lines.append(f'{k} = {"true" if v else "false"}')
        elif isinstance(v, int):
            lines.append(f'{k} = {v}')
        else:
            lines.append(f'{k} = "{v}"')
    path.write_text('\n'.join(lines) + '\n')
    return path


@pytest.fixture(autouse=True)
def _no_buffered_python_io(monkeypatch) -> None:
    """Ensure subprocess Python output is line-buffered so wait_for_log sees it."""
    monkeypatch.setenv("PYTHONUNBUFFERED", "1")
    # Force a UTF-8-clean locale inside subprocesses.
    if "LANG" not in os.environ:
        monkeypatch.setenv("LANG", "C.UTF-8")
