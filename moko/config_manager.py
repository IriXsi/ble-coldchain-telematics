"""
MOKO SMART BXP-S Configuration Manager.

Provides bidirectional serialization, export, and import of device configuration profiles:
- Reads all parameters from a live device over BLE GATT and exports to a structured JSON file.
- Reads a JSON configuration file from disk and applies all parameters to a target device.
- Schema matches 1:1 between export and import for seamless cloning, backup, and restore.
"""

# Ensure Bleak allows execution without Windows GUI STA check failures
try:
    from bleak.backends.winrt.util import allow_sta
    allow_sta()
except Exception:
    pass

import asyncio
import json
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from bleak import BleakClient

from .bxps_config import (
    ADV_NAME_TO_TYPE,
    ADV_TYPE_NAMES,
    DEFAULT_PASSWORD,
    BxpsConfigClient,
    BxpsError,
    build_slot_data,
    build_sensor_info_slot_content,
    build_ibeacon_slot_content,
)

SCHEMA_VERSION = "1.0"


def serialize_slot(slot_data: Dict[str, Any]) -> Dict[str, Any]:
    """Formats slot readback into a clean, JSON-serializable dictionary."""
    content_raw = slot_data.get("content_bytes", b"")
    content_dict = slot_data.get("content", {})
    adv_type = slot_data.get("adv_type", 0xFF)
    adv_name = slot_data.get("adv_type_name") or ADV_TYPE_NAMES.get(adv_type, f"0x{adv_type:02X}")

    return {
        "slot": slot_data.get("slot", 0),
        "slot_label": f"SLOT{slot_data.get('slot', 0) + 1}",
        "adv_type": adv_type,
        "adv_type_name": adv_name,
        "interval_ms": slot_data.get("interval_ms", 1000),
        "duration_s": slot_data.get("duration_s", 0),
        "standby_s": slot_data.get("standby_s", 0),
        "rssi_0m_dbm": slot_data.get("rssi_0m_dbm", -59),
        "content": content_dict,
        "content_hex": content_raw.hex() if isinstance(content_raw, (bytes, bytearray)) else "",
    }


def save_config_to_json(config: Dict[str, Any], file_path: str) -> str:
    """Saves a configuration dictionary to a clean JSON file."""
    # If user provided a directory instead of a file, generate default filename inside it
    if os.path.isdir(file_path) or file_path.endswith(("/", "\\")):
        safe_mac = config.get("metadata", {}).get("source_mac", "device").replace(":", "").replace("-", "").upper()
        ts = time.strftime("%Y%m%d_%H%M%S")
        file_path = os.path.join(file_path, f"bxps_{safe_mac}_{ts}.json")

    dir_name = os.path.dirname(os.path.abspath(file_path))
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    return file_path


