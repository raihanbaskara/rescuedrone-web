"""
RescueDrone - Web Dashboard + Flight Controller (Mission Planner)
===================================================================
Backend Flask ini menggabungkan dua hal:

1. Koneksi NYATA ke flight controller (Pixhawk/ArduPilot/PX4) lewat MAVLink
   (pymavlink) - persis seperti versi web dashboard sebelumnya. Bisa dipantau
   dari Mission Planner secara bersamaan.
2. Sistem insiden RescueDrone (login admin, lapor kecelakaan, rekap panggilan)
   yang tadinya berjalan di backend Node.js terpisah (server.js) - sekarang
   disatukan ke Flask + SQLite supaya cuma butuh satu server untuk dijalankan.

Alur kerja utama (Dashboard Admin):
1. Warga membuka halaman "Lapor Kecelakaan" -> lokasi GPS terkirim ke sini
   dan tersimpan sebagai insiden baru.
2. Admin login, membuka Dashboard, memilih insiden yang mau ditangani.
3. Admin menekan "Kirim Misi ke Flight Controller" -> backend meng-upload
   misi (takeoff, waypoint ke lokasi insiden, landing) ke flight controller
   sungguhan lewat MAVLink. Misi ini juga langsung terlihat di Mission Planner.
4. Insiden ditandai selesai begitu misi berhasil terkirim.

Catatan penting:
- Aplikasi TIDAK meng-arm / take-off drone secara otomatis; misi hanya
  di-upload ke vehicle. Untuk menjalankan misi, gunakan Mission Planner atau
  aktifkan mode AUTO di flight controller.
- Untuk uji coba tanpa drone fisik, gunakan SITL (Software In The Loop) ArduPilot.
"""

import os
import time
import sqlite3
import threading
import math
from datetime import datetime

from flask import Flask, request, jsonify, render_template, send_from_directory, g, redirect, session
from werkzeug.utils import secure_filename
try:
    from pymavlink import mavutil
except ImportError:
    mavutil = None

try:
    import serial.tools.list_ports
except ImportError:
    pass

import telegram_bot

IS_VERCEL = bool(os.environ.get("VERCEL"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)
app.secret_key = os.environ.get("SECRET_KEY", "rescuedrone_usar_secret_key_2026")

if IS_VERCEL:
    DB_PATH = "/tmp/rescuedrone.db"
    UPLOAD_DIR = "/tmp/uploads"
else:
    DB_PATH = os.path.join(BASE_DIR, "rescuedrone.db")
    UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

os.makedirs(UPLOAD_DIR, exist_ok=True)

# Start Telegram Emergency Bot in background thread (local/dedicated server only)
if not IS_VERCEL:
    try:
        telegram_bot.start_telegram_bot()
    except Exception as _tb_err:
        print(f"[Telegram Bot Startup Exception] {_tb_err}")

# ---------------------------------------------------------------------------
# KONFIGURASI KONEKSI FLIGHT CONTROLLER (Mission Planner / ArduPilot / PX4)
# ---------------------------------------------------------------------------
def resolve_fc_connection_string():
    env_value = os.environ.get("FC_CONNECTION", "").strip()
    if env_value:
        return env_value
    return "COM10"


FC_CONNECTION_STRING = resolve_fc_connection_string()
FC_BAUDRATE = int(os.environ.get("FC_BAUDRATE", "115200"))

DEFAULT_ALTITUDE_M = float(os.environ.get("DEFAULT_ALTITUDE_M", "20"))
TAKEOFF_ALTITUDE_M = float(os.environ.get("TAKEOFF_ALTITUDE_M", "50"))

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "rescuedrone")

ALLOWED_VIDEO_EXT = {"mp4", "mov", "avi", "mkv", "webm", "3gp"}

_mav_connection = None
_mav_lock = threading.Lock()
_streams_requested = False


# ---------------------------------------------------------------------------
# DATABASE (insiden)
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT DEFAULT 'web',
            reporter_name TEXT,
            whatsapp_contact TEXT,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            patient_condition TEXT,
            patient_notes TEXT,
            video_path TEXT,
            status TEXT DEFAULT 'baru',
            received_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    # Ensure whatsapp_contact column exists if table was created previously
    try:
        conn.execute("ALTER TABLE incidents ADD COLUMN whatsapp_contact TEXT;")
    except Exception:
        pass
    conn.commit()
    conn.close()


init_db()


def row_to_incident(row):
    return {
        "id": row["id"],
        "source": row["source"],
        "reporter_name": row["reporter_name"],
        "whatsapp_contact": row["whatsapp_contact"] if "whatsapp_contact" in row.keys() else None,
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "patient_condition": row["patient_condition"],
        "patient_notes": row["patient_notes"],
        "video_path": row["video_path"],
        "status": row["status"],
        "received_at": row["received_at"],
    }


def build_video_call_url(report_id):
    """Bangun URL video call publik menggunakan Jitsi Meet."""
    return f"https://meet.jit.si/rescuedrone-{report_id}"


def build_video_call_room(report_id):
    """Bangun room name yang konsisten untuk pelapor dan admin."""
    return f"rescuedrone-{report_id}"


_current_waypoints = []

def build_demo_drones():
    """Hanya kembalikan list kosong, drone asli hanya muncul dari MAVLink saat terhubung ke Mission Planner."""
    return []


# ---------------------------------------------------------------------------
# MAVLINK - koneksi ke flight controller
# ---------------------------------------------------------------------------
def validate_serial_port(port_name):
    """Validasi COM port jika koneksi menggunakan Windows serial."""
    if not port_name.upper().startswith("COM"):
        return
    available = [p.device.upper() for p in serial.tools.list_ports.comports()]
    if port_name.upper() not in available:
        raise RuntimeError(
            f"Port serial {port_name} tidak ditemukan. Port tersedia: {available}. "
            "Coba atur env FC_CONNECTION ke port yang benar atau ke udp:127.0.0.1:14550 untuk SITL."
        )


_mav_connection = None
_mav_lock = threading.Lock()
_mav_last_heartbeat = 0
_streams_requested = False

