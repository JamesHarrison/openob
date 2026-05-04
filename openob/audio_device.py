"""
Native audio device discovery and resolution for the v6.3+ ``coreaudio`` /
``wasapi`` / ``pulse`` backends.

The Linux ALSA path (``audio_input = "alsa"`` + ``alsa_device = "hw:N"``)
predates this module and stays untouched. New backends use a single unified
``audio_device`` field that accepts either a case-insensitive substring of
the device's display name OR an exact native device id; resolution happens
at pipeline-build time via :class:`Gst.DeviceMonitor` so a missing or
ambiguous match fails fast with every candidate listed.

Display-name matching is the recommended path. Native ids (CoreAudio
``AudioDeviceID`` uints, WASAPI MMDevice paths) churn across reboots and
USB replugs; display names don't, which means the supervisor restart loop
in :mod:`openob.node` recovers automatically when a USB device comes back
on a different id.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal


def _gst():
    """Lazy GStreamer accessor.

    Module-top ``import gi`` would fail on Windows test runners without
    PyGObject installed, even when only the dataclass and matching logic
    is exercised (with Gst.DeviceMonitor mocked). Pulling the import
    inside this getter keeps imports cheap and the module loadable on
    every platform.
    """
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst  # noqa: E402
    Gst.init(None)
    return Gst


# Map our backend label → (source factory, sink factory). The factory name
# is also how we filter Gst.Device entries that DeviceMonitor returns: every
# Audio/Source or Audio/Sink device exposes the factory it was discovered
# from, and that's the most portable backend identifier across Gst.Device
# property layouts.
_BACKEND_FACTORIES: dict[str, tuple[str, str]] = {
    'coreaudio': ('osxaudiosrc', 'osxaudiosink'),
    'wasapi':    ('wasapi2src', 'wasapi2sink'),
    'pulse':     ('pulsesrc', 'pulsesink'),
    'alsa':      ('alsasrc', 'alsasink'),
}


# Headless-Linux nudge for the "0 candidates for backend pulse" case.
_NO_CANDIDATES_HINT: dict[str, str] = {
    'pulse': (
        "PulseAudio doesn't appear to be running (or pulsesrc/pulsesink "
        "isn't installed — apt: gstreamer1.0-pulseaudio). For a headless "
        "Linux box without Pulse, use audio_input = \"alsa\" + alsa_device "
        "= \"hw:N\" instead."
    ),
    'coreaudio': (
        "No CoreAudio devices found. Is gst-plugins-good installed "
        "(brew install gst-plugins-good) and is at least one audio "
        "interface visible to Audio MIDI Setup?"
    ),
    'wasapi': (
        "No WASAPI devices found. Is the GStreamer MSI installed (with "
        "gst-plugins-good) and at least one audio device enabled in the "
        "Windows Sound control panel?"
    ),
    'alsa': (
        "No ALSA devices found via Gst.DeviceMonitor. `aplay -l` and `arecord "
        "-l` will show what the kernel sees; `alsa_device = \"hw:N\"` works "
        "even when DeviceMonitor returns nothing."
    ),
}


@dataclass(frozen=True)
class ResolvedDevice:
    """A single audio device matched by :func:`resolve_audio_device`.

    ``gst_device`` is an opaque handle that the pipeline-build path passes
    to :py:meth:`Gst.Device.create_element` — that returns an element with
    the right backend's ``device`` property pre-set (uint for CoreAudio,
    string for wasapi2/pulse), so one branch in tx.py/rx.py covers all
    three backends.
    """

    backend: str  # 'coreaudio'|'wasapi'|'pulse'|'alsa'
    direction: Literal['source', 'sink']
    display_name: str
    native_id: str
    is_default: bool
    gst_device: object  # Gst.Device — not annotated to keep the import surface clean for callers


class AudioDeviceError(ValueError):
    """Raised when an audio device cannot be resolved unambiguously.

    Inherits from ValueError so the existing ConfigError-style except
    blocks in cli.py still catch it cleanly when callers want one.
    """


def list_audio_devices(
    *,
    backend: str | None = None,
    direction: Literal['source', 'sink', 'both'] = 'both',
) -> list[ResolvedDevice]:
    """Enumerate audio devices visible via :class:`Gst.DeviceMonitor`.

    ``backend`` filters to one of ``coreaudio``/``wasapi``/``pulse``/``alsa``;
    None returns devices for every recognised backend. ``direction`` filters
    Source vs Sink. The ``is_default`` flag is set per-direction from the
    Gst.Device's ``is-default`` property when the backend exposes it.
    """
    if backend is not None and backend not in _BACKEND_FACTORIES:
        raise AudioDeviceError(
            f'unknown backend {backend!r}; expected one of '
            f'{sorted(_BACKEND_FACTORIES)}'
        )

    devices: list[ResolvedDevice] = []
    if direction in ('source', 'both'):
        devices.extend(_enumerate('Audio/Source', 'source', backend))
    if direction in ('sink', 'both'):
        devices.extend(_enumerate('Audio/Sink', 'sink', backend))
    return devices


def resolve_audio_device(
    backend: str,
    direction: Literal['source', 'sink'],
    name_or_id: str | None,
    *,
    logger: logging.Logger,
) -> ResolvedDevice | None:
    """Match ``name_or_id`` against discovered devices for ``backend``.

    Returns ``None`` when ``name_or_id`` is None — the caller then builds
    a bare element with no ``device`` set so the backend's own default
    kicks in. Avoids needing DeviceMonitor on stripped systems where it
    might flake.

    Raises :class:`AudioDeviceError` when:

    - the backend has zero candidate devices (with a hint about the likely
      cause — pulse without pulseaudio running, etc.);
    - ``name_or_id`` matches zero devices (lists every candidate so the
      operator sees what's actually plugged in vs what they typed);
    - ``name_or_id`` matches more than one device (lists each match so
      the operator can pick a longer substring).
    """
    if backend not in _BACKEND_FACTORIES:
        raise AudioDeviceError(
            f'unknown backend {backend!r}; expected one of '
            f'{sorted(_BACKEND_FACTORIES)}'
        )

    if name_or_id is None:
        return None

    candidates = list_audio_devices(backend=backend, direction=direction)
    if not candidates:
        hint = _NO_CANDIDATES_HINT.get(backend, '')
        raise AudioDeviceError(
            f'no {backend} {direction} devices visible to Gst.DeviceMonitor.'
            + (f' {hint}' if hint else '')
        )

    needle = name_or_id.casefold()
    substring_hits = [
        d for d in candidates if needle in d.display_name.casefold()
    ]
    if len(substring_hits) == 1:
        chosen = substring_hits[0]
    elif len(substring_hits) > 1:
        raise AudioDeviceError(
            f'audio_device={name_or_id!r} matches {len(substring_hits)} '
            f'{backend} {direction}s — pick a longer substring or use the '
            f'native id:\n' + _format_candidates(substring_hits)
        )
    else:
        # No substring hit; fall through to exact native-id match.
        id_hits = [d for d in candidates if d.native_id == name_or_id]
        if len(id_hits) == 1:
            chosen = id_hits[0]
        else:
            raise AudioDeviceError(
                f'audio_device={name_or_id!r} matched no {backend} {direction} '
                f'device. Available:\n' + _format_candidates(candidates)
            )

    logger.info(
        '%s %s resolved: %r (id=%s%s)',
        backend, direction, chosen.display_name, chosen.native_id,
        ', default' if chosen.is_default else '',
    )
    return chosen


# --- internals -------------------------------------------------------------


def _enumerate(
    class_filter: str,
    direction: Literal['source', 'sink'],
    backend_filter: str | None,
) -> list[ResolvedDevice]:
    """Walk a fresh Gst.DeviceMonitor for one Audio/Source or Audio/Sink class."""
    Gst = _gst()
    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter(class_filter, None)
    # Some GStreamer builds emit warnings on start() if no plugins are
    # installed; that's fine, the device list will simply be empty.
    started = monitor.start()
    try:
        gst_devices = monitor.get_devices() or []
    finally:
        if started:
            monitor.stop()

    out: list[ResolvedDevice] = []
    for dev in gst_devices:
        factory_name = _factory_name_for(dev)
        if factory_name is None:
            continue
        backend = _backend_for_factory(factory_name)
        if backend is None:
            continue
        if backend_filter is not None and backend != backend_filter:
            continue
        out.append(ResolvedDevice(
            backend=backend,
            direction=direction,
            display_name=dev.get_display_name() or '<unnamed>',
            native_id=_native_id_for(dev, backend),
            is_default=_is_default(dev),
            gst_device=dev,
        ))
    return out


def _factory_name_for(dev) -> str | None:
    """Return the GStreamer factory the device would build elements from.

    Most reliable backend identifier — Gst.Device's properties layout is
    backend-specific, but every device has exactly one factory.
    """
    # Newer GStreamer exposes get_element_factory() directly. On older builds
    # we fall back to creating a throwaway element and reading its factory.
    factory = None
    getter = getattr(dev, 'get_element_factory', None)
    if getter is not None:
        try:
            factory = getter()
        except Exception:  # pragma: no cover — defensive
            factory = None
    if factory is None:
        try:
            elem = dev.create_element(None)
        except Exception:  # pragma: no cover — defensive
            return None
        if elem is None:
            return None
        factory = elem.get_factory()
    if factory is None:
        return None
    return factory.get_name()


def _backend_for_factory(factory_name: str) -> str | None:
    """Reverse-lookup ``coreaudio``/``wasapi``/``pulse``/``alsa`` from factory name."""
    for backend, (src, sink) in _BACKEND_FACTORIES.items():
        if factory_name in (src, sink):
            return backend
    return None


def _native_id_for(dev, backend: str) -> str:
    """Best-effort extraction of the backend-specific device id, as a string.

    Per-backend priority — every Gst.Device exposes its id through a
    different property key, and on older plugin builds some keys are
    missing. We try the canonical key first and fall through to a generic
    set when it's absent.
    """
    props = dev.get_properties()
    if props is None:
        return dev.get_display_name() or '<unknown>'
    # Canonical key per backend (what the upstream GStreamer plugin sets):
    #   wasapi2 → device.strid (MMDevice path)
    #   pulse   → device.id    (Pulse sink/source name)
    #   coreaudio → device.id  (uint AudioDeviceID, sometimes a string)
    #   alsa    → device.id    (e.g. "hw:0,0")
    primary_keys = {
        'wasapi':    ('device.strid', 'device.id'),
        'pulse':     ('device.id', 'device.path'),
        'coreaudio': ('device.id', 'device.unique-id'),
        'alsa':      ('device.id', 'device.path'),
    }.get(backend, ('device.id', 'device.strid', 'device.path'))
    for key in primary_keys:
        try:
            value = props.get_string(key)
        except Exception:  # pragma: no cover — depends on Gst build
            value = None
        if value:
            return value
    # CoreAudio AudioDeviceID may be exposed as an int rather than a string;
    # render it as a decimal string so users have something stable to copy.
    for key in primary_keys:
        try:
            ok, n = props.get_int(key)
        except Exception:  # pragma: no cover
            ok, n = False, 0
        if ok:
            return str(n)
    return dev.get_display_name() or '<unknown>'


def _is_default(dev) -> bool:
    """Read the ``is-default`` Gst.Device property when the backend exposes it."""
    props = dev.get_properties()
    if props is None:
        return False
    try:
        ok, val = props.get_boolean('is-default')
    except Exception:  # pragma: no cover
        return False
    return bool(ok and val)


def _format_candidates(devices: list[ResolvedDevice]) -> str:
    """Pretty-print a candidate list for error messages."""
    lines = []
    for d in devices:
        marker = ' [default]' if d.is_default else ''
        lines.append(
            f'  - {d.display_name!r} (id={d.native_id}){marker}'
        )
    return '\n'.join(lines)
