"""
SRTP key derivation + caps-signal authenticated encryption for OpenOB v6.

PSK provisioning has two flavours:

- **Per-link PSK** (``psk = "..."`` in the link config) — each link has its
  own secret, set explicitly on both sides. Strict isolation; a leaked PSK
  compromises only that link.
- **Root PSK with per-link HKDF** (``root_psk = "..."`` in the secrets
  config) — a single shared secret across a fleet, with each link's PSK
  derived independently as ``HKDF(root_psk, info=link_name)``. Easier to
  provision in N:1 deployments; a leaked root compromises every link.

Per-link PSK takes precedence when both are configured.

From whichever PSK is in effect, two independent keys are derived via
HKDF-SHA256 with domain-separated salts so the SRTP key and caps-signal
key are never the same:

- **SRTP master key+salt** (30 bytes; AES-128-ICM key + 14-byte salt)
  → fed to GStreamer ``srtpenc`` / ``srtpdec`` ``key`` property.
- **Caps-signal key** (32 bytes; AES-256-GCM)
  → encrypts and authenticates the OPOB CAPS APP packet payload.

The caps-signal key closes the v6.0 gap where the in-band caps signal was
sent in cleartext alongside SRTP audio (RFC 5761 §7 says muxed RTCP must
be encrypted at the SRTP layer). We don't route the caps packet through
``srtpenc`` (PyGObject's RTCPPacket bindings make that impractical), but
encrypting the APP payload separately gives the same authenticity and
confidentiality properties for the caps content.

This is not the SRTP KDF from RFC 3711 — that lives inside libsrtp and
derives the per-stream session keys from the master we hand it. Our HKDF
is just the PSK → keys derivation, which RFC 3711 leaves to the keying
mechanism (we'd be using MIKEY-PSK if GStreamer supported it; it doesn't,
so we do this directly).
"""
from __future__ import annotations

import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


# AES-128-ICM master key length (RFC 3711 §3.2.3, libsrtp default cipher).
SRTP_MASTER_KEY_LEN = 16
# SRTP master salt is fixed at 112 bits = 14 bytes (RFC 3711 §4.1.2).
SRTP_MASTER_SALT_LEN = 14
SRTP_KEY_SALT_LEN = SRTP_MASTER_KEY_LEN + SRTP_MASTER_SALT_LEN  # 30 bytes

# GStreamer srtpenc property values for the AES-128-ICM + HMAC-SHA1-80
# default profile. Suitable for the 64–256 kbps Opus traffic OpenOB carries
# and well-supported on ARM (no GCM hardware offload needed).
SRTP_RTP_CIPHER = 'aes-128-icm'
SRTP_RTCP_CIPHER = 'aes-128-icm'
SRTP_RTP_AUTH = 'hmac-sha1-80'
SRTP_RTCP_AUTH = 'hmac-sha1-80'

# AES-256-GCM for the caps-signal AEAD.
CAPS_KEY_LEN = 32
CAPS_NONCE_LEN = 12
CAPS_TAG_LEN = 16
CAPS_ENVELOPE_OVERHEAD = CAPS_NONCE_LEN + CAPS_TAG_LEN  # 28 bytes per packet

# Domain-separation labels for HKDF. Bumping the version invalidates all
# previously-derived keys, so only change in a deliberate format break.
_HKDF_SALT_ROOT = b'openob-root-v1'
_HKDF_SALT_SRTP = b'openob-srtp-v1'
_HKDF_SALT_CAPS = b'openob-caps-v1'


class SecurityConfigError(ValueError):
    """Raised when security config is internally inconsistent
    (e.g. ``security="psk"`` but neither ``psk`` nor ``root_psk`` set)."""


def derive_link_psk_from_root(root_psk: str | bytes, link_name: str) -> bytes:
    """Derive a per-link PSK from a root PSK + the link name.

    Used when ops provisions a single root secret across a fleet and wants
    independent per-link keys without per-link configuration.
    """
    ikm = root_psk.encode('utf-8') if isinstance(root_psk, str) else root_psk
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT_ROOT,
        info=link_name.encode('utf-8'),
    ).derive(ikm)


