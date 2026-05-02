from __future__ import annotations

import math
import os
import re
import signal
import subprocess
import sys
import time
import wave
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
