Introduction
============

OpenOB is an audio over IP (AoIP) transciever tool that can manage establishment and recovery of AoIP links in a low overhead manner.

It supports lossless linear PCM audio as well as the Opus audio codec for compressed audio operation. It is designed to operate as a standalone tool, but is also straightforward to use in your own projects, such as for building AoIP hardware.

Architecture
------------

OpenOB is a peer to peer audio streaming system with a central configuration server.

The program itself is a set of Python classes wrapping the PyGST bindings for the GStreamer media framework, which itself performs the audio encoding/decoding and transmission.

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
- Debian Wheezy (7.0)

OpenOB has been known to run on systems with significantly lower specifications.


Embedded Systems
~~~~~~~~~~~~~~~~

OpenOB can be run on a Raspberry Pi, but due to a bug in the Pi USB drivers it can only run at 32kHz sample rate when using a USB audio codec, making it unsuitable for full-bandwidth operations on a Pi.

The BeagleBoard xM can run OpenOB in full bandwidth mode with a USB audio codec.

Other embedded systems that may handle OpenOB but have not been tested include:

* BeagleBoard Black
* Olimex Linux boards

Network
~~~~~~~

OpenOB offers a variety of modes that allow for low-bitrate operation for speech only links, and medium to high bitrate full bandwidth links for musical content. The :ref:`bitrates_and_bandwidths` section of the manual discusses these recommendations in detail.

In general your network must support more than 24 kilobytes per second with near-zero loss. You will need to be able to open ports on the firewall at the receiver side; transmitters can emit audio from behind NAT gateways and most permissive egress firewalls.

.. NOTE::
  When determining your network requirements, budget for at least 6kbps overhead to allow OpenOB itself room to communicate configuration data.

  Also bear in mind that framing sizes and error correction will add overhead to the basic audio bitstream itself.