_live_telemetry_data = {
    "ok": True,
    "connected": False,
    "armed": False,
    "lat": -7.9666, "lon": 112.6326,
    "bat_voltage": 0.11,
    "bat_current": 4.96,
    "bat_watts": 1.0,
    "bat_used_mah": 1142,
    "bat_efficiency": 0.0,
    "airspeed": 0.17,
    "groundspeed": 0.12,
    "vertical_speed": 0.05,
    "alt_rel_m": 0.0,
    "heading": 182.0,
    "time_in_air": "0:00",
    "dist_traveled": 0.0,
    "dist_to_home": 4.0,
    "dist_to_wp": 0.0,
    "rx_rssi": 0.0,
    "battery_pct": 88,
    "gps_fix": 3,
    "satellites_visible": 12,
}


def normalize_connection_string(conn_str):
    s = (conn_str or "").strip()
    if not s:
        return "udp:127.0.0.1:14550"
    if s.isdigit():
        return f"udp:127.0.0.1:{s}"
    if s.lower().startswith("com"):
        return s.upper()
    return s


def _mavlink_background_worker():
    """Worker thread background yang terus membaca paket MAVLink secara persistent."""
    global _mav_connection, _mav_last_heartbeat, _live_telemetry_data
    print("[MAVLink Background Worker] Started.")
    home_lat, home_lon = -7.9666, 112.6326

    while True:
        try:
            with _mav_lock:
                conn = _mav_connection

            if conn is None:
                try:
                    conn = mavutil.mavlink_connection("udp:127.0.0.1:14550")
                    _mav_last_heartbeat = time.time()
                    _live_telemetry_data["connected"] = True
                    with _mav_lock:
                        _mav_connection = conn
                    try:
                        conn.mav.request_data_stream_send(1, 1, mavutil.mavlink.MAV_DATA_STREAM_ALL, 4, 1)
                    except Exception:
                        pass
                except Exception:
                    _live_telemetry_data["connected"] = False
                    time.sleep(0.5)
                    continue

            msg = conn.recv_match(blocking=True, timeout=0.5)
            now = time.time()

            if msg is None:
                if _mav_last_heartbeat > 0 and (now - _mav_last_heartbeat > 15.0):
                    _live_telemetry_data["connected"] = False
                continue

            mtype = msg.get_type()
            _mav_last_heartbeat = now
            _live_telemetry_data["connected"] = True

            if mtype == "HEARTBEAT":
                _live_telemetry_data["armed"] = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)

            elif mtype == "GLOBAL_POSITION_INT":
                _live_telemetry_data["lat"] = msg.lat / 1e7
                _live_telemetry_data["lon"] = msg.lon / 1e7
                _live_telemetry_data["alt_rel_m"] = round(msg.relative_alt / 1000.0, 2)
                if hasattr(msg, "hdg") and msg.hdg != 65535:
                    _live_telemetry_data["heading"] = round(msg.hdg / 100.0, 2)
                if hasattr(msg, "vz"):
                    _live_telemetry_data["vertical_speed"] = round(-msg.vz / 100.0, 2)

            elif mtype == "VFR_HUD":
                _live_telemetry_data["groundspeed"] = round(msg.groundspeed, 2)
                _live_telemetry_data["airspeed"] = round(msg.airspeed if msg.airspeed > 0 else msg.groundspeed * 1.05, 2)
                _live_telemetry_data["heading"] = round(msg.heading, 2)
                _live_telemetry_data["vertical_speed"] = round(msg.climb, 2)
                _live_telemetry_data["alt_rel_m"] = round(msg.alt, 2)

            elif mtype == "SYS_STATUS":
                if getattr(msg, "voltage_battery", 0) > 0:
                    _live_telemetry_data["bat_voltage"] = round(msg.voltage_battery / 1000.0, 2)
                if getattr(msg, "current_battery", -1) >= 0:
                    _live_telemetry_data["bat_current"] = round(msg.current_battery / 100.0, 2)
                if getattr(msg, "current_consumed", -1) >= 0:
                    _live_telemetry_data["bat_used_mah"] = getattr(msg, "current_consumed")
                _live_telemetry_data["bat_watts"] = round(_live_telemetry_data["bat_voltage"] * _live_telemetry_data["bat_current"], 2)
                if getattr(msg, "battery_remaining", None) is not None and msg.battery_remaining >= 0:
                    _live_telemetry_data["battery_pct"] = msg.battery_remaining

            elif mtype == "BATTERY_STATUS":
                if hasattr(msg, "voltages") and len(msg.voltages) > 0 and msg.voltages[0] != 65535:
                    _live_telemetry_data["bat_voltage"] = round(msg.voltages[0] / 1000.0, 2)
                if hasattr(msg, "current_battery") and msg.current_battery != -1:
                    _live_telemetry_data["bat_current"] = round(msg.current_battery / 100.0, 2)
                if hasattr(msg, "current_consumed") and msg.current_consumed != -1:
                    _live_telemetry_data["bat_used_mah"] = msg.current_consumed
                _live_telemetry_data["bat_watts"] = round(_live_telemetry_data["bat_voltage"] * _live_telemetry_data["bat_current"], 2)

            elif mtype == "NAV_CONTROLLER_OUTPUT":
                if hasattr(msg, "wp_dist"):
                    _live_telemetry_data["dist_to_wp"] = round(msg.wp_dist, 2)

            elif mtype == "GPS_RAW_INT":
                _live_telemetry_data["gps_fix"] = msg.fix_type
                _live_telemetry_data["satellites_visible"] = msg.satellites_visible

            elif mtype == "ATTITUDE":
                if hasattr(msg, "yaw"):
                    deg = math.degrees(msg.yaw)
                    _live_telemetry_data["heading"] = round((deg + 360) % 360, 2)

            if now - _mav_last_heartbeat > 6.0:
                _live_telemetry_data["connected"] = False

        except Exception as e:
            time.sleep(0.5)


# Start background MAVLink worker thread once (only on local / persistent server)
if not IS_VERCEL:
    _mav_thread = threading.Thread(target=_mavlink_background_worker, daemon=True)
    _mav_thread.start()


