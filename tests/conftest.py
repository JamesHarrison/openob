from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Iterator

import pytest
import redis


def _wait_for_redis(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            redis.Redis(host=host, port=port, socket_connect_timeout=0.5).ping()
            return
        except Exception as e:
            last_err = e
            time.sleep(0.05)
    raise RuntimeError(f"redis-server at {host}:{port} did not respond in {timeout}s: {last_err}")


@pytest.fixture(scope="session")
def redis_server(tmp_path_factory) -> Iterator[tuple[str, int]]:
    """Spawn a private redis-server on an ephemeral port for the test session."""
    if shutil.which("redis-server") is None:
        pytest.skip("redis-server binary not on PATH (install redis-server or run inside the test container)")

    # Pick an ephemeral TCP port up front. There's an inherent race between
    # closing the socket and redis-server taking it, but it's tiny and good
    # enough for a single-test-runner CI.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    workdir = tmp_path_factory.mktemp("redis")
    log_path = workdir / "redis.log"

    proc = subprocess.Popen(
        [
            "redis-server",
            "--port", str(port),
            "--bind", "127.0.0.1",
            "--save", "",
            "--appendonly", "no",
            "--daemonize", "no",
            "--dir", str(workdir),
            "--logfile", str(log_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_redis("127.0.0.1", port)
        yield ("127.0.0.1", port)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture
def free_udp_port() -> int:
    """Allocate an unused UDP port. Brief race window; fine for local CI."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def link_name() -> str:
    """Per-test link name to avoid Redis-key collisions across tests."""
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def tmp_wav(tmp_path: Path) -> Path:
    return tmp_path / "rx.wav"


@pytest.fixture(autouse=True)
def _no_buffered_python_io(monkeypatch) -> None:
    """Ensure subprocess Python output is line-buffered so wait_for_log sees it."""
    monkeypatch.setenv("PYTHONUNBUFFERED", "1")
    # Force a UTF-8-clean locale inside subprocesses (slim Docker images can
    # otherwise default to POSIX/C and surprise GStreamer logging).
    if "LANG" not in os.environ:
        monkeypatch.setenv("LANG", "C.UTF-8")
