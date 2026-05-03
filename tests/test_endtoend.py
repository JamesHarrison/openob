"""
End-to-end tests: spin up real `openob` rx + tx subprocesses against real
GStreamer pipelines, with each test getting its own per-test TOML config
under a temporary ``OPENOB_CONFIG_DIR``.

Pink noise is injected via ``audiotestsrc`` on the TX, captured to a WAV
via ``wavenc → filesink`` on the RX, and the captured WAV is asserted to
contain non-silent audio (RMS above the silence floor).

These tests verify actual network/audio flow end-to-end. v6 has no Redis
dependency; the only runtime cross-side communication is RTP+RTCP-mux on
the configured UDP port.

Requires GStreamer 1.22+ runtime + plugins-base/good/bad on PATH — easiest
via ``docker compose run --rm tests``.
"""
from __future__ import annotations

import time
from pathlib import Path

from .conftest import write_link_toml
from .helpers import (
    OpenObProcess,
    wav_dropout_count_dbfs,
    wav_duration_seconds,
    wav_lowest_window_rms_dbfs,
    wav_rms_dbfs,
)


# Pink-noise at audiotestsrc's default volume of 0.8 lands around -10 dBFS
# RMS post-resampling. -40 dBFS is comfortably above any silent / 'codec
# start glitch' floor and well below a clipping signal.
_RMS_FLOOR_DBFS = -40.0
# Per-window dropout threshold: a 20 ms window with mean energy below this
# corresponds to a brief silence — exactly the kind of glitch a listener
# notices. Pink noise's natural per-window variation lives within ~10 dB
# of the long-term mean (~-10 dBFS), so any window dipping below this is
# a real gap, not just a pink-noise valley.
_DROPOUT_FLOOR_DBFS = -40.0
# Silence floor for negative tests where we expect ZERO useful audio (PSK
# mismatch). With srtpdec dropping every packet we get no decoded audio at
# all, so RMS should be -inf — anything above this is cause for alarm.
_RMS_SILENT_CEILING_DBFS = -60.0


def assert_continuous(wav, label: str = '') -> None:
    """Assert the WAV holds continuous audio: no 20ms window dips below
    the dropout floor. Catches glitches that an averaged RMS misses.
    """
    dropouts, total = wav_dropout_count_dbfs(
        wav, threshold_dbfs=_DROPOUT_FLOOR_DBFS,
    )
    assert total > 0, f'{label or wav}: WAV too short to analyse'
    assert dropouts == 0, (
        f'{label or wav}: {dropouts}/{total} 20ms windows dipped below '
        f'{_DROPOUT_FLOOR_DBFS} dBFS — audio was not continuous '
        f'(min window: {wav_lowest_window_rms_dbfs(wav):.2f} dBFS)'
    )
_TX_START_TIMEOUT = 15.0
_RX_START_TIMEOUT = 15.0
_AUDIO_FLOW_SECONDS = 3.0


def _argv_for(
    config_dir: Path,
    mode: str,
    link_name: str,
    extra: list[str] | None = None,
) -> list[str]:
    argv = [
        '-v',
        mode, link_name,
        '--config-dir', str(config_dir),
        '--node-name', f'{link_name}-{mode}',
    ]
    if extra:
        argv.extend(extra)
    return argv


def _run_pink_noise_roundtrip(
    config_dir: Path,
    link_name: str,
    tmp_wav: Path,
    *,
    encoding: str = 'opus',
    bitrate: int = 128,
    channels: int | None = None,
    psk: str | None = None,
) -> tuple[Path, str]:
    """Common harness: write the link TOML, launch RX then TX, wait for
    audio to flow, return (wav path, rx_started_log_line).
    """
    write_link_toml(
        config_dir, link_name,
        encoding=encoding,
        port=_pick_port(),
        bitrate=bitrate,
        channels=channels,
        security='psk' if psk else 'none',
        psk=psk,
        extra={
            'audio_input': 'test', 'test_wave': 'pink-noise',
            'audio_output': 'wav', 'wav_file': str(tmp_wav),
        },
    )

    with OpenObProcess(_argv_for(config_dir, 'rx', link_name)) as rx, \
         OpenObProcess(_argv_for(config_dir, 'tx', link_name)) as tx:
        tx.wait_for_log(r'Started (mono|stereo) audio transmission', timeout=_TX_START_TIMEOUT)
        rx_match = rx.wait_for_log(
            r'Receiving (mono|stereo) audio transmission', timeout=_RX_START_TIMEOUT,
        )
        time.sleep(_AUDIO_FLOW_SECONDS)
        rx.graceful_stop()
        tx.graceful_stop()

    return tmp_wav, rx_match.group(0)


