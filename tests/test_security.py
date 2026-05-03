"""Unit tests for the SRTP master-key derivation in openob.security."""
from __future__ import annotations

import pytest

from cryptography.exceptions import InvalidTag

from openob.security import (
    CAPS_ENVELOPE_OVERHEAD,
    CAPS_KEY_LEN,
    SRTP_KEY_SALT_LEN,
    SecurityConfigError,
    decrypt_caps_payload,
    derive_caps_key,
    derive_link_psk_from_root,
    derive_srtp_key,
    encrypt_caps_payload,
    resolve_caps_key,
    resolve_srtp_master,
)


def test_derive_srtp_key_length():
    key = derive_srtp_key('shared-secret', 'mylink')
    assert len(key) == SRTP_KEY_SALT_LEN  # 30 bytes (16 key + 14 salt)


def test_derive_srtp_key_deterministic():
    a = derive_srtp_key('shared-secret', 'mylink')
    b = derive_srtp_key('shared-secret', 'mylink')
    assert a == b


def test_derive_srtp_key_differs_per_link():
    a = derive_srtp_key('shared-secret', 'link-a')
    b = derive_srtp_key('shared-secret', 'link-b')
    assert a != b, 'link_name must be domain-separated in the HKDF info'


def test_derive_srtp_key_differs_per_psk():
    a = derive_srtp_key('alice', 'mylink')
    b = derive_srtp_key('bob', 'mylink')
    assert a != b


def test_root_psk_derivation():
    a = derive_link_psk_from_root('root-secret', 'flypack-1')
    b = derive_link_psk_from_root('root-secret', 'flypack-2')
    assert len(a) == 32
    assert a != b
    # Same root + same link → same per-link key
    assert derive_link_psk_from_root('root-secret', 'flypack-1') == a


def test_resolve_master_per_link_psk():
    master = resolve_srtp_master(link_name='mylink', psk='my-psk', root_psk=None)
    assert len(master) == SRTP_KEY_SALT_LEN
    # Should match the direct derivation
    assert master == derive_srtp_key('my-psk', 'mylink')


def test_resolve_master_root_psk_chain():
    master = resolve_srtp_master(
        link_name='flypack-1', psk=None, root_psk='root-secret',
    )
    # Equivalent to: derive per-link PSK, then derive SRTP key from it
    per_link = derive_link_psk_from_root('root-secret', 'flypack-1')
    expected = derive_srtp_key(per_link, 'flypack-1')
    assert master == expected


def test_per_link_psk_takes_precedence():
    """When both psk and root_psk are set, per-link psk wins."""
    master = resolve_srtp_master(
        link_name='mylink', psk='per-link', root_psk='root',
    )
    assert master == derive_srtp_key('per-link', 'mylink')
    assert master != resolve_srtp_master(
        link_name='mylink', psk=None, root_psk='root',
    )


def test_resolve_master_requires_secret():
    with pytest.raises(SecurityConfigError, match='requires either'):
        resolve_srtp_master(link_name='mylink', psk=None, root_psk=None)


def test_resolve_master_accepts_bytes_psk():
    """resolve_srtp_master should treat a bytes psk as already-encoded."""
    a = derive_srtp_key('hello', 'mylink')
    b = derive_srtp_key(b'hello', 'mylink')
    assert a == b


# --- caps signal AEAD ----------------------------------------------------


def test_derive_caps_key_length_and_determinism():
    a = derive_caps_key('shared-secret', 'mylink')
    assert len(a) == CAPS_KEY_LEN
    assert derive_caps_key('shared-secret', 'mylink') == a


def test_caps_key_differs_from_srtp_key():
    """Domain separation: caps key and SRTP key derived from the same PSK
    + link_name MUST be different — a leak of one mustn't yield the other."""
    psk = 'same-secret'
    link = 'mylink'
    assert derive_caps_key(psk, link)[:30] != derive_srtp_key(psk, link)


def test_caps_key_differs_per_link():
    a = derive_caps_key('shared', 'link-a')
    b = derive_caps_key('shared', 'link-b')
    assert a != b


def test_resolve_caps_key_per_link_psk():
    key = resolve_caps_key(link_name='mylink', psk='abc', root_psk=None)
    assert key == derive_caps_key('abc', 'mylink')


def test_resolve_caps_key_root_psk():
    """root_psk path: caps key is derived from the per-link PSK that was
    itself derived from the root."""
    key = resolve_caps_key(
        link_name='flypack-1', psk=None, root_psk='root-secret',
    )
    per_link_psk = derive_link_psk_from_root('root-secret', 'flypack-1')
    assert key == derive_caps_key(per_link_psk, 'flypack-1')


def test_resolve_caps_key_requires_secret():
    with pytest.raises(SecurityConfigError, match='requires either'):
        resolve_caps_key(link_name='mylink', psk=None, root_psk=None)


def test_caps_encrypt_decrypt_round_trip():
    key = derive_caps_key('the-psk', 'mylink')
    plaintext = b'application/x-srtp,media=(string)audio,encoding-name=(string)OPUS'
    blob = encrypt_caps_payload(key, plaintext)
    # Envelope adds nonce + tag overhead.
    assert len(blob) == len(plaintext) + CAPS_ENVELOPE_OVERHEAD
    assert decrypt_caps_payload(key, blob) == plaintext


def test_caps_encrypt_uses_fresh_nonce_each_call():
    """Two encryptions of the same plaintext must produce different
    ciphertexts (random nonce). Otherwise an attacker could confirm
    repeated caps strings even without the key."""
    key = derive_caps_key('the-psk', 'mylink')
    pt = b'caps-data'
    a = encrypt_caps_payload(key, pt)
    b = encrypt_caps_payload(key, pt)
    assert a != b
    # But both decrypt to the same plaintext.
    assert decrypt_caps_payload(key, a) == pt
    assert decrypt_caps_payload(key, b) == pt


def test_caps_decrypt_wrong_key_fails():
    key_a = derive_caps_key('alice', 'mylink')
    key_b = derive_caps_key('bob', 'mylink')
    blob = encrypt_caps_payload(key_a, b'secret-caps')
    with pytest.raises(InvalidTag):
        decrypt_caps_payload(key_b, blob)


def test_caps_decrypt_tampered_ciphertext_fails():
    key = derive_caps_key('psk', 'mylink')
    blob = encrypt_caps_payload(key, b'authentic')
    # Flip a bit in the ciphertext (after the 12-byte nonce).
    tampered = bytearray(blob)
    tampered[20] ^= 0x01
    with pytest.raises(InvalidTag):
        decrypt_caps_payload(key, bytes(tampered))


def test_caps_decrypt_too_short_blob_raises():
    key = derive_caps_key('psk', 'mylink')
    with pytest.raises(ValueError, match='too short'):
        decrypt_caps_payload(key, b'\x00' * 10)
