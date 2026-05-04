"""Optional OTLP metrics for OpenOB.

Off by default. Pulled in only when the user installs ``openob[telemetry]``
and sets ``telemetry_enabled = true`` + ``telemetry_otlp_endpoint = "..."``
in their link TOML (or via the matching CLI flags / env vars).

The module is a thin facade over the OpenTelemetry SDK:

- A :class:`TelemetryRegistry` exposes one public method per metric we
  care about (``set_audio_peak``, ``record_pipeline_restart``,
  ``update_rtp_session_stats``, …). Hook sites in :mod:`openob.node`,
  :mod:`openob.rtp.tx`, :mod:`openob.rtp.rx`, :mod:`openob.caps_signal`
  call those methods unconditionally.
- :meth:`TelemetryRegistry.disabled` returns a singleton whose public
  methods are no-ops, so call sites need no ``if self.telemetry:`` guard.
- :meth:`TelemetryRegistry.from_link` is the factory: with telemetry off
  it returns the disabled singleton; with telemetry on it lazy-imports
  ``opentelemetry.*`` and builds the SDK pipeline (MeterProvider →
  PeriodicExportingMetricReader → OTLPMetricExporter).

GStreamer counters (``rtprtxsend.num-rtx-requests``, jitterbuffer
``num-pushed`` etc.) reset to zero on every PLAYING transition, which would
make naive ``Counter.add(value)`` go backwards. We track per-(metric, attrs)
last values and push only positive deltas; on the PLAYING up-edge the link
supervisor calls :meth:`reset_counter_deltas` so the first post-restart
sample doesn't get skipped.

Every public method is wrapped in :func:`_safe` — telemetry must never
crash the audio path, so a failure in OTel internals or a malformed stats
struct degrades to a DEBUG log line.
"""
from __future__ import annotations

import logging
import math
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping

from openob._log_events import (
    EVT_TELEMETRY_CONFIGURED,
    EVT_TELEMETRY_DISABLED,
    EVT_TELEMETRY_ERROR,
)
from openob.logger import LoggerFactory


_DEFAULT_INTERVAL_SECONDS = 30
_MIN_INTERVAL_SECONDS = 5
# Counter-delta cache key. Plain tuple of (metric_name, sorted attr items)
# rather than a string so attribute values with embedded `=` or `,` can't
# cause aliasing.
_DeltaKey = tuple[str, tuple[tuple[str, str], ...]]


@dataclass(frozen=True)
class TelemetryConfig:
    """Telemetry settings extracted from a :class:`LinkConfig`.

    Mirrors the ``telemetry_*`` fields on LinkConfig; kept as its own
    dataclass so :class:`TelemetryRegistry` doesn't depend on the full
    LinkConfig (and so unit tests can build it without GStreamer in scope).
    """
    enabled: bool = False
    otlp_endpoint: str | None = None
    otlp_headers: str | None = None
    otlp_interval_seconds: int = _DEFAULT_INTERVAL_SECONDS
    otlp_timeout_seconds: int = 10
    otlp_compression: Literal['none', 'gzip'] = 'gzip'

    @classmethod
    def from_link(cls, link_config) -> 'TelemetryConfig':
        return cls(
            enabled=getattr(link_config, 'telemetry_enabled', False),
            otlp_endpoint=getattr(link_config, 'telemetry_otlp_endpoint', None),
            otlp_headers=getattr(link_config, 'telemetry_otlp_headers', None),
            otlp_interval_seconds=getattr(
                link_config, 'telemetry_otlp_interval_seconds',
                _DEFAULT_INTERVAL_SECONDS,
            ),
            otlp_timeout_seconds=getattr(
                link_config, 'telemetry_otlp_timeout_seconds', 10,
            ),
            otlp_compression=getattr(
                link_config, 'telemetry_otlp_compression', 'gzip',
            ),
        )


