Tutorial
========

Getting started with OpenOB is easy - this guide will take you through setting up OpenOB on two computers next to each other on a simple network.

We assume for the purposes of this guide that the two computers are standard x86/64 desktops, and not SBCs like the Raspberry Pi etc. We also assume these machines are Debian based, though Ubuntu should work with this guide similarly.

OpenOB System Basics
--------------------

OpenOB systems are made up of two or three computers. These are the transmitter, a receiver, and configuration server. Typically the configuration server is also installed on the receiver, but this is not always the case, and many OpenOB links (comprised of a transmitter and receiver pair) can share one configuration server.

Installing Prerequisites
------------------------

OpenOB relies on the GStreamer media framework for the underlying audio transport elements. 

Additionally, OpenOB needs some Python extensions, and on the configuration server, we must also install the Redis server used for configuration management.

On Debian you can install the prerequisites with the following command:

.. code-block:: bash

  sudo apt-get install python3-pip python3-gst-1.0 gstreamer1.0-plugins-base gstreamer1.0-alsa gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly gstreamer1.0-plugins-bad python3-redis

OpenOB 4.x and higher use Gstreamer 1.0 and Python 3. It is recommended that you use Debian Jessie or later when installing machines for use with OpenOB.

On one machine, which for this tutorial we'll assume is also our receiver, we'll install Redis:

.. code-block:: bash

  [user@rx-host] $ sudo apt-get install redis-server

We also need to make sure Redis binds itself to be accessible to remote machines, not just localhost. You can edit ``/etc/redis/redis.conf`` yourself or run the following to instantly make this adjustment

.. code-block:: bash

  [user@rx-host] $ sudo sed -i.bak ‘s/bind 127.*/bind 0.0.0.0/’ /etc/redis/redis.conf && sudo service redis restart

Installing OpenOB
-----------------

Now we can install OpenOB itself. You can install from git for the bleeding edge, but package releases are tested and stable, and easily installed:

.. code-block:: bash

  sudo pip install OpenOB

Networking
----------

Make sure you know the IP addresses of each machine and make sure there is no firewall between them. If there is a firewall in place, refer to :ref:`firewall-configuration` to ensure it is configured properly.

Audio testing
-------------

You should plug in an audio source to the microphone input of your transmitter - I recommend a music player of some sort for testing - and some speakers or headphones to the output of your receiver. Make sure these work before you proceed.

Setting up a link
-----------------

It's time to send some audio. The default settings will be fine to start with, so we'll set up a simple link.

Let's assume your receiver is also your config server, and has the IP address ``192.168.0.10``. The transmitter's address does not matter.

First we'll start the receiver.

.. code-block:: bash

  [user@rx-host] $ openob 192.168.0.10 test-rx-node test-link rx

Note the ``test-link`` name for the link. This is to differentiate between multiple links between hosts. The IP address we're giving is for the configuration host, and the 'rx' string says this is a receiver. The ``test-rx-node`` name is the node name, which identifies this computer.

The receiver will flail around and complain that it can't configure itself. This is expected!

Now let's start the transmitter.

.. code-block:: bash

  [user@tx-host] $ openob 192.168.0.10 test-tx-node test-link tx 192.168.0.10

We're passing this the same arguments as the receiver, but asking it to be a transmitter instead, and providing the target destination IP address (which since we're using the receiver as our configuration server, is the same as the configuration server's address).

This will start up and send some configuration information to the configuration server. When the receiver next checks for configuration (in a second or two) it will start up with the parameters sent by the transmitter, and you should start hearing audio from the receiver's sound card.

To close the link, just :kbd:`Control-c` both ends to send a kill signal.

Further Usage
-------------

OpenOB has many options on the command line. To find out about them, run ``openob -h``, or ``openob your-config-host node-name link-name tx -h`` to find out about tx/rx specific options.