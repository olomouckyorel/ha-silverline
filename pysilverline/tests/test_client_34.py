"""End-to-end integration tests for SilverlineClient over Tuya v3.4 (55AA / ECB + HMAC).

These exercise the *client* handshake, auto-probe and fallback paths that the
codec-level ``test_protocol_34`` never touches.

``FakeTuya34Server`` is the oracle, and its faithfulness is the whole point —
because there is NO real v3.4 hardware (the live device is the v3.3 PC-SLP090N).
The fake mirrors TinyTuya's device side exactly on the two details that bit the
v3.5 implementation:

  * it decodes the START/FINISH negotiation frames with the *real* key and
    switches to the derived ECB session key only *after* FINISH is processed.
    A client that (wrongly) encrypted FINISH under the session key would fail
    the fake's real-key decode → handshake fails → ``get_status`` times out.
  * it answers data requests by *echoing the request seqno* (v3.4 is gated
    ``version < 3.5`` in TinyTuya's ``_get_retcode``, so it echoes — unlike
    v3.5's global counter). The client's exact ``(seq, cmd)`` match resolves it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import struct
from typing import Any

import pytest

from pysilverline import SilverlineClient, const
from pysilverline.exceptions import CannotConnect, InvalidAuth
from pysilverline.protocol import (
    Frame34Codec,
    aes_decrypt,
    aes_encrypt,
    derive_session_key_34,
)

KEY = "0123456789abcdef"
KEY_B = KEY.encode()
DEVICE_ID = "bf12345678abcdefghijkl"
# Fixed device nonce so derivations are deterministic across the test run.
REMOTE_NONCE = bytes(range(16, 32))
_SHA = hashlib.sha256


def _encode_34(
    seq: int, cmd: int, plaintext: bytes, key: bytes, *, retcode: int | None = None
) -> bytes:
    """Build one 55AA HMAC frame with an explicit seq and encryption/HMAC key.

    ``plaintext`` is AES-ECB-encrypted with ``key``; an optional unencrypted
    ``retcode`` is prepended (device→client frames carry one); the trailer is a
    32-byte HMAC-SHA256 over header+payload, also keyed with ``key`` (the real
    key during the handshake, the session key afterwards).
    """
    ciphertext = aes_encrypt(plaintext, key)
    payload = b"" if retcode is None else struct.pack(">I", retcode)
    payload += ciphertext
    size = len(payload) + 36  # 32-byte HMAC + 4-byte suffix
    header = struct.pack(">IIII", const.FRAME_PREFIX, seq, cmd, size)
    pre = header + payload
    mac = hmac.new(key, pre, _SHA).digest()
    return pre + mac + struct.pack(">I", const.FRAME_SUFFIX)


class FakeTuya34Server:
    """A tiny TCP server speaking Tuya local protocol v3.4."""

    def __init__(self, *, resp_key: bytes | None = None) -> None:
        self.handlers: dict[int, Any] = {}
        self.received: list[tuple[int, int, dict[str, Any]]] = []
        # When set, encrypt the NEG_RESP under this (wrong) key so the client
        # cannot authenticate it — simulates a wrong local_key.
        self._resp_key = resp_key
        self._server: asyncio.base_events.Server | None = None
        self.port: int = 0
        self.finish_decoded_with_real_key = False
        self.finish_hmac_ok = False
        self.session_key: bytes | None = None
        self.connections = 0
        self.drop_after_handshake = False
        self._writer: asyncio.StreamWriter | None = None

    async def __aenter__(self) -> "FakeTuya34Server":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def push(self, cmd: int, body: dict[str, Any], *, retcode: int | None) -> None:
        """Send a spontaneous frame under the session key."""
        if self._writer is None or self.session_key is None:
            raise RuntimeError("push before handshake complete")
        wire = _encode_34(
            0x9999, cmd, json.dumps(body).encode(), self.session_key, retcode=retcode
        )
        self._writer.write(wire)
        await self._writer.drain()

    async def push_malformed(self) -> None:
        """Send a session-key frame with a corrupted suffix (clean desync drop)."""
        if self._writer is None or self.session_key is None:
            raise RuntimeError("push before handshake complete")
        wire = bytearray(
            _encode_34(
                0x1234, const.CMD_STATUS, b'{"dps":{}}', self.session_key, retcode=0
            )
        )
        wire[-2] ^= 0xFF
        self._writer.write(bytes(wire))
        await self._writer.drain()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writer = writer
        self.connections += 1
        # Codec starts on the real key — every negotiation frame must decode
        # under it; we switch to the session key only after FINISH.
        codec = Frame34Codec(KEY)
        local_nonce = b""
        session_key: bytes | None = None
        buf = bytearray()
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                buf.extend(chunk)
                while len(buf) >= 52:  # 16-byte header + 36-byte trailer minimum
                    try:
                        frame, remainder = codec.decode(bytes(buf))
                    except Exception:
                        # Real-key decode failure: the peer encrypted a
                        # negotiation frame with the wrong key (the
                        # FINISH-under-session-key bug) or it is not a v3.4
                        # peer. A real device drops the socket — so do we, which
                        # turns the bug into an observable RED.
                        return
                    buf = bytearray(remainder)

                    if frame.cmd == const.SESS_KEY_NEG_START:
                        local_nonce = aes_decrypt(frame.payload, KEY_B)
                        inner = REMOTE_NONCE + hmac.new(
                            KEY_B, local_nonce, _SHA
                        ).digest()
                        resp_key = self._resp_key or KEY_B
                        writer.write(
                            _encode_34(
                                frame.seq,
                                const.SESS_KEY_NEG_RESP,
                                inner,
                                resp_key,
                                retcode=0,
                            )
                        )
                        await writer.drain()
                    elif frame.cmd == const.SESS_KEY_NEG_FINISH:
                        # Reaching here means FINISH decoded under the REAL key.
                        self.finish_decoded_with_real_key = True
                        got = aes_decrypt(frame.payload, KEY_B)
                        expected = hmac.new(KEY_B, REMOTE_NONCE, _SHA).digest()
                        self.finish_hmac_ok = hmac.compare_digest(got, expected)
                        session_key = derive_session_key_34(
                            local_nonce, REMOTE_NONCE, KEY_B
                        )
                        self.session_key = session_key
                        codec.update_session_key(session_key)
                        if self.drop_after_handshake and self.connections == 1:
                            return
                    else:
                        body = codec.split_request_payload(frame.payload)
                        decrypted = codec.decrypt_body(body) if body else {}
                        self.received.append((frame.seq, frame.cmd, decrypted))
                        handler = self.handlers.get(frame.cmd)
                        if handler is not None:
                            assert session_key is not None
                            # v3.4 ECHOES the request seqno (TinyTuya's <3.5 gate).
                            response = handler(frame.seq, decrypted, session_key)
                            if response is not None:
                                writer.write(response)
                                await writer.drain()
        finally:
            writer.close()


def _dp_query_handler(dps: dict[str, Any]) -> Any:
    def handler(seq: int, body: dict[str, Any], session_key: bytes) -> bytes:
        # DP_QUERY responses are header-less.
        payload = json.dumps({"devId": DEVICE_ID, "dps": dps}).encode()
        return _encode_34(seq, const.CMD_DP_QUERY, payload, session_key, retcode=0)

    return handler


def _control_handler() -> Any:
    def handler(seq: int, body: dict[str, Any], session_key: bytes) -> bytes:
        # CONTROL responses carry the inner version header — proves the client's
        # decrypt_body strips it on the response path too.
        payload = const.PROTOCOL_34_HEADER + json.dumps(
            {"dps": body.get("dps", {})}
        ).encode()
        return _encode_34(seq, const.CMD_CONTROL, payload, session_key, retcode=0)

    return handler


# ---------------------------------------------------------------------------
# Handshake + data round-trips
# ---------------------------------------------------------------------------


async def test_v34_autoprobe_handshake_and_get_status() -> None:
    """Auto-probe (no pinned version) negotiates v3.4 and reads DPs end to end."""
    async with FakeTuya34Server() as server:
        server.handlers[const.CMD_DP_QUERY] = _dp_query_handler(
            {"1": True, "4": "Heat", "3": 27, "2": 30}
        )
        client = SilverlineClient(
            host="127.0.0.1",
            port=server.port,
            device_id=DEVICE_ID,
            local_key=KEY,
            protocol_version=None,  # auto-probe: v3.5 fails, v3.4 wins
            request_timeout=2.0,
        )
        await client.connect()
        try:
            assert client.detected_version == "3.4"
            state = await client.get_status()
            assert state.power is True
            assert state.mode == "Heat"
            assert state.temp_current == 27
            assert state.temp_set == 30
            # Handshake proof: a data round-trip is only possible because the
            # device decoded FINISH under the real key (then switched to the
            # session key) and the FINISH HMAC verified.
            assert server.finish_decoded_with_real_key is True
            assert server.finish_hmac_ok is True
        finally:
            await client.disconnect()


async def test_v34_pinned_handshake_and_get_status() -> None:
    """Pinning protocol_version='3.4' skips the probe and negotiates directly."""
    async with FakeTuya34Server() as server:
        server.handlers[const.CMD_DP_QUERY] = _dp_query_handler({"1": False})
        client = SilverlineClient(
            host="127.0.0.1",
            port=server.port,
            device_id=DEVICE_ID,
            local_key=KEY,
            protocol_version="3.4",
            request_timeout=2.0,
        )
        await client.connect()
        try:
            assert client.detected_version == "3.4"
            assert (await client.get_status()).power is False
        finally:
            await client.disconnect()


async def test_v34_set_multiple_round_trip() -> None:
    """A CONTROL command negotiates and is accepted over v3.4 (header inside ct)."""
    async with FakeTuya34Server() as server:
        server.handlers[const.CMD_CONTROL] = _control_handler()
        client = SilverlineClient(
            host="127.0.0.1",
            port=server.port,
            device_id=DEVICE_ID,
            local_key=KEY,
            protocol_version="3.4",
            request_timeout=2.0,
        )
        await client.connect()
        try:
            await client.set_multiple({const.DP_TEMP_SET: 28, const.DP_POWER: True})
            controls = [r for r in server.received if r[1] == const.CMD_CONTROL]
            assert len(controls) == 1
            # The device decrypted the CONTROL body — inner header stripped,
            # both DPs present.
            assert controls[0][2]["dps"] == {"2": 28, "1": True}
            assert client.state.temp_set == 28
            assert client.state.power is True
        finally:
            await client.disconnect()


@pytest.mark.parametrize("with_retcode", [True, False])
async def test_v34_push_is_dispatched_to_listener(with_retcode: bool) -> None:
    """A spontaneous device push over v3.4 reaches listeners, with or without
    the optional 4-byte retcode prefix (the len%16 heuristic handles both)."""
    async with FakeTuya34Server() as server:
        server.handlers[const.CMD_DP_QUERY] = _dp_query_handler({"1": True})
        client = SilverlineClient(
            host="127.0.0.1",
            port=server.port,
            device_id=DEVICE_ID,
            local_key=KEY,
            protocol_version="3.4",
            request_timeout=2.0,
        )
        seen: list[Any] = []
        client.add_listener(lambda state: seen.append(state))
        await client.connect()
        try:
            await client.get_status()  # establishes session_key on the server
            await server.push(
                const.CMD_STATUS, {"dps": {"3": 31}}, retcode=0 if with_retcode else None
            )
            for _ in range(50):
                if seen:
                    break
                await asyncio.sleep(0.02)
            assert seen, "push was never dispatched"
            assert seen[-1].temp_current == 31
        finally:
            await client.disconnect()


async def test_v34_reconnect_rehandshakes() -> None:
    """On reconnect the codec resets to the real key and negotiates afresh."""
    async with FakeTuya34Server() as server:
        server.drop_after_handshake = True
        server.handlers[const.CMD_DP_QUERY] = _dp_query_handler({"1": True})
        client = SilverlineClient(
            host="127.0.0.1",
            port=server.port,
            device_id=DEVICE_ID,
            local_key=KEY,
            protocol_version="3.4",
            request_timeout=2.0,
        )
        await client.connect()
        try:
            # First connection drops right after its handshake; the client must
            # reconnect and negotiate a fresh session key on connection #2.
            for _ in range(100):
                if server.connections >= 2 and client.connected:
                    break
                await asyncio.sleep(0.05)
            assert server.connections >= 2
            state = await client.get_status()
            assert state.power is True
        finally:
            await client.disconnect()


# ---------------------------------------------------------------------------
# Auto-probe ordering, fallback, and pinned-version failure
# ---------------------------------------------------------------------------


async def test_v34_autoprobe_falls_back_to_v33() -> None:
    """A v3.3-only device: auto-probe tries v3.5, then v3.4, then lands on v3.3.

    The v3.4 probe sends a 55AA 0x03 frame the v3.3 fake cannot parse, so it
    drops the socket — exactly the fallback path that must NOT misfire as
    InvalidAuth (55AA framing is shared with v3.3)."""
    from tests.test_client import FakeTuyaServer, _build_frame

    async with FakeTuyaServer() as server:
        server.handlers[const.CMD_DP_QUERY] = lambda seq, body: _build_frame(
            seq,
            const.CMD_DP_QUERY,
            {"devId": DEVICE_ID, "dps": {"1": True, "4": "Cool"}},
        )
        client = SilverlineClient(
            host="127.0.0.1",
            port=server.port,
            device_id=DEVICE_ID,
            local_key=KEY,
            protocol_version=None,
            request_timeout=2.0,
        )
        await client.connect()
        try:
            assert client.detected_version == "3.3"
            state = await client.get_status()
            assert state.power is True
            assert state.mode == "Cool"
        finally:
            await client.disconnect()


async def test_pinned_v34_against_v33_server_raises() -> None:
    """Pinning v3.4 against a v3.3-only device fails loudly (no silent fallback)."""
    from tests.test_client import FakeTuyaServer

    async with FakeTuyaServer() as server:
        client = SilverlineClient(
            host="127.0.0.1",
            port=server.port,
            device_id=DEVICE_ID,
            local_key=KEY,
            protocol_version="3.4",
            request_timeout=2.0,
        )
        with pytest.raises(CannotConnect):
            await client.connect()
        await client.disconnect()


async def test_pinned_v34_wrong_key_raises_invalid_auth() -> None:
    """A NEG_RESP the client can't authenticate surfaces as InvalidAuth when
    v3.4 is pinned — the reauth-trigger path, not CannotConnect."""
    async with FakeTuya34Server(resp_key=b"wrongkeywrongk!!") as server:
        client = SilverlineClient(
            host="127.0.0.1",
            port=server.port,
            device_id=DEVICE_ID,
            local_key=KEY,
            protocol_version="3.4",
            request_timeout=2.0,
        )
        with pytest.raises(InvalidAuth):
            await client.connect()
        await client.disconnect()