# We re-derive the port once per test by binding socket. Doing this inline
# in the toml writer keeps each test self-contained.
def _pick_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


# --- happy paths ----------------------------------------------------------


def test_pink_noise_round_trip_pcm(tmp_config_dir, link_name, tmp_wav):
    """PCM round-trip, no encryption."""
    write_link_toml(
        tmp_config_dir, link_name,
        encoding='pcm',
        port=_pick_port(),
        samplerate=48000,
        channels=2,
        security='none',
        extra={
            'audio_input': 'test', 'test_wave': 'pink-noise',
            'audio_output': 'wav', 'wav_file': str(tmp_wav),
        },
    )
    with OpenObProcess(_argv_for(tmp_config_dir, 'rx', link_name)) as rx, \
         OpenObProcess(_argv_for(tmp_config_dir, 'tx', link_name)) as tx:
        tx.wait_for_log(r'Started (mono|stereo) audio transmission', timeout=_TX_START_TIMEOUT)
        rx.wait_for_log(r'Receiving (mono|stereo) audio transmission', timeout=_RX_START_TIMEOUT)
        time.sleep(_AUDIO_FLOW_SECONDS)
        rx.graceful_stop()
        tx.graceful_stop()

    assert tmp_wav.exists(), 'RX did not produce a WAV file'
    duration = wav_duration_seconds(tmp_wav)
    assert duration >= _AUDIO_FLOW_SECONDS - 1.0, (
        f'WAV too short ({duration:.2f}s); pipeline likely not running long enough'
    )
    rms = wav_rms_dbfs(tmp_wav)
    assert rms > _RMS_FLOOR_DBFS, (
        f'WAV RMS {rms:.2f} dBFS is below silence floor {_RMS_FLOOR_DBFS} dBFS — '
        f'audio probably did not flow'
    )
    assert_continuous(tmp_wav)


def test_pink_noise_round_trip_opus(tmp_config_dir, link_name, tmp_wav):
    """Opus round-trip, no encryption."""
    wav, _ = _run_pink_noise_roundtrip(tmp_config_dir, link_name, tmp_wav)
    assert wav.exists()
    rms = wav_rms_dbfs(wav)
    assert rms > _RMS_FLOOR_DBFS
    assert_continuous(wav)


def test_pink_noise_round_trip_opus_psk(tmp_config_dir, link_name, tmp_wav):
    """Opus + SRTP with a per-link PSK; both sides derive the same master key."""
    wav, _ = _run_pink_noise_roundtrip(
        tmp_config_dir, link_name, tmp_wav,
        psk='correct-horse-battery-staple-test',
    )
    assert wav.exists()
    rms = wav_rms_dbfs(wav)
    assert rms > _RMS_FLOOR_DBFS
    assert_continuous(wav)