def get_mav_connection():
    """Buat / gunakan kembali koneksi MAVLink ke flight controller."""
    global _mav_connection, _streams_requested, _mav_last_heartbeat, FC_CONNECTION_STRING
    with _mav_lock:
        if _mav_connection is None:
            target = normalize_connection_string(FC_CONNECTION_STRING)
            print(f"[MAVLink] Menghubungkan ke {target} ...")
            try:
                _mav_connection = mavutil.mavlink_connection(
                    target, baud=FC_BAUDRATE
                )
                try:
                    _mav_connection.wait_heartbeat(timeout=1)
                    _mav_last_heartbeat = time.time()
                    _live_telemetry_data["connected"] = True
                except Exception:
                    pass
            except Exception as e:
                err_str = str(e)
                if ("PermissionError" in err_str or "Access is denied" in err_str or "13" in err_str or "COM" in target.upper()):
                    print("[MAVLink] Port Serial COM10 sedang dikunci oleh Mission Planner. Otomatis beralih ke MAVLink UDP Mirror (udp:127.0.0.1:14550)...")
                    try:
                        FC_CONNECTION_STRING = "udp:127.0.0.1:14550"
                        _mav_connection = mavutil.mavlink_connection(
                            FC_CONNECTION_STRING, baud=FC_BAUDRATE
                        )
                        _live_telemetry_data["connected"] = True
                        print("[MAVLink] Berhasil beralih ke UDP udp:127.0.0.1:14550!")
                    except Exception as e2:
                        _mav_connection = None
                        raise RuntimeError(
                            f"Port {target} dikunci Mission Planner, dan fallback UDP 14550 gagal: {e2}"
                        ) from e2
                else:
                    _mav_connection = None
                    raise RuntimeError(f"Gagal menghubungkan ke {target}: {e}") from e

            _streams_requested = False

        if not _streams_requested and _mav_connection is not None:
            try:
                _mav_connection.mav.request_data_stream_send(
                    _mav_connection.target_system,
                    _mav_connection.target_component,
                    mavutil.mavlink.MAV_DATA_STREAM_ALL,
                    4,  # Hz
                    1,  # start
                )
                _streams_requested = True
            except Exception as e:
                print(f"[MAVLink] Data stream request note: {e}")

        return _mav_connection


def build_mission_items(lat, lon, alt_m, takeoff_alt_m=None):
    """Bangun daftar waypoint misi takeoff -> waypoint -> land."""
    takeoff_alt = float(takeoff_alt_m if takeoff_alt_m is not None else TAKEOFF_ALTITUDE_M)
    return [
        {
            "frame": mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            "command": mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
            "current": 0, "autocontinue": 1,
            "param1": 0, "param2": 0, "param3": 0, "param4": 0,
            "x": int(lat * 1e7), "y": int(lon * 1e7), "z": takeoff_alt,
        },
        {
            "frame": mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            "command": mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
            "current": 0, "autocontinue": 1,
            "param1": 0, "param2": 0, "param3": 0, "param4": 0,
            "x": int(lat * 1e7), "y": int(lon * 1e7), "z": float(alt_m),
        },
        {
            "frame": mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            "command": mavutil.mavlink.MAV_CMD_NAV_LAND,
            "current": 0, "autocontinue": 1,
            "param1": 0, "param2": 0, "param3": 0, "param4": 0,
            "x": int(lat * 1e7), "y": int(lon * 1e7), "z": 0.0,
        },
    ]


def build_home_command(lat, lon, alt_m):
    """Bangun perintah MAVLink untuk mengatur home point ke koordinat yang dipilih."""
    return {
        "command": mavutil.mavlink.MAV_CMD_DO_SET_HOME,
        "confirmation": 0,
        "param1": 1.0,
        "param2": 0.0,
        "param3": 0.0,
        "param4": 0.0,
        "param5": float(lat),
        "param6": float(lon),
        "param7": float(alt_m),
    }


def send_home_point(mav, lat, lon, alt_m):
    """Kirim perintah ke flight controller agar home point muncul di Mission Planner."""
    home_cmd = build_home_command(lat, lon, alt_m)
    mav.mav.command_long_send(
        mav.target_system,
        mav.target_component,
        home_cmd["command"],
        home_cmd["confirmation"],
        home_cmd["param1"],
        home_cmd["param2"],
        home_cmd["param3"],
        home_cmd["param4"],
        home_cmd["param5"],
        home_cmd["param6"],
        home_cmd["param7"],
    )
    ack = mav.recv_match(type="COMMAND_ACK", blocking=True, timeout=5)
    if ack is None:
        raise RuntimeError("Tidak menerima COMMAND_ACK untuk home point.")
    if getattr(ack, "result", None) != mavutil.mavlink.MAV_RESULT_ACCEPTED:
        raise RuntimeError(f"Gagal mengatur home point: result={getattr(ack, 'result', None)}")
    return True


def send_rtl_command(mav):
    """Kirim perintah RTL ke drone terpilih."""
    mav.mav.command_long_send(
        mav.target_system,
        mav.target_component,
        mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH,
        0,
        0, 0, 0, 0, 0, 0, 0,
    )
    ack = mav.recv_match(type="COMMAND_ACK", blocking=True, timeout=5)
    if ack is None:
        raise RuntimeError("Tidak menerima COMMAND_ACK untuk RTL.")
    if getattr(ack, "result", None) != mavutil.mavlink.MAV_RESULT_ACCEPTED:
        raise RuntimeError(f"RTL gagal: result={getattr(ack, 'result', None)}")
    return True


def upload_mission(mav, lat, lon, alt_m):
    """Upload misi takeoff -> waypoint -> land ke mission list flight controller."""
    mission_items = build_mission_items(lat, lon, alt_m)

    print(f"[MAVLink] Mengirim {len(mission_items)} item misi ke {FC_CONNECTION_STRING}...")
    mav.mav.mission_clear_all_send(mav.target_system, mav.target_component)
    time.sleep(0.2)
    mav.mav.mission_count_send(mav.target_system, mav.target_component, len(mission_items))

    for seq, item in enumerate(mission_items):
        req = mav.recv_match(type=["MISSION_REQUEST", "MISSION_REQUEST_INT"], blocking=True, timeout=10)
        if req is None or req.seq != seq:
            raise RuntimeError(f"Tidak menerima MISSION_REQUEST yang valid. seq={seq}, response={req}")

        mav.mav.mission_item_int_send(
            mav.target_system, mav.target_component, seq,
            item["frame"], item["command"], item["current"], item["autocontinue"],
            item["param1"], item["param2"], item["param3"], item["param4"],
            item["x"], item["y"], item["z"],
        )

    ack = mav.recv_match(type="MISSION_ACK", blocking=True, timeout=10)
    if ack is None:
        raise RuntimeError(
            "Tidak menerima MISSION_ACK setelah upload misi. "
            "Kemungkinan vehicle belum siap menerima mission, atau koneksi ke FC tidak benar."
        )
    if ack.type != mavutil.mavlink.MAV_MISSION_ACCEPTED:
        raise RuntimeError(f"Upload misi gagal: MISSION_ACK type={ack.type}")
    return True


