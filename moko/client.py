#!/usr/bin/env python3
"""
bxps_config.py - Configure a MOKO BXP-S / MK Sensor (L02S etc.) over BLE.

Based on "BXP-S protocol 202608111.xlsx" (firmware BXP-S V1.0.0+) and the
"MK Sensor Series APP User Manual V1.3".

    pip install bleak
    python bxps_config.py                     # guided mode (scan, pick a device, configure)
    python bxps_config.py scan
    python bxps_config.py -a AA:BB:CC:DD:EE:FF show
    python bxps_config.py -a AA:BB:CC:DD:EE:FF set --sync-time --th-sampling 60
    python bxps_config.py settings            # what can be set (no hardware needed)
    python bxps_config.py -a AA:BB:CC:DD:EE:FF menu   # interactive prompts
    python bxps_config.py selftest            # no hardware needed

Protocol summary
  Service            0000AA00-0000-1000-8000-00805f9b34fb
  AA04 (write/notify)  password commands (0x51 verify, 0x52 set, 0x53 enable/read)
  AA01 (write/notify)  everything else
  AA09 (indicate)      stored temperature/humidity history
  TX frame: EA <flag 00=read 01=write> <cmd> <len> <data...>
  RX frame: EB <flag> <cmd> <len> <data...>      write reply data: AA = OK, 00 = fail

Default password in MOKO firmware: "Moko4321" (max 16 chars).
"""
import argparse
import asyncio
import struct
import sys
import time

SERVICE = "0000AA00-0000-1000-8000-00805f9b34fb"
CH_CFG = "0000AA01-0000-1000-8000-00805f9b34fb"   # general config
CH_PWD = "0000AA04-0000-1000-8000-00805f9b34fb"   # password
CH_HIS = "0000AA09-0000-1000-8000-00805f9b34fb"   # history (indicate)

HEAD_TX, HEAD_RX = 0xEA, 0xEB
READ, WRITE = 0x00, 0x01
OK = 0xAA

# command bytes
CMD_PWD_VERIFY = 0x51
CMD_PWD_SET = 0x52
CMD_PWD_ENABLE = 0x53
CMD_BTN_POWEROFF = 0x25
CMD_BTN_RESET = 0x23
CMD_SAVE_FACTORY = 0x27
CMD_SLOT = 0x34
CMD_RF_CHANNEL = 0x35
CMD_CONNECTABLE = 0x37
CMD_PHY = 0x3A
CMD_TAGID_AUTOFILL = 0x3C
CMD_TIMESTAMP = 0x3F
CMD_TH_STORE = 0x40
CMD_TH_SAMPLING = 0x41
CMD_TH_CLEAR = 0x42
CMD_TH_COUNT = 0x43
CMD_COUNTER = 0x3E
CMD_BATT_ADV = 0x6C

INTERVAL_MIN_MS, INTERVAL_MAX_MS, INTERVAL_STEP_MS = 100, 10000, 100   # vendor app limits


def check_interval(ms):
    if not (INTERVAL_MIN_MS <= ms <= INTERVAL_MAX_MS and ms % INTERVAL_STEP_MS == 0):
        raise ValueError("interval must be 100-10000 ms in steps of 100 "
                         "(for slower broadcasting use a sleep cycle: --broadcast-every)")
    return ms


def cycle_for_period(seconds):
    """Settings that give roughly one broadcast burst every `seconds`:
    (interval ms, duration s, standby s). Burst = 1 s, sleep = the rest."""
    if not 2 <= seconds <= 65535 + 1:
        raise ValueError("period must be 2-65536 seconds")
    return 1000, 1, seconds - 1


ADV_TYPES = {0x00: "UID", 0x10: "URL", 0x20: "TLM", 0x50: "iBeacon",
             0x80: "Sensor info", 0xFF: "No data"}
ADV_TYPE_BY_NAME = {"uid": 0x00, "url": 0x10, "tlm": 0x20, "ibeacon": 0x50,
                    "sensorinfo": 0x80, "sensor": 0x80, "none": 0xFF}


class DeviceError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Pure frame helpers (no BLE needed, covered by `selftest`)
# --------------------------------------------------------------------------
def build(flag, cmd, data=b""):
    return bytes([HEAD_TX, flag, cmd, len(data)]) + bytes(data)


def parse_rx(frame):
    """-> (flag, cmd, data). Raises DeviceError on a malformed frame."""
    if len(frame) < 4 or frame[0] != HEAD_RX:
        raise DeviceError(f"bad frame: {frame.hex(' ')}")
    return frame[1], frame[2], bytes(frame[4:])


def channel_mask(chs):
    """Advertising channels {37,38,39} -> bitmask (CH37=1, CH38=2, CH39=4)."""
    bits = {37: 0x01, 38: 0x02, 39: 0x04}
    m = 0
    for c in chs:
        if c not in bits:
            raise ValueError("channels must be 37, 38 or 39")
        m |= bits[c]
    if m == 0:
        raise ValueError("at least one channel is required")
    return m


def channels_from_mask(m):
    return [c for c, b in ((37, 1), (38, 2), (39, 4)) if m & b]


def build_sensor_info_content(name: str, tag_id_hex: str):
    """ADV type 0x80 + name len + name + tag len + tag id (see slot table)."""
    nb = name.encode("ascii")
    tb = bytes.fromhex(tag_id_hex)
    if not 1 <= len(nb) <= 20:
        raise ValueError("device name must be 1-20 ASCII characters")
    if not 1 <= len(tb) <= 6:
        raise ValueError("tag id must be 1-6 bytes (2-12 hex digits)")
    return bytes([0x80, len(nb)]) + nb + bytes([len(tb)]) + tb


def parse_slot(data):
    """Parse the data field of a slot read reply.

    Layout (from the spreadsheet): slot idx(1) interval ms(2) duration s(2)
    standby s(2) rssi(1, signed) adv-type(1) content(rest).
    Note: the example in the spreadsheet has a length byte that doesn't match
    its own payload, so we parse by position and ignore the declared length.
    """
    if len(data) < 9:
        raise DeviceError(f"slot reply too short: {data.hex(' ')}")
    idx, interval, duration, standby = struct.unpack(">BHHH", data[:7])
    rssi = struct.unpack("b", data[7:8])[0]
    adv_type = data[8]
    return {"slot": idx, "interval_ms": interval, "duration_s": duration,
            "standby_s": standby, "rssi_dbm": rssi, "adv_type": adv_type,
            "content": bytes(data[9:]), "raw": bytes(data)}


def build_slot(s):
    return (struct.pack(">BHHH", s["slot"], s["interval_ms"],
                        s["duration_s"], s["standby_s"])
            + struct.pack("b", s["rssi_dbm"])
            + bytes([s["adv_type"]]) + s["content"])


def describe_slot(s):
    t = ADV_TYPES.get(s["adv_type"], f"0x{s['adv_type']:02X}")
    out = (f"SLOT{s['slot'] + 1}: type={t} interval={s['interval_ms']}ms "
           f"duration={s['duration_s']}s standby={s['standby_s']}s "
           f"rssi@0m={s['rssi_dbm']}dBm")
    c = s["content"]
    if s["adv_type"] == 0x80 and len(c) >= 2:
        try:
            n = c[0]
            name = c[1:1 + n].decode("ascii", "replace")
            tl = c[1 + n]
            tag = c[2 + n:2 + n + tl].hex()
            out += f" name={name!r} tag_id=0x{tag}"
        except IndexError:
            out += f" content={c.hex(' ')}"
    elif c:
        out += f" content={c.hex(' ')}"
    return out


def parse_history_packet(pkt):
    """EB 03 80 | total(2) | idx(2) | len(1) | N * [ts(4) temp(2) hum(2)]
    (the spreadsheet's sheet 2 shows the head as EC 00 80; both accepted).
    Temperature: signed, 0.1 degC. Humidity: unsigned, 0.1 %RH. Big-endian
    is assumed (matches every other multi-byte field in this protocol)."""
    if len(pkt) < 8 or pkt[0] not in (0xEB, 0xEC) or pkt[2] != 0x80:
        raise DeviceError(f"bad history packet: {pkt.hex(' ')}")
    total, idx, ln = struct.unpack(">HHB", pkt[3:8])
    body = pkt[8:8 + ln]
    recs = []
    for i in range(0, len(body) - 7, 8):
        ts, t, h = struct.unpack(">IhH", body[i:i + 8])
        recs.append((ts, t / 10.0, h / 10.0))
    return total, idx, recs