def test_psk_mismatch_silent(tmp_config_dir, link_name, tmp_wav):
    """Wrong PSK on RX → srtpdec drops every packet → captured WAV is silent."""
    port = _pick_port()
    # Two separate config dirs so RX and TX can have different PSKs without
    # rewriting the same file.
    rx_dir = tmp_config_dir
    tx_dir = tmp_config_dir.parent / 'tx-config'
    (tx_dir / 'links').mkdir(parents=True)

    write_link_toml(
        rx_dir, link_name,
        encoding='opus', port=port, security='psk', psk='alice-key',
        extra={'audio_output': 'wav', 'wav_file': str(tmp_wav)},
    )
    write_link_toml(
        tx_dir, link_name,
        encoding='opus', port=port, security='psk', psk='bob-key-different',
        extra={'audio_input': 'test', 'test_wave': 'pink-noise'},
    )

    with OpenObProcess(_argv_for(rx_dir, 'rx', link_name)) as rx, \
         OpenObProcess(_argv_for(tx_dir, 'tx', link_name)) as tx:
        # TX still starts (it doesn't know RX is unhappy).
        tx.wait_for_log(r'Started (mono|stereo) audio transmission', timeout=_TX_START_TIMEOUT)
        # RX gets caps (caps signal is unauthenticated in v6.0) and starts a
        # pipeline, but srtpdec rejects every packet — no decoded audio.
        time.sleep(_AUDIO_FLOW_SECONDS)
        rx.graceful_stop()
        tx.graceful_stop()

    # Three valid silent outcomes:
    #  1. RX never produced a WAV file at all
    #  2. RX produced an empty/truncated WAV (wave.open raises EOFError)
    #  3. RX produced a WAV but it's silent (RMS below ceiling)
    if not tmp_wav.exists() or tmp_wav.stat().st_size < 44:  # 44 = WAV header
        return
    try:
        rms = wav_rms_dbfs(tmp_wav)
    except (EOFError, ValueError):
        return  # malformed/empty wav — same intent as silence
    assert rms < _RMS_SILENT_CEILING_DBFS, (
        f'WAV RMS {rms:.2f} dBFS is too high for a PSK-mismatched stream — '
        'srtpdec should have rejected every packet'
    )


def test_root_psk_hkdf(tmp_config_dir, link_name, tmp_wav):
    """Both sides have the same root_psk + same link_name → same derived key."""
    write_link_toml(
        tmp_config_dir, link_name,
        encoding='opus',
        port=_pick_port(),
        security='psk',
        root_psk='one-root-secret-shared-by-the-fleet',
        extra={
            'audio_input': 'test', 'test_wave': 'pink-noise',
            'audio_output': 'wav', 'wav_file': str(tmp_wav),
        },
    )

    with OpenObProcess(_argv_for(tmp_config_dir, 'rx', link_name)) as rx, \
         OpenObProcess(_argv_for(tmp_config_dir, 'tx', link_name)) as tx:
        tx.wait_for_log(r'Started (mono|stereo) audio transmission', timeout=_TX_START_TIMEOUT)
        rx.wait_for_log(r'Receiving (mono|stereo) audio transmission', timeout=_RX_START_TIMEOUT)
        time.sleep(_AUDIO_FLOW_SECONDS)
        rx.graceful_stop()
        tx.graceful_stop()

    assert tmp_wav.exists()
    rms = wav_rms_dbfs(tmp_wav)
    assert rms > _RMS_FLOOR_DBFS
    assert_continuous(tmp_wav)


# --- caps-signal scenarios -----------------------------------------------


def test_caps_signal_late_rx(tmp_config_dir, link_name, tmp_wav):
    """RX joins after TX has been running — picks up caps within ~2s and audio flows."""
    write_link_toml(
        tmp_config_dir, link_name,
        encoding='opus', port=_pick_port(), security='none',
        extra={
            'audio_input': 'test', 'test_wave': 'pink-noise',
            'audio_output': 'wav', 'wav_file': str(tmp_wav),
        },
    )

    with OpenObProcess(_argv_for(tmp_config_dir, 'tx', link_name)) as tx:
        tx.wait_for_log(r'Started (mono|stereo) audio transmission', timeout=_TX_START_TIMEOUT)
        time.sleep(3.0)  # give TX a head start so RX really is "joining late"

        with OpenObProcess(_argv_for(tmp_config_dir, 'rx', link_name)) as rx:
            rx.wait_for_log(
                r'Receiving (mono|stereo) audio transmission',
                timeout=_RX_START_TIMEOUT,
            )
            time.sleep(_AUDIO_FLOW_SECONDS)
            rx.graceful_stop()
        tx.graceful_stop()

    assert tmp_wav.exists()
    rms = wav_rms_dbfs(tmp_wav)
    assert rms > _RMS_FLOOR_DBFS
    # caps_signal_late_rx legitimately misses the very first packets while
    # RX is bootstrapping its caps, so we don't strictly assert
    # zero-dropouts here — the WAV-level RMS check is sufficient evidence
    # that audio flowed once RX caught up.


