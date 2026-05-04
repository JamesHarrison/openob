"""Unit tests for ``openob.hooks``.

These deliberately avoid GStreamer — they exercise the disabled singleton,
the state machine, the JSON envelope shape, the bounded-queue overflow
behaviour, and the subprocess timeout path. End-to-end coverage that
spawns a real OpenOB rx + tx pair with a hook configured lives in the
end-to-end suite (see ``tests/test_endtoend.py`` if/when added).
"""
from __future__ import annotations

import json
import stat
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from openob.hooks import (
    HOOK_SCHEMA,
    HookConfig,
    LinkHookRunner,
    _resolve_command,
)


# --- LinkHookRunner.disabled ------------------------------------------------


def test_disabled_singleton_is_reused():
    a = LinkHookRunner.disabled()
    b = LinkHookRunner.disabled()
    assert a is b


def test_disabled_methods_are_safe():
    """Every public method on a disabled runner returns None and never
    raises, regardless of input shape."""
    r = LinkHookRunner.disabled()
    r.start()
    r.stop()
    r.set_status('pending')
    r.set_status('up')
    r.set_status('down', reason='whatever')
    r.set_status('shutdown')
    r.update_audio_levels([-12.5, -10.0], [-20.0, -18.0])
    r.update_audio_levels(None, None)
    r.update_audio_levels([], [])


# --- HookConfig.from_link ---------------------------------------------------


@dataclass
class _FakeLink:
    name: str = 'test-link'
    hook_command: str | None = None
    hook_heartbeat_seconds: int = 0
    hook_timeout_seconds: int = 5


def test_from_link_disabled_when_no_command():
    link = _FakeLink(hook_command=None)
    r = LinkHookRunner.from_link(link, node_name='studio', role='tx')
    assert r is LinkHookRunner.disabled()


def test_from_link_active_when_command_set():
    link = _FakeLink(hook_command='/bin/true')
    r = LinkHookRunner.from_link(link, node_name='studio', role='tx')
    assert r is not LinkHookRunner.disabled()
    assert r._enabled is True


# --- _resolve_command -------------------------------------------------------


def test_resolve_command_simple_string():
    assert _resolve_command('/usr/local/bin/hook') == ['/usr/local/bin/hook']


def test_resolve_command_string_with_args():
    # POSIX-style shlex: quotes group multi-word args.
    assert _resolve_command('/bin/sh -c "echo hi"') == ['/bin/sh', '-c', 'echo hi']


def test_resolve_command_list_passes_through():
    """List form is the recommended way to pass Windows paths — backslashes
    survive without TOML escaping or shlex munging."""
    assert _resolve_command([r'C:\Tools\hook.bat', '--verbose']) == [
        r'C:\Tools\hook.bat', '--verbose',
    ]


def test_resolve_command_empty_list_rejected():
    with pytest.raises(ValueError):
        _resolve_command([])


# --- snapshot shape ---------------------------------------------------------


def _build_runner(**hook_overrides) -> LinkHookRunner:
    """Direct-construct a runner without start()ing it. Exercises the
    snapshot/state machine in isolation."""
    cfg = HookConfig(
        command=hook_overrides.get('command', '/bin/true'),
        heartbeat_seconds=hook_overrides.get('heartbeat_seconds', 0),
        timeout_seconds=hook_overrides.get('timeout_seconds', 5),
    )
    return LinkHookRunner(
        config=cfg,
        link_name='testlink',
        node_name='testnode',
        role='tx',
        version='6.3.0-test',
        enabled=True,
    )


def test_initial_snapshot_is_pending():
    r = _build_runner()
    snap = r._snapshot_locked('status', reason=None)
    assert snap['schema'] == HOOK_SCHEMA
    assert snap['status'] == 'pending'
    assert snap['prev_status'] is None
    assert snap['link'] == 'testlink'
    assert snap['node'] == 'testnode'
    assert snap['role'] == 'tx'
    assert snap['version'] == '6.3.0-test'
    assert snap['audio'] == {'peak_dbfs': [], 'rms_dbfs': []}


