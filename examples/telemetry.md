# OpenOB telemetry

OpenOB ships **optional** OTLP metrics + structured logging so an operator
can answer questions like "the sender sent 1000 packets — did the receiver
get them?" without packet-capturing both ends.

Telemetry is **off by default** and pulls in **no extra dependencies**
unless you opt in.

## Quickstart

1. Install the extra:

       pip install 'openob[telemetry]'

2. Add three lines to your link TOML
   (`~/.openob/links/<link_name>.toml`):

       telemetry_enabled = true
       telemetry_otlp_endpoint = "http://prom.example.com:9090/api/v1/otlp/v1/metrics"
       telemetry_otlp_interval_seconds = 30

3. Restart both ends. Within 30 s metrics named `openob_*` start showing
   up in your collector. The same TOML works on TX and RX, so a fleet
   that already shares config files Just Works.

## What you get

OpenOB pushes ~25 metrics every `telemetry_otlp_interval_seconds`. Every
metric carries `link`, `node`, `role` (tx|rx), and `version` attributes
so a single Prometheus query like

    rate(openob_rtp_packets_received_total{link="stl-main"}[1m])

joins both ends of a link. The full inventory:

| Metric | Type | Source |
|---|---|---|
| `openob.link.up` | gauge | 1 when audio is flowing, 0 on EOS/ERROR/timeout |
| `openob.pipeline.restarts` | counter | supervisor restart events, labelled by `reason` |
| `openob.audio.peak_dbfs` | gauge | per-`channel`, from the `level` element |
| `openob.audio.rms_dbfs` | gauge | per-`channel`, RMS |
| `openob.rtp.packets_sent` | counter (tx) | rtpsession.stats.packets-sent |
| `openob.rtp.packets_received` | counter (rx) | rtpsession.stats.packets-received |
| `openob.rtp.packets_lost` | counter (rx) | rtpsession.stats.packets-lost |
| `openob.rtp.jitter_seconds` | gauge | rtpsession.stats.jitter, RTP-clock-rate-converted |
| `openob.rtp.round_trip_seconds` | gauge | rtpsession.stats.rb-round-trip |
| `openob.rtx.nack_requests` | counter (tx) | rtprtxsend.num-rtx-requests |
| `openob.rtx.packets_resent` | counter (tx) | rtprtxsend.num-rtx-packets |
| `openob.rtx.nacks_sent` | counter (rx) | rtpsession.stats.sent-nack-count |
| `openob.jitterbuffer.pushed` | counter (rx) | rtpjitterbuffer.stats.num-pushed |
| `openob.jitterbuffer.lost` | counter (rx) | rtpjitterbuffer.stats.num-lost |
| `openob.jitterbuffer.late` | counter (rx) | rtpjitterbuffer.stats.num-late |
| `openob.jitterbuffer.duplicates` | counter (rx) | rtpjitterbuffer.stats.num-duplicates |
| `openob.jitterbuffer.rtx_success` | counter (rx) | rtpjitterbuffer.stats.rtx-success-count |
| `openob.jitterbuffer.rtx_rtt_seconds` | gauge (rx) | rtpjitterbuffer.stats.rtx-rtt |
| `openob.opus.plc_samples` | counter (rx) | opusdec.stats.plc-num-samples |
| `openob.opus.plc_seconds` | counter (rx) | opusdec.stats.plc-duration |
| `openob.tx.path_bytes_sent` | counter (tx) | per-`path`, from udpsink.bytes-served |
| `openob.rx.data_timeout` | counter (rx) | GstUDPSrcTimeout — 3 s of no packets |
| `openob.caps_signal.received` | counter (rx) | per-`result`: ok / auth_fail |
| `openob.srtp.decrypt_failures` | counter (rx) | srtpdec auth failures |
| `openob.build_info` | gauge=1 | once at start, with `gst_version` and `python_version` |

The headline correlation query — *"sender sent 1000, receiver got 900"* —
falls out as

    rate(openob_rtp_packets_sent_total{link="X"}[1m])
      - rate(openob_rtp_packets_received_total{link="X"}[1m])

(OTel `.` becomes `_` on Prometheus ingest, and counter names get a
`_total` suffix.)

## Where to point it

Anything that speaks OTLP HTTP/protobuf works. In rough order of
"smallest to set up":

### Prometheus 2.47+ (native OTLP receiver)

The simplest deployment for someone who already has Prometheus. Enable
the OTLP receiver in `prometheus.yml`:

    otlp:
      keep_identifying_resource_attributes: true

Then point OpenOB at:

    telemetry_otlp_endpoint = "http://prom-host:9090/api/v1/otlp/v1/metrics"

### Grafana Alloy / OpenTelemetry Collector / Mimir

Standard OTLP HTTP receiver on port 4318:

    telemetry_otlp_endpoint = "http://collector:4318/v1/metrics"

### Authenticated endpoint (OTel SaaS)

Bearer tokens go in the headers field via env-var substitution — never
on the CLI, never in the file directly:

    # ~/.openob/links/stl-main.toml
    telemetry_otlp_endpoint = "https://otlp.example.com:4318"
    telemetry_otlp_headers  = "Authorization=Bearer ${OPENOB_OTLP_TOKEN}"

Then export `OPENOB_OTLP_TOKEN=...` from your environment file or
systemd `EnvironmentFile=`.

## Troubleshooting

- **No metrics appearing.** Confirm the line `event=telemetry.configured`
  shows up in OpenOB's startup logs. Absence means either the config
  didn't take (check for the surfaced warning `event=telemetry.disabled`)
  or the OTLP connect failed silently — increase log verbosity with
  `-v` and look for `event=telemetry.error`.

- **Pi Zero (armv6) and `pip install` fails for protobuf.** OpenOB
  Pi-Zero support for `[telemetry]` is best-effort because OTel's
  protobuf wheel coverage on armv6 is patchy. The escape hatch is
  `pip install --extra-index-url https://www.piwheels.org/simple
  'openob[telemetry]'` — piwheels has the source-built protobuf for
  the major Python versions. Pi 3+ has native wheels and is fine.

- **Counters look low at startup.** The first sample after enable
  establishes a baseline; subsequent samples emit deltas. After a
  pipeline restart counters reset to 0 in GStreamer; the telemetry
  layer absorbs the reset and re-baselines, so you'll see counts
  pause for one sample then resume.

## Structured logging

When telemetry is enabled most operators also flip:

    telemetry_log_format = "json"

This switches the log line format to one JSON object per line, with the
metric-emission events tagged via an `event` field that mirrors the
metric names (`pipeline.started`, `rx.data_timeout`, `srtp.decrypt_failure`,
…). Aggregators like Loki, Vector, or an OTel logs receiver pick those
up directly. You can also drive this via env:

    OPENOB_LOG_FORMAT=json openob tx my-link

## Disabling telemetry

Either delete the `telemetry_*` keys from the TOML or set
`telemetry_enabled = false`. The base install of OpenOB never imports
OpenTelemetry, so there's no daemon to stop, no port held open, no
side-effect on the audio path.