def test_caps_from_config(tmp_config_dir, link_name, tmp_wav):
    """RX with --caps-from-config skips the wait and links immediately from config."""
    write_link_toml(
        tmp_config_dir, link_name,
        encoding='opus', port=_pick_port(), security='none',
        caps_from_config=True,
        channels=2,  # required so derived caps include channels=2 cleanly
        extra={
            'audio_input': 'test', 'test_wave': 'pink-noise',
            'audio_output': 'wav', 'wav_file': str(tmp_wav),
        },
    )

    with OpenObProcess(_argv_for(tmp_config_dir, 'rx', link_name)) as rx, \
         OpenObProcess(_argv_for(tmp_config_dir, 'tx', link_name)) as tx:
        # We should NOT see "Waiting for in-band caps signal" on RX.
        rx.wait_for_log(r'caps_from_config=true', timeout=_RX_START_TIMEOUT)
        tx.wait_for_log(r'Started (mono|stereo) audio transmission', timeout=_TX_START_TIMEOUT)
        rx.wait_for_log(r'Receiving (mono|stereo) audio transmission', timeout=_RX_START_TIMEOUT)
        time.sleep(_AUDIO_FLOW_SECONDS)
        rx.graceful_stop()
        tx.graceful_stop()

    assert tmp_wav.exists()
    rms = wav_rms_dbfs(tmp_wav)
    assert rms > _RMS_FLOOR_DBFS
    assert_continuous(tmp_wav)


def test_psk_via_env_no_toml(tmp_config_dir, tmp_path):
    """`openob {tx,rx} --security psk` with NO TOML on disk; PSK comes
    from OPENOB_PSK in the environment. Validates the ad-hoc CLI flow."""
    port = _pick_port()
    link_name = 'env-psk'
    wav = tmp_path / 'rx.wav'

    # Both ends will run with empty config dir but PSK in env.
    env = {'OPENOB_PSK': 'env-only-secret'}

    rx_argv = [
        '-v', 'rx', link_name,
        '--config-dir', str(tmp_config_dir),
        '--node-name', f'{link_name}-rx',
        '--port', str(port),
        '--security', 'psk',
        '--audio-output', 'wav',
        '--wav-file', str(wav),
    ]
    tx_argv = [
        '-v', 'tx', link_name,
        '--config-dir', str(tmp_config_dir),
        '--node-name', f'{link_name}-tx',
        '--port', str(port),
        '--receiver-host', '127.0.0.1',
        '--security', 'psk',
        '--encoding', 'opus',
        '--bitrate', '128',
        '--audio-input', 'test',
        '--test-wave', 'pink-noise',
    ]

    with OpenObProcess(rx_argv, extra_env=env) as rx, \
         OpenObProcess(tx_argv, extra_env=env) as tx:
        tx.wait_for_log(r'Started (mono|stereo) audio transmission', timeout=_TX_START_TIMEOUT)
        rx.wait_for_log(r'Receiving (mono|stereo) audio transmission', timeout=_RX_START_TIMEOUT)
        time.sleep(_AUDIO_FLOW_SECONDS)
        rx.graceful_stop()
        tx.graceful_stop()

    assert wav.exists()
    assert wav_rms_dbfs(wav) > _RMS_FLOOR_DBFS
    assert_continuous(wav)


