"""Unit tests for the channel-aware logging helpers in openob.rtp._gst_helpers."""
from __future__ import annotations

import pytest

from openob.rtp._gst_helpers import channel_descriptor, format_peaks


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
