# Example link configs

Drop-in TOML pairs for the four most common OpenOB deployment shapes.
Each subdirectory has a `tx.toml`, an `rx.toml`, and a `README.md`
explaining the network assumptions and trade-offs.

| Scenario | When to use |
|---|---|
| [`stl/`](stl/) | Fixed-line studio-transmitter link (fibre, leased line). High bitrate, low latency, SRTP on. The default starting point. |
| [`remote-4g/`](remote-4g/) | Single remote kit on one 4G/5G modem. Lower bitrate, Opus FEC, RTX enabled, larger jitter buffer to absorb mobile-network bursts. |
| [`remote-multipath-4g/`](remote-multipath-4g/) | Remote kit with **two** 4G modems for seamless protection. Identical RTP fans out both paths; RX dedupes. Survives one modem going dark mid-broadcast. |
| [`pcm-lan-multicast/`](pcm-lan-multicast/) | Bit-transparent LAN distribution to many receivers via IP multicast. PCM, no encryption, no RTX. |

## Using a scenario

Each pair is a complete config — copy the `tx.toml` to the transmitter
and the `rx.toml` to the receiver, both at
`~/.openob/links/<link_name>.toml` (or `/etc/openob/links/...` under
systemd). Adjust the `receiver_host`, audio device, and PSK. Then:

```sh
# RX
openob rx <link_name>

# TX
openob tx <link_name>
```

The link name is just a label — pick something descriptive (`studio-stl`,
`flypack-1`, `talkback-newsroom`). Both ends must use the same name and
the same TOML basename, but their contents are intentionally different
(TX carries the codec config; RX picks codec parameters up from TX's
in-band caps signal at runtime).

## What MUST match between TX and RX

- `port`
- `security` (and `psk` / `root_psk` if `security="psk"`)
- `multicast` flag (and `receiver_host` if multicast)

Everything else (codec, bitrate, audio I/O) is per-side. RX learns the
codec parameters from TX automatically — that's why these examples'
`rx.toml` files don't repeat them.
