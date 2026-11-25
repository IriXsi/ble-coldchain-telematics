"""
Tzone BT04B Binary Frame and Sample Record Parsers.
"""

import datetime
from typing import Any, Dict, List, Optional, Tuple


def decode_sample_record(raw_3bytes: bytes) -> Tuple[float, float]:
    """
    Decodes a 24-bit compact sample record into (temperature, humidity).
    - Temperature: 13-bit signed value (resolution 0.1 C)
    - Humidity: 11-bit unsigned value (resolution 0.1 %RH)
    """
    if len(raw_3bytes) != 3:
        raise ValueError("Sample record requires exactly 3 bytes")

    val = int.from_bytes(raw_3bytes, byteorder="big", signed=False)
    temp_bits = (val >> 11) & 0x1FFF
    hum_bits = val & 0x07FF

    # 13-bit two's complement sign extension
    if temp_bits & 0x1000:
        temp_signed = temp_bits - 0x2000
    else:
        temp_signed = temp_bits

    temperature = round(temp_signed * 0.1, 1)
    humidity = round(hum_bits * 0.1, 1)
    return temperature, humidity


def parse_history_payload(
    base_timestamp: datetime.datetime, interval_sec: int, data: bytes
) -> List[Dict[str, Any]]:
    """
    Unpacks a continuous stream of 3-byte temperature/humidity samples
    projected from a starting base timestamp.
    """
    records = []
    chunk_size = 3
    num_samples = len(data) // chunk_size

    for i in range(num_samples):
        chunk = data[i * chunk_size : (i + 1) * chunk_size]
        temp, hum = decode_sample_record(chunk)
        sample_time = base_timestamp + datetime.timedelta(seconds=i * interval_sec)
        records.append({
            "timestamp": sample_time.strftime("%Y-%m-%d %H:%M:%S"),
            "unix_ts": int(sample_time.timestamp()),
            "temperature_c": temp,
            "humidity_rh": hum,
        })

    return records
