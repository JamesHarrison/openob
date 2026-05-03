"""Unit tests for ``openob.telemetry`` and the JSON log formatter.

These tests deliberately avoid GStreamer — they exercise the no-op
disabled path, the counter-delta tracker, and the JSON formatter in
isolation. End-to-end coverage (the OTLP push, full pipeline scrape)
lives in tests/test_endtoend.py.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import pytest

from openob.logger import LinkLoggerAdapter, LoggerFactory, _JSONFormatter
from openob.telemetry import (
    TelemetryConfig,
    TelemetryRegistry,
    _peek_last_values,
    get_openob_version,
)


# --- TelemetryRegistry.disabled --------------------------------------------


def test_disabled_singleton_is_reused():
    a = TelemetryRegistry.disabled()
    b = TelemetryRegistry.disabled()
    assert a is b


def test_disabled_methods_are_safe():
    """Every public method on a disabled registry returns None and never
    raises, regardless of input shape."""
    r = TelemetryRegistry.disabled()
    # No values fed; no kwargs missing.
    r.start()
    r.stop()
    r.set_link_up(True)
    r.set_link_up(False)
    r.record_pipeline_restart(reason='ZeroDivisionError')
    r.set_pipeline_uptime(123.4)
    r.set_audio_peak(0, -12.5)
    r.set_audio_rms(1, -20.0)
    r.update_rtp_session_stats({}, clock_rate=48000)
    r.update_rtp_session_stats(None, clock_rate=48000)  # tolerates None
    r.update_jitterbuffer_stats({'num-pushed': 100})
    r.update_opusdec_stats({'plc-num-samples': 0, 'plc-duration': 0})
    r.update_rtx_send_stats(requests=0, packets=0)
    r.add_path_bytes(label='modem_a', bytes_served=1024)
    r.record_data_timeout()
    r.record_caps_signal(result='ok')
    r.record_caps_signal(result='auth_fail')
    r.record_srtp_failure()
    r.reset_counter_deltas()
    # Counter cache stays empty on disabled.
    assert _peek_last_values(r) == {}


# --- TelemetryRegistry.from_link --------------------------------------------


@dataclass
class _FakeLink:
    name: str = 'test-link'
    telemetry_enabled: bool = False
    telemetry_otlp_endpoint: str | None = None
    telemetry_otlp_headers: str | None = None
    telemetry_otlp_interval_seconds: int = 30
    telemetry_otlp_timeout_seconds: int = 10
    telemetry_otlp_compression: str = 'gzip'


def test_from_link_disabled_when_not_enabled():
    link = _FakeLink(telemetry_enabled=False, telemetry_otlp_endpoint='https://x:4318')
    r = TelemetryRegistry.from_link(link, node_name='studio', role='tx')
    assert r is TelemetryRegistry.disabled()


def test_from_link_disabled_when_endpoint_missing():
    link = _FakeLink(telemetry_enabled=True, telemetry_otlp_endpoint=None)
    r = TelemetryRegistry.from_link(link, node_name='studio', role='tx')
    assert r is TelemetryRegistry.disabled()


def test_from_link_disabled_when_endpoint_blank():
    link = _FakeLink(telemetry_enabled=True, telemetry_otlp_endpoint='')
    r = TelemetryRegistry.from_link(link, node_name='studio', role='tx')
    assert r is TelemetryRegistry.disabled()


def test_telemetry_config_from_link_pulls_fields():
    link = _FakeLink(
        telemetry_enabled=True,
        telemetry_otlp_endpoint='https://otlp.example.com:4318',
        telemetry_otlp_interval_seconds=60,
    )
    cfg = TelemetryConfig.from_link(link)
    assert cfg.enabled is True
    assert cfg.otlp_endpoint == 'https://otlp.example.com:4318'
    assert cfg.otlp_interval_seconds == 60


# --- counter delta tracking ------------------------------------------------


class _RecordingCounter:
    """Stand-in for an OTel Counter that captures (value, attrs) tuples."""
    def __init__(self) -> None:
        self.calls: list[tuple[float, dict[str, Any]]] = []

    def add(self, value, attributes=None) -> None:
        self.calls.append((value, dict(attributes or {})))


def _make_registry_with_meter():
    """Build a TelemetryRegistry that's "enabled" but uses a fake meter so
    we can assert what it would have pushed without spinning up an OTLP
    exporter (which needs the network and the OTel SDK).
    """
    r = TelemetryRegistry(
        config=TelemetryConfig(
            enabled=True,
            otlp_endpoint='http://noop',
            otlp_interval_seconds=5,
        ),
        link_name='test-link',
        node_name='studio',
        role='rx',
        version='6.2.0',
        enabled=True,
    )

    class _FakeMeter:
        def __init__(self) -> None:
            self.counters: dict[str, _RecordingCounter] = {}
            self.gauges: dict[str, _RecordingGauge] = {}

        def create_counter(self, name, unit='', description=''):
            counter = _RecordingCounter()
            self.counters[name] = counter
            return counter

        def create_gauge(self, name, unit='', description=''):
            gauge = _RecordingGauge()
            self.gauges[name] = gauge
            return gauge

    class _RecordingGauge:
        def __init__(self) -> None:
            self.calls: list[tuple[float, dict[str, Any]]] = []

        def set(self, value, attributes=None) -> None:
            self.calls.append((value, dict(attributes or {})))

    r._meter = _FakeMeter()
    r._started = True
    return r


def test_delta_tracker_first_sample_establishes_baseline():
    r = _make_registry_with_meter()
    # First sample: no delta pushed (we're establishing the zero point).
    r.update_rtp_session_stats(
        {'packets-received': 100, 'packets-lost': 0, 'sent-nack-count': 0,
         'jitter': 0, 'rb-round-trip': None},
        clock_rate=48000,
    )
    counter = r._meter.counters.get('openob.rtp.packets_received')
    assert counter is None or counter.calls == []


def test_delta_tracker_pushes_diffs():
    r = _make_registry_with_meter()
    r.update_rtp_session_stats(
        {'packets-received': 100}, clock_rate=48000,
    )
    r.update_rtp_session_stats(
        {'packets-received': 250}, clock_rate=48000,
    )
    counter = r._meter.counters['openob.rtp.packets_received']
    assert len(counter.calls) == 1
    delta, attrs = counter.calls[0]
    assert delta == 150
    assert attrs['link'] == 'test-link'
    assert attrs['role'] == 'rx'


def test_delta_tracker_handles_counter_reset():
    """When the underlying GStreamer counter resets to 0 on a pipeline
    restart, the next sample re-baselines without pushing a negative delta."""
    r = _make_registry_with_meter()
    r.update_rtp_session_stats({'packets-received': 100}, clock_rate=48000)
    r.update_rtp_session_stats({'packets-received': 250}, clock_rate=48000)
    # Pipeline restart: counter resets.
    r.update_rtp_session_stats({'packets-received': 0}, clock_rate=48000)
    # Next sample should produce a clean delta off the new baseline.
    r.update_rtp_session_stats({'packets-received': 50}, clock_rate=48000)

    counter = r._meter.counters['openob.rtp.packets_received']
    deltas = [d for d, _ in counter.calls]
    assert deltas == [150, 50]  # the reset's "negative" delta was dropped


def test_reset_counter_deltas_clears_cache():
    r = _make_registry_with_meter()
    r.update_rtp_session_stats({'packets-received': 100}, clock_rate=48000)
    assert _peek_last_values(r)  # cache populated
    r.reset_counter_deltas()
    assert _peek_last_values(r) == {}


def test_jitterbuffer_stats_counters():
    r = _make_registry_with_meter()
    r.update_jitterbuffer_stats({
        'num-pushed': 0, 'num-lost': 0, 'num-late': 0,
        'num-duplicates': 0, 'rtx-success-count': 0, 'rtx-rtt': 0,
    })
    r.update_jitterbuffer_stats({
        'num-pushed': 1000, 'num-lost': 5, 'num-late': 2,
        'num-duplicates': 0, 'rtx-success-count': 3, 'rtx-rtt': 25_000_000,
    })
    pushed = r._meter.counters['openob.jitterbuffer.pushed']
    lost = r._meter.counters['openob.jitterbuffer.lost']
    rtx_rtt_gauge = r._meter.gauges['openob.jitterbuffer.rtx_rtt_seconds']

    assert pushed.calls[0][0] == 1000
    assert lost.calls[0][0] == 5
    # ns → s conversion.
    assert rtx_rtt_gauge.calls[-1][0] == pytest.approx(0.025)


def test_audio_peak_gauge_carries_channel_attr():
    r = _make_registry_with_meter()
    r.set_audio_peak(0, -12.0)
    r.set_audio_peak(1, -10.0)
    gauge = r._meter.gauges['openob.audio.peak_dbfs']
    assert len(gauge.calls) == 2
    assert {c[1]['channel'] for c in gauge.calls} == {'0', '1'}


def test_record_event_passes_attrs():
    r = _make_registry_with_meter()
    r.record_caps_signal(result='auth_fail')
    counter = r._meter.counters['openob.caps_signal.received']
    assert len(counter.calls) == 1
    assert counter.calls[0][1]['result'] == 'auth_fail'


def test_safe_swallows_exceptions(caplog):
    """A method that internally throws should log at DEBUG and return None
    without propagating — the audio path must never see telemetry errors."""
    r = _make_registry_with_meter()

    class _ExplodingStruct:
        def get_value(self, _key):
            raise RuntimeError('boom')

    # Should not raise.
    with caplog.at_level(logging.DEBUG, logger='openob.telemetry.rx.test-link'):
        result = r.update_jitterbuffer_stats(_ExplodingStruct())
    assert result is None


# --- JSON formatter --------------------------------------------------------


def test_json_formatter_emits_valid_json():
    """Bare-event-tag message: the dotted-lowercase heuristic auto-tags
    the `event` field for aggregator grouping.
    """
    fmt = _JSONFormatter()
    record = logging.LogRecord(
        name='openob.foo', level=logging.INFO, pathname='', lineno=0,
        msg='pipeline.restart', args=(), exc_info=None,
    )
    record.link = 'stl-1'
    record.node = 'studio'
    record.role = 'tx'
    record.reason = 'ZeroDivisionError'
    out = fmt.format(record)
    parsed = json.loads(out)
    assert parsed['msg'] == 'pipeline.restart'
    assert parsed['event'] == 'pipeline.restart'  # auto-tagged from msg shape
    assert parsed['link'] == 'stl-1'
    assert parsed['node'] == 'studio'
    assert parsed['role'] == 'tx'
    assert parsed['reason'] == 'ZeroDivisionError'


def test_json_formatter_explicit_event_extra_wins():
    """A call site that emits a human-readable message AND sets
    extra={'event': '...'} preserves both: the readable msg goes through
    unchanged for text-mode operators, the event field comes from extra.
    """
    fmt = _JSONFormatter()
    record = logging.LogRecord(
        name='openob.foo', level=logging.INFO, pathname='', lineno=0,
        msg='Started stereo audio transmission', args=(), exc_info=None,
    )
    record.link = 'stl-1'
    record.node = 'studio'
    record.role = 'tx'
    record.event = 'pipeline.started'
    record.channels = 2
    parsed = json.loads(fmt.format(record))
    assert parsed['msg'] == 'Started stereo audio transmission'
    assert parsed['event'] == 'pipeline.started'
    assert parsed['channels'] == 2


def test_json_formatter_skips_dash_defaults():
    fmt = _JSONFormatter()
    record = logging.LogRecord(
        name='openob.foo', level=logging.INFO, pathname='', lineno=0,
        msg='hello', args=(), exc_info=None,
    )
    record.link = '-'
    record.node = '-'
    record.role = '-'
    out = fmt.format(record)
    parsed = json.loads(out)
    # Default placeholders are stripped so the JSON output stays clean
    # for non-link loggers (e.g. cli boot lines).
    assert 'link' not in parsed
    assert 'node' not in parsed
    assert 'role' not in parsed


def test_json_formatter_does_not_set_event_for_message_text():
    fmt = _JSONFormatter()
    record = logging.LogRecord(
        name='openob.foo', level=logging.INFO, pathname='', lineno=0,
        msg='Starting up transmitter', args=(), exc_info=None,
    )
    record.link = '-'
    record.node = '-'
    record.role = '-'
    parsed = json.loads(fmt.format(record))
    assert 'event' not in parsed


# --- LinkLoggerAdapter -----------------------------------------------------


def test_link_logger_adapter_injects_context():
    base = logging.getLogger('openob.test.adapter')
    adapter = LinkLoggerAdapter(
        base, extra={'link': 'L', 'node': 'N', 'role': 'R'},
    )
    msg, kwargs = adapter.process('hi', {})
    assert kwargs['extra']['link'] == 'L'
    assert kwargs['extra']['node'] == 'N'
    assert kwargs['extra']['role'] == 'R'


def test_link_logger_adapter_caller_extras_win():
    """A call site that explicitly passes ``extra={'role': 'override'}``
    should override the adapter's default — useful for one-off log lines."""
    base = logging.getLogger('openob.test.adapter2')
    adapter = LinkLoggerAdapter(
        base, extra={'link': 'L', 'node': 'N', 'role': 'tx'},
    )
    _, kwargs = adapter.process('hi', {'extra': {'role': 'rx'}})
    assert kwargs['extra']['role'] == 'rx'


def test_logger_factory_get_link_logger_returns_adapter():
    factory = LoggerFactory()
    adapter = factory.getLinkLogger(node='studio', link='stl-1', role='tx')
    assert isinstance(adapter, LinkLoggerAdapter)
    extra = adapter.extra or {}
    assert extra['link'] == 'stl-1'
    assert extra['node'] == 'studio'
    assert extra['role'] == 'tx'


def test_get_openob_version_returns_string():
    """Just shouldn't raise — version may be 'unknown' in dev installs."""
    v = get_openob_version()
    assert isinstance(v, str)
    assert len(v) > 0
