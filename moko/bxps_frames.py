"""
MOKO SMART BXP-S Series Broadcast Frames Parser.

Implements complete decoding of all BLE advertising and scan response packets
supported by MOKO BXP-S series beacons (L02, L02S, M2, M2S, etc.) based on
"BXP-S Series ADV Format V1.0" and "MK Sensor Series APP User Manual V1.2".

Supported frame formats:
  1. Customized Sensor info (0x80 on Service UUID 0x01EA / 0xEA01)
     - ADV_IND: Sensor status, trigger counts (hall, motion), 3-axis accel (mg),
       temperature, humidity, battery (mV or %), Tag ID.
     - SCAN_RSP: Device local name (AD Type 0x09).
  2. Customized T&H Frame (0x70 on Service UUID 0xABFE / 0xFEAB)
     - Ranging data, Adv interval, Temperature (signed 0.1°C), Humidity (0.1%RH),
       Battery voltage (mV), Device type, MAC address.
  3. Customized iBeacon Frame (0x50 SCAN_RSP on Service UUID 0xABFE + standard Apple iBeacon)
     - Apple iBeacon (0x004C, 0x02, 0x15): UUID, Major, Minor, RSSI@1m.
     - MOKO Scan Response: 0x50, RSSI@1m, Adv interval, iBeacon UUID, Major, Minor.
  4. Google Eddystone Frames (Service UUID 0xAAFE / 0xFEAA):
     - Eddystone-UID (0x00): RSSI@0m, Namespace ID (10B), Instance ID (6B).
     - Eddystone-URL (0x10): RSSI@0m, Scheme prefix, Encoded URL.
     - Eddystone-TLM (0x20): Unencrypted TLM, Battery (mV/%), Temp (signed 8.8 fixed-point),
       ADV_CNT, SEC_CNT (0.1s resolution).
  5. Customized Device Info Frame (0x40 on Service UUID 0xABFE):
     - Ranging data, interval, battery voltage, property flags, switch flags, MAC, firmware ver.
  6. Customized 3-Axis Acc Frame (0x60 on Service UUID 0xABFE):
     - Ranging data, interval, sample rate, full scale, motion threshold, raw X/Y/Z, battery, MAC.
"""

import struct
from typing import Any, Dict, List, Optional, Tuple, Union

# Known MOKO 16-bit Service UUIDs (canonical lowercase strings)
UUID_MOKO_SENSOR_INFO = "0000ea01-0000-1000-8000-00805f9b34fb"  # 0x01EA / 0xEA01
UUID_MOKO_CUSTOM = "0000feab-0000-1000-8000-00805f9b34fb"       # 0xABFE / 0xFEAB
UUID_EDDYSTONE = "0000feaa-0000-1000-8000-00805f9b34fb"          # 0xAAFE / 0xFEAA
UUID_APPLE_COMPANY_ID = 0x004C

# Eddystone URL constants
EDDYSTONE_URL_SCHEMES = {
    0: "http://www.",
    1: "https://www.",
    2: "http://",
    3: "https://",
}

EDDYSTONE_URL_EXPANSIONS = {
    0: ".com/",
    1: ".org/",
    2: ".edu/",
    3: ".net/",
    4: ".info/",
    5: ".biz/",
    6: ".gov/",
    7: ".com",
    8: ".org",
    9: ".edu",
    10: ".net",
    11: ".info",
    12: ".biz",
    13: ".gov",
}

# Chip & Sensor decoding tables (from BXP-S protocol / 设备类型 / 传感器型号)
CHIP_TYPES = {
    0x00: "Telink TLA 8251",
    0x01: "Telink TLA 8258",
    0x10: "Nordic nRF52810",
    0x11: "Nordic nRF52832",
    0x12: "Nordic nRF52811",
    0x13: "Nordic nRF52833",
    0x14: "Nordic nRF52840",
    0x15: "Nordic nRF52805",
    0x16: "Nordic nRF54L05",
    0x17: "Nordic nRF54L10",
    0x18: "Nordic nRF54L15",
    0x20: "Silicon Labs EFR32BG22",
    0x30: "Realtek 8762 CMF",
    0x31: "Realtek 8762 EMF",
    0x40: "Atmosic ATM2202",
}

TH_SENSOR_MODELS = {
    0x00: "None",
    0x01: "SHT30/SHT31",
    0x02: "SHT40",
    0x03: "STS40",
    0x04: "SHT43",
    0x05: "GXHT40",
    0x06: "PT100",
    0x07: "NTC",
}