def test_snapshot_includes_audio_levels_after_update():
    r = _build_runner()
    r.update_audio_levels([-12.5, -13.0], [-20.0, -21.0])
    snap = r._snapshot_locked('heartbeat', reason=None)
    assert snap['audio']['peak_dbfs'] == [-12.5, -13.0]
    assert snap['audio']['rms_dbfs'] == [-20.0, -21.0]


def test_snapshot_uptime_only_set_when_up():
    r = _build_runner()
    snap = r._snapshot_locked('status', reason=None)
    assert snap['uptime_seconds'] is None  # pending → None
    # Simulate up transition (without going through subprocess plumbing).
    r._status = 'up'
    r._up_since = time.monotonic() - 1.5
    snap = r._snapshot_locked('heartbeat', reason=None)
    assert snap['uptime_seconds'] is not None
    assert 1.0 < snap['uptime_seconds'] < 5.0


def test_snapshot_is_json_serialisable():
    r = _build_runner()
    r.update_audio_levels([-12.0], [-20.0])
    snap = r._snapshot_locked('heartbeat', reason='whatever')
    # Should serialise without TypeError.
    text = json.dumps(snap)
    decoded = json.loads(text)
    assert decoded['reason'] == 'whatever'


# --- end-to-end: a real subprocess fires ------------------------------------


def _make_python_hook(tmp_path: Path) -> tuple[list[str], Path]:
    """Cross-platform hook that runs under the test's Python interpreter
    and appends one tab-separated line per invocation.

    Returns (argv-list-for-hook_command, log_path). We use list form
    deliberately so Windows paths with backslashes pass through to
    subprocess verbatim, without shlex.split eating them.
    """
    log_path = tmp_path / 'hook.log'
    hook_path = tmp_path / 'hook.py'
    hook_path.write_text(textwrap.dedent(f'''\
        import sys
        stdin = sys.stdin.read()
        with open(r"{log_path}", "a", encoding="utf-8") as f:
            f.write("\\t".join(sys.argv[1:6]) + "\\t" + stdin + "\\n")
    '''))
    return [sys.executable, str(hook_path)], log_path


def test_status_transition_fires_subprocess_crossplatform(tmp_path):
    """Cross-platform smoke test: a Python-interpreter hook receives the
    transition events and the JSON envelope on stdin."""
    cmd, log_path = _make_python_hook(tmp_path)
    cfg = HookConfig(command=cmd, heartbeat_seconds=0, timeout_seconds=10)
    r = LinkHookRunner(
        config=cfg,
        link_name='lnk', node_name='nd', role='tx',
        version='6.3.0-test', enabled=True,
    )
    r.start()
    try:
        r.set_status('up')
        r.set_status('down', reason='unit_test')
    finally:
        r.stop()
    lines = _read_log_lines(log_path, expected_min=2)
    assert len(lines) >= 2, f'expected 2+ events, got {lines!r}'
    parts = [ln.split('\t') for ln in lines]
    events = [p[0] for p in parts]
    statuses = [p[3] for p in parts]
    assert events == ['status', 'status']
    assert statuses == ['up', 'down']
    payload_down = json.loads(parts[1][5])
    assert payload_down['reason'] == 'unit_test'
    assert payload_down['schema'] == HOOK_SCHEMA


def _make_hook_script(tmp_path: Path) -> tuple[Path, Path]:
    """Build a small POSIX shell hook that appends each invocation's argv +
    stdin to a log file. Returns (hook_path, log_path).

    Skips on Windows since /bin/sh isn't available there; the hook
    subsystem is cross-platform but the test fixture isn't.
    """
    if sys.platform == 'win32':
        pytest.skip('subprocess hook test uses /bin/sh; Windows-only CI is fine without')
    log_path = tmp_path / 'hook.log'
    hook_path = tmp_path / 'hook.sh'
    # Each invocation writes one JSON line with argv + stdin payload so the
    # test can assert on every event in order.
    hook_path.write_text(textwrap.dedent(f'''\
        #!/bin/sh
        set -eu
        STDIN=$(cat)
        printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "$1" "$2" "$3" "$4" "$5" "$STDIN" >> "{log_path}"
    '''))
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return hook_path, log_path


