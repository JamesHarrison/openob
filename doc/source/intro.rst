Introduction
============

OpenOB is an audio over IP (AoIP) transciever tool that can manage establishment and recovery of AoIP links in a low overhead manner.

It supports lossless linear PCM audio as well as the Opus audio codec for compressed audio operation. It is designed to operate as a standalone tool, but is also straightforward to use in your own projects, such as for building AoIP hardware.

Architecture
------------

OpenOB is a peer to peer audio streaming system with a central configuration server.

The program itself is a set of Python classes wrapping the Python GObject bindings for the GStreamer media framework, which itself performs the audio encoding/decoding and transmission.

An OpenOB *link* is comprised of a receiver and transmitter pair.

All OpenOB transmitters and receivers communicate configuration data by way of a *configuration server*.

The audio is sent directly between transmitter and receiver using the Real Time Protocol.


Audio Quality
-------------

OpenOB in linear PCM mode with appropriate sound cards is measurably transparent, over any network (assuming properly configured jitter buffer and sufficient bandwidth).

In Opus mode, OpenOB performs as well as the codec permits, which in the case of Opus is very well - at higher bitrates it is generally accepted to be equivalent in quality to higher-bitrate AAC and MP3 (ie 96k Opus beats 128k MP3). At lower bitrates, it also performs well, beating out codecs like G.722 for speech content.

The main limiting factors to quality are the sound cards used in the computers used by OpenOB, and available bitrate.

For most entry-level broadcast purposes, sufficiently well-engineered integrated audio cards may be sufficient for users. However, for professional use, the use of a professional sound card is typically required. OpenOB plays well with ALSA and JACK as well as other GStreamer supported audio sources, giving you excellent out of the box compatibility with a wide range of professional audio cards.

.. sidebar:: Sound Cards

  OpenOB works with any ALSA supported device, and can also make use of the JACK audio server to make use of more complex routing configurations.

  Any card that supports ALSA or can be used via JACK's firewire modules can be used, in other words.

  Tested cards include:

  * Behringer UCA202
  * ART USB Dual Tube

System Requirements
-------------------

OpenOB is relatively lightweight and will run on many systems; the requirement for near-real-time audio does impose more restrictions than mere CPU and RAM requirements.

The following is a recommended set of specifications that are known to run OpenOB and handle external sound cards without issue.

- Dual-core Intel Atom, i3 or better @ 1.2GHz or better
- 512MB RAM (2GB if you want a desktop environment)
- 100Mbps NIC
- Debian Jessie (8.0)

OpenOB has been known to run on systems with significantly lower specifications.

Embedded Systems
~~~~~~~~~~~~~~~~

Many embedded platforms, typically using ARM processors, offer a low cost Linux environment. Due to various issues around bus speeds, clock stability and CPU capability not all such platforms will work without audio discontinuities.

Platforms that are known to work without flaws are:

- Raspberry Pi with Wolfson Audio board
- Beagleboard xM with USB audio codec

Other platforms that are known not to work:

- Beagleboard Black with USB audio codec (clicks)
- Raspberry Pi with USB audio codec (clicks, massive discontinuities above 32kHz sample rate)
- Olimex A10 Linux boards (clicks)

Network
~~~~~~~

OpenOB offers a variety of modes that allow for low-bitrate operation for speech only links, and medium to high bitrate full bandwidth links for musical content. The :ref:`bitrates_and_bandwidths` section of the manual discusses these recommendations in detail.

In general your network must support more than 50 kilobytes per second with near-zero loss. You will need to be able to open ports on the firewall at the receiver side; transmitters can emit audio from behind NAT gateways and most permissive egress firewalls.

If a tunnel is required eg to bypass a firewall, a VPN operating with a UDP wrapper is recommended. TCP tunneling such as SSH will result in seriously degraded links, and is not recommended.

.. NOTE::
  When determining your network requirements, budget for at least 10kbps overhead to allow OpenOB itself room to communicate configuration data.

  Also bear in mind that framing sizes and error correction will add overhead to the basic audio bitstream itself.