ACCEL_SENSOR_MODELS = {
    0x00: "None",
    0x01: "LIS2DH/LIS3DH",
    0x02: "STK8328",
}


def parse_bxps_sensor_info(payload: bytes) -> Optional[Dict[str, Any]]:
    """
    Parses MOKO BXP-S Customized Sensor Info Frame (0x80).
    Service UUID: 0x01EA (represented in BLE Service Data as 0xEA01).

    Binary layout (18-24+ bytes):
      [0]     = 0x80 (Frame Type UUID)
      [1]     = Sensor status byte:
                Bit 0: Hall sensor status (1: Magnet away/Door open, 0: Magnet approach/Door closed)
                Bit 1: Accelerometer status (1: In move, 0: In static)
                Bit 2: Accelerometer equipped (1: Yes, 0: No)
                Bit 3: Temperature sensor equipped (1: Yes, 0: No)
                Bit 4: Humidity sensor equipped (1: Yes, 0: No)
                Bit 5: Flash equipped (1: Yes, 0: No)
                Bit 6-7: Reserved
      [2:4]   = Hall trigger event count (uint16 big-endian)
      [4:6]   = Motion trigger event count (uint16 big-endian)
      [6:8]   = X-axis acceleration in mg (int16 signed big-endian)
      [8:10]  = Y-axis acceleration in mg (int16 signed big-endian)
      [10:12] = Z-axis acceleration in mg (int16 signed big-endian)
      [12:14] = Temperature data (int16 signed big-endian, unit 0.1°C)
      [14:16] = Humidity data (uint16 unsigned big-endian, unit 0.1%RH)
      [16:18] = Battery value (uint16 big-endian: >100 is mV, <=100 is %)
      [18:24] = Tag ID (1-6 bytes hexadecimal)
    """
    if len(payload) < 18 or payload[0] != 0x80:
        return None

    (
        frame_type,
        sensor_status,
        hall_cnt,
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
        "door_open": bool(sensor_status & 0x01),
        "door_closed": not bool(sensor_status & 0x01),
        "is_moving": bool(sensor_status & 0x02),
        "is_static": not bool(sensor_status & 0x02),
        "accel_equipped": bool(sensor_status & 0x04),
        "temp_equipped": bool(sensor_status & 0x08),
        "humidity_equipped": bool(sensor_status & 0x10),
        "flash_equipped": bool(sensor_status & 0x20),
    }

    battery_info = (
        {"battery_voltage_mv": battery_raw}
        if battery_raw > 100
        else {"battery_percentage": battery_raw}
    )

    data = {
        "frame_type": "sensor_info",
        "frame_id": 0x80,
        "tag_id": tag_id,
        "temperature_c": round(temp_raw * 0.1, 1),
        "humidity_rh": round(hum_raw * 0.1, 1),
        "acceleration_mg": {"x": accel_x, "y": accel_y, "z": accel_z},
        "hall_trigger_count": hall_cnt,
        "motion_trigger_count": motion_cnt,
        **battery_info,
        "status_flags": status_flags,
    }
    return data


def parse_bxps_th_frame(payload: bytes) -> Optional[Dict[str, Any]]:
    """
    Parses MOKO Customized T&H Frame (0x70).
    Service UUID: 0xABFE (in BLE Service Data).

    Binary layout (16 bytes payload):
      [0]     = 0x70 (Frame type)
      [1]     = Ranging data (int8 signed, -100~0 dBm)
      [2]     = Adv interval (uint8, unit: 100ms)
      [3:5]   = Sampling temperature (int16 signed big-endian, unit 0.1°C)
      [5:7]   = Sampling humidity (uint16 unsigned big-endian, unit 0.1%RH)
      [7:9]   = Battery voltage (uint16 unsigned big-endian, unit 1 mV)
      [9]     = Device type (0x01: 3-axis, 0x02: T&H, 0x03: 3-axis + T&H)
      [10:16] = MAC address (6 bytes)
    """
    if len(payload) < 16 or payload[0] != 0x70:
        return None

    frame_type, ranging, adv_int, temp_raw, hum_raw, batt_mv, dev_type = struct.unpack(
        ">BbBhhHB", payload[:10]
    )
    mac_bytes = payload[10:16]
    mac_str = ":".join(f"{b:02X}" for b in mac_bytes)

    dev_type_map = {
        0x01: "3-Axis Only",
        0x02: "T&H Only",
        0x03: "3-Axis and T&H",
    }

    return {
        "frame_type": "custom_th",
        "frame_id": 0x70,
        "ranging_dbm": ranging,
        "adv_interval_ms": adv_int * 100,
        "temperature_c": round(temp_raw * 0.1, 1),
        "humidity_rh": round(hum_raw * 0.1, 1),
        "battery_voltage_mv": batt_mv,
        "device_type_code": dev_type,
        "device_type_desc": dev_type_map.get(dev_type, "Unknown"),
        "mac_address": mac_str,
    }


