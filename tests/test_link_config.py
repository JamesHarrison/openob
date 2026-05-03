"""Unit tests for the v6 TOML-backed LinkConfig loader and helpers."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from openob.link_config import (
    ConfigError,
    LinkConfig,
    load_link_config,
    sanitize_caps_for_wire,
)


# --- loader basics --------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_load_minimal_tx(tmp_path):
    """A bare-minimum TX config loads with sensible defaults."""
    _write(tmp_path / 'links' / 'mylink.toml', '''
        encoding = "opus"
        port = 4000
        receiver_host = "10.0.0.1"
    ''')
    cfg = load_link_config('mylink', 'tx', config_dir=tmp_path)
    assert cfg.name == 'mylink'
    assert cfg.mode == 'tx'
    assert cfg.encoding == 'opus'
    assert cfg.port == 4000
    assert cfg.receiver_host == '10.0.0.1'
    assert cfg.security == 'none'
    assert cfg.bitrate == 128  # default
    assert cfg.opus_fec is True


def test_load_minimal_rx(tmp_path):
    _write(tmp_path / 'links' / 'mylink.toml', 'port = 4000\nreceiver_host = "10.0.0.1"')
    cfg = load_link_config('mylink', 'rx', config_dir=tmp_path)
    assert cfg.mode == 'rx'
    # RX doesn't *require* receiver_host unless multicast, but having it
    # in the file is fine.
    assert cfg.receiver_host == '10.0.0.1'


def test_cli_overrides_take_precedence(tmp_path):
    _write(tmp_path / 'links' / 'mylink.toml', '''
        encoding = "opus"
        port = 4000
        receiver_host = "10.0.0.1"
        bitrate = 64
    ''')
    cfg = load_link_config(
        'mylink', 'tx',
        config_dir=tmp_path,
        cli_overrides={'bitrate': 256, 'multicast': True},
    )
    assert cfg.bitrate == 256
    assert cfg.multicast is True
    assert cfg.port == 4000  # untouched by CLI


def test_unknown_toml_keys_rejected(tmp_path):
    _write(tmp_path / 'links' / 'mylink.toml', '''
        encoding = "opus"
        port = 4000
        receiver_host = "10.0.0.1"
        nosuch_field = "wat"
    ''')
    with pytest.raises(ConfigError, match='unknown link-config keys'):
        load_link_config('mylink', 'tx', config_dir=tmp_path)


def test_missing_link_file_uses_defaults(tmp_path):
    """No file + CLI fills the gap → no error if required fields supplied via CLI."""
    cfg = load_link_config(
        'mylink', 'tx',
        config_dir=tmp_path,
        cli_overrides={'receiver_host': '127.0.0.1', 'port': 5000},
    )
    assert cfg.receiver_host == '127.0.0.1'
    assert cfg.port == 5000


def test_tx_requires_receiver_host(tmp_path):
    _write(tmp_path / 'links' / 'mylink.toml', 'port = 4000')
    with pytest.raises(ConfigError, match='receiver_host'):
        load_link_config('mylink', 'tx', config_dir=tmp_path)


# --- security validation --------------------------------------------------


def test_psk_security_requires_psk_or_root_psk(tmp_path):
    _write(tmp_path / 'links' / 'mylink.toml', '''
        port = 4000
        receiver_host = "10.0.0.1"
        security = "psk"
    ''')
    with pytest.raises(ConfigError, match="security='psk' requires"):
        load_link_config('mylink', 'tx', config_dir=tmp_path)


def test_psk_security_with_per_link_psk(tmp_path):
    _write(tmp_path / 'links' / 'mylink.toml', '''
        port = 4000
        receiver_host = "10.0.0.1"
        security = "psk"
        psk = "shared-secret"
    ''')
    cfg = load_link_config('mylink', 'tx', config_dir=tmp_path)
    assert cfg.security == 'psk'
    assert cfg.psk == 'shared-secret'


def test_root_psk_layered_via_secrets_file(tmp_path):
    _write(tmp_path / 'secrets.toml', '''
        [defaults]
        root_psk = "fleet-root"
    ''')
    _write(tmp_path / 'links' / 'mylink.toml', '''
        port = 4000
        receiver_host = "10.0.0.1"
        security = "psk"
    ''')
    cfg = load_link_config('mylink', 'tx', config_dir=tmp_path)
    assert cfg.security == 'psk'
    assert cfg.root_psk == 'fleet-root'
    assert cfg.psk is None


def test_per_link_psk_overrides_root_psk(tmp_path):
    _write(tmp_path / 'secrets.toml', '[defaults]\nroot_psk = "fleet-root"')
    _write(tmp_path / 'links' / 'mylink.toml', '''
        port = 4000
        receiver_host = "10.0.0.1"
        security = "psk"
        psk = "per-link-override"
    ''')
    cfg = load_link_config('mylink', 'tx', config_dir=tmp_path)
    # Both fields exposed; security.resolve_srtp_master picks per-link first.
    assert cfg.psk == 'per-link-override'
    assert cfg.root_psk == 'fleet-root'


# --- envvar substitution --------------------------------------------------


def test_envvar_substitution_for_psk(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENOB_TEST_PSK', 'from-env')
    _write(tmp_path / 'links' / 'mylink.toml', '''
        port = 4000
        receiver_host = "10.0.0.1"
        security = "psk"
        psk = "${OPENOB_TEST_PSK}"
    ''')
    cfg = load_link_config('mylink', 'tx', config_dir=tmp_path)
    assert cfg.psk == 'from-env'


def test_envvar_unset_raises(tmp_path):
    if 'OPENOB_DEFINITELY_NOT_SET' in os.environ:
        del os.environ['OPENOB_DEFINITELY_NOT_SET']
    _write(tmp_path / 'links' / 'mylink.toml', '''
        port = 4000
        receiver_host = "10.0.0.1"
        security = "psk"
        psk = "${OPENOB_DEFINITELY_NOT_SET}"
    ''')
    with pytest.raises(ConfigError, match='envvar.*not set'):
        load_link_config('mylink', 'tx', config_dir=tmp_path)


# --- OPENOB_PSK / OPENOB_ROOT_PSK env-var fallback ----------------------


def test_openob_psk_env_fallback(tmp_path, monkeypatch):
    """When TOML doesn't set psk but security=psk is in effect, fall back
    to OPENOB_PSK from the environment."""
    monkeypatch.setenv('OPENOB_PSK', 'env-fallback-psk')
    _write(tmp_path / 'links' / 'mylink.toml', '''
        port = 4000
        receiver_host = "10.0.0.1"
        security = "psk"
    ''')
    cfg = load_link_config('mylink', 'tx', config_dir=tmp_path)
    assert cfg.psk == 'env-fallback-psk'


def test_openob_root_psk_env_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENOB_ROOT_PSK', 'env-root')
    _write(tmp_path / 'links' / 'mylink.toml', '''
        port = 4000
        receiver_host = "10.0.0.1"
        security = "psk"
    ''')
    cfg = load_link_config('mylink', 'tx', config_dir=tmp_path)
    assert cfg.root_psk == 'env-root'


def test_toml_psk_overrides_env_fallback(tmp_path, monkeypatch):
    """TOML/file-set PSK wins over OPENOB_PSK — env is fallback only."""
    monkeypatch.setenv('OPENOB_PSK', 'env-fallback')
    _write(tmp_path / 'links' / 'mylink.toml', '''
        port = 4000
        receiver_host = "10.0.0.1"
        security = "psk"
        psk = "from-toml"
    ''')
    cfg = load_link_config('mylink', 'tx', config_dir=tmp_path)
    assert cfg.psk == 'from-toml'


def test_env_fallback_works_without_link_file(tmp_path, monkeypatch):
    """The full ad-hoc CLI use case: no TOML on disk, --security psk via
    CLI override, OPENOB_PSK in env."""
    monkeypatch.setenv('OPENOB_PSK', 'cli-time-psk')
    cfg = load_link_config(
        'adhoc', 'tx',
        config_dir=tmp_path,
        cli_overrides={
            'port': 5000,
            'receiver_host': '127.0.0.1',
            'security': 'psk',
        },
    )
    assert cfg.security == 'psk'
    assert cfg.psk == 'cli-time-psk'


# --- OPENOB_CONFIG_DIR env-var fallback ---------------------------------


def test_openob_config_dir_env_used_when_no_explicit_dir(tmp_path, monkeypatch):
    """With no config_dir argument, OPENOB_CONFIG_DIR points the loader at
    a different directory (the systemd-deployment path)."""
    monkeypatch.setenv('OPENOB_CONFIG_DIR', str(tmp_path))
    _write(tmp_path / 'links' / 'mylink.toml', '''
        port = 4000
        receiver_host = "10.0.0.1"
    ''')
    cfg = load_link_config('mylink', 'tx')
    assert cfg.port == 4000
    assert cfg.receiver_host == '10.0.0.1'


def test_explicit_config_dir_overrides_env(tmp_path, monkeypatch):
    """--config-dir / config_dir argument takes precedence over the env var."""
    bogus = tmp_path / 'bogus'
    bogus.mkdir()
    monkeypatch.setenv('OPENOB_CONFIG_DIR', str(bogus))

    real = tmp_path / 'real'
    _write(real / 'links' / 'mylink.toml', '''
        port = 4321
        receiver_host = "10.0.0.1"
    ''')
    cfg = load_link_config('mylink', 'tx', config_dir=real)
    assert cfg.port == 4321


# --- TOML flattening: legacy [tx]/[rx] sections rejected -----------------


def test_legacy_tx_section_rejected(tmp_path):
    _write(tmp_path / 'links' / 'mylink.toml', '''
        port = 4000
        receiver_host = "10.0.0.1"

        [tx]
        audio_input = "alsa"
    ''')
    with pytest.raises(ConfigError, match='flattened the TOML'):
        load_link_config('mylink', 'tx', config_dir=tmp_path)


def test_legacy_rx_section_rejected(tmp_path):
    _write(tmp_path / 'links' / 'mylink.toml', '''
        port = 4000

        [rx]
        audio_output = "alsa"
    ''')
    with pytest.raises(ConfigError, match='flattened the TOML'):
        load_link_config('mylink', 'rx', config_dir=tmp_path)


def test_audio_keys_at_top_level_pass_validation(tmp_path):
    """Audio fields at top level shouldn't trigger 'unknown key' rejection
    (they're handled by AudioInterface, but live in the same TOML)."""
    _write(tmp_path / 'links' / 'mylink.toml', '''
        port = 4000
        receiver_host = "10.0.0.1"
        audio_input = "alsa"
        alsa_device = "hw:0"
    ''')
    cfg = load_link_config('mylink', 'tx', config_dir=tmp_path)
    assert cfg.port == 4000
    # Audio fields aren't in the LinkConfig dataclass, but the loader
    # accepts them (AudioInterface picks them up separately).


# --- derived caps ---------------------------------------------------------


def test_derived_caps_opus_no_security():
    cfg = LinkConfig(
        name='x', mode='tx', encoding='opus', channels=2,
        port=3000, receiver_host='1.2.3.4',
    )
    caps = cfg.derived_rtp_caps()
    assert caps.startswith('application/x-rtp,')
    assert 'encoding-name=(string)OPUS' in caps
    assert 'clock-rate=(int)48000' in caps
    assert 'channels=(int)2' in caps


def test_derived_caps_opus_psk():
    cfg = LinkConfig(
        name='x', mode='tx', encoding='opus', security='psk', psk='k',
        port=3000, receiver_host='1.2.3.4',
    )
    caps = cfg.derived_rtp_caps()
    assert caps.startswith('application/x-srtp,'), caps


def test_derived_caps_pcm_requires_samplerate():
    cfg = LinkConfig(
        name='x', mode='tx', encoding='pcm', samplerate=48000, channels=2,
        port=3000, receiver_host='1.2.3.4',
    )
    caps = cfg.derived_rtp_caps()
    assert 'encoding-name=(string)L16' in caps
    assert 'clock-rate=(int)48000' in caps


# --- retransmission_time_limit ------------------------------------------


def test_retransmission_time_limit_defaults_to_zero(tmp_path):
    _write(tmp_path / 'links' / 'mylink.toml', '''
        port = 4000
        receiver_host = "10.0.0.1"
    ''')
    cfg = load_link_config('mylink', 'tx', config_dir=tmp_path)
    assert cfg.retransmission_time_limit == 0
    assert cfg.tx_host is None


def test_retransmission_time_limit_set(tmp_path):
    _write(tmp_path / 'links' / 'mylink.toml', '''
        port = 4000
        receiver_host = "10.0.0.1"
        retransmission_time_limit = 200
    ''')
    cfg = load_link_config('mylink', 'tx', config_dir=tmp_path)
    assert cfg.retransmission_time_limit == 200


def test_retransmission_time_limit_negative_rejected(tmp_path):
    _write(tmp_path / 'links' / 'mylink.toml', '''
        port = 4000
        receiver_host = "10.0.0.1"
        retransmission_time_limit = -1
    ''')
    with pytest.raises(ConfigError, match='retransmission_time_limit'):
        load_link_config('mylink', 'tx', config_dir=tmp_path)


def test_retransmission_time_limit_via_cli_override(tmp_path):
    _write(tmp_path / 'links' / 'mylink.toml', '''
        port = 4000
        receiver_host = "10.0.0.1"
    ''')
    cfg = load_link_config(
        'mylink', 'tx',
        config_dir=tmp_path,
        cli_overrides={'retransmission_time_limit': 250, 'tx_host': '10.0.0.5'},
    )
    assert cfg.retransmission_time_limit == 250
    assert cfg.tx_host == '10.0.0.5'


# --- sanitize_caps_for_wire ----------------------------------------------


def test_sanitize_strips_srtp_key():
    raw = (
        'application/x-srtp,media=(string)audio,encoding-name=(string)OPUS,'
        'srtp-key=(buffer)deadbeef,srtp-cipher=(string)aes-128-icm,'
        'clock-rate=(int)48000'
    )
    out = sanitize_caps_for_wire(raw)
    assert 'srtp-key' not in out
    assert 'srtp-cipher' not in out
    assert 'encoding-name=(string)OPUS' in out
    assert 'clock-rate=(int)48000' in out


def test_sanitize_passes_through_plain_rtp():
    raw = 'application/x-rtp,media=(string)audio,clock-rate=(int)48000'
    assert sanitize_caps_for_wire(raw) == raw
