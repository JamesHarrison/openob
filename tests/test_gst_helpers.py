"""Unit tests for the channel-aware logging helpers in openob.rtp._gst_helpers."""
from __future__ import annotations

import pytest

from openob.rtp._gst_helpers import channel_descriptor, format_peaks, struct_get


@pytest.mark.parametrize(
    "n, expected",
    [
        (1, "mono"),
        (2, "stereo"),
        (3, "3-channel"),
        (4, "4-channel"),
        (6, "6-channel"),  # 5.1
        (8, "8-channel"),  # 7.1
    ],
)
def test_channel_descriptor(n, expected):
    assert channel_descriptor(n) == expected


def test_format_peaks_mono():
    assert format_peaks([-12.5]) == "-12.50"


def test_format_peaks_stereo():
    assert format_peaks([-12.5, -13.4]) == "L -12.50 R -13.40"


def test_format_peaks_5_1():
    # Six-channel level array (e.g. 5.1 surround). Verify all values appear,
    # each tagged with their channel index.
    out = format_peaks([-10.0, -10.5, -20.0, -60.0, -15.0, -15.5])
    assert out == "0:-10.00 1:-10.50 2:-20.00 3:-60.00 4:-15.00 5:-15.50"


def test_format_peaks_quad():
    out = format_peaks([-1.0, -2.0, -3.0, -4.0])
    assert "0:-1.00" in out
    assert "3:-4.00" in out


# struct_get must handle three Gst.Structure shapes we encounter in the wild:
#   - PyGObject 3.50+ StructureWrapper: dict-style access only
#   - older PyGObject Gst.Structure: get_value() / typed get_int() etc.
#   - plain dicts (used by other helpers/tests)


class _DictWrapper:
    """Mimics PyGObject 3.50+ StructureWrapper: dict-style access, no typed getters."""

    def __init__(self, **kv):
        self._kv = kv

    def __contains__(self, key):
        return key in self._kv

    def __getitem__(self, key):
        return self._kv[key]


class _LegacyStructure:
    """Mimics older Gst.Structure: get_value() and typed tuple getters, no __getitem__."""

    def __init__(self, **kv):
        self._kv = kv

    def get_value(self, key):
        return self._kv.get(key)

    def get_int(self, key):
        v = self._kv.get(key)
        return (True, int(v)) if isinstance(v, int) and not isinstance(v, bool) else (False, 0)

    def get_string(self, key):
        v = self._kv.get(key)
        return v if isinstance(v, str) else None

    def get_boolean(self, key):
        v = self._kv.get(key)
        return (True, bool(v)) if isinstance(v, bool) else (False, False)


def test_struct_get_dict_wrapper_int():
    assert struct_get(_DictWrapper(channels=2), 'channels') == 2


def test_struct_get_dict_wrapper_missing_returns_default():
    assert struct_get(_DictWrapper(), 'channels', default=99) == 99


def test_struct_get_legacy_get_value():
    assert struct_get(_LegacyStructure(peak=[-12.0, -13.0]), 'peak') == [-12.0, -13.0]


def test_struct_get_legacy_typed_int():
    # When get_value returns None for an unknown key but the typed getter
    # succeeds, struct_get should fall back to the typed getter.
    class _IntOnly:
        def get_value(self, _key):
            return None
        def get_int(self, key):
            return (True, 42) if key == 'payload' else (False, 0)
    assert struct_get(_IntOnly(), 'payload') == 42


def test_struct_get_legacy_typed_bool_false_value():
    # get_boolean returns (True, False) — ok flag is True, value is False.
    # struct_get should return False (the actual value), not the default.
    assert struct_get(_LegacyStructure(**{'is-default': False}), 'is-default') is False


def test_struct_get_plain_dict():
    assert struct_get({'channels': 1, 'rate': 48000}, 'rate') == 48000


def test_struct_get_none_struct():
    assert struct_get(None, 'anything', default='x') == 'x'
