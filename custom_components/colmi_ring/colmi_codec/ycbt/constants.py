"""Protocol constants for the Yucheng YCBT-SDK ring family (R20/R99).

The wire format was reverse-engineered from HCI snoops of the official
companion app; this implementation is original to this integration.

Frame layout::

    [group(1)] [key(1)] [len_lo(1)] [len_hi(1)] [payload...] [crc_lo(1)] [crc_hi(1)]

with ``len = payload + 6`` and opcode = ``group << 8 | key``.
"""

from __future__ import annotations

# -------- Vendor BLE service & characteristics --------

UUID_SERVICE = "be940000-7333-be46-b7ae-689e71722bd5"
# Write (must be write-without-response: the firmware is sensitive to it)
# + indicate. Most command replies arrive here.
UUID_CHAR_WRITE = "be940001-7333-be46-b7ae-689e71722bd5"
# Indicate only - push frames (HR/SpO2/BP/battery uploads).
UUID_CHAR_NOTIFY = "be940003-7333-be46-b7ae-689e71722bd5"

# Standard Heart Rate service/characteristic (some firmwares, e.g. the R99,
# expose 0x180D/0x2A37 alongside or instead of the vendor pushes).
UUID_HRS_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
UUID_HRS_CHAR = "00002a37-0000-1000-8000-00805f9b34fb"

# -------- Opcode codes (group << 8 | key) --------

SETTING_TIME = 0x0100
SETTING_USER_INFO = 0x0103
ENABLE_HEALTH_SENSORS = 0x0104
#: Yucheng "user-bind" / pairing-state echo observed on every connect.
SETTING_USER_BIND_ECHO = 0x0109
SET_MONITOR_INTERVAL = 0x010C
#: Yucheng "sedentary reminder" / activity-goal echo.
SETTING_REMINDER_ECHO = 0x0112
ENABLE_BG_SPO2_MONITOR = 0x0126

GET_DEVICE_INFO = 0x0200
GET_DEVICE_SUPPORT_FN = 0x0201
GET_DEVICE_NAME = 0x0203
GET_NOW_STEP = 0x020C
GET_REAL_BLOOD_OXYGEN = 0x0211
GET_POWER_STATISTICS = 0x0225

APP_CONTROL_REAL = 0x0309
APP_PRIME_SENSORS = 0x030C
APP_START_MEASUREMENT = 0x032F

MEASUREMENT_COMPLETE = 0x040E
MEASUREMENT_STATUS = 0x0413

# History request opcodes (Group_Health = 5)
HEALTH_HISTORY_SPORT = 0x0502
HEALTH_HISTORY_SLEEP = 0x0504
HEALTH_HISTORY_HEART = 0x0506
HEALTH_HISTORY_BLOOD = 0x0508
HEALTH_HISTORY_ALL = 0x0509
#: Manual-workout session history (streams back under the same opcode).
HEALTH_HISTORY_SPORT_MODE = 0x052D
# History data-stream opcodes (returned after request)
HEALTH_STREAM_SPORT = 0x0511
HEALTH_STREAM_SLEEP = 0x0513
HEALTH_STREAM_HEART = 0x0515
HEALTH_STREAM_BLOOD = 0x0517
HEALTH_STREAM_ALL = 0x0518
HEALTH_HISTORY_ACK = 0x0580

# Delete-after-sync opcodes. The firmware retains history until the app
# clears it; this integration is deliberately READ-ONLY and never sends
# these, so the OEM companion app keeps working with the same ring.
HEALTH_DELETE_SPORT = 0x0540
HEALTH_DELETE_SLEEP = 0x0541
HEALTH_DELETE_HEART = 0x0542
HEALTH_DELETE_BLOOD = 0x0543
HEALTH_DELETE_ALL = 0x0544

# Real-time push frames (group 6)
REAL_UPLOAD_SNAPSHOT = 0x0600
REAL_UPLOAD_HEART = 0x0601
REAL_UPLOAD_BLOOD_OXY = 0x0602
REAL_UPLOAD_BLOOD_PRESS = 0x0603
#: Per-second measurement-progress push observed on R99 during a manual
#: SpO2 session (31-byte payload; byte 21 is an elapsed-seconds counter and
#: the value fields stay zero until the ring computes a reading). The R99
#: never pushes 0x0602, so this is its only live measurement frame.
REAL_UPLOAD_MEASURE_PROGRESS = 0x060D
REAL_UPLOAD_BODY_DATA = 0x0610
REAL_UPLOAD_BATTERY = 0x0615

#: Single-byte refusal the firmware answers when the link is not bound
#: (stale bond). Triggers the once-per-session re-bind recovery burst.
REJECTION_BYTE = 0xFC
#: Answer to a Get* command sent without its magic prefix.
NEEDS_MAGIC_BYTE = 0xFE

# -------- Per-command magic "permission" prefixes --------
# Firmware returns 0xFE without these 2-byte payload prefixes on the
# corresponding Get* commands.

MAGIC_GET_DEVICE_INFO = b"GC"
MAGIC_GET_DEVICE_SUPPORT_FN = b"GF"
MAGIC_GET_DEVICE_NAME = b"GP"
MAGIC_GET_REAL_BLOOD_OXYGEN = b"IS"

# -------- APP_START_MEASUREMENT type byte --------
# Payload layout: [action, type]; action 0x01 = start, 0x00 = stop.
# R20 firmware (2.32) only supports HR/BP/SpO2; the rest are SDK-wide.

MEASURE_HEART_RATE = 0
MEASURE_BLOOD_PRESSURE = 1
MEASURE_BLOOD_OXYGEN = 2

#: All measurement types defined by the YCBT SDK (supported or not).
MEASURE_TYPES = {
    "heart_rate": 0,
    "blood_pressure": 1,
    "blood_oxygen": 2,
    "respiration": 3,
    "temperature": 4,
    "blood_sugar": 5,
    "uric_acid": 6,
    "ketone": 7,
    "eda": 8,
    "lipids": 9,
    "hrv": 10,
    "ppg_raw": 11,
    "bp_alt": 12,
    "vo2max": 13,
}

#: Epoch offset used by Yucheng firmware: 2000-01-01 in Unix seconds.
EPOCH_2000_UNIX_SECONDS = 946_684_800

#: Manufacturer ID seen in the R99's advertisement (0x7830 LE: 30 73).
MANUFACTURER_ID = 0x7830

#: Which stream opcode answers each history request.
STREAM_FOR_REQUEST: dict[int, int] = {
    HEALTH_HISTORY_SPORT: HEALTH_STREAM_SPORT,
    HEALTH_HISTORY_SLEEP: HEALTH_STREAM_SLEEP,
    HEALTH_HISTORY_HEART: HEALTH_STREAM_HEART,
    HEALTH_HISTORY_BLOOD: HEALTH_STREAM_BLOOD,
    HEALTH_HISTORY_ALL: HEALTH_STREAM_ALL,
    # sport-mode streams back under its own request opcode
    HEALTH_HISTORY_SPORT_MODE: HEALTH_HISTORY_SPORT_MODE,
}
