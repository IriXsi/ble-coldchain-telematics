"""
Unit tests for BLE Cold-Chain Telematics binary frame decoders.
"""

import datetime
import unittest

from moko.parser import parse_moko_payload
from tzone.parser import decode_sample_record, parse_history_payload


class TestMokoParser(unittest.TestCase):
    def test_moko_payload_valid(self):
        import struct
        status_byte = 0x38
        magnet = 1
        motion = 2
        ax, ay, az = 10, -20, 1000
        temp_raw = 245  # 24.5 C
        hum_raw = 450   # 45.0 %
        batt_mv = 3000
        tag = bytes([0xA1, 0xB2, 0xC3])

        raw_frame = struct.pack(
            ">BBHHhhhHhH3s",
            0x80, status_byte, magnet, motion, ax, ay, az, temp_raw, hum_raw, batt_mv, tag
        )

        result = parse_moko_payload(raw_frame)
        self.assertIsNotNone(result)
        self.assertEqual(result["tag_id"], "A1B2C3")
        self.assertEqual(result["temperature_c"], 24.5)
        self.assertEqual(result["humidity_rh"], 45.0)
        self.assertEqual(result["acceleration_mg"]["x"], 10)
        self.assertEqual(result["acceleration_mg"]["y"], -20)
        self.assertEqual(result["acceleration_mg"]["z"], 1000)
        self.assertEqual(result["battery_voltage_mv"], 3000)
        self.assertTrue(result["status_flags"]["temp_sensor_equipped"])

    def test_moko_invalid_header(self):
        raw_frame = b"\x00" * 21
        self.assertIsNone(parse_moko_payload(raw_frame))


class TestTzoneParser(unittest.TestCase):
    def test_decode_sample_record(self):
        # Encode +25.0 C (250 = 0xFA) and 50.0% (500 = 0x1F4)
        # temp_bits = 250 << 11 = 0x7D000, hum_bits = 500 = 0x1F4
        val = (250 << 11) | 500
        raw_bytes = val.to_bytes(3, byteorder="big", signed=False)

        temp, hum = decode_sample_record(raw_bytes)
        self.assertEqual(temp, 25.0)
        self.assertEqual(hum, 50.0)

    def test_decode_negative_temperature(self):
        # Encode -10.5 C (-105). 13-bit two's complement: 0x2000 - 105 = 0x1F97
        temp_raw = -105
        temp_bits = (temp_raw + 0x2000) & 0x1FFF
        hum_bits = 400  # 40.0%
        val = (temp_bits << 11) | hum_bits
        raw_bytes = val.to_bytes(3, byteorder="big", signed=False)

        temp, hum = decode_sample_record(raw_bytes)
        self.assertEqual(temp, -10.5)
        self.assertEqual(hum, 40.0)

    def test_parse_history_payload(self):
        val1 = (200 << 11) | 400
        val2 = (210 << 11) | 420
        stream = val1.to_bytes(3, "big") + val2.to_bytes(3, "big")

        start = datetime.datetime(2026, 11, 25, 12, 0, 0)
        records = parse_history_payload(start, interval_sec=60, data=stream)

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["temperature_c"], 20.0)
        self.assertEqual(records[1]["temperature_c"], 21.0)
        self.assertEqual(records[0]["timestamp"], "2026-11-25 12:00:00")
        self.assertEqual(records[1]["timestamp"], "2026-11-25 12:01:00")


if __name__ == "__main__":
    unittest.main()