def _read_log_lines(log_path: Path, expected_min: int, timeout_s: float = 3.0) -> list[str]:
    """Poll log_path until it has at least ``expected_min`` lines or timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if log_path.exists():
            lines = [
                ln for ln in log_path.read_text().splitlines() if ln
            ]
            if len(lines) >= expected_min:
                return lines
        time.sleep(0.05)
    # Final read so the assertion error includes whatever did land.
    if log_path.exists():
        return [ln for ln in log_path.read_text().splitlines() if ln]
    return []


def test_status_transition_fires_subprocess(tmp_path):
    hook_path, log_path = _make_hook_script(tmp_path)
    cfg = HookConfig(command=str(hook_path), heartbeat_seconds=0, timeout_seconds=5)
    r = LinkHookRunner(
        config=cfg,
        link_name='lnk',
        node_name='nd',
        role='tx',
        version='6.3.0-test',
        enabled=True,
    )
    r.start()
    try:
        r.set_status('pending')  # first explicit pending — no-op (already pending)
        r.set_status('up')
        r.set_status('down', reason='unit_test')
    finally:
        # stop() drains queue; our shell script is fast enough.
        r.stop()

    lines = _read_log_lines(log_path, expected_min=2)
    assert len(lines) >= 2, f'expected 2+ events, got {lines!r}'
    # Each line is tab-separated argv + stdin JSON.
    parts = [ln.split('\t') for ln in lines]
    events = [p[0] for p in parts]
    statuses = [p[3] for p in parts]
    prevs = [p[4] for p in parts]
    assert events == ['status', 'status']
    assert statuses == ['up', 'down']
    assert prevs == ['pending', 'up']
    # JSON envelope on stdin parses + carries the same status as argv.
    payload_up = json.loads(parts[0][5])
    assert payload_up['schema'] == HOOK_SCHEMA
    assert payload_up['status'] == 'up'
    assert payload_up['prev_status'] == 'pending'
    payload_down = json.loads(parts[1][5])
    assert payload_down['status'] == 'down'
    assert payload_down['reason'] == 'unit_test'


def test_idempotent_status_does_not_fire(tmp_path):
    hook_path, log_path = _make_hook_script(tmp_path)
    cfg = HookConfig(command=str(hook_path), heartbeat_seconds=0, timeout_seconds=5)
    r = LinkHookRunner(
        config=cfg,
        link_name='lnk', node_name='nd', role='rx',
        version='-', enabled=True,
    )
    r.start()
    try:
        r.set_status('up')
        r.set_status('up')  # no-op
        r.set_status('up')  # no-op
    finally:
        r.stop()
    lines = _read_log_lines(log_path, expected_min=1)
    # Only the first transition fires; subsequent set_status('up') are no-ops.
    assert len(lines) == 1, f'expected 1 event, got {len(lines)}: {lines!r}'


def test_heartbeat_fires_on_interval(tmp_path):
    hook_path, log_path = _make_hook_script(tmp_path)
    # 1 second heartbeat — short enough to not slow the suite, long enough to be reliable.
    cfg = HookConfig(command=str(hook_path), heartbeat_seconds=1, timeout_seconds=5)
    r = LinkHookRunner(
        config=cfg,
        link_name='lnk', node_name='nd', role='tx',
        version='-', enabled=True,
    )
    r.start()
    try:
        r.set_status('up')
        # Wait long enough for at least 1 heartbeat. Allow generous slack.
        time.sleep(1.6)
    finally:
        r.stop()
    lines = _read_log_lines(log_path, expected_min=2)
    events = [ln.split('\t')[0] for ln in lines]
    assert 'status' in events
    assert 'heartbeat' in events


def test_subprocess_timeout_kills_child(tmp_path):
    """A hook that sleeps past hook_timeout_seconds gets SIGKILLed and
    the next event still fires — proving the runner doesn't wedge."""
    if sys.platform == 'win32':
        pytest.skip('uses /bin/sh sleep')
    log_path = tmp_path / 'hook.log'
    hook_path = tmp_path / 'slow_hook.sh'
    hook_path.write_text(textwrap.dedent(f'''\
        #!/bin/sh
        # First arg is event; we tag the log so the test sees what got through.
        if [ "$1" = "status" ] && [ "$4" = "up" ]; then
            sleep 10
        fi
        echo "$1 $4" >> "{log_path}"
    '''))
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    cfg = HookConfig(command=str(hook_path), heartbeat_seconds=0, timeout_seconds=1)
    r = LinkHookRunner(
        config=cfg,
        link_name='lnk', node_name='nd', role='tx',
        version='-', enabled=True,
    )
    r.start()
    try:
        r.set_status('up')   # this one will time out
        r.set_status('down')  # this one should still fire
        # Wait for the timeout (1s) + the next event to complete.
        time.sleep(1.8)
    finally:
        r.stop()
    lines = _read_log_lines(log_path, expected_min=1)
    # The 'up' got killed before it wrote; the 'down' completed.
    assert any('down' in ln for ln in lines), f'expected a down log, got {lines!r}'


