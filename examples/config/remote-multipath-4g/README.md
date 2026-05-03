# Remote contributor with two 4G/5G modems (seamless protection)

Two mobile-network modems in the remote kit, both contributing the
same audio to the studio. The RX picks whichever copy arrives first
and discards the duplicate — when one modem briefly stalls during a
cell handover, the audio keeps flowing on the other path with **zero
audible glitch**. This is the technique pro broadcast knows as
SMPTE ST 2022-7 seamless protection.

## Network assumptions

- TX has two named network interfaces — typically `wwan0` and `wwan1`,
  or `usb0` and `usb1` — each backed by its own modem on a different
  carrier (or the same carrier but a different SIM, for a partial
  win against carrier-wide failures).
- RX is on a single fixed-line ingress at `studio.example.com:3000`.
  No RX-side reconfiguration needed for multipath; the existing
  jitterbuffer dedupes naturally.
- 2× the egress bandwidth of the single-path case. Both paths carry
  the full stream; that's the cost of zero-glitch failover.

## OS-level setup the TOML can't do for you

Pinning a UDP socket to interface `wwan0` (via `IP_UNICAST_IF`) tells
the kernel which interface to *use*, but the kernel still picks the
next-hop gateway from the routing table. With two modems each
declaring a default route, you need **policy routing** so each
interface's table is consulted when packets are sourced from that
interface. Roughly:

```sh
# One routing table per modem, with that modem's default route in it:
echo "100 wwan0_table" | sudo tee -a /etc/iproute2/rt_tables
echo "101 wwan1_table" | sudo tee -a /etc/iproute2/rt_tables

# Send packets from wwan0's egress through wwan0_table:
sudo ip rule add oif wwan0 table wwan0_table
sudo ip rule add oif wwan1 table wwan1_table

# Each table's default route points at that modem's gateway:
sudo ip route add default via <wwan0-gateway> dev wwan0 table wwan0_table
sudo ip route add default via <wwan1-gateway> dev wwan1 table wwan1_table
```

The exact mechanics depend on your modem manager (NetworkManager,
ModemManager, raw `pppd`/`ip`, etc.). The principle is the same:
each modem gets its own routing table, an `ip rule` matches packets
by output interface, and `IP_UNICAST_IF` (set by OpenOB on the
per-path socket) drives the match.

If routing isn't set up, `iface=` still binds the socket to the
interface but the kernel may end up sending packets the wrong way and
both paths effectively become one. The `tx_path[i]: X.X KB/s` log line
on TX is how you confirm both paths are actually carrying traffic;
**watch this on first deployment**.

## Why iface= over bind=

Mobile networks issue dynamic IPs that change on every reattach.
`iface = "wwan0"` resolves to the current source IP each send, so a
modem rebinding to a new address is transparent. `bind = "10.x.y.z"`
locks the source IP at startup — fine for static-IP testing on
loopback aliases, wrong for production 4G.

`iface=` requires Linux 3.4+ and does **not** require `CAP_NET_RAW` —
OpenOB can run as an unprivileged user.

## What this protects against

- One modem's signal drops to nothing for several seconds
- One modem's cell hands over with a 1-second loss burst
- One carrier's regional incident
- One modem's hardware fails and gets unplugged (kernel drops the
  interface; the OpenOB queue's leaky-downstream policy keeps the tee
  flowing; audio continues on the other path)

## What this *can't* protect against

- Both modems on the same carrier in the same region during a
  carrier-wide outage. Use different carriers if budget allows.
- Both modems' signal failing simultaneously — covered indoors,
  remote site, etc. Multipath isn't magic; if no packets are leaving
  the building, there's nothing to receive.

## Verifying it works

The TX log emits per-path stats every second:

```
tx_path modem_a: 14.2 KB/s (bytes-served=12340567)
tx_path modem_b: 14.1 KB/s (bytes-served=12338290)
```

Both paths should show similar numbers. If one shows 0 KB/s, the
routing setup is wrong (or the modem is genuinely down — check
`ip a show wwan1`). If one shows half the other, you may have one
modem doing all the lifting because routing is collapsing both paths
through a single gateway.

The "did the RX actually get audio" check is the same as any other
link: the `Receiving … audio transmission` log line on RX, plus
silence on the audio output during a manual interface-down test:

```sh
# On TX, simulate modem_b dying for 5 seconds:
sudo ip link set wwan1 down; sleep 5; sudo ip link set wwan1 up
# RX audio should not glitch.
```
