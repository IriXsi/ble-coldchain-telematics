"""
Multi-Vendor BLE Cold-Chain Sensor Live Sniffer.
Continuously scans and decodes MOKO SMART and Tzone BT04B broadcast advertisements.
"""

import asyncio
import os
import sys

# Ensure repo root is on the path when running this script directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bleak import BleakScanner
from moko.parser import parse_moko_payload
from tzone.parser import parse_broadcast_data

MOKO_UUID_16 = "ea01"
TZONE_SERVICE_PREFIX = "2776"


def detection_callback(device, advertisement_data):
    # 1. Check for MOKO SMART L02 / L02S Beacon
    for uuid, payload in advertisement_data.service_data.items():
        if MOKO_UUID_16 in str(uuid).lower():
            parsed = parse_moko_payload(payload)
            if parsed:
                name = advertisement_data.local_name or device.name or "MOKO Sensor"
                batt = parsed.get("battery_voltage_mv")
                batt_str = f"{batt} mV" if batt else f"{parsed.get('battery_percentage')}%"
                print(f"[MOKO] {device.address} ({name}) | RSSI: {advertisement_data.rssi} dBm")
                print(f"       Temp: {parsed['temperature_c']} °C | Hum: {parsed['humidity_rh']} %RH | Batt: {batt_str}")
                print(f"       Motion: {parsed['status_flags']['is_moving']} (Count: {parsed['motion_count']}) | Tag: {parsed['tag_id']}")
                print("-" * 60)
                return

    # 2. Check for Tzone BT04B Data Logger
    # Tzone broadcasts either via Manufacturer Data or specific Service Data
    tzone_payload = None
    for _, m_data in advertisement_data.manufacturer_data.items():
        if len(m_data) >= 16:
            tzone_payload = m_data
            break

    if not tzone_payload:
        for uuid, s_data in advertisement_data.service_data.items():
            if TZONE_SERVICE_PREFIX in str(uuid).lower() or len(s_data) >= 16:
                tzone_payload = s_data
                break

    dev_name = advertisement_data.local_name or device.name or ""
    if tzone_payload or "BT04" in dev_name.upper():
        if tzone_payload:
            parsed = parse_broadcast_data(tzone_payload)
            if parsed and parsed.get("device_id"):
                print(f"[TZONE] {device.address} ({dev_name or 'BT04B'}) | RSSI: {advertisement_data.rssi} dBm")
                print(f"        ID: {parsed['device_id']} | Temp: {parsed['temperature_c']} °C | Hum: {parsed['humidity_percent']} %RH")
                print(f"        Battery: {parsed['battery_percent']}% | Status: {parsed['alarm_status_str']}")
                print("-" * 60)


async def main():
    print("Starting Multi-Vendor BLE Cold-Chain Sniffer (MOKO + Tzone)...")
    print("Press Ctrl+C to terminate.")
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
