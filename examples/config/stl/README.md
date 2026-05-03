# Studio-transmitter link

The classic OpenOB use case: a permanent point-to-point link between
two studios, or between a transmitter site and a studio, over a
fixed-line WAN.

## Network assumptions

- Both ends have stable (static or DHCP-pinned) IPs.
- Fixed-line carriage with single-digit-ms jitter and <0.1 % steady
  loss. Fibre, leased line, MPLS, business cable.
- A firewall in front of the RX has UDP/3000 forwarded inbound. (TX
  doesn't need any inbound rules — RTCP NACKs ride the same UDP socket
  the audio left on, so TX's NAT mapping carries them home.)

## Why these settings

- **Opus 192 kbps stereo.** Transparent for music; speech-only links
  are fine on 96–128. Bump to 256 if the carriage has the headroom and
  the source is a music network.
- **40 ms jitter buffer.** Default. Fixed-line carriage rarely needs
  more; if you see RX underruns, raise to 60–80 ms.
- **`retransmission_time_limit = 200`.** RTX is cheap and means the
  rare lost packet doesn't audibly glitch. RX picks it up from the
  in-band caps automatically — no RX-side config needed.
- **SRTP via per-link PSK.** Studio-to-studio links typically cross
  shared infrastructure; encrypting is good hygiene even when the path
  feels private.

## PSK handling

The TOML uses envvar substitution (`psk = "${OPENOB_PSK_STL}"`). Set
the secret in the systemd EnvironmentFile or via a vault — it doesn't
sit in the link TOML, doesn't appear in `ps`, and doesn't show up in
shell history. See `examples/systemd/README.md` for the systemd-creds
variant if you want the secret encrypted at rest.

If you'd rather hardcode the PSK in the file (single-machine deploys,
test rigs), replace the `${...}` value with the secret string. If you
manage many links from one secret, drop `psk` entirely and put
`root_psk = "fleet-secret"` in `~/.openob/secrets.toml` under
`[defaults]` — each link's actual key is HKDF-derived from
`(root_psk, link_name)`.

## Latency budget

Roughly 25 ms codec + 40 ms jitter buffer + network RTT/2 = on the
order of 70–100 ms one-way for a typical 25 ms intra-country link.
Fine for talkback, off-air monitoring, and most STL applications.
