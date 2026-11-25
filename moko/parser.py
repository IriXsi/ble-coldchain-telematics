"""
MOKO L02/L02S BLE Advertising Frame Parser.
Decodes 21-byte Sensor Info payload (0x80) broadcasted by MOKO beacons.
"""

import struct
from typing import Any, Dict, Optional


def parse_moko_payload(payload: bytes) -> Optional[Dict[str, Any]]:
    """
    Parses MOKO Sensor Info payload (21 bytes, Big-Endian).
    Returns a dictionary of parsed telemetry or None if frame is invalid.
    """
    header_idx = payload.find(b"\x80")
    if header_idx == -1:
        return None

    data = payload[header_idx:]
    if len(data) < 21:
        return None

    # Big-Endian format:
    # > B  (Frame Type: 0x80)
    #   B  (Sensor Status byte)
    #   H  (Magnet count)
    #   H  (Motion count)
    #   h  (Accel X, signed 16-bit)
    #   h  (Accel Y, signed 16-bit)
    #   h  (Accel Z, signed 16-bit)
    #   h  (Temperature, signed 16-bit)
    #   H  (Humidity, unsigned 16-bit)
    #   H  (Battery raw)
    #   3s (Tag ID, 3 bytes)
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
        tag_id_bytes,
    ) = struct.unpack(">BBHHhhhHhH3s", data[:21])

    if frame_type != 0x80:
        return None

    status_flags = {
        "magnetic_approaching": (sensor_status & 0x01) == 0,
        "is_moving": bool((sensor_status >> 1) & 0x01),
        "motion_sensor_equipped": bool((sensor_status >> 2) & 0x01),
        "temp_sensor_equipped": bool((sensor_status >> 3) & 0x01),
        "humidity_sensor_equipped": bool((sensor_status >> 4) & 0x01),
        "flash_equipped": bool((sensor_status >> 5) & 0x01),
    }

    battery_info = (
        {"battery_voltage_mv": battery_raw}
        if battery_raw > 100
        else {"battery_percentage": battery_raw}
    )

    return {
        "tag_id": tag_id_bytes.hex().upper(),
        "temperature_c": round(temp_raw * 0.1, 1),
        "humidity_rh": round(hum_raw * 0.1, 1),
        "acceleration_mg": {"x": accel_x, "y": accel_y, "z": accel_z},
        "motion_count": motion_cnt,
        "magnet_count": magnet_cnt,
        **battery_info,
        "status_flags": status_flags,
    }
