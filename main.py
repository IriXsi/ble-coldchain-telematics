#!/usr/bin/env python3
"""
BLE Cold-Chain Telematics - Unified Control Center.

An interactive terminal application integrating all telematics modules:
- Real-time Broadcast Sniffer (MOKO BXP-S / L02S + Tzone BT04B)
- BLE Device Scanner & Signal Strength Inspector
- MOKO BXP-S Configuration & Query Client
- Config Export: Read all device settings → JSON profile file
- Config Apply: Apply JSON profile file → device (file browser included)
- Flash Temperature & Humidity Log Downloader
- Tzone BT04B Graphical Dashboard Launcher
- Automated Protocol Validation Test Suite
"""

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
import unittest

# Ensure the repository root is in python path
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from bleak import BleakScanner, BleakClient

from moko.bxps_frames import parse_bxps_advertising
from moko.bxps_config import (
    DEFAULT_PASSWORD,
    SERVICE_UUID,
    BxpsConfigClient,
    BxpsError,
    ADV_TYPE_NAMES,
)
from moko.client import (
    Sensor,
    show as moko_show_all,
    save_history_csv,
    interactive as moko_interactive_menu,
    check_interval,
)
from moko.config_manager import (
    read_device_config,
    apply_device_config,
    save_config_to_json,
    load_config_from_json,
)


def clear_banner():
    print("\n" + "=" * 70)
    print("      ❄️  BLE COLD-CHAIN TELEMATICS - UNIFIED CONTROL CENTER  ❄️")
    print("=" * 70)


def print_menu():
    print("\nSelect an operation to perform:")
    print("  [1] 📡 Live Broadcast Sniffer      - Listen to all MOKO BXP-S & Tzone packets")
    print("  [2] 🔍 Scan for Nearby BLE Devices - Find MAC addresses and signal strengths")
    print("  [3] 📋 Read Device Configurations  - Connect and display all settings (MOKO)")
    print("  [4] ⚙️  Interactive Config Menu    - Configure slots, triggers, time, switches")
    print("  [5] ⏰ Quick UTC Clock Sync        - Synchronize beacon clock to system time")
    print("  [6] 💾 Download Stored Trip Logs   - Export historical T&H flash data to CSV")
    print("  [7] 🗑️  Clear Flash Log Storage    - Erase stored trip history on tag")
    print("  [8] 🖥️  Launch Tzone GUI Dashboard - Start BT04B Tkinter control panel")
    print("  [9] 🧪 Run Protocol Unit Tests     - Validate all decoders & builders (30 checks)")
    print()
    print("  ─── Configuration Profile Tools ─────────────────────────────────")
    print("  [E] 📤 Export Config to File       - Read all device settings → JSON profile")
    print("  [A] 📥 Apply Config from File      - Load JSON profile → write to device")
    print()
    print("  [0] 🚪 Exit")
    print("=" * 70)


# -----------------------------------------------------------------------------
# Action 1: Live Sniffer
# -----------------------------------------------------------------------------
def action_live_sniffer():
    print("\n[Action 1] Starting Live Broadcast Sniffer...")
    print("Listening passively to nearby BXP-S (0x80, 0x70, 0x40, 0x60, iBeacon, Eddystone) and BT04B.")
    print("Press Ctrl+C to return to main menu.\n")

    from examples.live_sniffer import detection_callback

    async def _sniff():
        scanner = BleakScanner(detection_callback)
        await scanner.start()
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await scanner.stop()

    try:
        asyncio.run(_sniff())
    except KeyboardInterrupt:
        print("\n[Live Sniffer Stopped]")


# -----------------------------------------------------------------------------
# Action 2: BLE Device Scanner
# -----------------------------------------------------------------------------
def action_scan():
    print("\n[Action 2] Scanning for nearby BLE devices (8 seconds)...")

    async def _scan():
        scanner = BleakScanner()
        devices = await scanner.discover(timeout=8.0, return_adv=True)
        print(f"\nFound {len(devices)} device(s):\n")
        print(f"{'#':<3} {'MAC Address':<20} {'RSSI':<8} {'Device Name':<24} {'Type'}")
        print("-" * 70)

        results = []
        for i, (addr, (dev, adv)) in enumerate(sorted(devices.items(), key=lambda kv: -kv[1][1].rssi), 1):
            name = dev.name or adv.local_name or "Unknown"
            dev_type = "BLE Device"
            # Quick check if MOKO or Tzone
            for u in adv.service_data:
                u_str = str(u).lower()
                if "ea01" in u_str or "01ea" in u_str:
                    dev_type = "MOKO BXP-S (Sensor Info)"
                elif "feab" in u_str or "abfe" in u_str:
                    dev_type = "MOKO BXP-S (Custom Frame)"
                elif "feaa" in u_str:
                    dev_type = "Eddystone Beacon"
            if 0xCB11 in adv.manufacturer_data or any(k in name.upper() for k in ("BT04", "TEMP")):
                dev_type = "Tzone BT04B"

            print(f"{i:<3} {addr:<20} {adv.rssi:<8} {name[:22]:<24} {dev_type}")
            results.append((addr, name))
        return results

    try:
        return asyncio.run(_scan())
    except KeyboardInterrupt:
        print("\n[Scan cancelled]")
        return []


