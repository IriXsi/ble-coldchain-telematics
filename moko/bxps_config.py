"""
MOKO SMART BXP-S Series GATT Configuration Module.

Provides comprehensive configuration capabilities over BLE GATT for MOKO BXP-S
beacons (such as L02S, L02, M2, M2S) according to the BXP-S Protocol Specification
and MK Sensor Series APP User Manual.

BLE GATT Architecture:
  Primary Service: 0000AA00-0000-1000-8000-00805F9B34FB
    - AA01 (Write/Notify): General Configuration (slots, radio, switches, sensors)
    - AA04 (Write/Notify): Password Management (verify, change, toggle)
    - AA09 (Indicate):     T&H Historical Log Download

Frame Format:
  TX Request:  0xEA <Flag: 0x00=Read, 0x01=Write> <CMD> <Length> <Data...>
  RX Response: 0xEB <Flag> <CMD> <Length> <Data...> (0xAA = Success)
"""

import asyncio
import struct
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

# GATT Service & Characteristic UUIDs
SERVICE_UUID = "0000aa00-0000-1000-8000-00805f9b34fb"
CHAR_CONFIG = "0000aa01-0000-1000-8000-00805f9b34fb"     # General configuration
CHAR_PASSWORD = "0000aa04-0000-1000-8000-00805f9b34fb"   # Password management
CHAR_HISTORY = "0000aa09-0000-1000-8000-00805f9b34fb"    # Stored T&H logs (Indicate)

# Protocol Framing Bytes
HEAD_TX = 0xEA
HEAD_RX = 0xEB
FLAG_READ = 0x00
FLAG_WRITE = 0x01
RESP_SUCCESS = 0xAA
RESP_FAIL = 0x00

# BXP-S Command OpCodes
CMD_REMOTE_RESET = 0x20        # Remote restart / factory reset
CMD_DEVICE_INFO = 0x21         # Read firmware/hardware version, MAC, manufacture date
CMD_REMOTE_REMINDER = 0x22     # Trigger buzzer / LED reminder
CMD_BTN_RESET = 0x23           # Reset-by-button enable/disable
CMD_TRIGGER_LED = 0x24         # Trigger LED indicator blink enable/disable
CMD_BTN_POWEROFF = 0x25        # Power-off-by-button enable/disable
CMD_POWER_OFF = 0x26           # Remote power off device
CMD_SAVE_FACTORY = 0x27        # Save current settings as factory defaults
CMD_RESET_BATTERY = 0x28       # Reset battery usage calculation to 100%
CMD_ACCEL_CONFIG = 0x30        # 3-axis accelerometer sampling rate, scale, threshold
CMD_SLOT = 0x34                # Advertising slot configuration (Slots 1-3)
CMD_RF_CHANNEL = 0x35          # BLE advertising channel selection (37, 38, 39)
CMD_CONNECTABLE = 0x37         # Connectable status switch
CMD_PHY = 0x3A                 # PHY mode: 1M Legacy vs Coded PHY Long Range
CMD_TAGID_AUTOFILL = 0x3C      # Tag ID autofill with MAC address switch
CMD_COUNTER = 0x3E             # Seconds since boot counter
CMD_TIMESTAMP = 0x3F           # UTC timestamp clock synchronization
CMD_TH_STORE = 0x40            # T&H flash storage switch and interval
CMD_TH_SAMPLING = 0x41         # T&H sensor sampling interval
CMD_TH_CLEAR = 0x42            # Clear T&H flash storage
CMD_TH_COUNT = 0x43            # Read count of stored T&H records
CMD_PWD_VERIFY = 0x51          # Verify connection password
CMD_PWD_SET = 0x52             # Change connection password
CMD_PWD_ENABLE = 0x53          # Enable/disable password verification
CMD_BATT_ADV = 0x6C            # Battery ADV mode: percentage (1) vs voltage (2)

