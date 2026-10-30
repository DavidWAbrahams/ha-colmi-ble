# Motion / gesture support for colmi_ring

Goal: read the ring's live accelerometer stream, detect **clap**, **rapid hand raise** and
**rapid hand lower**, and expose them as Home Assistant `event` entities. Streaming is
opt-in (a `switch`) because it holds the GATT link open and costs battery.

Protocol source: [`mrfloydst/smartringmidi bluetooth.html`](https://github.com/mrfloydst/smartringmidi/blob/main/bluetooth.html)
(user-provided), whose axis bit-shifting is itself lifted from
[`atc1441/ATC_RF03_Ring`](https://github.com/atc1441/ATC_RF03_Ring).

---

## Status: what is built

Phases 1-4 are implemented and validated offline; **Phase 0 was deliberately deferred** on the
user's instruction to implement against best guesses and correct the false ones later. Every guess
therefore sits in a named constant or a single documented assumption.

The assumptions split into two kinds, and the difference matters for what the ring run can teach us:

**Confirmed against the reference implementation.** The demo's JavaScript was transcribed literally
and compared with our Python - command frames byte-for-byte including the checksum, and the axis
decode over edge cases plus 200k random packets - with zero mismatches. This is what
[`scripts/_motion_ref_parity.py`](../scripts/_motion_ref_parity.py) re-checks in a second, no ring
required:

| Definition | Parity with the reference |
|---|---|
| RAW on = `A1 04 04` (checksum `A9`), off = `A1 02` (`A3`) | byte-identical |
| camera gesture `02 04` / `02 06` | byte-identical |
| 12-bit axes in wire order **Y, Z, X**, low bits in the **low** nibble of the next byte | identical over 200k packets |
| 12-bit two's-complement sign extension | identical |
| +/-4 g over 12 bits (`g = raw / 2048 * 4`) | identical |
| `packet[1] == 3` marks position data | same gate |

**Still unproven, and only the ring can settle these:**

| Assumption | Where it lives | If wrong |
|---|---|---|
| the R02 *answers* `A1 04 04` at all, and **which command byte it reports accel on** | `CMD_RAW_MODE` in `colmi_codec/motion.py` | no samples arrive; see the note below on how we would see this |
| position packets are `packet[1] == 3` **on the R02** | `colmi_codec/motion.py` | no samples are ever decoded |
| which `packet[1]` values are the camera gesture *reporting* (as opposed to our own enable command being echoed back) | `GESTURE_REPORT_TYPES` | phantom claps at session start, or none at all |
| the stream's real sample rate | `MOTION_MIN_HZ_FOR_STREAM_CLAP` | the stream clap is wrongly enabled/disabled |
| which axis is vertical, and its sign | `gestures.py` (gravity-relative by construction) | raise and lower swap |

Note the asymmetry in the first row: the reference classifies a notification **only** by `value[1]`
and attaches its listener to *every* notifiable characteristic, so it never asserts that position
data arrives on `0xA1`. We do - [`_handle_tx`](../custom_components/colmi_ring/api.py) dispatches by
`packet[0]`, and the motion session subscribes to `0xA1` and `0x02` only. If the R02 serves accel on
another command byte, the packets are dropped in `api.py` before reaching `MotionManager`, and the
evidence is the DEBUG-only "Unexpected packet type *N* (0x*NN*) … *payload*" line rather than the
manager's own "unknown stream packet" line. So for the ring run, enable DEBUG on
`custom_components.colmi_ring` (the integration's *root* logger: `api.py`, `coordinator.py` and
`motion_manager.py` all log through the shared `LOGGER` from `const.py`, so there is no separate
`.api` logger to enable); a byte that shows up repeatedly at stream-rate while streaming is the real
command byte. If that happens the fix is one constant - or a catch-all listener for the duration of a
session.

A pleasant consequence of projecting onto the *estimated gravity vector* instead of picking a
"vertical axis" by name: raise-vs-lower is correct for **any** axis mapping, since it only depends
on the sign of the component along gravity. Only the scale/nibble assumptions can break the
detector wholesale, and those are now the part the reference confirms.

---

## Decoded protocol

### Transport

Same UART service we already use ([`api.py`](../custom_components/colmi_ring/api.py) lines 53-55):
write to `6E400002-…`, notify from `6E400003-…`. The demo attaches its listener to *every*
notifiable characteristic of either the `de5bf728-…` (RF03) or `6E40fff0-…` (Colmi UART)
service, so on the R02 our existing TX notify subscription is the right place - no new
characteristic work.

### Enable / disable RAW mode

| Action | Bytes | Our equivalent |
|---|---|---|
| RAW data on | `A1 04 04 … A9` | `make_packet(0xA1, bytearray([4, 4]))` |
| RAW data off | `A1 02 … A3` | `make_packet(0xA1, bytearray([2]))` |

The demo builds the 16-byte frame as `arrayOut[15] = (sum of data bytes) & 0xFF`, which is
byte-for-byte what [`make_packet`](../custom_components/colmi_ring/colmi_codec/packet.py)
already does (byte 15 is zero when summed, so `sum(packet) & 255` is identical). **No new
framing code needed.**

### Accelerometer sample

Notifications are classified by **byte 1**; `byte[1] == 3` means position data. Each axis is
a **12-bit big-endian signed value split over two bytes** - high byte, then the low bits in
the *next* byte:

```js
rawY = int12(((value[2] << 4) | (value[3] & 0xf)) & 0xfff)
rawZ = int12(((value[4] << 4) | (value[5] & 0xf)) & 0xfff)
rawX = int12(((value[6] << 4) | (value[7] & 0xf)) & 0xfff)
int12(v) = v > 2047 ? v - 4096 : v
G        = (raw / 2048) * 4        // +/-4 g range
```

Notes for the decoder:
- **Axis order on the wire is Y, Z, X** - easy to get wrong, and it matters for
  raise-vs-lower sign.
- The low nibble is masked from the **low** nibble of the following byte (`& 0xf`). Copy it
  verbatim, but *confirm* with the Phase 0 probe whether the ring really parks the low bits
  there rather than in the high nibble (`>> 4`); if we read garbage that never sits near
  +/-1 g at rest, flip that assumption.
- Sanity check at rest: `|(Ax, Ay, Az)|` should be ~1.0 g. That single assertion validates
  offsets, scale and sign together, and is the acceptance test for Phase 1.

### Bonus: the ring already detects a clap on-device

From the demo's own text: *"Turn the ring's 'camera button' into a 'clap' trigger. The camera
gesture detection can be turned on by sending `0204` and turned off by sending `0206`."*
i.e. `cmd 0x02` with sub `0x04` / `0x06` (add a checksum byte as usual). This matters because
the stock firmware streams RAW data slowly (the demo notes a **custom firmware** is needed
to "speed up RAW data transmission intervals considerably"; its own interpolation loop runs
at 40 ms as an aspiration, not a measured rate). At a low native sample rate, deriving a
sub-120 ms clap impulse from the accel stream is unreliable - whereas the camera gesture is
detected **on the ring**, costs no continuous connection, and arrives as an event.

Both routes are therefore used **at the same time** (as built): a session arms the camera gesture and
runs the stream clap in parallel, labelling each event `source=ring` or `source=stream`, and the
measured rate decides only whether the stream route is kept. Which one ends up carrying clap is the
Phase 0 probe's to report, not to decide.

---

## Phase 0 - Probe the real ring ⏸ deferred (Phases 1-4 built ahead of it, deliberately)

The user chose to build against best guesses first and correct the false ones afterwards, so the
codec, transport, detector and HA surface all landed without a capture. The guesses and how each
one fails are tabulated under **Status** above. `scripts/_accel_probe.py` still needs updating to
the as-built packet names before it runs; its remaining unique value is the two things no
simulation can produce - the stream's real sample rate, and battery drain per streaming hour.

`scripts/_accel_probe.py` (dev-only, matching the existing `scripts/_*.py` convention):

1. Connect via local `bleak` to `78:14:12:A0:0F:8C`, subscribe to the TX char with a raw
   `hex() + monotonic timestamp` logger.
2. Record passively ~30 s (the ring may emit motion notifications unprompted).
3. Send `make_packet(0xA1, [4, 4])`; sample while the hand is (a) still, (b) raised,
   (c) lowered, (d) clapped; then send `make_packet(0xA1, [2])`.
4. Report: notification **rate** (Hz) - the number that decides the clap strategy; which
   byte offsets vary (per-byte variance isolates the axis fields); whether the rest
   magnitude is ~1 g with the `& 0xf` decode; the actual `byte[0]`/`byte[1]` values; and
   whether a camera/clap gesture packet appears after `02 04` (sweep `02 04` / `02 06` and
   log what arrives).

Deliverables: confirmed constants, decode function, measured Hz, and the clap source
(on-device gesture vs stream-derived).

## Phase 1 - Codec: `colmi_codec/motion.py` ✅ built

- `CMD_RAW_MODE = 0xA1`, `get_raw_start_packet()` / `get_raw_stop_packet()`.
- `CMD_GESTURE = 0x02`, `get_camera_gesture_on_packet()` / `..._off_packet()`; `Gesture` enum
  for what the ring reports (`parse_gesture_packet`).
- `TYPE_POSITION = 3`; `@dataclass(frozen=True, slots=True) AccelSample(x, y, z: float)` in
  **g**, plus `magnitude_g` and `gravity_vector()` so the detector never touches raw counts.
  No `pitch_roll()`: the detector compares two heading *vectors* by angle, which sidesteps the
  `atan2` quadrant and gimbal awkwardness of Euler angles entirely.
- `parse_accel_packet(packet) -> AccelSample | None` (named for what it returns, not for the
  stream) - returns `None` for a non-position packet or unexpected length instead of raising, so
  one stray frame cannot kill the stream (contrast:
  [`parse_real_time_reading`](../custom_components/colmi_ring/colmi_codec/real_time.py) asserts on
  the command byte, which would be fatal here).
- Validation is `scripts/_motion_replay.py` (see Phase 5) rather than a separate decode script:
  it replays either a synthetic stream or a captured one through this parser, so the ~1 g-at-rest
  and raise-sign assertions run against the same code path the integration uses.
- `scripts/_motion_ref_parity.py` checks this module against the reference web app itself (command
  frames + checksum, and the decode over 200k random packets). It needs no ring, so it is the check
  to run after *any* edit to this file; it currently passes with zero mismatches, which is why the
  **Status** table lists the decode as confirmed rather than assumed.

## Phase 2 - Transport: fan-out listener + long-lived shared session ✅ built

[`_RingConnection._handle_tx`](../custom_components/colmi_ring/api.py) routes each packet to
a single per-command `asyncio.Queue` awaited by one `_command` call, and silently drops
packets whose `packet[0]` has no handler. That cannot represent an open-ended stream.

- `add_listener(cmd, cb)` / `remove_listener(...)` added; `_handle_tx` fans out to queues **and**
  listeners, preserving the existing length guard and `_command` timeout semantics.
- Internal `_motion_stream(on_packet)` async context manager owns one long-lived
  `_RingConnection`: connect, register listeners, send RAW-on, yield; on exit (every exit path,
  including an exception in the body) cancel the watchdog, unregister and send RAW-off. Listeners
  are registered for **both** `CMD_RAW_MODE` and `CMD_CAMERA_GESTURE`, so one session sees the
  samples and the ring's own gestures.
- Public `async_stream_motion(on_packet, should_run, on_session)` runs the session in a *loop*: it
  hands the fresh `MotionSession` to `on_session` (where the manager arms auto-off, the rate probe
  and the camera-gesture enable), awaits `session.wait_stopped()`, and reconnects after
  `_STREAM_RETRY_S = 10` s if the session died by itself (`disconnected` / `silent`) while
  `should_run()` is still true. A user-requested stop is final.
- `MotionSession` is how a stop is requested from outside the task: `request_stop(reason)` sets an
  `asyncio.Event` that the runner awaits, so `async_stop` needs no cancellation.
- Silence detection lives in the connection: the listener calls `_RingConnection.touch_motion()` on
  every packet, and `_watch_stream` (every `_STREAM_WATCHDOG_S = 5.0`) ends the session if the link
  `dead` flag is set or nothing arrived for `MOTION_STREAM_SILENCE_TIMEOUT_S = 30.0`.
- A stream that cannot be opened **returns** `None` instead of raising, so the switch lands on `off`
  with one warning line rather than an exception out of a service call.
- **Connection contention - DECIDED: pause polling.** The ring serves one central and today
  every poll opens its own connection ([`_RingConnection`](../custom_components/colmi_ring/api.py)
  connects and disconnects per call), so a long-lived stream and a poll cannot coexist. While a
  stream is live the coordinator performs **no** BLE I/O, and resumes the moment the stream
  closes.
- **How the pause is implemented**: gate the top of
  [`_async_update_data`](../custom_components/colmi_ring/coordinator.py) on
  `MotionManager.is_streaming` and return the cached `self.data` unchanged. Prefer this over
  `async_shutdown()` (which has no supported restart) - the refresh timer keeps firing, each
  tick just costs nothing, and `_apply_poll_interval` state survives untouched.
- **Side effects of pausing, all handled:**
  - Heart rate / steps / battery go **frozen, not `unavailable`**, because the coordinator keeps
    succeeding with cached data and
    [`_heart_rate_is_stale`](../custom_components/colmi_ring/coordinator.py) never runs. On
    stream *start*, push a cleared HR (`heart_rate = None`, `heart_rate_timestamp = None`) via
    `async_set_updated_data` so the sensor reads `unknown` rather than lying to automations; on
    stream *stop*, call `coordinator.async_refresh()` so it recovers immediately instead of on
    the next scheduled tick.
  - Backfill only runs from `_async_update_data`, so it pauses too - harmless, since the
    scanned-days design is idempotent and re-reads those days on the next successful poll.
  - Every other call site that opens a connection must refuse while streaming: the SpO2 and
    on-demand heart-rate **buttons** ([`button.py`](../custom_components/colmi_ring/button.py)),
    blink / reboot, and the HR-interval **number** write
    ([`number.py`](../custom_components/colmi_ring/number.py)). Raise
    `HomeAssistantError("Unavailable while the motion stream is on")` so the UI shows a clean
    failure instead of contending for the link.
  - Stream start/stop flips a flag on the coordinator **before** the connect/disconnect, so a
    poll already in flight finishes (its `finally` disconnects) rather than racing the stream.
- **Robustness**: as above, and the packet path is **callback-safe by construction** -
  `MotionManager._on_packet` never awaits, and every gesture listener is wrapped so a raising
  entity cannot break the stream or the other listeners. Unparsable notifications are logged at
  DEBUG with their hex (which is how a wrong decode assumption becomes visible) and dropped.
- `MotionManager.async_shutdown()` runs on unload/reload *before* the platforms unload, so a live
  stream is never left holding a connection nobody owns: it requests the stop, waits 10 s, and
  cancels only if the task refuses to finish.

## Phase 3 - Gesture detection: `gestures.py` (+ `motion_manager.py`)

*As built.* The detector is the HA-free [`gestures.py`](../custom_components/colmi_ring/gestures.py)
`GestureDetector`; [`motion_manager.py`](../custom_components/colmi_ring/motion_manager.py) owns the
session, feeds it packets and publishes what comes back. (`colmi_codec/motion.py` is the codec only -
the original plan put the detector there.)

Every threshold is a **best guess pending the Phase 0 probe**, and every guess is a named `MOTION_*`
constant in [`const.py`](../custom_components/colmi_ring/const.py), so the probe corrects them in one
place. The detector is **rebuilt per session**: the gravity reference and the rate estimate belong to
one continuous connection, not to the integration's uptime.

- **Gravity is a complementary filter of the raw vector.** A slow EMA
  (`MOTION_GRAVITY_TAU_S = 3.0`) so a gesture cannot tilt the reference frame, converging *fast*
  (`MOTION_GRAVITY_FAST_TAU_S = 0.08`) whenever `|a|` sits within `MOTION_GRAVITY_TRUST_G = 0.2` of
  1 g - a sample whose magnitude is ~1 g is by definition not accelerating, so it *is* the gravity
  direction. Without that fast path, a wrist that changes pose while the slow estimate still points
  the old way (a stream resuming after a gap, or dropped samples) reads as hundreds of ms of slow
  downward acceleration and fires a phantom `hand_lower`.
- **Raise / Lower - a movement state machine judged on its closing edge.** The gravity-projected
  linear acceleration feeds a leaky integrator (`MOTION_VELOCITY_LEAK_TAU_S = 0.45`); positive
  velocity is `hand_raise`, negative is `hand_lower`. A movement opens when `|v|` crosses the
  threshold and is judged only once `|v|` falls back under `threshold * 0.3`, so both the peak
  velocity and the total heading swing are known by then. It is accepted only if:
  - it reached its peak within `MOTION_RAISE_MAX_PEAK_S = 0.6` of the last moment the hand was
    still - a *rapid* movement is defined by its snap, not its total impulse (otherwise a steady
    two-second push eventually accumulates enough leaked velocity to qualify); and
  - the gravity direction swung by at least `MOTION_RAISE_PITCH_DEG = 15` degrees between the
    movement's open and close (`_swung_during`, headings averaged over
    `MOTION_PITCH_RECENT_WINDOW_S = 0.25`), so a straight up-and-down bounce cannot pass.
- **Clap - four independent tests on the raw `magnitude_g`,** because a vibration (shaking hand,
  mower, steering wheel) produces a machine-gun of individually well-shaped impulses:
  1. **Rise**: from `< 0.4 x MOTION_CLAP_SPIKE_G` to the spike within `MOTION_CLAP_RISE_S = 0.15`.
  2. **Width**: at most `MOTION_CLAP_MAX_WIDTH_S = 0.25` - a clench or a swung arm sustains
     acceleration for hundreds of ms; a clap impulse is tens of ms.
  3. **Prior stillness**: `MOTION_CLAP_QUIET_S = 0.4` of `|a|` within
     `MOTION_CLAP_QUIET_BAND_G = 0.15` of 1 g. Measured on the **raw magnitude**, never on the
     gravity-projected accel, which crosses zero twice per vibration cycle and so cannot say
     "this hand is moving".
  4. **Ring-down**: after the impulse the motion must fall to `MOTION_CLAP_RING_DOWN_FRACTION =
     0.25` of *the impulse's own peak* and stay there `MOTION_CLAP_SETTLE_S = 0.2`, reaching it
     within `MOTION_CLAP_RING_DOWN_S = 0.6`. This is the test that separates a clap from the first
     half-cycle of a shake (with prior stillness the two have near-identical shapes) - a clap is
     followed by a coasting hand, a shake by more shaking. Relative rather than absolute because
     the counter-stroke of a real clap is *part of* the clap; the deadline matters because a shake
     keeps interrupting the ring-down until its very last half-cycle settles and would then be
     awarded a gesture a beat after the shaking stopped. The candidate is held **pending** until it
     rings down, costing that much latency.
- **The ring's own clap is used too, not instead.** On session open the manager best-effort sends
  the camera-gesture enable (`02 04`; firmware that lacks it simply answers nothing) and reports any
  gesture packet as `source=ring`, after a `MOTION_STARTUP_MUTE_S = 2.0` mute, because the ring
  echoes our own enable packet back at us. The stream-derived clap (`source=stream`) is disabled by
  `MotionManager._async_check_rate` if the measured rate is under `MOTION_MIN_HZ_FOR_STREAM_CLAP =
  15`, with a single INFO line naming the measured Hz - raise/lower still work, and only the
  weakest-evidence detector is dropped.
- **Design rule: sensitivity scales magnitudes only.** `GESTURE_SENSITIVITY_SCALE`
  (`low 0.6 / medium 1.0 / high 1.6`) is a *divisor* on the base thresholds, so a bigger number is
  more sensitive. The shape tests (rise, width, ring-down) and the orientation swing are
  deliberately **not** scaled: relaxing them on "high" would delete the very discrimination
  automations rely on.
- **Cooldowns**: `MOTION_GESTURE_COOLDOWN_S = 1.2` between reports of the same gesture,
  `MOTION_CLAP_REFRACTORY_S = 0.6` between claps (two claps in a row are a deliberate double clap),
  and `MOTION_GESTURE_SUPPRESS_S = 0.5` of mutual suppression, since a hard raise also looks like a
  clap.
- **Tuning aid**: `GestureEvent.as_event_data()` carries `gesture`, `source`, `peak_g` and
  `velocity_g_s`, so the event log shows what a real gesture actually scored against these guesses.

## Phase 4 - Home Assistant surface ✅ built

- **[`event.py`](../custom_components/colmi_ring/event.py)** - three `EventEntity`s (`Clap`, `Hand
  Raise`, `Hand Lower`) whose entity event type *is* the gesture key, so each can be an automation
  trigger on its own and gets free `last_triggered` history in the UI. Unique ids
  `{entry_id}_gesture_{key}`; children of the ring device. **In addition**, `MotionManager._publish`
  fires the `colmi_ring_gesture` event directly on the bus with
  `{gesture, source, peak_g, velocity_g_s, entry_id}` - deliberately *not* from an entity, so it
  still arrives when the event entities are disabled, and one trigger catches all three gestures
  regardless of how the event integration names its generated events.
- **[`switch.py`](../custom_components/colmi_ring/switch.py)** - `sensor/switch.colmi_ring_motion_stream`.
  `is_on` reads `MotionManager.is_streaming`, and the entity subscribes to `add_state_listener`, so
  the switch always follows reality: a failed open, an auto-off, or a reload all settle it back to
  `off` instead of lying. `icon: mdi:motion-sensor`; deliberately **not**
  `entity_category=CONFIG` (it is an operational toggle automations may call). A connect failure is
  not raised from `async_turn_on` - the client returns, the manager clears the flag, and the log
  gets one warning.
- **Accelerometer sensors** ([`sensor.py`](../custom_components/colmi_ring/sensor.py), same
  `CONF_MOTION_ENABLED` gate) - four `SensorEntity`s that mirror the raw sample instead of
  interpreting it: `Acceleration X`, `Acceleration Y`, `Acceleration Z` (unique ids
  `{entry_id}_accel_{x,y,z}`) plus `Acceleration` = the vector magnitude, all in `g` with
  `suggested_display_precision: 3`. No device class / unit enum on purpose: the codec's unit really is
  just `g`, and `SensorDeviceClass.ACCELERATION` is newer than the oldest core this integration has run
  on, so importing it would risk taking the *whole* sensor platform down for a cosmetic win.
  Push-driven, not polled: `MotionManager.last_sample` holds the newest `AccelSample`, and
  `add_sample_listener` callbacks tell the entities to re-read it, so the same callback also serves the
  sample *going away* (`_set_streaming(False)` clears it) - the sensors read `unknown` between sessions
  rather than freezing on a stale reading. Publishing is throttled to `MOTION_SENSOR_UPDATE_S` (1 s),
  because the stream runs at tens of Hz and every state change is a recorder row; the detector still
  sees every sample, and the full-rate signal remains available via the gesture events and
  `motion_log_samples`.
- **Stream-state plumbing** - `MotionManager` is **always constructed** in
  [`__init__.py`](../custom_components/colmi_ring/__init__.py) next to `BackfillManager` and stored
  on `runtime_data` *and* on the coordinator, because the coordinator has to be able to ask whether
  the stream holds the link even in setups where the option is off. It does no I/O until asked.
  `async_start` flips `is_streaming` and calls `coordinator.async_clear_heart_rate()` *before*
  connecting, so no poll can race onto the link and a frozen heart rate can never look fresh; when
  the stream task returns, `async_notify_stream_finished()` clears the flag and requests a refresh.
- **Auto-off guard** - the `motion_stream_timeout` **option** (default 30 min, `0` = stay on until
  switched off), rearmed by `async_call_later` on session open and on *every gesture*, so an active
  user is never cut off mid-session. Turning the stream off from the UI cancels it.
- **Options flow** ([`config_flow.py`](../custom_components/colmi_ring/config_flow.py)) -
  `CONF_MOTION_ENABLED` (creates the switch and the event entities at all; default **off**),
  `gesture_sensitivity` select (labelled *Low (only clear movements)* / *Medium* / *High (also
  slight movements)*), `motion_stream_timeout` number (0-480 min), `motion_log_samples` boolean
  (DEBUG-log every decoded sample, for tuning against a real wrist). `translations/en.json` carries
  the option labels and help text, including a plain statement that normal polling stops while the
  stream is on.
- **Wiring** - `const.py` constants; `PLATFORMS` gains `Platform.SWITCH` and `Platform.EVENT` (both
  platforms return early when `CONF_MOTION_ENABLED` is off, so nothing appears in a normal setup);
  `MotionManager` owns client + detector + session + timers, while the coordinator remains the sole
  owner of connection lifetime for everything else. Every other connection-opening call site refuses
  with `STREAM_BUSY_MESSAGE` via `ColmiRingEntity._assert_ring_free()`.

## Phase 5 - Validation

### Offline ✅ (this is what built the detector)

`homeassistant` is not importable on the dev host, so the pure modules are exercised directly by
two dev-only scripts that stub the HA packages out of `sys.modules`:

- [`scripts/_motion_synth.py`](../scripts/_motion_synth.py) generates physically plausible streams
  and replays them: `--scenario gestures` (still, clap, snap raise, snap lower) and `--scenario
  negatives` (a sustained clench, a 9 Hz shake, a slow two-second lift, and a walking bounce -
  movements that must *not* fire). `--out FILE` also writes CSV.
- [`scripts/_motion_replay.py`](../scripts/_motion_replay.py) replays a CSV or a probe capture
  through the real `parse_accel_packet` + `GestureDetector`, prints the signal summary and the
  gesture timeline, and can plot the stream when matplotlib is available.

Measured, at sensitivities 0.6 / 1.0 / 1.6:

| Scenario | Result |
|---|---|
| `gestures` | exactly 3 events: `clap`, `hand_raise`, `hand_lower` (e.g. `clap @2.44 s peak=2.37 g`, `hand_raise @4.7 s v=+0.30 g*s`, `hand_lower @7.4 s v=-0.34 g*s`) |
| `negatives` | **0 events at all three sensitivities** |
| CSV round-trip | identical timelines through `_motion_replay.py`, so the codec path is covered too |

Tuning that only this harness found (each one a real false positive or false negative): the
complementary gravity filter, stillness measured on the raw magnitude instead of the projected
accel, ring-down measured relative to the impulse's own peak rather than absolutely, zeroing the
"preceded by stillness" timer on every moving sample, the ring-down deadline, and removing the
sensitivity scaling from the orientation test.

- Static: `python -m compileall -q custom_components\colmi_ring`; `python -m ruff check
  custom_components\colmi_ring scripts\_motion_replay.py scripts\_motion_synth.py --select
  F,E9,B,PLR --ignore PLR2004` (repo baseline - `RUF` is *not* part of it, and adding it surfaces 26
  pre-existing findings).

### On-device ⏳ (needs the ring)

- Gestures fire with acceptable latency; the HR sensor reads `unknown` (not a stale value) while
  streaming and recovers within a second of the switch turning off.
- The SpO2/HR buttons, blink/reboot and the interval number fail with `STREAM_BUSY_MESSAGE` during
  a session.
- Walking out of range ends the session with a one-line warning and an `off` switch; the reconnect
  loop retries while the switch is still on; auto-off fires.
- The rate probe reports the measured Hz *and* the median at-rest magnitude, confirming or replacing
  `MOTION_MIN_HZ_FOR_STREAM_CLAP` and the +/-4 g scale; **measure and record battery drain per
  streaming hour here**.
- The four accelerometer sensors move about once a second while streaming, sit near `1.0 g` on the
  "up" axis at rest, and return to `unknown` when the switch turns off. If they show a constant
  nonsense value while streaming, the decode is wrong (same failure as the magnitude probe), not the
  sensor plumbing. Also check the recorder's growth rate while streaming to confirm 1 s is a sane
  publish cadence.
- Confirm the post-stream poll re-imports nothing twice (the scanned-days set is idempotent).
- Then fix/extend `scripts/_accel_probe.py` and correct whatever it disproves, in `const.py` and in
  the **Status** table above.

#### Before the next on-device run

- Enable DEBUG on `custom_components.colmi_ring` (the root logger; see the note above - there is no
  separate `.api` logger). The command byte is decided in `api.py::_handle_tx`, so if the R02 serves
  accel somewhere other than `0xA1` the proof is api's "Unexpected packet type" line (which now
  carries the hex byte and full payload), and it is invisible at INFO. One session with the logger on
  settles the only assumption in `colmi_codec/motion.py` that the reference app could not confirm.