def parse_bxps_device_info(payload: bytes) -> Optional[Dict[str, Any]]:
    """
    Parses MOKO Customized Device Info Frame (0x40).
    Service UUID: 0xABFE (in BLE Service Data).

    Binary layout (15 bytes payload):
      [0]     = 0x40 (Frame type)
      [1]     = Ranging data (int8, -100~0 dBm)
      [2]     = Adv interval (uint8, unit: 100ms)
      [3:5]   = Battery voltage (uint16, 1 mV)
      [5]     = Device property indicator:
                Bit 0-1: Password verification (00: Enabled, 10: Disabled)
                Bit 2: Ambient light sensor (1: Equipped, 0: Not)
                Bit 3: Hall Door sensor (1: Equipped, 0: Not)
                Bit 4: Tamper sensor (1: Equipped, 0: Not)
      [6]     = Switch status indicator:
                Bit 0: Connectable (1: Connectable, 0: Unconnectable)
                Bit 1: Ambient light detected (1: Yes, 0: No)
                Bit 2: Door status (1: Magnet absent/Open, 0: Magnet present/Closed)
                Bit 3: Tamper status (1: Alert, 0: Normal)
      [7:13]  = MAC address (6 bytes)
      [13:15] = Firmware version (2 bytes: e.g. 0x1100 -> V1.1.0)
    """
    if len(payload) < 15 or payload[0] != 0x40:
        return None

    frame_type, ranging, adv_int, batt_mv, prop, sw = struct.unpack(
        ">BbBHBB", payload[:7]
    )
    mac_bytes = payload[7:13]
    mac_str = ":".join(f"{b:02X}" for b in mac_bytes)
    fw_major = payload[13]
    fw_minor = payload[14]
    fw_str = f"V{(fw_major >> 4)}.{(fw_major & 0x0F)}.{fw_minor}"

    pwd_status_code = prop & 0x03
    pwd_status = "Enabled" if pwd_status_code == 0 else "Disabled"

    properties = {
        "password_verification": pwd_status,
        "light_sensor_equipped": bool(prop & 0x04),
        "hall_sensor_equipped": bool(prop & 0x08),
        "tamper_sensor_equipped": bool(prop & 0x10),
    }

    switches = {
        "connectable": bool(sw & 0x01),
        "light_detected": bool(sw & 0x02),
        "door_open": bool(sw & 0x04),
        "tamper_alert": bool(sw & 0x08),
    }

    return {
        "frame_type": "device_info",
        "frame_id": 0x40,
        "ranging_dbm": ranging,
        "adv_interval_ms": adv_int * 100,
        "battery_voltage_mv": batt_mv,
        "mac_address": mac_str,
        "firmware_version": fw_str,
        "properties": properties,
        "switch_status": switches,
    }


