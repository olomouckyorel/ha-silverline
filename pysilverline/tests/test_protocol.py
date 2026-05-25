"""Round-trip and edge-case tests for the v3.3 frame codec."""

from __future__ import annotations

import binascii
import json
import struct
from typing import Any

import pytest

from pysilverline import const
from pysilverline.exceptions import IncompleteFrame, InvalidAuth, ProtocolError
from pysilverline.protocol import (
    FrameCodec,
    aes_decrypt,
    aes_encrypt,
)

KEY = "0123456789abcdef"


def test_aes_round_trip() -> None:
    plaintext = b'{"hello":"world"}'
    ct = aes_encrypt(plaintext, KEY.encode())
    assert ct != plaintext
    assert aes_decrypt(ct, KEY.encode()) == plaintext


def test_aes_decrypt_wrong_key() -> None:
    ct = aes_encrypt(b"payload", KEY.encode())
    with pytest.raises(ProtocolError):
        aes_decrypt(ct, b"abcdefghijklmnop")


def test_codec_rejects_short_key() -> None:
    with pytest.raises(ValueError):
        FrameCodec("short")


def test_pkcs7_unpad_rejects_empty_or_misaligned() -> None:
    """The PKCS#7 unpad helper rejects empty and non-aligned buffers
    before pad-byte inspection. The public aes_decrypt path always gives
    it a valid multiple-of-16 because AES requires it; this guard is
    defense in depth for anyone reusing _pkcs7_unpad directly."""
    from pysilverline.protocol import _pkcs7_unpad

    with pytest.raises(ProtocolError):
        _pkcs7_unpad(b"")
    with pytest.raises(ProtocolError):
        _pkcs7_unpad(b"\x00" * 7)


def test_aes_decrypt_rejects_corrupt_pkcs7_padding() -> None:
    """Plaintext whose final-byte padding count doesn't match the actual
    trailing bytes is corrupt — we must not strip a guessed prefix."""
    # Build a 16-byte plaintext where the last byte claims pad_len=3 but
    # the preceding two bytes are not 0x03 — classic corrupt PKCS#7.
    bad_plain = b"X" * 13 + b"\xaa\xbb\x03"  # last 3 should be \x03\x03\x03
    ct = _make_cipher_for_test(KEY.encode()).encryptor().update(bad_plain)
    # Append the AES finalize() byte stream (ECB doesn't need any) and
    # decrypt: _pkcs7_unpad will reject.
    with pytest.raises(ProtocolError):
        aes_decrypt(ct, KEY.encode())


def test_aes_encrypt_rejects_wrong_size_key() -> None:
    """A non-16-byte key at the low-level helper raises ValueError — the
    FrameCodec ctor enforces the same invariant at the high level."""
    with pytest.raises(ValueError):
        aes_encrypt(b"payload", b"too-short")


