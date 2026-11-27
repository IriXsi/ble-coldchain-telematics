"""
MOKO SMART BLE Sensor Package (BXP-S Series / L02 / L02S / M2 / M2S).

Provides:
- bxps_frames: Decoding of all BXP-S broadcast frames (Sensor Info 0x80,
  Custom T&H 0x70, Device Info 0x40, Accel 0x60, iBeacon 0x50, Eddystone UID/URL/TLM).
- bxps_config: GATT configuration client and builders for all device settings.
- config_manager: Read all configs from device → JSON file, and apply JSON file → device.
- parser: Legacy/direct 0x80 Sensor Info parser.
"""

from .bxps_frames import (
    parse_apple_ibeacon,
    parse_bxps_accel_frame,
    parse_bxps_advertising,
    parse_bxps_device_info,
    parse_bxps_scan_response_ibeacon,
    parse_bxps_sensor_info,
    parse_bxps_th_frame,
    parse_eddystone_tlm,
    parse_eddystone_uid,
    parse_eddystone_url,
)
from .bxps_config import (
    BxpsConfigClient,
    BxpsError,
    build_accel_config,
    build_frame,
    build_ibeacon_slot_content,
    build_sensor_info_slot_content,
    build_slot_data,
    build_trigger_config,
    channel_mask_from_list,
    list_from_channel_mask,
    parse_accel_config,
    parse_history_packet,
    parse_response_frame,
    parse_slot_data,
)
from .parser import parse_moko_payload

__all__ = [
    # Frame decoders
    "parse_moko_payload",
    "parse_bxps_sensor_info",
    "parse_bxps_th_frame",
    "parse_bxps_device_info",
    "parse_bxps_accel_frame",
    "parse_bxps_scan_response_ibeacon",
    "parse_apple_ibeacon",
    "parse_eddystone_uid",
    "parse_eddystone_url",
    "parse_eddystone_tlm",
    "parse_bxps_advertising",
    # Config & GATT
    "BxpsConfigClient",
    "BxpsError",
    "build_frame",
    "parse_response_frame",
    "build_slot_data",
    "parse_slot_data",
    "build_trigger_config",
    "build_accel_config",
    "parse_accel_config",
    "build_sensor_info_slot_content",
    "build_ibeacon_slot_content",
    "channel_mask_from_list",
    "list_from_channel_mask",
    "parse_history_packet",
]
