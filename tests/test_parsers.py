"""
Comprehensive Unit Tests for BLE Cold-Chain Telematics binary frame decoders.
Uses known-good byte strings from manufacturer protocol specifications (MOKO SMART & Tzone).
"""

import unittest
from moko.parser import parse_moko_payload
from tzone.parser import decode_sample_24bit, decode_sample_record, parse_broadcast_data


class TestMokoParser(unittest.TestCase):
    """Verifies MOKO L02/L02S Sensor Info (0x80) decoding against vendor specification."""

    def test_table5_vendor_spec_example(self):
        """
        Known-good example directly from MK Sensor APP User Manual (Table 5):
        80 1C 0000 0002 003A 0076 03A7 00C8 0137 0CCC 000001
        - Status: 0x1C (motion=0, temp=1, hum=1, flash=0)
        - Magnet Count: 0
        - Motion Count: 2
        - Accel (X, Y, Z): 0x003A (58 mg), 0x0076 (118 mg), 0x03A7 (935 mg)
        - Temperature: 0x00C8 (200 -> 20.0 °C)
        - Humidity: 0x0137 (311 -> 31.1 %RH)
        - Battery: 0x0CCC (3276 -> 3276 mV)
        - Tag ID: 000001
        """
        raw = bytes.fromhex("80 1C 0000 0002 003A 0076 03A7 00C8 0137 0CCC 000001")
        res = parse_moko_payload(raw)

        self.assertIsNotNone(res)
        self.assertEqual(res["tag_id"], "000001")
        self.assertEqual(res["temperature_c"], 20.0)
        self.assertEqual(res["humidity_rh"], 31.1)
        self.assertEqual(res["battery_voltage_mv"], 3276)
        self.assertNotIn("battery_percentage", res)
        self.assertEqual(res["motion_count"], 2)
        self.assertEqual(res["magnet_count"], 0)
        self.assertEqual(res["acceleration_mg"], {"x": 58, "y": 118, "z": 935})
        self.assertFalse(res["status_flags"]["is_moving"])
        self.assertTrue(res["status_flags"]["temp_sensor_equipped"])
        self.assertTrue(res["status_flags"]["humidity_sensor_equipped"])
        self.assertFalse(res["status_flags"]["flash_equipped"])

    def test_negative_temperature_decoding(self):
        """
        Verifies signed 16-bit negative temperature decoding.
        -5.5 °C = -55 -> 0xFFC9
        -1.0 °C = -10 -> 0xFFF6
        """
        # -5.5 °C (0xFFC9)
        raw_neg55 = bytes.fromhex("80 1C 0000 0000 0000 0000 0000 FFC9 0137 0CCC 000001")
        res = parse_moko_payload(raw_neg55)
        self.assertIsNotNone(res)
        self.assertEqual(res["temperature_c"], -5.5)

        # -1.0 °C (0xFFF6)
        raw_neg10 = bytes.fromhex("80 1C 0000 0000 0000 0000 0000 FFF6 0200 0064 01")
        res2 = parse_moko_payload(raw_neg10)
        self.assertIsNotNone(res2)
        self.assertEqual(res2["temperature_c"], -1.0)

    def test_battery_boundary_100_vs_101(self):
        """
        Protocol rule: battery value <= 100 represents percentage (%),
        battery value > 100 represents millivolts (mV).
        """
        # Exactly 100 -> percentage
        raw_pct = bytes.fromhex("80 1C 0000 0000 0000 0000 0000 00C8 0137 0064 000001")
        res_pct = parse_moko_payload(raw_pct)
        self.assertIsNotNone(res_pct)
        self.assertEqual(res_pct["battery_percentage"], 100)
        self.assertNotIn("battery_voltage_mv", res_pct)

        # Exactly 101 -> millivolts
        raw_mv = bytes.fromhex("80 1C 0000 0000 0000 0000 0000 00C8 0137 0065 000001")
        res_mv = parse_moko_payload(raw_mv)
        self.assertIsNotNone(res_mv)
        self.assertEqual(res_mv["battery_voltage_mv"], 101)
        self.assertNotIn("battery_percentage", res_mv)

    def test_maximum_humidity_value(self):
        """100.0 %RH = 1000 raw (0x03E8)."""
        raw_max_hum = bytes.fromhex("80 1C 0000 0000 0000 0000 0000 00C8 03E8 0CCC 000001")
        res = parse_moko_payload(raw_max_hum)
        self.assertIsNotNone(res)
        self.assertEqual(res["humidity_rh"], 100.0)

    def test_short_and_invalid_frames(self):
        """Rejects short frames, invalid headers, or offset 0x80 bytes."""
        # Short frame (<18 bytes)
        self.assertIsNone(parse_moko_payload(b"\x80\x1c\x00\x01\x02"))

        # Wrong header (0x20 TLM instead of 0x80 Sensor Info)
        wrong_hdr = bytes.fromhex("20 1C 0000 0002 003A 0076 03A7 00C8 0137 0CCC 000001")
        self.assertIsNone(parse_moko_payload(wrong_hdr))

        # Fragile match test: 0x80 appears inside other payload at offset > 0
        offset_hdr = bytes.fromhex("00 01 02 80 1C 0000 0002 003A 0076 03A7 00C8 0137 0CCC 000001")
        self.assertIsNone(parse_moko_payload(offset_hdr))