EPOCH_2020 = 1577808000      # same threshold the vendor app uses


def normalize_ts(ts, boot_time):
    """Records carry Unix time if the device clock was synced, otherwise
    seconds since boot. Values below 2020-01-01 are treated as the latter."""
    return float(ts) if ts >= EPOCH_2020 else boot_time + ts


def save_history_csv(recs, path):
    with open(path, "w", newline="") as f:
        f.write("time_utc,unix_ts,temp_c,humidity_pct\n")
        for ts, t, h in recs:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(ts))},"
                    f"{int(ts)},{t},{h}\n")


# --------------------------------------------------------------------------
# Advertisement decoding (formats from the MK Sensor APP manual, chapter 2)
# --------------------------------------------------------------------------
UUID_MOKO_SENSOR = "0000ea01-0000-1000-8000-00805f9b34fb"
UUID_EDDYSTONE = "0000feaa-0000-1000-8000-00805f9b34fb"
URL_SCHEMES = {0: "http://www.", 1: "https://www.", 2: "http://", 3: "https://"}
URL_EXPANSIONS = {0: ".com/", 1: ".org/", 2: ".edu/", 3: ".net/", 4: ".info/",
                  5: ".biz/", 6: ".gov/", 7: ".com", 8: ".org", 9: ".edu",
                  10: ".net", 11: ".info", 12: ".biz", 13: ".gov"}


try:
    from .parser import parse_moko_payload
except (ImportError, ValueError):
    try:
        from moko.parser import parse_moko_payload
    except ImportError:
        from parser import parse_moko_payload


def decode_sensor_info(d):
    """MOKO 'Sensor info' service data (after the 01 EA UUID), frame type 0x80."""
    parsed = parse_moko_payload(d)
    if not parsed:
        return None
    out = {
        "frame": "Sensor info",
        "moving": parsed["status_flags"]["is_moving"],
        "has_motion_sensor": parsed["status_flags"]["motion_sensor_equipped"],
        "has_temp": parsed["status_flags"]["temp_sensor_equipped"],
        "has_humidity": parsed["status_flags"]["humidity_sensor_equipped"],
        "has_flash": parsed["status_flags"]["flash_equipped"],
        "magnet_count": parsed["magnet_count"],
        "motion_count": parsed["motion_count"],
        "accel_mg": (
            parsed["acceleration_mg"]["x"],
            parsed["acceleration_mg"]["y"],
            parsed["acceleration_mg"]["z"],
        ),
        "tag_id": parsed["tag_id"].lower(),
    }
    if out["has_temp"]:
        out["temp_c"] = parsed["temperature_c"]
    if out["has_humidity"]:
        out["humidity"] = parsed["humidity_rh"]
    if "battery_voltage_mv" in parsed:
        out["battery_mv"] = parsed["battery_voltage_mv"]
    else:
        out["battery_pct"] = parsed["battery_percentage"]
    return out


def decode_tlm(d):
    """Eddystone-TLM (frame 0x20, unencrypted)."""
    if len(d) < 14 or d[0] != 0x20:
        return None
    mv, t88, adv, sec = struct.unpack(">HhII", d[2:14])
    return {"frame": "Eddystone TLM", "battery_mv": mv, "temp_c": round(t88 / 256, 1),
            "adv_count": adv, "uptime_s": sec / 10.0}


def decode_uid(d):
    if len(d) < 18 or d[0] != 0x00:
        return None
    return {"frame": "Eddystone UID", "namespace": d[2:12].hex(), "instance": d[12:18].hex()}


def decode_url(d):
    if len(d) < 3 or d[0] != 0x10:
        return None
    url = URL_SCHEMES.get(d[2], "")
    for ch in d[3:]:
        url += URL_EXPANSIONS.get(ch, chr(ch) if 32 <= ch < 127 else "")
    return {"frame": "Eddystone URL", "url": url}


def decode_ibeacon(mfg):
    """Apple manufacturer data 0x004C: 02 15 <uuid16> <major2> <minor2> <rssi@1m>."""
    d = mfg.get(0x004C)
    if not d or len(d) < 23 or d[0] != 0x02 or d[1] != 0x15:
        return None
    u = d[2:18].hex()
    return {"frame": "iBeacon",
            "uuid": f"{u[:8]}-{u[8:12]}-{u[12:16]}-{u[16:20]}-{u[20:]}",
            "major": struct.unpack(">H", d[18:20])[0],
            "minor": struct.unpack(">H", d[20:22])[0],
            "rssi_1m": struct.unpack("b", d[22:23])[0]}


def decode_adv(adv):
    """-> list of decoded-frame dicts found in a bleak AdvertisementData."""
    frames = []
    for k, v in adv.service_data.items():
        k = k.lower()
        if k == UUID_MOKO_SENSOR:
            f = decode_sensor_info(bytes(v))
        elif k == UUID_EDDYSTONE:
            v = bytes(v)
            f = decode_uid(v) or decode_url(v) or decode_tlm(v)
        else:
            f = None
        if f:
            frames.append(f)
    ib = decode_ibeacon(getattr(adv, "manufacturer_data", {}) or {})
    if ib:
        frames.append(ib)
    return frames


def format_frame(f):
    """One human-readable line for a decoded frame."""
    t = f["frame"]
    if t == "Sensor info":
        p = []
        if "temp_c" in f: p.append(f"{f['temp_c']:.1f} C")
        if "humidity" in f: p.append(f"{f['humidity']:.1f} %RH")
        if "battery_mv" in f: p.append(f"batt {f['battery_mv']} mV")
        if "battery_pct" in f: p.append(f"batt {f['battery_pct']} %")
        p.append("MOVING" if f["moving"] else "static")
        p.append(f"motion#{f['motion_count']}")
        x, y, z = f["accel_mg"]
        p.append(f"accel {x},{y},{z} mg")
        if f["tag_id"]: p.append(f"tag 0x{f['tag_id']}")
        return "  ".join(p)
    if t == "Eddystone TLM":
        return (f"TLM  {f['temp_c']:.1f} C  batt {f['battery_mv']} mV  "
                f"adv#{f['adv_count']}  up {f['uptime_s']:.0f}s")
    if t == "Eddystone UID":
        return f"UID  ns={f['namespace']} id={f['instance']}"
    if t == "Eddystone URL":
        return f"URL  {f['url']}"
    if t == "iBeacon":
        return f"iBeacon  {f['uuid']} major={f['major']} minor={f['minor']}"
    return str(f)


def change_key(frames):
    """What counts as 'changed' when listening (ignores noisy accelerometer)."""
    k = []
    for f in frames:
        k.append((f["frame"], f.get("temp_c"), f.get("humidity"), f.get("battery_mv"),
                  f.get("battery_pct"), f.get("moving"), f.get("motion_count"),
                  f.get("adv_count") if f["frame"] != "Eddystone TLM" else None))
    return tuple(k)


