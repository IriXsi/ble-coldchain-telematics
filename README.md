# Industrial BLE Cold-Chain Telematics Suite

A modular Python framework for multi-vendor Bluetooth Low Energy (BLE) environmental sensors used in cold-chain logistics and fleet telematics.

Supports:
- **MOKO SMART L02 / L02S / M2 / M2S (BXP-S Series)**: BLE 5.1 sensor beacons, broadcast frame decoding, GATT configuration export/apply, and flash history retrieval.
- **Tzone BT04B**: BLE 5.0 temperature/humidity data loggers (Nordic nRF51822), GATT command interface, security handshake, and bulk record synchronisation.

---

## Architecture Overview

```
ble-coldchain-telematics/
├── main.py                 # Unified interactive control centre (all modules in one menu)
├── config_export.py        # CLI: Read all device settings → export to JSON profile
├── config_apply.py         # CLI: Apply JSON profile → write all settings to device
├── run.bat                 # Windows launcher for main.py
├── requirements.txt
├── .gitignore
│
├── moko/                   # MOKO SMART BXP-S Series driver & suite
│   ├── __init__.py         # Package exports for frames, config, and decoders
│   ├── bxps_frames.py      # All BXP-S broadcast decoders (0x80, 0x70, 0x40, 0x60, 0x50, Eddystone, iBeacon)
│   ├── bxps_config.py      # BXP-S GATT Configuration Client & frame builders
│   ├── config_manager.py   # Bidirectional config serialiser: device ↔ JSON
│   ├── parser.py           # Legacy/direct 0x80 sensor info parser
│   └── client.py           # Interactive CLI: scan, configure, download history
│
├── tzone/                  # Tzone BT04B driver & GATT service handler
│   ├── __init__.py
│   ├── parser.py           # Broadcast & 24-bit flash record decoders
│   └── dashboard.py        # Tkinter GUI: live scan, GATT sync, CSV export
│
├── examples/               # End-to-end integration examples
│   └── live_sniffer.py     # Multi-vendor real-time BLE broadcast listener
│
├── tests/                  # Comprehensive unit test suite (30 tests)
│   ├── __init__.py
│   ├── test_bxps.py        # Full BXP-S broadcast & GATT configuration tests (20 tests)
│   └── test_parsers.py     # Legacy frame decoder verification (10 tests)
│
├── configs/                # Exported device profiles (gitignored *.json, .gitkeep tracked)
│   └── .gitkeep
│
└── docs/
    └── MOKO_L02_Product_Specification.pdf
```

---

## Installation

```bash
git clone https://github.com/irixsi/ble-coldchain-telematics.git
cd ble-coldchain-telematics
pip install -r requirements.txt
```

> **Windows requirement:** Python 3.10+ with WinRT BLE support. Bluetooth adapter must support BLE 4.2+.

---

## Quick Start

### 1. Unified Interactive Control Centre (Recommended)

Launch the interactive terminal menu to run any module with a single keystroke:

```bash
# Windows — double-click or run from terminal:
.\run.bat

# Or directly with Python:
python main.py
```

**Menu options:**

| Key | Action |
|-----|--------|
| `1` | **Live Broadcast Sniffer** — Listen passively to all BXP-S & BT04B advertisements |
| `2` | **Scan for BLE Devices** — Discover nearby tags, MAC addresses, and RSSI |
| `3` | **Read Device Configurations** — Connect and print all tag settings |
| `4` | **Interactive Config Menu** — Change slots, triggers, time, and switches |
| `5` | **Quick UTC Clock Sync** — Set tag clock to system UTC |
| `6` | **Download Trip Logs** — Export stored flash history to CSV |
| `7` | **Clear Flash Storage** — Erase memory on the tag |
| `8` | **Tzone GUI Dashboard** — Launch the graphical BT04B control panel |
| `9` | **Run Protocol Tests** — Execute all 30 automated validation checks |
| `E` | **Export Config to File** — Read all device settings → JSON profile |
| `A` | **Apply Config from File** — Load JSON profile → write all settings to device |

---

### 2. Config Export & Apply (Standalone CLI)

Export a full device profile to JSON:

```bash
python config_export.py --mac 78:50:05:00:6C:54
# Saves to: configs/bxps_785005006C54_<timestamp>.json
```