- Run `python scripts/_motion_ref_parity.py` first: it takes a second, and if it fails then the
  decode has drifted from the reference and no ring result should be interpreted until it passes.

#### First on-device run (2026-10-29, firmware as-shipped, via the ESP32-C3 proxy)

- A session opened and ended ~2 s later with **no** rate-probe line and no gesture, i.e. it died
  before the 5 s probe. Nothing in the log said why: `_on_session` logged nothing and the end
  reason was DEBUG-only. Both are now INFO ("motion stream open…" / "motion stream ended
  (reason)"), so the next run will show where the time went.
- The post-stream coordinator refresh was silently dropped (`coroutine … was never awaited`,
  fixed by `MotionManager._request_refresh`), so HR stayed stale for up to a full poll interval
  after the switch went off.
- During an ordinary poll the ring sent command bytes `47` (0x2F) and `1` (0x01, the set-time ACK)
  that no parser claims. Harmless, but 0x2F is undocumented here: the "Unexpected packet type"
  debug line now includes the hex command and the full payload so an unexpected *stream* packet is
  identifiable (e.g. accel data served on a command byte other than the guessed `0xA1`).
- **No accelerometer packet has ever been seen.** Both runs show the stream opening and dying with
  zero `0xA1` traffic, so the "which command byte carries accel" question above is still open.

#### Second on-device run (2026-10-29): the switch toggled itself back off

- The log was unambiguous once read as a link problem rather than a decode problem:
  `motion stream open; waiting for accelerometer data` at `12:38:05.222`, `disconnected` at
  `12:38:05.283` - **61 ms**. Immediately before it, `Manually updated colmi_ring data`: that is
  `MotionManager._request_refresh`, i.e. the refresh the *previous* stream session asked for.
- Root cause: the ring answers **one central**, and two live GATT connections do not queue - one
  link gets dropped. The `is_streaming` gate in `coordinator._async_update_data` only stops polls
  that *start* after the flag flips; a poll already inside `async_get_data` still held its own
  connection while the stream connected beside it, and the ring took the stream's link. The same
  collision explains the first run's ~2 s session.
- Fix: `ColmiRingApiClient` now owns one `asyncio.Lock`, and every `_RingConnection` holds it for
  its whole life, so there is exactly one connection to a ring at a time. The wait budgets are
  deliberately asymmetric - a poll waits `POLL_LOCK_WAIT_S` (5 s) and then fails with
  `ColmiRingApiClientBusyError`, which the coordinator treats as *not a failure* (keep the last
  known values, do not mark the entity unavailable); a UI command waits `USER_LOCK_WAIT_S` (15 s);
  the stream waits `STREAM_LOCK_WAIT_S` (90 s), because the collision it hits is an ordinary
  seconds-long poll that is worth waiting out rather than refusing.
- The lock is claimed by polling `asyncio.Lock.locked()` rather than wrapping `acquire()` in a
  timeout: cancelling an acquire queued on the lock can leave it **held forever**, which would
  brick every later session. A free lock is claimed synchronously by the next `acquire()`, so
  check-then-acquire has no interleaving window.
- Also from this run: `_watch_stream` wakes on the connection's own `dead` event instead of
  sleeping a blind 5 s (a dropped link is noticed at once), and the reason a stream could not open
  is now logged at WARNING in `api.async_stream_motion` - previously DEBUG-only, and the manager's
  line only says "see the log above".

#### Third on-device run (2026-10-29): the lock did not fix it, and the poll is innocent

    14:42:51.634 INFO  motion stream open; waiting for accelerometer data
    14:42:51.663 DEBUG Colmi ring 78:14:12:A0:0F:8C disconnected
    14:42:52.770 DEBUG Unexpected packet type 47 (0x2F) from ring: 2ff10000000000000000000000000020
    14:42:52.770 DEBUG Unexpected packet type 1 (0x01) from ring: 01000100220000000001003001003086
    14:42:53.428 DEBUG Colmi ring 78:14:12:A0:0F:8C disconnected
    14:42:53.429 DEBUG Finished fetching colmi_ring data in 1.765 seconds (success: True)

- Still dying, now **29 ms** after opening, and still with zero `0xA1` traffic.
- **The poll-collision theory is disproved by this log.** The two unclaimed packets *are* ours:
  `0x01` is `CMD_SET_TIME` (the poll's own clock sync ACK) and `0x2F` is the still-undocumented
  notification from run 1. They belong to the **poll** connection, whose handler table holds only
  battery / HR log / HR settings / real-time / steps. And that poll finished at `53.429` after
  `1.765 s`, so it *started* around `51.664` - **1 ms after** the stream link was already gone at
  `51.663`. It could only acquire the client-wide lock because the stream had died and released it.
  The serialisation from the previous run is working; the poll is the *victim* of the ordering, not
  its cause. (Confirmed independently by the user: the failure is far too reliable to be a
  coincidental overlap with a poll that runs every few minutes.)
- What is left, in the 29 ms between "stream open" and "disconnected", is **our own writes**.
  `_motion_stream` sends `A1 04 04` (RAW on) and then `MotionManager._on_session` immediately fires
  `set_camera_gesture(True)` - `02 04`. 29 ms is about one BLE round trip, so one of those two
  writes is the prime suspect, and a write is a deterministic cause, which matches the reliability.
- Changes made for the next run, on the assumption that the *next* log has to be self-explanatory:
  - `_on_disconnect` now names the session purpose and the link's age, and says who closed it
    (`closing` flag set by `_safe_disconnect`): `Colmi ring … motion stream session disconnected
    (after 0.029s, DROPPED BY PEER)`. Previously the stream's death and an ordinary poll's end
    printed the identical line.
  - The RAW start packet and the camera-gesture packet are logged as hex as they are written.
  - `_StreamTally` counts **every** notification on the link (`_RingConnection.observer`, wired in
    before connecting), not just the command bytes we listen for. That distinction matters: packets
    arriving on a byte other than `0xA1` means the *codec guess* is wrong, while nothing at all
    means the firmware never answered. `_log_stream_session` reports the split at session close, at
    INFO unless real `0xA1` packets arrived.
  - "Unexpected packet type" now names the session that received it.
- Behaviour changes, per the decision to treat on-device gesture detection as non-critical while the
  stream is unstable:
  - `const.ENABLE_RING_CAMERA_GESTURE = False`: `_on_session` no longer writes `02 04`, so the
    RAW-stream command is the only thing written to the ring for the duration of a session. Nothing
    is lost but the ring's own clap reporting; raise/lower and the stream-side clap come from the
    accelerometer data. Re-enabling is one constant.
  - `async_stream_motion` no longer returns `None` at the first failure. It retries with a doubling
    delay (`_STREAM_RETRY_MIN_S` 1 s up to `_STREAM_RETRY_MAX_S` 30 s) for `_STREAM_GIVEUP_S` 180 s,
    resetting the budget whenever a session opens at all. The budget is bounded because the manager
    still pauses polling while it burns, so an unworkable stream must still end with polling
    resumed and the switch off.
- What the next run decides:
  - `DROPPED BY PEER … after 0.0xxs` with **no** notification at all -> the ring refuses, or cannot
    survive, the `A1 04 04` write itself. Next experiment: open the session and send *nothing*, and
    see whether the link survives (i.e. whether R02 streams unprompted, as `scripts/_accel_probe.py`
    suspects from its passive phase).
  - Notifications counted on a byte other than `0xA1` -> the connection is fine and
    `colmi_codec/motion.py` has the wrong command byte; fix the codec, not the transport.
  - A session that now survives -> the camera-gesture write was the killer, and it can be re-armed
    later behind a delay.

#### Fourth on-device run (2026-10-29): the ring is innocent — we close the link ourselves

    16:08:44.567 DEBUG RAW-stream start packet: a10404000000000000000000000000a9
    16:08:44.570 INFO  motion stream open; waiting for accelerometer data
    16:08:44.572 INFO  motion stream closed after 0.0s (stopped) with 0 notification(s) ...
    16:08:44.593 DEBUG motion stream session disconnected (after 0.025s, closed by us)
    16:08:45.592 DEBUG Unexpected packet type 47 (0x2F) ... (poll session)
    16:08:46.323 DEBUG Finished fetching colmi_ring data in 1.730 seconds (success: True)

- The three decision lines from run 3 all came back negative at once: the disconnect says
  **`closed by us`** (the `closing` flag was set, i.e. our own `_safe_disconnect` ended the link),
  the close line reports **`(stopped)`**, and the `0xA1` write went out cleanly. The ring never
  dropped anything.
- `(stopped)` is the tell: that line prints `session.reason or "stopped"`, so `reason` was
  **`None`** — nobody ever called `request_stop`. Not the user, not auto-off, not shutdown, not the
  watchdog. The session did not end; it was **unwound**.
- Corroboration: all three of `MotionManager._run`'s completion lines (the WARNING "could not be
  opened", the INFO "ended (%s)", the DEBUG "finished (user)") are **absent**, so `_run` never
  returned normally — an exception escaped it, or its task was cancelled. HA writes task exceptions
  under a `homeassistant.*` logger, which a log filtered to `custom_components.colmi_ring` cannot
  show. That is why the teardown looked silent.
- Timing: INFO at `44.570` → close at `44.572` is **2 ms** — pure synchronous code, no BLE round
  trip. The raise happens after `_on_session`'s own INFO line, or is a cancellation inside
  `wait_stopped()`. Suspects that survived inspection: `GestureDetector.reset()` (cleared —
  `__init__` initialises everything `reset()` touches), `_schedule_auto_off` / `_timeout_minutes` /
  `hass.async_call_later` (not cleared statically).
- The poll at `45.592` is again a *consequence*: the abort runs `async_notify_stream_finished()`,
  which forces the switch off (the "self-disabling" symptom) and calls `_request_refresh()`. Second
  independent confirmation that polling is downstream of the failure, not its cause.
- Changes made, so the next log names the fault instead of describing its silhouette:
  - `_log_stream_session` prints **`ABORTED (never stopped by anyone)`** instead of `(stopped)` when
    no reason was ever set.
  - `async_stream_motion` gained `except asyncio.CancelledError` (WARNING naming the cancellation,
    then re-raise) and a catch-all `except Exception` (full traceback on the first attempt, one
    line per retry, then the same bounded back-off). Both land on *this* logger.
  - `MotionManager._run` catches `Exception` and logs `Colmi ring motion stream task failed` with
    traceback, so nothing hides under `homeassistant.tasks` any more.
  - The arming callback now runs inside `_arm_session`, which logs
    `"arming callback raised; closing the session"` with traceback before re-raising — a
    manager-side fault is attributed to the manager instead of looking like a ring rejection, and a
    half-armed session is not left holding the ring.
  - The retry budget no longer resets just because a session *opened*: only a session that lived
    `_STREAM_STABLE_S` (10 s) resets it (`_session_lived`). Before, a fault that reproduced every
    attempt at ~2 ms reset the budget forever and retried at the 1 s minimum indefinitely, with the
    switch on and polling paused.
- What the next run decides:
  - A traceback from `arming callback raised` -> the fault is in `_on_session`; fix the callback.
  - `motion stream task was cancelled` -> find the canceller (reload/shutdown path or a
    `hass.async_create_task` ownership quirk).
  - `ABORTED` with neither of the above -> the unwind is inside the transport; the traceback names
    the frame.
  - A session that stays open past 10 s -> the fault was in the previous build's arming path, and
    the run proceeds to the real Phase 5 questions: sample rate, scale, axis signs.

#### Fifth on-device run (2026-10-29): root cause — a Home Assistant API we never had

    17:21:54.541 DEBUG RAW-stream start packet: a10404000000000000000000000000a9
    17:21:54.542 INFO  Colmi ring motion stream open; waiting for accelerometer data
    17:21:54.542 ERROR Colmi ring motion stream arming callback raised; closing the session
      File ".../api.py", line 1308, in _arm_session          on_session(session)
      File ".../motion_manager.py", line 267, in _on_session  self._schedule_auto_off()
      File ".../motion_manager.py", line 439, in _schedule_auto_off
        self._timer = self._hass.async_call_later(
    AttributeError: 'HomeAssistant' object has no attribute 'async_call_later'
    17:21:54.545 INFO  motion stream closed after 0.0s (ABORTED (never stopped by anyone)) with 0 notification(s) ...: none
    17:21:54.609 DEBUG motion stream session disconnected (after 0.068s, closed by us)
    17:21:54.610 ERROR motion stream aborted while the session was open; closing it and retrying
    17:21:55.839 / 17:21:58.244 / 17:22:03.826 / 17:22:12.667  ERROR ... aborted again (same traceback)
    17:21:58.298 DEBUG Unexpected packet type 161 (0xA1) from ring during motion stream session:
              a101000000d2009800b00100000000bd
    17:21:58.298 DEBUG Unexpected packet type 161 (0xA1) from ring during motion stream session:
              a10200002df72ced010a0100000000ec

- Run 4's decision table paid off on the first redeploy: the **`arming callback raised`** branch,
  with the traceback naming the exact frame. `HomeAssistant` has no `async_call_later`; the timer
  helper is `homeassistant.helpers.event.async_call_later(hass, delay, action)`, which returns the
  unsubscribe callable that `self._timer: Callable[[], None]` already expects. Both call sites
  (`_schedule_auto_off`, and the rate probe in `_on_session` that would have failed one line later)
  now use the helper. This is the whole bug: an `AttributeError` 0–2 ms after every session opened,
  unwinding a healthy transport, switching the entity off and triggering the post-mortem poll.
- Every mechanism added in run 4 behaved as designed — `_arm_session` attributed the fault to the
  manager instead of a ring rejection, `ABORTED (never stopped by anyone)` named the unwind, and the
  retry budget escalated 1 s → 2 s → 5 s → 9 s instead of resetting forever on a 2 ms session.
- **The ring does answer `A1 04 04`.** Two `0xA1` notifications arrived 54 ms after the start write
  of the 17:21:58 attempt — after that session had already been aborted and its listeners removed,
  which is exactly why the close line still reported `0 notification(s)` and a tally of `none`. The
  "R02 may refuse the RAW command" hypothesis is dead.
- Both frames are **checksum-valid** (`sum(packet[:15]) & 0xFF` equals the trailing byte: `0xBD`,
  `0xEC`; our own `A1 04 04` checks out at `0xA9`). They are well-formed protocol replies, not noise
  or truncation.
- But their subtypes are `01` and `02`, and `parse_accel_packet` only accepts `TYPE_POSITION = 3`.
  What the current codec derives from them (measured, not guessed):
  - `0xA1/1`: `x +0.016 g, y 0.000 g, z +0.004 g`, `|v| 0.016 g`, and only offsets 5, 7, 9, 10 are
    non-zero — an acknowledgement, not a sample.
  - `0xA1/2`: `x +1.400 g, y 0.000 g, z +1.420 g`, `|v| 1.994 g`, non-zero from offset 4 to 10.
    `02` is our own `SUB_RAW_OFF`, so a reply is the natural reading.
  - The suggestive part: a brute-force search over every axis offset triplet, both nibble
    conventions and full scales of 2/4/8/16 g finds **no** layout that makes `0xA1/1` look like a
    resting ring (closest `|v|` = 1.249 g), but `0xA1/2` lands on **0.9975 g** if the full scale were
    **2 g** instead of 4 g (offsets 4, 6, 8; the high-nibble variant gives 1.0034 g at offsets
    2, 4, 6). One frame at one magnitude is not evidence — but it is the single concrete
    alternative-layout lead, so keep it in mind if the next run shows a real stream that decodes to
    ~2 g instead of ~1 g.
  Neither frame is the stream (a stream is many frames per second, and the tally would show it), so
  the sample-rate question stays open; guessing a new layout from two frames would be unfounded.
- Changes, all in `motion_manager.py`:
  - `from homeassistant.helpers.event import async_call_later`, and both timers call it with
    `self._hass` as their first argument.
  - Undecodable frames are now named by the manager itself instead of only appearing as api's DEBUG
    "Unexpected packet type" line (which fires for *every* RAW frame, since RAW registers no one-shot
    handler). `_log_unknown_packet` logs the command/subtype pair and one example payload once per
    kind per session, capped at `_MAX_UNKNOWN_KINDS`, and `_async_check_rate` appends the kinds it
    saw when it finds no sample — so a session that survives reports the exact pair to teach the
    codec, at WARNING, without needing DEBUG logging enabled.
- What the next run decides:
  - `motion stream: N Hz over M samples, median magnitude ~1 g` -> the decode is right; go tune
    thresholds against real data (the actual Phase 5 goal).
  - `no accelerometer sample in 5 seconds (received 0xA1/1, 0xA1/2 instead)` plus the WARNING
    payloads -> the ring replies but streams elsewhere or in another layout; fix
    `colmi_codec/motion.py` from those frames.
  - A magnitude warning (median far from 1 g while samples do decode) -> scale or bit-packing is
    wrong, not the gesture logic. If the median sits near **2 g**, that is the 2 g-full-scale lead
    above confirmed, and the fix is one constant in `colmi_codec/motion.py`.
  - A session that lives past 10 s and then ends on its own -> a transport question again, and by
    now the log would say which.

## Risks

| Risk | Mitigation |
|---|---|
| Stock firmware streams too slowly for a stream-derived clap | rate probe disables *only* the stream clap and says so once, and raise/lower work at any rate. (The on-device camera gesture `02 04` *would* be the fallback, but it is switched off - `ENABLE_RING_CAMERA_GESTURE` - because that write is the leading suspect for killing the link; see the third run above.) |
| Low-nibble decode assumption is wrong | rest magnitude will be nowhere near 1 g, which the replay script shows immediately; unparsable/odd packets are logged with hex |
| Axis order / sign misread (wire order is Y, Z, X) | largely moot: velocity is projected onto the *estimated gravity vector*, so raise/lower are correct for any axis mapping; only scale/nibble can break it wholesale |
| Ring will not serve commands while streaming | **decided:** coordinator pauses polling for the session; HR reads `unknown`, other call sites refuse with `STREAM_BUSY_MESSAGE` |
| Frozen HR looks like working data while streaming | stream start clears `heart_rate` before connecting, so the sensor goes `unknown`; stream end triggers an immediate refresh |
| Continuous streaming drains battery | opt-in option (default off) *and* opt-in switch, auto-off rearmed per gesture, stream stops on unload/reload |
| False positives from walking, typing or a vibrating hand | the four clap tests (rise / width / prior stillness / ring-down) and the two movement tests (peak timing / orientation swing), all validated against a scripted must-not-fire scenario |
| Detection logic is tuned against simulation, not the real ring | every threshold is a named `MOTION_*` constant; `peak_g`/`velocity_g_s` ride along in the event data so real gestures can be compared to the guesses |
| A wrong guess makes the feature silently useless rather than broken | the Status table lists how each assumption fails, and each failure mode is visible in the log (`unknown stream packet`, the rate line, the gesture debug line) |

## Data flow

```mermaid
flowchart LR
  R[Ring accelerometer] -->|RAW notify 16B| H[_handle_tx fan-out]
  H --> Q[per-command queues]
  H --> L[motion listeners: RAW + camera gesture]
  R -->|camera gesture packet| H
  L --> P[MotionManager on_packet]
  P -->|accel sample| D[GestureDetector feed]
  P -->|gesture packet| M
  D -->|GestureEvent| M[MotionManager]
  M -->|async_fire colmi_ring_gesture| F[HA event bus]
  M -->|event listeners| E[gesture event entities]
  S[switch entity] -->|start/stop| M
  S -.->|state listener| M
  M -->|is_streaming gate| C[DataUpdateCoordinator]
  C -->|skips polls while streaming| K[_RingConnection]
  M -->|async_stream_motion session| K
  K --> R
  M -->|auto-off timer| M
```
