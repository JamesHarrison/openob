"""
End-to-end tests: spin up real `openob` rx + tx subprocesses against a real
private redis-server and a real GStreamer pipeline. Pink noise is injected via
audiotestsrc on the TX, captured to a WAV via wavenc → filesink on the RX, and
the captured WAV is asserted to contain non-silent audio.

These tests verify actual network/audio flow end-to-end, not just process
liveness. They require redis-server, GStreamer 1.22+ runtime, and the
gstreamer1.0-plugins-base/good packages on PATH — easiest via
`docker compose run --rm tests`.
"""
from __future__ import annotations

import time

from .helpers import OpenObProcess, wav_duration_seconds, wav_rms_dbfs


# Reasonable defaults: pink-noise at audiotestsrc's default volume of 0.8 lands
# around -10 dBFS RMS post-resampling. -40 dBFS is comfortably above any
# silent / 'codec start glitch' floor and well below a clipping signal.
_RMS_FLOOR_DBFS = -40.0
_TX_START_TIMEOUT = 15.0
_RX_START_TIMEOUT = 15.0
_AUDIO_FLOW_SECONDS = 3.0


def test_pink_noise_round_trip_pcm(redis_server, free_udp_port, link_name, tmp_wav):
    """PCM (linear) round-trip: pink-noise TX → RX captured to WAV; RMS > floor."""
    redis_host, redis_port = redis_server
    config_host = f"{redis_host}:{redis_port}"

    # Receiver first: it'll block waiting for caps from the transmitter.
    rx_argv = [
        "-v",
        config_host, f"{link_name}-rx", link_name,
        "rx",
        "-a", "wav",
        "--wav_file", str(tmp_wav),
    ]
    tx_argv = [
        "-v",
        config_host, f"{link_name}-tx", link_name,
        "tx", "127.0.0.1",
        "-a", "test",
        "--test_wave", "pink-noise",
        "-e", "pcm",
        "-p", str(free_udp_port),
        "--no-multicast",
    ]

    with OpenObProcess(rx_argv) as rx, OpenObProcess(tx_argv) as tx:
        tx.wait_for_log(r"Started (mono|stereo) audio transmission", timeout=_TX_START_TIMEOUT)
        rx.wait_for_log(r"Receiving (mono|stereo) audio transmission", timeout=_RX_START_TIMEOUT)
        time.sleep(_AUDIO_FLOW_SECONDS)
        # RX first so wavenc closes the file cleanly.
        rx.graceful_stop()
        tx.graceful_stop()

    assert tmp_wav.exists(), "RX did not produce a WAV file"
    duration = wav_duration_seconds(tmp_wav)
    assert duration >= _AUDIO_FLOW_SECONDS - 1.0, (
        f"WAV too short ({duration:.2f}s); pipeline likely not running long enough"
    )
    rms = wav_rms_dbfs(tmp_wav)
    assert rms > _RMS_FLOOR_DBFS, (
        f"WAV RMS {rms:.2f} dBFS is below silence floor {_RMS_FLOOR_DBFS} dBFS — "
        f"audio probably did not flow"
    )


def test_pink_noise_round_trip_opus(redis_server, free_udp_port, link_name, tmp_wav):
    """Opus round-trip: same as PCM but exercises encoder + RTP payloader."""
    redis_host, redis_port = redis_server
    config_host = f"{redis_host}:{redis_port}"

    rx_argv = [
        "-v",
        config_host, f"{link_name}-rx", link_name,
        "rx",
        "-a", "wav",
        "--wav_file", str(tmp_wav),
    ]
    tx_argv = [
        "-v",
        config_host, f"{link_name}-tx", link_name,
        "tx", "127.0.0.1",
        "-a", "test",
        "--test_wave", "pink-noise",
        "-e", "opus",
        "-b", "128",
        "-p", str(free_udp_port),
        "--no-multicast",
    ]

    with OpenObProcess(rx_argv) as rx, OpenObProcess(tx_argv) as tx:
        tx.wait_for_log(r"Started (mono|stereo) audio transmission", timeout=_TX_START_TIMEOUT)
        rx.wait_for_log(r"Receiving (mono|stereo) audio transmission", timeout=_RX_START_TIMEOUT)
        time.sleep(_AUDIO_FLOW_SECONDS)
        rx.graceful_stop()
        tx.graceful_stop()

    assert tmp_wav.exists(), "RX did not produce a WAV file"
    duration = wav_duration_seconds(tmp_wav)
    assert duration >= _AUDIO_FLOW_SECONDS - 1.0, (
        f"WAV too short ({duration:.2f}s); pipeline likely not running long enough"
    )
    rms = wav_rms_dbfs(tmp_wav)
    assert rms > _RMS_FLOOR_DBFS, (
        f"WAV RMS {rms:.2f} dBFS is below silence floor {_RMS_FLOOR_DBFS} dBFS — "
        f"audio probably did not flow"
    )