def parse_bxps_accel_frame(payload: bytes) -> Optional[Dict[str, Any]]:
    """
    Parses MOKO Customized 3-Axis Accel Frame (0x60).
    Service UUID: 0xABFE (in BLE Service Data).

    Binary layout (21 bytes payload):
      [0]     = 0x60 (Frame type)
      [1]     = Ranging data (int8, -100~0 dBm)
      [2]     = Adv interval (uint8, unit: 100ms)
      [3]     = Sampling rate (0:1Hz, 1:10Hz, 2:25Hz, 3:50Hz, 4:100Hz)
      [4]     = Full-scale (0:±2g, 1:±4g, 2:±8g, 3:±16g)
      [5]     = Motion threshold (unit: 0.1g)
      [6:8]   = X-axis raw (int16 signed)
      [8:10]  = Y-axis raw (int16 signed)
      [10:12] = Z-axis raw (int16 signed)
      [12:14] = Battery voltage (uint16, 1mV)
      [14]    = RFU
      [15:21] = MAC address (6 bytes)
    """
    if len(payload) < 21 or payload[0] != 0x60:
        return None

    frame_type, ranging, adv_int, rate_code, scale_code, thresh = struct.unpack(
        ">BbBBBB", payload[:6]
    )
    raw_x, raw_y, raw_z, batt_mv, rfu = struct.unpack(">hhhHB", payload[6:15])
    mac_bytes = payload[15:21]
    mac_str = ":".join(f"{b:02X}" for b in mac_bytes)

    rates = {0: "1Hz", 1: "10Hz", 2: "25Hz", 3: "50Hz", 4: "100Hz"}
    scales = {0: "±2g", 1: "±4g", 2: "±8g", 3: "±16g"}

    return {
        "frame_type": "custom_accel",
        "frame_id": 0x60,
        "ranging_dbm": ranging,
        "adv_interval_ms": adv_int * 100,
        "sampling_rate": rates.get(rate_code, f"{rate_code}"),
        "full_scale": scales.get(scale_code, f"{scale_code}"),
        "motion_threshold_g": thresh * 0.1,
        "raw_accel": {"x": raw_x, "y": raw_y, "z": raw_z},
        "battery_voltage_mv": batt_mv,
        "mac_address": mac_str,
    }


def parse_bxps_scan_response_ibeacon(payload: bytes) -> Optional[Dict[str, Any]]:
    """
    Parses MOKO Scan Response for iBeacon slot (Frame type 0x50 on UUID 0xABFE).

    Binary layout (23 bytes payload after UUID):
      [0]     = 0x50 (Frame type)
      [1]     = RSSI@1m (int8 signed)
      [2]     = Adv interval (uint8, unit 100ms)
      [3:19]  = iBeacon UUID (16 bytes)
      [19:21] = Major (uint16 big-endian)
      [21:23] = Minor (uint16 big-endian)
    """
    if len(payload) < 23 or payload[0] != 0x50:
        return None

    frame_type, rssi_1m, adv_int = struct.unpack(">BbB", payload[:3])
    uuid_hex = payload[3:19].hex()
    formatted_uuid = (
        f"{uuid_hex[:8]}-{uuid_hex[8:12]}-{uuid_hex[12:16]}-"
        f"{uuid_hex[16:20]}-{uuid_hex[20:]}"
    )
    major, minor = struct.unpack(">HH", payload[19:23])

    return {
        "frame_type": "moko_ibeacon_scan_rsp",
        "frame_id": 0x50,
        "rssi_1m_dbm": rssi_1m,
        "adv_interval_ms": adv_int * 100,
        "uuid": formatted_uuid,
        "major": major,
        "minor": minor,
    }


def parse_apple_ibeacon(mfg_data: Dict[int, bytes]) -> Optional[Dict[str, Any]]:
    """
    Parses standard Apple iBeacon packet (Company ID 0x004C).
    Structure: [0x02, 0x15, UUID(16), Major(2), Minor(2), RSSI@1m(1)]
    """
    data = mfg_data.get(UUID_APPLE_COMPANY_ID)
    if not data or len(data) < 23 or data[0] != 0x02 or data[1] != 0x15:
        return None

    uuid_raw = data[2:18].hex()
    uuid_str = (
        f"{uuid_raw[:8]}-{uuid_raw[8:12]}-{uuid_raw[12:16]}-"
        f"{uuid_raw[16:20]}-{uuid_raw[20:]}"
    )
    major, minor, rssi_1m = struct.unpack(">HHb", data[18:23])

    return {
        "frame_type": "ibeacon",
        "uuid": uuid_str,
        "major": major,
        "minor": minor,
        "rssi_1m_dbm": rssi_1m,
    }


def parse_eddystone_uid(payload: bytes) -> Optional[Dict[str, Any]]:
    """
    Parses Google Eddystone-UID (0x00 on Service UUID 0xFEAA).
    Layout: [0x00, RSSI@0m(1), NamespaceID(10), InstanceID(6), RFU(2)]
    """
    if len(payload) < 18 or payload[0] != 0x00:
        return None

    rssi_0m = struct.unpack("b", payload[1:2])[0]
    namespace_id = payload[2:12].hex().upper()
    instance_id = payload[12:18].hex().upper()

    return {
        "frame_type": "eddystone_uid",
        "rssi_0m_dbm": rssi_0m,
        "namespace_id": namespace_id,
        "instance_id": instance_id,
    }