def _make_cipher_for_test(key: bytes) -> Any:
    """Tiny helper so the corrupt-padding test can build a malformed
    ciphertext without going through aes_encrypt (which adds correct
    padding for us)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    return Cipher(algorithms.AES(key), modes.ECB())


def test_encode_query_no_header() -> None:
    codec = FrameCodec(KEY)
    body = {"gwId": "abc", "devId": "abc"}
    wire = codec.encode(const.CMD_DP_QUERY, body)

    prefix, seq, cmd, size = struct.unpack(">IIII", wire[:16])
    assert prefix == const.FRAME_PREFIX
    assert seq == 1
    assert cmd == const.CMD_DP_QUERY
    assert size == len(wire) - 16

    suffix = struct.unpack(">I", wire[-4:])[0]
    assert suffix == const.FRAME_SUFFIX

    crc_actual = struct.unpack(">I", wire[-8:-4])[0]
    assert crc_actual == binascii.crc32(wire[:-8]) & 0xFFFFFFFF

    inner = wire[16:-8]
    # No 3.3 header on DP_QUERY
    assert not inner.startswith(b"3.3")


def test_encode_control_has_header() -> None:
    codec = FrameCodec(KEY)
    body = {"dps": {"1": True}}
    wire = codec.encode(const.CMD_CONTROL, body)
    inner = wire[16:-8]
    assert inner.startswith(const.PROTOCOL_33_HEADER)


def test_seq_monotonic() -> None:
    codec = FrameCodec(KEY)
    seq1 = struct.unpack(">I", codec.encode(const.CMD_DP_QUERY, {})[4:8])[0]
    seq2 = struct.unpack(">I", codec.encode(const.CMD_DP_QUERY, {})[4:8])[0]
    seq3 = struct.unpack(">I", codec.encode(const.CMD_CONTROL, {})[4:8])[0]
    assert seq1 < seq2 < seq3


def test_round_trip_query() -> None:
    """Build a query, then build a synthetic response and decode it."""

    codec = FrameCodec(KEY)
    codec.encode(const.CMD_DP_QUERY, {"gwId": "x"})  # advance seq

    response_body = {"devId": "x", "dps": {"1": True, "4": "Heat"}}
    plaintext = json.dumps(response_body).encode()
    ciphertext = aes_encrypt(plaintext, KEY.encode())

    payload = struct.pack(">I", 0) + ciphertext  # retcode=0
    size = len(payload) + 8
    header = struct.pack(">IIII", const.FRAME_PREFIX, 42, const.CMD_DP_QUERY, size)
    pre_crc = header + payload
    crc = binascii.crc32(pre_crc) & 0xFFFFFFFF
    wire = pre_crc + struct.pack(">II", crc, const.FRAME_SUFFIX)

    frame, remainder = codec.decode(wire)
    assert frame.seq == 42
    assert frame.cmd == const.CMD_DP_QUERY
    assert remainder == b""
    retcode, body = codec.split_response_payload(frame.cmd, frame.payload)
    assert retcode == 0
    decoded = codec.decrypt_body(body)
    assert decoded == response_body


def test_decode_handles_v33_header_in_response() -> None:
    """Some firmwares prepend the v3.3 header even on DP_QUERY responses."""
    codec = FrameCodec(KEY)
    body = {"dps": {"1": True}}
    plaintext = json.dumps(body).encode()
    ciphertext = aes_encrypt(plaintext, KEY.encode())

    payload = struct.pack(">I", 0) + const.PROTOCOL_33_HEADER + ciphertext
    size = len(payload) + 8
    header = struct.pack(">IIII", const.FRAME_PREFIX, 7, const.CMD_DP_QUERY, size)
    pre_crc = header + payload
    crc = binascii.crc32(pre_crc) & 0xFFFFFFFF
    wire = pre_crc + struct.pack(">II", crc, const.FRAME_SUFFIX)

    frame, _ = codec.decode(wire)
    _, peeled = codec.split_response_payload(frame.cmd, frame.payload)
    assert codec.decrypt_body(peeled) == body


def test_decode_status_push_no_retcode() -> None:
    """Spontaneous CMD_STATUS pushes have no leading retcode."""
    codec = FrameCodec(KEY)
    body = {"dps": {"3": 26}}
    plaintext = json.dumps(body).encode()
    ciphertext = aes_encrypt(plaintext, KEY.encode())

    payload = ciphertext  # no retcode prefix for push
    size = len(payload) + 8
    header = struct.pack(">IIII", const.FRAME_PREFIX, 99, const.CMD_STATUS, size)
    pre_crc = header + payload
    crc = binascii.crc32(pre_crc) & 0xFFFFFFFF
    wire = pre_crc + struct.pack(">II", crc, const.FRAME_SUFFIX)

    frame, _ = codec.decode(wire)
    assert frame.cmd == const.CMD_STATUS
    bare = codec.split_request_payload(frame.payload)
    assert codec.decrypt_body(bare) == body


def test_decode_status_push_with_retcode_and_v33_header() -> None:
    """Real WBR3 firmware pushes carry a 4-byte zero retcode + v3.3 header
    before the ciphertext, despite the protocol notes saying pushes are bare.

    Verified against a live PC-SLP090N: every spontaneous DP-3 (temperature)
    push has this shape, so the codec must peel both prefixes."""
    codec = FrameCodec(KEY)
    body = {"dps": {"3": 28}, "t": 77848}
    plaintext = json.dumps(body, separators=(",", ":")).encode()
    ciphertext = aes_encrypt(plaintext, KEY.encode())

    payload = struct.pack(">I", 0) + const.PROTOCOL_33_HEADER + ciphertext
    size = len(payload) + 8
    header = struct.pack(">IIII", const.FRAME_PREFIX, 0, const.CMD_STATUS, size)
    pre_crc = header + payload
    crc = binascii.crc32(pre_crc) & 0xFFFFFFFF
    wire = pre_crc + struct.pack(">II", crc, const.FRAME_SUFFIX)

    frame, _ = codec.decode(wire)
    bare = codec.split_request_payload(frame.payload)
    assert codec.decrypt_body(bare) == body


def test_decode_short_buffer_is_incomplete_not_malformed() -> None:
    """A buffer too small to even hold the header must raise
    IncompleteFrame, not ProtocolError — TCP can deliver a single byte
    at a time and the reader needs to keep accumulating, not drop the
    connection."""
    codec = FrameCodec(KEY)
    with pytest.raises(IncompleteFrame):
        codec.decode(b"\x00\x00\x55\xaa" + b"\x00" * 4)


def test_decode_partial_payload_is_incomplete_not_malformed() -> None:
    """A valid header that claims more bytes than the buffer contains
    is the normal under-fragmentation case: header arrived, body hasn't
    yet. Must raise IncompleteFrame so the reader can wait for more."""
    codec = FrameCodec(KEY)
    wire = codec.encode(const.CMD_DP_QUERY, {"x": 1})
    # Slice off the last few bytes so the size field still claims the
    # full length but the buffer doesn't contain it yet.
    with pytest.raises(IncompleteFrame):
        codec.decode(wire[:-4])


def test_decode_bad_prefix() -> None:
    codec = FrameCodec(KEY)
    bad = b"\xde\xad\xbe\xef" + b"\x00" * 32
    with pytest.raises(ProtocolError):
        codec.decode(bad)


def test_decode_bad_suffix() -> None:
    """A frame with the right prefix and CRC but a corrupted suffix
    constant is a desync signal — reject so the read loop drops the
    socket."""
    codec = FrameCodec(KEY)
    wire = bytearray(codec.encode(const.CMD_DP_QUERY, {"x": 1}))
    # Zero out the suffix so it no longer matches FRAME_SUFFIX, then
    # recompute the CRC so the suffix check is the one that fires.
    wire[-4:] = b"\x00\x00\x00\x00"
    new_crc = binascii.crc32(bytes(wire[:-8])) & 0xFFFFFFFF
    wire[-8:-4] = struct.pack(">I", new_crc)
    with pytest.raises(ProtocolError, match="bad suffix"):
        codec.decode(bytes(wire))


def test_decode_bad_crc() -> None:
    codec = FrameCodec(KEY)
    wire = bytearray(codec.encode(const.CMD_DP_QUERY, {"x": 1}))
    wire[-8] ^= 0xFF  # flip a bit in the CRC
    with pytest.raises(ProtocolError):
        codec.decode(bytes(wire))


def test_decrypt_body_rejects_garbage() -> None:
    codec = FrameCodec(KEY)
    with pytest.raises(InvalidAuth):
        codec.decrypt_body(b"\x00" * 16)


def test_decrypt_body_empty_returns_empty_dict() -> None:
    codec = FrameCodec(KEY)
    assert codec.decrypt_body(b"") == {}


def test_decrypt_body_post_aes_corruption_is_protocol_error_not_invalid_auth() -> None:
    """AES decrypts cleanly with our key but the plaintext is not JSON →
    ProtocolError. Distinguishes a one-shot wire-corruption event (the
    next frame will land fine) from a permanently-wrong local_key, so
    the caller doesn't trigger a needless reauth flow."""
    codec = FrameCodec(KEY)
    ct = aes_encrypt(b"not json at all", KEY.encode())
    with pytest.raises(ProtocolError):
        codec.decrypt_body(ct)


