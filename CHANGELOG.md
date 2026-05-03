## 6.1.0

* **RTP retransmission.** New TX-side `retransmission_time_limit` option (also `--retransmission-time-limit`, in milliseconds) wires `rtprtxsend` and `rtprtxreceive` into the pipeline so dropped packets can be resent on RTCP NACK feedback. RX picks up RTX support from the in-band caps signal — no RX-side config required in the normal case. RTCP NACKs flow back to TX over the same UDP socket the audio rides on (true RTCP-mux), so this works through TX-side NAT without operator port forwards. New `tx_host` field is available for the `caps_from_config` + RTX combination. TX logs `RTX stats: nack-requests=N packets-resent=M` once per second when retransmission is enabled, so operators can confirm the link is actually doing recovery work under loss.
* **Loss-recovery test coverage.** New `test_rtx_recovers_from_packet_loss` injects 30% packet loss via `netsim` (gated behind the `OPENOB_TEST_PACKET_LOSS_PERCENT` env var) and asserts that `rtprtxsend` actually retransmits AND the captured WAV is continuous — every 20 ms window stays above the dropout floor, not just the long-term average. 30% was picked empirically: at lower loss Opus FEC handles things alone (so the test wouldn't differentiate working RTX from broken RTX), at higher loss Opus' decoder also breaks down. At 30%, ~60% of unrecovered runs leave at least one audible gap in our probes; with RTX every run is gap-free.
* **Continuity assertions across the round-trip suite.** All happy-path round-trip tests now run a 20 ms-windowed scan over the captured WAV (`wav_dropout_count_dbfs` in `tests/helpers.py`) and fail if any window dips below -40 dBFS. Mean RMS is preserved as a coarser sanity check, but only the windowed scan would catch a brief silent gap from a regression in the jitter buffer, encoder, or packet-handling paths.

## 6.0.0

Major release. Drops the Redis dependency entirely and adds optional SRTP encryption. The one-shot `openob migrate-v5` command translates v5 Redis configs into v6 TOML.

This is an incompatible change due to bitstream changes if moving to the new (recommended) encryption - upgrade both ends at the same time if you're trying to keep downtime low.

### Headline changes

* **Config files exist:** OpenOB can now be configured either by CLI or config file (TOML)
* **Redis is gone.** No more `redis-server` to install, no more `--config-host` positional, no more "RX flails until TX writes caps." Each end reads a per-link TOML at `~/.openob/links/<link>.toml` at startup and the only runtime cross-side communication is the RTP/RTCP stream itself, which uses the same single port previously used.
* **Optional encryption and authentication of audio via pre-shared key.** New `security = "psk"` mode wires `srtpenc`/`srtpdec` (AES-128-ICM + HMAC-SHA1-80, RFC 3711 default profile). The 30-byte SRTP master key+salt is derived deterministically from the PSK + link name via HKDF-SHA256, so both ends arrive at the same key without any over-the-wire exchange. Default stays `security = "none"` (cleartext, v5-equivalent on the wire).
* **PSK provisioning options:** per-link `psk` in config files (environment variable substitution supported), or `OPENOB_PSK` / `OPENOB_ROOT_PSK` environment variables

### New features

* **In-band signalling.** Replaces the v5 Redis-stored `caps` field. TX broadcasts an OpenOB-specific RTCP APP packet (`OPOB`, subtype 0) every 2 s carrying the codec parameters. RX bootstrap-waits for the first one, then attaches a pad-probe to drop subsequent RTCP from rtpbin's input. Supports a `--caps-from-config` opt-in for STL deployments wanting deterministic startup.
* **RX adapts to TX codec on the fly.** RX picks `encoding`, `samplerate`, `channels`, `bitrate` up from the in-band caps signal, so the RX TOML only needs `port` + `security` + audio output. Change codec params on TX and RX adapts within ~2 seconds.
* **Layered loader with explicit precedence:** defaults → `OPENOB_PSK` env vars → `secrets.toml [defaults]` → per-link TOML → CLI flags. CLI flags can override anything in the TOML, so trivial 1:1 setups can skip the TOML entirely.
* **One-shot v5 migration helper.** `openob migrate-v5 <redis_host> <link_name>...` reads v5 Redis configs and prints v6 TOML on stdout. Optional dep on `redis` (`pip install "openob[migrate]"`); not a runtime dep of v6.

### CLI surface

* New shape: `openob {tx,rx,migrate-v5} <link_name> [opts]`. Removed the v5 `<config_host>` and `<node_name>` positionals; `--node-name` is now an optional flag (defaults to the machine hostname; only used for log labelling).
* CLI flag names normalised to kebab-case (`--receiver-host`, `--audio-input`, `--alsa-device`, `--opus-fec` / `--no-opus-fec`, `--caps-from-config`, etc.). Short forms preserved where they were unambiguous (`-p`, `-e`, `-b`, `-r`, `-c`, `-a`, `-d`, `-v`).
* New `--security {none,psk}` and `--caps-from-config`.
* New `OPENOB_CONFIG_DIR` env var to override `~/.openob` (used by the systemd examples).

### Dependencies

* **Removed:** `redis>=3` runtime dep, `redis-server` apt package.
* **Added:** `cryptography>=42` (HKDF). New optional `[migrate]` extra pulls in `redis` for the v5 migration helper.
* **Required apt packages:** added `gstreamer1.0-plugins-bad` for the SRTP elements.
* **Python:** 3.9+ supported. Stdlib `tomllib` is used on 3.11+; on 3.9/3.10 we depend on the PyPI `tomli` package (same API). Debian 11 Bullseye (Python 3.9) and Debian 12 Bookworm (Python 3.11) are both covered.

### Other known v6.0 limitations

* **No opportunistic encryption mode.** Considered but skipped — the natural fit (ZRTP via `gstzrtp`) is not in mainstream `gst-plugins-bad` packaging, which is a non-starter for the Raspberry Pi / `apt install` audience. v7 candidate if/when that lands upstream.

## 5.0.1

* Lowered the Python floor to 3.9 so OpenOB runs on pre-Bookworm distros (Debian 11 Bullseye = Python 3.9, GStreamer 1.18). All `X | Y` annotations are already gated by `from __future__ import annotations` so the change is metadata-only on the source side.
* Added a tiny GStreamer-version-compat shim: `request_pad_simple` (1.20+) falls back to `get_request_pad` (1.18 and earlier) so `rtpbin` pad-request works across the full 1.18 → current range.
* Verified end-to-end on both Debian 12 / GStreamer 1.22 / Python 3.11 *and* Debian 11 / GStreamer 1.18 / Python 3.9.
* Widened the `redis` dependency to `>=3` (no upper bound) so OS-supplied installs (Debian 11 ships redis-py 3.5.x) coexist with the latest releases.

## 5.0.0

* Revival release: codebase modernised for current Python and GStreamer
* Python 3.10+ required (Python 2 support dropped); type hints added across the project
* GStreamer 1.22+ required: replaced deprecated `get_request_pad` with `request_pad_simple`, switched to canonical hyphenated property names (`auto-multicast`, `multicast-group`, `post-messages`)
* redis-py 5.x compatible: switched from `StrictRedis` to `Redis`, dropped removed `charset` kwarg, fixed boolean type-coercion bug in `LinkConfig.get`
* Fixed Python 2 tuple-style `raise` in `LinkConfig.commit_changes`
* Configuration host now accepts `host:port` form for non-default Redis ports
* New `--test_wave` flag on `openob tx -a test` to select audiotestsrc waveform (sine, pink-noise, white-noise, etc.)
* New `wav` audio output mode on `openob rx` (`-a wav --wav_file PATH`) for capturing the decoded stream to a WAV file — used by the test harness
* Switched packaging from `setup.py` to `pyproject.toml` (PEP 621)
* New end-to-end test harness in `tests/` covering pink-noise round-trip over both PCM and Opus, asserting captured WAV is non-silent
* Replaced Travis CI with GitHub Actions running tests in a Docker Compose container; same `docker compose run --rm tests` works locally on any platform

## 4.0.0-dev

* Upgraded GStreamer libraries to ^1.0
* Added Python 3 compatability

## 3.1

* Improved command line interface (Jonty Sewell)
* Bugfix for PCM mode operation (Jonty Sewell)

## 3.0.3

* Packaging bugfixes

## 3.0.0

* Released

## 3.0.0a1

* Refactored the Manager into Node, LinkConfig, AudioInterface and Logger classes
* Refactored logging to use Python's logging framework
* Removed audio level feedback on the command line
* Moved configuration into objects for shared and node-specific (audio interface) values
* Removed PulseAudio element support (except through the 'auto' interface type)
* Removed variable size input queue, since this was an attempt to fix the audiorate related glitches but made no difference
* Removed CELT support; only Opus is now supported for compressed audio
* Removed colorama dependency
* Enabled support for disabling automatic connection under JACK (Chris Roberts)
* Added support for asking the audio interface for a sample rate (Chris Roberts)

## 2.3.6

* Fixed level messages reporting incorrect channel counts

## 2.3.5

* Added multicast support (Jonty Sewell)
* Added automatic audio interface selection support (Jonty Sewell)

## 2.3.4

* Minor bugfix

## 2.3.3

* Added support for Opus features:

    * Discontinuous transmission
    * In-band Forward Error Correction (and loss % assumption)
    * Frame size selection

## <2.3.3

* Check the git changelog for these changes.