# --------------------------------------------------------------------------
# BLE session
# --------------------------------------------------------------------------
class Sensor:
    def __init__(self, client, timeout=5.0):
        self.c = client
        self.timeout = timeout
        self.q = asyncio.Queue()
        self.hist = asyncio.Queue()

    async def start(self):
        await self.c.start_notify(CH_CFG, lambda _, d: self.q.put_nowait(bytes(d)))
        await self.c.start_notify(CH_PWD, lambda _, d: self.q.put_nowait(bytes(d)))

    async def _xfer(self, char, flag, cmd, data=b""):
        while not self.q.empty():          # drop stale notifications
            self.q.get_nowait()
        await self.c.write_gatt_char(char, build(flag, cmd, data), response=True)
        loop = asyncio.get_running_loop()
        end = loop.time() + self.timeout
        while True:
            left = end - loop.time()
            if left <= 0:
                raise DeviceError(f"timeout waiting for reply to cmd 0x{cmd:02X}")
            fr = await asyncio.wait_for(self.q.get(), left)
            f, c, d = parse_rx(fr)
            if f == flag and c == cmd:
                return d

    async def read(self, cmd, char=CH_CFG, data=b""):
        return await self._xfer(char, READ, cmd, data)

    async def write(self, cmd, data, char=CH_CFG):
        r = await self._xfer(char, WRITE, cmd, data)
        if r != bytes([OK]):
            raise DeviceError(f"device rejected write cmd 0x{cmd:02X} "
                              f"(reply {r.hex(' ') or 'empty'})")

    # ---- password -------------------------------------------------------
    async def password_required(self):
        return (await self.read(CMD_PWD_ENABLE, CH_PWD))[0] == 1

    async def authenticate(self, password):
        try:
            await self.write(CMD_PWD_VERIFY, password.encode("ascii"), CH_PWD)
        except DeviceError:
            raise DeviceError(
                "wrong password (it is case-sensitive; the factory default is "
                "'Moko4321' with a capital M). If it was changed and is lost, "
                "do a hardware factory reset - see the manual section 3.8.2")

    async def set_password(self, new):
        if not 1 <= len(new) <= 16:
            raise ValueError("password must be 1-16 characters")
        await self.write(CMD_PWD_SET, new.encode("ascii"), CH_PWD)

    async def set_password_required(self, on):
        await self.write(CMD_PWD_ENABLE, bytes([1 if on else 0]), CH_PWD)

    # ---- simple switches --------------------------------------------------
    async def get_connectable(self):
        return (await self.read(CMD_CONNECTABLE))[0] == 1

    async def set_connectable(self, on):
        await self.write(CMD_CONNECTABLE, bytes([1 if on else 0]))

    # NOTE polarity: for power-off-by-button 0 = ENABLED, 1 = DISABLED.
    async def get_button_poweroff(self):
        return (await self.read(CMD_BTN_POWEROFF))[0] == 0

    async def set_button_poweroff(self, enabled):
        await self.write(CMD_BTN_POWEROFF, bytes([0 if enabled else 1]))

    # NOTE polarity: for reset-by-button 1 = ENABLED, 0 = DISABLED.
    async def get_button_reset(self):
        return (await self.read(CMD_BTN_RESET))[0] == 1

    async def set_button_reset(self, enabled):
        await self.write(CMD_BTN_RESET, bytes([1 if enabled else 0]))

    async def get_tagid_autofill(self):
        return (await self.read(CMD_TAGID_AUTOFILL))[0] == 1

    async def set_tagid_autofill(self, on):
        await self.write(CMD_TAGID_AUTOFILL, bytes([1 if on else 0]))

    async def get_phy(self):
        return {1: "legacy", 2: "long-range"}.get((await self.read(CMD_PHY))[0], "?")

    async def set_phy(self, mode):
        await self.write(CMD_PHY, bytes([2 if mode == "long-range" else 1]))

    async def get_channels(self):
        return channels_from_mask((await self.read(CMD_RF_CHANNEL))[0])

    async def set_channels(self, chs):
        await self.write(CMD_RF_CHANNEL, bytes([channel_mask(chs)]))

    async def get_battery_mode(self):
        return {1: "percentage", 2: "voltage"}.get((await self.read(CMD_BATT_ADV))[0], "?")

    async def set_battery_mode(self, mode):
        await self.write(CMD_BATT_ADV, bytes([1 if mode == "percentage" else 2]))

    # ---- clock & temperature/humidity logging ------------------------------
    async def get_time(self):
        return struct.unpack(">I", await self.read(CMD_TIMESTAMP))[0]

    async def set_time(self, unix=None):
        unix = int(time.time()) if unix is None else int(unix)
        await self.write(CMD_TIMESTAMP, struct.pack(">I", unix))

    async def get_th_store(self):
        d = await self.read(CMD_TH_STORE)
        return bool(d[0]), struct.unpack(">H", d[1:3])[0]   # (enabled, minutes)

    async def set_th_store(self, enabled, interval_min):
        if not 0 <= interval_min <= 65535:
            raise ValueError("storage interval must be 0-65535 minutes")
        await self.write(CMD_TH_STORE, bytes([1 if enabled else 0])
                         + struct.pack(">H", interval_min))

    async def get_th_sampling(self):
        return struct.unpack(">H", await self.read(CMD_TH_SAMPLING))[0]  # seconds

    async def set_th_sampling(self, seconds):
        if not 1 <= seconds <= 65535:
            raise ValueError("sampling interval must be 1-65535 seconds")
        await self.write(CMD_TH_SAMPLING, struct.pack(">H", seconds))

    # ---- advertising slots -------------------------------------------------
    async def get_slot(self, idx):
        if idx not in (0, 1, 2):
            raise ValueError("slot index must be 0, 1 or 2")
        return parse_slot(await self.read(CMD_SLOT, data=bytes([idx])))

    async def put_slot(self, s):
        await self.write(CMD_SLOT, build_slot(s))

    async def modify_slot(self, idx, interval_ms=None, duration_s=None,
                          standby_s=None, rssi_dbm=None, adv_type=None,
                          content=None):
        """Read-modify-write: untouched fields keep the device's own values."""
        s = await self.get_slot(idx)
        if interval_ms is not None: s["interval_ms"] = interval_ms
        if duration_s is not None: s["duration_s"] = duration_s
        if standby_s is not None: s["standby_s"] = standby_s
        if rssi_dbm is not None: s["rssi_dbm"] = rssi_dbm
        if adv_type is not None: s["adv_type"] = adv_type
        if content is not None: s["content"] = content
        check_interval(s["interval_ms"])
        await self.put_slot(s)
        return await self.get_slot(idx)       # read back to confirm

    async def save_as_factory(self):
        await self.write(CMD_SAVE_FACTORY, b"")

    # ---- stored history ----------------------------------------------------
    async def get_history_count(self):
        return int.from_bytes(await self.read(CMD_TH_COUNT), "big")

    async def get_counter(self):
        """Seconds since the sensor booted (used to date records when the
        device clock was never synced)."""
        return int.from_bytes(await self.read(CMD_COUNTER), "big")

    async def clear_history(self):
        await self.write(CMD_TH_CLEAR, b"")

    async def download_history(self, idle_timeout=15.0, progress=None):
        """Same sequence as the vendor app (ExportTHDataActivity):
        1. read the device counter (0x3E)  2. read the record count (0x43)
        3. enable indications on AA09 - the sensor then pushes all packets.
        Returns (records, announced_count); records = [(unix_ts, degC, %RH)].
        """
        counter = await self.get_counter()
        boot_time = time.time() - counter
        expected = await self.get_history_count()
        if expected == 0:
            return [], 0

        while not self.hist.empty():
            self.hist.get_nowait()
        got = {}

        def on_ind(_, d):
            self.hist.put_nowait(bytes(d))

        await self.c.start_notify(CH_HIS, on_ind)
        try:
            while True:
                try:
                    pkt = await asyncio.wait_for(self.hist.get(), idle_timeout)
                except asyncio.TimeoutError:
                    break
                total, idx, recs = parse_history_packet(pkt)
                got[idx] = recs
                if progress:
                    progress(sum(len(v) for v in got.values()), expected)
                if total == 0 or idx == total - 1:
                    break
        finally:
            try:
                await self.c.stop_notify(CH_HIS)
            except Exception:
                pass
        out = []
        for i in sorted(got):
            for ts, t, h in got[i]:
                out.append((normalize_ts(ts, boot_time), t, h))
        out.sort(key=lambda r: r[0])
        return out, expected


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
async def cmd_scan(args):
    from bleak import BleakScanner
    print("Scanning 8 s ...")
    found = await BleakScanner.discover(timeout=8.0, return_adv=True)
    for addr, (dev, adv) in sorted(found.items(), key=lambda kv: -kv[1][1].rssi):
        name = dev.name or adv.local_name or ""
        print(f"{addr}  {adv.rssi:4d} dBm  {name}")
        for f in decode_adv(adv):
            print("      " + format_frame(f))


async def show(sn):
    async def tryit(label, coro):
        try:
            print(f"{label:28s} {await coro}")
        except Exception as e:                     # keep going on partial support
            print(f"{label:28s} (unavailable: {e})")

    await tryit("Connectable", sn.get_connectable())
    await tryit("Password required", sn.password_required())
    await tryit("Button power-off enabled", sn.get_button_poweroff())
    await tryit("Button reset enabled", sn.get_button_reset())
    await tryit("Tag ID autofill", sn.get_tagid_autofill())
    await tryit("PHY mode", sn.get_phy())
    await tryit("Advertising channels", sn.get_channels())
    await tryit("Battery ADV mode", sn.get_battery_mode())
    try:
        t = await sn.get_time()
        print(f"{'Device clock':28s} {t} ({time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(t))} UTC)")
    except Exception as e:
        print(f"{'Device clock':28s} (unavailable: {e})")
    try:
        en, mins = await sn.get_th_store()
        print(f"{'T&H storage':28s} {'on' if en else 'off'}, every {mins} min (0 = every sample)")
    except Exception as e:
        print(f"{'T&H storage':28s} (unavailable: {e})")
    try:
        print(f"{'T&H sampling interval':28s} {await sn.get_th_sampling()} s")
    except Exception as e:
        print(f"{'T&H sampling interval':28s} (unavailable: {e})")
    for i in range(3):
        try:
            print(describe_slot(await sn.get_slot(i)))
        except Exception as e:
            print(f"SLOT{i + 1}: (unavailable: {e})")