class TestTzoneParser(unittest.TestCase):
    """Verifies Tzone BT04B 24-bit compact record and broadcast decoders."""

    def test_decode_sample_24bit_positive_temp(self):
        """
        24-bit record with +22.5 °C (raw_temp = 225) and 55% humidity.
        - humidity: 55 -> byte0 = (55 << 1) = 110 = 0x6E
        - raw_temp: 225 -> (225 << 6) in raw24
        """
        raw24 = (55 << 17) | (225 << 6)
        chunk = raw24.to_bytes(3, byteorder="big", signed=False)

        temp, hum = decode_sample_24bit(chunk)
        self.assertEqual(temp, 22.5)
        self.assertEqual(hum, 55.0)

        # Verify alias
        t2, h2 = decode_sample_record(chunk)
        self.assertEqual(t2, temp)
        self.assertEqual(h2, hum)

    def test_decode_sample_24bit_negative_temp(self):
        """
        BT04B negative temperature rule:
        If raw_temp >= 1250, temp = (raw_temp - 2048) * 0.1 °C.
        For -10.5 °C: raw_temp = 2048 - 105 = 1943.
        Humidity: 40% (byte0 = 40 << 1 = 80).
        """
        raw24 = (40 << 17) | (1943 << 6)
        chunk = raw24.to_bytes(3, byteorder="big", signed=False)

        temp, hum = decode_sample_24bit(chunk)
        self.assertEqual(temp, -10.5)
        self.assertEqual(hum, 40.0)

    def test_parse_broadcast_data_valid(self):
        """Decodes 20-byte broadcast service data payload."""
        # hw=0x0102, fw=1 (v1), id=AABBCCDD, batt=85%, temp=+21.50 C (2150=0x0866), hum=45.20% (4520=0x11A8), alarm=0x00
        payload = bytes.fromhex("01 02 01 AA BB CC DD 55 00 08 66 11 A8 00 00 00")
        res = parse_broadcast_data(payload)

        self.assertIsNotNone(res)
        self.assertEqual(res["device_id"], "AABBCCDD")
        self.assertEqual(res["battery_percent"], 85)
        self.assertEqual(res["temperature_c"], 21.5)
        self.assertEqual(res["humidity_percent"], 45.2)
        self.assertEqual(res["alarm_status_str"], "Normal")
        self.assertFalse(res["low_power_alarm"])
        self.assertFalse(res["temp_alarm"])

    def test_parse_broadcast_negative_temp_and_alarms(self):
        """
        Broadcast negative temperature bit (bit 14 of raw_temp = 0x4000).
        -5.25 °C = 0x4000 | 525 = 0x420D.
        Alarm byte: 0xC0 (low_batt 0x80 + temp_overrun 0x40).
        """
        payload = bytes.fromhex("01 02 01 AA BB CC DD 10 00 42 0D 11 A8 00 00 C0")
        res = parse_broadcast_data(payload)

        self.assertIsNotNone(res)
        self.assertEqual(res["temperature_c"], -5.25)
        self.assertTrue(res["low_power_alarm"])
        self.assertTrue(res["temp_alarm"])
        self.assertEqual(res["alarm_status_str"], "Low Batt + Over-Temp")

    def test_short_and_invalid_tzone(self):
        """Rejects short chunk or invalid broadcast."""
        t, h = decode_sample_24bit(b"\x00\x01")
        self.assertIsNone(t)
        self.assertIsNone(h)

        self.assertIsNone(parse_broadcast_data(b"\x11\x00\x01"))


if __name__ == "__main__":
    unittest.main()
