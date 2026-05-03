"""Standardised event names used at metric-emission log sites.

Interning these as module constants keeps the wire-form stable: a JSON log
aggregator can group on ``event=pipeline.restart`` without grepping the
human-readable message text, and a future rename surfaces as a single edit
here rather than scattered string literals.
"""
from __future__ import annotations


# --- pipeline lifecycle ---
EVT_PIPELINE_STARTED = 'pipeline.started'
EVT_PIPELINE_RESTART = 'pipeline.restart'
EVT_LINK_UP = 'pipeline.link_up'
EVT_LINK_DOWN = 'pipeline.link_down'

# --- audio path ---
EVT_LEVEL = 'audio.level'

# --- network / RTP ---
EVT_RTX_STATS = 'rtx.stats'
EVT_PATH_STATS = 'tx.path.stats'
EVT_DATA_TIMEOUT = 'rx.data_timeout'
EVT_RTP_SESSION_STATS = 'rtp.session.stats'

# --- security ---
EVT_CAPS_SIGNAL_RX = 'caps_signal.received'
EVT_SRTP_DECRYPT_FAIL = 'srtp.decrypt_failure'

# --- telemetry self-describing ---
EVT_TELEMETRY_CONFIGURED = 'telemetry.configured'
EVT_TELEMETRY_DISABLED = 'telemetry.disabled'
EVT_TELEMETRY_ERROR = 'telemetry.error'
