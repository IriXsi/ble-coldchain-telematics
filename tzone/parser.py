"""
Tzone BT04B Binary Frame and Sample Record Parsers.
Official protocol specification for broadcast packets and 24-bit compact flash history.
"""

import datetime
import struct
from typing import Any, Dict, List, Optional, Tuple


def decode_sample_24bit(chunk: bytes) -> Tuple[Optional[float], Optional[float]]:
    """
    Decodes the 24-bit packed sample from BT04B flash history synchronization:
    - 7-bit humidity: (byte0 >> 1) & 0x7F (resolution 1 %RH)
    - 11-bit temperature: (raw24 >> 6) & 0x7FF
      * If raw >= 1250: temp = (raw - 2048) * 0.1 °C (negative temperature)
      * Else: temp = raw * 0.1 °C
    - 6-bit reserve: raw24 & 0x3F
    """
    if len(chunk) < 3:
        return None, None

    raw24 = (chunk[0] << 16) | (chunk[1] << 8) | chunk[2]
    humidity = float((chunk[0] >> 1) & 0x7F)
    raw_temp = (raw24 >> 6) & 0x7FF

    if raw_temp >= 1250:
        temp = (raw_temp - 2048) * 0.1
    else:
        temp = raw_temp * 0.1

    return round(temp, 1), round(humidity, 1)


# Backward-compatibility alias
decode_sample_record = decode_sample_24bit


def parse_broadcast_data(data: bytes) -> Optional[Dict[str, Any]]:
    """
    Decodes 20-byte Service Data / Manufacturer Data from BT04B advertising packets.
    Handles both direct Service Data (starting with 0x11) and Manufacturer payloads.
    """
    payload = None
    idx = data.find(b"\xff\xcb\x11")
    if idx != -1 and len(data[idx:]) >= 19:
        payload = data[idx + 3:]
    elif data.startswith(b"\x11") and len(data) >= 17:
        payload = data[1:]
    elif len(data) >= 16:
        payload = data

    if not payload or len(payload) < 16:
        return None

    hw_type = f"0x{payload[0:2].hex().upper()}"
    fw_ver = f"v{payload[2]}"
    device_id = payload[3:7].hex().upper()
    battery_pct = payload[7]

    # Temperature (2 bytes, Big-Endian)
    raw_temp = struct.unpack(">H", payload[9:11])[0]
    temp_abnormal = bool(raw_temp & 0x8000)
    temp_neg = bool(raw_temp & 0x4000)
    temp_val = (raw_temp & 0x3FFF) * 0.01
    temperature = -temp_val if temp_neg else temp_val

    # Humidity (2 bytes, Big-Endian)
    raw_hum = struct.unpack(">H", payload[11:13])[0]
    hum_abnormal = bool(raw_hum & 0x8000)
    humidity = (raw_hum & 0x7FFF) * 0.01

    alarm = payload[15]
    low_batt = bool(alarm & 0x80)
    temp_overrun = bool(alarm & 0x40)

    alarm_str = "Normal"
    if low_batt and temp_overrun:
        alarm_str = "Low Batt + Over-Temp"
    elif low_batt:
        alarm_str = "Low Battery"
    elif temp_overrun:
        alarm_str = "Over-Temp"

    return {
        "hardware_type": hw_type,
        "firmware_version": fw_ver,
        "device_id": device_id,
        "battery_percent": battery_pct,
        "temperature_c": None if temp_abnormal else round(temperature, 2),
        "humidity_percent": None if hum_abnormal else round(humidity, 2),
        "low_power_alarm": low_batt,
        "temp_alarm": temp_overrun,
        "alarm_status_str": alarm_str,
    }


def parse_history_payload(
    base_timestamp: datetime.datetime, interval_sec: int, data: bytes
) -> List[Dict[str, Any]]:
    """
    Unpacks a continuous stream of 3-byte samples projected from a base timestamp.
    """
    records = []
    chunk_size = 3
    num_samples = len(data) // chunk_size

    for i in range(num_samples):
        chunk = data[i * chunk_size : (i + 1) * chunk_size]
        temp, hum = decode_sample_24bit(chunk)
        sample_time = base_timestamp + datetime.timedelta(seconds=i * interval_sec)
        records.append({
            "timestamp": sample_time.strftime("%Y-%m-%d %H:%M:%S"),
            "unix_ts": int(sample_time.timestamp()),
            "temperature_c": temp,
            "humidity_rh": hum,
        })

    return records
