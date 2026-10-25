# Colmi Ring BLE Integration — Debug & Fix Plan

## Status / root-cause diagnosis (CORRECTED after on-hardware testing)
**Correction:** the HA host uses a **local BLE radio (no ESPHome proxy)**. The earlier "proxy routing" hypothesis was wrong. The library's own CLI (`colmi_r02_client … info`, which uses a host-local `BleakClient(address)` — [`client.py:66`](../custom_components/colmi_ring/api.py)) was run by the user on a machine with a working local radio and **succeeded**, proving the protocol/codec layer, the MAC (`64:72:6A:79:38:36`), and the FFF0/Nordic-UART service+chars are all correct for this ring.

Therefore the integration's actual "never worked" cause was NOT proxy routing. It was the combination of:
1. **Unreachable pinned dependency.** `manifest.json` pinned library commit `9e08383...`, which was garbage-collected upstream → a fresh install could not resolve `colmi_r02_client`, so the integration likely never cleanly installed. (Fixed: re-pinned to HEAD `19e70aa5`.)
2. **Uncaught `TimeoutError`** (see Secondary bug #1) and the other latent bugs (None-payload `native_value`, fragile config-flow validation) that crash the first refresh / config flow even once the library imports.

On THIS Windows dev box the library CLI fails with `BleakDeviceNotFoundError: ... was not found` — because the box's radio is broken (Broadcom BCM20703 stuck in Error state) and/or the ring is out of range. That is a property of this PC, not of the code; the ring is reachable by a healthy local radio (proven by the user's CLI success).

Confirmed environment facts:
- HA has a **local radio**, no proxy. (Validated: user ran the library CLI successfully on a local-radio machine.)
- Dev venv has `bleak 0.22.3` but **not** `bleak_retry_connector`/`homeassistant`; the Windows dev box has no usable radio, so live ring + HA validation must happen on the real HA instance.
- HA dev pin bumped from `2025.2.4` to `2026.10.4` ([`requirements.txt`](../requirements.txt)).

## Secondary latent bugs (fix while in there)
1. **Uncaught `TimeoutError`.** Library log reads use `asyncio.wait_for(..., timeout=2)` ([`client.py:193`](../custom_components/colmi_ring/api.py:50), [`client.py:226`](../custom_components/colmi_ring/api.py:50)). `TimeoutError` is not a `ColmiRingApiClientError`, so it bypasses [`coordinator.py:31`](../custom_components/colmi_ring/coordinator.py:31) and crashes the poll with a raw traceback.
2. **`native_value` on `None` payload.** [`sensor.py:59`](../custom_components/colmi_ring/sensor.py:59) calls `self.coordinator.data.get(...)`; `async_get_data` can return `None` ([`api.py:76`](../custom_components/colmi_ring/api.py:52)), raising `AttributeError`.
3. **Dead code.** Stored `self._colmi_client` in [`api.py:47`](../custom_components/colmi_ring/api.py:47) is created then never used (each method opens a fresh client).
4. **Config-flow validation is fragile.** `_test_credentials` ([`config_flow.py:73`](../custom_components/colmi_ring/config_flow.py:73)) runs a full `get_full_data`; if it throws an unwrapped type the flow shows "unknown".
5. **Unique id via `slugify(address)`** ([`config_flow.py:51`](../custom_components/colmi_ring/config_flow.py:51)) is flagged in-code as not-production-grade; prefer `format_mac`/Bluetooth discovery info unique id.

## STATUS: Option B IMPLEMENTED (pending live-host validation)
The integration now owns its BLE transport via `bleak_retry_connector.establish_connection` + HA's `async_ble_device_from_address` lookup (see [`api.py`](../custom_components/colmi_ring/api.py)); the library's `Client` connection internals are no longer used — only the pure codecs (hr/steps/battery/packet/date_utils/set_time/blink_twice/reboot) are imported. The pinned library commit was garbage-collected upstream, so it was re-pinned to HEAD `19e70aa5...` (codecs verified identical). `bleak-retry-connector` added to [`manifest.json`](../custom_components/colmi_ring/manifest.json) + [`requirements.txt`](../requirements.txt). Secondary bugs (uncaught TimeoutError, None-payload native_value, slugify unique_id, dead code) all fixed.