# Advertising slot types
ADV_TYPE_UID = 0x00
ADV_TYPE_URL = 0x10
ADV_TYPE_TLM = 0x20
ADV_TYPE_IBEACON = 0x50
ADV_TYPE_SENSOR_INFO = 0x80
ADV_TYPE_NO_DATA = 0xFF

ADV_TYPE_NAMES = {
    ADV_TYPE_UID: "Eddystone-UID",
    ADV_TYPE_URL: "Eddystone-URL",
    ADV_TYPE_TLM: "Eddystone-TLM",
    ADV_TYPE_IBEACON: "iBeacon",
    ADV_TYPE_SENSOR_INFO: "Sensor info",
    ADV_TYPE_NO_DATA: "No data",
}

ADV_NAME_TO_TYPE = {
    "uid": ADV_TYPE_UID,
    "url": ADV_TYPE_URL,
    "tlm": ADV_TYPE_TLM,
    "ibeacon": ADV_TYPE_IBEACON,
    "sensor_info": ADV_TYPE_SENSOR_INFO,
    "sensor": ADV_TYPE_SENSOR_INFO,
    "none": ADV_TYPE_NO_DATA,
    "no_data": ADV_TYPE_NO_DATA,
}

# Accelerometer configuration mappings
ACCEL_RATES = {0: "1Hz", 1: "10Hz", 2: "25Hz", 3: "50Hz", 4: "100Hz"}
ACCEL_RATE_TO_CODE = {"1hz": 0, "10hz": 1, "25hz": 2, "50hz": 3, "100hz": 4}
ACCEL_SCALES = {0: "±2g", 1: "±4g", 2: "±8g", 3: "±16g"}
ACCEL_SCALE_TO_CODE = {"2g": 0, "±2g": 0, "4g": 1, "±4g": 1, "8g": 2, "±8g": 2, "16g": 3, "±16g": 3}

DEFAULT_PASSWORD = "Moko4321"


class BxpsError(Exception):
    """Base exception for BXP-S communication and protocol errors."""
    pass


# ==============================================================================
# Frame Builders & Parsers (Pure binary, testable without BLE hardware)
# ==============================================================================

def build_frame(flag: int, cmd: int, data: bytes = b"") -> bytes:
    """Builds a BXP-S GATT request frame: [0xEA, flag, cmd, len(data), data...]."""
    return bytes([HEAD_TX, flag, cmd, len(data)]) + bytes(data)


def parse_response_frame(frame: bytes) -> Tuple[int, int, bytes]:
    """
    Parses a BXP-S GATT response frame: [0xEB, flag, cmd, len, data...].
    Returns (flag, cmd, data). Raises BxpsError on invalid frame.
    """
    if len(frame) < 4:
        raise BxpsError(f"Response frame too short: {frame.hex()}")
    if frame[0] != HEAD_RX:
        raise BxpsError(f"Invalid response header: 0x{frame[0]:02X} (expected 0xEB)")
    expected_len = frame[3]
    data = bytes(frame[4:])
    if len(data) < expected_len:
        raise BxpsError(f"Incomplete payload: got {len(data)} bytes, expected {expected_len}")
    return frame[1], frame[2], data[:expected_len]


def channel_mask_from_list(channels: List[int]) -> int:
    """Converts BLE channel list {37, 38, 39} to bitmask (CH37=1, CH38=2, CH39=4)."""
    mask = 0
    mapping = {37: 0x01, 38: 0x02, 39: 0x04}
    for ch in channels:
        if ch not in mapping:
            raise ValueError(f"Invalid advertising channel {ch}. Allowed: 37, 38, 39")
        mask |= mapping[ch]
    if mask == 0:
        raise ValueError("At least one advertising channel must be selected")
    return mask


def list_from_channel_mask(mask: int) -> List[int]:
    """Converts bitmask to list of BLE advertising channels."""
    channels = []
    if mask & 0x01: channels.append(37)
    if mask & 0x02: channels.append(38)
    if mask & 0x04: channels.append(39)
    return channels