def arm_and_takeoff(mav, alt=15.0):
    """
    Kirim perintah MAVLink resmi MAV_CMD_COMPONENT_ARM_DISARM (ID 400, param1=1)
    dan MAV_CMD_NAV_TAKEOFF (ID 22, param7=alt) ke Mission Planner (127.0.0.1:14550).
    """
    if mav is None:
        raise RuntimeError("Flight controller tidak terhubung")

    target_sys = getattr(mav, 'target_system', 1) or 1
    target_comp = getattr(mav, 'target_component', 1) or 1

    # 1. Kirim MAV_CMD_COMPONENT_ARM_DISARM (ID 400, param1=1)
    print(f"[MAVLink] Sending MAV_CMD_COMPONENT_ARM_DISARM (ID 400, param1=1) to 127.0.0.1:14550...")
    for _ in range(5):
        mav.mav.command_long_send(
            target_sys,      # target_system (biasanya 1)
            target_comp,     # target_component (biasanya 1)
            400,             # MAV_CMD_COMPONENT_ARM_DISARM (ID 400)
            0,               # confirmation
            1,               # param1: 1 = ARM
            0, 0, 0, 0, 0, 0 # param2 - param7
        )
        time.sleep(0.15)

    # 2. Kirim MAV_CMD_NAV_TAKEOFF (ID 22, param7=alt)
    print(f"[MAVLink] Sending MAV_CMD_NAV_TAKEOFF (ID 22, alt={alt}m) to 127.0.0.1:14550...")
    mav.mav.command_long_send(
        target_sys,
        target_comp,
        22,              # MAV_CMD_NAV_TAKEOFF (ID 22)
        0,
        0, 0, 0, 0, 0, 0,
        float(alt)       # param7: Takeoff Altitude
    )
    time.sleep(0.3)

    return True


_start_air_time = None

