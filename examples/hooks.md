# OpenOB hooks

OpenOB can invoke a user-supplied command on link state changes
(`pending` → `up` → `down` → `shutdown`) and, optionally, on a
configurable heartbeat. It's the small lever for hardware integrators
who want to drive a relay, flash an LED, or post a single cell into an
existing dashboard, without standing up an OpenTelemetry collector.

Hooks are **off by default** and add **no dependencies** — just a
script you write and a command path in your link TOML.

## Quickstart

1. Drop a script anywhere — `/usr/local/bin/openob-hook` is a good
   default. Make it executable.

2. Wire it in:

       # ~/.openob/links/stl-main.toml
       hook_command = "/usr/local/bin/openob-hook"
       hook_heartbeat_seconds = 0   # 0 = status events only (default)
       hook_timeout_seconds = 5

   Or via the CLI:

       openob tx stl-main --hook-command /usr/local/bin/openob-hook

3. Restart the link. Your script will be invoked on every status
   transition. If you set `hook_heartbeat_seconds = N` it also fires
   every N seconds with the latest audio levels.

## What your script receives

Each invocation gets:

- **Positional argv** for `case`-style switching from shell:

      $1  event       (status | heartbeat)
      $2  link name
      $3  role        (tx | rx)
      $4  status      (pending | up | down | shutdown)
      $5  prev_status (status events only; "-" for heartbeat)

- **A JSON envelope on stdin** for richer consumers:

      {
        "schema": "openob.hook/1",
        "event": "status",
        "ts": 1746201234.567,
        "link": "stl-main",
        "node": "studio-tx",
        "role": "tx",
        "version": "6.3.0",
        "status": "up",
        "prev_status": "pending",
        "reason": null,
        "uptime_seconds": 0.0,
        "audio": {
          "peak_dbfs": [-12.4, -13.1],
          "rms_dbfs":  [-21.1, -22.3]
        }
      }

`reason` is set on `down` transitions — `eos`, `pipeline_error`,
`udp_timeout`, or the exception class name from the supervisor restart
loop. `uptime_seconds` is null while the link is `pending` / `down`,
zero on the `up` transition itself, then climbs.

## Status state machine

    pending  --(level message arrives)-->  up
       ^                                    |
       |                                    | (EOS / pipeline error /
       |                                    |  3s no-data on RX)
       |                                    v
       |                                  down
       |                                    |
       |                                    | (supervisor restarts pipeline)
       +----------------------------(start)-+

`shutdown` fires from Node's `finally`-block on every clean process
exit, so a hook that turned a relay on at `up` can release it
deterministically — even if the last transition was `down`.

## Examples

### Bash: blink an LED via GPIO

    #!/bin/sh
    # /usr/local/bin/openob-hook
    EVENT=$1; STATUS=$4
    GPIO=/sys/class/gpio/gpio23/value
    case "$EVENT" in
      status)
        case "$STATUS" in
          up)        echo 1 > "$GPIO" ;;
          down|pending|shutdown) echo 0 > "$GPIO" ;;
        esac
        ;;
    esac

### Bash: post the JSON envelope to a webhook

    #!/bin/sh
    # /usr/local/bin/openob-hook
    exec curl -fsS --max-time 3 \
      -H 'Content-Type: application/json' \
      --data-binary @- \
      https://dashboards.example.com/openob/event

OpenOB's hook subsystem feeds the JSON on stdin, so `--data-binary @-`
forwards it verbatim.

### Python: log audio peaks to a circular buffer

    #!/usr/bin/env python3
    import json, sys, sqlite3
    payload = json.load(sys.stdin)
    if payload['event'] != 'heartbeat': sys.exit(0)
    db = sqlite3.connect('/var/lib/openob/levels.db')
    db.execute(
        'INSERT INTO levels(ts, link, peak_l, peak_r) VALUES(?,?,?,?)',
        (payload['ts'], payload['link'],
         (payload['audio']['peak_dbfs'] or [None])[0],
         (payload['audio']['peak_dbfs'] or [None, None])[1] if len(payload['audio']['peak_dbfs']) > 1 else None),
    )
    db.commit()

## Reliability notes

OpenOB is outside-broadcast software — its primary job is to keep audio
flowing, and the hook subsystem is built around that constraint:

- **Hooks never block the audio path.** Every invocation runs on a
  dedicated worker thread; the GLib main loop only enqueues events.
- **Hooks never crash OpenOB.** A non-zero exit, a missing binary, or
  a timeout is logged at WARNING and the link continues. The next
  event still fires.
- **Wedged scripts get killed.** `hook_timeout_seconds` (default 5)
  bounds how long any one invocation may run.
- **Slow scripts drop heartbeats, not status events.** The internal
  queue is bounded; on overflow heartbeats are dropped (you'll see
  `event=hook.dropped` in the log) but status events block briefly to
  ensure they make it through.

