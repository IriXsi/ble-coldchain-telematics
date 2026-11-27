"""
Comprehensive Unit Tests for MOKO BXP-S Series (L02S / L02 / M2 / M2S).

Verifies:
1. All broadcast advertisement and scan response decoders in moko.bxps_frames.
2. Binary frame builders and parsers in moko.bxps_config (GATT configuration).
3. Bundle decoder parse_bxps_advertising for multi-frame broadcasts.
"""

import struct
import unittest

from moko.bxps_frames import (
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
from moko.bxps_config import (
    CMD_ACCEL_CONFIG,
    CMD_BATT_ADV,
    CMD_BTN_POWEROFF,
    CMD_BTN_RESET,
    CMD_CONNECTABLE,
    CMD_PHY,
    CMD_PWD_ENABLE,
    CMD_PWD_SET,
    CMD_PWD_VERIFY,
    CMD_RF_CHANNEL,
    CMD_SLOT,
    CMD_TAGID_AUTOFILL,
    CMD_TH_CLEAR,
    CMD_TH_COUNT,
    CMD_TH_SAMPLING,
    CMD_TH_STORE,
    CMD_TIMESTAMP,
    FLAG_READ,
    FLAG_WRITE,
    build_accel_config,
    build_frame,
    build_ibeacon_slot_content,
    build_sensor_info_slot_content,
    build_slot_data,
    build_trigger_config,
    channel_mask_from_list,
    list_from_channel_mask,
    parse_accel_config,
    parse_device_info_response,
    parse_history_packet,
    parse_response_frame,
    parse_slot_data,
)


class TestBxpsBroadcastFrames(unittest.TestCase):
    """Tests all BXP-S advertising and scan response decoders."""

    def test_sensor_info_0x80_spec_vector(self):
        """Vendor spec vector from Table 5 (20.0°C, 31.1%RH, 3276mV, Tag ID: 000001)."""
        raw = bytes.fromhex("80 1C 0000 0002 003A 0076 03A7 00C8 0137 0CCC 000001")
        res = parse_bxps_sensor_info(raw)

        self.assertIsNotNone(res)
        self.assertEqual(res["frame_type"], "sensor_info")
        self.assertEqual(res["tag_id"], "000001")
        self.assertEqual(res["temperature_c"], 20.0)
        self.assertEqual(res["humidity_rh"], 31.1)
        self.assertEqual(res["battery_voltage_mv"], 3276)
        self.assertEqual(res["hall_trigger_count"], 0)
        self.assertEqual(res["motion_trigger_count"], 2)
        self.assertEqual(res["acceleration_mg"], {"x": 58, "y": 118, "z": 935})
        self.assertTrue(res["status_flags"]["door_closed"])
        self.assertFalse(res["status_flags"]["door_open"])
        self.assertFalse(res["status_flags"]["is_moving"])
        self.assertTrue(res["status_flags"]["is_static"])

    def test_sensor_info_negative_temp_and_door_open(self):
        """Verifies negative temperature (-12.5°C = 0xFF83), door open, moving, percentage battery."""
        # Temp: -125 (0xFF83), Humidity: 65.4% (654 = 0x028E), Batt: 85% (0x0055), Status: 0x03 (door open + moving)
        raw = bytes.fromhex("80 03 0005 0009 0000 0000 0000 FF83 028E 0055 AABBCCDDEEFF")
        res = parse_bxps_sensor_info(raw)

        self.assertIsNotNone(res)
        self.assertEqual(res["temperature_c"], -12.5)
        self.assertEqual(res["humidity_rh"], 65.4)
        self.assertEqual(res["battery_percentage"], 85)
        self.assertNotIn("battery_voltage_mv", res)
        self.assertTrue(res["status_flags"]["door_open"])
        self.assertTrue(res["status_flags"]["is_moving"])
        self.assertEqual(res["hall_trigger_count"], 5)
        self.assertEqual(res["motion_trigger_count"], 9)
        self.assertEqual(res["tag_id"], "AABBCCDDEEFF")

    def test_custom_th_frame_0x70(self):
        """Verifies Customized T&H Frame (0x70 on UUID 0xABFE)."""
        # [0x70, Ranging: -59 (0xC5), Interval: 10 (1000ms), Temp: 245 (24.5C = 0x00F5), Hum: 450 (45.0% = 0x01C2), Batt: 3000mV (0x0BB8), DevType: 0x03, MAC: C0:EE:40:01:02:03]
        payload = bytes.fromhex("70 C5 0A 00F5 01C2 0BB8 03 C0EE40010203")
        res = parse_bxps_th_frame(payload)

        self.assertIsNotNone(res)
        self.assertEqual(res["frame_id"], 0x70)
        self.assertEqual(res["ranging_dbm"], -59)
        self.assertEqual(res["adv_interval_ms"], 1000)
        self.assertEqual(res["temperature_c"], 24.5)
        self.assertEqual(res["humidity_rh"], 45.0)
        self.assertEqual(res["battery_voltage_mv"], 3000)
        self.assertEqual(res["device_type_desc"], "3-Axis and T&H")
        self.assertEqual(res["mac_address"], "C0:EE:40:01:02:03")

    def test_custom_device_info_0x40(self):
        """Verifies Customized Device Info Frame (0x40 on UUID 0xABFE)."""
        # [0x40, Ranging: -60 (0xC4), Interval: 10, Batt: 3100 (0x0C1C), Prop: 0x08 (pwd enabled, hall equipped), Switch: 0x01 (connectable), MAC: AA:BB:CC:DD:EE:FF, FW: 0x12 0x00 -> V1.2.0]
        payload = bytes.fromhex("40 C4 0A 0C1C 08 01 AABBCCDDEEFF 12 00")
        res = parse_bxps_device_info(payload)

        self.assertIsNotNone(res)
        self.assertEqual(res["frame_id"], 0x40)
        self.assertEqual(res["battery_voltage_mv"], 3100)
        self.assertEqual(res["ranging_dbm"], -60)
        self.assertEqual(res["adv_interval_ms"], 1000)
        self.assertEqual(res["firmware_version"], "V1.2.0")
        self.assertEqual(res["mac_address"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(res["properties"]["password_verification"], "Enabled")
        self.assertTrue(res["properties"]["hall_sensor_equipped"])
        self.assertTrue(res["switch_status"]["connectable"])
        self.assertFalse(res["switch_status"]["door_open"])

    def test_custom_accel_frame_0x60(self):
        """Verifies Customized 3-Axis Accel Frame (0x60 on UUID 0xABFE)."""
        # [0x60, Ranging: -55 (0xC9), Interval: 5 (500ms), Rate: 1 (10Hz), Scale: 0 (±2g), Thresh: 2 (0.2g), X: 50 (0x0032), Y: -50 (0xFFCE), Z: 980 (0x03D4), Batt: 3000 (0x0BB8), RFU: 0, MAC: 11:22:33:44:55:66]
        payload = bytes.fromhex("60 C9 05 01 00 02 0032 FFCE 03D4 0BB8 00 112233445566")
        res = parse_bxps_accel_frame(payload)

        self.assertIsNotNone(res)
        self.assertEqual(res["frame_id"], 0x60)
        self.assertEqual(res["adv_interval_ms"], 500)
        self.assertEqual(res["sampling_rate"], "10Hz")
        self.assertEqual(res["full_scale"], "±2g")
        self.assertEqual(res["motion_threshold_g"], 0.2)
        self.assertEqual(res["raw_accel"], {"x": 50, "y": -50, "z": 980})
        self.assertEqual(res["battery_voltage_mv"], 3000)
        self.assertEqual(res["mac_address"], "11:22:33:44:55:66")

    def test_scan_response_ibeacon_0x50(self):
        """Verifies MOKO-Defined iBeacon Scan Response (0x50 on UUID 0xABFE)."""
        # [0x50, RSSI@1m: -59 (0xC5), Interval: 10 (1000ms), UUID: 00..0F (16B), Major: 1001 (0x03E9), Minor: 2002 (0x07D2)]
        uuid_bytes = bytes(range(16))
        payload = bytes([0x50, 0xC5, 0x0A]) + uuid_bytes + struct.pack(">HH", 1001, 2002)
        res = parse_bxps_scan_response_ibeacon(payload)

        self.assertIsNotNone(res)
        self.assertEqual(res["rssi_1m_dbm"], -59)
        self.assertEqual(res["adv_interval_ms"], 1000)
        self.assertEqual(res["major"], 1001)
        self.assertEqual(res["minor"], 2002)
        self.assertEqual(res["uuid"], "00010203-0405-0607-0809-0a0b0c0d0e0f")

    def test_apple_ibeacon_standard(self):
        """Verifies standard Apple iBeacon (0x004C)."""
        uuid_bytes = bytes.fromhex("E2C56DB5DFFB48D2B060D0F5A71096E0")
        mfg_data = {0x004C: b"\x02\x15" + uuid_bytes + struct.pack(">HHb", 1, 2, -59)}
        res = parse_apple_ibeacon(mfg_data)

        self.assertIsNotNone(res)
        self.assertEqual(res["uuid"].upper(), "E2C56DB5-DFFB-48D2-B060-D0F5A71096E0")
        self.assertEqual(res["major"], 1)
        self.assertEqual(res["minor"], 2)
        self.assertEqual(res["rssi_1m_dbm"], -59)

    def test_eddystone_uid(self):
        """Verifies Eddystone-UID (0x00)."""
        # [0x00, RSSI@0m: -20 (0xEC), Namespace (10B), Instance (6B), RFU (2B)]
        ns = bytes.fromhex("0102030405060708090A")
        inst = bytes.fromhex("A1B2C3D4E5F6")
        payload = b"\x00\xEC" + ns + inst + b"\x00\x00"
        res = parse_eddystone_uid(payload)

        self.assertIsNotNone(res)
        self.assertEqual(res["rssi_0m_dbm"], -20)
        self.assertEqual(res["namespace_id"], "0102030405060708090A")
        self.assertEqual(res["instance_id"], "A1B2C3D4E5F6")

    def test_eddystone_url(self):
        """Verifies Eddystone-URL (0x10) with scheme and expansion."""
        # Scheme 1: 'https://www.', 'mokosmart', Expansion 0: '.com/'
        payload = b"\x10\xF4\x01mokosmart\x00"
        res = parse_eddystone_url(payload)

        self.assertIsNotNone(res)
        self.assertEqual(res["rssi_0m_dbm"], -12)
        self.assertEqual(res["url"], "https://www.mokosmart.com/")

    def test_eddystone_tlm(self):
        """Verifies Eddystone-TLM (0x20, unencrypted)."""
        # [0x20, version: 0, batt: 3000mV (0x0BB8), temp: 22.5 C (22.5 * 256 = 5760 = 0x1680), adv_cnt: 100000, sec_cnt: 5000 (500.0s)]
        payload = struct.pack(">BBHhII", 0x20, 0x00, 3000, 5760, 100000, 5000)
        res = parse_eddystone_tlm(payload)

        self.assertIsNotNone(res)
        self.assertEqual(res["battery_voltage_mv"], 3000)
        self.assertEqual(res["temperature_c"], 22.5)
        self.assertEqual(res["adv_count"], 100000)
        self.assertEqual(res["uptime_seconds"], 500.0)

    def test_parse_bxps_advertising_master_bundle(self):
        """Verifies master parser handles multi-frame advertising bundles simultaneously."""
        service_data = {
            "0000ea01-0000-1000-8000-00805f9b34fb": bytes.fromhex(
                "80 1C 0000 0002 003A 0076 03A7 00C8 0137 0CCC 000001"
            ),
            "0000feab-0000-1000-8000-00805f9b34fb": bytes.fromhex(
                "70 C5 0A 00F5 01C2 0BB8 03 C0EE40010203"
            ),
            "0000feaa-0000-1000-8000-00805f9b34fb": b"\x10\xF4\x01mokosmart\x00",
        }
        apple_mfg = {
            0x004C: b"\x02\x15" + bytes.fromhex("E2C56DB5DFFB48D2B060D0F5A71096E0") + struct.pack(">HHb", 1, 2, -59)
        }

        frames = parse_bxps_advertising(service_data, apple_mfg, local_name="L02S_Test")
        self.assertEqual(len(frames), 4)

        types = [f["frame_type"] for f in frames]
        self.assertIn("sensor_info", types)
        self.assertIn("custom_th", types)
        self.assertIn("eddystone_url", types)
        self.assertIn("ibeacon", types)


class TestBxpsConfigProtocol(unittest.TestCase):
    """Tests BXP-S binary GATT command encoding, decoding, and parameter validation."""

    def test_build_frame_and_parse_response(self):
        """Verifies TX 0xEA frame assembly and RX 0xEB response disassembly."""
        # Read CMD_PWD_ENABLE (0x53)
        tx = build_frame(FLAG_READ, CMD_PWD_ENABLE)
        self.assertEqual(tx, bytes.fromhex("EA 00 53 00"))

        # Write CMD_CONNECTABLE (0x37) = 1
        tx_write = build_frame(FLAG_WRITE, CMD_CONNECTABLE, b"\x01")
        self.assertEqual(tx_write, bytes.fromhex("EA 01 37 01 01"))

        # RX Success response: EB 01 37 01 AA
        rx = bytes.fromhex("EB 01 37 01 AA")
        flag, cmd, data = parse_response_frame(rx)
        self.assertEqual(flag, FLAG_WRITE)
        self.assertEqual(cmd, CMD_CONNECTABLE)
        self.assertEqual(data, b"\xAA")

    def test_password_command_vectors(self):
        """Verifies password verify, set, and toggle commands."""
        pwd_bytes = b"Moko4321"
        self.assertEqual(
            build_frame(FLAG_WRITE, CMD_PWD_VERIFY, pwd_bytes),
            bytes.fromhex("EA 01 51 08 4D 6F 6B 6F 34 33 32 31"),
        )
        self.assertEqual(
            build_frame(FLAG_WRITE, CMD_PWD_SET, pwd_bytes),
            bytes.fromhex("EA 01 52 08 4D 6F 6B 6F 34 33 32 31"),
        )
        self.assertEqual(
            build_frame(FLAG_WRITE, CMD_PWD_ENABLE, b"\x01"),
            bytes.fromhex("EA 01 53 01 01"),
        )

    def test_channel_mask_logic(self):
        """Verifies BLE channel bitmask calculation and parsing."""
        # All 3 channels (37, 38, 39) -> mask = 1 | 2 | 4 = 7
        self.assertEqual(channel_mask_from_list([37, 38, 39]), 0x07)
        self.assertEqual(list_from_channel_mask(0x07), [37, 38, 39])

        # Single channel 38 -> mask = 2
        self.assertEqual(channel_mask_from_list([38]), 0x02)
        self.assertEqual(list_from_channel_mask(0x02), [38])

        # Channels 37 and 39 -> mask = 5
        self.assertEqual(channel_mask_from_list([37, 39]), 0x05)
        self.assertEqual(list_from_channel_mask(0x05), [37, 39])

    def test_slot_build_and_parse_roundtrip(self):
        """Verifies Slot 1 Sensor Info configuration encode/decode."""
        content = build_sensor_info_slot_content("L02S", "000001")
        slot_data = build_slot_data(
            slot_idx=0,
            interval_ms=1000,
            duration_s=0,
            standby_s=0,
            rssi_0m_dbm=-59,
            adv_type=0x80,
            content=content,
        )

        parsed = parse_slot_data(slot_data)
        self.assertEqual(parsed["slot"], 0)
        self.assertEqual(parsed["interval_ms"], 1000)
        self.assertEqual(parsed["duration_s"], 0)
        self.assertEqual(parsed["standby_s"], 0)
        self.assertEqual(parsed["rssi_0m_dbm"], -59)
        self.assertEqual(parsed["adv_type"], 0x80)
        self.assertEqual(parsed["content"]["device_name"], "L02S")
        self.assertEqual(parsed["content"]["tag_id"], "000001")

    def test_ibeacon_slot_content_builder(self):
        """Verifies iBeacon slot payload encoding."""
        uuid_str = "E2C56DB5-DFFB-48D2-B060-D0F5A71096E0"
        content = build_ibeacon_slot_content(uuid_str, 100, 200)
        self.assertEqual(content[:16], bytes.fromhex(uuid_str.replace("-", "")))
        maj, min_val = struct.unpack(">HH", content[16:20])
        self.assertEqual(maj, 100)
        self.assertEqual(min_val, 200)

    def test_accelerometer_config(self):
        """Verifies 3-axis accelerometer rate/scale/threshold builder and parser."""
        # 10Hz (code 1), ±2g (code 0), 200mg (200 // 100 = 2)
        payload = build_accel_config(1, 0, 200)
        self.assertEqual(payload, b"\x01\x00\x02")

        parsed = parse_accel_config(payload)
        self.assertEqual(parsed["sampling_rate"], "10Hz")
        self.assertEqual(parsed["full_scale"], "±2g")
        self.assertEqual(parsed["motion_threshold_mg"], 200)

    def test_trigger_config_builder(self):
        """Verifies trigger parameter binary packing for slot trigger function."""
        # Slot 0, Motion detection (type 1), Start moving (condition 0), interval 500ms, Tx power 0dBm, static verify 10s, lock duration 5s
        trig = build_trigger_config(
            slot_idx=0,
            trigger_type=1,
            condition=0,
            adv_interval_ms=500,
            adv_duration_s=10,
            tx_power_dbm=0,
            static_verify_s=10,
            lock_duration_s=5,
        )
        self.assertEqual(len(trig), 11)
        slot, t_type, cond, interval, tx_pow, verify, lock = struct.unpack(">BBBHhHH", trig)
        self.assertEqual(slot, 0)
        self.assertEqual(t_type, 1)
        self.assertEqual(cond, 0)
        self.assertEqual(interval, 500)
        self.assertEqual(tx_pow, 0)
        self.assertEqual(verify, 10)
        self.assertEqual(lock, 5)

    def test_device_info_response_parsing(self):
        """Verifies parsing of MAC, firmware version, and manufacture date."""
        # MAC: AA:BB:CC:DD:EE:FF, FW: V1.2.0 (0x12, 0x00), Date: 2026-05-15 (0x20 0x26 0x05 0x15)
        resp_data = bytes.fromhex("AA BB CC DD EE FF 12 00 20 26 05 15")
        info = parse_device_info_response(resp_data)
        self.assertEqual(info["mac_address"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(info["firmware_version"], "V1.2.0")
        self.assertEqual(info["manufacture_date"], "20260515")

    def test_history_packet_parsing(self):
        """Verifies parsing of historical temperature and humidity log packets."""
        # Total packets: 2 (0x0002), Current index: 0 (0x0000), Len: 16 (2 records)
        # Rec 1: TS 1700000000 (0x65540D00), Temp 21.5 C (215 = 0x00D7), Hum 50.0% (500 = 0x01F4)
        # Rec 2: TS 1700000060 (0x65540D3C), Temp -5.0 C (-50 = 0xFFCE), Hum 55.5% (555 = 0x022B)
        hdr = bytes([0xEB, 0x03, 0x80])
        meta = struct.pack(">HHB", 2, 0, 16)
        rec1 = struct.pack(">IhH", 1700000000, 215, 500)
        rec2 = struct.pack(">IhH", 1700000060, -50, 555)
        pkt = hdr + meta + rec1 + rec2

        total, idx, records = parse_history_packet(pkt)
        self.assertEqual(total, 2)
        self.assertEqual(idx, 0)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0], (1700000000, 21.5, 50.0))
        self.assertEqual(records[1], (1700000060, -5.0, 55.5))


if __name__ == "__main__":
    unittest.main()