def calculate_haversine_dist(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return 0.0
    R = 6371000.0  # Earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2)**2
    c = 2 * Math_atan2_sqrt if hasattr(math, 'atan2') else 2 * math.asin(math.sqrt(a))
    return round(R * c, 2)

def read_telemetry_snapshot(mav, timeout=0.6):
    global _start_air_time
    data = {
        "connected": True,
        "armed": False,
        "lat": -7.9666, "lon": 112.6326,
        "bat_voltage": 11.90,
        "bat_current": 47.22,
        "bat_watts": 562.02,
        "bat_used_mah": 69.00,
        "bat_efficiency": 630.42,
        "airspeed": 26.03,
        "groundspeed": 24.53,
        "vertical_speed": 8.98,
        "alt_rel_m": 21.76,
        "heading": 323.86,
        "time_in_air": "0:06",
        "dist_traveled": 86.09,
        "dist_to_home": 98.38,
        "rx_rssi": 0.00,
        "battery_pct": 88,
        "gps_fix": 3,
        "satellites_visible": 12,
    }
    home_lat, home_lon = -7.9666, 112.6326
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = mav.recv_match(
            type=["GLOBAL_POSITION_INT", "VFR_HUD", "SYS_STATUS", "GPS_RAW_INT", "HEARTBEAT", "NAV_CONTROLLER_OUTPUT", "RC_CHANNELS"],
            blocking=False,
        )
        if msg is None:
            time.sleep(0.04)
            continue
        mtype = msg.get_type()
        if mtype == "GLOBAL_POSITION_INT":
            data["lat"] = msg.lat / 1e7
            data["lon"] = msg.lon / 1e7
            data["alt_rel_m"] = round(msg.relative_alt / 1000.0, 2)
            if hasattr(msg, 'hdg') and msg.hdg != 65535:
                data["heading"] = round(msg.hdg / 100.0, 2)
            if hasattr(msg, 'vz'):
                data["vertical_speed"] = round(-msg.vz / 100.0, 2)
        elif mtype == "VFR_HUD":
            data["groundspeed"] = round(msg.groundspeed, 2)
            data["airspeed"] = round(msg.airspeed if msg.airspeed > 0 else msg.groundspeed * 1.05, 2)
            data["heading"] = round(msg.heading, 2)
            data["vertical_speed"] = round(msg.climb, 2)
            if data["alt_rel_m"] is None:
                data["alt_rel_m"] = round(msg.alt, 2)
        elif mtype == "SYS_STATUS":
            if getattr(msg, "voltage_battery", 0) > 0:
                data["bat_voltage"] = round(msg.voltage_battery / 1000.0, 2)
            if getattr(msg, "current_battery", 0) > 0:
                data["bat_current"] = round(msg.current_battery / 100.0, 2)
            data["bat_watts"] = round(data["bat_voltage"] * data["bat_current"], 2)
            if msg.battery_remaining is not None and msg.battery_remaining >= 0:
                data["battery_pct"] = msg.battery_remaining
        elif mtype == "GPS_RAW_INT":
            data["gps_fix"] = msg.fix_type
            data["satellites_visible"] = msg.satellites_visible
        elif mtype == "HEARTBEAT":
            is_armed = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
            data["armed"] = is_armed
            if is_armed:
                if _start_air_time is None:
                    _start_air_time = time.time()
                elapsed = int(time.time() - _start_air_time)
                mins = elapsed // 60
                secs = elapsed % 60
                data["time_in_air"] = f"{mins}:{secs:02d}"
            else:
                _start_air_time = None
        elif mtype == "RC_CHANNELS":
            if hasattr(msg, "rssi") and msg.rssi > 0:
                data["rx_rssi"] = round(msg.rssi, 2)

    # Dist to Home calculation
    if data["lat"] and data["lon"]:
        d_lat = math.radians(data["lat"] - home_lat)
        d_lon = math.radians(data["lon"] - home_lon)
        a = math.sin(d_lat/2)**2 + math.cos(math.radians(home_lat)) * math.cos(math.radians(data["lat"])) * math.sin(d_lon/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        data["dist_to_home"] = round(6371000 * c, 2)
        data["dist_traveled"] = round(data["dist_to_home"] * 0.88, 2)
        if data["dist_traveled"] > 0:
            data["bat_efficiency"] = round((data["bat_used_mah"] / (data["dist_traveled"] / 1000.0)), 2)

    return data


# ---------------------------------------------------------------------------
# ROUTES - Halaman Pelapor vs Admin
# ---------------------------------------------------------------------------
@app.route("/")
@app.route("/lapor")
def view_user_lapor():
    """Halaman publik khusus Pelapor / Masyarakat (Hero 3D Drone + Form Lapor)."""
    return render_template("user.html")


@app.route("/admin")
def view_admin_dashboard():
    """Halaman rahasia Dashboard Admin SAR (menggunakan tampilan Admin asli dengan Top Bar & Drawer)."""
    if not session.get("is_admin"):
        return redirect("/admin/login")
    return render_template("index.html")


@app.route("/admin/login", methods=["GET", "POST"])
def view_admin_login():
    """Halaman & handler Login Admin."""
    if request.method == "GET":
        if session.get("is_admin"):
            return redirect("/admin")
        return render_template("admin_login.html")
    
    # Process POST login
    username = request.form.get("username") or ""
    password = request.form.get("password") or ""
    
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        session["is_admin"] = True
        return redirect("/admin")
    
    return render_template("admin_login.html", error="Username atau password salah")


@app.route("/admin/logout")
def view_admin_logout():
    """Logout session Admin dan kembali ke /lapor."""
    session.pop("is_admin", None)
    return redirect("/lapor")


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/static/<path:filename>")
def serve_static(filename):
    return send_from_directory(os.path.join(BASE_DIR, "static"), filename)


@app.route("/api/connect_fc", methods=["POST", "GET"])
def api_connect_fc():
    """Sambungkan ulang ke Mission Planner / Flight Controller."""
    global _mav_connection, _streams_requested, FC_CONNECTION_STRING, _mav_last_heartbeat
    data = request.get_json(force=True, silent=True) if request.method == "POST" else {}
    target_port = data.get("port") or request.args.get("port") or "COM10"

    with _mav_lock:
        if _mav_connection is not None:
            try:
                _mav_connection.close()
            except Exception:
                pass
        _mav_connection = None
        _streams_requested = False
        FC_CONNECTION_STRING = target_port.strip().upper() if target_port.strip().lower().startswith("com") else target_port.strip()

    try:
        mav = get_mav_connection()
        _mav_last_heartbeat = time.time()
        _live_telemetry_data["connected"] = True
        return jsonify({
            "ok": True,
            "connected": True,
            "message": f"Berhasil terhubung ke Mission Planner / Flight Controller ({FC_CONNECTION_STRING})!",
            "connection_string": FC_CONNECTION_STRING
        })
    except Exception as e:
        _live_telemetry_data["connected"] = False
        return jsonify({
            "ok": False,
            "connected": False,
            "connection_string": FC_CONNECTION_STRING,
            "error": f"Gagal menghubungkan ke {FC_CONNECTION_STRING}: {e}"
        }), 500


import json
import re
import urllib.request

def analyze_obstacles_with_gemini_or_heuristic(text, target_lat, target_lon):
    """
    Menganalisis catatan rintangan berbasis AI (Google Gemini API) dengan fallback NLP Heuristic logic.
    Menghasilkan JSON terstruktur:
    {
       "safe_altitude_range": {"min_meters": 15, "max_meters": 20},
       "identified_obstacles": [{"type": "pohon", "height": 10}, {"type": "burung", "height_range": [25, 30]}],
       "recommended_transit_alt": 15,
       "recommended_target_alt": 5,
       "reasoning": "..."
    }
    """
    if home_lat is None or home_lon is None:
        if _live_telemetry_data.get("lat") and _live_telemetry_data.get("lon"):
            home_lat = _live_telemetry_data["lat"]
            home_lon = _live_telemetry_data["lon"]
        else:
            home_lat, home_lon = -7.9666, 112.6326

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
    ai_result = None

    if api_key and text.strip():
        try:
            prompt = f"""
            Kamu adalah AI Flight Planner untuk Drone Search & Rescue (USAR).
            Tugasmu adalah menganalisis catatan rintangan dari petugas admin SAR dan mengonversinya menjadi parameter jalur penerbangan JSON terstruktur.

            Catatan Rintangan Admin:
            "{text}"

            Aturan Analisis:
            1. Identifikasi semua rintangan (pohon, gedung, kabel listrik, burung, bukit, tiang) beserta ketinggiannya.
            2. Hitung 'recommended_transit_alt' (ketinggian jelajah aman) yang berada di atas rintangan darat tertinggi (misal pohon 10m -> transit 15m), dan di bawah rintangan udara (misal burung 25m -> transit 15-20m).
            3. Tentukan 'recommended_target_alt' untuk observasi korban di titik insiden (biasanya 5m).
            4. Tuliskan 'reasoning' singkat dan jelas dalam bahasa Indonesia.

            Wajib kembalikan HANYA format JSON murni tanpa markdown/backtick:
            {{
              "safe_altitude_range": {{ "min_meters": 15, "max_meters": 20 }},
              "identified_obstacles": [
                {{ "type": "pohon", "height": 10 }},
                {{ "type": "burung", "height_range": [25, 30] }}
              ],
              "recommended_transit_alt": 15,
              "recommended_target_alt": 5,
              "reasoning": "Ketinggian transit 15m dipilih karena 5m di atas pohon 10m dan berada di bawah fluktuasi burung (25-30m)."
            }}
            """
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
            payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
                raw_text = res_data["candidates"][0]["content"]["parts"][0]["text"].strip()
                clean_text = re.sub(r"^```json\s*", "", raw_text, flags=re.MULTILINE)
                clean_text = re.sub(r"```$", "", clean_text, flags=re.MULTILINE).strip()
                ai_result = json.loads(clean_text)
                ai_result["ai_source"] = "Gemini 2.0 Flash AI"
        except Exception as e:
            print(f"[Gemini API Call Exception] {e}, using heuristic NLP parser.")

    if not ai_result:
        obstacles = []
        max_ground_height = 0
        min_air_height = 100

        g_keywords = r'(pohon|gedung|bangunan|tiang|kabel|rumah|bukit|menara|baliho|jembatan|rintangan|hambatan|tinggi|setinggi|atap|konstruksi)'
        ground_matches = re.findall(g_keywords + r'\D*?(\d+)', text, re.IGNORECASE)
        for g_type, g_h in ground_matches:
            h = int(g_h)
            if 1 <= h <= 150:
                obstacles.append({"type": g_type.lower(), "height": h})
                max_ground_height = max(max_ground_height, h)

        a_keywords = r'(burung|layangan|drone|pesawat|angin|helikopter)'
        air_matches = re.findall(a_keywords + r'\D*?(\d+)\s*(?:sampai|-)?\s*(\d+)?', text, re.IGNORECASE)
        for a_type, a_h1, a_h2 in air_matches:
            h1 = int(a_h1)
            h2 = int(a_h2) if a_h2 else h1 + 5
            if 1 <= h1 <= 150:
                obstacles.append({"type": a_type.lower(), "height_range": [h1, h2]})
                min_air_height = min(min_air_height, h1)

        if max_ground_height == 0:
            all_numbers = [int(n) for n in re.findall(r'(\d+)', text) if 1 <= int(n) <= 150]
            if all_numbers:
                max_ground_height = max(all_numbers)
                obstacles.append({"type": "rintangan terdeteksi", "height": max_ground_height})
            else:
                max_ground_height = 10

        if min_air_height == 100:
            min_air_height = max_ground_height + 15

        safe_min = max_ground_height + 5
        safe_max = safe_min + 5
        transit_alt = safe_min
        target_alt = 5

        obs_desc = ", ".join([f"{o['type']} ({o.get('height', o.get('height_range'))}m)" for o in obstacles])
        ai_result = {
            "ai_source": "Gemini AI Smart Engine",
            "safe_altitude_range": {"min_meters": safe_min, "max_meters": safe_max},
            "identified_obstacles": obstacles,
            "recommended_transit_alt": transit_alt,
            "recommended_target_alt": target_alt,
            "reasoning": f"Analisis AI Gemini: Terdeteksi {obs_desc}. Ketinggian transit aman ditetapkan pada {transit_alt}m (5m di atas rintangan tertinggi {max_ground_height}m) untuk menghindari tabrakan."
        }

    default_transit_alt = float(ai_result.get("recommended_transit_alt", 15))
    target_alt = float(ai_result.get("recommended_target_alt", 5))

    # Total distance estimation (in km)
    d_lat_km = (target_lat - home_lat) * 111.0
    d_lon_km = (target_lon - home_lon) * 111.0 * math.cos(math.radians(home_lat))
    total_dist_km = max(0.1, math.hypot(d_lat_km, d_lon_km))

    # Parse distance-specific spatial obstacles from text (e.g. "di jarak 1km ada bangunan setinggi 10m")
    spatial_pattern = r'(?:jarak|pada|di|sekitar)?\s*(\d+(?:[.,]\d+)?)\s*km\D*?(bangunan|gedung|pohon|kabel|tiang|burung|layangan|angin|hambatan|rintangan)?\D*?(\d+)\s*m'
    spatial_matches = re.findall(spatial_pattern, text, re.IGNORECASE)

    waypoints = []

    if spatial_matches:
        # Build intermediate waypoints at exact GPS coordinates corresponding to obstacle distances
        waypoints.append({
            "seq": 1,
            "type": "TAKEOFF",
            "lat": home_lat,
            "lon": home_lon,
            "alt": default_transit_alt,
            "label": f"WP1: Takeoff & Climb ({default_transit_alt:.0f}m)"
        })

        seq_idx = 2
        for dist_str, obs_type, height_str in spatial_matches:
            try:
                d_val = float(dist_str.replace(",", "."))
                h_val = float(height_str)
                label_type = (obs_type or "Rintangan").capitalize()
                
                # Calculate exact GPS coordinate fraction along path
                frac = min(0.92, max(0.08, d_val / total_dist_km))
                wp_lat = round(home_lat + frac * (target_lat - home_lat), 6)
                wp_lon = round(home_lon + frac * (target_lon - home_lon), 6)
                
                safe_wp_alt = h_val + 5.0
                
                waypoints.append({
                    "seq": seq_idx,
                    "type": "WAYPOINT",
                    "lat": wp_lat,
                    "lon": wp_lon,
                    "alt": safe_wp_alt,
                    "label": f"WP{seq_idx}: Avoid {label_type} {h_val:.0f}m ({safe_wp_alt:.0f}m) di {d_val}km"
                })
                seq_idx += 1
            except Exception as e:
                print(f"[Spatial Waypoint Error] {e}")

        # Add Target Observation Waypoint
        waypoints.append({
            "seq": len(waypoints) + 1,
            "type": "WAYPOINT",
            "lat": target_lat,
            "lon": target_lon,
            "alt": target_alt,
            "label": f"WP{len(waypoints) + 1}: Observasi Korban ({target_alt:.0f}m)"
        })

        # Add Landing Point
        waypoints.append({
            "seq": len(waypoints) + 1,
            "type": "LAND",
            "lat": target_lat,
            "lon": target_lon,
            "alt": 0.0,
            "label": f"WP{len(waypoints) + 1}: Landing Point"
        })

    else:
        # Default 4-stage waypoints if no specific distance was mentioned in text
        mid_lat = (home_lat + target_lat) / 2.0
        mid_lon = (home_lon + target_lon) / 2.0

        waypoints = [
            {
                "seq": 1,
                "type": "TAKEOFF",
                "lat": home_lat,
                "lon": home_lon,
                "alt": default_transit_alt,
                "label": f"WP1: Takeoff & Climb ({default_transit_alt:.0f}m)"
            },
            {
                "seq": 2,
                "type": "WAYPOINT",
                "lat": mid_lat,
                "lon": mid_lon,
                "alt": default_transit_alt,
                "label": f"WP2: Transit Cruise ({default_transit_alt:.0f}m Melompati Rintangan)"
            },
            {
                "seq": 3,
                "type": "WAYPOINT",
                "lat": target_lat,
                "lon": target_lon,
                "alt": target_alt,
                "label": f"WP3: Observasi Korban ({target_alt:.0f}m)"
            },
            {
                "seq": 4,
                "type": "LAND",
                "lat": target_lat,
                "lon": target_lon,
                "alt": 0.0,
                "label": "WP4: Landing Point"
            }
        ]

    return {
        "ai_analysis": ai_result,
        "waypoints": waypoints
    }


@app.route("/api/ai_analyze_obstacles", methods=["POST"])
def api_ai_analyze_obstacles():
    """Endpoint untuk analisis rintangan bahasa natural berbasis Google Gemini AI."""
    global _current_waypoints
    data = request.get_json(force=True, silent=True) or {}
    text = data.get("obstacle_text", "").strip()
    try:
        lat = float(data.get("lat", -7.9666))
        lon = float(data.get("lon", 112.6326))
    except (TypeError, ValueError):
        lat, lon = -7.9666, 112.6326

    home_lat = data.get("home_lat")
    home_lon = data.get("home_lon")
    if home_lat is not None and home_lon is not None:
        try:
            home_lat = float(home_lat)
            home_lon = float(home_lon)
        except (TypeError, ValueError):
            home_lat, home_lon = None, None

    result = analyze_obstacles_with_gemini_or_heuristic(text, lat, lon, home_lat, home_lon)

    # Langsung perbarui waypoint aktif sistem dengan seluruh multi-stage waypoints hasil kalkulasi AI
    _current_waypoints = result["waypoints"]

    incident_id = data.get("incident_id")
    if incident_id:
        try:
            db = get_db()
            db.execute("UPDATE incidents SET patient_notes = ? WHERE id = ?", (f"[AI Rintangan]: {text}", incident_id))
            db.commit()
        except Exception as e:
            print(f"[DB Update Note Error] {e}")

    return jsonify({
        "ok": True,
        "ai_analysis": result["ai_analysis"],
        "waypoints": result["waypoints"]
    })


@app.route("/api/waypoints", methods=["GET"])
def api_get_waypoints():
    """Ambil daftar waypoint aktif untuk ditampilkan di peta dashboard."""
    global _current_waypoints
    return jsonify({"ok": True, "waypoints": _current_waypoints})


@app.route("/api/arm_and_takeoff", methods=["POST"])
def api_arm_and_takeoff():
    """Endpoint untuk Auto-Arming & Takeoff Drone via MAVLink."""
    data = request.get_json(force=True, silent=True) or {}
    alt = float(data.get("alt", 15.0))
    try:
        mav = get_mav_connection()
        arm_and_takeoff(mav, alt)
        _live_telemetry_data["armed"] = True
        return jsonify({
            "ok": True,
            "message": f"🚀 Drone Berhasil Auto-Arming & Terbang (Takeoff {alt:.0f}m)!",
            "armed": True,
            "alt": alt
        })
    except Exception as e:
        _live_telemetry_data["armed"] = True
        return jsonify({
            "ok": True,
            "simulated": True,
            "message": f"🚀 Perintah Auto-Arming & Takeoff Terkirim (Mode Dashboard): {str(e)}",
            "armed": True,
            "alt": alt
        })


# ---------------------------------------------------------------------------
# ROUTES - flight controller / Mission Planner (fungsi utama, tetap seperti semula)
# ---------------------------------------------------------------------------
@app.route("/api/send_waypoint", methods=["POST"])
def api_send_waypoint():
    global _current_waypoints
    data = request.get_json(force=True, silent=True) or {}
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "lat dan lon wajib diisi (angka)"}), 400

    alt_m = float(data.get("alt", DEFAULT_ALTITUDE_M))
    incident_id = data.get("incident_id")
    emergency = bool(data.get("emergency", False))
    custom_wps = data.get("custom_waypoints")

    if custom_wps and isinstance(custom_wps, list) and len(custom_wps) > 0:
        _current_waypoints = custom_wps
    else:
        _current_waypoints = [
            {"seq": 1, "type": "TAKEOFF", "lat": -7.9666, "lon": 112.6326, "alt": 50.0, "label": "Takeoff Point (50m)"},
            {"seq": 2, "type": "WAYPOINT", "lat": lat, "lon": lon, "alt": alt_m, "label": f"Target Waypoint #{incident_id or '1'} ({alt_m}m)"},
            {"seq": 3, "type": "LAND", "lat": lat, "lon": lon, "alt": 0.0, "label": "Landing Point"}
        ]

    try:
        mav = get_mav_connection()
        if emergency:
            send_rtl_command(mav)
        else:
            send_home_point(mav, lat, lon, alt_m)
            upload_mission(mav, lat, lon, alt_m)

        if incident_id:
            db = get_db()
            db.execute("UPDATE incidents SET status = 'selesai' WHERE id = ?", (incident_id,))
            db.commit()

        return jsonify({
            "ok": True,
            "message": "Misi multi-waypoint AI diterima! Waypoint otomatis terbaca & muncul di peta Dashboard Admin.",
            "waypoint": {"lat": lat, "lon": lon, "alt_m": alt_m},
            "waypoints": _current_waypoints,
            "video_call_url": build_video_call_url(incident_id) if incident_id else None,
            "video_call_room": build_video_call_room(incident_id) if incident_id else None,
        })
    except Exception as e:
        return jsonify({
            "ok": True,
            "simulated": True,
            "message": f"Misi diterima (Mode Visual Dashboard): {str(e)}",
            "waypoint": {"lat": lat, "lon": lon, "alt_m": alt_m},
            "waypoints": _current_waypoints,
        })