def load_config_from_json(file_path: str) -> Dict[str, Any]:
    """Loads and validates a JSON configuration file."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Configuration file not found: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Invalid config format in {file_path}: expected JSON object")

    # Basic schema check
    if "slots" not in data and "switches" not in data and "radio" not in data:
        raise ValueError(f"File {file_path} does not appear to be a valid BXP-S configuration file")

    return data


async def read_device_config(
    mac_address: str,
    password: str = DEFAULT_PASSWORD,
    client: Optional[BleakClient] = None,
    log_fn: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """
    Connects to a MOKO BXP-S device and reads all configurable parameters.
    Uses resilient per-parameter reading so individual unsupported commands don't abort export.
    """
    should_close = False
    if client is None:
        client = BleakClient(mac_address)
        await client.connect()
        should_close = True

    try:
        cfg = BxpsConfigClient(client)
        await cfg.start()

        # Helper to safely read a setting with delay and fallback
        async def _safe_read(label: str, coro, default=None):
            await asyncio.sleep(0.06)
            try:
                val = await coro
                return val
            except Exception as exc:
                err_msg = str(exc) or type(exc).__name__
                log_fn(f"    - {label}: unavailable ({err_msg})")
                return default

        # 1. Authentication
        pwd_req = False
        try:
            pwd_req = await cfg.is_password_required()
            if pwd_req:
                log_fn("Authenticating with device password...")
                await cfg.authenticate(password)
        except Exception as e:
            err_msg = str(e) or type(e).__name__
            log_fn(f"  Auth check note: {err_msg}")

        log_fn(f"Reading configurations from {mac_address}...")

        # 2. Basic Switches
        log_fn("  Reading quick switches...")
        switches = {
            "connectable": await _safe_read("connectable", cfg.get_connectable(), True),
            "tagid_autofill": await _safe_read("tagid_autofill", cfg.get_tagid_autofill(), False),
            "button_poweroff_enabled": await _safe_read("button_poweroff", cfg.get_button_poweroff_enabled(), True),
            "button_reset_enabled": await _safe_read("button_reset", cfg.get_button_reset_enabled(), True),
            "password_required": pwd_req,
        }

        # 3. Radio & Hardware
        log_fn("  Reading radio & hardware parameters...")
        radio = {
            "phy_mode": await _safe_read("phy_mode", cfg.get_phy_mode(), "legacy"),
            "advertising_channels": await _safe_read("channels", cfg.get_advertising_channels(), [37, 38, 39]),
            "battery_adv_mode": await _safe_read("battery_adv_mode", cfg.get_battery_adv_mode(), "voltage"),
        }

        # 4. Sensor & Logging
        log_fn("  Reading sensor logging parameters...")
        th_storage = await _safe_read("th_storage", cfg.get_th_storage_settings(), (False, 0))
        th_enabled = th_storage[0] if isinstance(th_storage, (tuple, list)) else False
        th_interval = th_storage[1] if isinstance(th_storage, (tuple, list)) else 0

        sensor_logging: Dict[str, Any] = {
            "th_sampling_interval_s": await _safe_read("th_sampling", cfg.get_th_sampling_interval(), 300),
            "th_storage_enabled": th_enabled,
            "th_storage_interval_min": th_interval,
        }

        # Accelerometer is not supported / omitted for L02S telematics profiles
        sensor_logging["accel_equipped"] = False

        # 5. Advertising Slots 1 to 3
        log_fn("  Reading advertising slots 1-3...")
        slots = []
        for slot_idx in range(3):
            try:
                await asyncio.sleep(0.06)
                slot_data = await cfg.get_slot(slot_idx)
                slots.append(serialize_slot(slot_data))
                adv_type_name = slot_data.get("adv_type_name", "Unknown")
                log_fn(f"    ✓ Slot {slot_idx + 1}: {adv_type_name} ({slot_data.get('interval_ms', 1000)}ms)")
            except Exception as e:
                err_msg = str(e) or type(e).__name__
                slots.append({
                    "slot": slot_idx,
                    "slot_label": f"SLOT{slot_idx + 1}",
                    "error": err_msg,
                })
                log_fn(f"    - Slot {slot_idx + 1}: unavailable ({err_msg})")

        config: Dict[str, Any] = {
            "metadata": {
                "source_mac": mac_address,
                "exported_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                "device_model": "MOKO BXP-S / L02S",
                "schema_version": SCHEMA_VERSION,
            },
            "switches": switches,
            "radio": radio,
            "sensor_logging": sensor_logging,
            "slots": slots,
        }

        return config

    finally:
        if should_close:
            await client.disconnect()


async def apply_device_config(
    mac_address: str,
    config: Dict[str, Any],
    password: str = DEFAULT_PASSWORD,
    client: Optional[BleakClient] = None,
    sync_clock: bool = True,
    log_fn: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """
    Connects to a MOKO BXP-S device and writes all parameters specified in the config dict.
    Returns a status report summarizing applied and skipped settings.
    """
    should_close = False
    if client is None:
        client = BleakClient(mac_address)
        await client.connect()
        should_close = True

    results: Dict[str, Any] = {
        "target_mac": mac_address,
        "applied": [],
        "errors": [],
    }

    try:
        cfg = BxpsConfigClient(client)
        await cfg.start()

        # 1. Authentication
        if await cfg.is_password_required():
            log_fn("Authenticating with target device...")
            await cfg.authenticate(password)

        log_fn(f"Applying configuration to {mac_address}...")

        # 2. Apply Radio Parameters
        radio = config.get("radio", {})
        if "phy_mode" in radio:
            try:
                await cfg.set_phy_mode(radio["phy_mode"])
                results["applied"].append(f"Radio PHY mode: {radio['phy_mode']}")
                log_fn(f"  ✓ PHY mode set to '{radio['phy_mode']}'")
            except Exception as e:
                results["errors"].append(f"PHY mode: {e}")

        if "advertising_channels" in radio and isinstance(radio["advertising_channels"], list):
            try:
                await cfg.set_advertising_channels(radio["advertising_channels"])
                results["applied"].append(f"Channels: {radio['advertising_channels']}")
                log_fn(f"  ✓ Advertising channels set to {radio['advertising_channels']}")
            except Exception as e:
                results["errors"].append(f"Channels: {e}")

        if "battery_adv_mode" in radio:
            try:
                await cfg.set_battery_adv_mode(radio["battery_adv_mode"])
                results["applied"].append(f"Battery ADV mode: {radio['battery_adv_mode']}")
                log_fn(f"  ✓ Battery ADV mode set to '{radio['battery_adv_mode']}'")
            except Exception as e:
                results["errors"].append(f"Battery ADV mode: {e}")

        # 3. Apply Quick Switches
        switches = config.get("switches", {})


        if "tagid_autofill" in switches:
            try:
                await cfg.set_tagid_autofill(bool(switches["tagid_autofill"]))
                results["applied"].append(f"Tag ID autofill: {switches['tagid_autofill']}")
                log_fn(f"  ✓ Tag ID autofill set to {switches['tagid_autofill']}")
            except Exception as e:
                results["errors"].append(f"Tag ID autofill: {e}")

        if "button_poweroff_enabled" in switches:
            try:
                await cfg.set_button_poweroff_enabled(bool(switches["button_poweroff_enabled"]))
                results["applied"].append(f"Button power-off: {switches['button_poweroff_enabled']}")
                log_fn(f"  ✓ Button power-off function set to {switches['button_poweroff_enabled']}")
            except Exception as e:
                results["errors"].append(f"Button power-off: {e}")

        if "button_reset_enabled" in switches:
            try:
                await cfg.set_button_reset_enabled(bool(switches["button_reset_enabled"]))
                results["applied"].append(f"Button reset: {switches['button_reset_enabled']}")
                log_fn(f"  ✓ Button reset function set to {switches['button_reset_enabled']}")
            except Exception as e:
                results["errors"].append(f"Button reset: {e}")

        # 4. Apply Sensor & Logging Settings
        sensor = config.get("sensor_logging", {})
        if "th_sampling_interval_s" in sensor:
            try:
                await cfg.set_th_sampling_interval(int(sensor["th_sampling_interval_s"]))
                results["applied"].append(f"T&H sampling: {sensor['th_sampling_interval_s']}s")
                log_fn(f"  ✓ T&H sampling interval set to {sensor['th_sampling_interval_s']}s")
            except Exception as e:
                results["errors"].append(f"T&H sampling: {e}")

        if "th_storage_enabled" in sensor:
            enabled = bool(sensor["th_storage_enabled"])
            interval = int(sensor.get("th_storage_interval_min", 0))
            try:
                await cfg.set_th_storage_settings(enabled, interval)
                results["applied"].append(f"T&H storage: enabled={enabled}, interval={interval}min")
                log_fn(f"  ✓ T&H flash storage set to enabled={enabled}, interval={interval}m")
            except Exception as e:
                results["errors"].append(f"T&H storage: {e}")



        # 5. Apply Advertising Slots 1 to 3
        slots = config.get("slots", [])
        for slot in slots:
            if not isinstance(slot, dict) or "slot" not in slot:
                continue
            idx = int(slot["slot"])
            adv_type = slot.get("adv_type")
            if adv_type is None and "adv_type_name" in slot:
                adv_type = ADV_NAME_TO_TYPE.get(slot["adv_type_name"].lower(), 0xFF)

            interval_ms = int(slot.get("interval_ms", 1000))
            duration_s = int(slot.get("duration_s", 0))
            standby_s = int(slot.get("standby_s", 0))
            rssi_0m = int(slot.get("rssi_0m_dbm", -59))

            # Determine content payload
            content_bytes = b""
            if "content_hex" in slot and slot["content_hex"]:
                try:
                    content_bytes = bytes.fromhex(slot["content_hex"])
                except Exception:
                    pass

            if not content_bytes and isinstance(slot.get("content"), dict):
                c = slot["content"]
                if adv_type == 0x80 and "device_name" in c and "tag_id" in c:
                    content_bytes = build_sensor_info_slot_content(c["device_name"], c["tag_id"])
                elif adv_type == 0x50 and "uuid" in c and "major" in c and "minor" in c:
                    content_bytes = build_ibeacon_slot_content(c["uuid"], c["major"], c["minor"])

            try:
                await cfg.configure_slot(
                    slot_idx=idx,
                    interval_ms=interval_ms,
                    duration_s=duration_s,
                    standby_s=standby_s,
                    rssi_0m_dbm=rssi_0m,
                    adv_type=adv_type if adv_type is not None else 0xFF,
                    content=content_bytes,
                )
                type_name = slot.get("adv_type_name", f"0x{adv_type:02X}")
                results["applied"].append(f"Slot {idx + 1} ({type_name}, {interval_ms}ms)")
                log_fn(f"  ✓ Slot {idx + 1} configured: {type_name}, interval={interval_ms}ms")
            except Exception as e:
                results["errors"].append(f"Slot {idx + 1}: {e}")

        # 6. Optional Time Sync
        if sync_clock:
            try:
                await cfg.sync_time()
                results["applied"].append("Clock synchronized to UTC")
                log_fn("  ✓ Device clock synchronized to current UTC time")
            except Exception as e:
                results["errors"].append(f"Time sync: {e}")

        # 7. Apply Connectable Switch last (if False, device becomes unconnectable!)
        if "connectable" in switches and switches["connectable"] is False:
            try:
                await cfg.set_connectable(False)
                results["applied"].append("Connectable status: False")
                log_fn("  ⚠️ Connectable status set to FALSE (Hardware reset required to reconnect)")
            except Exception as e:
                results["errors"].append(f"Connectable switch: {e}")

        return results

    finally:
        if should_close:
            await client.disconnect()