def test_missing_hook_binary_does_not_crash(tmp_path):
    """If hook_command points at a non-existent path, set_status logs a
    warning but the runner stays alive for the next event."""
    cfg = HookConfig(
        command=str(tmp_path / 'nope_does_not_exist'),
        heartbeat_seconds=0,
        timeout_seconds=2,
    )
    r = LinkHookRunner(
        config=cfg,
        link_name='lnk', node_name='nd', role='tx',
        version='-', enabled=True,
    )
    r.start()
    try:
        r.set_status('up')
        r.set_status('down')
        time.sleep(0.3)
    finally:
        # stop() must return promptly even when every dispatch failed.
        r.stop()
    # If we got here without hanging or raising, the test passes.


def test_queue_overflow_drops_heartbeats_keeps_status():
    """Stuff the queue with heartbeats, then enqueue a status — status
    must succeed (or block briefly) while heartbeats drop on Full.

    We simulate a wedged subprocess by NOT calling start() and pushing
    events directly into the queue with the worker quiescent."""
    cfg = HookConfig(command='/bin/true', heartbeat_seconds=0, timeout_seconds=5)
    r = LinkHookRunner(
        config=cfg,
        link_name='lnk', node_name='nd', role='tx',
        version='-', enabled=True,
    )
    # Manually attach a queue but NOT a worker — so nothing drains.
    import queue as _q
    r._queue = _q.Queue(maxsize=2)
    # Fill it with heartbeat sentinels.
    from openob.hooks import _Event
    for _ in range(2):
        r._queue.put_nowait(_Event(kind='heartbeat', argv_extra=[], payload={}))
    # Heartbeat enqueue on full queue: must drop without exception.
    r._enqueue(_Event(kind='heartbeat', argv_extra=[], payload={}))
    assert r._queue.qsize() == 2
    # Status enqueue on full queue: blocks for STATUS_ENQUEUE_TIMEOUT_S then
    # logs a drop. Either way it must not raise.
    t0 = time.monotonic()
    r._enqueue(_Event(kind='status', argv_extra=[], payload={}))
    elapsed = time.monotonic() - t0
    # Should have waited approximately the status enqueue timeout (0.5s)
    # and then dropped silently.
    assert elapsed < 1.5  # generous upper bound
    assert r._queue.qsize() == 2  # nothing got in
