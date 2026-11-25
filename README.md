# Industrial BLE Cold-Chain Telematics Suite

A modular Python framework for multi-vendor Bluetooth Low Energy (BLE) environmental sensors used in cold-chain logistics and fleet telematics.

Supports:
- **MOKO SMART L02 / L02S**: BLE 5.1 sensor beacons (Silicon Labs), broadcast frame decoding (`0x80`), acceleration, and flash history retrieval.
- **Tzone BT04B**: BLE 5.0 temperature/humidity data loggers (Nordic nRF51822), GATT command interface, security handshake, and bulk record synchronization.

---

## Architecture Overview

```
ble-coldchain-telematics/
├── moko/                   # MOKO SMART L02/L02S driver & frame decoders
│   ├── __init__.py
│   ├── parser.py          # Broadcast payload unpacker (21-byte sensor frame)
│   └── client.py          # BXP-S GATT configuration & log download client
├── tzone/                  # Tzone BT04B driver & GATT service handler
│   ├── __init__.py
│   ├── parser.py          # Telemetry & historical record unpackers
│   └── client.py          # Bleak GATT client & authorization handshake
├── examples/               # End-to-end integration examples
│   ├── live_sniffer.py    # Multi-vendor real-time BLE broadcast listener
│   └── export_logs.py     # Script to pull flash records into CSV format
├── tests/                  # Unit test suite (standard unittest)
│   └── test_parsers.py    # Binary frame decoders verification
├── data/
│   └── sample_trip.csv    # Sample cold-chain telemetry dataset
├── requirements.txt
└── .gitignore
```

---

## Installation

```bash
git clone https://github.com/irixsi/ble-coldchain-telematics.git
cd ble-coldchain-telematics
pip install -r requirements.txt
```

---

## Quick Start

### 1. Real-Time Passive Sniffer
Listen for nearby broadcast packets from deployed cold-chain tags without pairing:

```bash
python examples/live_sniffer.py
```

### 2. Verify Decoders
Run the automated protocol validation suite:

```bash
python -m unittest discover tests
```

---

## Protocol Specifications

### MOKO L02S Sensor Frame (`0x80`)
Unpacks 21-byte big-endian broadcast payload containing:
- Temperature ($0.1\,^\circ\text{C}$ resolution, signed 16-bit integer)
- Relative Humidity ($0.1\,\%\text{RH}$ resolution, unsigned 16-bit integer)
- 3-axis Accelerometer ($X, Y, Z$ in milli-g)
- Motion & magnetic trigger counters
- Battery state (percentage or millivolts)

### Tzone BT04B Service Architecture
Operates over proprietary GATT Service `27763B10-999C-4D6A-9FC4-C7272BE10900`:
- Secure 6-byte password handshake (`0x3B13`)
- Synchronous record notification streaming (`0x3B21` / `0x3B31`)
- Compact 24-bit bitpacked sample decoders

---

## Development Notes & Attribution
- **Architecture & Domain Design:** Engineered for industrial cold-chain logistics telemetry workflows based on MOKO SMART and Tzone hardware specifications.
- **AI-Assisted Scaffolding:** Developed with AI pair-programming assistance for protocol reverse-engineering, package modularization, and unit test generation.
- **Hardware Validation:** Packet parsers, GATT handshakes, and sample records verified and calibrated against physical MOKO L02S and Tzone BT04B data loggers.

---

## License
MIT License.

