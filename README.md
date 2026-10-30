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

While the motion stream switch is enabled on the Home Assistant device, the device constantly
reports accelerometer data and one gesture that is specifically suppored by the ring firmware: `Clap`.

Enable it in the integration's options ("Enable gesture detection"). That adds:

- a `Motion Stream` switch,
- an event entity - `Clap` - that you can use directly as an automation
  trigger,
- four accelerometer sensors - `Acceleration X`, `Acceleration Y`, `Acceleration Z` and
  `Acceleration` (the magnitude) - see below, and
- a `Ring Angle` sensor with a `Calibrate Ring Angle` button - see below.

There is a `colmi_ring_gesture` event that carries the gesture, and any future ones that
we may implement:

```yaml
trigger:
  - platform: event
    event_type: colmi_ring_gesture
    event_data:
      gesture: clap
action:
  - variables:
      peak_g: "{{ trigger.event.data.peak_g }}"
```

The event data includes `gesture`, `source` (`ring` if the ring detected its own clap, `stream` if we
derived it from the accelerometer), `peak_g` and `velocity_g_s`.

**Why it is opt-in.** The ring serves one Bluetooth connection at a time, so while the stream is on,
*normal polling stops* - heart rate, steps and battery freeze. The other buttons and the sampling-interval number refuse to run until the
stream is off. Streaming also drains the battery quickly. The stream turns
itself off after a configurable number of minutes (30 by default, restarted every time you make a
gesture).

## Watching the raw accelerometer data

While the `Motion Stream` switch is on, the ring's raw accelerometer readings are also published as
four sensors, all in units of `g` and in the ring's own axis frame:

| Sensor | Value |
| --- | --- |
| `sensor.<ring>_acceleration_x` | X axis, as the ring reports it |
| `sensor.<ring>_acceleration_y` | Y axis, as the ring reports it |
| `sensor.<ring>_acceleration_z` | Z axis, as the ring reports it |
| `sensor.<ring>_acceleration` | `sqrt(x^2 + y^2 + z^2)` |

On my R02 ring, the axis are like this:
z: the axis through the finger hole
Y: in the plane of the ring, roughly through the charging port
x: in the plane of the ring, roughly through the red LED

These are deliberately *unprocessed*: no low-pass filter, no gravity removal, no tilt or orientation
solving.

In the stock firmware, these messages are sent once per second. You can easily flash a modified firmware
from your browser that increases this to 4 per second. See [R02_3.00.06_FasterRawValuesMOD.bin](https://github.com/atc1441/ATC_RF03_Ring/blob/main/R02_3.00.06_FasterRawValuesMOD.bin) which can be flashed using [ATC RF03 Ring OTA Flasher](https://atc1441.github.io/ATC_RF03_Writer.html). If you do this, you will likely want to alter this integrations "motion_sensor_update_s" to a lower value, like 0.25.

The motion sensors read `unknown` whenever the motion stream is off. Updates are published about once a second
rather than at the full stream rate - every state change is a recorder row, and a higher rate could
swamp your database. Automations that need the sharp signal could use
the gesture event(s).

## Ring angle

`sensor.<ring>_ring_angle` is the one derived motion sensor: how far the ring has tilted, in degrees,
from the pose it was last zeroed in. `0` is that pose, `90` is a quarter turn away from it, and `180`
is upside down.

It is derived from the accelerometer alone, on the assumption that a still ring reads gravity. Two
consequences are worth knowing before building an automation on it:

- it measures *tilt*, not *roll*. A still accelerometer cannot see rotation about the direction of
  gravity, so turning your hand like a doorknob leaves the value alone. A single non-negative number
  is the whole story for that reason.
- it cannot drift, because nothing is integrated. Bring your hand back to the zero pose and the
  sensor returns to zero.

The angle is zeroed automatically on the first still pose after the stream starts, and on demand by
the `Calibrate Ring Angle` button. That button is the one control that works *while* the stream is on:
it writes nothing to the ring, so the stream keeps running and the sensor simply starts counting from
wherever your hand happens to be. The reading is low-pass filtered and has a small deadband, so a
resting hand holds a steady `0` instead of wandering by a few degrees.

# TODO
1) Consider dubious metrics like blood pressure, blood sugar, ECG, HRV, stress etc. The protocol can technically provide these, but on a $20 optical ring they are probably nonsense, so I don't expose them.