def test_rx_minimal_config_picks_up_encoding_from_caps(tmp_config_dir, tmp_path):
    """A truly-minimal RX TOML (no encoding/bitrate/samplerate/channels)
    still works: encoding is parsed from the in-band caps signal.

    TX is opus; RX TOML has only port + audio_output + (no security)."""
    port = _pick_port()
    link_name = 'asymmetric'
    wav = tmp_path / 'rx.wav'

    # TX: full codec config including encoding=opus, bitrate, etc.
    tx_dir = tmp_path / 'tx-cfg'
    (tx_dir / 'links').mkdir(parents=True)
    write_link_toml(
        tx_dir, link_name,
        encoding='opus', port=port, bitrate=128, security='none',
        extra={'audio_input': 'test', 'test_wave': 'pink-noise'},
    )

    # RX: minimal — just port + audio_output. NO encoding, NO bitrate,
    # NO samplerate, NO channels.
    rx_dir = tmp_config_dir
    (rx_dir / 'links' / f'{link_name}.toml').write_text(
        f'port = {port}\n'
        f'security = "none"\n'
        f'audio_output = "wav"\n'
        f'wav_file = "{wav}"\n'
    )

    with OpenObProcess(_argv_for(rx_dir, 'rx', link_name)) as rx, \
         OpenObProcess(_argv_for(tx_dir, 'tx', link_name)) as tx:
        tx.wait_for_log(r'Started (mono|stereo) audio transmission', timeout=_TX_START_TIMEOUT)
        rx.wait_for_log(r'encoding=opus — derived from caps', timeout=_RX_START_TIMEOUT)
        rx.wait_for_log(r'Receiving (mono|stereo) audio transmission', timeout=_RX_START_TIMEOUT)
        time.sleep(_AUDIO_FLOW_SECONDS)
        rx.graceful_stop()
        tx.graceful_stop()

    assert wav.exists()
    assert wav_rms_dbfs(wav) > _RMS_FLOOR_DBFS
    assert_continuous(wav)


def test_rx_minimal_config_picks_up_pcm_from_caps(tmp_config_dir, tmp_path):
    """Same idea but TX is PCM — RX still adapts via the caps signal,
    even though its config doesn't say which codec."""
    port = _pick_port()
    link_name = 'asym-pcm'
    wav = tmp_path / 'rx.wav'

    tx_dir = tmp_path / 'tx-cfg'
    (tx_dir / 'links').mkdir(parents=True)
    write_link_toml(
        tx_dir, link_name,
        encoding='pcm', port=port, samplerate=48000, channels=2, security='none',
        extra={'audio_input': 'test', 'test_wave': 'pink-noise'},
    )

    rx_dir = tmp_config_dir
    (rx_dir / 'links' / f'{link_name}.toml').write_text(
        f'port = {port}\n'
        f'security = "none"\n'
        f'audio_output = "wav"\n'
        f'wav_file = "{wav}"\n'
    )

    with OpenObProcess(_argv_for(rx_dir, 'rx', link_name)) as rx, \
         OpenObProcess(_argv_for(tx_dir, 'tx', link_name)) as tx:
        tx.wait_for_log(r'Started (mono|stereo) audio transmission', timeout=_TX_START_TIMEOUT)
        rx.wait_for_log(r'encoding=pcm — derived from caps', timeout=_RX_START_TIMEOUT)
        rx.wait_for_log(r'Receiving (mono|stereo) audio transmission', timeout=_RX_START_TIMEOUT)
        time.sleep(_AUDIO_FLOW_SECONDS)
        rx.graceful_stop()
        tx.graceful_stop()

    assert wav.exists()
    assert wav_rms_dbfs(wav) > _RMS_FLOOR_DBFS
    assert_continuous(wav)


# --- topology scenarios --------------------------------------------------


def test_n_to_1_two_remote_kits(tmp_config_dir, tmp_path):
    """Two simultaneous links with separate ports + PSKs (the N:1 hub-spoke
    topology). Each end-to-end stream lands cleanly in its own WAV."""
    wav_a = tmp_path / 'flypack-a.wav'
    wav_b = tmp_path / 'flypack-b.wav'
    port_a = _pick_port()
    port_b = _pick_port()
    if port_a == port_b:
        port_b += 1  # tiny race; trivially handled

    write_link_toml(
        tmp_config_dir, 'flypack-a',
        encoding='opus', port=port_a, security='psk', psk='alpha-key',
        extra={
            'audio_input': 'test', 'test_wave': 'pink-noise',
            'audio_output': 'wav', 'wav_file': str(wav_a),
        },
    )
    write_link_toml(
        tmp_config_dir, 'flypack-b',
        encoding='opus', port=port_b, security='psk', psk='beta-key-different',
        extra={
            'audio_input': 'test', 'test_wave': 'pink-noise',
            'audio_output': 'wav', 'wav_file': str(wav_b),
        },
    )

    with (
        OpenObProcess(_argv_for(tmp_config_dir, 'rx', 'flypack-a')) as rx_a,
        OpenObProcess(_argv_for(tmp_config_dir, 'rx', 'flypack-b')) as rx_b,
        OpenObProcess(_argv_for(tmp_config_dir, 'tx', 'flypack-a')) as tx_a,
        OpenObProcess(_argv_for(tmp_config_dir, 'tx', 'flypack-b')) as tx_b,
    ):
        tx_a.wait_for_log(r'Started (mono|stereo) audio transmission', timeout=_TX_START_TIMEOUT)
        tx_b.wait_for_log(r'Started (mono|stereo) audio transmission', timeout=_TX_START_TIMEOUT)
        rx_a.wait_for_log(r'Receiving (mono|stereo) audio transmission', timeout=_RX_START_TIMEOUT)
        rx_b.wait_for_log(r'Receiving (mono|stereo) audio transmission', timeout=_RX_START_TIMEOUT)
        time.sleep(_AUDIO_FLOW_SECONDS)
        rx_a.graceful_stop()
        rx_b.graceful_stop()
        tx_a.graceful_stop()
        tx_b.graceful_stop()

    for wav in (wav_a, wav_b):
        assert wav.exists(), f'{wav} not produced'
        assert wav_rms_dbfs(wav) > _RMS_FLOOR_DBFS, (
            f'{wav} is silent — link did not carry audio'
        )
        assert_continuous(wav, label=str(wav))


