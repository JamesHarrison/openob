# Running OpenOB under systemd

This directory contains an example template unit and per-instance config
fragments that let you run one or many OpenOB links as auto-starting,
auto-restarting system services.

## Why a template

The unit file is `openob@.service` — the `@` makes it a *template*. You
create per-link config files and enable each instance separately, e.g.
`openob@studio-stl.service` and `openob@talkback.service`, both backed by
the same unit. systemd substitutes `%i` in the unit with everything between
`@` and `.service` and reads its CLI args from `/etc/openob/<i>.conf`.

## One-time setup

Create the unprivileged user (the unit refuses to run as root by default
because it sets `User=openob`), give it audio access, and lay down the
config directory:

```sh
sudo useradd --system --shell /usr/sbin/nologin --groups audio openob
sudo mkdir -p /etc/openob
sudo install -m 0644 openob@.service /etc/systemd/system/
```

If your `openob` binary lives somewhere other than `/usr/local/bin/openob`
(common when installing into a venv at e.g. `/opt/openob`), edit the
`ExecStart=` line in the installed unit to the right path before reloading
systemd.

```sh
sudo systemctl daemon-reload
```

## Adding a TX link

```sh
sudo install -m 0644 openob-tx.conf.example /etc/openob/studio-stl.conf
sudo $EDITOR /etc/openob/studio-stl.conf      # adapt redis host, node/link names, audio device, bitrate
sudo systemctl enable --now openob@studio-stl
```

## Adding an RX link

```sh
sudo install -m 0644 openob-rx.conf.example /etc/openob/studio-stl.conf
sudo $EDITOR /etc/openob/studio-stl.conf
sudo systemctl enable --now openob@studio-stl
```

(The `<instance>` part is purely a systemd label — both ends of the same
link can share the name `studio-stl` because they live on different boxes.)

## Watching it run

OpenOB writes its log to stdout, which systemd routes into the journal:

```sh
journalctl -u openob@studio-stl -f
```

Common things to watch for:

- `Connected to Redis at host:port` — Redis is reachable
- `Started <mono|stereo|N-channel> audio transmission (...caps...)` (TX) or
  `Receiving ... audio transmission (...)` (RX) — pipeline is rolling
- `No data received for 3 seconds!` — RX timed out; the supervisor will
  rebuild the pipeline automatically
- `Redis ping failed (attempt N): ...; retrying in N.Ns` — Redis transiently
  unreachable; exponential backoff up to 5 s

## Restart behaviour

`Restart=on-failure` means systemd only restarts on a non-zero exit. A
clean `systemctl stop` exits cleanly via SIGTERM and stays stopped.
`StartLimitBurst=5 / StartLimitIntervalSec=60s` prevents tight crash loops
on persistent config errors — five failures within a minute and systemd
will refuse to keep restarting until you address the cause.

OpenOB *also* has its own internal supervisor that rebuilds the GStreamer
pipeline on transient failures, so you'll typically only see the systemd
layer kick in for genuinely fatal problems (missing plugin, bad CLI args,
sustained Redis outage exhausting the in-process retry loop, etc.).