def _parse_otlp_headers(raw: str | None) -> dict[str, str]:
    """Parse a comma-separated ``Key=Value, Key2=Value2`` headers string.

    Matches the OTel ``OTEL_EXPORTER_OTLP_HEADERS`` convention. Empty input
    yields an empty dict; whitespace around keys/values is stripped.
    """
    if not raw:
        return {}
    out: dict[str, str] = {}
    for pair in raw.split(','):
        if '=' not in pair:
            continue
        k, v = pair.split('=', 1)
        k = k.strip()
        v = v.strip()
        if k:
            out[k] = v
    return out


def _ns_to_seconds(value: Any) -> float | None:
    """Best-effort ns → seconds converter; tolerates None / non-numeric."""
    if value is None:
        return None
    try:
        return float(value) / 1e9
    except (TypeError, ValueError):
        return None


def _struct_get(struct: Any, key: str) -> Any:
    """Fetch a field from a Gst.Structure-like object or a plain dict.

    PyGObject 3.50+ wraps Gst.Structure as ``StructureWrapper`` (dict-style
    access only); older PyGObject exposes ``get_value(key)``. Unit tests pass
    plain dicts. Try mapping access first (covers wrappers and dicts) before
    falling back to ``get_value``.
    """
    if struct is None:
        return None
    if isinstance(struct, Mapping):
        return struct.get(key)
    try:
        if key in struct:
            return struct[key]
    except (TypeError, AttributeError):
        pass
    getter = getattr(struct, 'get_value', None)
    if getter is None:
        return None
    try:
        return getter(key)
    except Exception:
        return None