async def apply_sets(sn, a):
    """Apply each requested change in a safe order, print each result."""
    async def step(label, coro):
        await coro
        print(f"  OK  {label}")

    if a.sync_time:
        await step("clock synced to PC (UTC)", sn.set_time())
    if a.th_sampling is not None:
        await step(f"T&H sampling = {a.th_sampling} s", sn.set_th_sampling(a.th_sampling))
    if a.th_store is not None or a.th_store_interval is not None:
        cur_en, cur_iv = await sn.get_th_store()
        en = cur_en if a.th_store is None else a.th_store == "on"
        iv = cur_iv if a.th_store_interval is None else a.th_store_interval
        await step(f"T&H storage = {'on' if en else 'off'}, every {iv} min",
                   sn.set_th_store(en, iv))
    if a.battery_mode:
        await step(f"battery ADV = {a.battery_mode}", sn.set_battery_mode(a.battery_mode))
    if a.phy:
        await step(f"PHY = {a.phy}", sn.set_phy(a.phy))
    if a.channels:
        chs = [int(c) for c in a.channels.split(",")]
        await step(f"ADV channels = {chs}", sn.set_channels(chs))
    if a.tagid_autofill:
        await step(f"tag-ID autofill = {a.tagid_autofill}",
                   sn.set_tagid_autofill(a.tagid_autofill == "on"))
    if a.button_poweroff:
        await step(f"power-off button = {a.button_poweroff}",
                   sn.set_button_poweroff(a.button_poweroff == "on"))
    if a.button_reset:
        await step(f"reset button = {a.button_reset}",
                   sn.set_button_reset(a.button_reset == "on"))

    if a.broadcast_interval is not None or a.broadcast_duration is not None \
            or a.broadcast_standby is not None:
        try:
            if a.broadcast_interval is not None:
                check_interval(a.broadcast_interval)
        except ValueError as e:
            sys.exit(f"--broadcast-interval: {e}")
        await apply_broadcast(sn, a.broadcast_interval, a.broadcast_duration,
                              a.broadcast_standby)
    if a.broadcast_every is not None:
        iv, du, sb = cycle_for_period(a.broadcast_every)
        print(f"  one broadcast burst every ~{a.broadcast_every} s "
              f"(interval {iv} ms, {du} s on, {sb} s sleep)")
        await apply_broadcast(sn, iv, du, sb)
    if a.slot is not None:
        idx = a.slot - 1
        content, adv_type = None, None
        if a.slot_type:
            adv_type = ADV_TYPE_BY_NAME[a.slot_type]
        if a.sensor_name is not None or a.tag_id is not None:
            cur = await sn.get_slot(idx)
            name, tag = a.sensor_name, a.tag_id
            if (name is None or tag is None) and cur["adv_type"] == 0x80 and cur["content"]:
                n = cur["content"][0]
                name = name if name is not None else cur["content"][1:1 + n].decode("ascii", "replace")
                tl = cur["content"][1 + n]
                tag = tag if tag is not None else cur["content"][2 + n:2 + n + tl].hex()
            if name is None or tag is None:
                sys.exit("--sensor-name and --tag-id are both needed unless "
                         "the slot is already a Sensor-info slot")
            content = build_sensor_info_content(name, tag)[1:]   # drop type byte
            adv_type = 0x80
        res = await sn.modify_slot(
            idx, interval_ms=a.interval, duration_s=a.duration,
            standby_s=a.standby, rssi_dbm=a.rssi, adv_type=adv_type, content=content)
        print("  OK  slot written; read-back:", describe_slot(res))

    if a.connectable:
        if a.connectable == "off" and not a.i_understand:
            sys.exit("Refusing: non-connectable can only be undone by a hardware "
                     "factory reset. Add --i-understand to proceed.")
        await step(f"connectable = {a.connectable}", sn.set_connectable(a.connectable == "on"))
    if a.save_factory:
        await step("current parameters saved as factory defaults", sn.save_as_factory())
    if a.new_password:
        await step("connection password changed", sn.set_password(a.new_password))
    if a.password_required:
        await step(f"password verification = {a.password_required}",
                   sn.set_password_required(a.password_required == "on"))


async def run(args):
    if args.cmd == "scan":
        return await cmd_scan(args)
    from bleak import BleakClient
    if not args.address:
        sys.exit("--address is required (use `scan` to find it)")
    async with BleakClient(args.address, timeout=20.0) as client:
        sn = Sensor(client)
        await sn.start()
        if await sn.password_required():
            await sn.authenticate(args.password)
            print("password accepted")
        else:
            print("device does not require a password")
        if args.cmd == "show":
            await show(sn)
        elif args.cmd == "menu":
            await interactive(sn)
        elif args.cmd == "set":
            await apply_sets(sn, args)
        elif args.cmd == "history":
            await do_history(sn, args.out)
        elif args.cmd == "clear-history":
            await sn.clear_history()
            print("stored history erased")


async def do_history(sn, path):
    n = await sn.get_history_count()
    print(f"{n} records stored on the sensor")
    if n == 0:
        return 0
    def prog(got, total):
        print(f"\r  received {got}/{total}", end="", flush=True)
    recs, expected = await sn.download_history(progress=prog)
    print()
    if len(recs) < expected:
        print(f"  WARNING: only {len(recs)} of {expected} records arrived "
              "(link dropped or timed out) - move closer and retry")
    save_history_csv(recs, path)
    print(f"saved {len(recs)} records to {path}")
    if recs:
        t = [r[1] for r in recs]; h = [r[2] for r in recs]
        print(f"  {time.strftime('%Y-%m-%d %H:%M', time.gmtime(recs[0][0]))} -> "
              f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(recs[-1][0]))} UTC | "
              f"temp {min(t)}..{max(t)} C | humidity {min(h)}..{max(h)} %")
    return len(recs)


# --------------------------------------------------------------------------
# Settings reference + interactive menu
# --------------------------------------------------------------------------
async def ask(prompt, parse, default_hint=""):
    """Prompt until `parse(text)` succeeds. Blank input -> None (skip)."""
    while True:
        raw = (await asyncio.to_thread(input, prompt)).strip()
        if raw == "":
            return None
        try:
            return parse(raw)
        except Exception as e:
            print(f"  invalid: {e}")


def p_int(lo, hi):
    def f(s):
        v = int(s)
        if not lo <= v <= hi:
            raise ValueError(f"must be {lo}..{hi}")
        return v
    return f


def check_interval_str(s):
    return check_interval(int(s))


def p_choice(*opts):
    def f(s):
        s = s.lower()
        if s not in opts:
            raise ValueError("choose one of: " + ", ".join(opts))
        return s
    return f


def p_channels(s):
    chs = sorted({int(x) for x in s.replace(" ", "").split(",") if x})
    channel_mask(chs)               # validates
    return chs


def p_password(s):
    if not 1 <= len(s) <= 16 or not s.isascii():
        raise ValueError("1-16 ASCII characters")
    return s