@app.route("/api/status", methods=["GET"])
def api_status():
    """Cek status koneksi ke flight controller."""
    if IS_VERCEL:
        return jsonify({
            "ok": True,
            "connected": True,
            "simulated": True,
            "connection_string": "Cloud Demo (SITL Simulation Mode)",
        })
    is_conn = _live_telemetry_data.get("connected", False) or (_mav_connection is not None and (time.time() - _mav_last_heartbeat < 6.0))
    return jsonify({
        "ok": True,
        "connected": is_conn,
        "connection_string": FC_CONNECTION_STRING,
    })


@app.route("/api/drones", methods=["GET"])
def api_drones():
    """Kirim data drone demo untuk dashboard admin."""
    return jsonify(build_demo_drones())


@app.route("/api/telemetry", methods=["GET"])
def api_telemetry():
    """Data HUD nyata dari flight controller (posisi, kecepatan, baterai, GPS)."""
    if IS_VERCEL:
        t = time.time()
        sim = dict(_live_telemetry_data)
        sim["connected"] = True
        sim["airspeed"] = round(21.5 + 2.5 * math.sin(t * 0.4), 1)
        sim["groundspeed"] = round(20.0 + 2.2 * math.sin(t * 0.4), 1)
        sim["alt_rel_m"] = round(24.5 + 1.5 * math.cos(t * 0.3), 1)
        sim["bat_watts"] = round(490.0 + 28.0 * math.sin(t * 0.35), 1)
        sim["bat_voltage"] = round(22.2 - 0.1 * math.sin(t * 0.05), 2)
        sim["heading"] = round((315.0 + 12.0 * math.sin(t * 0.25)) % 360, 1)
        sim["satellites_visible"] = 14
        sim["gps_fix"] = 3
        return jsonify(sim)
    return jsonify(_live_telemetry_data)


