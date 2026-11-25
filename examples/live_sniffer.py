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

    # 1. MOKO SMART L02 / L02S — identified by Service UUID 0xEA01
    for uuid, payload in advertisement_data.service_data.items():
        if MOKO_UUID_16 in str(uuid).lower():
            parsed = parse_moko_payload(payload)
            if parsed:
                batt = parsed.get("battery_voltage_mv")
                batt_str = f"{batt} mV" if batt else f"{parsed.get('battery_percentage')}%"
                print(f"[MOKO] ...{dev_id} ({dev_name or 'MOKO Sensor'}) | RSSI: {advertisement_data.rssi} dBm")
                print(f"       Temp: {parsed['temperature_c']} °C | Hum: {parsed['humidity_rh']} %RH | Batt: {batt_str}")
                print(f"       Motion: {parsed['status_flags']['is_moving']} (Count: {parsed['motion_count']}) | Tag: {parsed['tag_id']}")
                print("-" * 60)
            return

    # 2. Tzone BT04B — identified ONLY by Manufacturer ID 0xCB11 or known device name prefix.
    #    Do NOT fall back to payload length alone — that causes false positives on MOKO gateways.
    tzone_payload = advertisement_data.manufacturer_data.get(TZONE_MFR_ID)

    if tzone_payload is None and _is_tzone_name(dev_name):
        # Device name matches but no MFR ID — try any manufacturer payload as last resort
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
        print("-" * 60)


async def main():
    print("Starting Multi-Vendor BLE Cold-Chain Sniffer (MOKO L02/L02S + Tzone BT04B)...")
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
