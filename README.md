# OpenOB
[![PyPI version](https://badge.fury.io/py/OpenOB.png)](http://badge.fury.io/py/OpenOB) [![CI](https://github.com/JamesHarrison/openob/actions/workflows/ci.yml/badge.svg)](https://github.com/JamesHarrison/openob/actions/workflows/ci.yml)

OpenOB (Open Outside Broadcast) is a simple Python/GStreamer based application which implements a highly configurable RTP-based audio link system.

It is primarily designed for broadcast applications including (but not limited to) contribution links, emission/studio-transmitter links, talkback, and intranet audio distribution systems.

## Status

OpenOB 5.0 is a revival of the previously-unmaintained codebase, modernised for current Python (3.10+), GStreamer (1.22+), and redis-py (5.x) to support ongoing community usage on modern distros.

## Features

* IETF standard Opus codec - variable bandwidth and bitrate, 16-384kbps
* Linear PCM mode for transparent audio transit over 1600kbps capable connections (LANs, fast wifi)
* Trivial configuration and setup via command line
* Transmitter-configured receivers for standalone receiver operation and control
* Low latency performance (codec internal latency under 5ms PCM, under 25ms Opus) with variable jitter buffer (0-150ms)
* System latency in low hundreds to tens of milliseconds for most applications; more over the internet/lossy links
* Automatic link recovery in the event of failures

## Getting started

Linux is **highly** recommended as the OS of choice for OpenOB operation, though Windows and OSX will work if you can satisfy the dependencies. Debian and derivatives are the primary distro for tests.

OpenOB is a single Python package with minimal dependencies. You'll need to install Python and GStreamer, for example:

```bash
apt-get install -y --no-install-recommends \
        gstreamer1.0-tools \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-libav \
        gir1.2-gstreamer-1.0 \
        python3-gi \
        python3-gst-1.0
```

You also need a Redis server running, which only one end requires - this is usually whichever end doesn't move so much. It doesn't need to be installed on the same host as the audio source or target. This is the `redis-server` package on most distros.

Since most distros now don't permit global package installation, it's recommended to set up a virtual environment to install OpenOB, such as:

```bash
python3 -m venv openob-venv
source openob-venv/bin/activate
pip install openob
openob --help
```

You can run this binary from the global scope by addressing the full path of the binary, which is useful for systemd init scripts:

```bash
$ which openob
/home/james/openob-venv/bin/openob
```

## Running on boot

A templated systemd unit and example per-link config files live in [`examples/systemd/`](examples/systemd/) — one unit handles many TX/RX links, restarts on crash, and routes logs through `journalctl`. See the README in that directory for setup.

## Running tests / development

End-to-end tests live in `tests/` and exercise real network and audio flow against private redis-server and GStreamer pipelines. The simplest path is Docker:

```sh
docker compose run --rm tests
```

This works identically on Linux, macOS, and Windows (with Docker Desktop). For a native Linux/WSL2 setup, see [`tests/README.md`](tests/README.md).


## Licensing and Credits

OpenOB was developed by James Harrison, with chunks of example code used from Alexandre Bourget and various other GStreamer documentation sites such as the PyGST manual.

Since OpenOB 5.0, some contributions were made by Anthropic's Opus 4.7 model using Claude Code. This may have licensing implications, but since it's 3-clause BSD anyway...

Copyright (c) 2018, James Harrison

License is 3-clause BSD:

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following  conditions are met:

* Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
* Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
* Neither the name of the OpenOB project nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL JAMES HARRISON OR OTHER OPENOB CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
