"""
BT04B Temperature & Humidity Data Logger - Complete GATT Workflow & Dashboard
Protocol Version: V2.0 (Feb 23, 2021)
Platform: Windows 10/11 (Python 3.8+ with Bleak)
IDE: Visual Studio Code
"""

import asyncio
import csv
import datetime
import json
import logging
import struct
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable, Dict, List, Optional, Tuple, Union

try:
    from bleak import BleakClient, BleakScanner
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData
    from bleak.exc import BleakError
except ImportError:
    print("\n[ERROR] 'bleak' library is not installed.")
    print("Please run: pip install bleak\n")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("BT04B")

# ============================================================================
# PROTOCOL UUIDS (SECTION 2.1)
# ============================================================================

SERVICE_UUID = "27763b10-999c-4d6a-9fc4-c7272be10900"

CHAR_DEVICE_ID = "27763b11-999c-4d6a-9fc4-c7272be10900"          # 4B, R/W (Hex)
CHAR_BROADCAST_INTERVAL = "27763b12-999c-4d6a-9fc4-c7272be10900" # 2B, R/W (1ms, uint16 LE)
CHAR_PASSWORD = "27763b13-999c-4d6a-9fc4-c7272be10900"           # 6B, R/W (0x00-0x09 per digit)
CHAR_TX_POWER = "27763b14-999c-4d6a-9fc4-c7272be10900"           # 1B, R/W
CHAR_ACQ_INTERVAL = "27763b15-999c-4d6a-9fc4-c7272be10900"       # 4B, R/W (1s, uint32 LE)
CHAR_STORAGE_INTERVAL = "27763b16-999c-4d6a-9fc4-c7272be10900"   # 4B, R/W (2B norm + 2B alarm, LE)
CHAR_STORED_COUNT = "27763b18-999c-4d6a-9fc4-c7272be10900"       # 2B, Read (uint16 LE)
CHAR_ALARM_PARAM = "27763b19-999c-4d6a-9fc4-c7272be10900"        # 2B, R/W (int8 low, int8 high)
CHAR_UTC_TIME = "27763b20-999c-4d6a-9fc4-c7272be10900"           # 6B, R/W (YY, MM, DD, HH, MM, SS)
CHAR_SYNC_SWITCH = "27763b21-999c-4d6a-9fc4-c7272be10900"        # 2B, Notify
CHAR_RECORD_CONTROL = "27763b22-999c-4d6a-9fc4-c7272be10900"     # 1B, R/W (0: Stop, 1: Start)
CHAR_MODEL_VERSION = "27763b23-999c-4d6a-9fc4-c7272be10900"      # 3B, Read
CHAR_STORAGE_R1 = "27763b27-999c-4d6a-9fc4-c7272be10900"         # 20B, R/W
CHAR_STORAGE_R2 = "27763b28-999c-4d6a-9fc4-c7272be10900"         # 20B, R/W
CHAR_STORAGE_R3 = "27763b29-999c-4d6a-9fc4-c7272be10900"         # 20B, R/W
CHAR_STORAGE_R4 = "27763b2a-999c-4d6a-9fc4-c7272be10900"         # 20B, R/W
CHAR_STORAGE_R5 = "27763b2b-999c-4d6a-9fc4-c7272be10900"         # 16B, R/W
CHAR_SYNC_MODE = "27763b31-999c-4d6a-9fc4-c7272be10900"          # 9B, R/W (Big-Endian Timestamps)
CHAR_DEVICE_NAME = "27763b40-999c-4d6a-9fc4-c7272be10900"        # 8B, R/W (1B length + 7B ASCII)

TX_POWER_MAP = {
    0: "+4 dBm",
    1: "0 dBm",
    2: "-4 dBm",
    3: "-8 dBm",
    4: "-12 dBm",
    5: "-16 dBm",
    7: "-30 dBm",
}
REV_TX_POWER_MAP = {v: k for k, v in TX_POWER_MAP.items()}


# ============================================================================
# BROADCAST & DATA RECORD PARSERS
# ============================================================================

def parse_broadcast_data(data: bytes) -> Optional[Dict]:
    """Decodes 20-byte Service Data payload from advertising packets."""
    payload = None
    idx = data.find(b"\xff\xcb\x11")
    if idx != -1 and len(data[idx:]) >= 19:
        payload = data[idx + 3:]
    elif data.startswith(b"\x11") and len(data) >= 17:
        payload = data[1:]

    if not payload or len(payload) < 16:
        return None

    hw_type = f"0x{payload[0:2].hex().upper()}"
    fw_ver = f"v{payload[2]}"
    device_id = payload[3:7].hex().upper()
    battery_pct = payload[7]

    # Temperature (2 bytes, Big-Endian)
    raw_temp = struct.unpack(">H", payload[9:11])[0]
    temp_abnormal = bool(raw_temp & 0x8000)
    temp_neg = bool(raw_temp & 0x4000)
    temp_val = (raw_temp & 0x3FFF) * 0.01
    temperature = -temp_val if temp_neg else temp_val

    # Humidity (2 bytes, Big-Endian)
    raw_hum = struct.unpack(">H", payload[11:13])[0]
    hum_abnormal = bool(raw_hum & 0x8000)
    humidity = (raw_hum & 0x7FFF) * 0.01

    alarm = payload[15]
    low_batt = bool(alarm & 0x80)
    temp_overrun = bool(alarm & 0x40)

    alarm_str = "Normal"
    if low_batt and temp_overrun:
        alarm_str = "Low Batt + Over-Temp"
    elif low_batt:
        alarm_str = "Low Battery"
    elif temp_overrun:
        alarm_str = "Over-Temp"

    return {
        "hardware_type": hw_type,
        "firmware_version": fw_ver,
        "device_id": device_id,
        "battery_percent": battery_pct,
        "temperature_c": None if temp_abnormal else round(temperature, 2),
        "humidity_percent": None if hum_abnormal else round(humidity, 2),
        "low_power_alarm": low_batt,
        "temp_alarm": temp_overrun,
        "alarm_status_str": alarm_str,
    }


def decode_sample_24bit(chunk: bytes) -> Tuple[Optional[float], Optional[float]]:
    """
    Decodes the 24-bit packed sample:
    - 7-bit humidity: (byte0 >> 1) & 0x7F
    - 11-bit temperature: (raw24 >> 6) & 0x7FF
      * If raw >= 1250: temp = (raw - 2048) * 0.1 °C
      * Else: temp = raw * 0.1 °C
    - 6-bit reserve: raw24 & 0x3F
    """
    if len(chunk) < 3:
        return None, None
    raw24 = (chunk[0] << 16) | (chunk[1] << 8) | chunk[2]
    humidity = float((chunk[0] >> 1) & 0x7F)
    raw_temp = (raw24 >> 6) & 0x7FF
    temp = (raw_temp - 2048) * 0.1 if raw_temp >= 1250 else raw_temp * 0.1
    return round(temp, 1), round(humidity, 1)


def encode_password_bytes(pwd_str: str) -> bytes:
    """Encodes a 6-digit decimal password into raw bytes where each digit is 0x00 - 0x09."""
    cleaned = pwd_str.strip()
    if len(cleaned) != 6 or not cleaned.isdigit():
        raise ValueError("Password must be exactly 6 decimal digits (e.g., '000000' or '123456').")
    return bytes([int(char) for char in cleaned])


# ============================================================================
# BT04B COMPLETE GATT CLIENT DRIVER
# ============================================================================