async def _slot_wizard(sn):
    n = await ask("  Slot number (1-3): ", p_int(1, 3))
    if n is None:
        return None
    cur = await sn.get_slot(n - 1)
    print("  current:", describe_slot(cur))
    print("  (press Enter on any question to keep the current value)")
    kw = {}
    t = await ask("  Frame type [uid/url/tlm/ibeacon/sensorinfo/none]: ",
                  p_choice(*ADV_TYPE_BY_NAME))
    if t:
        kw["adv_type"] = ADV_TYPE_BY_NAME[t]
    kw["interval_ms"] = await ask("  Advertising interval, ms (100-10000, steps of 100): ", check_interval_str)
    kw["duration_s"] = await ask("  Advertising duration, s (0-65535): ", p_int(0, 65535))
    kw["standby_s"] = await ask("  Standby duration, s (0 = low-power mode off): ", p_int(0, 65535))
    kw["rssi_dbm"] = await ask("  RSSI@0m, dBm (-100..0): ", p_int(-100, 0))
    if (t or "sensorinfo") in ("sensorinfo", "sensor") and \
            (t or cur["adv_type"] == 0x80):
        name = await ask("  Device name (1-20 ASCII chars): ",
                         lambda s: s if 1 <= len(s) <= 20 and s.isascii()
                         else (_ for _ in ()).throw(ValueError("1-20 ASCII chars")))
        tag = await ask("  Tag ID, hex (1-6 bytes, e.g. 000001): ",
                        lambda s: (bytes.fromhex(s), s)[1] if 1 <= len(bytes.fromhex(s)) <= 6
                        else (_ for _ in ()).throw(ValueError("1-6 bytes")))
        if name or tag:
            c = cur["content"]
            if cur["adv_type"] == 0x80 and c:
                n0 = c[0]
                name = name or c[1:1 + n0].decode("ascii", "replace")
                tag = tag or c[2 + n0:2 + n0 + c[1 + n0]].hex()
            if not (name and tag):
                print("  need both name and tag ID when changing a slot to Sensor info")
                return None
            kw["content"] = build_sensor_info_content(name, tag)[1:]
            kw["adv_type"] = 0x80
    elif t in ("uid", "url", "ibeacon", "tlm"):
        raw = await ask(f"  {t.upper()} content as hex bytes after the type byte "
                        "(see the 'Settings reference'; Enter = keep): ",
                        lambda s: bytes.fromhex(s.replace(" ", "")))
        if raw is not None:
            kw["content"] = raw
    return n - 1, kw


def _fmt_on(v):
    return "on" if v else "off"


# Each entry: key, title, what it does, allowed values, current-value reader,
# prompt coroutine (returns the value to apply, or None to cancel), applier.
SETTINGS = [
    dict(key="sync_time", title="Device clock",
         desc="Sets the sensor's internal clock (Unix time, UTC). Stored "
              "temperature/humidity records are stamped with this clock, so "
              "set it after every battery change.",
         values="No input - uses this computer's current time",
         read=lambda sn: sn.get_time(),
         fmt=lambda t: f"{t} ({time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(t))} UTC)",
         prompt=lambda sn: ask("  Type 'now' to set the clock to this PC's time: ",
                               p_choice("now")),
         apply=lambda sn, v: sn.set_time()),
    dict(key="th_sampling", title="T&H sampling interval",
         desc="How often the sensor measures temperature and humidity.",
         values="1-65535 seconds",
         read=lambda sn: sn.get_th_sampling(), fmt=lambda v: f"{v} s",
         prompt=lambda sn: ask("  New sampling interval (s): ", p_int(1, 65535)),
         apply=lambda sn, v: sn.set_th_sampling(v)),
    dict(key="th_store", title="T&H data logging (on/off)",
         desc="Turns saving of measurements to the sensor's flash memory on or "
              "off (up to 60000 records on L02S). Needed for the history download.",
         values="on / off",
         read=lambda sn: sn.get_th_store(), fmt=lambda v: _fmt_on(v[0]),
         prompt=lambda sn: ask("  Logging [on/off]: ", p_choice("on", "off")),
         apply=lambda sn, v: _apply_store(sn, enabled=(v == "on"))),
    dict(key="th_store_interval", title="T&H logging interval",
         desc="How often a measurement is written to flash. 0 = save every "
              "sample. Only has an effect when logging is on.",
         values="0-65535 minutes",
         read=lambda sn: sn.get_th_store(), fmt=lambda v: f"{v[1]} min",
         prompt=lambda sn: ask("  Logging interval (min): ", p_int(0, 65535)),
         apply=lambda sn, v: _apply_store(sn, interval=v)),
    dict(key="frequency", title="Broadcast frequency (all active slots)",
         desc="How often the sensor sends its advertising packets, applied to "
              "every active slot at once. A shorter interval = faster to "
              "detect and more up-to-date readings, but uses more battery. "
              "Optionally add a sleep cycle (broadcast for N s, then sleep) to "
              "save battery. Note: for the first 30 s after power-on the "
              "sensor always broadcasts every 1000 ms (configuration period), "
              "and trigger-based fast broadcasting (e.g. while moving) is set "
              "in the BeaconX Pro app, not here.",
         values="interval 100-10000 ms in steps of 100 | \"once every N seconds\" "
                "(up to ~18 h, via sleep cycle) | "
                "optional duration 1-65535 s and standby 0-65535 s",
         read=lambda sn: broadcast_summary(sn), fmt=str,
         prompt=lambda sn: _ask_broadcast(sn),
         apply=lambda sn, v: apply_broadcast(sn, *v)),
    dict(key="slot", title="Advertising slot (what the sensor broadcasts)",
         desc="Up to 3 slots. Each has a frame type, interval, duration, "
              "standby time, RSSI@0m and content. Duration + standby make the "
              "low-power cycle (broadcast for 'duration', sleep for 'standby'); "
              "standby 0 = broadcast continuously. Frame types: UID, URL, TLM, "
              "iBeacon, Sensor info (name + tag ID + sensor data), or none. "
              "Setting all 3 to none stops all broadcasting. Normal mode only - "
              "trigger settings must be done in the BeaconX Pro app.",
         values="slot 1-3 | interval 100-10000 ms (steps of 100) | duration 1-65535 s | "
                "standby 0-65535 s | RSSI@0m -100..0 dBm | name 1-20 chars | "
                "tag ID 1-6 hex bytes",
         read=None, fmt=None,
         prompt=_slot_wizard,
         apply=lambda sn, v: _apply_slot(sn, v)),
    dict(key="battery_mode", title="Battery value in Sensor-info frame",
         desc="Whether the broadcast reports battery as a percentage or as "
              "millivolts.",
         values="percentage / voltage",
         read=lambda sn: sn.get_battery_mode(), fmt=str,
         prompt=lambda sn: ask("  Battery mode [percentage/voltage]: ",
                               p_choice("percentage", "voltage")),
         apply=lambda sn, v: sn.set_battery_mode(v)),
    dict(key="phy", title="Advertising PHY (range)",
         desc="Legacy = standard 1M PHY, ~150 m. Long-range = Coded PHY, up to "
              "~350 m, but only phones/gateways with Bluetooth 5 coded-PHY "
              "scanning will see it.",
         values="legacy / long-range",
         read=lambda sn: sn.get_phy(), fmt=str,
         prompt=lambda sn: ask("  PHY [legacy/long-range]: ", p_choice("legacy", "long-range")),
         apply=lambda sn, v: sn.set_phy(v)),
    dict(key="channels", title="Advertising channels",
         desc="Which BLE advertising channels are used. All three = easiest to "
              "detect; a single channel uses less power but scanners may miss it.",
         values="any of 37, 38, 39 (comma separated, e.g. 37,38,39)",
         read=lambda sn: sn.get_channels(), fmt=lambda c: ",".join(map(str, c)),
         prompt=lambda sn: ask("  Channels: ", p_channels),
         apply=lambda sn, v: sn.set_channels(v)),
    dict(key="tagid_autofill", title="Tag-ID autofill",
         desc="When on, the Sensor-info Tag ID is automatically set to the "
              "sensor's MAC address.",
         values="on / off",
         read=lambda sn: sn.get_tagid_autofill(), fmt=_fmt_on,
         prompt=lambda sn: ask("  Autofill [on/off]: ", p_choice("on", "off")),
         apply=lambda sn, v: sn.set_tagid_autofill(v == "on")),
    dict(key="button_poweroff", title="Power-off by button/magnet",
         desc="Allows the button (or magnet) to switch the sensor off. Turn off "
              "to prevent accidental or unauthorised shutdown.",
         values="on / off",
         read=lambda sn: sn.get_button_poweroff(), fmt=_fmt_on,
         prompt=lambda sn: ask("  Power-off by button [on/off]: ", p_choice("on", "off")),
         apply=lambda sn, v: sn.set_button_poweroff(v == "on")),
    dict(key="button_reset", title="Factory reset by button/magnet",
         desc="Allows the 10 s-hold + press hardware reset sequence. Turn off "
              "to prevent unwanted resets (a remote reset in the app still works).",
         values="on / off",
         read=lambda sn: sn.get_button_reset(), fmt=_fmt_on,
         prompt=lambda sn: ask("  Reset by button [on/off]: ", p_choice("on", "off")),
         apply=lambda sn, v: sn.set_button_reset(v == "on")),
    dict(key="connectable", title="Connectable",
         desc="DANGER: when off, the sensor no longer accepts connections - you "
              "cannot configure it again until a hardware factory reset "
              "(button/magnet hold, or the battery-reinsert sequence).",
         values="on / off", danger=True,
         read=lambda sn: sn.get_connectable(), fmt=_fmt_on,
         prompt=lambda sn: ask("  Connectable [on/off]: ", p_choice("on", "off")),
         apply=lambda sn, v: sn.set_connectable(v == "on")),
    dict(key="password_required", title="Password verification",
         desc="When on, a password is required to connect and change anything. "
              "While it is off you cannot change the password or reset the "
              "beacon remotely.",
         values="on / off",
         read=lambda sn: sn.password_required(), fmt=_fmt_on,
         prompt=lambda sn: ask("  Password verification [on/off]: ", p_choice("on", "off")),
         apply=lambda sn, v: sn.set_password_required(v == "on")),
    dict(key="new_password", title="Change connection password",
         desc="Sets a new connection password. Default is 'Moko4321'. A "
              "software factory reset does NOT restore it - write the new one "
              "down.",
         values="1-16 ASCII characters",
         read=None, fmt=None, danger=True,
         prompt=lambda sn: ask("  New password: ", p_password),
         apply=lambda sn, v: sn.set_password(v)),
    dict(key="history", title="Download stored T&H history (CSV)",
         desc="Reads all temperature/humidity records saved in the sensor's "
              "flash (needs T&H data logging on) and saves them to a CSV file "
              "with time (UTC), temperature and humidity. If the clock was "
              "never synced, times are estimated from the sensor's uptime "
              "counter, so sync the clock (item 1) before logging for "
              "accurate times.",
         values="a file name (Enter = history.csv)",
         read=lambda sn: sn.get_history_count(), fmt=lambda n: f"{n} records stored",
         prompt=lambda sn: _ask_file(),
         apply=lambda sn, v: do_history(sn, v)),
    dict(key="clear_history", title="Erase stored T&H history",
         desc="Permanently deletes all records stored in the sensor's flash. "
              "Download them first.",
         values="No input - confirmation only", danger=True,
         read=None, fmt=None,
         prompt=lambda sn: ask("  Type 'yes' to erase all stored records: ", p_choice("yes")),
         apply=lambda sn, v: sn.clear_history()),
    dict(key="save_factory", title="Save current settings as factory defaults",
         desc="Overwrites the sensor's factory-default parameters with the "
              "current ones, so a later reset returns to these values.",
         values="No input - confirmation only", danger=True,
         read=None, fmt=None,
         prompt=lambda sn: ask("  Type 'yes' to continue: ", p_choice("yes")),
         apply=lambda sn, v: sn.save_as_factory()),
]