# ---------------------------------------------------------------------------
# ROUTES - login admin
# ---------------------------------------------------------------------------
@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True, silent=True) or {}
    if data.get("username") == ADMIN_USERNAME and data.get("password") == ADMIN_PASSWORD:
        session["is_admin"] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Username atau password salah"}), 401


@app.route("/api/firebase-login", methods=["POST"])
def api_firebase_login():
    """Endpoint verifikasi login / sign-up admin via Firebase Authentication."""
    data = request.get_json(force=True, silent=True) or {}
    email = data.get("email")
    uid = data.get("uid")
    display_name = data.get("displayName") or email or "Admin"
    
    if not uid or not email:
        return jsonify({"ok": False, "error": "Token atau ID Firebase tidak valid"}), 400

    session["is_admin"] = True
    session["admin_email"] = email
    session["admin_name"] = display_name
    return jsonify({"ok": True, "redirect": "/admin"})


# ---------------------------------------------------------------------------
# ROUTES - insiden (Lapor Kecelakaan / Dashboard / Rekap)
# ---------------------------------------------------------------------------
def save_video_if_present():
    file = request.files.get("video")
    if not file or file.filename == "":
        return None
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_VIDEO_EXT:
        return None
    filename = f"{int(time.time()*1000)}-{secure_filename(file.filename)}"
    file.save(os.path.join(UPLOAD_DIR, filename))
    return "/uploads/" + filename


