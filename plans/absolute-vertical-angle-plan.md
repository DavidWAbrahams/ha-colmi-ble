# Plan: Absolute Vertical Angle sensor

## Goal

`sensor.<ring>_vertical_angle` currently reports how far the ring has tilted
**from the pose it was zeroed in** (relative, 0..180, requires zeroing via
auto-capture or the Zero Vertical Angle button).

Change it to an **absolute** measure: how far the ring's **Z axis** (the axis
through the ring hole) is deflected from the horizontal plane.

- Z axis nearly perpendicular to gravity (`|z| ≈ 0 g`) → ring hole axis is
  horizontal → sensor reads **≈ 0°**.
- Z axis along gravity (`|z| ≈ 1 g`, either sign) → sensor reads **≈ 90°**.
- Always in **0..90°**: folding via `abs(z)` makes the ±1 g cases equivalent
  and yaw rotation in the horizontal plane invisible (it only changes x/y).

## The math

With the existing low-passed gravity direction `pose` (a unit vector in the
ring's own frame, already computed by the tilt code):

```
vertical_angle_deg = degrees(asin(clamp(abs(pose.z), 0, 1)))
```

- `asin` gives the elevation of the Z axis above the horizontal plane,
  exactly the "deflection from horizontal" requested.
- `abs()` handles the ring being flipped end-for-end (±1 g both read 90°).
- Yaw about gravity does not change `z`, so horizontal-plane rotation cannot
  move the reading. This is inherent, not a filter.
- Clamping guards `asin` against a filtered vector whose z rounds past 1.

No zeroing, no calibration, nothing integrated → cannot drift, and needs no
"first still pose" capture.

## Design decisions

1. **Replace `TiltTracker` with `VerticalAngleTracker`** in `gestures.py`
   rather than adding a parallel class. The tilt tracker has exactly one
   consumer (the Vertical Angle sensor via `MotionManager`); the gesture
   detector keeps its own private gravity estimate and is untouched.
2. **Delete the zeroing machinery**: auto-zero on first still pose, the
   stillness window, and the `Zero Vertical Angle` button. An absolute sensor
   has no meaningful "zero" to set. (Note in the PR/README: the old
   `zero_vertical_angle` button entity will appear as an orphaned "missing
   device" entity after upgrade until removed — HA does not auto-purge removed
   entities of a still-loaded config entry.)
3. **Keep the parts of `TiltTracker` that are still needed**:
   - the slerp low-pass of the unit gravity vector (`_update_pose`, `_slerp`,
     `MOTION_TILT_TAU_S` → rename to a `MOTION_VERTICAL_*` name),
   - the "hold the estimate while moving" trust gate (`_trusts`,
     `MOTION_TILT_TRUST_G`),
   - gap handling (`_elapsed`: hold across stream gaps > 1 s),
   - the zero deadband to keep a resting ring from emitting recorder rows
     (reuse `MOTION_TILT_DEADBAND_DEG` semantics: report `0.0` below it).
   Dropped: `zero()`, `_capture_zero`, `_strays`, `_median_pose`,
   `_window_median`, the still-run bookkeeping, and `calibrated`
   (`angle_deg` becomes available as soon as the first trusted pose exists).
4. **Axis assumption.** The user asserts Z is the ring-hole axis, but the
   codebase docs (`gestures.py` header, `colmi_codec/motion.py`) correctly
   note the firmware's axis mapping has never been confirmed on our R02.
   Mitigation: put the axis choice in one constant
   (`MOTION_VERTICAL_AXIS: Literal["x", "y", "z"] = "z"` in `const.py`), read
   it in the tracker, and add a **verification step** (below) before merging.
   If verification says otherwise, it's a one-character change.

## File-by-file changes

### `custom_components/colmi_ring/gestures.py`
- Replace `TiltTracker` with `VerticalAngleTracker`:
  - `feed(sample, now)` → update/hold the pose as today, then set and return
    `angle_deg = degrees(asin(abs(pose[axis])))` (0 below deadband).
  - `reset()` only (no `zero()`, no `calibrated`).
- Delete `_separation_deg` **only if** no other caller remains (it is
  tilt-only today — check with a search before removing).
- Update the module docstring's paragraph about `TiltTracker`.

### `custom_components/colmi_ring/const.py`
- Rename the tilt tuning block to "Vertical angle":
  - keep: tau, trust-g, deadband (renamed `MOTION_VERTICAL_*`),
  - delete: `MOTION_TILT_ZERO_STILL_S`, `MOTION_TILT_MIN_ZERO_SAMPLES`,
    `MOTION_TILT_ZERO_TOLERANCE_DEG`, `MOTION_TILT_ZERO_TIMEOUT_S`.
- Add `MOTION_VERTICAL_AXIS = "z"` with a comment describing the assumption
  and how to change it.

### `custom_components/colmi_ring/motion_manager.py`
- Use `VerticalAngleTracker` instead of `TiltTracker`; same feed site
  (`_tilt.feed(sample)` in the sample path) and same published attribute
  name `last_angle_deg` (keeps sensor/manager plumbing identical).
- Delete `async_zero_angle()`; clear `last_angle_deg` on stream stop as today.
- Update the field comment on `last_angle_deg` (now: available from the first
  trusted sample, not "until still enough to zero on").

### `custom_components/colmi_ring/sensor.py`
- Keep entity key `vertical_angle`, unique id, unit `°`, precision 1 — the
  history/entity_id survive; only the meaning of the number changes.
- Rewrite the `ColmiRingVerticalAngleSensor` and `VERTICAL_ANGLE_SENSOR`
  docstrings/comments: absolute elevation of the ring-hole axis above
  horizontal, 0..90, sign-insensitive, yaw-insensitive, no zeroing.
- State-class note: value is now bounded 0..90.

### `custom_components/colmi_ring/button.py`
- Remove `ColmiRingZeroVerticalAngleButton` and its conditional registration
  (and the now-unused import if any).

### `scripts/_tilt_check.py` → `scripts/_vertical_angle_check.py`
Rewrite the offline checks (same harness style):
1. Z horizontal (`tilt from +Z = 90°`, i.e. `z = 0`) → ≈ 0°.
2. Z at 30°/45°/60° elevation → ≈ same.
3. Z straight up and straight down (±z) → both ≈ 90°.
4. Yaw about gravity at fixed elevation → reading constant.
5. A linear-acceleration burst (magnitude away from 1 g) doesn't move it.
6. A stream gap doesn't move it.
7. A too-weak sample is ignored.
8. Resting-hand jitter within deadband reads 0.
9. `reset()` → `None` again; first trusted sample yields a value without any
   still-pose wait.

### `README.md`
- Rewrite the "Vertical angle" section: absolute elevation of the ring-hole
  axis above horizontal; 0° when the hole axis is horizontal, 90° when it
  points at the sky or the floor; insensitive to spinning in the horizontal
  plane; no zero button anymore.
- Update the entity bullet list (drop the `Zero Vertical Angle` button).

## Verification steps

1. `python scripts/_vertical_angle_check.py` — all synthetic poses pass.
2. `scripts/lint` (ruff) clean.
3. **Hardware axis check** (the only unverified assumption): with the motion
   stream on and the axis sensors visible, place the ring so the hole axis is
   (a) horizontal, then (b) vertical, and confirm `z` is the axis whose g
   reading flips 0 → ±1 between the two poses, and that the Vertical Angle
   sensor follows 0° → 90°. If a different axis tracks, change
   `MOTION_VERTICAL_AXIS` and re-run step 1.
4. If `scripts/_motion_replay.py` / a captured session log exists, replay it
   and eyeball the new angle trace for sanity (0 while hand flat, rising when
   the finger points up/down).

## Risks / notes

- **Semantics change under a stable entity id**: automations keyed on the old
  relative meaning will see different numbers. Acceptable pre-1.0; call it out
  in the changelog/README.
- **Yaw blindness is a physical limit** (an accelerometer at rest cannot see
  rotation about gravity) — it is also exactly the requested behavior here,
  so it is documented as a feature, not a bug.
- **Worn-on-finger caveat**: "horizontal ring" while worn depends on the
  finger and hand posture; the sensor measures the axis, not intent. One
  sentence in the README is enough.
- `_angle_deg`/`_separation_deg`: `_angle_deg` is used by the gesture
  detector and stays; remove `_separation_deg` only if nothing else uses it.
