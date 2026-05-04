"""Unit tests for openob.audio_device.

Real device enumeration is platform-specific and untestable in the docker-only
CI, so every test here mocks the Gst.DeviceMonitor that openob.audio_device
imports. The mock symbol patched is the bound name `Gst` inside the module,
not the global `gi.repository.Gst` — patching the latter would fight the
import system.
"""
from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import pytest

from openob.audio_device import (
    AudioDeviceError,
    list_audio_devices,
    resolve_audio_device,
)


# --- fakes ----------------------------------------------------------------


class _FakeFactory:
    def __init__(self, name: str) -> None:
        self._name = name

    def get_name(self) -> str:
        return self._name


class _FakeProperties:
    """Minimal stand-in for Gst.Structure that the resolver consults."""

    def __init__(self, **kv: Any) -> None:
        self._kv = kv

    def get_string(self, key: str) -> str | None:
        v = self._kv.get(key)
        return str(v) if isinstance(v, str) else None

    def get_int(self, key: str) -> tuple[bool, int]:
        v = self._kv.get(key)
        return (True, int(v)) if isinstance(v, int) else (False, 0)

    def get_boolean(self, key: str) -> tuple[bool, bool]:
        v = self._kv.get(key)
        return (True, bool(v)) if isinstance(v, bool) else (False, False)


class _FakeGstDevice:
    """Stand-in for Gst.Device — the resolver only calls a handful of methods."""

    def __init__(
        self,
        *,
        display_name: str,
        factory_name: str,
        native_id: str,
        is_default: bool = False,
        device_class: str = 'Audio/Source',
    ) -> None:
        self._display = display_name
        self._factory = _FakeFactory(factory_name)
        self._props = _FakeProperties(**{
            'device.id': native_id,
            'device.strid': native_id if 'wasapi' in factory_name else None,
            'is-default': is_default,
        })
        self._class = device_class

    def get_display_name(self) -> str:
        return self._display

    def get_element_factory(self) -> _FakeFactory:
        return self._factory

    def get_properties(self) -> _FakeProperties:
        return self._props

    def get_device_class(self) -> str:
        return self._class

    def create_element(self, name: str | None = None):  # pragma: no cover — not called by tests
        del name
        return object()


class _FakeMonitor:
    """Stand-in for Gst.DeviceMonitor — only the methods the resolver calls."""

    devices_by_class: dict[str, list[_FakeGstDevice]] = {}

    def __init__(self) -> None:
        self._class_filter: str | None = None

    @classmethod
    def new(cls) -> '_FakeMonitor':
        return cls()

    def add_filter(self, class_filter: str, _caps: Any) -> None:
        self._class_filter = class_filter

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        pass

    def get_devices(self) -> list[_FakeGstDevice]:
        return self.devices_by_class.get(self._class_filter or '', [])


class _FakeGst:
    """Minimal Gst stand-in returned by the patched _gst() accessor."""
    DeviceMonitor = _FakeMonitor


@pytest.fixture
def patched_monitor():
    """Patch the lazy `_gst()` accessor inside openob.audio_device.

    The module imports `gi` lazily so it loads on Windows test runners
    without PyGObject; the resolver and lister go through `_gst()` for
    every Gst.* lookup.
    """
    with patch('openob.audio_device._gst', return_value=_FakeGst):
        # Reset device tables for test isolation.
        _FakeMonitor.devices_by_class = {}
        yield


# --- helpers --------------------------------------------------------------


def _set_devices(*, sources: list[_FakeGstDevice] | None = None,
                 sinks: list[_FakeGstDevice] | None = None) -> None:
    _FakeMonitor.devices_by_class['Audio/Source'] = list(sources or [])
    _FakeMonitor.devices_by_class['Audio/Sink'] = list(sinks or [])


# --- tests ----------------------------------------------------------------


def test_resolve_by_substring_case_insensitive(patched_monitor):
    _set_devices(sources=[
        _FakeGstDevice(display_name='Built-in Microphone',
                       factory_name='osxaudiosrc', native_id='49',
                       is_default=True),
        _FakeGstDevice(display_name='Scarlett 2i2 USB',
                       factory_name='osxaudiosrc', native_id='72'),
    ])
    logger = logging.getLogger('test')
    chosen = resolve_audio_device('coreaudio', 'source', 'scarl', logger=logger)
    assert chosen is not None
    assert chosen.display_name == 'Scarlett 2i2 USB'
    assert chosen.native_id == '72'
    assert chosen.is_default is False


