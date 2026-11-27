#!/usr/bin/env python3
"""
config_apply.py — Apply a JSON configuration profile to a MOKO BXP-S / L02S device.

Usage:
    python config_apply.py --mac AA:BB:CC:DD:EE:FF --config my_device.json
    python config_apply.py --mac AA:BB:CC:DD:EE:FF --config my_device.json --no-clock-sync
    python config_apply.py --mac AA:BB:CC:DD:EE:FF --config my_device.json --password MySecret
    python config_apply.py --mac AA:BB:CC:DD:EE:FF  (interactive file browser to pick the .json)

The config file must have been created by config_export.py (schema v1.0).
You can also edit the JSON manually before applying — all fields are human-readable.

Safe-guards built in:
  - Connectable=False is applied LAST (would lock out further connection).
  - Each setting is applied independently; a failure on one does not abort the rest.
  - A full status report (applied / failed) is printed at the end.
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

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from moko.config_manager import apply_device_config, load_config_from_json


def pick_config_file_gui(start_dir: str = None) -> str:
    """Opens a native OS file browser dialog in an isolated subprocess to keep COM MTA clean for Bleak."""
    import subprocess
    start = start_dir or os.path.join(ROOT, "configs")
    if not os.path.exists(start):
        start = ROOT

    try:
        script = (
            "import tkinter as tk, os\n"
            "from tkinter import filedialog\n"
            "root = tk.Tk()\n"
            "root.withdraw()\n"
            "root.attributes('-topmost', True)\n"
            f"f = filedialog.askopenfilename(title='Select BXP-S Configuration File', initialdir={repr(start)}, filetypes=[('BXP-S Config Files', '*.json'), ('All Files', '*.*')])\n"
            "if f:\n"
            "    print(f)\n"
            "root.destroy()\n"
        )
        res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
        chosen = res.stdout.strip()
        if chosen and os.path.isfile(chosen):
            return chosen
    except Exception:
        pass
    return ""


def pick_config_file_terminal(start_dir: str = None) -> str:
    """List available .json config files and let user choose one by number."""
    config_dir = start_dir or os.path.join(ROOT, "configs")
    files = []

    if os.path.isdir(config_dir):
        files = sorted([
            f for f in os.listdir(config_dir)
            if f.endswith(".json")
        ], reverse=True)

    if not files:
        print(f"  No .json files found in '{config_dir}'.")
        path = input("  Enter full path to your config file: ").strip().strip('"').strip("'")
        return path

    print(f"\n  Config files available in '{config_dir}':\n")
    for i, fname in enumerate(files, 1):
        full = os.path.join(config_dir, fname)
        size = os.path.getsize(full)
        mtime = os.path.getmtime(full)
        import time
        modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
        print(f"    [{i:>2}] {fname:<45} {size:>6} bytes  {modified}")

    print()
    choice = input("  Enter number, or type full path: ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(files):
        return os.path.join(config_dir, files[int(choice) - 1])
    return choice.strip('"').strip("'")


async def _run(mac: str, config_path: str, password: str, sync_clock: bool):
    print("=" * 65)
    print("  MOKO BXP-S Configuration Applicator")
    print("=" * 65)
    print(f"  Target device  : {mac}")
    print(f"  Config file    : {os.path.basename(config_path)}")
    print(f"  Clock sync     : {'Yes (sync to UTC now)' if sync_clock else 'No'}")
    print("=" * 65)

    # Load and show config summary
    try:
        config = load_config_from_json(config_path)
    except Exception as e:
        print(f"\n[ERROR] Cannot read config file: {e}")
        sys.exit(1)

    meta = config.get("metadata", {})
    print()
    print("  Config file details:")
    print(f"    Exported from : {meta.get('source_mac', 'Unknown')}")
    print(f"    Exported at   : {meta.get('exported_at', 'Unknown')}")
    print(f"    Device model  : {meta.get('device_model', 'Unknown')}")
    print(f"    Schema version: {meta.get('schema_version', '?')}")
    print()

    # Warn if applying to a different MAC
    source_mac = meta.get("source_mac", "").upper()
    target_mac = mac.upper()
    if source_mac and source_mac.replace(":", "") != target_mac.replace(":", ""):
        print(f"  ⚠️  Note: Config was exported from {source_mac}.")
        print(f"            Applying to a different device: {target_mac}")
        confirm = input("  Continue? (yes/no): ").strip().lower()
        if confirm not in ("y", "yes"):
            print("  Aborted.")
            sys.exit(0)

    print(f"\n  Connecting to {mac} and applying settings...")
    print()

    try:
        result = await apply_device_config(
            mac_address=mac,
            config=config,
            password=password,
            sync_clock=sync_clock,
            log_fn=lambda msg: print(f"  {msg}"),
        )
    except Exception as e:
        print(f"\n[ERROR] Failed to apply configuration: {e}")
        sys.exit(1)

    # Print status report
    print()
    print("=" * 65)
    applied = result.get("applied", [])
    errors = result.get("errors", [])

    if applied:
        print(f"  ✅ Successfully applied {len(applied)} setting(s):")
        for item in applied:
            print(f"      • {item}")

    if errors:
        print(f"\n  ⚠️  {len(errors)} setting(s) could not be applied:")
        for item in errors:
            print(f"      ✗ {item}")

    if not errors:
        print(f"\n  🎉 All settings applied cleanly to {mac}!")
    else:
        print(f"\n  ✅ Configuration applied with {len(errors)} non-fatal issue(s).")

    print("=" * 65)
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Apply a BXP-S JSON configuration profile to a device.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mac", "-m",
        required=True,
        help="BLE MAC address of the target device (e.g. AA:BB:CC:DD:EE:FF)",
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="Path to .json config file (omit to open file browser dialog)",
    )
    parser.add_argument(
        "--password", "-p",
        default="Moko4321",
        help="Connection password (default: Moko4321)",
    )
    parser.add_argument(
        "--no-clock-sync",
        action="store_true",
        help="Skip synchronizing device clock to current UTC time",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Use GUI file browser to select config (default on Windows if no --config given)",
    )
    args = parser.parse_args()

    # Resolve config file path
    config_path = args.config
    if not config_path:
        # Try GUI first, fall back to terminal list
        try:
            if args.gui or sys.platform == "win32":
                config_path = pick_config_file_gui()
        except Exception:
            config_path = None

        if not config_path:
            config_path = pick_config_file_terminal()

    if not config_path:
        print("[ERROR] No config file selected. Exiting.")
        sys.exit(1)

    if not os.path.isfile(config_path):
        print(f"[ERROR] File not found: {config_path}")
        sys.exit(1)

    asyncio.run(_run(
        mac=args.mac,
        config_path=config_path,
        password=args.password,
        sync_clock=not args.no_clock_sync,
    ))


if __name__ == "__main__":
    main()