def derive_srtp_key(psk: str | bytes, link_name: str) -> bytes:
    """Derive the 30-byte SRTP master key+salt from a PSK + link name.

    Both sides must call this with the same ``psk`` and ``link_name`` to
    arrive at the same master key.
    """
    ikm = psk.encode('utf-8') if isinstance(psk, str) else psk
    return HKDF(
        algorithm=hashes.SHA256(),
        length=SRTP_KEY_SALT_LEN,
        salt=_HKDF_SALT_SRTP,
        info=link_name.encode('utf-8'),
    ).derive(ikm)


def resolve_srtp_master(
    *,
    link_name: str,
    psk: str | None,
    root_psk: str | None,
) -> bytes:
    """Return the 30-byte SRTP master key+salt for a link, given its config.

    Per-link ``psk`` takes precedence over ``root_psk``. Raises
    :class:`SecurityConfigError` if neither is configured.
    """
    return derive_srtp_key(_resolve_link_psk(link_name, psk, root_psk), link_name)


def _resolve_link_psk(
    link_name: str,
    psk: str | None,
    root_psk: str | None,
) -> bytes:
    """Pick the effective per-link PSK bytes. Raises if neither is set."""
    if psk:
        return psk.encode('utf-8') if isinstance(psk, str) else psk
    if root_psk:
        return derive_link_psk_from_root(root_psk, link_name)
    raise SecurityConfigError(
        'security=psk requires either a per-link "psk" or a "root_psk" '
        '(in ~/.openob/secrets.toml [defaults])'
    )


# --------------------------------------------------------------------- caps


def derive_caps_key(psk: str | bytes, link_name: str) -> bytes:
    """Derive the 32-byte AES-256-GCM key used to AEAD-protect the
    in-band caps signal. Domain-separated from the SRTP key so a leak of
    one cannot be used to derive the other.
    """
    ikm = psk.encode('utf-8') if isinstance(psk, str) else psk
    return HKDF(
        algorithm=hashes.SHA256(),
        length=CAPS_KEY_LEN,
        salt=_HKDF_SALT_CAPS,
        info=link_name.encode('utf-8'),
    ).derive(ikm)


def resolve_caps_key(
    *,
    link_name: str,
    psk: str | None,
    root_psk: str | None,
) -> bytes:
    """Return the AES-256-GCM caps key for a link. Same precedence as
    :func:`resolve_srtp_master`: per-link ``psk`` over ``root_psk``."""
    return derive_caps_key(_resolve_link_psk(link_name, psk, root_psk), link_name)


def encrypt_caps_payload(caps_key: bytes, plaintext: bytes) -> bytes:
    """AEAD-encrypt ``plaintext`` for the OPOB CAPS APP packet payload.

    Wire format: ``nonce (12 bytes) || ciphertext || tag (16 bytes)``.
    Random nonce per packet (cryptographically safe well past any
    realistic packet count for a single link key).
    """
    nonce = os.urandom(CAPS_NONCE_LEN)
    aesgcm = AESGCM(caps_key)
    ct_and_tag = aesgcm.encrypt(nonce, plaintext, associated_data=None)
    return nonce + ct_and_tag


def decrypt_caps_payload(caps_key: bytes, blob: bytes) -> bytes:
    """Decrypt + verify a caps payload. Raises ``cryptography.exceptions
    .InvalidTag`` on auth failure (caller should drop the packet)."""
    if len(blob) < CAPS_NONCE_LEN + CAPS_TAG_LEN:
        raise ValueError(
            f'caps blob too short: {len(blob)} bytes, need at least '
            f'{CAPS_NONCE_LEN + CAPS_TAG_LEN}'
        )
    nonce = blob[:CAPS_NONCE_LEN]
    ct_and_tag = blob[CAPS_NONCE_LEN:]
    aesgcm = AESGCM(caps_key)
    return aesgcm.decrypt(nonce, ct_and_tag, associated_data=None)