def parse_eddystone_url(payload: bytes) -> Optional[Dict[str, Any]]:
    """
    Parses Google Eddystone-URL (0x10 on Service UUID 0xFEAA).
    Layout: [0x10, RSSI@0m(1), URL_Scheme(1), Encoded_URL(1-17)]
    """
    if len(payload) < 3 or payload[0] != 0x10:
        return None

    rssi_0m = struct.unpack("b", payload[1:2])[0]
    scheme_code = payload[2]
    url_prefix = EDDYSTONE_URL_SCHEMES.get(scheme_code, "")

    url_parts = [url_prefix]
    for b in payload[3:]:
        if b in EDDYSTONE_URL_EXPANSIONS:
            url_parts.append(EDDYSTONE_URL_EXPANSIONS[b])
        elif 32 <= b < 127:
            url_parts.append(chr(b))

    return {
        "frame_type": "eddystone_url",
        "rssi_0m_dbm": rssi_0m,
        "url": "".join(url_parts),
    }


def parse_eddystone_tlm(payload: bytes) -> Optional[Dict[str, Any]]:
    """
    Parses Google Eddystone-TLM (0x20 on Service UUID 0xFEAA, unencrypted).
    Layout:
      [0]     = 0x20
      [1]     = TLM Version (0x00 = unencrypted)
      [2:4]   = Battery voltage (uint16 mV)
      [4:6]   = Beacon temperature (int16 signed 8.8 fixed-point)
      [6:10]  = ADV count (uint32)
      [10:14] = SEC count (uint32, 0.1s resolution)
    """
    if len(payload) < 14 or payload[0] != 0x20:
        return None

    version = payload[1]
    if version != 0x00:
        return {
            "frame_type": "eddystone_tlm",
            "version": version,
            "encrypted": True,
        }

    batt_raw, temp_fixed, adv_cnt, sec_cnt = struct.unpack(">HhII", payload[2:14])
    # 8.8 fixed point notation
    temp_c = round(temp_fixed / 256.0, 2)
    uptime_s = round(sec_cnt * 0.1, 1)

    battery_info = (
        {"battery_voltage_mv": batt_raw}
        if batt_raw > 100
        else {"battery_percentage": batt_raw}
    )

    return {
        "frame_type": "eddystone_tlm",
        "version": 0,
        "encrypted": False,
        **battery_info,
        "temperature_c": temp_c,
        "adv_count": adv_cnt,
        "uptime_seconds": uptime_s,
    }


def parse_bxps_advertising(
    service_data: Dict[str, bytes],
    manufacturer_data: Optional[Dict[int, bytes]] = None,
    local_name: str = "",
) -> List[Dict[str, Any]]:
    """
    Master decoder for any MOKO BXP-S advertising bundle.
    Decodes all standard and customized frames present in the broadcast.
    """
    results: List[Dict[str, Any]] = []

    # 1. Inspect Service Data
    for uuid_key, payload in service_data.items():
        k = str(uuid_key).lower()
        if "ea01" in k or "01ea" in k or k == UUID_MOKO_SENSOR_INFO:
            parsed = parse_bxps_sensor_info(payload)
            if parsed:
                if local_name:
                    parsed["device_name"] = local_name
                results.append(parsed)

        elif "feab" in k or "abfe" in k or k == UUID_MOKO_CUSTOM:
            if len(payload) > 0:
                frame_type = payload[0]
                if frame_type == 0x70:
                    parsed = parse_bxps_th_frame(payload)
                elif frame_type == 0x40:
                    parsed = parse_bxps_device_info(payload)
                elif frame_type == 0x60:
                    parsed = parse_bxps_accel_frame(payload)
                elif frame_type == 0x50:
                    parsed = parse_bxps_scan_response_ibeacon(payload)
                else:
                    parsed = None

                if parsed:
                    if local_name:
                        parsed["device_name"] = local_name
                    results.append(parsed)

        elif "feaa" in k or "aafe" in k or k == UUID_EDDYSTONE:
            if len(payload) > 0:
                ft = payload[0]
                if ft == 0x00:
                    parsed = parse_eddystone_uid(payload)
                elif ft == 0x10:
                    parsed = parse_eddystone_url(payload)
                elif ft == 0x20:
                    parsed = parse_eddystone_tlm(payload)
                else:
                    parsed = None

                if parsed:
                    results.append(parsed)

    # 2. Inspect Manufacturer Data (Apple iBeacon)
    if manufacturer_data:
        ib = parse_apple_ibeacon(manufacturer_data)
        if ib:
            results.append(ib)

    return results
