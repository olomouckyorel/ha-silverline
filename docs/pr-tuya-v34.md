# Pull request: Tuya v3.4 support (Poolex Silverline wfzeiyn1ed3axxde)

Use the text below when opening a PR against `christianreiss/ha-silverline`.

---

## Summary

Adds **Tuya protocol v3.4** support to `pysilverline` and the Home Assistant integration, verified live against a **Poolex Silverline** pool heat pump (`productKey` `wfzeiyn1ed3axxde`, WBR3 WiFi module).

v3.4 sits between v3.3 (55AA + AES-ECB, no handshake) and v3.5 (6699 + AES-GCM). It reuses the 55AA framing but adds a **3-message session-key handshake** and **HMAC-SHA256 frame authentication** instead of CRC32.

### Protocol (`pysilverline`)

- `Frame34Codec` — session-key derivation, HMAC footer, encrypted payloads
- Auto-probe order extended to **3.5 → 3.4 → 3.3**
- v3.4 socket lifecycle: device closes TCP after each query; client reconnects lazily on the next poll (no heartbeat loop)
- Unit tests: `test_protocol_34.py`, `test_client_34.py` (fake server including post-query peer-close)

### Integration

- New device profile **`silverline_v34`** with DP layout mapped from Tuya IoT console + live LAN dump
- Fan RPM on **DP 114** (not DP 110 on this firmware)
- `productKey` `wfzeiyn1ed3axxde` added to discovery allowlist
- `layout_for_model()` wires semantic fields to the v3.4 DP numbering

### Library version

- `pysilverline` **0.3.6** (integration manifest pins `pysilverline==0.3.6`)
- Integration version **0.8.6**

## Test plan

- [x] `pytest pysilverline/tests/` — v3.4 protocol + client tests pass
- [x] Live HA setup: config flow, climate control, presets, diagnostic sensors
- [x] No entity `unavailable` flicker on 30 s poll interval (v3.4 reconnect path)
- [ ] Maintainer: publish `pysilverline` 0.3.6 to PyPI before integration release tag

## Live verification

| Item | Value |
|---|---|
| Hardware | Poolex Silverline pool heat pump (2026 firmware) |
| `productKey` | `wfzeiyn1ed3axxde` |
| Protocol | Tuya v3.4 (auto-detected) |
| Home Assistant | Custom integration via HACS, 30 s polling |

No device IDs, local keys, or LAN IPs are included in this PR.

## Notes for reviewer

1. **v3.4 DP numbering differs** from legacy JetLine/PC-SLP090N — see `pysilverline/src/pysilverline/layouts.py` (`LAYOUT_V34_WFZEIYN`).
2. **Single TCP client** — disable cloud Tuya / close Smart Life during local setup (existing WBR3 limitation).
3. **Push updates on v3.4** are best-effort only; polling remains the reliable update path because the device closes the socket after each exchange.
4. Temporary fork used vendored `pysilverline` for HACS testing before PyPI publish — **removed** in this PR; integration relies on the PyPI pin as usual.

## Credits

Community reverse-engineering of Tuya v3.4 (TinyTuya, Tuya IoT DP export) plus live testing on real hardware.