def build_sensor_info_slot_content(device_name: str, tag_id_hex: str) -> bytes:
    """Encodes Sensor Info slot payload: name_len + name + tag_len + tag_id."""
    name_bytes = device_name.encode("ascii")
    if not (1 <= len(name_bytes) <= 20):
        raise ValueError("Device name must be 1-20 ASCII characters")
    tag_bytes = bytes.fromhex(tag_id_hex)
    if not (1 <= len(tag_bytes) <= 6):
        raise ValueError("Tag ID must be 1-6 bytes (2-12 hex digits)")
    return bytes([len(name_bytes)]) + name_bytes + bytes([len(tag_bytes)]) + tag_bytes


def build_ibeacon_slot_content(uuid_hex: str, major: int, minor: int) -> bytes:
    """Encodes iBeacon slot payload: UUID (16B) + Major (2B) + Minor (2B)."""
    clean_uuid = uuid_hex.replace("-", "").strip()
    uuid_bytes = bytes.fromhex(clean_uuid)
    if len(uuid_bytes) != 16:
        raise ValueError("iBeacon UUID must be 16 bytes (32 hex characters)")
    if not (0 <= major <= 65535 and 0 <= minor <= 65535):
        raise ValueError("Major and Minor must be uint16 (0-65535)")
    return uuid_bytes + struct.pack(">HH", major, minor)


def parse_slot_data(data: bytes) -> Dict[str, Any]:
    """
    Parses the data payload returned by CMD_SLOT (0x34).
    Layout: slot(1B), interval_ms(2B), duration_s(2B), standby_s(2B), rssi@0m(1B), adv_type(1B), content(rest).
    """
    if len(data) < 9:
        raise BxpsError(f"Slot data too short ({len(data)} bytes): {data.hex()}")
    slot_idx, interval_ms, duration_s, standby_s = struct.unpack(">BHHH", data[:7])
    rssi_dbm = struct.unpack("b", data[7:8])[0]
    adv_type = data[8]
    content = bytes(data[9:])

    parsed_content: Dict[str, Any] = {}
    if adv_type == ADV_TYPE_SENSOR_INFO and len(content) >= 2:
        try:
            nl = content[0]
            name = content[1:1 + nl].decode("ascii", errors="replace")
            tl = content[1 + nl]
            tag_id = content[2 + nl:2 + nl + tl].hex().upper()
            parsed_content = {"device_name": name, "tag_id": tag_id}
        except Exception:
            parsed_content = {"raw_content": content.hex()}
    elif adv_type == ADV_TYPE_IBEACON and len(content) >= 20:
        try:
            u = content[:16].hex()
            formatted_u = f"{u[:8]}-{u[8:12]}-{u[12:16]}-{u[16:20]}-{u[20:]}"
            maj, min_val = struct.unpack(">HH", content[16:20])
            parsed_content = {"uuid": formatted_u, "major": maj, "minor": min_val}
        except Exception:
            parsed_content = {"raw_content": content.hex()}

    return {
        "slot": slot_idx,
        "interval_ms": interval_ms,
        "duration_s": duration_s,
        "standby_s": standby_s,
        "rssi_0m_dbm": rssi_dbm,
        "adv_type": adv_type,
        "adv_type_name": ADV_TYPE_NAMES.get(adv_type, f"0x{adv_type:02X}"),
        "content_bytes": content,
        "content": parsed_content,
    }