def _safe(method: Callable) -> Callable:
    """Decorator: swallow any exception inside a public TelemetryRegistry
    method, log it at DEBUG, and return None. Telemetry must never bring
    the audio pipeline down.
    """
    name = method.__name__

    def wrapper(self: 'TelemetryRegistry', *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — telemetry boundary
            logger = getattr(self, '_logger', None)
            if logger is not None:
                logger.debug(
                    EVT_TELEMETRY_ERROR,
                    extra={'method': name, 'error': str(exc)},
                )
            return None
    wrapper.__name__ = name
    wrapper.__qualname__ = getattr(method, '__qualname__', name)
    return wrapper


@dataclass
class _Instruments:
    """Cache of OTel instrument objects keyed by metric name.

    Built lazily on first use — we don't want to instantiate every
    instrument upfront if a particular link never produces certain
    metrics (e.g. the TX-only ones on an RX node).
    """
    counters: dict[str, Any] = field(default_factory=dict)
    gauges: dict[str, Any] = field(default_factory=dict)


class TelemetryRegistry:
    """Facade over the OTel MeterProvider for one link-side process.

    Single instance per ``Node.run_link`` invocation. Wraps OTel SDK calls
    in ``_safe`` so a malformed stats struct or a network blip never
    propagates back into the audio pipeline.
    """

    _disabled_singleton: 'TelemetryRegistry | None' = None

    def __init__(
        self,
        *,
        config: TelemetryConfig,
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
        self._started = False
        self._provider = None
        self._meter = None
        self._instruments = _Instruments()
        self._last_value: dict[_DeltaKey, float] = {}
        self._lock = threading.Lock()
        self._logger_factory = LoggerFactory()
        self._logger = self._logger_factory.getLogger(f'telemetry.{role}.{link_name}')
        # Common attributes merged into every emission.
        self._common_attrs: dict[str, str] = {
            'link': link_name,
            'node': node_name,
            'role': role,
            'version': version,
        }

    # ------------------------------------------------------------ factories

    @classmethod
    def disabled(cls) -> 'TelemetryRegistry':
        """Return the singleton no-op registry.

        Returned by :meth:`from_link` whenever telemetry is off (or its
        config is incomplete). Every public method is wrapped in ``_safe``
        and additionally short-circuits on ``self._enabled is False``.
        """
        if cls._disabled_singleton is None:
            cls._disabled_singleton = cls(
                config=TelemetryConfig(),
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
    ) -> 'TelemetryRegistry':
        """Build a registry for a link, or return ``disabled()`` if off.

        Off when:
        - ``telemetry_enabled`` is False, OR
        - ``telemetry_otlp_endpoint`` is None / empty (nowhere to push to).
        """
        cfg = TelemetryConfig.from_link(link_config)
        if not cfg.enabled:
            return cls.disabled()
        if not cfg.otlp_endpoint:
            disabled = cls.disabled()
            disabled._logger.warning(
                EVT_TELEMETRY_DISABLED,
                extra={
                    'reason': 'no_otlp_endpoint',
                    'link': link_config.name,
                    'role': role,
                },
            )
            return disabled
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
        """Build the OTel MeterProvider and register the OTLP exporter.

        No-op on the disabled singleton. Idempotent — repeated calls after
        the first do nothing (the link supervisor's restart loop calls
        ``start()`` once per ``run_link`` invocation, but the registry
        survives across pipeline-internal restarts).
        """
        if not self._enabled or self._started:
            return
        try:
            self._build_provider()
            self._started = True
            self._logger.info(
                EVT_TELEMETRY_CONFIGURED,
                extra={
                    'otlp_endpoint': self._config.otlp_endpoint,
                    'interval_seconds': self._config.otlp_interval_seconds,
                    'compression': self._config.otlp_compression,
                    'role': self._role,
                },
            )
            # Build_info is set once at start so the operator can confirm
            # which version is running just by querying the metric.
            self._set_gauge('openob.build_info', 1.0, extra_attrs=None)
        except Exception as exc:  # noqa: BLE001 — startup failures must not crash
            self._logger.warning(
                EVT_TELEMETRY_ERROR,
                extra={'method': 'start', 'error': str(exc)},
            )
            # Demote to disabled for the rest of this run so the hook
            # call sites no-op rather than re-attempting on every poll.
            self._enabled = False

    def stop(self) -> None:
        """Flush and shut down the MeterProvider.

        Bounded shutdown timeout (2s) so a wedged collector never blocks
        the link's process exit.
        """
        if not self._started:
            return
        try:
            if self._provider is not None:
                self._provider.shutdown(timeout_millis=2000)
        except Exception as exc:  # noqa: BLE001
            self._logger.debug(
                EVT_TELEMETRY_ERROR,
                extra={'method': 'stop', 'error': str(exc)},
            )
        finally:
            self._started = False

    def _build_provider(self) -> None:
        """Lazy-import OTel and construct the MeterProvider.

        Kept in its own method so the disabled path (and unit tests
        exercising the no-op singleton) never triggers the OTel imports.
        """
        from opentelemetry import metrics  # noqa: PLC0415
        from opentelemetry.exporter.otlp.proto.http import Compression  # noqa: PLC0415
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (  # noqa: PLC0415
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider  # noqa: PLC0415
        from opentelemetry.sdk.metrics.export import (  # noqa: PLC0415
            PeriodicExportingMetricReader,
        )
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415

        headers = _parse_otlp_headers(self._config.otlp_headers)
        # OTLPMetricExporter takes a Compression enum, not a string —
        # passing 'gzip' triggers a confusing AttributeError deep in the
        # SDK ("str has no attribute 'value'").
        compression = (
            Compression.Gzip if self._config.otlp_compression == 'gzip'
            else Compression.NoCompression
        )

        exporter = OTLPMetricExporter(
            endpoint=self._config.otlp_endpoint,
            headers=headers or None,
            timeout=self._config.otlp_timeout_seconds,
            compression=compression,
        )
        reader = PeriodicExportingMetricReader(
            exporter,
            export_interval_millis=self._config.otlp_interval_seconds * 1000,
            export_timeout_millis=self._config.otlp_timeout_seconds * 1000,
        )
        resource = Resource.create({
            'service.name': 'openob',
            'service.instance.id': f'{self._node_name}-{self._link_name}-{self._role}',
            'service.version': self._version,
            'openob.link': self._link_name,
            'openob.node': self._node_name,
            'openob.role': self._role,
        })
        self._provider = MeterProvider(
            resource=resource,
            metric_readers=[reader],
        )
        # Use a private MeterProvider rather than the global one so two
        # registries inside one process (the e2e test runs both rx + tx
        # in-process under some configurations) don't collide.
        self._meter = self._provider.get_meter('openob', self._version)
        # Stash the api module for instrument creation.
        self._metrics_api = metrics

    # ------------------------------------------------------------ instrument cache

    def _get_counter(self, name: str, *, unit: str = '1', description: str = '') -> Any:
        if name in self._instruments.counters:
            return self._instruments.counters[name]
        if self._meter is None:
            return None
        counter = self._meter.create_counter(
            name=name, unit=unit, description=description,
        )
        self._instruments.counters[name] = counter
        return counter

    def _get_gauge(self, name: str, *, unit: str = '1', description: str = '') -> Any:
        if name in self._instruments.gauges:
            return self._instruments.gauges[name]
        if self._meter is None:
            return None
        # Gauge instrument was added in OTel Python SDK 1.28; we pin >=1.28
        # in pyproject so this is always available.
        gauge = self._meter.create_gauge(
            name=name, unit=unit, description=description,
        )
        self._instruments.gauges[name] = gauge
        return gauge

    # ------------------------------------------------------------ helpers

    def _attrs(self, extra_attrs: Mapping[str, Any] | None) -> dict[str, str]:
        attrs = dict(self._common_attrs)
        if extra_attrs:
            for k, v in extra_attrs.items():
                if v is None:
                    continue
                attrs[k] = str(v)
        return attrs

    def _set_gauge(
        self,
        name: str,
        value: float | int | None,
        *,
        extra_attrs: Mapping[str, Any] | None,
        unit: str = '1',
    ) -> None:
        if value is None:
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        if math.isnan(v) or math.isinf(v):
            return
        gauge = self._get_gauge(name, unit=unit)
        if gauge is None:
            return
        gauge.set(v, attributes=self._attrs(extra_attrs))

    def _add_counter_delta(
        self,
        name: str,
        cumulative: float | int | None,
        *,
        extra_attrs: Mapping[str, Any] | None,
        unit: str = '1',
    ) -> None:
        """Push the delta against the previous cumulative value.

        Skips when ``cumulative`` is None / non-numeric. On a counter
        regression (current < last — happens when GStreamer resets to
        zero on a pipeline restart) we update the cache to the new
        baseline and push nothing for this sample, so the next call
        produces a normal positive delta.
        """
        if cumulative is None:
            return
        try:
            current = float(cumulative)
        except (TypeError, ValueError):
            return
        if math.isnan(current) or math.isinf(current) or current < 0:
            return
        attrs = self._attrs(extra_attrs)
        attr_key = tuple(sorted(attrs.items()))
        key: _DeltaKey = (name, attr_key)
        with self._lock:
            last = self._last_value.get(key)
            if last is None:
                # First sample for this (metric, attrs) — establish baseline
                # without pushing. Operators see counts begin at zero on
                # subsequent samples, which matches the Prometheus convention.
                self._last_value[key] = current
                delta = 0.0
            elif current < last:
                # Counter reset (pipeline restart). Establish new baseline.
                self._last_value[key] = current
                delta = 0.0
            else:
                delta = current - last
                self._last_value[key] = current
        if delta <= 0:
            return
        counter = self._get_counter(name, unit=unit)
        if counter is None:
            return
        counter.add(delta, attributes=attrs)

    def _record_event(
        self,
        name: str,
        *,
        extra_attrs: Mapping[str, Any] | None = None,
        amount: int = 1,
        unit: str = '1',
    ) -> None:
        """Increment an event-counter by ``amount`` (no delta tracking).

        Used for events that we count directly rather than poll — restarts,
        UDP timeouts, decryption failures, caps-signal arrivals.
        """
        counter = self._get_counter(name, unit=unit)
        if counter is None:
            return
        counter.add(amount, attributes=self._attrs(extra_attrs))

    # ------------------------------------------------------------ public API

    @_safe
    def reset_counter_deltas(self) -> None:
        """Drop the per-metric last-value cache.

        Called by the link supervisor on a PLAYING up-edge so the first
        post-restart sample of each polled counter establishes a fresh
        baseline rather than producing a giant negative-then-zero delta.
        """
        if not self._enabled:
            return
        with self._lock:
            self._last_value.clear()

    @_safe
    def set_link_up(self, up: bool) -> None:
        if not self._enabled:
            return
        self._set_gauge('openob.link.up', 1.0 if up else 0.0, extra_attrs=None)

    @_safe
    def record_pipeline_restart(self, *, reason: str) -> None:
        if not self._enabled:
            return
        self._record_event(
            'openob.pipeline.restarts',
            extra_attrs={'reason': reason},
        )

    @_safe
    def set_pipeline_uptime(self, seconds: float) -> None:
        if not self._enabled:
            return
        self._set_gauge(
            'openob.pipeline.uptime',
            seconds,
            extra_attrs=None,
            unit='s',
        )

    @_safe
    def set_audio_peak(self, channel: int, dbfs: float) -> None:
        if not self._enabled:
            return
        self._set_gauge(
            'openob.audio.peak_dbfs',
            dbfs,
            extra_attrs={'channel': channel},
            unit='dB',
        )

    @_safe
    def set_audio_rms(self, channel: int, dbfs: float) -> None:
        if not self._enabled:
            return
        self._set_gauge(
            'openob.audio.rms_dbfs',
            dbfs,
            extra_attrs={'channel': channel},
            unit='dB',
        )

    @_safe
    def update_rtp_session_stats(
        self,
        stats: Any,
        *,
        clock_rate: int | None = None,
    ) -> None:
        """Push counters/gauges from a GstRTPSession ``stats`` structure.

        ``stats`` may be a ``Gst.Structure`` (production) or a plain dict
        (unit tests). All fields are best-effort — if rtpsession hasn't
        seen its first packet yet the structure exists but most fields
        are zero, which is fine.
        """
        if not self._enabled or stats is None:
            return
        if self._role == 'tx':
            self._add_counter_delta(
                'openob.rtp.packets_sent',
                _struct_get(stats, 'packets-sent'),
                extra_attrs=None,
            )
        elif self._role == 'rx':
            self._add_counter_delta(
                'openob.rtp.packets_received',
                _struct_get(stats, 'packets-received'),
                extra_attrs=None,
            )
            self._add_counter_delta(
                'openob.rtp.packets_lost',
                _struct_get(stats, 'packets-lost'),
                extra_attrs=None,
            )
            self._add_counter_delta(
                'openob.rtx.nacks_sent',
                _struct_get(stats, 'sent-nack-count'),
                extra_attrs=None,
            )
        # Jitter is in RTP clock units; convert with the negotiated
        # clock-rate (48000 for opus, samplerate for pcm). Skip if we
        # weren't told the rate.
        jitter_clock = _struct_get(stats, 'jitter')
        if jitter_clock is not None and clock_rate:
            try:
                self._set_gauge(
                    'openob.rtp.jitter_seconds',
                    float(jitter_clock) / float(clock_rate),
                    extra_attrs=None,
                    unit='s',
                )
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        rtt_ns = _struct_get(stats, 'rb-round-trip')
        rtt_s = _ns_to_seconds(rtt_ns)
        if rtt_s is not None:
            self._set_gauge(
                'openob.rtp.round_trip_seconds',
                rtt_s,
                extra_attrs=None,
                unit='s',
            )

    @_safe
    def update_jitterbuffer_stats(self, stats: Any) -> None:
        if not self._enabled or stats is None:
            return
        self._add_counter_delta(
            'openob.jitterbuffer.pushed',
            _struct_get(stats, 'num-pushed'),
            extra_attrs=None,
        )
        self._add_counter_delta(
            'openob.jitterbuffer.lost',
            _struct_get(stats, 'num-lost'),
            extra_attrs=None,
        )
        self._add_counter_delta(
            'openob.jitterbuffer.late',
            _struct_get(stats, 'num-late'),
            extra_attrs=None,
        )
        self._add_counter_delta(
            'openob.jitterbuffer.duplicates',
            _struct_get(stats, 'num-duplicates'),
            extra_attrs=None,
        )
        self._add_counter_delta(
            'openob.jitterbuffer.rtx_success',
            _struct_get(stats, 'rtx-success-count'),
            extra_attrs=None,
        )
        rtx_rtt_s = _ns_to_seconds(_struct_get(stats, 'rtx-rtt'))
        if rtx_rtt_s is not None:
            self._set_gauge(
                'openob.jitterbuffer.rtx_rtt_seconds',
                rtx_rtt_s,
                extra_attrs=None,
                unit='s',
            )

    @_safe
    def update_opusdec_stats(self, stats: Any) -> None:
        if not self._enabled or stats is None:
            return
        self._add_counter_delta(
            'openob.opus.plc_samples',
            _struct_get(stats, 'plc-num-samples'),
            extra_attrs=None,
        )
        plc_dur_s = _ns_to_seconds(_struct_get(stats, 'plc-duration'))
        if plc_dur_s is not None and plc_dur_s >= 0:
            # plc-duration on opusdec is cumulative-since-start, so feed
            # it through the delta tracker like any other counter.
            # Multiply to ms-of-resolution before flooring back down to
            # avoid losing very-small deltas to float→counter rounding;
            # OTel counters accept floats, so we just push seconds.
            self._add_counter_delta(
                'openob.opus.plc_seconds',
                plc_dur_s,
                extra_attrs=None,
                unit='s',
            )

    @_safe
    def update_rtx_send_stats(self, *, requests: int, packets: int) -> None:
        if not self._enabled:
            return
        self._add_counter_delta(
            'openob.rtx.nack_requests',
            requests,
            extra_attrs=None,
        )
        self._add_counter_delta(
            'openob.rtx.packets_resent',
            packets,
            extra_attrs=None,
        )

    @_safe
    def add_path_bytes(self, *, label: str, bytes_served: int) -> None:
        if not self._enabled:
            return
        self._add_counter_delta(
            'openob.tx.path_bytes_sent',
            bytes_served,
            extra_attrs={'path': label},
            unit='By',
        )

    @_safe
    def record_data_timeout(self) -> None:
        if not self._enabled:
            return
        self._record_event('openob.rx.data_timeout')

    @_safe
    def record_caps_signal(self, *, result: str) -> None:
        if not self._enabled:
            return
        self._record_event(
            'openob.caps_signal.received',
            extra_attrs={'result': result},
        )

    @_safe
    def record_srtp_failure(self) -> None:
        if not self._enabled:
            return
        self._record_event('openob.srtp.decrypt_failures')


# Convenience accessor used by tests + cli to surface the running version
# without having to import importlib.metadata at every call site.
def get_openob_version() -> str:
    """Return the installed OpenOB package version, or ``'unknown'``."""
    try:
        from importlib.metadata import version  # noqa: PLC0415
        return version('OpenOB')
    except Exception:
        return os.environ.get('OPENOB_VERSION', 'unknown')


# Public helper for the unit tests + e2e harness to inspect the
# delta-tracker state without poking at private attrs.
def _peek_last_values(registry: TelemetryRegistry) -> dict[_DeltaKey, float]:
    return dict(registry._last_value)


__all__ = [
    'TelemetryConfig',
    'TelemetryRegistry',
    'get_openob_version',
]


# Silence the unused-import warning when type-checkers run without the
# OTel package installed.
_ = logging