def test_resolve_by_native_id_fallback(patched_monitor):
    _set_devices(sources=[
        _FakeGstDevice(display_name='Built-in Microphone',
                       factory_name='osxaudiosrc', native_id='49'),
        _FakeGstDevice(display_name='Scarlett 2i2 USB',
                       factory_name='osxaudiosrc', native_id='72'),
    ])
    logger = logging.getLogger('test')
    chosen = resolve_audio_device('coreaudio', 'source', '72', logger=logger)
    assert chosen is not None
    assert chosen.display_name == 'Scarlett 2i2 USB'


def test_resolve_returns_none_when_name_is_none(patched_monitor):
    _set_devices(sources=[
        _FakeGstDevice(display_name='Built-in Microphone',
                       factory_name='osxaudiosrc', native_id='49'),
    ])
    logger = logging.getLogger('test')
    assert resolve_audio_device('coreaudio', 'source', None, logger=logger) is None


def test_resolve_no_match_lists_candidates(patched_monitor):
    _set_devices(sources=[
        _FakeGstDevice(display_name='Built-in Microphone',
                       factory_name='osxaudiosrc', native_id='49'),
        _FakeGstDevice(display_name='Scarlett 2i2 USB',
                       factory_name='osxaudiosrc', native_id='72'),
    ])
    logger = logging.getLogger('test')
    with pytest.raises(AudioDeviceError) as excinfo:
        resolve_audio_device('coreaudio', 'source', 'Yamaha', logger=logger)
    msg = str(excinfo.value)
    assert "'Yamaha'" in msg
    assert 'Built-in Microphone' in msg
    assert 'Scarlett 2i2 USB' in msg


def test_resolve_multiple_matches_listed(patched_monitor):
    _set_devices(sources=[
        _FakeGstDevice(display_name='USB Audio Device A',
                       factory_name='wasapi2src', native_id='\\\\?\\dev_a'),
        _FakeGstDevice(display_name='USB Audio Device B',
                       factory_name='wasapi2src', native_id='\\\\?\\dev_b'),
    ])
    logger = logging.getLogger('test')
    with pytest.raises(AudioDeviceError) as excinfo:
        resolve_audio_device('wasapi', 'source', 'USB', logger=logger)
    msg = str(excinfo.value)
    assert 'matches 2' in msg
    assert 'USB Audio Device A' in msg
    assert 'USB Audio Device B' in msg


def test_resolve_zero_candidates_for_pulse_suggests_alsa(patched_monitor):
    _set_devices(sources=[])
    logger = logging.getLogger('test')
    with pytest.raises(AudioDeviceError) as excinfo:
        resolve_audio_device('pulse', 'source', 'usb', logger=logger)
    msg = str(excinfo.value)
    assert 'pulse' in msg.lower()
    assert 'alsa' in msg.lower()


def test_resolve_unknown_backend_rejected(patched_monitor):
    logger = logging.getLogger('test')
    with pytest.raises(AudioDeviceError) as excinfo:
        resolve_audio_device('made-up-backend', 'source', 'x', logger=logger)
    assert 'made-up-backend' in str(excinfo.value)


def test_list_audio_devices_filters_by_backend(patched_monitor):
    _set_devices(sources=[
        _FakeGstDevice(display_name='Built-in Microphone',
                       factory_name='osxaudiosrc', native_id='49'),
        _FakeGstDevice(display_name='USB Mic',
                       factory_name='pulsesrc', native_id='alsa_input.usb'),
    ])
    devices = list_audio_devices(backend='coreaudio')
    assert len(devices) == 1
    assert devices[0].backend == 'coreaudio'


def test_list_audio_devices_filters_by_direction(patched_monitor):
    _set_devices(
        sources=[
            _FakeGstDevice(display_name='Mic A', factory_name='pulsesrc',
                           native_id='alsa_input.a'),
        ],
        sinks=[
            _FakeGstDevice(display_name='Speaker B', factory_name='pulsesink',
                           native_id='alsa_output.b', device_class='Audio/Sink'),
        ],
    )
    sources = list_audio_devices(direction='source')
    sinks = list_audio_devices(direction='sink')
    assert [d.display_name for d in sources] == ['Mic A']
    assert [d.display_name for d in sinks] == ['Speaker B']


def test_list_audio_devices_default_marker(patched_monitor):
    _set_devices(sources=[
        _FakeGstDevice(display_name='Built-in', factory_name='osxaudiosrc',
                       native_id='49', is_default=True),
        _FakeGstDevice(display_name='Other', factory_name='osxaudiosrc',
                       native_id='72'),
    ])
    devices = list_audio_devices(backend='coreaudio', direction='source')
    by_name = {d.display_name: d for d in devices}
    assert by_name['Built-in'].is_default is True
    assert by_name['Other'].is_default is False
