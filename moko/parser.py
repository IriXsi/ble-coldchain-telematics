"""
MOKO L02/L02S BLE Advertising Frame Parser.
Decodes Sensor Info payload (0x80) broadcasted by MOKO beacons.
"""

import struct
from typing import Any, Dict, Optional


def parse_moko_payload(payload: bytes) -> Optional[Dict[str, Any]]:
    """
    Parses MOKO Sensor Info payload (18-24 bytes, Big-Endian).
    Returns a dictionary of parsed telemetry or None if frame is invalid.

    Binary Layout:
    [0]     = 0x80 (Frame Type)
    [1]     = Sensor status byte (flags for motion, equipped sensors)
    [2:4]   = Magnet trigger count (uint16)
    [4:6]   = Motion trigger count (uint16)
    [6:8]   = Accel X (int16 signed, mg)
    [8:10]  = Accel Y (int16 signed, mg)
    [10:12] = Accel Z (int16 signed, mg)
    [12:14] = Temperature (int16 signed, resolution 0.1 °C)
    [14:16] = Relative Humidity (uint16 unsigned, resolution 0.1 %RH)
    [16:18] = Battery raw (uint16: >100 is mV, <=100 is percentage)
    [18:]   = Tag ID (1-6 bytes)
    """
    if len(payload) < 18 or payload[0] != 0x80:
        return None

    # Big-Endian unpacking:
    # > B  (Frame Type: 0x80)
    #   B  (Sensor Status)
    #   H  (Magnet count, uint16)
    #   H  (Motion count, uint16)
    #   h  (Accel X, int16 signed)
    #   h  (Accel Y, int16 signed)
    #   h  (Accel Z, int16 signed)
    #   h  (Temperature, int16 signed, 0.1 °C)
    #   H  (Humidity, uint16 unsigned, 0.1 %RH)
    #   H  (Battery raw, uint16)
    (
        frame_type,
        sensor_status,
        magnet_cnt,
        motion_cnt,
        accel_x,
        accel_y,
        accel_z,
        temp_raw,
        hum_raw,
        battery_raw,
    ) = struct.unpack(">BBHHhhhhHH", payload[:18])

    tag_id = payload[18:].hex().upper() if len(payload) > 18 else ""

    status_flags = {
        "magnetic_approaching": (sensor_status & 0x01) == 0,
        "is_moving": bool(sensor_status & 0x02),
        "motion_sensor_equipped": bool(sensor_status & 0x04),
        "temp_sensor_equipped": bool(sensor_status & 0x08),
        "humidity_sensor_equipped": bool(sensor_status & 0x10),
        "flash_equipped": bool(sensor_status & 0x20),
    }

    battery_info = (
        {"battery_voltage_mv": battery_raw}
        if battery_raw > 100
        else {"battery_percentage": battery_raw}
    )

    return {
        "tag_id": tag_id,
        "temperature_c": round(temp_raw * 0.1, 1),
        "humidity_rh": round(hum_raw * 0.1, 1),
        "acceleration_mg": {"x": accel_x, "y": accel_y, "z": accel_z},
        "motion_count": motion_cnt,
        "magnet_count": magnet_cnt,
        **battery_info,
        "status_flags": status_flags,
    }
