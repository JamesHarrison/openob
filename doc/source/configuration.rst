Configuring OpenOB
==================

OpenOB is configured at runtime on the command line on the transmitter. The receiver has configuration to indicate what 

.. _bitrates_and_bandwidths:

Bitrates and Bandwidths
-----------------------

In linear PCM mode the input sample rate of your sound card determines the sample rate of the link. You can in theory run 192kHz LPCM links (to transport broadcast FM modulation, for instance) but this is not tested. 48kHz audio uses around 1400kbps. You cannot make use of OpenOB's redundancy features (PLC, FEC etc) as these are Opus features.

In Opus mode, the input sample rate is constrained by Opus' requirements (with 48kHz being the typical maximum) and bitrate can be set between 16 and 384kbps. Lower bitrates or input sample rates imply lower Opus bandwidth modes.

Framing Overhead
~~~~~~~~~~~~~~~~

OpenOB supports multiple framing sizes for Opus, configurable with --framesize. This defaults to 20ms frames, which induces a 22.5ms algorithmic delay in the codec. In 20ms frame mode, the bitrates specified on the command line will closely match on-the-wire bitrates.

When using lower frame sizes, the amount of framing overhead increases and the algorithmic latency decreases. In most cases, leaving frame size alone is recommended. Where very low latency is required, 10ms frame sizes only incur a 10% bitrate overhead, as shown in the table below.

========== ================= ========
Frame Size Algorithmic Delay Overhead
---------- ----------------- --------
20ms       22.5ms            0%
10ms       12.5ms            10%
5ms        7.5ms             32.5%
2.5ms      5ms               75%
========== ================= ========

In the worst-case 2.5ms frame size, a 64kbps configured bitrate will consume around 112kbps on the wire. 

Example Bitrates
~~~~~~~~~~~~~~~~

These bitrates are not definitive and are provided as a suggestion only. Work is ongoing within standards bodies such as the EBU to define appropriate bitrates for Opus in professional usage.

======= =====
Bitrate Usage
------- -----
256kbps Full-bandwidth music; studio-transmitter links, etc
128kbps Full-bandwidth music; bitrate constrained contribution feed recommended minimum
64kbps  Wide-bandwidth music; minimum recommended
64kbps  Speech; high quality wide-bandwidth
32kbps  Speech; medium quality wide-bandwidth
24kbps  Speech; medium quality medium-bandwidth; bitrate constrained contribution feed recommended minimum
16kbps  Speech; low quality narrowband
======= =====

.. _firewall-configuration:

Firewall Configuration
----------------------

You must have the following ports open between the transmitter and receiver:

* UDP 3000
* TCP 6379

If you need to negotiate a firewall or Network Address Translation (NAT) gateway, you may wish to run OpenOB within a VPN tunnel; this can be done so long as the tunnel itself uses UDP (to allow for loss to occur without incurring retransmission delays).

.. _delay-management:

Delay Management
----------------

Any audio over IP system suffers from delay. Delays are incurred:

* Converting analogue audio to digital audio
* Converting digital audio to analogue audio
* Sending IP data across the network
* Reordering received packets into a consistent bitstream

Delays can be mitigated by system configuration - for instance, using lower buffer sizes on sound card interfaces, or using a soft real time preemptive kernel optimized for real time audio usage. IP network reliability and consistency can have a huge impact on the required size of jitter buffers, and latency of the network of course defines the absolute minimum latency of a system.

Documentation on optimization of Linux systems for real time usage is outside the scope of this document, but it is a well-trodden topic and many resources exist.
