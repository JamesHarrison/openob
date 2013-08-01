Zookeeper API
=============

OpenOB 3 uses a new inter-link communications system based on the Apache Zookeeper project. Zookeeper is an off the shelf distributed system coordination server which supports advanced features and high availability clustering.

Where previously links were constrained to being simple A-B connections and a set of assumptions allowed for simple use of a Redis server to manage coordination, the demand for more complex link configurations has spurred a change to this more flexible system. However, it requires more forethought!

Zookeeper's main interface is a hierarchal namespace containing *znodes*. This is similar to a file system but with sequential consistency, atomicity, reliability and timeliness. In clusters, clients are guaranteed to see the same view of the system regardless of which server they use.

Concepts
--------

We have *nodes*. These are computers running an OpenOB node daemon. They may be transmitters, receivers, or both, and are centrally configured.

These nodes have *interfaces*. These are audio interfaces, defined in a local config file, which can be used for input or output.

Each interface can participate in a *link*. The participation may be as a transmitter or as a receiver.

Link Types
~~~~~~~~~~

To support our use cases, we have to abstract the following link types.

* One-to-one: Interface A to interface B. Single unicast RTP stream.
* One-to-one (multistream): Interface A to interface B. Multiple unicast RTP streams on differing ports and/or addresses.
* One-to-many: Interface A to interface B, C, D. Multiple unicast RTP streams.
* Multicast: Interface A to interfaces B, C, D. Single multicast SSM RTP stream.

In all of these link types the transmitter behaves differently at the UDP/RTP sinking level. The receivers must also configure themselves differently, again at the UDP/RTP level. All other parts of the TX/RX pipeline are unaffected by these.

Metadata
~~~~~~~~

Some link detail should be transmitted by its location in the Zookeeper node hierarchy. To express more complex configuration data such as stream capabilities or requested bitrate/jitter buffer, data can be added to the nodes themselves.

This data shall be in JSON format and be comprised of a hash containing any information.

znode Hierarchy
---------------

Zookeeper

Each node will register an ephemeral znode::

  /node/:node_name

Where :node_name will be the system hostname or FQDN, or a value set in the OpenOB node configuration file. This ephemeral znode can be used by clients to identify which nodes are up, as the node will be removed on network connectivity loss.

For each interface that is an input (ie, an input from a USB microphone), the node will register a znode::

  /tx/:node_name/:interface_name

Where :interface_name is the name defined in the node configuration file. The name should be made conformant to the znode naming requirements.

For interfaces that are outputs (ie, connected to a transmitter or speakers), the node will register a znode::

  /rx/:node_name/:interface_name

These interface nodes should contain any relevant metadata about the interface, including any name or descriptive information specified in the configuration file. The node is responsible for cleaning up any old interfaces defined under ``/(rx|tx)/:node_name``.

The znodes under rx/tx are persistent - that is, a node that crashes or shuts down and comes back up should act on the znodes present as if they had just appeared. There should be no functional difference between a freshly started node and a node that has been configured by znodes changing while it is running.

The children of interface nodes represent a RX or TX portion of a link configuration.

znode Example
~~~~~~~~~~~~~

Assume we have one node, ``studio``, and we'd like to transmit to another node, ``transmitter``. The studio node has an input interface named ``desk-outputs`` and the transmitter node has an output interface named ``processor-input``. This is a one-to-one link.

The configuration client can look at ``/node``'s children to identify which nodes are available, and sees both ``studio`` and ``transmitter``.

For each node the client can now look at ``/tx/:node_name`` and ``/rx/:node_name`` to identify what interfaces are available. It may perform a further check for children on these interface znodes to see which are available. This could be displayed to the user.

Knowing which nodes/interfaces this link requires, and having verified the nodes and interfaces are available, the configuration client creates a new znode, ``/tx/studio/desk-outputs/transmitter/processor-input``, along with the requested parameters as data on that znode (bitrate etc).

The studio node, seeing this new znode, ensures there are no children under ``/rx/transmitter/processor-input``. If there are none it proceeds to start a transmitter. Once the stream is set up and port capabilities are available, a new znode is created by the transmitter at ``/rx/transmitter/processor-input/studio/desk-outputs`` with the parameters required to receive the stream.

The transmitter node sees this new znode and starts a receiver with the given parameters.

If either node crashes or is restarted, it reads its interface configuration file, deletes any znodes under ``/(tx|rx)/:node_name`` that do not appear in the configuration file, and for the remaining znodes it checks for children and starts any transmitters or receivers as required to match its running state with the configuration in Zookeeper.

One-to-one (multistream)
~~~~~~~~~~~~~~~~~~~~~~~~



One-to-many
~~~~~~~~~~~

Multicast
~~~~~~~~~