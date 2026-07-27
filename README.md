# RescueDrone — Dashboard + Flight Controller (Mission Planner)

Ini adalah gabungan dari dua proyek:

1. **Web Waypoint → Flight Controller** (Flask + pymavlink) — backend yang
   sudah bisa terhubung nyata ke flight controller (Pixhawk/ArduPilot/PX4)
   lewat MAVLink, dan misinya terlihat di **Mission Planner**.
2. **RescueDrone UI/UX** (`rescuedrone.html`) — tampilan lengkap: Beranda,
   Login Admin, Lapor Kecelakaan, Dashboard, Rekap Panggilan, dan Tentang.
   Backend Node.js yang tadinya dipakai proyek ini sudah **dipindahkan
   ke Flask + SQLite**, jadi sekarang cukup jalankan **satu server saja**.

Dashboard Admin sekarang **tidak lagi simulasi**: tombol "Kirim Misi ke
Flight Controller" betul-betul meng-upload misi (takeoff → waypoint lokasi
insiden → landing) ke flight controller lewat MAVLink, dan panel HUD
(ketinggian, kecepatan, baterai, GPS) diambil dari telemetry nyata
(`GLOBAL_POSITION_INT`, `VFR_HUD`, `SYS_STATUS`, `GPS_RAW_INT`) — bukan
angka acak.

## Struktur

```
rescuedrone-mission/
├── app.py                 # Backend Flask: MAVLink + insiden (SQLite) + login
├── requirements.txt
├── rescuedrone.db          # dibuat otomatis saat pertama kali dijalankan
├── uploads/                # video laporan kecelakaan tersimpan di sini
└── templates/
    └── index.html          # UI RescueDrone (Home, Login, Lapor, Dashboard, Rekap, Tentang)
```

## Instalasi

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Konfigurasi koneksi ke flight controller

> Di Windows, pastikan `pyserial` sudah terpasang agar koneksi serial `COMxx` bekerja.

```bash
# Contoh: terhubung ke Pixhawk lewat USB
export FC_CONNECTION="COM3"
export FC_BAUDRATE="115200"

# Contoh: terhubung ke radio telemetry
export FC_CONNECTION="COM4"
export FC_BAUDRATE="57600"

# Contoh: terhubung ke SITL (simulator) atau Mission Planner via UDP
export FC_CONNECTION="udp:127.0.0.1:14550"
```

Jika tidak memakai device nyata, gunakan `udp:127.0.0.1:14550`.

Di Windows, jika tidak ada COM port yang valid terdeteksi, aplikasi akan
mencoba fallback ke UDP supaya SITL dan Mission Planner tetap bisa diuji.

Ketinggian takeoff & default (opsional):
```bash
export DEFAULT_ALTITUDE_M="20"
export TAKEOFF_ALTITUDE_M="50"
```

Login admin (opsional, default `admin` / `rescuedrone`):
```bash
export ADMIN_USERNAME="admin"
export ADMIN_PASSWORD="rescuedrone"
```

## Menjalankan

```bash
python app.py
```

Lalu buka `http://localhost:5000` (atau `http://<ip-komputer>:5000` dari
perangkat lain di jaringan yang sama).

## Alur pemakaian

1. Warga membuka **Lapor Kecelakaan** → tekan tombol darurat → lokasi GPS
   terkirim dan tersimpan sebagai insiden baru (tersimpan di `rescuedrone.db`).
2. Admin login lewat menu (☰) → **Dashboard Admin**.
3. Dashboard menampilkan insiden yang masuk di peta, plus status koneksi
   flight controller (real, dari `/api/status`).
4. Admin memilih insiden, atur ketinggian waypoint, lalu tekan
   **"Kirim Misi ke Flight Controller"**. Backend meng-upload misi
   takeoff-waypoint-landing lewat MAVLink — bisa langsung dicek di
   Mission Planner.
5. HUD di dashboard menampilkan telemetry nyata (ketinggian, kecepatan,
   baterai, GPS fix) yang dipoll dari flight controller setiap 3 detik.
6. Semua insiden (selesai/belum) bisa dilihat lengkap di **Rekap Panggilan**.

## Menguji tanpa drone fisik (SITL)

```bash
sim_vehicle.py -v ArduCopter --console --map -out udp:127.0.0.1:14550
```

Lalu set `FC_CONNECTION="udp:127.0.0.1:14550"` dan jalankan `app.py`.

## ⚠️ Catatan keselamatan (WAJIB dibaca sebelum dipakai ke drone sungguhan)

- Aplikasi **tidak** meng-arm / take-off otomatis; misi hanya di-upload.
  Untuk menjalankan misi, gunakan Mission Planner atau aktifkan mode AUTO
  di flight controller.
- **Selalu uji di simulator (SITL) dulu** sebelum mencoba pada drone fisik.
- Tambahkan validasi geofence / batas area terbang di backend sebelum
  mengirim perintah ke flight controller.
- Login admin di sini masih gerbang sederhana (untuk demo) — untuk
  pemakaian sungguhan, tambahkan autentikasi yang lebih kuat (hash
  password, sesi/JWT, HTTPS, dsb).
- Sesuaikan `MAV_FRAME` dan mode terbang (GUIDED/AUTO/mission item) dengan
  firmware yang dipakai (ArduPilot vs PX4 memiliki sedikit perbedaan mode).