async def apply_broadcast(sn, interval_ms=None, duration_s=None, standby_s=None):
    """Change broadcast timing on every slot that is actually broadcasting."""
    changed = 0
    for idx in range(3):
        cur = await sn.get_slot(idx)
        if cur["adv_type"] == 0xFF:                 # "No data" = slot disabled
            continue
        res = await sn.modify_slot(idx, interval_ms=interval_ms,
                                   duration_s=duration_s, standby_s=standby_s)
        print("  " + describe_slot(res))
        changed += 1
    if not changed:
        print("  no active slots - nothing changed")


async def broadcast_summary(sn):
    parts = []
    for idx in range(3):
        s = await sn.get_slot(idx)
        if s["adv_type"] == 0xFF:
            parts.append(f"SLOT{idx + 1} off")
        else:
            cyc = (f", {s['duration_s']}s on / {s['standby_s']}s sleep"
                   if s["standby_s"] else ", continuous")
            parts.append(f"SLOT{idx + 1} every {s['interval_ms']} ms{cyc}")
    return " | ".join(parts)


async def _ask_broadcast(sn):
    print("  How often should the sensor broadcast?")
    print("    1) very often   - every 100 ms   (fastest to detect, most battery use)")
    print("    2) often        - every 500 ms")
    print("    3) normal       - every 1000 ms  (factory default)")
    print("    4) slow         - every 2000 ms")
    print("    5) very slow    - every 5000 ms")
    print("    6) slowest      - every 10000 ms (the longest single interval allowed)")
    print("    7) once every N seconds/minutes (uses a sleep cycle, e.g. 60 = once a minute)")
    print("    8) custom       - enter your own interval (100-10000 ms, steps of 100)")
    choice = await ask("  Choose 1-8 (Enter = cancel): ", p_int(1, 8))
    if choice is None:
        return None
    if choice == 7:
        sec = await ask("  Broadcast once every how many seconds? (2-65536, 60 = 1 min): ",
                        p_int(2, 65536))
        if sec is None:
            return None
        return cycle_for_period(sec)
    if choice == 8:
        interval = await ask("  Interval in ms (100-10000, steps of 100): ", check_interval_str)
        if interval is None:
            return None
    else:
        interval = {1: 100, 2: 500, 3: 1000, 4: 2000, 5: 5000, 6: 10000}[choice]
    print("  Optional power-saving cycle: broadcast for N seconds, then sleep.")
    dur = await ask("  Broadcast duration, s (Enter = keep current): ", p_int(1, 65535))
    stb = None
    if dur is not None:
        stb = await ask("  Sleep (standby) between bursts, s (0 = never sleep): ", p_int(0, 65535))
    return interval, dur, stb


async def _ask_file():
    return (await ask('  Save to file [history.csv]: ', lambda s: s)) or 'history.csv'


async def _apply_store(sn, enabled=None, interval=None):
    cur_en, cur_iv = await sn.get_th_store()      # command carries both fields
    await sn.set_th_store(cur_en if enabled is None else enabled,
                          cur_iv if interval is None else interval)


async def _apply_slot(sn, v):
    idx, kw = v
    kw = {k: x for k, x in kw.items() if x is not None}
    res = await sn.modify_slot(idx, **kw)
    print("  read-back:", describe_slot(res))


def print_reference():
    print("SETTINGS REFERENCE\n" + "=" * 60)
    for n, s in enumerate(SETTINGS, 1):
        print(f"\n{n:2d}. {s['title']}" + ("   [use with care]" if s.get("danger") else ""))
        print(f"    {s['desc']}")
        print(f"    Allowed: {s['values']}")


async def read_current(sn, s):
    if not s.get("read"):
        return None
    try:
        return s["fmt"](await s["read"](sn))
    except Exception as e:
        return f"(unavailable: {e})"


async def interactive(sn):
    print("\nInteractive configuration - choose a number, 'r' to refresh, "
          "'q' to quit.")
    while True:
        print("\n" + "-" * 60)
        for n, s in enumerate(SETTINGS, 1):
            cur = await read_current(sn, s)
            print(f"{n:2d}. {s['title']:<46s}{'' if cur is None else cur}")
        choice = (await asyncio.to_thread(input, "\nSetting number (? N for help): ")).strip().lower()
        if choice in ("q", "quit", "exit"):
            return
        if choice in ("", "r"):
            continue
        help_only = choice.startswith("?")
        num = choice.lstrip("? ").strip()
        if not num.isdigit() or not 1 <= int(num) <= len(SETTINGS):
            print("  not a valid number")
            continue
        s = SETTINGS[int(num) - 1]
        print(f"\n{s['title']}\n  {s['desc']}\n  Allowed: {s['values']}")
        cur = await read_current(sn, s)
        if cur is not None:
            print(f"  Current: {cur}")
        if help_only:
            continue
        try:
            val = await s["prompt"](sn)
            if val is None:
                print("  (cancelled / unchanged)")
                continue
            if s.get("danger"):
                ok = await ask(f"  This is a sensitive change. Type 'confirm' to apply: ",
                               p_choice("confirm"))
                if ok is None:
                    print("  (cancelled)")
                    continue
            await s["apply"](sn, val)
            print("  OK - applied")
        except (DeviceError, ValueError) as e:
            print(f"  FAILED: {e}")


