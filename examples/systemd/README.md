# Running OpenOB v6 under systemd

This directory contains an example template unit and per-instance config
fragments that let you run one or many OpenOB links as auto-starting,
auto-restarting system services. **No Redis required** — v6 uses per-link
TOML files under `/etc/openob/links/` instead.

## Why a template

The unit file is `openob@.service` — the `@` makes it a *template*. You
create per-link config files and enable each instance separately, e.g.
`openob@studio-stl.service` and `openob@talkback.service`, both backed by
the same unit. systemd substitutes `%i` in the unit with everything between
`@` and `.service` and reads its CLI args from `/etc/openob/<i>.conf`.

## One-time setup

Create the unprivileged user (the unit refuses to run as root by default
because it sets `User=openob`), give it audio access, and lay down the
config directories:

```sh
sudo useradd --system --shell /usr/sbin/nologin --groups audio openob
sudo mkdir -p /etc/openob/links
sudo install -m 0644 openob@.service /etc/systemd/system/
```

If your `openob` binary lives somewhere other than `/usr/local/bin/openob`
(common when installing into a venv at e.g. `/opt/openob`), edit the
`ExecStart=` line in the installed unit to the right path before reloading
systemd.

```sh
sudo systemctl daemon-reload
```

## Adding a link

Each link needs **two** files:

1. **Link config** at `/etc/openob/links/<link>.toml` — the same content on
   both ends (TX and RX). All codec, security, and PSK settings live here.
2. **systemd instance config** at `/etc/openob/<instance>.conf` — just tells
   systemd which subcommand (`tx` or `rx`) and which link name to invoke.

```sh
# 1. Lay down the link config (same on both ends):
sudo install -m 0644 openob-link.toml.example /etc/openob/links/studio-stl.toml
sudo $EDITOR /etc/openob/links/studio-stl.toml      # adapt receiver_host, codec, security/PSK

# 2. systemd instance — TX side:
sudo install -m 0644 openob-tx.conf.example /etc/openob/studio-stl.conf
sudo systemctl enable --now openob@studio-stl

# 2. systemd instance — RX side (same instance name fine, different host):
sudo install -m 0644 openob-rx.conf.example /etc/openob/studio-stl.conf
sudo systemctl enable --now openob@studio-stl
```

## Storing PSKs out of the link file

For ops who want SRTP secrets out of the file system, the link TOML
supports envvar substitution:

```toml
# /etc/openob/links/studio-stl.toml
security = "psk"
psk      = "${OPENOB_PSK_STUDIO_STL}"
```

Then in the systemd instance config:

```bash
# /etc/openob/studio-stl.conf
OPENOB_ARGS="tx studio-stl"
OPENOB_PSK_STUDIO_STL=correct-horse-battery-staple
```

If you want the env file off-disk, swap `EnvironmentFile=` for systemd-creds:

```sh
sudo systemd-creds encrypt --name=openob_psk_studio_stl - /etc/openob/creds/studio-stl
# then in the unit:
LoadCredentialEncrypted=openob_psk_studio_stl:/etc/openob/creds/studio-stl
ExecStart=/bin/sh -c 'OPENOB_PSK_STUDIO_STL=$(cat $CREDENTIALS_DIRECTORY/openob_psk_studio_stl) /usr/local/bin/openob $OPENOB_ARGS'
```

## Watching it run

OpenOB writes its log to stdout, which systemd routes into the journal:

```sh
journalctl -u openob@studio-stl -f
```

Common things to watch for:

- `OpenOB 6.0.0 starting: node=… link=… mode=…` — args parsed cleanly
- `Waiting for in-band caps signal on UDP <port>` (RX) — RX is up; waiting for TX
- `Started <mono|stereo|N-channel> audio transmission (...caps...)` (TX) or
  `Receiving ... audio transmission (...)` (RX) — pipeline is rolling
- `SRTP enabled (cipher=aes-128-icm/aes-128-icm auth=hmac-sha1-80/hmac-sha1-80, …)` — PSK mode active
- `Caps-signal sender active: <host>:<port> every 2.0s (RTCP-mux on RTP port)` (TX)
- `Got caps from <peer> after N packet(s)` (RX) — caps signal arrived
- `No data received for 3 seconds!` — RX timed out; the supervisor will
  rebuild the pipeline automatically

## Restart behaviour

`Restart=on-failure` means systemd only restarts on a non-zero exit. A
clean `systemctl stop` exits cleanly via SIGTERM and stays stopped.
`StartLimitBurst=5 / StartLimitIntervalSec=60s` prevents tight crash loops
on persistent config errors — five failures within a minute and systemd
will refuse to keep restarting until you address the cause.

OpenOB *also* has its own internal supervisor that rebuilds the GStreamer
pipeline on transient failures, so you'll typically only see the systemd
layer kick in for genuinely fatal problems (missing plugin, bad CLI args,
config validation errors, etc.).

## Migrating from v5

If you have existing v5 deployments backed by Redis, the one-shot helper
prints v6 TOML you can drop straight into `/etc/openob/links/`:

```sh
pip install "openob[migrate]"
openob migrate-v5 redis.example.com:6379 studio-stl flypack-1 talkback \
  > migrated.toml
# split migrated.toml by the `# v6 config migrated from v5 Redis link …` headers
# and place each block at /etc/openob/links/<link>.toml
```

The systemd unit, env file format, and instance naming all work the same
way as before — only the args inside `OPENOB_ARGS` change.