Note: with HA on a **local radio** (this host's actual setup), Option B performs the same host-local connect that the working library CLI uses, because `async_ble_device_from_address` returns the locally-discovered `BLEDevice` and `establish_connection` then builds a host-local `BleakClient` — additionally it handles ESPHome proxies automatically if one is ever added. So Option B is correct for the local-radio case too. Remaining step = end-to-end validation on the live HA host (cannot be validated on this Windows dev box: no `homeassistant`/`bleak_retry_connector` installed and no usable radio).

## Decided approach
Route BLE through Home Assistant's Bluetooth stack so ESPHome proxies work. Two implementation options — pick one during the code step after a short spike:

### Option A — monkey-patch the library's client factory (smallest change, recommended first)
Inject a proxy-aware `BleakClient` into `colmi_r02_client.client.Client` before it calls `BleakClient(...)`:
- Resolve a `BLEDevice` from HA's cached scanner via `homeassistant.components.bluetooth.async_bluetooth_address(...)` / `async_bleak_scanner_start` + `async_discovered_service_info`, then construct the client with `bleak_retry_connector.create_platform_client(address_or_device, ...)` (or pass the resolved `BLEDevice` to `BleakClient`, which HA's `habluetooth` patches at runtime to route via `bleak_retry_connector`).
- Requires adding `bleak_retry_connector` (and `bluetooth-adapters`) to `manifest.json` requirements.

### Option B — vendor/replace the connection layer (cleanest long-term)
Stop using the library's `Client` connection internals; instead build our own client that holds an HA-managed `BleakClient` and reuse only the library's **pure** packet codecs (`hr`, `steps`, `battery`, `packet`, `date_utils`). More robust and matches HA's [BLE best-practices note](../custom_components/colmi_ring/api.py:44), but larger.

> Recommendation: prototype **Option A** first to unblock the proxy path quickly, then refactor toward Option B if the monkey-patch proves brittle.

## Phased todo (execution order)
1. **Add diagnostic logging + exception hardening** (safe, no behavior change):
   - `api.py`: log the dialed address, whether connect starts, and the exact exception type from `get_full_data`; wrap calls so any `Exception`/`TimeoutError` becomes a `ColmiRingApiClientCommunicationError`.
   - `coordinator.py`: also catch `TimeoutError`/generic `Exception` → `UpdateFailed`.
   - `sensor.py`: guard `data` being `None` (return `None`).
2. **Spike Option A** on the real HA host: add `bleak_retry_connector`/`bluetooth-adapters` to `manifest.json` requirements; write a tiny `async` connect using HA's `BLEDevice` for the ring address; confirm `BleakClient` connects through the ESPHome proxy and `get_battery()` returns.
3. **Wire the proxy-aware client** into `ColmiRingApiClient` (replace raw `BleakClient(...)` with the HA-resolved device / `create_platform_client`), per chosen option.
4. **Fix config flow**: validate using the same proxy-aware path; set a stable `unique_id` via `homeassistant.components.bluetooth` (MAC) instead of `slugify`.
5. **Clean up dead code** (`self._colmi_client`, unused `aiohttp`/`async_timeout`/`ATTRIBUTION`).
6. **Validate end-to-end on the real HA instance** (`https://w8wat.duckdns.org:38327/`): integration loads, heart-rate sensor updates, Blink Twice / Reboot buttons act on the ring.
7. **Tune** refresh interval and consider real-time heart-rate (library `get_realtime_reading`) instead of the day-log path if latency matters.

## Validation checkpoints
- Log shows a successful `BleakClient` connect through the proxy (not "Device with address ... was not found" / "Not found").
- `get_battery()` and `get_heart_rate_log()` succeed via CLI/HA on the HA host.
- Integration config entry loads (device + entities appear); sensor gets a non-`None` value.

## Risks / open questions
- ESPHome proxy must have `bluetooth_proxy: active: true` and the ring must not be held by a phone app (single-central limit).
- HA 2025.2 BLE APIs (`habluetooth`/`bleak_retry_connector`) evolve; pin versions to what's on the host.
- Library pinned to a garbage-collected commit upstream — consider re-pinning to a reachable commit/tag to keep installs reproducible.