def build_slot_data(
    slot_idx: int,
    interval_ms: int,
    duration_s: int,
    standby_s: int,
    rssi_0m_dbm: int,
    adv_type: int,
    content: bytes = b"",
) -> bytes:
    """Builds binary slot data for writing via CMD_SLOT (0x34)."""
    if slot_idx not in (0, 1, 2):
        raise ValueError("Slot index must be 0, 1, or 2 (SLOT1, SLOT2, SLOT3)")
    if not (100 <= interval_ms <= 10000 and interval_ms % 100 == 0):
        raise ValueError("Advertising interval must be 100-10000 ms in steps of 100 ms")
    if not (0 <= duration_s <= 65535 and 0 <= standby_s <= 65535):
        raise ValueError("Duration and Standby must be between 0 and 65535 seconds")
    if not (-100 <= rssi_0m_dbm <= 0):
        raise ValueError("RSSI@0m must be between -100 and 0 dBm")

    header = struct.pack(">BHHH", slot_idx, interval_ms, duration_s, standby_s)
    rssi_byte = struct.pack("b", rssi_0m_dbm)
    type_byte = bytes([adv_type])
    return header + rssi_byte + type_byte + content


def build_trigger_config(
    slot_idx: int,
    trigger_type: int,
    condition: int,
    adv_interval_ms: int,
    adv_duration_s: int,
    tx_power_dbm: int,
    static_verify_s: int = 0,
    lock_duration_s: int = 0,
) -> bytes:
    """
    Builds trigger configuration parameters for a slot (Section 3.4.2).
    Trigger types: 0=None, 1=Motion, 2=Temperature, 3=Humidity, 4=Magnetic.
    """
    if slot_idx not in (0, 1, 2):
        raise ValueError("Slot index must be 0, 1, or 2")
    return struct.pack(
        ">BBBHhHH",
        slot_idx,
        trigger_type,
        condition,
        adv_interval_ms,
        tx_power_dbm,
        static_verify_s,
        lock_duration_s,
    )