def selftest():
    """Check frame builders/parsers against the examples in the spreadsheet."""
    H = lambda s: bytes.fromhex(s.replace(" ", ""))
    assert build(READ, CMD_PWD_ENABLE) == H("EA 00 53 00")
    assert build(WRITE, CMD_PWD_ENABLE, b"\x01") == H("EA 01 53 01 01")
    assert build(WRITE, CMD_PWD_SET, b"Moko4321") == H("EA 01 52 08 4D 6F 6B 6F 34 33 32 31")
    assert build(WRITE, CMD_PWD_VERIFY, b"Moko4321") == H("EA 01 51 08 4D 6F 6B 6F 34 33 32 31")
    assert build(WRITE, CMD_CONNECTABLE, b"\x01") == H("EA 01 37 01 01")
    assert build(WRITE, CMD_BTN_POWEROFF, b"\x00") == H("EA 01 25 01 00")
    assert build(WRITE, CMD_BTN_RESET, b"\x01") == H("EA 01 23 01 01")
    assert build(WRITE, CMD_TAGID_AUTOFILL, b"\x00") == H("EA 01 3C 01 00")
    assert build(WRITE, CMD_PHY, b"\x02") == H("EA 01 3A 01 02")
    assert build(WRITE, CMD_BATT_ADV, b"\x02") == H("EA 01 6C 01 02")
    assert build(WRITE, CMD_RF_CHANNEL, bytes([channel_mask([37, 38, 39])])) == H("EA 01 35 01 07")
    assert channel_mask([39]) == 4 and channels_from_mask(6) == [38, 39]
    assert build(WRITE, CMD_TIMESTAMP, struct.pack(">I", 0)) == H("EA 01 3F 04 00 00 00 00")
    assert build(WRITE, CMD_TH_STORE, b"\x01" + struct.pack(">H", 5)) == H("EA 01 40 03 01 00 05")
    assert build(WRITE, CMD_TH_SAMPLING, struct.pack(">H", 300)) == H("EA 01 41 02 01 2C")
    assert build(READ, CMD_SLOT, b"\x00") == H("EA 00 34 01 00")
    assert build(WRITE, CMD_SAVE_FACTORY) == H("EA 01 27 00")
    # slot round-trip using the spreadsheet's example payload
    ex = H("00 03 E8 00 0A 00 00 00 00 01 00 00 00 00 00 00 00 00 00 00 02 00 00 00 00 00")
    s = parse_slot(ex)
    assert (s["slot"], s["interval_ms"], s["duration_s"], s["standby_s"]) == (0, 1000, 10, 0)
    assert s["adv_type"] == 0x00 and build_slot(s) == ex
    # reply parsing
    assert parse_rx(H("EB 00 3A 01 01")) == (0, 0x3A, b"\x01")
    assert parse_rx(H("EB 01 41 01 AA")) == (1, 0x41, b"\xAA")
    # sensor info content
    assert build_sensor_info_content("MK Sensor", "000001") == (
        bytes([0x80, 9]) + b"MK Sensor" + bytes([3, 0, 0, 1]))
    # history packet
    pkt = H("EB 03 80 00 04 00 00 08") + struct.pack(">IhH", 1700000000, 215, 456)
    assert parse_history_packet(pkt) == (4, 0, [(1700000000, 21.5, 45.6)])
    assert normalize_ts(1700000000, 5) == 1700000000.0
    assert normalize_ts(100, 1700000000.0) == 1700000100.0
    assert build(READ, CMD_TH_COUNT) == H("EA 00 43 00")
    assert build(READ, CMD_COUNTER) == H("EA 00 3E 00")
    assert build(WRITE, CMD_TH_CLEAR) == H("EA 01 42 00")
    # Sensor-info example from the manual (Table 5)
    ex = H("80 1C 0000 0002 003A 0076 03A7 00C8 0137 0CCC 000001")
    f = decode_sensor_info(ex)
    assert f["temp_c"] == 20.0 and f["humidity"] == 31.1 and f["battery_mv"] == 3276
    assert f["motion_count"] == 2 and f["accel_mg"] == (58, 118, 935) and f["tag_id"] == "000001"
    assert f["moving"] is False and f["has_temp"] and f["has_humidity"] and not f["has_flash"]
    assert decode_sensor_info(H("80 1C 0000 0000 0000 0000 0000 F6FF 0200 0064 01"))["battery_pct"] == 100
    # negative temperature, -1.0 C
    assert decode_sensor_info(H("80 1C 0000 0000 0000 0000 0000 FFF6 0200 0064 01"))["temp_c"] == -1.0
    # Eddystone TLM example from the manual (3207 mV, 13.5 C, adv 1625, 1623.9 s)
    t = decode_tlm(H("20 00 0C87 0D80 00000659 00003F6F"))
    assert t["battery_mv"] == 3207 and t["temp_c"] == 13.5 and t["adv_count"] == 1625 and t["uptime_s"] == 1623.9
    assert decode_uid(H("00 00 0102030405060708090A 102030405060"))["instance"] == "102030405060"
    assert decode_url(H("10 00 00 6D6F6B6F736D617274 07"))["url"] == "http://www.mokosmart.com"
    ib = decode_ibeacon({0x004C: H("0215 000102030405060708090A0B0C0D0E0F 0001 0001 C5")})
    assert ib["major"] == 1 and ib["minor"] == 1 and ib["rssi_1m"] == -59
    assert check_interval(100) == 100 and check_interval(10000) == 10000
    for bad in (50, 250, 10100, 99):
        try:
            check_interval(bad); raise SystemExit(f"should reject {bad}")
        except ValueError:
            pass
    assert cycle_for_period(60) == (1000, 1, 59)
    assert normalize_mac("5c:c7:c1:f7:d4:28") == "5C:C7:C1:F7:D4:28"
    assert normalize_mac("5CC7C1F7D428") == "5C:C7:C1:F7:D4:28"
    assert normalize_mac("5c-c7-c1-f7-d4-28") == "5C:C7:C1:F7:D4:28"
    assert normalize_mac("12345678-1234-1234-1234-123456789ABC").count("-") == 4
    for bad in ("5C:C7:C1:F7:D4", "ZZ:C7:C1:F7:D4:28", ""):
        try:
            normalize_mac(bad); raise SystemExit("should reject " + bad)
        except ValueError:
            pass
    print("selftest: all checks passed")


# --------------------------------------------------------------------------
# Guided mode: just run `python bxps_config.py`
# --------------------------------------------------------------------------
def _make_cb(store):
    def cb(dev, adv):
        old = store.get(dev.address, {})
        name = dev.name or adv.local_name or old.get("name", "")
        mk = old.get("mk", False) or any("ea01" in k.lower() for k in adv.service_data)
        frames = decode_adv(adv) or old.get("frames", [])
        store[dev.address] = {"name": name, "rssi": adv.rssi, "mk": mk,
                              "new": dev.address not in store, "frames": frames,
                              "seen": time.time()}
    return cb


async def scan_timed(store, seconds=10):
    from bleak import BleakScanner
    async with BleakScanner(_make_cb(store)):
        for left in range(seconds, 0, -1):
            print(f"\rScanning... {left:2d}s  ({len(store)} devices found)  ", end="", flush=True)
            await asyncio.sleep(1)
    print(f"\rScan finished: {len(store)} devices found.            ")


async def listen_forever(store):
    """Scan continuously, printing devices and their decoded data as it
    changes, until Enter is pressed."""
    from bleak import BleakScanner
    print("Listening continuously (decoding broadcasts) - press Enter to stop.\n")
    stop = asyncio.ensure_future(asyncio.to_thread(input))
    last_key, last_print = {}, {}
    async with BleakScanner(_make_cb(store)):
        while not stop.done():
            now = time.time()
            for addr, d in list(store.items()):
                key = change_key(d["frames"])
                if addr not in last_key or (key != last_key[addr] and now - last_print[addr] >= 1.0):
                    first = addr not in last_key
                    last_key[addr], last_print[addr] = key, now
                    tag = "  <- MOKO sensor" if d["mk"] else ""
                    head = f"{time.strftime('%H:%M:%S')}  {addr}  {d['rssi']:4d} dBm  {d['name'] or '(no name)'}"
                    print(("  + " if first else "  ~ ") + head + tag)
                    for f in d["frames"]:
                        print("        " + format_frame(f))
            await asyncio.sleep(0.5)
    print(f"\nStopped. {len(store)} devices seen in total.")


def sorted_devices(store):
    return sorted(store.items(),
                  key=lambda kv: (not kv[1]["mk"], not kv[1]["name"], -kv[1]["rssi"]))