def test_multicast_psk_roundtrip(tmp_config_dir, link_name, tmp_wav):
    """SRTP over multicast: TX sends to 239.x.x.x, RX joins the group, audio flows."""
    # 239.0.0.x is the IPv4 administratively-scoped multicast block; pick a
    # uniqueish address per test to avoid in-host collisions.
    import secrets
    mcast = f'239.0.{secrets.randbelow(256)}.{1 + secrets.randbelow(254)}'

    write_link_toml(
        tmp_config_dir, link_name,
        encoding='opus',
        port=_pick_port(),
        receiver_host=mcast,
        multicast=True,
        security='psk',
        psk='multicast-test-key',
        extra={
            'audio_input': 'test', 'test_wave': 'pink-noise',
            'audio_output': 'wav', 'wav_file': str(tmp_wav),
        },
    )

    with OpenObProcess(_argv_for(tmp_config_dir, 'rx', link_name)) as rx, \
         OpenObProcess(_argv_for(tmp_config_dir, 'tx', link_name)) as tx:
        tx.wait_for_log(r'Started (mono|stereo) audio transmission', timeout=_TX_START_TIMEOUT)
        rx.wait_for_log(r'Receiving (mono|stereo) audio transmission', timeout=_RX_START_TIMEOUT)
        time.sleep(_AUDIO_FLOW_SECONDS)
        rx.graceful_stop()
        tx.graceful_stop()

    assert tmp_wav.exists(), 'RX did not produce a WAV file'
    rms = wav_rms_dbfs(tmp_wav)
    assert rms > _RMS_FLOOR_DBFS, (
        f'multicast SRTP WAV RMS {rms:.2f} dBFS below floor — multicast or SRTP path broken'
    )
    assert_continuous(tmp_wav)


# --- regression coverage from v5 -----------------------------------------


# --- retransmission ------------------------------------------------------


def test_pink_noise_round_trip_with_rtx(tmp_config_dir, link_name, tmp_wav):
    """RTX enabled on TX (retransmission_time_limit=200ms): audio still flows
    end-to-end and both sides log the RTX-enabled lines.

    Doesn't simulate loss — just verifies the new code path doesn't break the
    happy case. A loss-injection test is a follow-up using the netsim element.
    """
    write_link_toml(
        tmp_config_dir, link_name,
        encoding='opus',
        port=_pick_port(),
        security='none',
        retransmission_time_limit=200,
        extra={
            'audio_input': 'test', 'test_wave': 'pink-noise',
            'audio_output': 'wav', 'wav_file': str(tmp_wav),
        },
    )

    with OpenObProcess(_argv_for(tmp_config_dir, 'rx', link_name)) as rx, \
         OpenObProcess(_argv_for(tmp_config_dir, 'tx', link_name)) as tx:
        tx.wait_for_log(
            r'RTX enabled \(max-size-time=200ms\)', timeout=_TX_START_TIMEOUT,
        )
        tx.wait_for_log(r'Started (mono|stereo) audio transmission', timeout=_TX_START_TIMEOUT)
        rx.wait_for_log(
            r'RTX enabled \(rtx-time=200ms', timeout=_RX_START_TIMEOUT,
        )
        rx.wait_for_log(r'Receiving (mono|stereo) audio transmission', timeout=_RX_START_TIMEOUT)
        time.sleep(_AUDIO_FLOW_SECONDS)
        rx.graceful_stop()
        tx.graceful_stop()

    assert tmp_wav.exists()
    rms = wav_rms_dbfs(tmp_wav)
    assert rms > _RMS_FLOOR_DBFS, (
        f'RTX-enabled link did not carry audio (RMS {rms:.2f} dBFS)'
    )
    assert_continuous(tmp_wav)


