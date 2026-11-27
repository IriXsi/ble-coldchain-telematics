#!/usr/bin/env python3
"""
config_export.py — Read ALL configurations from a MOKO BXP-S / L02S device
and save them to a JSON profile file.

Usage:
    python config_export.py --mac AA:BB:CC:DD:EE:FF
    python config_export.py --mac AA:BB:CC:DD:EE:FF --out my_device.json
    python config_export.py --mac AA:BB:CC:DD:EE:FF --password MySecret --out backup.json

The exported file is a human-readable JSON that can be opened, edited, and
fed directly to config_apply.py to restore or clone the device.

Schema (config_schema_v1.0.json):
  metadata            → source MAC, timestamp, model, schema version
  switches            → connectable, trigger_led, tagid_autofill, buttons, password
  radio               → phy_mode, channels, battery_adv_mode
  sensor_logging      → T&H sampling/storage, accelerometer rate/scale/threshold
  slots               → 3 advertising slot entries (type, interval, duration, standby, rssi, content)
"""

import argparse
# Ensure Bleak allows execution without Windows GUI STA check failures
try:
    from bleak.backends.winrt.util import allow_sta
    allow_sta()
except Exception:
    pass

import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from moko.config_manager import read_device_config, save_config_to_json


def default_output_path(mac: str) -> str:
    """Generate a default filename from the MAC address and current timestamp."""
    safe_mac = mac.replace(":", "").replace("-", "").upper()
    ts = time.strftime("%Y%m%d_%H%M%S")
    return f"configs/bxps_{safe_mac}_{ts}.json"


async def _run(mac: str, password: str, out_path: str):
    print("=" * 65)
    print("  MOKO BXP-S Configuration Exporter")
    print("=" * 65)
    print(f"  Target device  : {mac}")
    print(f"  Output file    : {out_path}")
    print("=" * 65)
    print()

    try:
        config = await read_device_config(
            mac_address=mac,
            password=password,
            log_fn=lambda msg: print(f"  {msg}"),
        )
    except Exception as e:
        print(f"\n[ERROR] Failed to read device: {e}")
        sys.exit(1)

    # Save to JSON
    save_config_to_json(config, out_path)

    # Print summary
    print()
    print("=" * 65)
    print(f"  ✅ Configuration exported successfully!")
    print(f"  File saved to: {os.path.abspath(out_path)}")
    print()
    print("  Summary of exported settings:")
    print(f"    Slots:          {len(config.get('slots', []))} slots")
    for s in config.get("slots", []):
        label = s.get("slot_label", f"SLOT{s.get('slot', 0) + 1}")
        t = s.get("adv_type_name", "Unknown")
        interval = s.get("interval_ms", "?")
        err = s.get("error")
        if err:
            print(f"      {label}: ERROR — {err}")
        else:
            print(f"      {label}: {t}, {interval}ms interval")

    sw = config.get("switches", {})
    print(f"    Connectable:    {sw.get('connectable', '?')}")
    print(f"    PHY mode:       {config.get('radio', {}).get('phy_mode', '?')}")
    print(f"    Channels:       {config.get('radio', {}).get('advertising_channels', '?')}")
    sl = config.get("sensor_logging", {})
    print(f"    T&H sampling:   {sl.get('th_sampling_interval_s', '?')} s")
    if "accel_sampling_rate" in sl:
        print(f"    Accel rate:     {sl.get('accel_sampling_rate')}, {sl.get('accel_full_scale')}")
    print("=" * 65)
    print()
    print(f"  → You can now edit '{out_path}' and apply it to any device using:")
    print(f"    python config_apply.py --mac <target_mac> --config \"{out_path}\"")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Export all BXP-S device configurations to a JSON profile.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mac", "-m",
        required=True,
        help="BLE MAC address of the target device (e.g. AA:BB:CC:DD:EE:FF)",
    )
    parser.add_argument(
        "--password", "-p",
        default="Moko4321",
        help="Connection password (default: Moko4321)",
    )
    parser.add_argument(
        "--out", "-o",
        default=None,
        help="Output JSON file path (default: configs/bxps_<MAC>_<timestamp>.json)",
    )
    args = parser.parse_args()

    out_path = args.out or default_output_path(args.mac)
    asyncio.run(_run(args.mac, args.password, out_path))


if __name__ == "__main__":
    main()
