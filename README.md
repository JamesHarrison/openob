# OpenOB

*There is now a mailing list available for [OpenOB users](http://lists.talkunafraid.co.uk/listinfo/openob-users) to share experiences and discuss future development.*

OpenOB (Open Outside Broadcast) is a simple Python/GStreamer based application which implements a highly configurable RTP-based audio link system.

It is intended to allow professional broadcasters to avoid expensive 'codec' hardware, which is typically costed based on quality - you pay a premium for better quality or lower latencies. OpenOB supports Opus, a high-quality efficient low-latency codec, and allows you to run all of this off commodity computer hardware at much much lower cost, lowering the bar to entry for broadcasters, allowing professional quality contribution links to be set up with nothing more than an entry-level computer at each end.

While outside broadcast contribution links are the primary use-case, the system is also usable for other tasks requiring a high-reliability audio link, such as studio-studio links and studio-transmitter links.

## Features

* IETF standard Opus codec - variable bandwidth and bitrate, 16-384kbps, superior to practically everything
* Linear PCM mode for transparent audio transit over 1600kbps capable connections (LANs, fast wifi)
* Trivial configuration and setup via command line
* Transmitter-configured receivers for standalone receiver operation and control
* Low latency performance (under 5ms PCM, under 50ms Opus) with variable jitter buffer
* Automatic link recovery
* Simple command-line interface for operators, color coded level feedback


OpenOB is 100% open source, lightweight, and runs on a Raspberry Pi or similar mini-SBCs - with it you can build a broadcast quality STL or OB rig for under £200, bringing IP STLs and OBs to student and community radio who could previously not afford £3500 an end links. It can run over wired or wireless networks as well as 3G cellular data networks.

## Development Status

OpenOB is stable and suitable for production usage.

If you intend to put it into production, test thoroughly before doing so, as with any other system you might deploy.

## Technical Information

OpenOB is a single program which can be run as a transmitter or receiver. It is configured by command-line options.

By default the Opus codec is used for audio transport. This is a codec equally suited to music as it is to speech, and which is very bandwidth efficient, allowing good quality links at as low as 16 or 32kbps with very high quality links at 96kbps and above. It's also patent-free and unencumbered. Opus' predecessor, CELT, is also available for legacy systems.

Additionally, OpenOB can move raw little-endian PCM 16-bit audio around, allowing for entirely transparent lossless audio links for studio-transmitter links or OBs where a high quality link is available.

The actual network protocol used to move audio is the Real Time Protocol.

Audio level monitoring is provided via simple printouts at each end for easy debugging. Network failures are recovered from automatically. Level monitoring of each end along with timestamps are provided bidirectionally for debugging and monitoring, so you can easily see what's going on at each end.

Latency is by default configured limited on jitter buffer size with a 150ms buffer, which works well on congested switched networks with no QoS. 10ms and lower are possible on suitable hardware with a suitable connection.

## Usage/Installation

### Operating System

*It is recommended that you run OpenOB on a dedicated system to avoid glitches* unless you know what you're doing with regards to real-time kernels and priorities.

The primary operating system that OpenOB is developed on is Debian, currently the Wheezy version in testing. It's recommended you use a server version of the OS without a graphical interface to avoid additional overhead. That said, OpenOB is very lightweight and does cope fine with a desktop UI alongside it on relatively chunky hardware (something newer than a Pentium 4 with a GHz or so kicking about).

### Non-Python Dependencies

On Debian Wheezy and later you should be able to just run the following:

    sudo apt-get install python-gst0.10 python-setuptools gstreamer0.10-plugins-base gstreamer0.10-plugins-bad \ 
    gstreamer0.10-plugins-good gstreamer0.10-plugins-ugly gstreamer0.10-ffmpeg gstreamer0.10-tools \
    python-gobject python-gobject-2 gstreamer0.10-alsa python-argparse

You will also need to install Redis on a machine - it does not need to be on the receiver or transmitter, but the receiver machine is probably where you want to put it.
 
    sudo apt-get install redis-server

You then need to edit /etc/redis/redis.conf and instruct redis to bind to your external IP address or 0.0.0.0, then restart redis.

### Installing

    sudo easy_install OpenOB

That's it!

You can now run OpenOB. `openob -h` will list help; `openob dummyhost someconfig rx -h` will give help for the receiver mode, for instance.

### Example Usage

Basic usage in all cases is this - you need a receiver and a transmitter, the receiver runs like this:

    openob rx.example.org test-link rx

The transmitter like this:

    openob rx.example.org test-link tx rx.example.org

This assumes rx.example.org is both your receiving host and your Redis server. 

This will default to 96kbps Opus encoded audio from ALSA. You can vary the bitrate with the `-b` flag, eg `-b 192` for high quality or `-b 16` to see how speech works at low bitrates.


## Dependencies

* Linux (Debian Wheezy tested, Ubuntu 12.04 works fine for CELT/PCM but doesn't support Opus)
* gstreamer 0.10, plus plugins (alsasrc/jackaudiosrc, opusenc and rtpopuspay/rtpL16pay bins required - install good, bad and ugly packages, plus any gstreamer-alsa packages for your distro)
* Python 2.7 or lower (3.x tentatively supported)
* Python gstreamer bindings
* Python redis bindings
* Python gobject bindings
* JACK server (optionally, ALSA is fine though)
* Redis server (only needed on the configuration server, usually configured on the receiver)

## Networking

OpenOB requires network connectivity of at least 128kbps to support a stable link. A recommended bandwidth of 256kbps permits some more leeway when it comes to IP overhead and so on.

You need ports open to accept audio coming into a link. By default OpenOB needs ports 3000-3002/udp open, along with 6379/tcp for Redis on your configuraiton host (usually your receiver). You do not need to open these on a transmitter, only a receiver.

You can change the port range used with the command line flag -p, which sets the first port number.

If you want to run bidirectional links within networks that you do not have ingress access to, OpenVPN may provide a solution. This has not, however, been tested. Ensure you are running OpenVPN in UDP mode.

## Troubleshooting

### Audio levels

OpenOB tries to be really obvious and provides extreme visual feedback in the terminal for users. The level readout will be suffixed with !!! LEVELS !!! if the input is close to clipping, and it will be suffixed with !!! CLIP !!! if it's very very close. These appear in orange or red backgrounds.

Normal communication shows up in green to indicate how well everything is going.

### Intermittent glitching

Check the CPU available on each machine - OpenOB should be using under 70% and nothing else should be getting in the way.

If the CPU is not an issue, adjust the jitter buffer to give yourself more resistance against network disruption.

### Redis Unreachable

    -- Unable to configure myself from the configuration host; has the transmitter been started yet? (Error 111 connecting some-address:6379. Connection refused.)

This tends to mean your RX can't talk to Redis. Check you've edited /etc/redis/redis.conf to either comment out the bind 127.0.0.1 default or set bind to your public IP, then restart Redis. You may also have a firewall issue - ensure TCP port 6379 is open.

### Silence/-700 level

Check your sound card with alsaconfig, and ensure you're passing the right device (arecord -l to get a list) to OpenOB.

### Element unavailable

     -- Couldn't fulfill our gstreamer module dependencies! You don't have the following element available: opusenc

If opusenc shows as unavailable you likely don't have the GStreamer bad plugins package installed or it's too old to have Opus. Run `gst-inspect opus` to see what you've got. You can fall back to the CELT encoder/decoder with `-e celt` on the command line on the transmitter.

## Hardware Requirements

### Recommended

* Intel Atom 1.6GHz or better
* 512MB or more system memory
* External sound card and external power source
* 256kbps synchronous WAN/LAN connection (encoded)
* 10Mbps synchronous WAN/LAN connection (lossless)

### Minimum tested

* Broadcom ARM1176JZF-S 700 MHz (Raspberry Pi SOC CPU) or equivalent (Pentium 4/Celeron D or higher)
* 256MB of RAM
* External sound card strongly recommended (Focusrite Scarlett 2i2 tested on RPi with USB bus power and 2A PSU)

The Raspberry Pi Single-Board Computer is an active development target for OpenOB and is entirely supported without modifications to hardware or software.

## Real-World Users

This list is new; if you're using OpenOB in the real world (or testing/developing with it/evaluating it), let me know so you can be added to the list (or just add yourself in a fork and throw in a pull request).

* [Insanity Radio 103.2FM](http://insanityradio.com/) - Studio-Transmitter Link for FM, outside broadcasts from events
* [Stafford FM](http://staffordfm.com/) - Studio-Transmitter Link
* [CSR FM](http://www.csrfm.com/) - FestiNet - multi-stage outside broadcast (Lounge on the Farm 2013)
* [Clear Channel Media + Entertainment](http://www.clearchannel.com/CCME/Pages/default.aspx) - Various dedicated audio links

## Licensing and Credits

OpenOB was developed by James Harrison, with chunks of example code used from Alexandre Bourget and various other GStreamer documentation sites such as the PyGST manual.

Copyright (c) 2012, James Harrison

All rights reserved.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following  conditions are met:
    * Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
    * Neither the name of the OpenOB project nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL JAMES HARRISON OR OTHER OPENOB CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
