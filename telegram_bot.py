"""
RescueDrone - Telegram Emergency Bot Listener
==============================================
Bot ini mendengarkan laporan lokasi darurat dari warga via Telegram.
Saat pengguna mengirim /start atau /help, bot merespons dengan tombol khusus
[ 📍 Kirim Lokasi Darurat (GPS) ] (request_location=True).

Begitu koordinat lokasi GPS diterima:
1. Data disimpan ke database SQLite (incidents table).
2. Bot memberikan konfirmasi pesan sukses ke pengguna.
3. Dashboard Admin akan otomatis mendeteksi dan menampilkan insiden baru di peta.
"""

import os
import time
import json
import sqlite3
import threading
import urllib.request
import urllib.parse

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/" if BOT_TOKEN else ""

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if os.environ.get("VERCEL"):
    DB_PATH = "/tmp/rescuedrone.db"
else:
    DB_PATH = os.path.join(BASE_DIR, "rescuedrone.db")


def send_telegram_request(method, payload=None):
    url = TELEGRAM_API_URL + method
    try:
        if payload:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        else:
            req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        # print(f"[Telegram Bot Error] {method}: {e}")
        return None


from datetime import datetime

# Temporary memory to store shared phone numbers and last reported incident per chat_id
USER_CONTACTS = {}
LAST_USER_INCIDENT = {}


def send_welcome_keyboard(chat_id, user_name):
    text = (
        f"🚨 *RescueDrone - Sistem Tanggap Darurat USAR*\n\n"
        f"Halo *{user_name}*, Anda berada di saluran darurat medis & kecelakaan RescueDrone.\n\n"
        f"Silakan tekan tombol di bawah ini:\n"
        f"1. *📍 Kirim Lokasi Darurat (GPS)* untuk memanggil Drone Rescue.\n"
        f"2. *📱 Bagikan No. Telp / WA* untuk mencantumkan kontak Anda di laporan web."
    )
    keyboard = {
        "keyboard": [
            [{"text": "📍 Kirim Lokasi Darurat (GPS)", "request_location": True}],
            [{"text": "📱 Bagikan No. Telp / WA", "request_contact": True}]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": keyboard
    }
    send_telegram_request("sendMessage", payload)


def save_incident_to_db(reporter_name, whatsapp_contact, lat, lon):
    try:
        conn = sqlite3.connect(DB_PATH)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            """INSERT INTO incidents (source, reporter_name, whatsapp_contact, latitude, longitude, patient_condition, status, received_at)
               VALUES ('telegram', ?, ?, ?, ?, 'Laporan Darurat via Telegram Bot', 'baru', ?)""",
            (reporter_name, whatsapp_contact or "—", float(lat), float(lon), now_str)
        )
        conn.commit()
        incident_id = cur.lastrowid
        conn.close()
        return incident_id, now_str
    except Exception as e:
        print(f"[Telegram Bot DB Error] {e}")
        return None, None


def update_incident_contact_in_db(incident_id, phone_number):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE incidents SET whatsapp_contact = ? WHERE id = ?",
            (phone_number, incident_id)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[Telegram Bot DB Update Error] {e}")
        return False


def handle_update(update):
    message = update.get("message")
    if not message:
        return

    chat_id = message.get("chat", {}).get("id")
    from_user = message.get("from", {})
    user_id = from_user.get("id", "")
    first_name = from_user.get("first_name", "Warga Telegram")
    username = from_user.get("username")

    # Format ID Telegram & Nama Pelapor
    reporter_name = f"{first_name} (ID: {user_id})"
    if username:
        reporter_name += f" @{username}"

    contact = message.get("contact")
    if contact:
        phone = contact.get("phone_number", "")
        if phone:
            if not phone.startswith("+"):
                phone = f"+{phone}"
            USER_CONTACTS[chat_id] = phone

            # Jika pelapor sudah membuat laporan sebelumnya, perbarui laporan yang sudah ada di web!
            last_inc_id = LAST_USER_INCIDENT.get(chat_id)
            if last_inc_id:
                update_incident_contact_in_db(last_inc_id, phone)
                send_telegram_request("sendMessage", {
                    "chat_id": chat_id,
                    "text": (
                        f"✓ *NOMOR TELEPON HAMPIR TERHUBUNG!*\n\n"
                        f"📋 *ID Laporan:* `#{last_inc_id}`\n"
                        f"📞 *No. Telepon / WA:* `{phone}`\n\n"
                        f"Nomor kontak Anda telah berhasil diperbarui langsung pada Laporan `#{last_inc_id}` di Dashboard Admin SAR."
                    ),
                    "parse_mode": "Markdown"
                })
            else:
                send_telegram_request("sendMessage", {
                    "chat_id": chat_id,
                    "text": f"✓ *No. Telepon Diterima:* `{phone}`\n\nSekarang silakan tekan tombol *📍 Kirim Lokasi Darurat (GPS)*.",
                    "parse_mode": "Markdown"
                })
            return

    text = message.get("text", "")
    location = message.get("location")

    if location:
        lat = location.get("latitude")
        lon = location.get("longitude")
        phone_contact = USER_CONTACTS.get(chat_id, None)

        if lat is not None and lon is not None:
            incident_id, timestamp_str = save_incident_to_db(reporter_name, phone_contact, lat, lon)
            if incident_id:
                LAST_USER_INCIDENT[chat_id] = incident_id

            reply_text = (
                f"✓ *LAPORAN DARURAT BERHASIL DITERIMA!*\n\n"
                f"📋 *ID Laporan:* `#{incident_id}`\n"
                f"👤 *ID / Pelapor:* `{reporter_name}`\n"
                f"📞 *No. Telepon / WA:* `{phone_contact or 'Tidak dicantumkan (Tekan tombol Bagikan No. HP)'}`\n"
                f"📅 *Tanggal & Waktu Diterima:* `{timestamp_str}`\n"
                f"📍 *Koordinat GPS:* `{lat:.5f}, {lon:.5f}`\n\n"
                f"🚀 *Status:* Laporan otomatis muncul di Peta Dashboard Admin SAR. Tim Rescue & Drone sedang diproses!"
            )
            send_telegram_request("sendMessage", {
                "chat_id": chat_id,
                "text": reply_text,
                "parse_mode": "Markdown"
            })
            print(f"[Telegram Bot] Laporan #{incident_id} diterima pada {timestamp_str} dari {reporter_name} ({phone_contact}) [{lat}, {lon}]")
    elif text:
        send_welcome_keyboard(chat_id, first_name)


def poll_telegram_updates():
    print(f"[Telegram Bot] Memulai polling Telegram Emergency Bot (Token: {BOT_TOKEN[:10]}...)...")
    # Hapus webhook lama (jika ada) agar mode polling aktif 100%
    send_telegram_request("deleteWebhook", {"drop_pending_updates": False})
    
    offset = 0
    while True:
        try:
            res = send_telegram_request("getUpdates", {"offset": offset, "timeout": 10})
            if res and res.get("ok"):
                for update in res.get("result", []):
                    offset = update["update_id"] + 1
                    handle_update(update)
        except Exception as e:
            # print(f"[Telegram Polling Loop Error] {e}")
            pass
        time.sleep(1)


def start_telegram_bot():
    thread = threading.Thread(target=poll_telegram_updates, daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    poll_telegram_updates()