def test_decrypt_body_non_object_json_is_protocol_error() -> None:
    """Valid JSON but not a JSON object (e.g. an array or scalar) is the
    same wire-corruption shape — ProtocolError, not InvalidAuth."""
    codec = FrameCodec(KEY)
    ct = aes_encrypt(b"[1,2,3]", KEY.encode())
    with pytest.raises(ProtocolError):
        codec.decrypt_body(ct)


def test_split_response_payload_does_not_strip_three_byte_coincidence() -> None:
    """split_response_payload must require the full 15-byte v3.3 header
    before peeling it off — peeling on just the 3-byte ASCII prefix
    "3.3" would silently truncate a random AES ciphertext that happened
    to begin with those bytes (1 in ~16M)."""
    codec = FrameCodec(KEY)
    # Build a body shaped like a response payload: 4-byte retcode + bytes
    # that start with b"3.3" but are NOT a v3.3 header (next 12 bytes are
    # not zeros, so this is plain ciphertext that just begins with "3.3").
    fake_ciphertext = b"3.3" + bytes(range(0x10, 0x1D))  # 16 bytes total
    payload = b"\x00\x00\x00\x00" + fake_ciphertext
    retcode, body = codec.split_response_payload(const.CMD_DP_QUERY, payload)
    assert retcode == 0
    assert body == fake_ciphertext  # NOT truncated


def test_decode_rejects_oversize_size_field() -> None:
    """A header claiming a multi-GiB frame must be rejected immediately
    instead of waiting for the bytes to arrive — protects against a
    hostile LAN peer trying to exhaust memory."""
    codec = FrameCodec(KEY)
    # Header with size = 0xFFFFFFFF (~4 GiB); no payload follows.
    header = struct.pack(">IIII", const.FRAME_PREFIX, 1, const.CMD_DP_QUERY, 0xFFFFFFFF)
    # Pad to clear the "frame too short" guard so the size check is reached.
    wire = header + b"\x00" * 8
    with pytest.raises(ProtocolError, match="frame too large"):
        codec.decode(wire)
