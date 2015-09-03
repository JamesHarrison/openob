# OpenOB
 [![PyPI version](https://badge.fury.io/py/OpenOB.png)](http://badge.fury.io/py/OpenOB)

*There is now a mailing list available for [OpenOB users](http://lists.talkunafraid.co.uk/listinfo/openob-users) to share experiences and discuss future development.*

OpenOB is a Python/GStreamer daemon and associated tools to let you build audio over IP networks.

It is primarily designed for broadcast applications including (but not limited to) contribution links, emission links, talkback, and intranet audio distribution systems.

OpenOB is in daily use across the world, in student/community radio stations, commercial radio stations and radio/TV production use.

## Features

* IETF standard high quality music/speech Opus codec - variable bandwidth and bitrate, 16-384kbps
* Linear PCM mode for transparent audio transit over 1600kbps capable connections (LANs, fast wifi)
* Low latency performance (~30ms internal to codec, ~100ms end to end easily achieved) with variable jitter buffer sizing
* Centralized configuration capable of high availability and distributed operation for highly available OpenOB clusters
* Complex topologies possible including point-to-point, point-to-multipoint, multicast, and redundant stream point-to-point
* Configuration of any encoders/decoders from any node in the system, nodes automatically reconfigure to match configuration
* Nodes automatically recover from network or encode/decode failures; daemon can be monitored/booted as a regular Linux service

### Changes from 3.x and earlier

* OpenOB 4's new configuration approach takes the old receiver-follows-transmitter model and extends it, enabling any node to configure any encoder, decoder, transmitter or receiver in the cluster remotely; the nodes involved will automatically adjust their configuration and restart components
* More complex topologies are available for audio streaming, including redundant streams
* Python 3 and GStreamer 1.0 are now targeted, enabling new codec features and improving reliability and quality

## Licensing and Credits

OpenOB was originally developed by James Harrison, with chunks of example code used from Alexandre Bourget and various other GStreamer documentation sites such as the PyGST manual.

[Others](https://github.com/JamesHarrison/openob/graphs/contributors) have helped since, notably Jonty Sewell and Chris Roberts.

Copyright (c) 2015 James Harrison

All rights reserved.


OpenOB is licensed under the BSD 3-clause license, as follows:

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:
    * Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
    * Neither the name of the OpenOB project nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL JAMES HARRISON OR OTHER OPENOB CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
