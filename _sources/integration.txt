Integrating OpenOB
==================

OpenOB provides a simple API for system integrators who want to use OpenOB in their applications; for instance, as part of a hardware AoIP box that can be given to reporters with a very simple interface on it.


.. WARNING::

  OpenOB is not tested to work in all configurations, and cannot be ensured to function perfectly as an integrated component. You should perform integration testing against your complete stack.


.. WARNING::
  
  The following behaviours are described, but not yet implemented. They will be available in a subsequent release.



Interfacing with a Node
-----------------------

OpenOB links are formed of two nodes, each of which runs one end of a link.

Changes can be made to the link configuration. Some of these changes require a link teardown and start to commit, which will result in lost audio frames. Other changes can be performed without disrupting audio at any point. The following parameters do not require a link restart:

* Encoding bitrate
* Forward error correction packet loss percentage
* Audio frame size

No parameters in a linear PCM link can be adjusted without a restart.

Changes to link configuration should be performed by instantiating a new LinkConfig object for the relevant link on the configuration host. The commit_changes function should be called once new values have been set, with the reset flag if appropriate.

Running Links
-------------

The RTPTransmitter and RTPReceiver classes can be used directly to run links within your program.

Care should be taken around handling blocking operations on the receiver in particular, eg waiting for port caps to be sent from the transmitter.