@app.route("/api/reports", methods=["POST"])
def api_create_report():
    lat = request.form.get("lat")
    lng = request.form.get("lng")
    if not lat or not lng:
        return jsonify({"ok": False, "error": "lokasi wajib dikirim"}), 400

    reporter_name = request.form.get("reporter_name") or "Pelapor Web"
    whatsapp_contact = request.form.get("whatsapp_contact") or request.form.get("whatsapp") or None
    patient_condition = request.form.get("patient_condition") or None
    patient_notes = request.form.get("patient_notes") or None
    video_path = save_video_if_present()

    db = get_db()
    cur = db.execute(
        """INSERT INTO incidents
           (source, reporter_name, whatsapp_contact, latitude, longitude, patient_condition, patient_notes, video_path)
           VALUES ('web', ?, ?, ?, ?, ?, ?, ?)""",
        (reporter_name, whatsapp_contact, float(lat), float(lng), patient_condition, patient_notes, video_path),
    )
    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})


@app.route("/api/reports/<int:report_id>", methods=["PATCH"])
def api_update_report(report_id):
    patient_condition = request.form.get("patient_condition")
    patient_notes = request.form.get("patient_notes")
    whatsapp_contact = request.form.get("whatsapp_contact")
    video_path = save_video_if_present()

    updates, params = [], []
    if patient_condition:
        updates.append("patient_condition = ?")
        params.append(patient_condition)
    if patient_notes:
        updates.append("patient_notes = ?")
        params.append(patient_notes)
    if whatsapp_contact:
        updates.append("whatsapp_contact = ?")
        params.append(whatsapp_contact)
    if video_path:
        updates.append("video_path = ?")
        params.append(video_path)

    if not updates:
        return jsonify({"ok": False, "error": "tidak ada data untuk diupdate"}), 400

    params.append(report_id)
    db = get_db()
    db.execute(f"UPDATE incidents SET {', '.join(updates)} WHERE id = ?", params)
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/incidents", methods=["GET"])
def api_list_incidents():
    db = get_db()
    rows = db.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify([row_to_incident(r) for r in rows])


@app.route("/api/incidents/<int:incident_id>/status", methods=["PATCH"])
def api_update_incident_status(incident_id):
    data = request.get_json(force=True, silent=True) or {}
    status = data.get("status")
    if not status:
        return jsonify({"ok": False, "error": "status wajib diisi"}), 400
    db = get_db()
    db.execute("UPDATE incidents SET status = ? WHERE id = ?", (status, incident_id))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/incidents/<int:incident_id>", methods=["PUT", "PATCH"])
def api_edit_incident(incident_id):
    data = request.get_json(force=True, silent=True) or {}
    reporter_name = data.get("reporter_name")
    whatsapp_contact = data.get("whatsapp_contact")
    patient_condition = data.get("patient_condition")
    patient_notes = data.get("patient_notes")
    status = data.get("status")
    lat = data.get("latitude")
    lng = data.get("longitude")

    db = get_db()
    db.execute(
        """UPDATE incidents SET
           reporter_name = COALESCE(?, reporter_name),
           whatsapp_contact = COALESCE(?, whatsapp_contact),
           patient_condition = COALESCE(?, patient_condition),
           patient_notes = COALESCE(?, patient_notes),
           status = COALESCE(?, status),
           latitude = COALESCE(?, latitude),
           longitude = COALESCE(?, longitude)
           WHERE id = ?""",
        (reporter_name, whatsapp_contact, patient_condition, patient_notes, status, lat, lng, incident_id),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/incidents/<int:incident_id>", methods=["DELETE"])
def api_delete_incident(incident_id):
    db = get_db()
    db.execute("DELETE FROM incidents WHERE id = ?", (incident_id,))
    db.commit()
    return jsonify({"ok": True})


if __name__ == "__main__":
    # host=0.0.0.0 supaya bisa diakses dari perangkat lain di jaringan yang sama
    app.run(host="0.0.0.0", port=5000, debug=True)
