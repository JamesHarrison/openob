# Remote contributor on a single 4G/5G modem

A remote-broadcast Pi or laptop with one mobile-network modem,
contributing into a studio in the cloud or behind a fixed-line link.

## Network assumptions

- TX is on a public mobile network (CGNAT, dynamic IP, no inbound
  reachability).
- RX is on a fixed-line link with UDP/3000 reachable from the
  internet — the TX initiates the audio stream and TX's NAT mapping
  carries RTCP NACKs back to the same socket.
- 30–150 ms RTT typical; loss bursts of 50–500 ms during cell handovers
  or signal dips.

## Why these settings

The four-layer recovery stack:

1. **Opus inband FEC** (`opus_fec = true`,
   `opus_loss_expectation = 10`) — TX bakes a low-bitrate copy of each
   frame into the next packet. Recovers single-packet loss with
   ~5–10 % bitrate overhead, with no round-trip required.
2. **Jitter buffer** (300 ms) — absorbs reordering and brief stalls
   without requesting anything.
3. **RTX** (`retransmission_time_limit = 300`) — when the jitter buffer
   sees a gap, it asks TX to resend via an RTCP NACK. TX has up to
   300 ms of recent packets buffered; the resend usually arrives in
   time to fill the gap.
4. **Continuous reconnect** — the supervisor in `Node.run_link` rebuilds
   the GStreamer pipeline if it stalls (e.g., RX times out after 3 s
   of silence, or TX's modem rebinds to a new IP).

The jitter buffer (RX) must be **at least as large** as
`retransmission_time_limit` (TX). Smaller and the NACK replay arrives
after the slot has already played out — wasted bandwidth.

## Bitrate trade-off

96 kbps stereo Opus is the sweet spot for mobile contribution: full
bandwidth, reasonable music quality, and ~120 kbps on the wire after
RTP/UDP/IP overhead — well under the working uplink budget of every
LTE plan and most rural 5G.

For speech-only contribution drop to 64 kbps mono. For high-quality
remote pickup over a strong 5G link, push to 128 or 192 kbps stereo.

## What this *can't* save

A 5-second outage. A modem that loses signal entirely. An ISP routing
incident. RTX needs the path to come back within
`retransmission_time_limit` to recover; longer outages cause audible
silence. If your contribution is too important to risk this, see
[`../remote-multipath-4g/`](../remote-multipath-4g/) — two modems in
parallel turn most of those outages invisible.