def print_devices(store):
    devs = sorted_devices(store)
    if not devs:
        print("  (no devices found)")
    for n, (addr, d) in enumerate(devs, 1):
        tag = "  <- MOKO sensor" if d["mk"] else ""
        print(f"{n:3d}. {addr}  {d['rssi']:4d} dBm  {d['name'] or '(no name)'}{tag}")
        for f in d.get("frames", []):
            print("       " + format_frame(f))
    return [a for a, _ in devs]


import re as _re


def normalize_mac(text):
    """Accept AA:BB:CC:DD:EE:FF, AA-BB-..., AABBCCDDEEFF (any case), or a
    macOS-style UUID. Returns the cleaned address or raises ValueError."""
    t = text.strip().strip("'\"")
    if _re.fullmatch(r"[0-9A-Fa-f]{8}-([0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}", t):
        return t.upper()                              # CoreBluetooth UUID
    h = _re.sub(r"[:\-\s.]", "", t)
    if not _re.fullmatch(r"[0-9A-Fa-f]{12}", h):
        raise ValueError("expected 6 bytes like AA:BB:CC:DD:EE:FF")
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)).upper()


async def configure_device(address, wait=None, attempts=1):
    """Connect and open the settings menu.

    wait=N: first scan up to N seconds until the device is heard advertising
    (needed for sensors on a sleep cycle, and for addresses not yet scanned),
    and retry up to `attempts` times."""
    from bleak import BleakClient, BleakScanner
    pw = (await asyncio.to_thread(input, "Password [Enter = Moko4321]: ")).strip() or "Moko4321"
    last_err = None
    for attempt in range(1, attempts + 1):
        target = address
        if wait:
            print(f"Looking for {address} (up to {wait} s; a sleeping sensor "
                  f"only broadcasts once per cycle) ... [{attempt}/{attempts}]")
            target = await BleakScanner.find_device_by_address(address, timeout=wait)
            if target is None:
                last_err = DeviceError("device not heard - wake it with the button/magnet "
                                       "(it is connectable for ~30 s after power-on) and move closer")
                continue
            print("  found it, connecting ...")
        else:
            print(f"Connecting to {address} ...")
        try:
            async with BleakClient(target, timeout=20.0) as client:
                sn = Sensor(client)
                await sn.start()
                if await sn.password_required():
                    await sn.authenticate(pw)
                    print("Password accepted.")
                else:
                    print("Device does not require a password.")
                await interactive(sn)
            print("Disconnected.")
            return
        except DeviceError as e:
            if "wrong password" in str(e):
                raise                               # retrying cannot help
            last_err = e
        except Exception as e:
            last_err = e
        if attempt < attempts:
            print(f"  attempt failed ({type(last_err).__name__}: {last_err}); retrying ...")
    raise last_err if last_err else DeviceError("could not connect")


async def guided():
    try:
        import bleak  # noqa: F401
    except ImportError:
        sys.exit("The 'bleak' package is required:  pip install bleak")
    store = {}
    print("BXP-S / MK Sensor configuration tool\n")
    await scan_timed(store, 10)
    while True:
        print("\nDevices:")
        addrs = print_devices(store)
        print("\n  <number>  configure that device"
              "\n  l         listen continuously (Enter to stop)"
              "\n  r         rescan for 10 s"
              "\n  q         quit")
        print(f"\n{len(addrs) + 1:3d}. [m] Connect using a MAC address "
              "(device not in the list, or asleep)")
        c = (await asyncio.to_thread(input, "> ")).strip().lower()
        if c in ("q", "quit", "exit"):
            return
        if c in ("m", "mac", str(len(addrs) + 1)):
            raw = (await asyncio.to_thread(
                input, "  MAC address (e.g. 5C:C7:C1:F7:D4:28), Enter = back: ")).strip()
            if not raw:
                continue
            try:
                mac = normalize_mac(raw)
            except ValueError as e:
                print(f"  invalid address: {e}")
                continue
            print("  Tip: if the sensor is on a sleep cycle, switch it off/on with the "
                  "button or magnet first - it is connectable for ~30 s after power-on.")
            try:
                await configure_device(mac, wait=1, attempts=300)
            except Exception as e:
                print(f"\nCould not configure device: {type(e).__name__}: {e}")
            continue
        if c == "l":
            await listen_forever(store)
        elif c == "r":
            await scan_timed(store, 10)
        elif c.isdigit() and 1 <= int(c) <= len(addrs):
            try:
                await configure_device(addrs[int(c) - 1], wait=1, attempts=300)
            except Exception as e:                   # keep the program alive
                print(f"\nCould not configure device: {type(e).__name__}: {e}")
                print("Tips: wake the sensor (it advertises ~30 s after power-on), "
                      "check the password, and move closer.")
        else:
            print("  not a valid choice")


def main():
    if len(sys.argv) == 1:                       # double-click / `python file.py`
        try:
            asyncio.run(guided())
        except KeyboardInterrupt:
            print("\nBye.")
        return
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-a", "--address", help="BLE address (MAC, or UUID on macOS)")
    p.add_argument("-p", "--password", default="Moko4321", help="connection password")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="list nearby BLE devices")
    sub.add_parser("show", help="read and print all settings")
    sub.add_parser("selftest", help="verify frame encoding against the spec examples")
    sub.add_parser("menu", help="interactive: pick a setting, see what it does, enter a value")
    sub.add_parser("settings", help="print what can be set for each setting (no sensor needed)")

    h = sub.add_parser("history", help="download stored temperature/humidity data to CSV")
    h.add_argument("--out", default="history.csv")
    sub.add_parser("clear-history", help="erase the stored temperature/humidity data")

    s = sub.add_parser("set", help="change settings (only the flags you pass)")
    onoff = ["on", "off"]
    s.add_argument("--sync-time", action="store_true", help="set device clock to now (UTC)")
    s.add_argument("--th-sampling", type=int, metavar="SEC", help="T&H sampling interval 1-65535 s")
    s.add_argument("--th-store", choices=onoff, help="store T&H samples to flash")
    s.add_argument("--th-store-interval", type=int, metavar="MIN",
                   help="storage interval in minutes (0 = every sample)")
    s.add_argument("--battery-mode", choices=["percentage", "voltage"])
    s.add_argument("--phy", choices=["legacy", "long-range"])
    s.add_argument("--channels", help="comma list from 37,38,39 e.g. 37,38,39")
    s.add_argument("--tagid-autofill", choices=onoff)
    s.add_argument("--button-poweroff", choices=onoff, help="power-off-by-button function")
    s.add_argument("--button-reset", choices=onoff, help="reset-by-button function")
    s.add_argument("--connectable", choices=onoff)
    s.add_argument("--i-understand", action="store_true",
                   help="required with --connectable off (irreversible without hardware reset)")
    s.add_argument("--new-password", help="new connection password (1-16 chars)")
    s.add_argument("--password-required", choices=onoff)
    s.add_argument("--save-factory", action="store_true",
                   help="store current parameters as the factory defaults")
    f = s.add_argument_group("broadcast frequency (applies to all active slots)")
    f.add_argument("--broadcast-interval", type=int, metavar="MS",
                   help="100-10000 ms between packets, steps of 100")
    f.add_argument("--broadcast-every", type=int, metavar="SEC",
                   help="broadcast once every N seconds using a sleep cycle (60 = once a minute)")
    f.add_argument("--broadcast-duration", type=int, metavar="SEC", help="burst length, s (power-saving cycle)")
    f.add_argument("--broadcast-standby", type=int, metavar="SEC", help="sleep between bursts, s (0 = never)")
    g = s.add_argument_group("advertising slot (normal mode, no trigger)")
    g.add_argument("--slot", type=int, choices=[1, 2, 3])
    g.add_argument("--slot-type", choices=sorted(ADV_TYPE_BY_NAME))
    g.add_argument("--interval", type=int, metavar="MS", help="adv interval ms (100-10000, steps of 100)")
    g.add_argument("--duration", type=int, metavar="SEC", help="adv duration s")
    g.add_argument("--standby", type=int, metavar="SEC", help="standby duration s (0 = low-power mode off)")
    g.add_argument("--rssi", type=int, metavar="DBM", help="RSSI@0m, -100..0")
    g.add_argument("--sensor-name", help="Sensor-info device name (1-20 chars)")
    g.add_argument("--tag-id", help="Sensor-info tag ID as hex (1-6 bytes), e.g. 000001")

    args = p.parse_args()
    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "settings":
        return print_reference()
    try:
        asyncio.run(run(args))
    except DeviceError as e:
        sys.exit(f"device error: {e}")


if __name__ == "__main__":
    main()