def ask_address(cached_devices=None):
    if cached_devices:
        print("\nRecently scanned devices:")
        for idx, (addr, name) in enumerate(cached_devices[:5], 1):
            print(f"  [{idx}] {addr} ({name})")
        choice = input("\nEnter number to select, or paste MAC address directly: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(cached_devices):
            return cached_devices[int(choice) - 1][0]
        if choice:
            return choice

    addr = input("Enter device MAC address (e.g. AA:BB:CC:DD:EE:FF): ").strip()
    return addr


# -----------------------------------------------------------------------------
# Action 3: Read Device Configurations
# -----------------------------------------------------------------------------
def action_read_settings(cached_devices=None):
    addr = ask_address(cached_devices)
    if not addr:
        return
    pwd = input(f"Enter connection password [default: {DEFAULT_PASSWORD}]: ").strip() or DEFAULT_PASSWORD

    print(f"\nConnecting to {addr}...")

    async def _read():
        async with BleakClient(addr) as client:
            sn = Sensor(client)
            await sn.start()
            if await sn.password_required():
                print("Authenticating with password...")
                await sn.authenticate(pwd)
            print("\nReading device settings:")
            print("-" * 55)
            await moko_show_all(sn)
            print("-" * 55)

    try:
        asyncio.run(_read())
    except Exception as e:
        print(f"\n[Error reading device]: {e}")


# -----------------------------------------------------------------------------
# Action 4: Interactive Configuration Menu
# -----------------------------------------------------------------------------
def action_configure(cached_devices=None):
    addr = ask_address(cached_devices)
    if not addr:
        return
    pwd = input(f"Enter connection password [default: {DEFAULT_PASSWORD}]: ").strip() or DEFAULT_PASSWORD

    print(f"\nConnecting to {addr}...")

    async def _config():
        async with BleakClient(addr) as client:
            sn = Sensor(client)
            await sn.start()
            if await sn.password_required():
                await sn.authenticate(pwd)
            await moko_interactive_menu(sn)

    try:
        asyncio.run(_config())
    except Exception as e:
        print(f"\n[Configuration Error]: {e}")


# -----------------------------------------------------------------------------
# Action 5: Quick Time Synchronization
# -----------------------------------------------------------------------------
def action_sync_time(cached_devices=None):
    addr = ask_address(cached_devices)
    if not addr:
        return
    pwd = input(f"Enter connection password [default: {DEFAULT_PASSWORD}]: ").strip() or DEFAULT_PASSWORD

    async def _sync():
        async with BleakClient(addr) as client:
            sn = Sensor(client)
            await sn.start()
            if await sn.password_required():
                await sn.authenticate(pwd)
            now = int(time.time())
            await sn.set_time(now)
            readback = await sn.get_time()
            utc_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(readback))
            print(f"\nClock successfully synchronized to: {utc_str} (Unix timestamp: {readback})")

    try:
        asyncio.run(_sync())
    except Exception as e:
        print(f"\n[Clock Sync Error]: {e}")


# -----------------------------------------------------------------------------
# Action 6: Download Trip History
# -----------------------------------------------------------------------------
def action_download_history(cached_devices=None):
    addr = ask_address(cached_devices)
    if not addr:
        return
    pwd = input(f"Enter connection password [default: {DEFAULT_PASSWORD}]: ").strip() or DEFAULT_PASSWORD
    default_csv = f"moko_history_{addr.replace(':', '').replace('-', '')[-6:]}.csv"
    csv_file = input(f"Output CSV filename [default: {default_csv}]: ").strip() or default_csv

    async def _down():
        async with BleakClient(addr) as client:
            sn = Sensor(client)
            await sn.start()
            if await sn.password_required():
                await sn.authenticate(pwd)

            def _progress(got, total):
                pct = (got / total * 100) if total else 100
                print(f"Downloading: {got}/{total} records ({pct:.1f}%)...", end="\r", flush=True)

            print("\nRequesting stored temperature & humidity logs...")
            records, total = await sn.download_history(progress=_progress)
            print()
            if not records:
                print("No stored records found on device flash.")
                return

            save_history_csv(records, csv_file)
            print(f"Successfully saved {len(records)} records to '{csv_file}'!")

    try:
        asyncio.run(_down())
    except Exception as e:
        print(f"\n[History Download Error]: {e}")


