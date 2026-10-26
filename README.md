# What?
This is a VERY early attempt to integrate the Colmi line of smart rings with Home Assistant. It is an incomplete work in progress.
Colmi rings (like the R02, R06, R10 etc) can be found for udner $20 on AliExpress.

# How?
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

Someday I might get auto-discovery to work. Until then, go to Settings -> Devices and Services -> Add Integration -> Colmi Ring

# TODO
1) Add more data
2) Fill in gaps when you were out of the house