def test_rtx_recovers_from_packet_loss(tmp_config_dir, link_name, tmp_wav):
    """Inject 30% packet loss on TX (after rtprtxsend's buffer) and confirm:
      - audio is CONTINUOUS — every 20ms window stays above the dropout
        floor (Opus FEC alone leaves audible gaps at this rate; RTX is
        what closes them).
      - rtprtxsend reports both NACK requests received AND packets resent.

    30% is chosen empirically: at 5% Opus FEC alone passes the continuity
    check (so the test wouldn't differentiate RTX-working from
    RTX-broken), at >40% Opus' decoder is also breaking down. 30% is in
    the sweet spot where Opus FEC alone produces ~1-3 dropouts per 8s
    run but RTX brings that to a reliable zero.

    Loss is generated by the netsim element, gated behind
    OPENOB_TEST_PACKET_LOSS_PERCENT — the production code path doesn't see
    netsim at all unless that env var is set.
    """
    write_link_toml(
        tmp_config_dir, link_name,
        encoding='opus',
        port=_pick_port(),
        security='none',
        retransmission_time_limit=200,
        # Wider jitter buffer than the default 40ms: at 30% loss the
        # rtpjitterbuffer needs time to wait for retransmissions
        # before it gives up on a slot. 200ms aligns with TX's RTX window.
        extra={
            'jitter_buffer': 200,
            'audio_input': 'test', 'test_wave': 'pink-noise',
            'audio_output': 'wav', 'wav_file': str(tmp_wav),
        },
    )

    tx_env = {'OPENOB_TEST_PACKET_LOSS_PERCENT': '30'}
    flow_seconds = 8.0  # long enough for many RTX cycles to accumulate

    with OpenObProcess(_argv_for(tmp_config_dir, 'rx', link_name)) as rx, \
         OpenObProcess(_argv_for(tmp_config_dir, 'tx', link_name), extra_env=tx_env) as tx:
        tx.wait_for_log(
            r'OPENOB_TEST_PACKET_LOSS_PERCENT=30\.00', timeout=_TX_START_TIMEOUT,
        )
        tx.wait_for_log(r'Started (mono|stereo) audio transmission', timeout=_TX_START_TIMEOUT)
        rx.wait_for_log(r'RTX enabled \(rtx-time=200ms', timeout=_RX_START_TIMEOUT)
        rx.wait_for_log(r'Receiving (mono|stereo) audio transmission', timeout=_RX_START_TIMEOUT)
        time.sleep(flow_seconds)

        # Read RTX stats out of the TX log BEFORE shutting down — the
        # 1-Hz timer in RTPTransmitter._log_rtx_stats logs counter snapshots.
        import re
        stats_lines = re.findall(
            r'RTX stats: nack-requests=(\d+) packets-resent=(\d+)',
            tx.read_log(),
        )
        rx.graceful_stop()
        tx.graceful_stop()

    assert stats_lines, (
        f'TX never logged RTX stats — the 1s timer never fired? '
        f'Full log:\n{tx.read_log()}'
    )
    final_requests, final_packets = (int(x) for x in stats_lines[-1])
    assert final_requests > 0, (
        f'After {flow_seconds}s of 30% loss, TX received zero NACK requests — '
        f'RX is not asking for retransmission. stats history: {stats_lines}'
    )
    assert final_packets > 0, (
        f'TX got {final_requests} NACK requests but resent zero packets — '
        f'rtprtxsend buffer too small or NACKs reference packets it dropped.'
    )

    assert tmp_wav.exists()
    rms = wav_rms_dbfs(tmp_wav)
    assert rms > _RMS_FLOOR_DBFS, (
        f'WAV RMS {rms:.2f} dBFS below floor — RTX did not save the audio under '
        f'30% loss. rtx stats: requests={final_requests}, resent={final_packets}'
    )
    # The whole point of RTX is no audible glitches: every 20ms window
    # should still hold continuous audio, not just on-average-loud audio.
    # At 30% loss Opus FEC alone leaves visible gaps in 5/8 runs in our
    # probes; with RTX we want zero gaps every run.
    dropouts, total = wav_dropout_count_dbfs(
        tmp_wav, threshold_dbfs=_DROPOUT_FLOOR_DBFS,
    )
    assert dropouts == 0, (
        f'{dropouts}/{total} 20ms windows below {_DROPOUT_FLOOR_DBFS} dBFS — '
        f'RTX did not prevent audible gaps under 30% loss '
        f'(min window: {wav_lowest_window_rms_dbfs(tmp_wav):.2f} dBFS, '
        f'rtx stats: requests={final_requests}, resent={final_packets})'
    )


