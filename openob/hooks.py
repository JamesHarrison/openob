"""Per-link command hooks for OpenOB.

Off by default. Operators wire ``hook_command`` in their link TOML (or
``--hook-command`` on the CLI) to a script that gets invoked on link state
transitions (``pending`` → ``up`` → ``down`` → ``shutdown``) and, if
``hook_heartbeat_seconds`` is non-zero, on a regular cadence with the
current audio levels and uptime.

Why this exists
---------------
Hardware integrators on Pi-grade equipment often want to drive a relay,
flash an LED, or post a single cell to an existing dashboard whenever the
link state changes. OpenTelemetry is overkill for that — a five-line bash
script is the right tool. This module is the wiring that makes a
five-line bash script a first-class citizen.

The hook command receives:

- positional argv suitable for ``case`` in shell::

    $1 = event       ("status" | "heartbeat")
    $2 = link name
    $3 = role        ("tx" | "rx")
    $4 = status      ("pending" | "up" | "down" | "shutdown")
    $5 = prev_status (status events only; "-" for heartbeat)

- a JSON envelope on stdin for richer consumers — link, role, status,
  audio peak/rms (per channel), uptime since the last ``up`` transition,
  the running OpenOB version. See :data:`HOOK_SCHEMA` for the schema id.

Reliability invariants
----------------------
The hook subsystem **must never** block or crash the audio path. We enforce
that with three layered guarantees:

1. All subprocess work runs on a dedicated worker thread; the GLib main
   loop only enqueues events.
2. The enqueue queue is bounded (16 entries). Heartbeats drop silently on
   overflow; status events get a short blocking timeout but log + skip if
   the queue is genuinely wedged.
3. Every public method is wrapped in :func:`_safe`, so a programming bug
   in the hook subsystem itself degrades to a DEBUG log and the audio
   path continues. Hook scripts that exit non-zero, time out, or fail to
   exec are logged at WARNING but never propagate.

Hook timeouts (``hook_timeout_seconds``) bound how long a single
invocation may run. On expiry the worker terminates the child and moves
on; the next event still fires.
"""
from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

from openob._log_events import (
    EVT_HOOK_DROPPED,
    EVT_HOOK_FAILED,
    EVT_HOOK_FIRED,
)
from openob.logger import LoggerFactory


HOOK_SCHEMA = 'openob.hook/1'

# Bounded enqueue depth. The worker drains at roughly 1/hook_timeout_seconds
# events/sec in the worst case (wedged child); 16 entries is enough to ride
# out a slow hook for ~16 seconds of activity before we drop.
_QUEUE_MAXSIZE = 16

# How long status events may block while waiting for queue space. Status
# events are higher-priority than heartbeats — we'd rather sit briefly
# than lose a state-change notification.
_STATUS_ENQUEUE_TIMEOUT_S = 0.5

# How long stop() will wait for the worker thread to finish its current
# subprocess invocation before giving up. Bounded so a wedged hook can't
# delay process exit by more than a few seconds.
_WORKER_JOIN_TIMEOUT_S = 2.0


Status = Literal['pending', 'up', 'down', 'shutdown']
EventKind = Literal['status', 'heartbeat']


@dataclass(frozen=True)
class HookConfig:
    """Hook settings extracted from a :class:`LinkConfig`.

    ``command`` is either a string (parsed via shlex with POSIX-style
    quoting) or a list of strings (passed through to subprocess as argv
    verbatim — preferred for Windows paths that would otherwise need
    awkward escaping).
    """
    command: 'str | list[str] | None' = None
    heartbeat_seconds: int = 0
    timeout_seconds: int = 5

    @classmethod
    def from_link(cls, link_config) -> 'HookConfig':
        return cls(
            command=getattr(link_config, 'hook_command', None),
            heartbeat_seconds=getattr(link_config, 'hook_heartbeat_seconds', 0),
            timeout_seconds=getattr(link_config, 'hook_timeout_seconds', 5),
        )


