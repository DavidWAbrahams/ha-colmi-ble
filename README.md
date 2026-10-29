# What?
Integrate the Colmi line of smart rings with Home Assistant.
Colmi rings (like the R02, R06, R10 etc) can be found for udner $20 on AliExpress.
It will read heart rate, blood oxygen levels, steps, and a few other derived metrics.

# How?
Home Assistant will periodically pull data from your ring via Bluetooth Low Energy.
This is built on top of tahnok's Python API (https://github.com/tahnok/colmi_r02_client) and ludeeus's Home Assistant Integration blueprint (https://github.com/ludeeus/integration_blueprint)

# Requirements
Your home assistant device must have the ability to make active Bluetooth Low Energy (BLE) connections. The bluetooth adapter in a raspberry pi or some PC motherboards will satisfy this. It will also work with ESPHome bluetooth repeaters if they are configured something like this:

```
esp32_ble_tracker:
  scan_parameters:
    active: true

bluetooth_proxy:
  active: true
```

# Setup
Install HACS. Add this repo to your Custom Repositories. Install "Colmi Ring" from the HACS list.

If for some reason autodiscovery doesn't work, try going to Settings -> Devices and Services -> Add Integration -> Colmi Ring

# A note on SpO2
The ring's SpO2 (blood oxygen) reading may be a larger battery drain than heart rate, which is why Colmi's own firmware doesn't sample it automatically and there is no automatic sampling rate here either. The "Read SpO2" button triggers an on-demand measurement. You can automate pressing that button in Home Assistant at your own risk (expect reduced battery life if you do it frequently).

# Filling in the gaps (backfill)
The ring only reports data while it is within Bluetooth range of Home Assistant. The moment you leave the house and walk out of range, the BLE connection drops and Home Assistant stops receiving anything new.

The ring doesn't stop measuring, though. It keeps logging heart rate and activity to internal memory. When you come home and the ring reconnects, this integration reads that stored log and tries to backfill the missing time.

- **Heart rate** is backfilled automatically (on by default). Because it is a plain measurement, it shows up correctly in both the raw history graph and long-term statistics.
- **Steps, distance, and calories** backfill is optional and experimental (off by default). These are total-increasing sensors, so I don't know if backfilled data points will play nicely with statistics (min/max/avg). Enable it from the integration's options ("Experimental: also backfill steps/distance/calories history") if are ok with that and want their history graphs filled in anyway.

You can toggle these behaviors under Settings -> Devices and Services -> Colmi Ring -> Configure.

# Gestures (experimental, off by default)

The ring can stream its raw accelerometer data, and this integration can watch that stream for three
gestures: a **clap**, a **rapid hand raise**, and a **rapid hand lower**.

Enable it in the integration's options ("Enable gesture detection"). That adds:

- a `Motion Stream` switch, and
- three event entities - `Clap`, `Hand Raise`, `Hand Lower` - you can use directly as automation
  triggers.

There is also a single `colmi_ring_gesture` event that carries every gesture, so one trigger can
catch all three and inspect what happened:

```yaml
trigger:
  - platform: event
    event_type: colmi_ring_gesture
    event_data:
      gesture: hand_raise
action:
  - variables:
      peak_g: "{{ trigger.event.data.peak_g }}"
```

The event data includes `gesture`, `source` (`ring` if the ring detected its own clap, `stream` if we
derived it from the accelerometer), `peak_g` and `velocity_g_s`.

**Why it is opt-in.** The ring serves one Bluetooth connection at a time, so while the stream is on,
*normal polling stops* - heart rate, steps and battery freeze, and heart rate reads `unknown` rather
than showing a stale value. The other buttons and the sampling-interval number refuse to run until the
stream is off. Streaming also drains the battery noticeably faster than polling does. The stream turns
itself off after a configurable number of minutes (30 by default, restarted every time you make a
gesture) so a forgotten switch cannot flatten the ring.

**Tips.** A gesture has to be *sharp*: a slow lift is deliberately not a raise, and the detector also
wants your hand to have been still just before the gesture and to settle just after it, which is what
keeps walking, typing and vibrating from firing claps. If gestures are hard to trigger, raise the
"Gesture sensitivity" option; if they fire when you did not mean it, lower it. The `peak_g` in each
event tells you what your real gestures actually scored.

**Status.** The gesture thresholds were tuned against simulated motion rather than a captured stream
from this exact firmware, so they may need adjustment for a real wrist on a real finger. About five
seconds after the stream turns on, the integration logs one line naming the measured sample rate and
the median accelerometer magnitude, which is the quickest way to judge your setup: the rate should be
at least 15 Hz for a clap to be derived from the stream (below that it comes from the ring's own
built-in clap detector instead, which is enabled at the same time, so such events arrive with
`source: ring`), and the magnitude should be near `1.00 g` while your hand is at rest. A magnitude far
from that means the packet decode itself is wrong rather than the tuning being off, and is logged as a
warning. Turning on "Log every accelerometer sample to the debug log" in the options shows what your
wrist is actually producing, which is the fastest way to tell a wrong guess from a threshold that
needs moving.

# TODO
1) Consider dubious metrics like blood pressure, blood sugar, ECG, HRV, stress etc. The protocol can technically provide these, but on a $20 optical ring they are probably nonsense, so I don't expose them.