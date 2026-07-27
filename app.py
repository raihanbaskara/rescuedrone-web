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
from datetime import datetime

from flask import Flask, request, jsonify, render_template, send_from_directory, g, redirect, session
from werkzeug.utils import secure_filename
from pymavlink import mavutil
import serial.tools.list_ports

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "rescuedrone_usar_secret_key_2026")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "rescuedrone.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# KONFIGURASI KONEKSI FLIGHT CONTROLLER (Mission Planner / ArduPilot / PX4)
# ---------------------------------------------------------------------------
def resolve_fc_connection_string():
    env_value = os.environ.get("FC_CONNECTION", "").strip()
    if env_value:
        return env_value

    if os.name == "nt":
        available = [p.device.upper() for p in serial.tools.list_ports.comports()]
        if available:
            preferred = next((port for port in available if port.startswith("COM")), available[0])
            return preferred
        return "udp:127.0.0.1:14550"
    return "udp:127.0.0.1:14550"


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


def build_demo_drones():
    """Bangun daftar drone demo untuk dashboard admin."""
    db = get_db()
    rows = db.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT 8").fetchall()
    incidents = [row_to_incident(r) for r in rows]

    if not incidents:
        return [
            {
                "id": "drone-01",
                "name": "Drone 01",
                "lat": -7.9666,
                "lon": 112.6326,
                "battery_pct": 88,
                "status": "active",
                "waypoint_lat": -7.9666,
                "waypoint_lon": 112.6326,
                "waypoint_label": "Home / menunggu panggilan",
            }
        ]

    drones = []
    spread_points = [
        (-0.0015, -0.0022),
        (0.0018, -0.0007),
        (-0.0004, 0.0025),
    ]
    for idx, incident in enumerate(incidents[:3]):
        offset_lat, offset_lon = spread_points[idx % len(spread_points)]
        status = "active" if idx == 0 else "standby"
        if idx % 2 == 0 and idx != 0:
            status = "charging"
        drones.append(
            {
                "id": f"drone-{idx + 1:02d}",
                "name": f"Drone {idx + 1:02d}",
                "lat": float(incident["latitude"]) + offset_lat,
                "lon": float(incident["longitude"]) + offset_lon,
                "battery_pct": max(20, 100 - (idx * 8)),
                "status": status,
                "waypoint_lat": float(incident["latitude"]),
                "waypoint_lon": float(incident["longitude"]),
                "waypoint_label": f"Waypoint #{incident['id']}",
            }
        )
    return drones


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


def get_mav_connection():
    """Buat / gunakan kembali koneksi MAVLink ke flight controller."""
    global _mav_connection, _streams_requested
    with _mav_lock:
        if _mav_connection is None:
            validate_serial_port(FC_CONNECTION_STRING)
            print(f"[MAVLink] Menghubungkan ke {FC_CONNECTION_STRING} ...")
            try:
                _mav_connection = mavutil.mavlink_connection(
                    FC_CONNECTION_STRING, baud=FC_BAUDRATE
                )
                _mav_connection.wait_heartbeat(timeout=10)
            except Exception as e:
                _mav_connection = None
                raise RuntimeError(
                    f"Gagal menghubungkan ke {FC_CONNECTION_STRING}: {e}. "
                    "Pastikan flight controller atau SITL sudah aktif, atau sesuaikan setting FC_CONNECTION."
                ) from e
            print(
                f"[MAVLink] Terhubung! system={_mav_connection.target_system} "
                f"component={_mav_connection.target_component}"
            )
            _streams_requested = False

        if not _streams_requested:
            # Minta flight controller mengirim data telemetry (posisi, HUD,
            # status baterai) secara berkala supaya dashboard bisa menampilkan
            # data nyata, bukan simulasi.
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
                print(f"[MAVLink] Gagal minta data stream: {e}")

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


def read_telemetry_snapshot(mav, timeout=0.6):
    """
    Ambil data telemetry terbaru yang sudah dikirim flight controller
    (non-blocking / best-effort). Tidak mengganggu upload misi karena
    memakai lock yang sama dan durasi baca dibatasi singkat.
    """
    data = {
        "connected": True,
        "armed": None,
        "lat": None, "lon": None,
        "alt_rel_m": None,
        "groundspeed_ms": None,
        "battery_pct": None,
        "gps_fix": None,
        "satellites_visible": None,
    }
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = mav.recv_match(
            type=["GLOBAL_POSITION_INT", "VFR_HUD", "SYS_STATUS", "GPS_RAW_INT", "HEARTBEAT"],
            blocking=False,
        )
        if msg is None:
            time.sleep(0.05)
            continue
        mtype = msg.get_type()
        if mtype == "GLOBAL_POSITION_INT":
            data["lat"] = msg.lat / 1e7
            data["lon"] = msg.lon / 1e7
            data["alt_rel_m"] = round(msg.relative_alt / 1000.0, 1)
        elif mtype == "VFR_HUD":
            data["groundspeed_ms"] = round(msg.groundspeed, 1)
            if data["alt_rel_m"] is None:
                data["alt_rel_m"] = round(msg.alt, 1)
        elif mtype == "SYS_STATUS":
            if msg.battery_remaining is not None and msg.battery_remaining >= 0:
                data["battery_pct"] = msg.battery_remaining
        elif mtype == "GPS_RAW_INT":
            data["gps_fix"] = msg.fix_type
            data["satellites_visible"] = msg.satellites_visible
        elif mtype == "HEARTBEAT":
            data["armed"] = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
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


# ---------------------------------------------------------------------------
# ROUTES - flight controller / Mission Planner (fungsi utama, tetap seperti semula)
# ---------------------------------------------------------------------------
@app.route("/api/send_waypoint", methods=["POST"])
def api_send_waypoint():
    data = request.get_json(force=True, silent=True) or {}
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "lat dan lon wajib diisi (angka)"}), 400

    alt_m = float(data.get("alt", DEFAULT_ALTITUDE_M))
    incident_id = data.get("incident_id")
    emergency = bool(data.get("emergency", False))

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
            "message": "Home point dan misi takeoff/waypoint/landing berhasil di-upload ke flight controller. "
                       "Cek Mission Planner untuk melihat home point dan waypoint.",
            "waypoint": {"lat": lat, "lon": lon, "alt_m": alt_m},
            "video_call_url": build_video_call_url(incident_id) if incident_id else None,
            "video_call_room": build_video_call_room(incident_id) if incident_id else None,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/status", methods=["GET"])
def api_status():
    """Cek status koneksi ke flight controller."""
    try:
        mav = get_mav_connection()
        return jsonify({
            "ok": True,
            "connected": True,
            "target_system": mav.target_system,
            "target_component": mav.target_component,
            "connection_string": FC_CONNECTION_STRING,
        })
    except Exception as e:
        return jsonify({"ok": False, "connected": False, "error": str(e)}), 500


@app.route("/api/drones", methods=["GET"])
def api_drones():
    """Kirim data drone demo untuk dashboard admin."""
    return jsonify(build_demo_drones())


@app.route("/api/telemetry", methods=["GET"])
def api_telemetry():
    """Data HUD nyata dari flight controller (posisi, kecepatan, baterai, GPS)."""
    try:
        mav = get_mav_connection()
        with _mav_lock:
            snap = read_telemetry_snapshot(mav)
        return jsonify(snap)
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)}), 200


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
