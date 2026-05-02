# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What it is and why it exists

OpenOB is outside-broadcast/studio-transmitter-link software - it is designed to reliably move audio from A to B without interruption, disruption, or supervision. It's critical that the default behaviours are to keep audio flowing. Network links are assumed to be unreliable and flaky, with variable loss, latency, and jitter, with only one end assumed to have firewall ports reachable inbound from the remote end.

The users are predominantly low-Python-skill audio and radio engineers. The software is usually run on low-end machines including Raspberry Pis, so architecture independence/compatibility and efficiency is important.

## Running

OpenOB is invoked via the `openob` console script (installed by pip from `openob.cli:main`). Three computers are typically involved: a transmitter, a receiver, and a Redis configuration server (often co-located with the receiver). Both ends must reach the same Redis host, and the link name must match on both ends.

```bash
# Receiver (start first; blocks waiting for tx caps)
openob <config_host>[:<port>] <node_name> <link_name> rx

# Transmitter
openob <config_host>[:<port>] <node_name> <link_name> tx <receiver_host>
```

`<config_host>` accepts an optional `:port` (default 6379).

Install for development: `pip install -e .[test]` inside a venv created with `--system-site-packages` (PyGObject's `python3-gi` has no wheel — comes from apt). The Docker image in `Dockerfile` does this for you.

## Tests

Real end-to-end tests live in `tests/`. They spin up a private redis-server, run real `openob` rx + tx subprocesses, inject pink-noise via `audiotestsrc` on the TX, capture the result to a WAV via `wavenc → filesink` on the RX, and assert the captured WAV is non-silent (RMS above -40 dBFS). PCM and Opus paths are both covered.

```sh
docker compose run --rm tests       # primary path; works on Windows too
./scripts/local_ci.sh                # equivalent wrapper
```

CI runs the same Docker container on GitHub Actions (`.github/workflows/ci.yml`).

## Architecture

The system is genuinely distributed: TX and RX processes coordinate via Redis, not directly. Understanding this asymmetry is essential before changing anything.

**Configuration flow (`openob/link_config.py`):** All link parameters live in Redis under keys `openob:<link_name>:<field>`. The TX process writes its full config on startup via `set_from_argparse`. The RX process reads it back — including a `caps` field that the TX populates only *after* its GStreamer pipeline has negotiated (see `node.py` and `tx.py`'s `run` loop). RX uses `blocking_get("caps")` to wait for this, which is why a receiver started before its transmitter "flails around" (this is by design).

`LinkConfig.__getattr__` proxies to `get()`, so `link_config.port` is a Redis round-trip every access. Type coercion (int / bool) is hardcoded in two tuples at the top of `LinkConfig` — adding new typed fields requires updating those.

**Supervision (`openob/node.py`):** `Node.run_link` is an infinite `while True` that catches exceptions from the GStreamer pipeline and restarts it. RX additionally exits its main loop on a `GstUDPSrcTimeout` after 3 seconds of no data, which causes Node to recreate the receiver. There is no clean shutdown — SIGTERM/Ctrl-C is the documented exit method (the test harness exploits this).

**GStreamer pipelines (`openob/rtp/{tx,rx}.py`):** Each side builds three Bins linked in series:

- TX: `audio` (source → level → resample → convert → capsfilter) → `encoder` (opusenc/rtpopuspay or rtpL16pay) → `transport` (rtpbin → udpsink)
- RX: `transport` (udpsrc → rtpbin → valve) → `decoder` (rtpopusdepay/opusdec or rtpL16depay) → `audio` (resample → convert → level → sink)

The `level` element fires periodic bus messages; the first one received flips a `started` flag and logs "Started/Receiving … audio transmission" (the test harness greps for this to know the pipeline is live). The `valve` on RX exists because `rtpbin` only creates its src pad once packets arrive — `rtpbin_pad_added` re-links it when that happens.

PCM mode skips the encoder/decoder element but keeps the payloader/depayloader. The `if encoder is not None` / `if decoder is not None` branches in the bin builders handle this.

The RX `audio` bin has a fifth output type, `wav`, that builds `wavenc → filesink` for test capture (`-a wav --wav_file PATH`). It is otherwise structurally identical to the standard sink branch.

**AudioInterface (`openob/audio_interface.py`):** Per-node, *not* shared via Redis. Each end picks its own input/output type independently (alsa/jack/auto/test/wav). Same `__getattr__`-to-`get()` pattern as LinkConfig, backed by an in-process dict.

## Things to know when changing code

- New CLI flags need wiring in three places: `openob/cli.py` (argparse), `link_config.py` `set_from_argparse` (if shared across nodes) or `audio_interface.py` `set_from_argparse` (if local), and the consumer in `rtp/tx.py` or `rtp/rx.py`. Typed Redis-stored fields also need adding to `int_properties` / `bool_properties` on `LinkConfig`.
- `gi.require_version('Gst', '1.0')` and `Gst.init(None)` happen at module import time in `rtp/tx.py` and `rtp/rx.py` — `node.py` defers those imports to `run_link()` so `openob --help` and the test harness can import `openob.cli` without initialising GStreamer.
- `LinkConfig` and `AudioInterface` use `__getattr__` to proxy attribute access to `.get()`. This means any *typo* on an attribute returns whatever `.get()` does for an unknown key — `LinkConfig` returns `None` (and may then crash with a `TypeError` when the caller does arithmetic on it), `AudioInterface` raises `KeyError`. Be deliberate when adding new fields.
- Type hints are present on our own code (`from __future__ import annotations` + PEP 604 unions) but not enforced by mypy in CI. GStreamer GI bindings have no stubs, so `rtp/tx.py` and `rtp/rx.py` annotate signatures only — bodies that touch `Gst.*` are intentionally un-annotated.
