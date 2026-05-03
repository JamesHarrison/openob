# PCM stereo over IP multicast

Bit-transparent audio distribution to many receivers on the same
LAN — monitoring positions, recording rigs, downstream encoders,
backup confidence checks. One TX, N RXes, flat bandwidth regardless
of how many join.

## Network assumptions

- TX and all RXes are on the same wired LAN (or a multicast-routed
  segment with PIM/IGMP snooping working). Wi-Fi is *technically*
  possible but multicast over Wi-Fi is delivered at the slowest
  associated client's rate and in many cases falls back to broadcast.
- Switch fabric handles IGMP snooping properly. Without it, the switch
  floods the multicast packets to every port, which mostly works but
  is bad citizenship on a shared LAN.
- Sub-millisecond RTT, single-digit-ms jitter.

## Why these settings

- **PCM, not Opus.** "In-house" implies you don't want any codec
  artefacts in the chain. PCM 48k/2 is ~1.5 Mbps on the wire —
  trivial for any modern LAN.
- **`jitter_buffer = 20`.** Tight enough to keep total latency low
  (network delay + 20 ms is what your monitoring positions hear).
  LAN jitter is well below 5 ms; 20 ms is comfortable headroom.
- **No SRTP.** Multicast + SRTP technically works (every joiner needs
  the same PSK), but the operational overhead rarely earns its keep
  on a trusted LAN.
- **No RTX, no multipath.** Both are mutually exclusive with
  multicast — NACKs assume a single TX/RX pair, and multipath assumes
  unicast egress points. OpenOB rejects either combination at config
  load with a clear error.

## Adding receivers

Drop `rx.toml` on each new RX, run `openob rx rx` (the second `rx` is the name of the .toml file). The
kernel issues an IGMP join when the udpsrc starts, the switch starts
forwarding the multicast onto that port, and the RX picks up the next
caps signal (within ~2 s) and starts decoding.

There is no "TX-side registration" — TX doesn't know how many RXes
are listening, and doesn't care.

## When to NOT use multicast

- Across the public internet. Multicast doesn't route over the open
  internet. Use unicast — see the other examples.
- One-to-one links. Use unicast; multicast on a 1:1 link adds IGMP
  overhead with no benefit.
- When you need encryption. Use unicast SRTP per RX, or layer a VPN
  underneath the multicast group.