# -----------------------------------------------------------------------------
# Action 7: Clear Flash Storage
# -----------------------------------------------------------------------------
def action_clear_history(cached_devices=None):
    addr = ask_address(cached_devices)
    if not addr:
        return
    pwd = input(f"Enter connection password [default: {DEFAULT_PASSWORD}]: ").strip() or DEFAULT_PASSWORD
    confirm = input("⚠️  Are you sure you want to erase all flash records? (yes/no): ").strip().lower()
    if confirm not in ("y", "yes"):
        print("Operation aborted.")
        return

    async def _clear():
        async with BleakClient(addr) as client:
            sn = Sensor(client)
            await sn.start()
            if await sn.password_required():
                await sn.authenticate(pwd)
            await sn.clear_history()
            print("\nFlash storage erased successfully!")

    try:
        asyncio.run(_clear())
    except Exception as e:
        print(f"\n[Clear History Error]: {e}")


# -----------------------------------------------------------------------------
# Action 8: Launch Tzone GUI Dashboard
# -----------------------------------------------------------------------------
def action_tzone_gui():
    print("\n[Action 8] Launching Tzone BT04B Graphical Dashboard (Tkinter)...")
    try:
        import subprocess
        gui_script = os.path.join(ROOT_DIR, "tzone", "dashboard.py")
        subprocess.Popen([sys.executable, gui_script])
        print("Tzone Dashboard launched in background.")
    except Exception as e:
        print(f"[Error launching GUI]: {e}")


# -----------------------------------------------------------------------------
# Action 9: Run Automated Unit Tests
# -----------------------------------------------------------------------------
def action_run_tests():
    print("\n[Action 9] Running Automated Protocol Unit Tests (BXP-S & Tzone)...")
    loader = unittest.defaultTestLoader
    suite = loader.discover(os.path.join(ROOT_DIR, "tests"))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if result.wasSuccessful():
        print(f"\n🎉 ALL {result.testsRun} TESTS PASSED SUCCESSFULLY!")
    else:
        print(f"\n❌ {len(result.failures)} failure(s), {len(result.errors)} error(s).")




# -----------------------------------------------------------------------------
# Action E: Export device config to JSON file
# -----------------------------------------------------------------------------
def action_export_config(cached_devices=None):
    addr = ask_address(cached_devices)
    if not addr:
        return
    pwd = input(f"Enter connection password [default: {DEFAULT_PASSWORD}]: ").strip() or DEFAULT_PASSWORD
    safe_mac = addr.replace(":", "").replace("-", "").upper()
    ts = time.strftime("%Y%m%d_%H%M%S")
    default_out = os.path.join(ROOT_DIR, "configs", f"bxps_{safe_mac}_{ts}.json")
    user_choice = input(f"Save config to [{default_out}]: ").strip().strip('"').strip("'")
    if not user_choice:
        out_path = default_out
    elif os.path.isdir(user_choice) or user_choice.endswith(("/", "\\")):
        out_path = os.path.join(user_choice, f"bxps_{safe_mac}_{ts}.json")
    else:
        out_path = user_choice

    async def _export():
        config = await read_device_config(
            mac_address=addr,
            password=pwd,
            log_fn=lambda msg: print(f"  {msg}"),
        )
        saved = save_config_to_json(config, out_path)
        print(f"\n  ✅ Config saved to: {os.path.abspath(saved)}")
        slots = config.get("slots", [])
        print(f"  Slots exported: {len(slots)}")
        for s in slots:
            print(f"    Slot {s.get('slot', 0) + 1}: {s.get('adv_type_name', '?')} @ {s.get('interval_ms', '?')} ms")
        sw = config.get("switches", {})
        print(f"  Switches: connectable={sw.get('connectable')}, tagid_autofill={sw.get('tagid_autofill')}, "
              f"btn_pwr={sw.get('button_poweroff_enabled')}, btn_rst={sw.get('button_reset_enabled')}")
        print("  Edit the JSON and apply it with [A] to any BXP-S device.")

    try:
        asyncio.run(_export())
    except Exception as exc:
        err_msg = str(exc) or type(exc).__name__
        print(f"\n[Export Error]: {err_msg}")


