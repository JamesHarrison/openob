# OpenOB
[![PyPI version](https://badge.fury.io/py/OpenOB.png)](http://badge.fury.io/py/OpenOB) [![CI](https://github.com/JamesHarrison/openob/actions/workflows/ci.yml/badge.svg)](https://github.com/JamesHarrison/openob/actions/workflows/ci.yml)

OpenOB (Open Outside Broadcast) is a simple Python/GStreamer-based application that implements a highly configurable RTP-based audio link system.

It's designed for broadcast applications including (but not limited to) contribution links, emission/studio-transmitter links (STL), talkback, and intranet audio distribution.

## Status

**OpenOB 6.0** is a major release that drops the Redis dependency and adds optional SRTP encryption, along with other features for improved reliability. 

6.1 introduced retransmission support; 6.2 introduced security and monitoring improvements along with support for multipath seamless redundancy.

All users are encouraged to upgrade, and to raise GitHub issues for any problems found.

If you're upgrading from v5, see the [migration notes](#upgrading-from-v5) below. v5 stays available on PyPI for users not ready to move.

OpenOB was unmaintained between ~2016 and ~2026, but has been picked back up to ensure continued operation for community radio stations relying on the software. It is maintained "best effort".

## Features

* IETF standard Opus codec — variable bandwidth and bitrate, 16-2048 kbps (supporting Quality Extensions above 1024kbps)
* Linear PCM mode for transparent audio over LAN
* SRTP encryption with a pre-shared-key (PSK), opt-in, off by default
* Configurable jitter buffer and missed-packet retransmission support
* Configuration via per-link TOML files in `~/.openob/links/`
* Single inbound UDP port (RTP+RTCP muxed per RFC 5761) — easy to get your firewall configured for
* Both 1:1 (STL) and N:1 (central + many remote contributor kits, so long as only 1 is active at a time) topologies
* Low latency (codec internal under 5 ms PCM, under 25 ms w/ Opus)
* Multipath support for redundant transmission (e.g. using multiple 4/5G dongles) and automatic recombination (SMTPE ST 2022-7-ish)
* Automatic link recovery on failure
* OpenTelemetry metrics for monitoring, including audio levels and link quality indicators

# Getting started

Linux is **highly** recommended. Debian/Ubuntu derivatives are the primary target. OSX/Windows are supported in principle.

Install GStreamer (including `plugins-bad` for SRTP) and the GI bindings:

```bash
apt-get install -y --no-install-recommends \
        gstreamer1.0-tools \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-libav \
        gir1.2-gstreamer-1.0 \
        python3-gi \
        python3-gst-1.0
```

Set up a venv and install OpenOB:

```bash
python3 -m venv --system-site-packages openob-venv
source openob-venv/bin/activate
pip install openob
openob --help
```

`--system-site-packages` is required so the apt-installed `python3-gi` is visible inside the venv.

## Configuring a link

A link is a defined connection from end A to end B.

Each end has its own TOML file at `~/.openob/links/<link_name>.toml`. The TOML is **flat** — all keys at the top level, no `[tx]` / `[rx]` subsections (each TOML describes one machine, so there's only one role per file).

**The two ends' files are NOT identical.** TX needs everything: codec, bitrate, where to send, where to source audio. RX picks codec parameters up dynamically from TX's in-band caps signal, so the RX file only needs the port to listen on, the audio output, and the security settings.

### Transmitter (full config)

```toml
# ~/.openob/links/stl-main.toml on the TX side

encoding      = "opus"
bitrate       = 128
samplerate    = 48000
channels      = 2
port          = 3000
receiver_host = "studio.example.com"
# For higher-latency, higher-reliability-requirement links, set the receiver's jitter buffer 
# to match or exceed retransmission_time_limit to enable retransmission support
# retransmission_time_limit = 200
# see examples/systemd/openob-link.toml.example for more examples/details

audio_input = "alsa"
alsa_device = "hw:0"
```

### Receiver (minimal config)

```toml
# ~/.openob/links/stl-main.toml on the RX side
# No codec / bitrate / samplerate / channels — RX picks those up
# automatically from the TX's caps signal.

port          = 3000
jitter_buffer = 20      # ms; tune for your link's typical jitter

audio_output = "alsa"
alsa_device  = "hw:0"
```

```bash
# On the studio (RX) machine:
openob rx stl-main

# On the transmitter (TX) machine:
openob tx stl-main
```

### What MUST match between the two ends

| Field | Reason |
|-------|--------|
| `port` | RX binds it; TX targets it |
| `security` (and `psk` / `root_psk`) | both sides must derive the same SRTP master key |
| `multicast` | both must agree on multicast vs unicast transport |
| `receiver_host` (multicast only) | both join the same multicast group |

Everything else — `encoding`, `bitrate`, `samplerate`, `channels`, all `opus_*` tunables — only needs to live in the transmitter's config. The receiver automatically configures itself within 2 seconds of the transmitter starting or changing parameters.

### JACK example

```toml
# ~/.openob/links/jack-stl.toml on the TX side
encoding      = "opus"
bitrate       = 192
port          = 3000
receiver_host = "studio.example.com"

audio_input  = "jack"
jack_name    = "openob-stl-tx"
jack_auto    = true              # auto-connect to physical inputs
# jack_port_pattern = "system:capture_*"
```

```toml
# ~/.openob/links/jack-stl.toml on the RX side
port = 3000

audio_output = "jack"
jack_name    = "openob-stl-rx"
jack_auto    = true              # auto-connect to physical outputs
# jack_port_pattern = "system:playback_*"
```

### CLI flags vs the TOML file

CLI flags **override** the corresponding TOML keys, so anything you can put in the file can also be passed on the command line. The lookup order is, lowest-to-highest priority:

1. Hardcoded defaults
2. `OPENOB_PSK` / `OPENOB_ROOT_PSK` from the environment (PSK fallback only — see below)
3. `~/.openob/secrets.toml` `[defaults]` section (typically `root_psk`)
4. The per-link TOML at `~/.openob/links/<link_name>.toml`
5. CLI flags

Trivial 1:1 setups can skip the TOML entirely and pass everything on the CLI:

```bash
openob tx mylink \
  --receiver-host 1.2.3.4 \
  --port 3000 \
  --encoding opus --bitrate 128 \
  --audio-input test --test-wave pink-noise

openob rx mylink --port 3000 --audio-output alsa --alsa-device hw:0
```

CLI flag names match the TOML keys (with hyphens for readability): `--receiver-host`, `--audio-input`, `--alsa-device`, `--opus-fec` / `--no-opus-fec`, etc. Run `openob tx --help` or `openob rx --help` for the full list.

**PSKs are NEVER accepted on the CLI** — only via the TOML file, the `OPENOB_PSK` / `OPENOB_ROOT_PSK` env vars, or `${OPENOB_PSK_X}` substitution inside the TOML. Putting a PSK in a CLI flag exposes it in `ps`, shell history, and systemd's status output. The fully ad-hoc workflow is one line:

```bash
OPENOB_PSK=correct-horse-battery-staple openob tx mylink \
  --receiver-host 1.2.3.4 --port 3000 --security psk \
  --audio-input alsa --alsa-device hw:0
```

### Overriding the config directory

Both ends respect `OPENOB_CONFIG_DIR` (default `~/.openob`) and the per-invocation `--config FILE` / `--config-dir DIR` flags. The `examples/systemd/` units use `OPENOB_CONFIG_DIR=/etc/openob`.

## Enabling SRTP (PSK mode)

Add a security mode and a PSK to the link config. **Both ends must agree on the mode and PSK.**

```toml
# Per-link PSK
security = "psk"
psk      = "correct-horse-battery-staple"
```

Or, for fleets running many remote kits against one central site, use a root PSK with per-link HKDF derivation. Put the root in a secrets file:

```toml
# ~/.openob/secrets.toml
[defaults]
root_psk = "fleet-master-secret-rotated-quarterly"
```

```toml
# ~/.openob/links/flypack-1.toml
security = "psk"
# psk omitted — derived from root_psk + this link_name
```

PSKs can come from the environment three ways, all keeping secrets off the CLI:

- **TOML envvar substitution** — `psk = "${OPENOB_PSK_FLYPACK1}"` resolves at load time; useful for per-link secrets when a TOML lists many links.
- **`OPENOB_PSK` fallback** — used when `security = "psk"` is in effect but no `psk` is set anywhere; convenient for one-link systemd units (`Environment=OPENOB_PSK=...`).
- **`OPENOB_ROOT_PSK` fallback** — same, but populates `root_psk` (per-link key derived via HKDF from a fleet-wide secret).

PSKs are never accepted on the CLI — that would expose them in `ps` output and shell history.

SRTP uses AES-128-ICM with HMAC-SHA1-80 (RFC 3711 default profile). The on-wire master key + salt is derived from the PSK + link name via HKDF-SHA256, so both sides arrive at the same key without any over-the-wire exchange.

## N:1 topologies (central site + many remote kits)

Each remote/central pair is a separate link with its own TOML file and PSK. The central site runs one `openob rx <link>` per active remote.

```bash
# At the studio:
openob rx flypack-1 &
openob rx flypack-2 &
openob rx flypack-3 &
```

```bash
# At each remote kit:
openob tx flypack-1   # on flypack-1
openob tx flypack-2   # on flypack-2
```

A `root_psk` shared across all sites + per-link HKDF derivation keeps provisioning to one secret to distribute, while still giving each link its own SRTP key.

## Multicast

Set `multicast = true` and use a multicast group address (`239.x.x.x`) as the receiver host. The same TOML works for one or many receivers — all RXes joined to the group share the same SRTP key.

```toml
multicast = true
receiver_host = "239.255.0.42"
```

## Signalling

OpenOB carries the RTP metadata from TX to RX in-band, as RFC 3550 §6.7 RTCP APP packets muxed on the same UDP port as the audio (RFC 5761 `rtcp-mux`). One inbound port at the receiver — no extra firewall hole, and the RX automatically adapts to whatever codec params the TX is using.

For STL deployments where ops want fully deterministic startup (RX builds caps from local config and starts immediately, ignoring any in-band signal), set `caps_from_config = true` in the link config or pass `--caps-from-config` on the CLI and configure the same parameters on the RX as on the TX.

## Upgrading from v5

Run the one-shot migration helper to dump existing v5 Redis configs as v6 TOML:

```bash
# Requires the redis package (one-shot — not a runtime dep of v6):
pip install "openob[migrate]"

openob migrate-v5 redis.example.com:6379 stl-main flypack-1 flypack-2
# → prints TOML to stdout, suitable for ~/.openob/links/<name>.toml
```

Major v6 changes:

* `<config_host>` and `<node_name>` positionals are gone. Use `--config` / `--node-name` (the latter defaults to the machine's hostname).
* The CLI is now `openob {tx,rx,migrate-v5} <link_name> [opts]`.
* All link parameters live in `~/.openob/links/<link_name>.toml` instead of Redis.
* PSK secrets live in the link file or `~/.openob/secrets.toml`, with envvar substitution available.
* New `--security` flag; defaults to `none` (cleartext, v5-compatible behaviour).
* CLI flag names use kebab-case (`--receiver-host`, `--audio-input`) instead of v5's mix of underscored and hyphenated.

## Running on boot

A templated systemd unit and example per-link config files live in [`examples/systemd/`](examples/systemd/) — one unit handles many TX/RX links, restarts on crash, and routes logs through `journalctl`. See the README in that directory for setup.

## Running tests / development

End-to-end tests live in `tests/` and exercise real network and audio flow against real GStreamer pipelines. The simplest path is Docker:

```sh
docker compose run --rm tests
```

This works identically on Linux, macOS, and Windows (with Docker Desktop). For a native Linux/WSL2 setup, see [`tests/README.md`](tests/README.md).

## Licensing and Credits

OpenOB was developed by James Harrison, with chunks of example code used from Alexandre Bourget and various other GStreamer documentation sites such as the PyGST manual.

Since OpenOB 5.0, significant contributions were made by Anthropic's Opus 4.7 model using Claude Code. This may have licensing implications, but since it's 3-clause BSD anyway...

Copyright (c) 2018, James Harrison

License is 3-clause BSD:

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following  conditions are met:

* Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
* Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
* Neither the name of the OpenOB project nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL JAMES HARRISON OR OTHER OPENOB CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