def _safe(method: Callable) -> Callable:
    """Decorator: swallow any exception inside a public LinkHookRunner
    method, log it at DEBUG, and return None. The hook subsystem must
    never bring the audio pipeline down.
    """
    name = method.__name__

    def wrapper(self: 'LinkHookRunner', *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — hooks boundary
            logger = getattr(self, '_logger', None)
            if logger is not None:
                logger.debug(
                    'hook subsystem internal error',
                    extra={
                        'event': EVT_HOOK_FAILED,
                        'method': name,
                        'error': str(exc),
                    },
                )
            return None
    wrapper.__name__ = name
    wrapper.__qualname__ = getattr(method, '__qualname__', name)
    return wrapper


def _resolve_command(command: 'str | list[str] | tuple[str, ...]') -> list[str]:
    """Normalise a hook command into argv.

    Accepts a list/tuple of strings (returned as-is — preferred for
    Windows paths with backslashes) or a single string (shlex.split with
    POSIX-style quoting; quote backslash-laden paths in TOML to keep
    them intact).
    """
    if isinstance(command, (list, tuple)):
        if not command:
            raise ValueError('hook_command list is empty')
        return [str(part) for part in command]
    if not isinstance(command, str):
        raise TypeError(
            f'hook_command must be str or list[str], got {type(command).__name__}'
        )
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        # Quoting mismatch — fall back to a single-element argv so the
        # operator at least sees an exec error pointing at the literal
        # they wrote, rather than a confusing tokenization mismatch.
        argv = [command]
    if not argv:
        return [command]
    return argv


@dataclass
class _Event:
    kind: EventKind
    argv_extra: list[str]
    payload: dict[str, Any]


class LinkHookRunner:
    """Per-link hook dispatcher.

    Single instance per ``Node.run_link`` invocation. Mirrors
    :class:`openob.telemetry.TelemetryRegistry`: a disabled singleton when
    no hook is configured, a worker-backed instance when one is. All
    public methods are ``_safe``-wrapped so call sites need no try/except.
    """

    _disabled_singleton: 'LinkHookRunner | None' = None

    def __init__(
        self,
        *,
        config: HookConfig,
        link_name: str,
        node_name: str,
        role: str,
        version: str,
        enabled: bool,
    ) -> None:
        self._config = config
        self._link_name = link_name
        self._node_name = node_name
        self._role = role
        self._version = version
        self._enabled = enabled
        self._argv: list[str] = (
            _resolve_command(config.command) if config.command else []
        )

        # State machine + level snapshot, read by the heartbeat snapshot
        # builder and updated by the public methods.
        self._state_lock = threading.Lock()
        self._status: Status = 'pending'
        self._prev_status: Status | None = None
        # Set at every up-edge; None while pending/down — used to compute
        # uptime_seconds in heartbeat / status payloads.
        self._up_since: float | None = None
        self._audio_peak: list[float] = []
        self._audio_rms: list[float] = []

        # Worker plumbing. Kept None on the disabled singleton so accidental
        # method calls don't allocate threads/queues.
        self._queue: queue.Queue[_Event | None] | None = None
        self._worker: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._started = False

        self._logger = LoggerFactory().getLogger(
            f'hooks.{role}.{link_name}'
        )

    # ------------------------------------------------------------ factories

    @classmethod
    def disabled(cls) -> 'LinkHookRunner':
        """Return the singleton no-op runner."""
        if cls._disabled_singleton is None:
            cls._disabled_singleton = cls(
                config=HookConfig(),
                link_name='-',
                node_name='-',
                role='-',
                version='-',
                enabled=False,
            )
        return cls._disabled_singleton

    @classmethod
    def from_link(
        cls,
        link_config,
        *,
        node_name: str,
        role: str,
        version: str = 'unknown',
    ) -> 'LinkHookRunner':
        """Build a runner for a link, or return ``disabled()`` if no hook."""
        cfg = HookConfig.from_link(link_config)
        if not cfg.command:
            return cls.disabled()
        return cls(
            config=cfg,
            link_name=link_config.name,
            node_name=node_name,
            role=role,
            version=version,
            enabled=True,
        )

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Spawn the worker + heartbeat threads.

        Idempotent: a second call is a no-op. The disabled singleton's
        ``start`` is also a no-op.
        """
        if not self._enabled or self._started:
            return
        self._stop_event.clear()
        self._queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._worker = threading.Thread(
            target=self._worker_loop,
            name=f'openob-hooks-{self._link_name}',
            daemon=True,
        )
        self._worker.start()
        if self._config.heartbeat_seconds > 0:
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                name=f'openob-hooks-hb-{self._link_name}',
                daemon=True,
            )
            self._heartbeat_thread.start()
        self._started = True
        self._logger.info(
            'hooks configured: command=%s heartbeat=%ds timeout=%ds',
            self._argv[0] if self._argv else '-',
            self._config.heartbeat_seconds,
            self._config.timeout_seconds,
        )

    def stop(self) -> None:
        """Signal threads to drain and exit. Bounded join."""
        if not self._started:
            return
        self._stop_event.set()
        # Sentinel to wake the worker if it's blocked on queue.get.
        if self._queue is not None:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                # Queue full — worker will pick up the stop_event after
                # finishing its current item.
                pass
        if self._worker is not None:
            self._worker.join(timeout=_WORKER_JOIN_TIMEOUT_S)
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=_WORKER_JOIN_TIMEOUT_S)
        self._started = False

    # ------------------------------------------------------------ public API

    @_safe
    def update_audio_levels(
        self,
        peaks: list[float] | tuple[float, ...] | None,
        rms: list[float] | tuple[float, ...] | None,
    ) -> None:
        """Cache the most recent per-channel level data for snapshots.

        Called from the GStreamer level-message handler at the same site
        as the telemetry gauges. Cheap — just a list copy under a lock.
        Heartbeat snapshots read this; status events also include the
        latest values so the operator's hook always sees something
        recent rather than zeros.
        """
        if not self._enabled:
            return
        peaks_list = [float(v) for v in peaks] if peaks else []
        rms_list = [float(v) for v in rms] if rms else []
        with self._state_lock:
            self._audio_peak = peaks_list
            self._audio_rms = rms_list

    @_safe
    def set_status(self, status: Status, *, reason: str | None = None) -> None:
        """Record a state transition and fire a status hook.

        Idempotent: calling with the current status is a no-op (no event
        fired). Status sequence: ``pending`` (initial) → ``up`` (level
        message received) → ``down`` (EOS/error/timeout) → ``up``
        (recovered) → ``shutdown`` (clean exit).
        """
        if not self._enabled:
            return
        if status not in ('pending', 'up', 'down', 'shutdown'):
            raise ValueError(f'unknown hook status: {status!r}')
        with self._state_lock:
            if status == self._status:
                return
            prev = self._status
            self._prev_status = prev
            self._status = status
            if status == 'up':
                self._up_since = time.monotonic()
            elif status in ('down', 'shutdown'):
                self._up_since = None
            payload = self._snapshot_locked('status', reason=reason)
        argv_extra = [
            'status', self._link_name, self._role, status, prev,
        ]
        self._enqueue(_Event(kind='status', argv_extra=argv_extra, payload=payload))

    # ------------------------------------------------------------ internals

    def _enqueue(self, evt: _Event) -> None:
        """Push ``evt`` onto the worker queue.

        Status events block briefly (up to :data:`_STATUS_ENQUEUE_TIMEOUT_S`)
        because we'd rather pause than miss a state change. Heartbeats use
        ``put_nowait`` and drop silently on overflow — by the time the
        queue is full, the user script is too slow to keep up with the
        configured heartbeat anyway, and dropping the heartbeat is the
        correct backpressure response.
        """
        if self._queue is None:
            return
        try:
            if evt.kind == 'status':
                self._queue.put(evt, timeout=_STATUS_ENQUEUE_TIMEOUT_S)
            else:
                self._queue.put_nowait(evt)
        except queue.Full:
            self._logger.warning(
                'hook queue full; dropping %s event',
                evt.kind,
                extra={
                    'event': EVT_HOOK_DROPPED,
                    'kind': evt.kind,
                    'link': self._link_name,
                },
            )

    def _heartbeat_loop(self) -> None:
        """Periodic enqueuer for heartbeat events.

        Uses ``Event.wait`` rather than ``time.sleep`` so ``stop()`` wakes
        us within the heartbeat interval rather than at the end of it.
        """
        interval = self._config.heartbeat_seconds
        while not self._stop_event.is_set():
            # wait returns True if the event got set (i.e. stop requested),
            # False on timeout (i.e. fire a heartbeat).
            if self._stop_event.wait(timeout=interval):
                return
            with self._state_lock:
                payload = self._snapshot_locked('heartbeat', reason=None)
                status = self._status
            argv_extra = [
                'heartbeat', self._link_name, self._role, status, '-',
            ]
            self._enqueue(_Event(
                kind='heartbeat', argv_extra=argv_extra, payload=payload,
            ))

    def _worker_loop(self) -> None:
        """Drain the queue, exec the user command for each event."""
        assert self._queue is not None
        while True:
            try:
                evt = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop_event.is_set():
                    return
                continue
            if evt is None:  # shutdown sentinel
                return
            self._dispatch(evt)
            if self._stop_event.is_set():
                # Drain any remaining items briefly so a final 'shutdown'
                # status hook fires before exit, but don't loop forever.
                self._drain_remaining()
                return

    def _drain_remaining(self) -> None:
        assert self._queue is not None
        while True:
            try:
                evt = self._queue.get_nowait()
            except queue.Empty:
                return
            if evt is None:
                return
            self._dispatch(evt)

    def _dispatch(self, evt: _Event) -> None:
        argv = list(self._argv) + evt.argv_extra
        try:
            stdin_data = json.dumps(evt.payload).encode('utf-8')
        except (TypeError, ValueError) as exc:
            # Should never happen — we control the payload shape — but
            # defensively skip rather than killing the worker.
            self._logger.debug(
                'failed to serialise hook payload: %s', exc,
                extra={'event': EVT_HOOK_FAILED, 'reason': 'json_encode'},
            )
            return
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=(sys.platform != 'win32'),
                # Prevent Ctrl-C from propagating into the hook on POSIX:
                # the operator is shutting OpenOB down, the hook should run
                # to completion and exit naturally rather than dying mid-call.
                # On Windows there's no equivalent setpgrp; CREATE_NEW_PROCESS_GROUP
                # would prevent Ctrl-C propagation but breaks shell-script hooks.
                preexec_fn=os.setpgrp if sys.platform != 'win32' else None,
            )
        except OSError as exc:
            # Most common cause: hook_command points at a non-existent
            # binary. Log loudly the first time, but don't disable the
            # runner — the operator may fix the script in place.
            self._logger.warning(
                'hook exec failed (%s): %s', argv[0] if argv else '?', exc,
                extra={
                    'event': EVT_HOOK_FAILED,
                    'reason': 'exec',
                    'kind': evt.kind,
                    'error': str(exc),
                },
            )
            return
        try:
            stdout, stderr = proc.communicate(
                input=stdin_data,
                timeout=self._config.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass  # already SIGKILLed; the OS will reap
            self._logger.warning(
                'hook timed out after %ds (%s)',
                self._config.timeout_seconds, evt.kind,
                extra={
                    'event': EVT_HOOK_FAILED,
                    'reason': 'timeout',
                    'kind': evt.kind,
                    'timeout_seconds': self._config.timeout_seconds,
                },
            )
            return
        rc = proc.returncode
        if rc == 0:
            self._logger.debug(
                'hook fired ok (%s)', evt.kind,
                extra={
                    'event': EVT_HOOK_FIRED,
                    'kind': evt.kind,
                    'rc': rc,
                },
            )
            return
        # Non-zero exit → log at WARNING with truncated stderr so an
        # operator can debug without their hook spamming megabytes.
        stderr_text = (stderr or b'').decode('utf-8', errors='replace')[:512]
        self._logger.warning(
            'hook exited non-zero (rc=%d, kind=%s): %s',
            rc, evt.kind, stderr_text.strip() or '(no stderr)',
            extra={
                'event': EVT_HOOK_FAILED,
                'reason': 'nonzero_exit',
                'kind': evt.kind,
                'rc': rc,
            },
        )

    def _snapshot_locked(
        self,
        kind: EventKind,
        *,
        reason: str | None,
    ) -> dict[str, Any]:
        """Build a JSON-serialisable snapshot. Caller holds ``_state_lock``.

        Stable fields that downstream consumers can rely on are documented
        in the module docstring. ``audio.peak_dbfs`` / ``audio.rms_dbfs``
        are the most recent values seen by ``update_audio_levels`` — empty
        lists when no audio has flowed yet (pre-first level message).
        """
        uptime: float | None
        if self._up_since is not None:
            uptime = round(time.monotonic() - self._up_since, 3)
        else:
            uptime = 0.0 if self._status == 'up' else None
        payload: dict[str, Any] = {
            'schema': HOOK_SCHEMA,
            'event': kind,
            'ts': round(time.time(), 3),
            'link': self._link_name,
            'node': self._node_name,
            'role': self._role,
            'version': self._version,
            'status': self._status,
            'prev_status': self._prev_status,
            'reason': reason,
            'uptime_seconds': uptime,
            'audio': {
                'peak_dbfs': list(self._audio_peak),
                'rms_dbfs': list(self._audio_rms),
            },
        }
        return payload


__all__ = [
    'HookConfig',
    'HOOK_SCHEMA',
    'LinkHookRunner',
]