def build_accel_config(sampling_rate_code: int, full_scale_code: int, motion_threshold_mg: int) -> bytes:
    """
    Builds 3-axis accelerometer config payload (CMD_ACCEL_CONFIG 0x30).
    - sampling_rate: 0=1Hz, 1=10Hz, 2=25Hz, 3=50Hz, 4=100Hz
    - full_scale: 0=±2g, 1=±4g, 2=±8g, 3=±16g
    - motion_threshold: unit 0.1g (e.g. 100 mg = 1)
    """
    if sampling_rate_code not in ACCEL_RATES:
        raise ValueError(f"Invalid sampling rate code: {sampling_rate_code}")
    if full_scale_code not in ACCEL_SCALES:
        raise ValueError(f"Invalid full-scale code: {full_scale_code}")
    thresh_byte = max(1, min(255, motion_threshold_mg // 100))
    return bytes([sampling_rate_code, full_scale_code, thresh_byte])


def parse_accel_config(data: bytes) -> Dict[str, Any]:
    """Parses 3-axis accelerometer config response."""
    if len(data) < 3:
        raise BxpsError(f"Accel config data too short: {data.hex()}")
    rate_code, scale_code, thresh_byte = data[0], data[1], data[2]
    return {
        "sampling_rate_code": rate_code,
        "sampling_rate": ACCEL_RATES.get(rate_code, f"{rate_code}"),
        "full_scale_code": scale_code,
        "full_scale": ACCEL_SCALES.get(scale_code, f"{scale_code}"),
        "motion_threshold_mg": thresh_byte * 100,
    }


def parse_device_info_response(data: bytes) -> Dict[str, Any]:
    """
    Parses response from CMD_DEVICE_INFO (0x21).
    Extracts firmware version, hardware version, MAC address, and manufacture date.
    """
    if len(data) < 8:
        return {"raw": data.hex()}
    mac = ":".join(f"{b:02X}" for b in data[:6])
    fw_ver = f"V{data[6] >> 4}.{data[6] & 0x0F}.{data[7] if len(data) > 7 else 0}"
    info = {"mac_address": mac, "firmware_version": fw_ver}
    if len(data) >= 12:
        info["manufacture_date"] = data[8:12].hex()
    return info


def parse_history_packet(pkt: bytes) -> Tuple[int, int, List[Tuple[int, float, float]]]:
    """
    Parses a historical T&H indication packet from AA09.
    Returns (total_packets, packet_index, [(timestamp, temp_c, hum_rh), ...]).
    """
    if len(pkt) < 8 or pkt[0] not in (HEAD_RX, 0xEC) or pkt[2] != 0x80:
        raise BxpsError(f"Malformed history packet: {pkt.hex()}")
    total, idx, body_len = struct.unpack(">HHB", pkt[3:8])
    body = pkt[8:8 + body_len]
    records = []
    for i in range(0, len(body) - 7, 8):
        ts, temp_raw, hum_raw = struct.unpack(">IhH", body[i:i + 8])
        records.append((ts, temp_raw / 10.0, hum_raw / 10.0))
    return total, idx, records


# ==============================================================================
# Async BLE GATT Configuration Client
# ==============================================================================

class BxpsConfigClient:
    """
    High-level BLE GATT client for configuring MOKO BXP-S devices.
    Wraps BleakClient with structured BXP-S commands and error handling.
    """

    def __init__(self, bleak_client: Any, timeout: float = 6.0):
        self.client = bleak_client
        self.timeout = timeout
        self._notify_queue: asyncio.Queue = asyncio.Queue()
        self._history_queue: asyncio.Queue = asyncio.Queue()

    async def start(self) -> None:
        """Subscribes to general config and password notifications."""
        await self.client.start_notify(CHAR_CONFIG, self._on_config_notify)
        await self.client.start_notify(CHAR_PASSWORD, self._on_config_notify)

    def _on_config_notify(self, _, data: bytearray) -> None:
        self._notify_queue.put_nowait(bytes(data))

    async def _send_command(self, char_uuid: str, flag: int, cmd: int, data: bytes = b"") -> bytes:
        """Sends a frame and awaits matching response notification."""
        while not self._notify_queue.empty():
            self._notify_queue.get_nowait()

        tx_frame = build_frame(flag, cmd, data)
        await self.client.write_gatt_char(char_uuid, tx_frame, response=True)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise BxpsError(f"Timeout waiting for reply to command 0x{cmd:02X}")
            try:
                rx_frame = await asyncio.wait_for(self._notify_queue.get(), remaining)
            except asyncio.TimeoutError:
                raise BxpsError(f"Timeout waiting for reply to command 0x{cmd:02X}")
            resp_flag, resp_cmd, resp_data = parse_response_frame(rx_frame)
            if resp_flag == flag and resp_cmd == cmd:
                return resp_data

    async def read(self, cmd: int, char_uuid: str = CHAR_CONFIG, data: bytes = b"") -> bytes:
        """Executes a read command (flag=0x00)."""
        return await self._send_command(char_uuid, FLAG_READ, cmd, data)

    async def write(self, cmd: int, data: bytes = b"", char_uuid: str = CHAR_CONFIG) -> None:
        """Executes a write command (flag=0x01) and verifies 0xAA success acknowledgement."""
        resp = await self._send_command(char_uuid, FLAG_WRITE, cmd, data)
        if resp != bytes([RESP_SUCCESS]):
            raise BxpsError(f"Device rejected write command 0x{cmd:02X} (reply: {resp.hex()})")

    # ---- Password Management (AA04) -------------------------------------------

    async def is_password_required(self) -> bool:
        """Checks if password verification is currently enabled on device."""
        resp = await self.read(CMD_PWD_ENABLE, char_uuid=CHAR_PASSWORD)
        return resp[0] == 1

    async def authenticate(self, password: str = DEFAULT_PASSWORD) -> None:
        """Authenticates with the device (default: 'Moko4321')."""
        if not (1 <= len(password) <= 16):
            raise ValueError("Password must be 1-16 characters")
        try:
            await self.write(CMD_PWD_VERIFY, password.encode("ascii"), char_uuid=CHAR_PASSWORD)
        except BxpsError as e:
            raise BxpsError(
                f"Password authentication failed: {e}. Default is 'Moko4321' (case-sensitive)."
            )

    async def set_password(self, new_password: str) -> None:
        """Sets a new connection password (1-16 characters)."""
        if not (1 <= len(new_password) <= 16):
            raise ValueError("Password must be 1-16 ASCII characters")
        await self.write(CMD_PWD_SET, new_password.encode("ascii"), char_uuid=CHAR_PASSWORD)

    async def set_password_required(self, required: bool) -> None:
        """Enables or disables connection password verification."""
        await self.write(CMD_PWD_ENABLE, bytes([1 if required else 0]), char_uuid=CHAR_PASSWORD)

    # ---- Basic Quick Switches (AA01) -----------------------------------------

    async def get_connectable(self) -> bool:
        """Reads connectable status (True: connectable, False: non-connectable)."""
        return (await self.read(CMD_CONNECTABLE))[0] == 1

    async def set_connectable(self, connectable: bool) -> None:
        """Sets connectable status. Caution: setting to False requires hardware reset to reconnect!"""
        await self.write(CMD_CONNECTABLE, bytes([1 if connectable else 0]))

    async def get_trigger_led_enabled(self) -> bool:
        """Reads whether trigger LED indicator blink is enabled."""
        return (await self.read(CMD_TRIGGER_LED))[0] == 1

    async def set_trigger_led_enabled(self, enabled: bool) -> None:
        """Enables or disables LED blink when trigger event occurs."""
        await self.write(CMD_TRIGGER_LED, bytes([1 if enabled else 0]))

    async def get_tagid_autofill(self) -> bool:
        """Reads whether Tag ID is automatically filled with MAC address."""
        return (await self.read(CMD_TAGID_AUTOFILL))[0] == 1

    async def set_tagid_autofill(self, enabled: bool) -> None:
        """Enables or disables automatic Tag ID autofill with MAC address."""
        await self.write(CMD_TAGID_AUTOFILL, bytes([1 if enabled else 0]))

    async def get_button_poweroff_enabled(self) -> bool:
        """Reads whether turn-off-by-button is enabled (firmware polarity: 0=enabled, 1=disabled)."""
        return (await self.read(CMD_BTN_POWEROFF))[0] == 0

    async def set_button_poweroff_enabled(self, enabled: bool) -> None:
        """Enables or disables power off via physical button."""
        await self.write(CMD_BTN_POWEROFF, bytes([0 if enabled else 1]))

    async def get_button_reset_enabled(self) -> bool:
        """Reads whether factory-reset-by-button is enabled (firmware polarity: 1=enabled, 0=disabled)."""
        return (await self.read(CMD_BTN_RESET))[0] == 1

    async def set_button_reset_enabled(self, enabled: bool) -> None:
        """Enables or disables factory reset via physical button."""
        await self.write(CMD_BTN_RESET, bytes([1 if enabled else 0]))

    # ---- Radio & Hardware Settings (AA01) -----------------------------------

    async def get_phy_mode(self) -> str:
        """Reads PHY mode: 'legacy' (1M PHY) or 'long-range' (Coded PHY)."""
        code = (await self.read(CMD_PHY))[0]
        return "long-range" if code == 2 else "legacy"

    async def set_phy_mode(self, mode: str) -> None:
        """Sets PHY mode ('legacy' or 'long-range')."""
        val = 2 if mode.lower() in ("long-range", "coded", "coded_phy") else 1
        await self.write(CMD_PHY, bytes([val]))

    async def get_advertising_channels(self) -> List[int]:
        """Reads enabled BLE advertising channels (from 37, 38, 39)."""
        mask = (await self.read(CMD_RF_CHANNEL))[0]
        return list_from_channel_mask(mask)

    async def set_advertising_channels(self, channels: List[int]) -> None:
        """Sets enabled BLE advertising channels (subset of [37, 38, 39])."""
        mask = channel_mask_from_list(channels)
        await self.write(CMD_RF_CHANNEL, bytes([mask]))

    async def get_battery_adv_mode(self) -> str:
        """Reads battery broadcast mode: 'percentage' or 'voltage'."""
        code = (await self.read(CMD_BATT_ADV))[0]
        return "percentage" if code == 1 else "voltage"

    async def set_battery_adv_mode(self, mode: str) -> None:
        """Sets battery broadcast mode ('percentage' or 'voltage')."""
        code = 1 if mode.lower() in ("percentage", "percent", "%") else 2
        await self.write(CMD_BATT_ADV, bytes([code]))

    async def reset_battery_calculation(self) -> None:
        """Resets battery usage tracking to 100% after battery replacement."""
        await self.write(CMD_RESET_BATTERY, b"")

    async def trigger_remote_reminder(self, duration_s: int = 5) -> None:
        """Triggers device LED / buzzer reminder."""
        await self.write(CMD_REMOTE_REMINDER, bytes([duration_s]))

    async def remote_reset(self) -> None:
        """Remotely restarts / factory resets the beacon."""
        await self.write(CMD_REMOTE_RESET, b"")

    async def power_off(self) -> None:
        """Remotely powers off the beacon."""
        await self.write(CMD_POWER_OFF, b"")

    async def save_settings_as_factory(self) -> None:
        """Saves current configuration as permanent factory defaults."""
        await self.write(CMD_SAVE_FACTORY, b"")

    # ---- Device Clock & Time Sync (AA01) -------------------------------------

    async def get_device_time(self) -> int:
        """Reads device UTC timestamp."""
        return struct.unpack(">I", await self.read(CMD_TIMESTAMP))[0]

    async def sync_time(self, unix_timestamp: Optional[int] = None) -> None:
        """Synchronizes device clock with UTC Unix timestamp (defaults to current system time)."""
        ts = int(time.time()) if unix_timestamp is None else int(unix_timestamp)
        await self.write(CMD_TIMESTAMP, struct.pack(">I", ts))

    async def get_uptime_seconds(self) -> int:
        """Reads seconds elapsed since device boot."""
        return int.from_bytes(await self.read(CMD_COUNTER), "big")

    # ---- Sensor Configurations & Logging (AA01) ------------------------------

    async def get_accel_config(self) -> Dict[str, Any]:
        """Reads 3-axis accelerometer sampling rate, scale, and motion threshold."""
        return parse_accel_config(await self.read(CMD_ACCEL_CONFIG))

    async def set_accel_config(self, sampling_rate_hz: str, full_scale: str, threshold_mg: int = 100) -> None:
        """Configures 3-axis accelerometer parameters."""
        rate_code = ACCEL_RATE_TO_CODE.get(sampling_rate_hz.lower(), 1)
        scale_code = ACCEL_SCALE_TO_CODE.get(full_scale.lower(), 0)
        payload = build_accel_config(rate_code, scale_code, threshold_mg)
        await self.write(CMD_ACCEL_CONFIG, payload)

    async def get_th_sampling_interval(self) -> int:
        """Reads T&H sensor sampling interval in seconds."""
        return struct.unpack(">H", await self.read(CMD_TH_SAMPLING))[0]

    async def set_th_sampling_interval(self, seconds: int) -> None:
        """Sets T&H sensor sampling interval in seconds (1-65535 s)."""
        if not (1 <= seconds <= 65535):
            raise ValueError("Sampling interval must be 1-65535 seconds")
        await self.write(CMD_TH_SAMPLING, struct.pack(">H", seconds))

    async def get_th_storage_settings(self) -> Tuple[bool, int]:
        """Reads T&H flash storage settings: (enabled, interval_minutes)."""
        data = await self.read(CMD_TH_STORE)
        enabled = bool(data[0])
        interval_min = struct.unpack(">H", data[1:3])[0]
        return enabled, interval_min

    async def set_th_storage_settings(self, enabled: bool, interval_minutes: int = 0) -> None:
        """Sets T&H flash storage enable status and log interval (0 = log every sample)."""
        if not (0 <= interval_minutes <= 65535):
            raise ValueError("Storage interval must be 0-65535 minutes")
        payload = bytes([1 if enabled else 0]) + struct.pack(">H", interval_minutes)
        await self.write(CMD_TH_STORE, payload)

    async def get_stored_th_count(self) -> int:
        """Reads total count of stored T&H records in flash."""
        return int.from_bytes(await self.read(CMD_TH_COUNT), "big")

    async def clear_stored_th(self) -> None:
        """Erases all stored T&H records from flash memory."""
        await self.write(CMD_TH_CLEAR, b"")

    # ---- Advertising Slots (AA01) -------------------------------------------

    async def get_slot(self, slot_idx: int) -> Dict[str, Any]:
        """Reads advertising slot configuration (slot_idx: 0, 1, or 2)."""
        if slot_idx not in (0, 1, 2):
            raise ValueError("Slot index must be 0, 1, or 2")
        raw = await self.read(CMD_SLOT, data=bytes([slot_idx]))
        return parse_slot_data(raw)

    async def configure_slot(
        self,
        slot_idx: int,
        interval_ms: int,
        duration_s: int,
        standby_s: int,
        rssi_0m_dbm: int,
        adv_type: int,
        content: bytes = b"",
    ) -> None:
        """Configures all parameters of an advertising slot."""
        payload = build_slot_data(
            slot_idx=slot_idx,
            interval_ms=interval_ms,
            duration_s=duration_s,
            standby_s=standby_s,
            rssi_0m_dbm=rssi_0m_dbm,
            adv_type=adv_type,
            content=content,
        )
        await self.write(CMD_SLOT, payload)

    async def configure_sensor_info_slot(
        self,
        slot_idx: int,
        device_name: str,
        tag_id_hex: str,
        interval_ms: int = 1000,
        duration_s: int = 0,
        standby_s: int = 0,
        rssi_0m_dbm: int = -59,
    ) -> None:
        """Convenience method to configure a slot with Sensor Info broadcast (0x80)."""
        content = build_sensor_info_slot_content(device_name, tag_id_hex)
        await self.configure_slot(
            slot_idx=slot_idx,
            interval_ms=interval_ms,
            duration_s=duration_s,
            standby_s=standby_s,
            rssi_0m_dbm=rssi_0m_dbm,
            adv_type=ADV_TYPE_SENSOR_INFO,
            content=content,
        )

    async def read_all_settings(self) -> Dict[str, Any]:
        """Reads a comprehensive snapshot of all device settings."""
        settings: Dict[str, Any] = {}

        async def _safe(key: str, coro):
            try:
                settings[key] = await coro
            except Exception as e:
                settings[key] = f"Error: {e}"

        await _safe("password_required", self.is_password_required())
        await _safe("connectable", self.get_connectable())
        await _safe("trigger_led_enabled", self.get_trigger_led_enabled())
        await _safe("tagid_autofill", self.get_tagid_autofill())
        await _safe("button_poweroff", self.get_button_poweroff_enabled())
        await _safe("button_reset", self.get_button_reset_enabled())
        await _safe("phy_mode", self.get_phy_mode())
        await _safe("channels", self.get_advertising_channels())
        await _safe("battery_mode", self.get_battery_adv_mode())
        await _safe("device_time", self.get_device_time())
        await _safe("uptime_seconds", self.get_uptime_seconds())
        await _safe("th_sampling_interval_s", self.get_th_sampling_interval())
        await _safe("th_storage", self.get_th_storage_settings())
        await _safe("stored_th_count", self.get_stored_th_count())

        # Slots 1-3
        slots = []
        for i in range(3):
            try:
                slots.append(await self.get_slot(i))
            except Exception as e:
                slots.append({"slot": i, "error": str(e)})
        settings["slots"] = slots

        return settings