Apply a saved profile to any device (clone or restore):

```bash
python config_apply.py --mac 78:50:05:00:6C:D9 --config configs/bxps_785005006C54_20261127_170302.json
```

The JSON profile is fully human-readable and editable before re-application.

---

### 3. MOKO BXP-S GATT CLI

```bash
# Scan for nearby MOKO devices
python moko/client.py scan

# Show all device settings
python moko/client.py -a AA:BB:CC:DD:EE:FF show

# Sync device clock, set sampling interval, download history to CSV
python moko/client.py -a AA:BB:CC:DD:EE:FF set --sync-time --th-sampling 60
python moko/client.py -a AA:BB:CC:DD:EE:FF history --out trip.csv

# Run protocol selftest (no hardware needed)
python moko/client.py selftest
```

---

### 4. Tzone BT04B Dashboard

```bash
python tzone/dashboard.py
```

---

### 5. Run Protocol Tests

```bash
python -m unittest discover tests
# or
python -m pytest tests/ -v
```

Expected: **30 passed**.

---

## Protocol Specifications

### MOKO BXP-S Broadcast Frames

| Frame type | ID | Description |
|---|---|---|
| Sensor Info | `0x80` | Temperature, humidity, XYZ acceleration, battery, motion counters |
| Device Info | `0x70` | Firmware version, hardware revision, MAC, manufacture date |
| Accelerometer | `0x40` | Raw 3-axis accelerometer data |
| T&H History  | `0x60` | Stored flash record chunk |
| iBeacon scan response | `0x50` | UUID, major, minor, TX power |
| Eddystone-UID / URL / TLM | `0x00/0x10/0x20` | Standard Eddystone advertising frames |

**Sensor frame (`0x80`) payload** (21 bytes, big-endian):
- Temperature: `±0.1 °C` resolution (signed 16-bit)
- Relative humidity: `0.1 %RH` resolution (unsigned 16-bit)
- XYZ acceleration: milli-g (signed 16-bit each)
- Motion & magnetic trigger counters
- Battery: percentage or millivolts

### MOKO BXP-S GATT Configuration

```
Primary Service: 0000AA00-0000-1000-8000-00805F9B34FB
  AA01 (Write/Notify): General configuration (slots, radio, switches, sensors)
  AA04 (Write/Notify): Password management (verify, change, toggle)
  AA09 (Indicate):     T&H historical log download

Frame format:
  TX: 0xEA <flag: 0x00=Read, 0x01=Write> <CMD> <len> <data...>
  RX: 0xEB <flag> <CMD> <len> <data...>  (0xAA = Success)
```

### Tzone BT04B GATT Service

```
Service UUID: 27763B10-999C-4D6A-9FC4-C7272BE10900
  - 6-byte password handshake (0x3B13)
  - Synchronous record notification streaming (0x3B21 / 0x3B31)
  - Compact 24-bit bitpacked sample records
```

---

## Device Compatibility

| Device | Series | Config Export | Config Apply | History Download | Broadcast Decode |
|---|---|:---:|:---:|:---:|:---:|
| MOKO L02S | BXP-S | ✅ | ✅ | ✅ | ✅ |
| MOKO L02  | BXP-S | ✅ | ✅ | ✅ | ✅ |
| MOKO M2   | BXP-S | ✅ | ✅ | ✅ | ✅ |
| MOKO M2S  | BXP-S | ✅ | ✅ | ✅ | ✅ |
| Tzone BT04B | — | — | — | ✅ | ✅ |

> **Note:** CMD_TRIGGER_LED (0x24) and CMD_ACCEL_CONFIG (0x30) are not supported on L02S hardware and are automatically skipped during export/apply.

---

## Development Notes & Attribution
- **Architecture & Domain Design:** Engineered for industrial cold-chain logistics telemetry workflows based on MOKO SMART and Tzone hardware specifications.
- **AI-Assisted Scaffolding:** Developed with AI pair-programming assistance for protocol reverse-engineering, package modularisation, and unit test generation.
- **Hardware Validation:** Packet parsers, GATT handshakes, and sample records verified and calibrated against physical MOKO L02S and Tzone BT04B data loggers.

---

## License
MIT License.