def test_qext_warns_at_low_bitrate(redis_server, free_udp_port, link_name, tmp_wav):
    """`--qext` at <1024 kbps should warn; if the installed opusenc has no
    qext property the second warning fires too. Either way the link
    must still come up cleanly."""
    redis_host, redis_port = redis_server
    config_host = f"{redis_host}:{redis_port}"

    rx_argv = [
        "-v",
        config_host, f"{link_name}-rx", link_name,
        "rx",
        "-a", "wav",
        "--wav_file", str(tmp_wav),
    ]
    tx_argv = [
        "-v",
        config_host, f"{link_name}-tx", link_name,
        "tx", "127.0.0.1",
        "-a", "test",
        "--test_wave", "pink-noise",
        "-e", "opus",
        "-b", "128",  # well below the 1024 threshold
        "--qext",
        "-p", str(free_udp_port),
        "--no-multicast",
    ]

    with OpenObProcess(rx_argv) as rx, OpenObProcess(tx_argv) as tx:
        # First warning: low-bitrate qext
        tx.wait_for_log(
            r"Opus qext enabled at 128 kbps", timeout=_TX_START_TIMEOUT,
        )
        # Audio should still flow even if the property doesn't exist on this
        # opusenc — link comes up with qext effectively a no-op.
        tx.wait_for_log(
            r"Started (mono|stereo) audio transmission", timeout=_TX_START_TIMEOUT,
        )
        rx.wait_for_log(
            r"Receiving (mono|stereo) audio transmission", timeout=_RX_START_TIMEOUT,
        )
        time.sleep(_AUDIO_FLOW_SECONDS)
        rx.graceful_stop()
        tx.graceful_stop()

    assert tmp_wav.exists()
    rms = wav_rms_dbfs(tmp_wav)
    assert rms > _RMS_FLOOR_DBFS


def test_channels_flag_forces_stereo(redis_server, free_udp_port, link_name, tmp_wav):
    """``--channels 2`` should make the negotiated caps 2-channel and the log say stereo."""
    redis_host, redis_port = redis_server
    config_host = f"{redis_host}:{redis_port}"

    rx_argv = [
        "-v",
        config_host, f"{link_name}-rx", link_name,
        "rx",
        "-a", "wav",
        "--wav_file", str(tmp_wav),
    ]
    tx_argv = [
        "-v",
        config_host, f"{link_name}-tx", link_name,
        "tx", "127.0.0.1",
        "-a", "test",
        "--test_wave", "pink-noise",
        "-c", "2",
        "-e", "pcm",
        "-p", str(free_udp_port),
        "--no-multicast",
    ]

    with OpenObProcess(rx_argv) as rx, OpenObProcess(tx_argv) as tx:
        # Match through to end of line — the caps string contains '(int)'
        # parens, so a non-')' capture would close prematurely.
        tx_match = tx.wait_for_log(
            r"Started stereo audio transmission .*$", timeout=_TX_START_TIMEOUT,
        )
        rx_match = rx.wait_for_log(
            r"Receiving stereo audio transmission .*$", timeout=_RX_START_TIMEOUT,
        )
        # Caps string should record channels=2 on both sides (TX from the
        # post-capsfilter ghost pad, RX from the level element after decode).
        assert "channels=(int)2" in tx_match.group(0), tx_match.group(0)
        assert "channels=(int)2" in rx_match.group(0), rx_match.group(0)
        time.sleep(_AUDIO_FLOW_SECONDS)
        rx.graceful_stop()
        tx.graceful_stop()

    assert tmp_wav.exists(), "RX did not produce a WAV file"
    rms = wav_rms_dbfs(tmp_wav)
    assert rms > _RMS_FLOOR_DBFS
