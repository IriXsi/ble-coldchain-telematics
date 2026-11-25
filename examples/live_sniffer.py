"""
Multi-Vendor BLE Cold-Chain Sensor Live Sniffer.
Continuously scans for MOKO SMART and Tzone broadcast advertisements.
"""

import asyncio
from bleak import BleakScanner
from moko.parser import parse_moko_payload

MOKO_UUID_16 = "ea01"


def detection_callback(device, advertisement_data):
    # 1. Check for MOKO SMART L02 / L02S Beacon
    for uuid, payload in advertisement_data.service_data.items():
        if MOKO_UUID_16 in str(uuid).lower():
            parsed = parse_moko_payload(payload)
            if parsed:
                name = advertisement_data.local_name or device.name or "MOKO Sensor"
                print(f"[MOKO] {device.address} ({name}) | RSSI: {advertisement_data.rssi} dBm")
                print(f"       Temp: {parsed['temperature_c']} C | Hum: {parsed['humidity_rh']} %RH")
                print(f"       Battery: {parsed.get('battery_voltage_mv', parsed.get('battery_percentage'))}")
                print(f"       Motion: {parsed['status_flags']['is_moving']} (Count: {parsed['motion_count']})")
                print("-" * 55)


async def main():
    print("Starting BLE Cold-Chain Sniffer... (Press Ctrl+C to terminate)")
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