def test_qext_warns_at_low_bitrate(tmp_config_dir, link_name, tmp_wav):
    """`opus_qext = true` at <1024 kbps still emits the warning and the link
    still comes up cleanly."""
    write_link_toml(
        tmp_config_dir, link_name,
        encoding='opus',
        port=_pick_port(),
        bitrate=128,
        security='none',
        extra={
            'audio_input': 'test', 'test_wave': 'pink-noise',
            'audio_output': 'wav', 'wav_file': str(tmp_wav),
        },
    )
    # opus_qext goes via CLI to keep the per-test config simple; either path works.
    tx_argv = _argv_for(tmp_config_dir, 'tx', link_name, ['--opus-qext'])

    with OpenObProcess(_argv_for(tmp_config_dir, 'rx', link_name)) as rx, \
         OpenObProcess(tx_argv) as tx:
        tx.wait_for_log(r'Opus qext enabled at 128 kbps', timeout=_TX_START_TIMEOUT)
        tx.wait_for_log(r'Started (mono|stereo) audio transmission', timeout=_TX_START_TIMEOUT)
        rx.wait_for_log(r'Receiving (mono|stereo) audio transmission', timeout=_RX_START_TIMEOUT)
        time.sleep(_AUDIO_FLOW_SECONDS)
        rx.graceful_stop()
        tx.graceful_stop()

    assert tmp_wav.exists()
    assert wav_rms_dbfs(tmp_wav) > _RMS_FLOOR_DBFS
    assert_continuous(tmp_wav)


def test_channels_flag_forces_stereo(tmp_config_dir, link_name, tmp_wav):
    """`channels = 2` should make the negotiated caps stereo end-to-end."""
    write_link_toml(
        tmp_config_dir, link_name,
        encoding='pcm',
        port=_pick_port(),
        samplerate=48000,
        channels=2,
        security='none',
        extra={
            'audio_input': 'test', 'test_wave': 'pink-noise',
            'audio_output': 'wav', 'wav_file': str(tmp_wav),
        },
    )

    with OpenObProcess(_argv_for(tmp_config_dir, 'rx', link_name)) as rx, \
         OpenObProcess(_argv_for(tmp_config_dir, 'tx', link_name)) as tx:
        tx_match = tx.wait_for_log(
            r'Started stereo audio transmission .*$', timeout=_TX_START_TIMEOUT,
        )
        rx_match = rx.wait_for_log(
            r'Receiving stereo audio transmission .*$', timeout=_RX_START_TIMEOUT,
        )
        assert 'channels=(int)2' in tx_match.group(0), tx_match.group(0)
        assert 'channels=(int)2' in rx_match.group(0), rx_match.group(0)
        time.sleep(_AUDIO_FLOW_SECONDS)
        rx.graceful_stop()
        tx.graceful_stop()

    assert tmp_wav.exists()
    assert wav_rms_dbfs(tmp_wav) > _RMS_FLOOR_DBFS
    assert_continuous(tmp_wav)
