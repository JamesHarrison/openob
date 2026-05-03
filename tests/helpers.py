from __future__ import annotations

import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import IO


class OpenObProcess:
    """
    Context manager that runs `openob ...` as a subprocess, captures its
    combined stdout/stderr to a tempfile, and exposes log-line waiting helpers.

    Use as:
        with OpenObProcess(["...", "rx"]) as p:
            p.wait_for_log("Receiving mono audio transmission", timeout=10)
    """

    def __init__(self, argv: list[str], extra_env: dict[str, str] | None = None) -> None:
        self.argv = ["openob", *argv]
        self.extra_env = extra_env or {}
        self._log_file: IO[bytes] | None = None
        self._log_path: Path | None = None
        self._proc: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> "OpenObProcess":
        # Combined stdout+stderr into one append-only file we tail.
        f = NamedTemporaryFile(prefix="openob-", suffix=".log", delete=False)
        self._log_file = f
        self._log_path = Path(f.name)

        env = os.environ.copy()
        env.update(self.extra_env)
        env.setdefault("PYTHONUNBUFFERED", "1")

        self._proc = subprocess.Popen(
            self.argv,
            stdout=f,
            stderr=subprocess.STDOUT,
            env=env,
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.send_signal(signal.SIGTERM)
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait()
        finally:
            if self._log_file is not None:
                try:
                    self._log_file.close()
                except Exception:
                    pass
            if self._log_path is not None and self._log_path.exists():
                if exc_type is not None:
                    # Test failed — dump the captured log to stderr so the
                    # diagnostic shows in pytest output, then leave the file
                    # in place for post-mortem inspection.
                    sys.stderr.write(f"\n--- {' '.join(self.argv)} log ---\n")
                    sys.stderr.write(self._log_path.read_text(errors="replace"))
                    sys.stderr.write("--- end log ---\n")
                else:
                    # Test passed — clean up so we don't leak temp files.
                    try:
                        self._log_path.unlink()
                    except OSError:
                        pass

    @property
    def log_path(self) -> Path:
        assert self._log_path is not None
        return self._log_path

    def read_log(self) -> str:
        if self._log_path is None or not self._log_path.exists():
            return ""
        return self._log_path.read_text(errors="replace")

    def wait_for_log(self, pattern: str, timeout: float = 10.0) -> re.Match[str]:
        """Block until a line matching `pattern` appears in the log, or raise."""
        compiled = re.compile(pattern)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc and self._proc.poll() is not None:
                raise RuntimeError(
                    f"{self.argv[0]} exited (code {self._proc.returncode}) before "
                    f"{pattern!r} was seen.\nLog:\n{self.read_log()}"
                )
            text = self.read_log()
            m = compiled.search(text)
            if m:
                return m
            time.sleep(0.1)
        raise TimeoutError(
            f"Timed out after {timeout}s waiting for {pattern!r}.\nLog:\n{self.read_log()}"
        )

    def graceful_stop(self, timeout: float = 3.0) -> None:
        """SIGTERM the process and wait for it to exit. Idempotent."""
        if self._proc is None or self._proc.poll() is not None:
            return
        self._proc.send_signal(signal.SIGTERM)
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()


def wav_rms_dbfs(path: Path) -> float:
    """
    Compute the RMS level of a 16-bit PCM WAV file in dBFS.

    Returns -inf for fully-silent input. Uses stdlib `wave` + `array` so we
    avoid pulling numpy in just for this.
    """
    import array

    with wave.open(str(path), "rb") as w:
        nchannels = w.getnchannels()
        sampwidth = w.getsampwidth()
        nframes = w.getnframes()
        if sampwidth != 2:
            raise ValueError(
                f"wav_rms_dbfs expected 16-bit PCM (sampwidth=2), got sampwidth={sampwidth}"
            )
        raw = w.readframes(nframes)

    if not raw:
        return -math.inf

    samples = array.array("h")
    samples.frombytes(raw)

    if not samples:
        return -math.inf

    # Mean-square across all samples (interleaved channels are fine — RMS is per-amplitude)
    sq_sum = 0.0
    for s in samples:
        sq_sum += s * s
    mean_sq = sq_sum / len(samples)
    if mean_sq <= 0:
        return -math.inf
    rms = math.sqrt(mean_sq)
    # 16-bit signed full-scale is 32768
    return 20.0 * math.log10(rms / 32768.0)


def wav_duration_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _wav_window_rms_dbfs_series(
    path: Path,
    *,
    window_ms: float = 20.0,
    trim_ms: float = 200.0,
) -> list[float]:
    """
    Return RMS in dBFS for each non-overlapping ``window_ms`` window of the
    WAV's interior (start and end are trimmed by ``trim_ms`` to ignore
    pipeline ramp-up / teardown). A 20 ms window is one Opus frame at
    48 kHz, so each entry corresponds to one audio packet — the unit a
    listener would notice as a dropout.
    """
    import array

    with wave.open(str(path), "rb") as w:
        framerate = w.getframerate()
        nchannels = w.getnchannels()
        sampwidth = w.getsampwidth()
        nframes = w.getnframes()
        if sampwidth != 2:
            raise ValueError(
                f"expected 16-bit PCM (sampwidth=2), got {sampwidth}"
            )
        raw = w.readframes(nframes)

    if not raw:
        return []

    samples = array.array("h")
    samples.frombytes(raw)
    if not samples:
        return []

    frames_per_window = int(framerate * window_ms / 1000.0)
    samples_per_window = frames_per_window * nchannels
    if samples_per_window <= 0:
        return []

    trim_frames = int(framerate * trim_ms / 1000.0)
    trim_samples = trim_frames * nchannels
    interior = samples[trim_samples : len(samples) - trim_samples]
    if len(interior) < samples_per_window:
        return []

    out: list[float] = []
    for offset in range(0, len(interior) - samples_per_window + 1, samples_per_window):
        chunk = interior[offset : offset + samples_per_window]
        sq_sum = 0.0
        for s in chunk:
            sq_sum += s * s
        mean_sq = sq_sum / len(chunk)
        if mean_sq <= 0:
            out.append(-math.inf)
        else:
            rms = math.sqrt(mean_sq)
            out.append(20.0 * math.log10(rms / 32768.0))
    return out


def wav_lowest_window_rms_dbfs(
    path: Path,
    *,
    window_ms: float = 20.0,
    trim_ms: float = 200.0,
) -> float:
    """The dimmest non-silent window in the WAV's interior, in dBFS.

    For a continuous pink-noise input, every window should be near the
    average level — ANY window much lower than that signals a dropout.
    """
    series = _wav_window_rms_dbfs_series(path, window_ms=window_ms, trim_ms=trim_ms)
    return min(series) if series else -math.inf


def wav_dropout_count_dbfs(
    path: Path,
    *,
    threshold_dbfs: float = -40.0,
    window_ms: float = 20.0,
    trim_ms: float = 200.0,
) -> tuple[int, int]:
    """How many ``window_ms``-windows fall below ``threshold_dbfs``.

    Returns ``(dropout_count, total_windows)`` so callers can report a
    rate, not just a count. With a continuous input, ``dropout_count``
    should be 0 on a clean link.
    """
    series = _wav_window_rms_dbfs_series(path, window_ms=window_ms, trim_ms=trim_ms)
    dropouts = sum(1 for db in series if db < threshold_dbfs)
    return dropouts, len(series)


# --- OTLP receiver fixture for telemetry e2e -------------------------------


class CapturedOTLPMetric:
    """Lightweight projection of a single OTLP metric across all exports.

    Attributes are aggregated across every export request the receiver has
    accepted, so callers can write ``metric_for('openob.rtp.packets_sent')``
    and not worry about which export window contained the latest sample.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        # (value, attrs_dict) tuples in the order they were observed.
        self.points: list[tuple[float, dict[str, str]]] = []

    def attr_values(self, attr_name: str) -> set[str]:
        return {a.get(attr_name, '') for _, a in self.points}

    def latest(self) -> float | None:
        return self.points[-1][0] if self.points else None

    def total(self) -> float:
        return sum(v for v, _ in self.points)

    def points_with(self, **expected: str) -> list[tuple[float, dict[str, str]]]:
        out = []
        for v, a in self.points:
            if all(a.get(k) == val for k, val in expected.items()):
                out.append((v, a))
        return out


class OTLPReceiver:
    """Tiny HTTP server that decodes OTLP metric exports for tests.

    Avoids opentelemetry-collector — the receiver is the only thing the
    e2e suite needs from the OTLP wire. Decodes via the protobuf module
    that ships with ``opentelemetry-proto`` (a transitive dep of the
    OTLP HTTP exporter).

    Usage::

        with OTLPReceiver() as r:
            ... run pipeline pointed at r.url ...
            metrics = r.metrics()
            assert metrics['openob.rtp.packets_sent'].latest() > 0
    """

    def __init__(self) -> None:
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._raw_bodies: list[bytes] = []

    def __enter__(self) -> 'OTLPReceiver':
        bodies = self._raw_bodies

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # type: ignore[override]
                length = int(self.headers.get('Content-Length', '0'))
                body = self.rfile.read(length) if length else b''
                bodies.append(body)
                # OTLP HTTP responds with an empty ExportMetricsServiceResponse;
                # 200 + zero bytes is acceptable to the OTLP HTTP exporter.
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-protobuf')
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, format, *args) -> None:  # noqa: A002,ARG002
                # Silence the per-request stderr spam — tests get loud enough.
                pass

        self._server = HTTPServer(('127.0.0.1', 0), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name='otlp-receiver',
        )
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        server = self._server
        if server is not None:
            server.shutdown()
            server.server_close()

    @property
    def host(self) -> str:
        assert self._server is not None
        host_part = self._server.server_address[0]
        return host_part if isinstance(host_part, str) else host_part.decode()

    @property
    def port(self) -> int:
        assert self._server is not None
        port_part = self._server.server_address[1]
        return int(port_part) if isinstance(port_part, (int, str)) else 0

    @property
    def url(self) -> str:
        return f'http://{self.host}:{self.port}/v1/metrics'

    def export_count(self) -> int:
        return len(self._raw_bodies)

    def metrics(self) -> dict[str, CapturedOTLPMetric]:
        """Decode all received OTLP exports into ``{name: CapturedOTLPMetric}``.

        Decoding happens on demand (rather than per-request) so test
        assertions can compute the snapshot once after the audio flow has
        finished. Returns an empty dict if no bodies have arrived yet.
        """
        try:
            from opentelemetry.proto.collector.metrics.v1 import (  # noqa: PLC0415
                metrics_service_pb2,
            )
        except ImportError as exc:
            import pytest  # noqa: PLC0415
            pytest.skip(f'opentelemetry-proto not installed: {exc}')
            return {}  # unreachable; pytest.skip raises

        out: dict[str, CapturedOTLPMetric] = {}
        for body in list(self._raw_bodies):
            req = metrics_service_pb2.ExportMetricsServiceRequest()
            try:
                req.ParseFromString(body)
            except Exception:
                continue
            for rm in req.resource_metrics:
                for sm in rm.scope_metrics:
                    for metric in sm.metrics:
                        cap = out.setdefault(
                            metric.name, CapturedOTLPMetric(metric.name),
                        )
                        _consume_data_points(metric, cap)
        return out


def _consume_data_points(metric, cap: CapturedOTLPMetric) -> None:
    """Walk the per-instrument data-point lists and append to ``cap``.

    Counters live under ``sum.data_points``, gauges under ``gauge.data_points``;
    histograms exist but OpenOB doesn't emit any. Supports both as_int and
    as_double point values which the OTel SDK can pick between.
    """
    if metric.HasField('sum'):
        for dp in metric.sum.data_points:
            cap.points.append(_point_tuple(dp))
    if metric.HasField('gauge'):
        for dp in metric.gauge.data_points:
            cap.points.append(_point_tuple(dp))


def _point_tuple(dp) -> tuple[float, dict[str, str]]:
    if dp.HasField('as_int'):
        value = float(dp.as_int)
    elif dp.HasField('as_double'):
        value = dp.as_double
    else:
        value = 0.0
    attrs: dict[str, str] = {}
    for kv in dp.attributes:
        v = kv.value
        if v.HasField('string_value'):
            attrs[kv.key] = v.string_value
        elif v.HasField('int_value'):
            attrs[kv.key] = str(v.int_value)
        elif v.HasField('double_value'):
            attrs[kv.key] = str(v.double_value)
        elif v.HasField('bool_value'):
            attrs[kv.key] = 'true' if v.bool_value else 'false'
    return value, attrs
