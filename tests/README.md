# Tests

End-to-end tests that spin up real `openob` rx and tx processes against a real
private redis-server, push pink-noise through audiotestsrc on the TX, and
capture the result to a WAV on the RX. Each test asserts the captured WAV is
non-silent (RMS above a sensible floor) — proving actual audio content
traversed the network, not just that processes started.

## Run inside Docker (recommended; works on Windows / macOS / Linux)

```sh
docker compose run --rm tests
```

Or, equivalently:

```sh
./scripts/local_ci.sh
```

This builds the test image (Debian 12 + GStreamer 1.22 + Python 3.12) and runs
`pytest`. Any args you pass are forwarded to pytest, e.g.:

```sh
docker compose run --rm tests pytest tests/test_endtoend.py::test_pink_noise_round_trip_opus -v
```

## Run natively (Linux / WSL2)

You'll need:

- Python 3.10+
- `redis-server` on PATH
- GStreamer 1.22+ runtime + Python bindings:
  `apt install gstreamer1.0-tools gstreamer1.0-plugins-base
  gstreamer1.0-plugins-good gir1.2-gstreamer-1.0 python3-gi python3-gst-1.0`
- A virtualenv created with `--system-site-packages` so the apt-installed
  `python3-gi` is visible (PyGObject is not installable as a wheel).

Then:

```sh
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e .[test]
pytest tests/ -v
```

## What the tests cover

- `test_pink_noise_round_trip_pcm` — 16-bit linear PCM round-trip. Exercises
  `rtpL16pay`/`rtpL16depay` and the WAV capture pipeline.
- `test_pink_noise_round_trip_opus` — Opus round-trip at 128 kbps. Exercises
  `opusenc`/`opusdec`, FEC/PLC defaults, and the RTP payload path.

Both tests bind redis on an ephemeral port and the RTP audio on a free UDP
port, so they don't collide with anything else running on the machine.
