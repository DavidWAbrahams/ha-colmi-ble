# What?
Integrate the Colmi line of smart rings with Home Assistant.
Colmi rings (like the R02, R06, R10 etc) can be found for under $20 on AliExpress.
These can be distiguished because they use QRing as their phone app.
It will read heart rate, blood oxygen levels, steps, and a few other derived metrics.
It also supports gestures!

There is also partial support for some clones that are sold interchangeably on AliExpress, i.e. the YCBT R20 and R99 which use "Smarthealth" app. I've been burned by sellers mislabeling their product and wanted something to work for you in that case.
This does not include gesture support.

# How?
Home Assistant will periodically pull data from your ring via Bluetooth Low Energy.
This is built on top of tahnok's Python API (https://github.com/tahnok/colmi_r02_client) and ludeeus's Home Assistant Integration blueprint (https://github.com/ludeeus/integration_blueprint)

# Requirements
Your home assistant device must have the ability to make active Bluetooth Low Energy (BLE) connections. The bluetooth adapter in a raspberry pi or some PC motherboards will satisfy this. It will also work with ESPHome bluetooth proxies if they are configured something like this:

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
The ring's SpO2 (blood oxygen) reading may be a larger battery drain than heart rate, which is why Colmi's own firmware doesn't sample it automatically and there is no automatic sampling rate here either. The "Read SpO2" button triggers an on-demand measurement. You can automate pressing that button in Home Assistant as you wish, understanding that battery life will be reduced.

# Filling in the gaps (backfill)
The ring only reports data while it is within Bluetooth range of Home Assistant. When you leave the house, the BLE connection drops and Home Assistant stops receiving data.

The ring doesn't stop measuring, though. It keeps logging heart rate and activity to internal memory. When you come home and the ring reconnects, this integration reads that stored log and tries to backfill the missing time.

You can toggle these behaviors under Settings -> Devices and Services -> Colmi Ring -> Configure.

# Gestures

| Switch | What it does | What it costs |
| --- | --- | --- |
| `Wave Detection` | Asks the ring to run its own on-device wave detector and report when it fires | Shares the Bluetooth link: heart rate, battery and steps keep updating while it is on; turns itself off after a configurable number of minutes (120 by default) |
| `Motion Stream` | Makes the ring stream raw accelerometer data continuously. The integration derives two gestures from that stream (`Hand still`, `Hand flip` - see below), and the raw values are published as sensors so you can build your own gestures in HA too | Takes the link exclusively, so polling and the buttons freeze while it is on, and it drains the battery quickly; turns itself off after a configurable number of minutes (30 by default, restarted on every gesture) |

Both switches are available on any Colmi ring; nothing runs (and nothing costs
battery) until you turn one on. While the `Motion Stream` switch is on, it also
provides:

- four accelerometer sensors - `Acceleration X`, `Acceleration Y`, `Acceleration Z` and
  `Acceleration` (the magnitude) - see below, and
- a `Vertical Angle` sensor - see below.

The two switches are mutually exclusive with *each other*. The RAW stream puts the firmware into a
mode that takes over the accelerometer and stops the wave detector's reports.

The stream-derived gestures are designed within the limits of the hardware: the stock R02
firmware sends one sample per second, so these gestures are about *poses held for seconds*,
not quick motions. They work at 1 Hz and get a little crisper if
you flash the 4 Hz firmware mentioned below. How long a pose must be held is configurable in the
integration's options.

| Gesture | Fires when |
| --- | --- |
| `hand_still` | your hand stays roughly motionless for the hold time |
| `hand_flip` | you hold one pose (e.g. palm up), quickly turn your hand 180°, and hold the opposite pose (palm down) |

Each gesture also has a matching *event entity* (`event.<ring>_wave`, `event.<ring>_hand_still`,
`event.<ring>_hand_flip`), so automations can trigger on either the entity or the event below.

There is a `colmi_ring_gesture` event that carries the gesture, and any future ones that
we may implement:

```yaml
trigger:
  - platform: event
    event_type: colmi_ring_gesture
    event_data:
      gesture: hand_flip
action:
  - variables:
      angle_deg: "{{ trigger.event.data.angle_deg }}"
```

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

In the stock firmware, these messages are sent once per second. You can easily flash a modified firmware
from your browser that increases an R02 to 4 messages per second. See [R02_3.00.06_FasterRawValuesMOD.bin](https://github.com/atc1441/ATC_RF03_Ring/blob/main/R02_3.00.06_FasterRawValuesMOD.bin) which can be flashed using [ATC RF03 Ring OTA Flasher](https://atc1441.github.io/ATC_RF03_Writer.html). If you do this, you may want to alter this integrations "motion_sensor_update_s" to a lower value, like 0.25s.

Even with a flashed firmware, Home Assistant updates are published about once a second
rather than at the full stream rate - every state change is a recorder row, and a higher rate could
swamp your database. Automations that need a sharp signal could use the gesture event(s).

## Vertical angle

`sensor.<ring>_vertical_angle` is a derived motion sensor: how far the finger is deflected from the horizontal plane. It reads between 0 and 90 degrees:

- about **0** when the ring is held horizontally (the finger is parallel to the floor),
- about **90** when the ring is held vertically (the finger is perpendicular to the floor - up or down both read 90, the sensor does know which end is up).

In practice a real ring never quite reaches either end: sensor bias and tiny
residual motion keep a "flat" reading a degree or two above 0 and an upright one
a degree or two below 90. If you want the sensors to actually use the full 0-90 scale,
there are two configuration options to manually adjust the endpoints.

# TODO
1) Consider dubious metrics like blood pressure, blood sugar, ECG, HRV, stress etc. The protocol can technically provide these, but on a $20 optical ring they are probably nonsense, so I don't expose them.
