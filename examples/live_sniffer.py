"""
Multi-Vendor BLE Cold-Chain Sensor Live Sniffer.
Continuously scans and decodes MOKO SMART BXP-S (L02/L02S/M2) and Tzone BT04B broadcast advertisements.
"""

import asyncio
import os
import sys

# Ensure repo root is on the path when running this script directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bleak import BleakScanner
from moko.bxps_frames import parse_bxps_advertising
from tzone.parser import parse_broadcast_data

# Tzone BT04B identifies itself with Manufacturer ID 0xCB11 in Manufacturer Data,
# or with a local name containing "BT04" or "Temp".
TZONE_MFR_ID = 0xCB11
TZONE_NAME_KEYWORDS = ("BT04", "TEMP_", "TEMP-")


def _is_tzone_name(name: str) -> bool:
    name_up = name.upper()
    return any(kw in name_up for kw in TZONE_NAME_KEYWORDS)


def _is_sane_tzone(parsed: dict) -> bool:
    """Reject clearly invalid decoded payloads."""
    if parsed.get("device_id", "00000000") == "00000000":
        return False
    batt = parsed.get("battery_percent", 0)
    if batt > 100:
        return False
    temp = parsed.get("temperature_c")
    if temp is not None and (temp < -40.0 or temp > 85.0):
        return False
    return True


def _anon(mac: str) -> str:
    """Return last 3 bytes of MAC as anonymous short ID, e.g. 6C:54:00."""
    return mac.upper().replace("-", ":")[-8:]


def detection_callback(device, advertisement_data):
    dev_name = advertisement_data.local_name or device.name or ""
    dev_id = _anon(device.address)

    # 1. MOKO SMART BXP-S Series (L02, L02S, M2, M2S, etc.)
    # Decodes all broadcast frames: Sensor Info (0x80), Custom T&H (0x70),
    # Device Info (0x40), Accel (0x60), iBeacon Scan RSP (0x50), Eddystone (UID/URL/TLM), and Apple iBeacon.
    moko_frames = parse_bxps_advertising(
        advertisement_data.service_data,
        advertisement_data.manufacturer_data,
        local_name=dev_name,
    )

    if moko_frames:
        for frame in moko_frames:
            ftype = frame.get("frame_type")
            print(f"[MOKO BXP-S] ...{dev_id} ({dev_name or 'BXP-S Beacon'}) | RSSI: {advertisement_data.rssi} dBm")

            if ftype == "sensor_info":
                batt = frame.get("battery_voltage_mv")
                batt_str = f"{batt} mV" if batt else f"{frame.get('battery_percentage')}%"
                door_state = "OPEN" if frame["status_flags"]["door_open"] else "CLOSED"
                motion_state = "MOVING" if frame["status_flags"]["is_moving"] else "STATIC"
                acc = frame.get("acceleration_mg", {})
                print(f"       [0x80 Sensor Info] Temp: {frame['temperature_c']} °C | Hum: {frame['humidity_rh']} %RH | Batt: {batt_str}")
                print(f"       Door: {door_state} (Triggers: {frame['hall_trigger_count']}) | Motion: {motion_state} (Triggers: {frame['motion_trigger_count']})")
                print(f"       Accel: X={acc.get('x')} mg, Y={acc.get('y')} mg, Z={acc.get('z')} mg | Tag ID: {frame['tag_id']}")

            elif ftype == "custom_th":
                print(f"       [0x70 Custom T&H] Temp: {frame['temperature_c']} °C | Hum: {frame['humidity_rh']} %RH | Batt: {frame['battery_voltage_mv']} mV")
                print(f"       Device Type: {frame['device_type_desc']} | Interval: {frame['adv_interval_ms']} ms | MAC: {frame['mac_address']}")

            elif ftype == "device_info":
                sw = frame.get("switch_status", {})
                prop = frame.get("properties", {})
                print(f"       [0x40 Device Info] FW: {frame['firmware_version']} | Batt: {frame['battery_voltage_mv']} mV | MAC: {frame['mac_address']}")
                print(f"       Connectable: {sw.get('connectable')} | Password: {prop.get('password_verification')} | Door: {'OPEN' if sw.get('door_open') else 'CLOSED'}")

            elif ftype == "custom_accel":
                raw_a = frame.get("raw_accel", {})
                print(f"       [0x60 Accel Frame] Rate: {frame['sampling_rate']} | Scale: {frame['full_scale']} | Threshold: {frame['motion_threshold_g']}g")
                print(f"       Raw Accel: X={raw_a.get('x')}, Y={raw_a.get('y')}, Z={raw_a.get('z')} | Batt: {frame['battery_voltage_mv']} mV")

            elif ftype in ("ibeacon", "moko_ibeacon_scan_rsp"):
                print(f"       [iBeacon] UUID: {frame['uuid']} | Major: {frame['major']} | Minor: {frame['minor']} | RSSI@1m: {frame.get('rssi_1m_dbm')} dBm")

            elif ftype == "eddystone_tlm":
                batt_val = frame.get("battery_voltage_mv") or f"{frame.get('battery_percentage')}%"
                print(f"       [Eddystone-TLM] Temp: {frame.get('temperature_c')} °C | Batt: {batt_val} | AdvCount: {frame.get('adv_count')} | Uptime: {frame.get('uptime_seconds')}s")

            elif ftype == "eddystone_uid":
                print(f"       [Eddystone-UID] Namespace: {frame['namespace_id']} | Instance: {frame['instance_id']} | RSSI@0m: {frame['rssi_0m_dbm']} dBm")

            elif ftype == "eddystone_url":
                print(f"       [Eddystone-URL] URL: {frame['url']} | RSSI@0m: {frame['rssi_0m_dbm']} dBm")

            print("-" * 65)
        return

    # 2. Tzone BT04B — identified ONLY by Manufacturer ID 0xCB11 or known device name prefix.
    tzone_payload = advertisement_data.manufacturer_data.get(TZONE_MFR_ID)

    if tzone_payload is None and _is_tzone_name(dev_name):
        for _, m_data in advertisement_data.manufacturer_data.items():
            if len(m_data) >= 16:
                tzone_payload = m_data
                break

    if tzone_payload is None:
        return

    parsed = parse_broadcast_data(tzone_payload)
    if parsed and _is_sane_tzone(parsed):
        t = parsed["temperature_c"]
        h = parsed["humidity_percent"]
        b = parsed["battery_percent"]
        t_str = f"{t} °C" if t is not None else "-- °C"
        h_str = f"{h} %RH" if h is not None else "-- %RH"
        print(f"[TZONE] ...{dev_id} ({dev_name or 'BT04B'}) | RSSI: {advertisement_data.rssi} dBm")
        print(f"        ID: {parsed['device_id']} | Temp: {t_str} | Hum: {h_str}")
        print(f"        Battery: {b}% | Status: {parsed['alarm_status_str']}")
        print("-" * 65)


async def main():
    print("Starting Multi-Vendor BLE Cold-Chain Sniffer (MOKO BXP-S / L02S + Tzone BT04B)...")
    print("Listening for all BXP-S frames (Sensor Info 0x80, T&H 0x70, Device Info 0x40, Accel 0x60, iBeacon, Eddystone)...")
    print("Press Ctrl+C to terminate.\n")
    scanner = BleakScanner(detection_callback)
    await scanner.start()
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await scanner.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nSniffer stopped by user.")
