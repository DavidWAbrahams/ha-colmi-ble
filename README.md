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

The only gesture implemented so far is `Clap`. It can come from either of two places, each behind its
own switch, and both feed the same `Clap` event entity:

| Switch | What it does | What it costs |
| --- | --- | --- |
| `Clap Detection` | Asks the ring to run its own on-device clap detector and report when it fires | Holds the ring's single Bluetooth link, so polling freezes while it is on; turns itself off after a configurable number of minutes (120 by default) |
| `Motion Stream` | Makes the ring stream raw accelerometer data continuously, so that gestures can be derived here | Also freezes polling, and drains the battery quickly; turns itself off after a configurable number of minutes (30 by default, restarted on every gesture) |

Whether *either* of them can actually give you a clap depends on your ring's firmware - see
[If the `Clap` event never fires](#if-the-clap-event-never-fires).

`Clap Detection` needs no option and is always there - just switch it on. So is the `Clap` event
entity, since a clap has its own source and should not disappear because gesture streaming is off.

`Motion Stream` is opt-in from the integration's options ("Enable gesture detection"). Enabling it adds
that switch plus:

- four accelerometer sensors - `Acceleration X`, `Acceleration Y`, `Acceleration Z` and
  `Acceleration` (the magnitude) - see below, and
- a `Vertical Angle` sensor with a `Zero Vertical Angle` button - see below.

The two switches are mutually exclusive. Both want the same accelerometer over the same one link, so
turning one on while the other is running is *refused*, with an error naming the switch to turn off,
rather than silently ending a session you were relying on.

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

**Why they cost something.** The ring serves one Bluetooth connection at a time, and a BLE notification
only reaches a central that is connected *at that moment*. So either switch has to hold the ring's link
open for as long as it is on, and while it does, *normal polling stops* - heart rate, steps and battery
freeze - and the buttons and the sampling-interval number refuse to run until the switch is off.
Streaming additionally drains the battery quickly. Both switches turn themselves off after a
configurable number of minutes; the stream's timer restarts every time you make a gesture, the clap
listener's deliberately does **not**, so that someone clapping every few minutes cannot keep the ring's
link (and therefore its data) held indefinitely.

## If the `Clap` event never fires

There are two possible sources for a clap, and on some firmware there is no working one. This is a
limitation of the ring, not something the integration can tune around, so it is worth knowing which
situation you are in before spending time on automations.

- **The ring's own detector** (`source: ring`), behind the `Clap Detection` switch. The integration asks
  the firmware to report its camera-button gesture - the one the ring uses to trigger a phone's camera
  shutter - which the vendor's own app exposes as a clap. This is now measured rather than assumed:
  with the ring *not* streaming, the enable write `02 04 00 … 06` was followed by two manual claps, and
  each one pushed a single unsolicited `02 02 00 … 04` notification back. So the detector needs no RAW
  stream, which is why it got its own switch - and why that switch has to hold the link, because a clap
  reported while we are disconnected is simply gone. Some firmware ignores the request entirely; then
  the switch cannot work, and Home Assistant says so instead of pretending (see the log lines below).
- **Derived from the accelerometer** (`source: stream`), behind the `Motion Stream` switch. A clap is a
  very sharp, very short impulse, so this needs a fast stream: at least 15 samples per second. Stock
  firmware reports about 1 per second and the
  [faster-raw-values MOD](https://github.com/atc1441/ATC_RF03_Ring/blob/main/R02_3.00.06_FasterRawValuesMOD.bin)
  about 4, both far too few to resolve an impulse narrower than one sample interval. Below that rate
  the integration turns this route off on purpose rather than invent claps from noise.

The two do not coexist on our firmware: RAW mode and camera mode are both users of one accelerometer,
which is why the switches refuse each other rather than sharing.

To find out what your ring supports, switch on `Clap Detection` and look in the Home Assistant log.

- If the `Clap` event entity fires when you clap, nothing further is needed.
- A `WARNING` saying clap detection *turned itself off because the ring never accepted the write that
  enables its clap detector (`02 04`)* means this firmware has no on-device clap to report. The session
  closes instead of holding the link for nothing, so polling resumes on its own.
- A `WARNING` saying clap detection *could not be opened* is a connection problem (ring out of range,
  or another program such as the probe below still holding it), not a missing feature.

[`scripts/_accel_probe.py`](scripts/_accel_probe.py) is the direct test if you want the raw evidence. It
talks to the ring over Bluetooth without Home Assistant (turn both switches off first, since the ring
serves one connection at a time), subscribes to *every* notifiable characteristic, and arms the
camera-button gesture **twice** - once on its own with the stream off, and once again on top of a
running stream - prompting you to clap each time:

```
python scripts/_accel_probe.py --address AA:BB:CC:DD:EE:FF
```

It ends with a `Verdict:` line naming which case you have:

- **camera mode confirmed** - `0x02` reports arrive with the stream off. Your ring can clap, and the
  `Clap Detection` switch is the switch to use.
- **mutually exclusive** - `0x02` reports arrive only while the stream is off. Same conclusion, and the
  reason the two switches refuse each other instead of both being on.
- **no gesture either way** - no `0x02` reports in either phase. This firmware has no on-device clap,
  and the only remaining route would be firmware that streams fast enough to derive one here.

The `0x02` count is also printed for the window where the gesture was switched *off*. Claps counted
there would mean the ring reports them whether or not it was asked, which is why the enabled count
alone is not proof.

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

## Vertical angle

`sensor.<ring>_vertical_angle` is the one derived motion sensor: how far the ring has tilted away from
standing up straight, in degrees. `0` means the ring is upright, `90` is a quarter turn away from that,
and `180` is upside down. Like a plumb line it only measures against the vertical, which is why it is a
single non-negative number rather than a compass heading.

It is derived from the accelerometer alone, on the assumption that a still ring reads gravity. Two
consequences are worth knowing before building an automation on it:

- it measures *tilt*, not *roll*. A still accelerometer cannot see rotation about the direction of
  gravity, so turning your hand like a doorknob leaves the value alone.
- it cannot drift, because nothing is integrated. Bring your hand back to the zero pose and the
  sensor returns to zero.

The angle is zeroed automatically on the first still pose after the stream starts, and on demand by
the `Zero Vertical Angle` button. That button is the one control that works *while* the stream is on:
it writes nothing to the ring, so the stream keeps running and the sensor simply starts counting from
wherever your hand happens to be. Zero it with the ring held upright and the reading becomes the angle
from vertical itself - handy if the pose the stream happened to auto-zero on isn't the reference you
want. The reading is low-pass filtered and has a small deadband, so a resting hand holds a steady `0`
instead of wandering by a few degrees.

# TODO
1) Consider dubious metrics like blood pressure, blood sugar, ECG, HRV, stress etc. The protocol can technically provide these, but on a $20 optical ring they are probably nonsense, so I don't expose them.