class BT04BGattClient:
    """
    Implements the official BT04B Section 2 & 3 Connection, Authentication,
    Configuration, and Data Synchronization workflows.
    """

    def __init__(
        self,
        target_device: Union[str, BLEDevice],
        password: str = "000000",
        on_disconnected_cb: Optional[Callable[[], None]] = None,
        logger_func: Optional[Callable[[str], None]] = None,
    ):
        self.target_device = target_device
        self.address = target_device.address if isinstance(target_device, BLEDevice) else target_device.strip()
        self.password = password.strip()
        self.client: Optional[BleakClient] = None
        self._is_authenticated = False
        self._sync_done = asyncio.Event()
        self._external_on_disconnect = on_disconnected_cb
        self.log = logger_func or logger.info

    def _internal_disconnect_handler(self, client: BleakClient):
        self._is_authenticated = False
        self.log(f"[GATT] Connection closed by device {self.address}.")
        if self._external_on_disconnect:
            self._external_on_disconnect()

    def _services_changed_handler(self, client: BleakClient):
        self.log(f"[GATT WARN] Services re-enumerated on {self.address}. Refreshing cache...")

    async def _safe_write(self, char_uuid: str, data: bytes):
        """Writes to characteristic adapting dynamically to write-with-response vs write-command."""
        char = self.client.services.get_characteristic(char_uuid)
        if not char:
            raise BleakError(f"Characteristic {char_uuid} not present in GATT table.")
        use_response = "write" in char.properties
        await self.client.write_gatt_char(char, data, response=use_response)
        await asyncio.sleep(0.08)  # Inter-command pacing delay

    async def _safe_read(self, char_uuid: str) -> bytearray:
        """Reads characteristic with inter-command pacing."""
        val = await self.client.read_gatt_char(char_uuid)
        await asyncio.sleep(0.08)
        return val

    # ------------------------------------------------------------------------
    # STEP 1 & 2: CONNECTION & MANDATORY AUTHENTICATION
    # ------------------------------------------------------------------------
    async def connect_and_authenticate(self, timeout: float = 14.0, max_retries: int = 2):
        """
        Executes Step 1 (GATT Link & Service Discovery) and Step 2 (Mandatory Password Write).
        Directly uses cached BLEDevice to eliminate Windows secondary scan dropouts.
        """
        last_error = None
        self._is_authenticated = False

        for attempt in range(1, max_retries + 1):
            try:
                self.log(f"[GATT] Step 1/3: Resolving Bluetooth handle for {self.address}...")

                if isinstance(self.target_device, BLEDevice):
                    target = self.target_device
                else:
                    device = await BleakScanner.find_device_by_address(self.address, timeout=4.0)
                    target = device if device else self.address

                self.client = BleakClient(
                    target,
                    disconnected_callback=self._internal_disconnect_handler,
                    timeout=timeout,
                )

                # Bind services changed handler to eliminate WinRT warnings
                if hasattr(self.client, "set_services_changed_callback"):
                    self.client.set_services_changed_callback(self._services_changed_handler)

                self.log(f"[GATT] Connecting (Attempt {attempt}/{max_retries})...")
                await self.client.connect()

                # Allow Windows WinRT GATT cache to populate
                await asyncio.sleep(0.5)

                self.log(f"[GATT] Step 2/3: Discovering Primary Service ({SERVICE_UUID})...")
                service = self.client.services.get_service(SERVICE_UUID)
                if not service:
                    self.log("[WARN] Primary service UUID not matched directly; probing available services...")
                    for s in self.client.services:
                        self.log(f"   Found Service: {s.uuid}")

                # Step 2: Password Authentication (Mandatory First Action)
                self.log(f"[GATT] Step 3/3: Transmitting 6-byte password to 0x3B13...")
                pwd_bytes = encode_password_bytes(self.password)
                await self._safe_write(CHAR_PASSWORD, pwd_bytes)

                # Settle delay for MCU authorization table
                await asyncio.sleep(0.3)

                self._is_authenticated = True
                self.log("[GATT] Authentication SUCCESSFUL. Session unlocked!")
                return

            except Exception as e:
                last_error = e
                self.log(f"[GATT ERROR] Handshake failed on attempt {attempt}: {e}")
                if self.client and self.client.is_connected:
                    try:
                        await self.client.disconnect()
                    except Exception:
                        pass
                if attempt < max_retries:
                    await asyncio.sleep(1.2)

        raise BleakError(f"GATT connection/authentication failed for {self.address}: {last_error}")

    async def disconnect(self):
        self._is_authenticated = False
        if self.client and self.client.is_connected:
            await self.client.disconnect()

    @property
    def is_connected(self) -> bool:
        return bool(self.client and self.client.is_connected and self._is_authenticated)

    # ------------------------------------------------------------------------
    # STEP 3A: STANDARD CONFIGURATION (READ / WRITE)
    # ------------------------------------------------------------------------
    async def read_stored_data_count(self) -> int:
        """Reads Data Count Characteristic (0x27763B18..., uint16 LE)."""
        data = await self._safe_read(CHAR_STORED_COUNT)
        return struct.unpack("<H", data[:2])[0]

    async def read_all_configurations(self) -> Dict:
        """Reads complete configuration profile using Little-Endian format."""
        dev_id = (await self._safe_read(CHAR_DEVICE_ID)).hex().upper()
        mod_raw = await self._safe_read(CHAR_MODEL_VERSION)
        hw_type = f"0x{mod_raw[0:2].hex().upper()}"
        fw_ver = f"v{mod_raw[2]}"

        name_raw = await self._safe_read(CHAR_DEVICE_NAME)
        dev_name = name_raw[1 : 1 + name_raw[0]].decode("ascii", errors="ignore") if name_raw else ""

        b_int = struct.unpack("<H", await self._safe_read(CHAR_BROADCAST_INTERVAL))[0]
        tx_p = (await self._safe_read(CHAR_TX_POWER))[0]
        acq_int = struct.unpack("<I", await self._safe_read(CHAR_ACQ_INTERVAL))[0]

        s_raw = await self._safe_read(CHAR_STORAGE_INTERVAL)
        s_norm, s_alarm = struct.unpack("<HH", s_raw[:4])

        alm_raw = await self._safe_read(CHAR_ALARM_PARAM)
        t_low, t_high = struct.unpack("<bb", alm_raw[:2])

        utc_raw = await self._safe_read(CHAR_UTC_TIME)
        utc_dt = datetime.datetime(
            2000 + utc_raw[0], utc_raw[1], utc_raw[2], utc_raw[3], utc_raw[4], utc_raw[5], tzinfo=datetime.timezone.utc
        )

        stored_cnt = await self.read_stored_data_count()
        rec_stat = (await self._safe_read(CHAR_RECORD_CONTROL))[0]

        return {
            "mac_address": self.address,
            "device_id": dev_id,
            "hw_type": hw_type,
            "fw_ver": fw_ver,
            "dev_name": dev_name,
            "broadcast_int": b_int,
            "tx_power_code": tx_p,
            "tx_power_str": TX_POWER_MAP.get(tx_p, f"Unknown ({tx_p})"),
            "acq_interval": acq_int,
            "storage_norm": s_norm,
            "storage_alarm": s_alarm,
            "alarm_low": t_low,
            "alarm_high": t_high,
            "utc_time": utc_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "stored_count": stored_cnt,
            "recording_active": rec_stat == 1,
        }

    async def sync_clock_utc(self) -> datetime.datetime:
        """Sets device UTC time (0x27763B20...) based on laptop UTC clock."""
        now = datetime.datetime.now(datetime.timezone.utc)
        payload = bytes([now.year - 2000, now.month, now.day, now.hour, now.minute, now.second])
        await self._safe_write(CHAR_UTC_TIME, payload)
        return now

    async def set_recording_state(self, active: bool):
        """Starts (1) or stops (0) logger recording (0x27763B22...)."""
        val = 1 if active else 0
        await self._safe_write(CHAR_RECORD_CONTROL, bytes([val]))

    async def write_configurations(self, config: Dict):
        """Writes configuration settings using appropriate Little-Endian payload packing."""
        if "device_id" in config and config["device_id"]:
            id_bytes = bytes.fromhex(config["device_id"])
            if len(id_bytes) == 4:
                await self._safe_write(CHAR_DEVICE_ID, id_bytes)
        if "broadcast_int" in config:
            await self._safe_write(CHAR_BROADCAST_INTERVAL, struct.pack("<H", config["broadcast_int"]))
        if "tx_power_code" in config:
            await self._safe_write(CHAR_TX_POWER, bytes([config["tx_power_code"]]))
        if "acq_interval" in config:
            await self._safe_write(CHAR_ACQ_INTERVAL, struct.pack("<I", config["acq_interval"]))
        if "storage_norm" in config and "storage_alarm" in config:
            payload = struct.pack("<HH", config["storage_norm"], config["storage_alarm"])
            await self._safe_write(CHAR_STORAGE_INTERVAL, payload)
        if "alarm_low" in config and "alarm_high" in config:
            payload = struct.pack("<bb", config["alarm_low"], config["alarm_high"])
            await self._safe_write(CHAR_ALARM_PARAM, payload)
        if "dev_name" in config:
            raw_n = config["dev_name"].encode("ascii")[:7]
            payload = (bytes([len(raw_n)]) + raw_n).ljust(8, b"\x00")
            await self._safe_write(CHAR_DEVICE_NAME, payload)
        if "new_password" in config and config["new_password"]:
            new_pwd_bytes = encode_password_bytes(config["new_password"])
            await self._safe_write(CHAR_PASSWORD, new_pwd_bytes)
            self.password = config["new_password"]

    async def read_memory_block(self, block_num: int) -> bytes:
        uuid_map = {1: CHAR_STORAGE_R1, 2: CHAR_STORAGE_R2, 3: CHAR_STORAGE_R3, 4: CHAR_STORAGE_R4, 5: CHAR_STORAGE_R5}
        return await self._safe_read(uuid_map[block_num])

    async def write_memory_block(self, block_num: int, data: bytes):
        uuid_map = {1: CHAR_STORAGE_R1, 2: CHAR_STORAGE_R2, 3: CHAR_STORAGE_R3, 4: CHAR_STORAGE_R4, 5: CHAR_STORAGE_R5}
        max_lens = {1: 20, 2: 20, 3: 20, 4: 20, 5: 16}
        payload = data.ljust(max_lens[block_num], b"\x00")[: max_lens[block_num]]
        await self._safe_write(uuid_map[block_num], payload)

    # ------------------------------------------------------------------------
    # STEP 3B: HISTORICAL DATA - FAST READ MODE (SECTION 3.3)
    # ------------------------------------------------------------------------
    async def download_history_fast(
        self,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        start_ts: int = 0,
        end_ts: int = 0,
    ) -> List[Dict]:
        """
        Executes Fast Read Mode:
        1. Enables Notify on Synchronous Data Switch (0x27763B21...).
        2. Writes 9-byte payload to Synchronous Data Mode (0x27763B31...):
           start_time (4B BE) + end_time (4B BE) + mode 0x01 (1B).
        3. Decodes Start (2), Mid (1), Temp (0), and Stop (3) packets.
        """
        stored_count = await self.read_stored_data_count()
        self.log(f"[FAST READ] Flash reports {stored_count} stored sample(s).")
        if stored_count == 0:
            return []

        records: List[Dict] = []
        expected_records = stored_count
        current_time = 0
        current_interval = 0

        def notif_handler(_, data: bytearray):
            nonlocal expected_records, current_time, current_interval
            raw = bytes(data)
            if len(raw) < 2:
                return

            header = struct.unpack(">H", raw[:2])[0]
            pkt_type = (header >> 13) & 0x07
            payload = raw[2:]

            if pkt_type == 2:  # Start_Packet
                if len(payload) >= 2:
                    expected_records = struct.unpack(">H", payload[:2])[0]
                self.log(f"[FAST READ] Start Packet: Target {expected_records} records.")

            elif pkt_type == 1:  # Mid_Packet
                if len(payload) >= 8:
                    start_time = struct.unpack(">I", payload[:4])[0]
                    interval = struct.unpack(">I", payload[4:8])[0]
                    current_interval = interval
                    current_time = start_time
                    samples = payload[8:]
                else:
                    samples = payload
                
                for i in range(0, len(samples), 3):
                    chunk = samples[i : i + 3]
                    if len(chunk) < 3:
                        break
                    t, h = decode_sample_24bit(chunk)
                    records.append({
                        "index": len(records) + 1,
                        "timestamp": current_time,
                        "utc_datetime": datetime.datetime.fromtimestamp(current_time, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        "temperature": t,
                        "humidity": h,
                    })
                    current_time += current_interval
                    if progress_cb:
                        progress_cb(len(records), expected_records)

            elif pkt_type == 0:  # Temp_Packet
                for i in range(0, len(payload), 3):
                    chunk = payload[i : i + 3]
                    if len(chunk) < 3:
                        break
                    t, h = decode_sample_24bit(chunk)
                    records.append({
                        "index": len(records) + 1,
                        "timestamp": current_time,
                        "utc_datetime": datetime.datetime.fromtimestamp(current_time, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        "temperature": t,
                        "humidity": h,
                    })
                    current_time += current_interval
                    if progress_cb:
                        progress_cb(len(records), expected_records)

            elif pkt_type == 3:  # Stop_Packet
                if len(payload) >= 2:
                    actual_uploaded = struct.unpack(">H", payload[:2])[0]
                    total_pkts = struct.unpack(">H", payload[2:4])[0] if len(payload) >= 4 else 0
                    self.log(f"[FAST READ] Stop Packet: Uploaded {actual_uploaded} samples in {total_pkts} packets.")
                self._sync_done.set()

            # Early termination guard if all records received
            if expected_records > 0 and len(records) >= expected_records:
                self._sync_done.set()

        self._sync_done.clear()

        # Subscribe to notifications first to avoid missing packet #1
        await self.client.start_notify(CHAR_SYNC_SWITCH, notif_handler)

        # Write 9-byte sync mode payload (Big-Endian timestamps + mode 0x01)
        mode_payload = struct.pack(">IIB", start_ts, end_ts, 0x01)
        self.log(f"[FAST READ] Writing Mode 0x01 to 0x3B31: {mode_payload.hex().upper()}")
        await self._safe_write(CHAR_SYNC_MODE, mode_payload)

        try:
            # Extended 180s timeout to support ~10,000 records on Windows WinRT
            await asyncio.wait_for(self._sync_done.wait(), timeout=180.0)
        finally:
            await self.client.stop_notify(CHAR_SYNC_SWITCH)

        return records

    # ------------------------------------------------------------------------
    # STEP 3B: HISTORICAL DATA - SLOW READ MODE (SECTION 3.2)
    # ------------------------------------------------------------------------
    async def download_history_slow(
        self,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        start_ts: int = 0,
        end_ts: int = 0,
    ) -> List[Dict]:
        """
        Executes Slow Read Mode:
        1. Reads Data Count Characteristic (0x27763B18...).
        2. Sets Slow Mode (mode 0x00) on Synchronous Data Mode (0x27763B31...).
        3. Enables Notify on Synchronous Data Switch (0x27763B21...).
        4. Parses 7-byte record structure + 2-byte serial + 1-byte checksum.
        """
        stored_count = await self.read_stored_data_count()
        self.log(f"[SLOW READ] Flash reports {stored_count} stored sample(s).")
        if stored_count == 0:
            return []

        records: List[Dict] = []
        expected_records = stored_count

        def notif_handler(_, data: bytearray):
            nonlocal expected_records
            raw = bytes(data)
            if not raw:
                return

            if raw[0] == 0x2A and raw[-1] == 0x23:
                expected_records = struct.unpack(">H", raw[1:3])[0]
                self.log(f"[SLOW READ] Start Delimiter: {expected_records} records expected.")
                return
            elif raw[0] == 0x24 and raw[-1] == 0x23:
                self.log("[SLOW READ] End Delimiter received.")
                self._sync_done.set()
                return

            if len(raw) < 10:
                return

            calc_checksum = sum(raw[:-1]) & 0xFF
            pkt_checksum = raw[-1]
            if calc_checksum != pkt_checksum:
                self.log(f"[WARN] Checksum mismatch in slow packet (calc {calc_checksum} != pkt {pkt_checksum})")

            payload = raw[:-3]

            for i in range(0, len(payload), 7):
                chunk = payload[i : i + 7]
                if len(chunk) < 7:
                    break

                ts = struct.unpack(">I", chunk[:4])[0]
                t, h = decode_sample_24bit(chunk[4:7])

                records.append({
                    "index": len(records) + 1,
                    "timestamp": ts,
                    "utc_datetime": datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "temperature": t,
                    "humidity": h,
                })

                if progress_cb:
                    progress_cb(len(records), expected_records)

            if len(records) >= expected_records:
                self._sync_done.set()

        self._sync_done.clear()

        # Subscribe to notifications
        await self.client.start_notify(CHAR_SYNC_SWITCH, notif_handler)

        # Write Slow Mode config (0x00) to 0x3B31
        mode_payload = struct.pack(">IIB", start_ts, end_ts, 0x00)
        self.log(f"[SLOW READ] Writing Mode 0x00 to 0x3B31: {mode_payload.hex().upper()}")
        await self._safe_write(CHAR_SYNC_MODE, mode_payload)

        try:
            await asyncio.wait_for(self._sync_done.wait(), timeout=90.0)
        finally:
            await self.client.stop_notify(CHAR_SYNC_SWITCH)

        return records


# ============================================================================
# ASYNC BACKGROUND RUNNER
# ============================================================================

class AsyncLoopThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.loop = asyncio.new_event_loop()

    def run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro, callback=None, errback=None):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def done_handler(f):
            try:
                res = f.result()
                if callback:
                    callback(res)
            except Exception as exc:
                if errback:
                    errback(exc)
                else:
                    logging.exception("Async background error: %s", exc)

        future.add_done_callback(done_handler)


# ============================================================================
# GUI DASHBOARD APPLICATION
# ============================================================================

class BT04BDashboardApp(tk.Tk):
    """Full Windows Dashboard with Complete GATT Workflow and Real-Time Sniffer."""

    def __init__(self):
        super().__init__()
        self.title("BT04B BLE Data Logger - Official Protocol V2.0 GATT Dashboard")
        self.geometry("1180x840")
        self.minsize(1040, 740)

        self.async_worker = AsyncLoopThread()
        self.async_worker.start()

        self.driver: Optional[BT04BGattClient] = None
        self.scanned_devices: Dict[str, Dict] = {}
        self.downloaded_records: List[Dict] = []
        
        self.continuous_scanning = False
        self.scanner_instance: Optional[BleakScanner] = None
        self._scanner_stop_event = asyncio.Event()

        self.last_rx_timestamp: Optional[float] = None
        self.last_rx_dev_info: str = "None"

        self._apply_theme()
        self._build_ui()

        self._update_clock_timer()
        self.after(500, self.start_continuous_scan)

    def _apply_theme(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TLabel", font=("Segoe UI", 9))
        style.configure("TButton", font=("Segoe UI", 9), padding=4)
        style.configure("Metric.TLabel", font=("Segoe UI", 16, "bold"), foreground="#0b5345")
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

    def _build_ui(self):
        # 1. Top Controls Bar
        top_bar = ttk.LabelFrame(self, text="Real-Time Controls & Connection", padding=8)
        top_bar.pack(side=tk.TOP, fill=tk.X, padx=10, pady=4)

        self.btn_live_scan = ttk.Button(top_bar, text="⏹ Stop Sniffer", width=13, command=self.toggle_continuous_scan)
        self.btn_live_scan.grid(row=0, column=0, padx=3, pady=2)

        ttk.Button(top_bar, text="➕ Find by ID", width=12, command=self.on_find_by_id_clicked).grid(row=0, column=1, padx=3)
        ttk.Button(top_bar, text="⚡ Wake Up All", width=14, command=self.on_wake_up_all_clicked).grid(row=0, column=2, padx=3)

        ttk.Label(top_bar, text="Target:").grid(row=0, column=3, padx=4, sticky=tk.E)
        self.cb_devices = ttk.Combobox(top_bar, width=38, state="readonly")
        self.cb_devices.grid(row=0, column=4, padx=4)
        self.cb_devices.bind("<<ComboboxSelected>>", self.on_device_selected_changed)

        ttk.Label(top_bar, text="Password (6-digit):").grid(row=0, column=5, padx=4, sticky=tk.E)
        self.ent_pwd = ttk.Entry(top_bar, width=8, show="*")
        self.ent_pwd.insert(0, "000000")
        self.ent_pwd.grid(row=0, column=6, padx=4)

        self.btn_connect = ttk.Button(top_bar, text="🔗 Connect & Auth", command=self.on_connect_toggle)
        self.btn_connect.grid(row=0, column=7, padx=5)

        self.lbl_status = ttk.Label(top_bar, text="Sniffer: Active", foreground="#196f3d", font=("Segoe UI", 9, "bold"))
        self.lbl_status.grid(row=0, column=8, padx=6)

        # 2. Host Monitor Bar
        host_bar = ttk.Frame(self, padding=4)
        host_bar.pack(side=tk.TOP, fill=tk.X, padx=10, pady=2)

        self.lbl_laptop_clock = ttk.Label(host_bar, text="💻 Laptop Local Time: --:--:--", font=("Segoe UI", 9, "bold"), foreground="#283747")
        self.lbl_laptop_clock.pack(side=tk.LEFT, padx=5)

        self.lbl_laptop_last_rx = ttk.Label(
            host_bar,
            text="⏱️ Last Message Received: Waiting for broadcast...",
            font=("Segoe UI", 9),
            foreground="#1a5276",
        )
        self.lbl_laptop_last_rx.pack(side=tk.LEFT, padx=15)

        # 3. Main Notebook Tabs
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=4)

        self.tab_broadcast = ttk.Frame(self.notebook, padding=10)
        self.tab_rx_log = ttk.Frame(self.notebook, padding=10)
        self.tab_overview = ttk.Frame(self.notebook, padding=10)
        self.tab_config = ttk.Frame(self.notebook, padding=10)
        self.tab_history = ttk.Frame(self.notebook, padding=10)
        self.tab_memory = ttk.Frame(self.notebook, padding=10)

        self.notebook.add(self.tab_broadcast, text="📡 Live Broadcast Monitor")
        self.notebook.add(self.tab_rx_log, text="💻 Laptop Reception Log")
        self.notebook.add(self.tab_overview, text="📊 Overview & Telemetry")
        self.notebook.add(self.tab_config, text="⚙️ Configurations (0x3B..)")
        self.notebook.add(self.tab_history, text="📈 Historical Records (Fast/Slow)")
        self.notebook.add(self.tab_memory, text="💾 Custom Flash Blocks")

        self._build_broadcast_tab()
        self._build_rx_log_tab()
        self._build_overview_tab()
        self._build_config_tab()
        self._build_history_tab()
        self._build_memory_tab()

        # 4. Activity Log Console
        log_frame = ttk.LabelFrame(self, text="Real-Time Event Log", padding=5)
        log_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=4)

        self.txt_log = tk.Text(log_frame, height=4, wrap=tk.WORD, font=("Consolas", 8), bg="#fcfcfc")
        self.txt_log.pack(side=tk.LEFT, fill=tk.X, expand=True)
        sb_log = ttk.Scrollbar(log_frame, command=self.txt_log.yview)
        sb_log.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt_log.config(yscrollcommand=sb_log.set)

    # ------------------------------------------------------------------------
    # TAB BUILDERS
    # ------------------------------------------------------------------------
    def _build_broadcast_tab(self):
        top_box = ttk.Frame(self.tab_broadcast)
        top_box.pack(fill=tk.X, pady=3)
        ttk.Label(top_box, text="Connectionless real-time advertising telemetry:", font=("Segoe UI", 9, "italic")).pack(side=tk.LEFT)

        cols = ("id", "mac", "temp", "hum", "batt", "alarm", "rssi", "pkts", "last_rx")
        self.tree_broadcast = ttk.Treeview(self.tab_broadcast, columns=cols, show="headings", height=18)
        self.tree_broadcast.heading("id", text="Device ID")
        self.tree_broadcast.heading("mac", text="MAC Address")
        self.tree_broadcast.heading("temp", text="Temperature")
        self.tree_broadcast.heading("hum", text="Humidity")
        self.tree_broadcast.heading("batt", text="Battery")
        self.tree_broadcast.heading("alarm", text="Alarm State")
        self.tree_broadcast.heading("rssi", text="Signal (RSSI)")
        self.tree_broadcast.heading("pkts", text="Packets")
        self.tree_broadcast.heading("last_rx", text="Last Laptop RX")

        for c in cols:
            self.tree_broadcast.column(c, anchor=tk.CENTER)
        self.tree_broadcast.column("mac", width=140)
        self.tree_broadcast.column("alarm", width=160)

        sb_y = ttk.Scrollbar(self.tab_broadcast, orient="vertical", command=self.tree_broadcast.yview)
        self.tree_broadcast.configure(yscrollcommand=sb_y.set)
        self.tree_broadcast.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb_y.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_broadcast.bind("<Double-1>", self.on_tree_device_double_click)

    def _build_rx_log_tab(self):
        f = ttk.Frame(self.tab_rx_log)
        f.pack(fill=tk.BOTH, expand=True)

        ctrl_bar = ttk.Frame(f)
        ctrl_bar.pack(fill=tk.X, pady=4)
        ttk.Button(ctrl_bar, text="🗑️ Clear Log", command=self.clear_rx_log).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl_bar, text="💾 Export Log", command=self.export_rx_log).pack(side=tk.LEFT, padx=5)

        cols = ("rx_time", "dev_id", "mac", "temp", "hum", "batt", "alarm", "rssi")
        self.tree_rx_log = ttk.Treeview(f, columns=cols, show="headings", height=16)
        for c in cols:
            self.tree_rx_log.heading(c, text=c.replace("_", " ").title())
            self.tree_rx_log.column(c, anchor=tk.CENTER)
        self.tree_rx_log.column("rx_time", width=180)

        sb = ttk.Scrollbar(f, orient="vertical", command=self.tree_rx_log.yview)
        self.tree_rx_log.configure(yscrollcommand=sb.set)
        self.tree_rx_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_overview_tab(self):
        cards = ttk.Frame(self.tab_overview)
        cards.pack(fill=tk.X, pady=5)

        c1 = ttk.LabelFrame(cards, text="Live Telemetry", padding=10)
        c1.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        self.lbl_card_temp = ttk.Label(c1, text="-- °C", style="Metric.TLabel")
        self.lbl_card_temp.pack(anchor=tk.W)
        self.lbl_card_hum = ttk.Label(c1, text="-- % RH", style="Metric.TLabel")
        self.lbl_card_hum.pack(anchor=tk.W)
        self.lbl_card_batt = ttk.Label(c1, text="Battery: -- %")
        self.lbl_card_batt.pack(anchor=tk.W, pady=2)
        self.lbl_card_alarm = ttk.Label(c1, text="Alarm: None", foreground="#1a5276")
        self.lbl_card_alarm.pack(anchor=tk.W, pady=2)

        c2 = ttk.LabelFrame(cards, text="Device Identity", padding=10)
        c2.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        self.lbl_dev_mac = ttk.Label(c2, text="MAC Address: --")
        self.lbl_dev_mac.pack(anchor=tk.W)
        self.lbl_dev_rssi = ttk.Label(c2, text="Signal (RSSI): --")
        self.lbl_dev_rssi.pack(anchor=tk.W)
        self.lbl_dev_id = ttk.Label(c2, text="Device ID: --")
        self.lbl_dev_id.pack(anchor=tk.W)
        self.lbl_dev_name = ttk.Label(c2, text="Name: --")
        self.lbl_dev_name.pack(anchor=tk.W)
        self.lbl_dev_hw_fw = ttk.Label(c2, text="Hardware / FW: -- / --")
        self.lbl_dev_hw_fw.pack(anchor=tk.W)

        c3 = ttk.LabelFrame(cards, text="Flash Memory & Logger State", padding=10)
        c3.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        self.lbl_rec_state = ttk.Label(c3, text="Logging State: Disconnected", font=("Segoe UI", 9, "bold"))
        self.lbl_rec_state.pack(anchor=tk.W)
        self.lbl_stored_samples = ttk.Label(c3, text="Stored Samples (0x3B18): --")
        self.lbl_stored_samples.pack(anchor=tk.W, pady=2)
        self.lbl_dev_clock = ttk.Label(c3, text="Device Clock (UTC): --")
        self.lbl_dev_clock.pack(anchor=tk.W, pady=2)

        actions = ttk.LabelFrame(self.tab_overview, text="Actions (Requires GATT Connection)", padding=10)
        actions.pack(fill=tk.X, pady=10)
        ttk.Button(actions, text="🔄 Read Full Configurations", command=self.on_refresh_status).pack(side=tk.LEFT, padx=5)
        ttk.Button(actions, text="⏱️ Sync RTC with Laptop UTC", command=self.on_sync_clock).pack(side=tk.LEFT, padx=5)
        ttk.Button(actions, text="▶️ Start Recording (0x3B22)", command=lambda: self.on_set_record(True)).pack(side=tk.LEFT, padx=5)
        ttk.Button(actions, text="⏹️ Stop Recording (0x3B22)", command=lambda: self.on_set_record(False)).pack(side=tk.LEFT, padx=5)

    def _build_config_tab(self):
        f = ttk.Frame(self.tab_config)
        f.pack(fill=tk.BOTH, expand=True)

        row = 0
        ttk.Label(f, text="Device ID (0x3B11, 4-byte Hex):").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.cfg_dev_id = ttk.Entry(f, width=16)
        self.cfg_dev_id.grid(row=row, column=1, sticky=tk.W, pady=4)

        row += 1
        ttk.Label(f, text="Broadcast Interval (0x3B12, 100-10000 ms, LE):").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.cfg_broadcast = ttk.Entry(f, width=16)
        self.cfg_broadcast.grid(row=row, column=1, sticky=tk.W, pady=4)

        row += 1
        ttk.Label(f, text="RF Transmit Power (0x3B14):").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.cfg_tx_power = ttk.Combobox(f, values=list(REV_TX_POWER_MAP.keys()), width=14, state="readonly")
        self.cfg_tx_power.grid(row=row, column=1, sticky=tk.W, pady=4)

        row += 1
        ttk.Label(f, text="Sensor Acquisition Interval (0x3B15, 1-100000 s, LE):").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.cfg_acq = ttk.Entry(f, width=16)
        self.cfg_acq.grid(row=row, column=1, sticky=tk.W, pady=4)

        row += 1
        ttk.Label(f, text="Normal Storage Interval (0x3B16, 10-3600 s, LE):").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.cfg_s_norm = ttk.Entry(f, width=16)
        self.cfg_s_norm.grid(row=row, column=1, sticky=tk.W, pady=4)

        row += 1
        ttk.Label(f, text="Alarm Storage Interval (0x3B16, 10-3600 s, LE):").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.cfg_s_alarm = ttk.Entry(f, width=16)
        self.cfg_s_alarm.grid(row=row, column=1, sticky=tk.W, pady=4)

        row += 1
        ttk.Label(f, text="Low Temp Alarm Threshold (0x3B19, -20 to 60 °C):").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.cfg_t_low = ttk.Entry(f, width=16)
        self.cfg_t_low.grid(row=row, column=1, sticky=tk.W, pady=4)

        row += 1
        ttk.Label(f, text="High Temp Alarm Threshold (0x3B19, -20 to 60 °C):").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.cfg_t_high = ttk.Entry(f, width=16)
        self.cfg_t_high.grid(row=row, column=1, sticky=tk.W, pady=4)

        row += 1
        ttk.Label(f, text="Device Name (0x3B40, max 7 ASCII):").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.cfg_name = ttk.Entry(f, width=16)
        self.cfg_name.grid(row=row, column=1, sticky=tk.W, pady=4)

        row += 1
        ttk.Label(f, text="Change Password (0x3B13, 6-digit decimal):").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.cfg_new_pwd = ttk.Entry(f, width=16)
        self.cfg_new_pwd.grid(row=row, column=1, sticky=tk.W, pady=4)

        row += 1
        btn_box = ttk.Frame(f)
        btn_box.grid(row=row, column=0, columnspan=2, pady=15, sticky=tk.W)
        ttk.Button(btn_box, text="💾 Write Configurations", command=self.on_apply_config).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_box, text="🔄 Reload Settings", command=self.on_refresh_status).pack(side=tk.LEFT, padx=5)

    def _build_history_tab(self):
        ctrl = ttk.Frame(self.tab_history)
        ctrl.pack(fill=tk.X, pady=5)

        ttk.Button(ctrl, text="⬇️ Fast Read Mode (0x3B31=0x01)", command=lambda: self.on_download_history(mode="fast")).pack(side=tk.LEFT, padx=4)
        ttk.Button(ctrl, text="⬇️ Slow Read Mode (Section 3.2)", command=lambda: self.on_download_history(mode="slow")).pack(side=tk.LEFT, padx=4)

        self.pbar = ttk.Progressbar(ctrl, orient="horizontal", mode="determinate", length=220)
        self.pbar.pack(side=tk.LEFT, padx=10)

        self.lbl_down_status = ttk.Label(ctrl, text="Ready.")
        self.lbl_down_status.pack(side=tk.LEFT, padx=5)

        ttk.Button(ctrl, text="Export CSV", command=self.on_export_csv).pack(side=tk.RIGHT, padx=4)
        ttk.Button(ctrl, text="Export JSON", command=self.on_export_json).pack(side=tk.RIGHT, padx=4)

        cols = ("idx", "datetime", "timestamp", "temp", "hum")
        self.tree_history = ttk.Treeview(self.tab_history, columns=cols, show="headings", height=16)
        self.tree_history.heading("idx", text="#")
        self.tree_history.heading("datetime", text="UTC Date/Time")
        self.tree_history.heading("timestamp", text="Unix Timestamp")
        self.tree_history.heading("temp", text="Temperature (°C)")
        self.tree_history.heading("hum", text="Humidity (% RH)")

        for c in cols:
            self.tree_history.column(c, anchor=tk.CENTER)

        sb_y = ttk.Scrollbar(self.tab_history, orient="vertical", command=self.tree_history.yview)
        self.tree_history.configure(yscrollcommand=sb_y.set)
        self.tree_history.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb_y.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_memory_tab(self):
        f = ttk.Frame(self.tab_memory)
        f.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        ttk.Label(f, text="Blocks 1-4: 20 bytes each | Block 5: 16 bytes", font=("Segoe UI", 9, "italic")).grid(
            row=0, column=0, columnspan=3, sticky=tk.W, pady=5
        )

        ttk.Label(f, text="Select Block (1-5):").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.cb_mem_block = ttk.Combobox(f, values=[1, 2, 3, 4, 5], state="readonly", width=8)
        self.cb_mem_block.current(0)
        self.cb_mem_block.grid(row=1, column=1, sticky=tk.W, pady=5)

        ttk.Button(f, text="Read Block", command=self.on_read_memory).grid(row=1, column=2, padx=10)

        ttk.Label(f, text="Block Content:").grid(row=2, column=0, sticky=tk.W, pady=5)
        self.ent_mem_text = ttk.Entry(f, width=40)
        self.ent_mem_text.grid(row=2, column=1, columnspan=2, sticky=tk.W, pady=5)

        ttk.Button(f, text="Write Text to Block", command=self.on_write_memory).grid(row=3, column=1, sticky=tk.W, pady=8)

    # ------------------------------------------------------------------------
    # LOGGING & SNIFFER OPERATIONS
    # ------------------------------------------------------------------------
    def log_message(self, text: str):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self.txt_log.insert(tk.END, f"[{now}] {text}\n")
        self.txt_log.see(tk.END)

    def _update_clock_timer(self):
        now = datetime.datetime.now()
        self.lbl_laptop_clock.config(text=f"💻 Laptop Local Time: {now.strftime('%H:%M:%S')}")
        if self.last_rx_timestamp:
            elapsed = time.time() - self.last_rx_timestamp
            rx_dt_str = datetime.datetime.fromtimestamp(self.last_rx_timestamp).strftime("%H:%M:%S.%f")[:-3]
            self.lbl_laptop_last_rx.config(
                text=f"⏱️ Last Message Received: {rx_dt_str} ({elapsed:.1f}s ago from {self.last_rx_dev_info})"
            )
        self.after(500, self._update_clock_timer)

    def toggle_continuous_scan(self):
        if self.continuous_scanning:
            self.async_worker.submit(self.stop_continuous_scan_coro())
        else:
            self.start_continuous_scan()

    def start_continuous_scan(self):
        if self.continuous_scanning:
            return
        self.continuous_scanning = True
        self.btn_live_scan.config(text="⏹ Stop Sniffer")
        self.lbl_status.config(text="Sniffer: Active", foreground="#196f3d")
        self.log_message("Continuous Active Broadcast Sniffer STARTED.")

        async def scan_loop():
            def cb(device: BLEDevice, adv: AdvertisementData):
                p = None
                for _, s_data in adv.service_data.items():
                    p = parse_broadcast_data(s_data)
                    if p:
                        break
                if not p and adv.manufacturer_data:
                    for _, m_data in adv.manufacturer_data.items():
                        p = parse_broadcast_data(m_data)
                        if p:
                            break
                name = device.name or adv.local_name or ""
                if p or "BT04" in name:
                    self.after(0, self._on_broadcast_received, device, p or {}, adv.rssi)

            self._scanner_stop_event.clear()
            self.scanner_instance = BleakScanner(detection_callback=cb, scanning_mode="active")
            await self.scanner_instance.start()
            await self._scanner_stop_event.wait()
            await self.scanner_instance.stop()
            self.scanner_instance = None

        self.async_worker.submit(scan_loop(), errback=lambda e: self.after(0, self.log_message, f"Sniffer error: {e}"))

    # RADIO RELEASE DELAY: 1.5 SECONDS TO PREVENT WINRT ADAPTER LOCKUPS
    async def stop_continuous_scan_coro(self):
        if not self.continuous_scanning:
            return
        self.continuous_scanning = False
        self._scanner_stop_event.set()

        # 1.5-second buffer ensures Windows radio completes HCI teardown
        await asyncio.sleep(1.5)

        self.after(0, lambda: [
            self.btn_live_scan.config(text="▶ Start Sniffer"),
            self.lbl_status.config(text="Sniffer: Paused", foreground="#78281f"),
            self.log_message("Continuous Broadcast Sniffer STOPPED."),
        ])

    def _on_broadcast_received(self, device: BLEDevice, parsed: Dict, rssi: int):
        rx_time = datetime.datetime.now()
        rx_time_str = rx_time.strftime("%H:%M:%S.%f")[:-3]
        self.last_rx_timestamp = rx_time.timestamp()

        addr = device.address
        d_id = parsed.get("device_id", "--------")
        self.last_rx_dev_info = f"[ID: {d_id}] ({addr})"

        if addr in self.scanned_devices:
            entry = self.scanned_devices[addr]
            entry["packet_count"] += 1
            entry["last_seen"] = rx_time_str
            entry["rssi"] = rssi
            entry["device"] = device  # Refresh cached BLEDevice handle
            if parsed:
                entry["parsed"] = parsed
        else:
            entry = {
                "device": device,
                "parsed": parsed,
                "rssi": rssi,
                "packet_count": 1,
                "last_seen": rx_time_str,
            }
            self.scanned_devices[addr] = entry
            self._update_combobox_values()

        t_val = f"{entry['parsed'].get('temperature_c')} °C" if entry['parsed'].get('temperature_c') is not None else "--"
        h_val = f"{entry['parsed'].get('humidity_percent')} %" if entry['parsed'].get('humidity_percent') is not None else "--"
        b_val = f"{entry['parsed'].get('battery_percent', '--')} %"
        alarm = entry["parsed"].get("alarm_status_str", "Normal")
        rssi_str = f"{rssi} dBm"
        pkts = str(entry["packet_count"])

        if self.tree_broadcast.exists(addr):
            self.tree_broadcast.item(addr, values=(d_id, addr, t_val, h_val, b_val, alarm, rssi_str, pkts, rx_time_str))
        else:
            self.tree_broadcast.insert("", tk.END, iid=addr, values=(d_id, addr, t_val, h_val, b_val, alarm, rssi_str, pkts, rx_time_str))

        self.tree_rx_log.insert("", 0, values=(rx_time_str, d_id, addr, t_val, h_val, b_val, alarm, rssi_str))
        children = self.tree_rx_log.get_children()
        if len(children) > 300:
            self.tree_rx_log.delete(children[-1])

        sel = self.cb_devices.get()
        if sel and addr in sel:
            self._update_cards_from_broadcast(addr, entry)

    def _update_cards_from_broadcast(self, addr: str, entry: Dict):
        parsed = entry["parsed"]
        t = parsed.get("temperature_c")
        h = parsed.get("humidity_percent")
        b = parsed.get("battery_percent")
        self.lbl_card_temp.config(text=f"{t} °C" if t is not None else "-- °C")
        self.lbl_card_hum.config(text=f"{h} % RH" if h is not None else "-- % RH")
        self.lbl_card_batt.config(text=f"Battery: {b} %" if b is not None else "Battery: -- %")
        self.lbl_card_alarm.config(text=f"Alarm: {parsed.get('alarm_status_str', 'Normal')}")
        self.lbl_dev_mac.config(text=f"MAC Address: {addr}")
        self.lbl_dev_rssi.config(text=f"Signal (RSSI): {entry['rssi']} dBm")
        self.lbl_dev_id.config(text=f"Device ID: {parsed.get('device_id', '--')}")
        self.lbl_dev_hw_fw.config(text=f"HW: {parsed.get('hardware_type', '--')} | FW: {parsed.get('firmware_version', '--')}")

    def _update_combobox_values(self):
        items = []
        for addr, info in self.scanned_devices.items():
            d_id = info["parsed"].get("device_id", "--------")
            name = info["device"].name or "BT04B"
            items.append(f"[ID: {d_id}] {name} ({addr})")
        self.cb_devices["values"] = items
        if items and not self.cb_devices.get():
            self.cb_devices.current(0)

    def on_device_selected_changed(self, event=None):
        sel = self.cb_devices.get()
        if not sel:
            return
        addr = sel.split("(")[1].split(")")[0]
        if addr in self.scanned_devices:
            self._update_cards_from_broadcast(addr, self.scanned_devices[addr])

    def on_tree_device_double_click(self, event):
        item = self.tree_broadcast.focus()
        if item and item in self.scanned_devices:
            for idx, val in enumerate(self.cb_devices["values"]):
                if item in val:
                    self.cb_devices.current(idx)
                    self._update_cards_from_broadcast(item, self.scanned_devices[item])
                    self.notebook.select(self.tab_overview)
                    break

    def clear_rx_log(self):
        for item in self.tree_rx_log.get_children():
            self.tree_rx_log.delete(item)

    def export_rx_log(self):
        rows = [self.tree_rx_log.item(item)["values"] for item in self.tree_rx_log.get_children()]
        if not rows:
            messagebox.showinfo("Export", "No log items to export.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV Files", "*.csv")])
        if path:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Laptop_RX_Time", "Device_ID", "MAC_Address", "Temperature", "Humidity", "Battery", "Alarm", "RSSI"])
                writer.writerows(rows)
            messagebox.showinfo("Exported", f"Reception log saved to {path}")

    def on_find_by_id_clicked(self):
        target = simpledialog.askstring("Add / Find by ID", "Enter 4-Byte Device ID or MAC Address:", parent=self)
        if not target:
            return
        clean_target = target.replace("0x", "").replace(":", "").replace("-", "").replace(" ", "").upper()
        for addr, info in self.scanned_devices.items():
            dev_id = info["parsed"].get("device_id", "")
            mac_clean = addr.replace(":", "").upper()
            if dev_id == clean_target or mac_clean == clean_target:
                for idx, val in enumerate(self.cb_devices["values"]):
                    if addr in val:
                        self.cb_devices.current(idx)
                        self._update_cards_from_broadcast(addr, info)
                        messagebox.showinfo("Located", f"Device matched:\n{addr}")
                        return
        messagebox.showinfo("Listening", f"Device ID '{clean_target}' not yet heard. Sniffer is listening.")

    # ------------------------------------------------------------------------
    # STEP 1 & 2: CONNECT & AUTHENTICATE BUTTON HANDLER
    # ------------------------------------------------------------------------
    def on_connect_toggle(self):
        if self.driver and self.driver.is_connected:
            self.log_message("[GATT] Disconnecting...")
            self.async_worker.submit(self.driver.disconnect(), callback=lambda _: self.after(0, self._set_disconnected_ui))
        else:
            selection = self.cb_devices.get()
            if not selection:
                messagebox.showwarning("Warning", "Select a target device first.")
                return

            try:
                addr = selection.split("(")[1].split(")")[0].strip()
            except Exception:
                addr = selection.strip()

            pwd = self.ent_pwd.get().strip() or "000000"
            self.lbl_status.config(text="Connecting & Auth...", foreground="#b7950b")

            async def conn_sequence():
                # Await scanner teardown with the full 1.5s delay
                await self.stop_continuous_scan_coro()

                # Pass the cached BLEDevice handle directly to BleakClient
                if addr in self.scanned_devices and "device" in self.scanned_devices[addr]:
                    cached_device = self.scanned_devices[addr]["device"]
                else:
                    cached_device = addr

                self.driver = BT04BGattClient(
                    cached_device,
                    password=pwd,
                    on_disconnected_cb=lambda: self.after(0, self._set_disconnected_ui),
                    logger_func=lambda msg: self.after(0, self.log_message, msg),
                )
                await self.driver.connect_and_authenticate()
                return await self.driver.read_all_configurations()

            def on_connected(status: Dict):
                self._update_status_ui(status)
                self.btn_connect.config(text="❌ Disconnect")
                self.lbl_status.config(text="Connected (Auth OK)", foreground="#196f3d")
                self.log_message(f"[SUCCESS] Handshake verified with {addr}.")

            def on_error(exc: Exception):
                self._set_disconnected_ui()
                self.log_message(f"[FAILED] GATT handshake error: {exc}")
                messagebox.showerror(
                    "GATT Error",
                    f"Connection/Authentication failed:\n\n{exc}\n\n"
                    "Note: Ensure you press the physical button on the logger "
                    "to wake it into connectable mode before clicking connect."
                )

            self.async_worker.submit(
                conn_sequence(),
                callback=lambda st: self.after(0, on_connected, st),
                errback=lambda err: self.after(0, on_error, err),
            )

    def _set_disconnected_ui(self):
        self.btn_connect.config(text="🔗 Connect & Auth")
        self.lbl_status.config(text="Disconnected", foreground="#78281f")
        self.lbl_rec_state.config(text="Logging State: Disconnected")
        self.log_message("[GATT] Session ended.")
        self.start_continuous_scan()

    def _update_status_ui(self, st: Dict):
        self.lbl_dev_mac.config(text=f"MAC Address: {st['mac_address']}")
        self.lbl_dev_id.config(text=f"Device ID: {st['device_id']}")
        self.lbl_dev_name.config(text=f"Name: {st['dev_name']}")
        self.lbl_dev_hw_fw.config(text=f"HW: {st['hw_type']} | FW: {st['fw_ver']}")
        self.lbl_dev_clock.config(text=f"Clock (UTC): {st['utc_time']}")
        self.lbl_stored_samples.config(text=f"Stored Samples (0x3B18): {st['stored_count']}")

        if st["recording_active"]:
            self.lbl_rec_state.config(text="Logging State: ACTIVE (Recording)", foreground="#196f3d")
        else:
            self.lbl_rec_state.config(text="Logging State: STOPPED", foreground="#78281f")

        self.cfg_dev_id.delete(0, tk.END)
        self.cfg_dev_id.insert(0, st["device_id"])

        self.cfg_broadcast.delete(0, tk.END)
        self.cfg_broadcast.insert(0, str(st["broadcast_int"]))

        self.cfg_tx_power.set(st["tx_power_str"])

        self.cfg_acq.delete(0, tk.END)
        self.cfg_acq.insert(0, str(st["acq_interval"]))

        self.cfg_s_norm.delete(0, tk.END)
        self.cfg_s_norm.insert(0, str(st["storage_norm"]))

        self.cfg_s_alarm.delete(0, tk.END)
        self.cfg_s_alarm.insert(0, str(st["storage_alarm"]))

        self.cfg_t_low.delete(0, tk.END)
        self.cfg_t_low.insert(0, str(st["alarm_low"]))

        self.cfg_t_high.delete(0, tk.END)
        self.cfg_t_high.insert(0, str(st["alarm_high"]))

        self.cfg_name.delete(0, tk.END)
        self.cfg_name.insert(0, st["dev_name"])

    # ------------------------------------------------------------------------
    # CONFIGURATION & RECORDING ACTIONS
    # ------------------------------------------------------------------------
    def on_refresh_status(self):
        if not self.driver or not self.driver.is_connected:
            messagebox.showinfo("Info", "Connect & Authenticate first.")
            return
        self.async_worker.submit(
            self.driver.read_all_configurations(),
            callback=lambda st: self.after(0, self._update_status_ui, st),
            errback=lambda err: self.after(0, lambda: self.log_message(f"Error: {err}")),
        )

    def on_sync_clock(self):
        if not self.driver or not self.driver.is_connected:
            messagebox.showinfo("Info", "Connect & Authenticate first.")
            return
        self.async_worker.submit(
            self.driver.sync_clock_utc(),
            callback=lambda dt: self.after(0, lambda: self.log_message(f"RTC Clock synced to {dt} UTC")),
            errback=lambda err: self.after(0, lambda: messagebox.showerror("Error", str(err))),
        )

    def on_set_record(self, active: bool):
        if not self.driver or not self.driver.is_connected:
            messagebox.showinfo("Info", "Connect & Authenticate first.")
            return
        msg = "Starting recording will clear existing historical memory. Proceed?" if active else "Stop recording?"
        if not messagebox.askyesno("Confirm", msg):
            return
        self.async_worker.submit(
            self.driver.set_recording_state(active),
            callback=lambda _: self.after(0, self.on_refresh_status),
            errback=lambda err: self.after(0, lambda: messagebox.showerror("Error", str(err))),
        )

    def on_apply_config(self):
        if not self.driver or not self.driver.is_connected:
            messagebox.showinfo("Info", "Connect & Authenticate first.")
            return
        try:
            cfg = {
                "device_id": self.cfg_dev_id.get().strip().upper(),
                "broadcast_int": int(self.cfg_broadcast.get()),
                "tx_power_code": REV_TX_POWER_MAP[self.cfg_tx_power.get()],
                "acq_interval": int(self.cfg_acq.get()),
                "storage_norm": int(self.cfg_s_norm.get()),
                "storage_alarm": int(self.cfg_s_alarm.get()),
                "alarm_low": int(self.cfg_t_low.get()),
                "alarm_high": int(self.cfg_t_high.get()),
                "dev_name": self.cfg_name.get().strip(),
                "new_password": self.cfg_new_pwd.get().strip(),
            }
        except Exception as e:
            messagebox.showerror("Invalid Input", f"Input error: {e}")
            return

        self.async_worker.submit(
            self.driver.write_configurations(cfg),
            callback=lambda _: self.after(0, lambda: [messagebox.showinfo("Success", "Configurations applied!"), self.on_refresh_status()]),
            errback=lambda err: self.after(0, lambda: messagebox.showerror("Error", str(err))),
        )

    # ------------------------------------------------------------------------
    # HISTORICAL DATA DOWNLOAD ACTIONS (FAST & SLOW MODES)
    # ------------------------------------------------------------------------
    def on_download_history(self, mode: str = "fast"):
        if not self.driver or not self.driver.is_connected:
            messagebox.showinfo("Info", "Connect & Authenticate first.")
            return

        self.downloaded_records.clear()
        for item in self.tree_history.get_children():
            self.tree_history.delete(item)

        self.pbar["value"] = 0
        self.lbl_down_status.config(text=f"Starting {mode.upper()} mode download...")

        def progress(current: int, total: int):
            def update():
                if total > 0:
                    pct = int((current / total) * 100)
                    self.pbar["value"] = pct
                    self.lbl_down_status.config(text=f"{current} / {total} records ({pct}%)")
            self.after(0, update)

        def on_complete(records: List[Dict]):
            self.downloaded_records = records
            self.lbl_down_status.config(text=f"Downloaded {len(records)} records ({mode.upper()}).")
            self.log_message(f"[{mode.upper()} READ] Completed: {len(records)} records extracted.")
            for r in records:
                self.tree_history.insert("", tk.END, values=(r["index"], r["utc_datetime"], r["timestamp"], r["temperature"], r["humidity"]))

        def on_fail(err):
            self.lbl_down_status.config(text="Download failed.")
            self.log_message(f"[{mode.upper()} READ] Error: {err}")

        coro = self.driver.download_history_fast(progress) if mode == "fast" else self.driver.download_history_slow(progress)
        self.async_worker.submit(coro, callback=lambda recs: self.after(0, on_complete, recs), errback=lambda err: self.after(0, on_fail, err))

    def on_export_csv(self):
        if not self.downloaded_records:
            messagebox.showinfo("Export", "No records to export.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV Files", "*.csv")])
        if path:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.downloaded_records[0].keys())
                writer.writeheader()
                writer.writerows(self.downloaded_records)
            messagebox.showinfo("Exported", f"Saved to {path}")

    def on_export_json(self):
        if not self.downloaded_records:
            messagebox.showinfo("Export", "No records to export.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON Files", "*.json")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.downloaded_records, f, indent=2)
            messagebox.showinfo("Exported", f"Saved to {path}")

    def on_read_memory(self):
        if not self.driver or not self.driver.is_connected:
            messagebox.showinfo("Info", "Connect & Authenticate first.")
            return
        blk = int(self.cb_mem_block.get())
        def on_read(data: bytes):
            text = data.decode("latin1", errors="replace").rstrip("\x00")
            self.ent_mem_text.delete(0, tk.END)
            self.ent_mem_text.insert(0, text)
            self.log_message(f"Read Block {blk}: {data.hex().upper()}")

        self.async_worker.submit(
            self.driver.read_memory_block(blk),
            callback=lambda d: self.after(0, on_read, d),
            errback=lambda err: self.after(0, lambda: messagebox.showerror("Read Error", str(err))),
        )

    def on_write_memory(self):
        if not self.driver or not self.driver.is_connected:
            messagebox.showinfo("Info", "Connect & Authenticate first.")
            return
        blk = int(self.cb_mem_block.get())
        raw = self.ent_mem_text.get().encode("utf-8")
        self.async_worker.submit(
            self.driver.write_memory_block(blk, raw),
            callback=lambda _: self.after(0, lambda: self.log_message(f"Block {blk} written.")),
            errback=lambda err: self.after(0, lambda: messagebox.showerror("Write Error", str(err))),
        )

    # ------------------------------------------------------------------------
    # BATCH WAKE-UP
    # ------------------------------------------------------------------------
    def on_wake_up_all_clicked(self):
        if not self.scanned_devices:
            messagebox.showwarning("No Devices", "No BT04B loggers detected in scan cache.")
            return
        count = len(self.scanned_devices)
        if not messagebox.askyesno("Wake Up All", f"Awaken and start recording on all {count} detected devices?"):
            return

        pwd = self.ent_pwd.get().strip() or "000000"

        async def wake_up_coro():
            await self.stop_continuous_scan_coro()
            results = []
            for addr, info in list(self.scanned_devices.items()):
                d_id = info["parsed"].get("device_id", addr)
                device_target = info.get("device", addr)
                client = BT04BGattClient(device_target, password=pwd, logger_func=lambda m: self.after(0, self.log_message, m))
                ok = False
                try:
                    await client.connect_and_authenticate(timeout=8.0)
                    await client.sync_clock_utc()
                    await client.set_recording_state(True)
                    await client.disconnect()
                    ok = True
                except Exception as e:
                    if client.client and client.client.is_connected:
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
                results.append((d_id, addr, ok))
                await asyncio.sleep(0.4)
            return results

        def on_done(results):
            self.start_continuous_scan()
            succeeded = sum(1 for r in results if r[2])
            messagebox.showinfo("Result", f"Successfully awakened: {succeeded}/{len(results)}")

        self.async_worker.submit(wake_up_coro(), callback=lambda r: self.after(0, on_done, r))


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    app = BT04BDashboardApp()
    app.mainloop()