# -----------------------------------------------------------------------------
# Action A: Apply JSON config file to a device
# -----------------------------------------------------------------------------
def action_apply_config(cached_devices=None):
    addr = ask_address(cached_devices)
    if not addr:
        return
    pwd = input(f"Enter connection password [default: {DEFAULT_PASSWORD}]: ").strip() or DEFAULT_PASSWORD

    config_path = None
    config_dir = os.path.join(ROOT_DIR, "configs")

    # Try GUI file picker in isolated subprocess so COM apartment stays clean for Bleak
    try:
        import subprocess
        start_dir = config_dir if os.path.isdir(config_dir) else ROOT_DIR
        script = (
            "import tkinter as tk, os\n"
            "from tkinter import filedialog\n"
            "root = tk.Tk()\n"
            "root.withdraw()\n"
            "root.attributes('-topmost', True)\n"
            f"f = filedialog.askopenfilename(title='Select BXP-S Configuration Profile (.json)', initialdir={repr(start_dir)}, filetypes=[('BXP-S Config Files', '*.json'), ('All Files', '*.*')])\n"
            "if f:\n"
            "    print(f)\n"
            "root.destroy()\n"
        )
        res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
        chosen = res.stdout.strip()
        if chosen and os.path.isfile(chosen):
            config_path = chosen
    except Exception:
        pass

    if not config_path:
        if os.path.isdir(config_dir):
            files = sorted(
                [f for f in os.listdir(config_dir) if f.endswith(".json")],
                reverse=True,
            )
            if files:
                print(f"\n  Available configs in '{config_dir}':\n")
                for i, fn in enumerate(files, 1):
                    print(f"    [{i:>2}] {fn}")
                choice = input("\n  Enter number or full file path: ").strip()
                if choice.isdigit() and 1 <= int(choice) <= len(files):
                    config_path = os.path.join(config_dir, files[int(choice) - 1])
                else:
                    config_path = choice.strip('"').strip("'")
        if not config_path:
            config_path = input("  Enter full path to config file: ").strip().strip('"').strip("'")

    if not config_path or not os.path.isfile(config_path):
        print(f"  [Error] File not found: {config_path}")
        return

    sync_inp = input("  Sync device clock to UTC now? [Y/n]: ").strip().lower()
    sync_clock = sync_inp not in ("n", "no")

    async def _apply():
        try:
            config = load_config_from_json(config_path)
        except Exception as exc:
            print(f"\n  [Error] Cannot load config: {exc}")
            return
        meta = config.get("metadata", {})
        src_mac = meta.get("source_mac", "unknown")
        exported_at = meta.get("exported_at", "unknown")
        print(f"\n  Config source MAC : {src_mac}")
        print(f"  Config exported at: {exported_at}")
        if src_mac.upper().replace(":", "") != addr.upper().replace(":", ""):
            confirm = input(
                f"\n  Applying config from {src_mac} to {addr}. Continue? (yes/no): "
            ).strip().lower()
            if confirm not in ("y", "yes"):
                print("  Aborted.")
                return
        result = await apply_device_config(
            mac_address=addr,
            config=config,
            password=pwd,
            sync_clock=sync_clock,
            log_fn=lambda msg: print(f"  {msg}"),
        )
        applied = result.get("applied", [])
        errors = result.get("errors", [])
        print(f"\n  Applied: {len(applied)} settings")
        for item in applied:
            print(f"      {item}")
        if errors:
            print(f"\n  Failed:  {len(errors)} settings")
            for item in errors:
                print(f"      {item}")

    try:
        from bleak.backends.winrt.util import uninitialize_sta
        uninitialize_sta()
    except Exception:
        pass

    try:
        asyncio.run(_apply())
    except Exception as exc:
        err_msg = str(exc) or type(exc).__name__
        print(f"\n[Apply Error]: {err_msg}")


# -----------------------------------------------------------------------------
# Main Loop
# -----------------------------------------------------------------------------
def main():
    cached_devices = []
    clear_banner()
    while True:
        print_menu()
        choice = input("Enter choice (0-9, E, A): ").strip().lower()

        if choice == "1":
            action_live_sniffer()
        elif choice == "2":
            cached_devices = action_scan()
        elif choice == "3":
            action_read_settings(cached_devices)
        elif choice == "4":
            action_configure(cached_devices)
        elif choice == "5":
            action_sync_time(cached_devices)
        elif choice == "6":
            action_download_history(cached_devices)
        elif choice == "7":
            action_clear_history(cached_devices)
        elif choice == "8":
            action_tzone_gui()
        elif choice == "9":
            action_run_tests()
        elif choice == "e":
            action_export_config(cached_devices)
        elif choice == "a":
            action_apply_config(cached_devices)
        elif choice in ("0", "q", "exit"):
            print("\nExiting BLE Telematics Suite. Goodbye!\n")
            break
        else:
            print("Invalid selection. Please choose 0-9, E, or A.")

        input("\nPress Enter to return to main menu...")


if __name__ == "__main__":
    main()
