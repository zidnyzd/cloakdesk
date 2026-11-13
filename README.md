# CloakDesk

<center>
  <img src="assets/cloakdesk-logo.png" alt="CloakDesk Logo" width="160" height="160" />
</center>

<center>
  <b>Manajer Profil Browser Anti-Detect Mandiri untuk Linux</b><br />
  Kompatibel penuh dengan SDK ixBrowser Local API v2, berjalan lokal tanpa ketergantungan cloud pihak ketiga, dilengkapi sinkronisasi multi-perangkat terenkripsi ujung ke ujung.
</center>

---

## Ringkasan Proyek

**CloakDesk** adalah platform manajemen profil browser anti-detect yang berjalan di atas engine Chromium terisolasi (*fingerprint-chromium* 150) pada sistem Linux. CloakDesk dirancang dengan prinsip **local-first**: seluruh eksekusi browser, manipulasi jendela, perutean proxy, serta data sesi berjalan langsung di komputer lokal pengguna demi menjaga keaslian sidik jari perangkat keras (GPU, layar, audio, WebGL, timezone).

CloakDesk menyediakan API lokal di `127.0.0.1:53200` yang mengimplementasikan kontrak **ixBrowser Local API v2**, sehingga skrip otomasi Selenium, Puppeteer, maupun pustaka resmi `ixbrowser-local-api` dapat langsung digunakan tanpa modifikasi kode.

Selain sebagai peluncur lokal, CloakDesk dilengkapi arsitektur **sinkronisasi multi-perangkat mandiri (self-hosted)**: server sinkronisasi dapat dipasang di VPS pribadi untuk menyinkronkan profil, grup, proxy, cookie sesi, bookmark, ekstensi, dan password tersimpan antar-komputer secara aman dengan enkripsi amplop sisi klien (*client-side envelope encryption*). Server tidak pernah menjalankan browser dan tidak memiliki akses ke data sensitif (*zero-knowledge*).

---

## Fitur Utama

- **Isolasi Fingerprint Nyata**: Engine *fingerprint-chromium* 150 mengisolasi Canvas, WebGL, AudioContext, font, ClientRects, platform OS, core CPU, memori, bahasa, dan timezone.
- **Penyaringan Seed Aman (Seed Screening)**: Menggunakan tabel 2.093 seed aman terverifikasi pada engine 150 untuk mencegah anomali lebar teks kanvas bernilai negatif.
- **Kompatibilitas SDK ixBrowser**: Meniru endpoint `/api/v2/<action>` ixBrowser v2 secara transparan, lengkap dengan format respons standar dan pagination.
- **Dukungan Proxy Lengkap dengan Autentikasi**: Mendukung proxy Direct, HTTP, HTTPS, dan SOCKS5. Proxy yang membutuhkan autentikasi username/password dijembatani otomatis via proses `gost` per profil.
- **Sinkronisasi Multi-Perangkat (Self-Hosted VPS)**:
  - **Server Mandiri**: Berjalan sebagai kontainer ringan atau proses Python di VPS dengan database SQLite WAL.
  - **Kunci Profil Terdistribusi (Distributed Profile Lease)**: Mencegah pembukaan profil yang sama secara bersamaan di dua komputer (TTL 60 detik, heartbeat otomatis 20 detik, dan fencing token monotonik).
  - **Enkripsi Klien Zero-Knowledge**: Cookie sesi dan password dienkripsi dengan AES-256-GCM berbasis kunci turunan frasa sandi pengguna (*passphrase*) sebelum diunggah ke server.
  - **Dukungan Buka Offline**: Profil tetap dapat dibuka dan diedit saat komputer offline (*Allow with Warning*), antrean perubahan tersimpan di outbox lokal dan otomatis dikirim saat kembali online.
- **Impor Penuh Arsip ixBrowser**:
  - Mengimpor file zip ekspor cookie (`<serial>_<judul>.txt`) dan metadata spreadsheet (`excel.xlsx`).
  - Pemetaan grup, penentuan platform otomatis dari domain cookie, dan pembacaan cookie Chromium v10.
  - Tersedia lewat antarmuka dialog GUI dan perintah CLI `scripts/import-ixbrowser.py`.
- **Penutupan Profil & Preservasi Sesi Akun Google**:
  - Penutupan bersih dilakukan via protokol DevTools (`Browser.close` pada CDP WebSocket) agar Chromium menutup seluruh tab secara atomik dari dalam, memicu `SessionService::Save`, dan mem-flush snapshot sesi serta session cookie ke disk secara utuh (dengan fallback penutupan per-tab `/json/close` hanya jika WebSocket gagal).
  - Memantau penyelesaian rollback journal SQLite di kedua lokasi standar Chromium (`Default/Network/Cookies-journal` dan `Default/Cookies-journal`) sebelum proses dianggap selesai.
- **Cooldown Keamanan 30 Detik (Anti Rapid-Restart)**:
  - Mencegah pembukaan kembali profil yang baru ditutup selama 30 detik (baik dari countdown tombol UI maupun validasi backend API), menjaga cookie rotasi waktu Google (`__Secure-1PSIDTS` dan `__Secure-3PSIDTS`) tetap sinkron sehingga sesi login multi-akun tidak dicabut oleh heuristik server Google.
  - Durasi cooldown dapat dikonfigurasi melalui variabel lingkungan `ANTIDETECT_COOLDOWN_SECONDS` (default `30`), atau dinonaktifkan sementara untuk kebutuhan skrip otomatisasi dengan `ANTIDETECT_NO_COOLDOWN=1`.
- **Pembersihan Otomatis Proses Mati**: Jika jendela browser ditutup langsung dari tombol silang desktop (X11), status profil di memori dan kunci lease langsung dibersihkan otomatis.
- **Antarmuka Desktop Modern**: Dashboard berbasis Vue 3 + Element Plus yang disajikan langsung oleh server lokal di `http://127.0.0.1:53200` dengan tema bersih, indikator status sinkronisasi topbar, badge lease perangkat lain, manajemen kernel Chromium, dialog profil 5 tab, keranjang sampah, dan log aktivitas lokal.

---

## Panduan Instalasi dari Nol (Clone Baru)

### 1. Prasyarat Sistem Linux

Pastikan sistem Linux (Debian, Ubuntu, Linux Mint, Fedora, Arch) telah memiliki paket dasar berikut:

- **Python 3.10+** beserta `venv` dan `pip`:
  ```bash
  # Debian / Ubuntu / Linux Mint
  sudo apt update && sudo apt install -y python3 python3-venv python3-pip curl
  ```
- **Node.js 20+ dan npm** (untuk membangun antarmuka Web UI):
  ```bash
  # Debian / Ubuntu / Linux Mint
  sudo apt install -y nodejs npm
  ```

### 2. Klon Repositori dan Instalasi Dependensi Python

```bash
git clone https://github.com/zidnyzd/antidetect.git cloakdesk
cd cloakdesk

# Buat virtual environment dan pasang dependensi Python
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 3. Penyiapan Engine Fingerprint Chromium

CloakDesk membutuhkan engine browser *fingerprint-chromium* 150 (berbasis ungoogled-chromium yang mendukung flag `--fingerprint=<seed>`).

- **Lokasi Default Engine**:
  CloakDesk secara otomatis mencari biner engine di:
  `~/.local/share/antidetect/squashfs-root/opt/ungoogled-chromium/chrome`
  (atau lokasi kustom melalui environment variable `ANTIDETECT_ENGINE_DIR`).
- **Ekstraksi dari AppImage (Jika Memiliki `engine.AppImage`)**:
  Jika engine didistribusikan dalam format AppImage, ekstrak terlebih dahulu ke direktori tersebut:
  ```bash
  mkdir -p ~/.local/share/antidetect/squashfs-root
  ./engine.AppImage --appimage-extract >/dev/null 2>&1
  mv squashfs-root ~/.local/share/antidetect/
  ```
- **Manajemen Multi-Kernel (Opsional)**:
  Untuk memasang atau mengelola versi kernel Chromium tambahan, CloakDesk menyediakan:
  ```bash
  ./scripts/install-kernel.sh /path/ke/arsip-kernel.tar.xz "Nama Kernel"
  ```
  Kernel yang terpasang akan tersimpan di `~/.local/share/antidetect/kernels/` dan dapat dipilih langsung dari tab Kernel di antarmuka CloakDesk.

### 4. Build Antarmuka Pengguna (Web UI) dan Menjalankan Server

Terdapat dua cara menjalankan CloakDesk sesuai kebutuhan:

#### Opsi A: Menjalankan Aplikasi Lengkap (Mode Produksi / Harian)

Server backend otomatis menyajikan antarmuka Vue 3 yang telah dikompilasi (`ui/dist`) langsung pada port `53200`:

```bash
# Masuk ke folder ui, pasang dependensi npm, lalu build antarmuka
cd ui
npm install
npm run build
cd ..

# Jalankan server backend CloakDesk
./venv/bin/python -m antidetect serve
```

Buka peramban Anda dan akses **http://127.0.0.1:53200**. Seluruh fitur antarmuka dan API lokal aktif dalam satu port.

#### Opsi B: Mode Pengembangan Frontend (Hot Reload)

Jika Anda mengembangkan atau memodifikasi kode Vue di `ui/src`:

1. Terminal 1 (Backend API):
   ```bash
   ./venv/bin/python -m antidetect serve
   ```
2. Terminal 2 (Frontend Vite Dev Server):
   ```bash
   cd ui
   npm install
   npm run dev
   ```
3. Buka peramban di **http://127.0.0.1:5180**. Vite dev server akan melakukan *hot reload* otomatis setiap kali berkas Vue berubah.

---

## Penggunaan Perintah CLI

Selain antarmuka grafis, CloakDesk menyediakan CLI terpadu melalui modul `antidetect`:

```bash
# Menampilkan seluruh daftar perintah
./venv/bin/python -m antidetect --help

# Membuat profil baru
./venv/bin/python -m antidetect create "Toko Utama" --platform Windows --tz Asia/Jakarta --lang id-ID

# Membuka dan menutup profil
./venv/bin/python -m antidetect open 1
./venv/bin/python -m antidetect close 1

# Menampilkan daftar profil
./venv/bin/python -m antidetect list
```

---

## Panduan Sinkronisasi Multi-Perangkat (Self-Hosted)

Sistem sinkronisasi CloakDesk memisahkan fungsi server murni sebagai penyimpan metadata dan blob terenkripsi, sementara browser selalu berjalan di komputer lokal pengguna.

```text
+-----------------------+              +-----------------------+
|   Laptop Utama (A)    |              |    Laptop Kedua (B)   |
|  - Engine Lokal       |              |  - Engine Lokal       |
|  - SQLite antidetect  |              |  - SQLite antidetect  |
+-----------+-----------+              +-----------+-----------+
            |                                      |
            | Push / Pull                          | Push / Pull
            | Kunci Lease (Heartbeat)              | Kunci Lease
            | AES-256-GCM Blobs                    | AES-256-GCM Blobs
            v                                      v
  +----------------------------------------------------------+
  |             SERVER SINKRONISASI (VPS Pribadi)            |
  |  - Endpoint /v1/sync/* (HTTPS / Reverse Proxy)           |
  |  - Database server_sync.db (SQLite WAL + change_log)     |
  |  - Penyimpanan Berkas Blob Terenkripsi                   |
  +----------------------------------------------------------+
```

### 1. Menjalankan Server Sinkronisasi di VPS

Di server VPS (Linux), pasang dependensi dan jalankan server sinkronisasi:

```bash
# Menjalankan langsung via Python
python3 -m antidetect sync-server run --host 0.0.0.0 --port 53210 --data-dir /var/lib/cloakdesk-sync

# Atau menggunakan Docker
docker build -f Dockerfile.sync-server -t cloakdesk-sync .
docker run -d --name cloakdesk-sync -p 53210:53210 -v /var/lib/cloakdesk-sync:/data cloakdesk-sync
```

Buat token pendaftaran satu kali untuk menghubungkan komputer pertama:
```bash
python3 -m antidetect sync-server token create --label "Laptop Utama" --ttl 120
# Output: ad_enroll_xxxxxxxxxxxxxxxxxxxxxxxx
```

### 2. Menghubungkan Komputer Lokal

1. Buka antarmuka CloakDesk di browser (`http://127.0.0.1:53200/#/settings`).
2. Masuk ke kartu **Sinkronisasi Antar Perangkat**.
3. Masukkan:
   - **URL Server**: Alamat server VPS kamu (misalnya `https://sync.domainanda.com` atau `http://IP_VPS:53210`).
   - **Token Pendaftaran**: Token yang dihasilkan dari langkah sebelumnya.
   - **Nama Perangkat**: Nama komputer ini (misalnya `Laptop Kerja`).
   - **Frasa Sandi Enkripsi (Passphrase)**: Buat kata sandi rahasia untuk mengunci data sesi.
4. Klik **Hubungkan Server**.

Indikator status pada bagian kanan topbar akan otomatis berubah menjadi hijau (**Sync: Aktif**), dan seluruh profil lokal akan disinkronkan secara aman ke server.

---

## Pemetaan Fingerprint ke Flag Engine

| Konfigurasi Fingerprint | Flag Engine Chromium | Keterangan & Efek |
| --- | --- | --- |
| `platform` (`Windows` / `Macos`) | `--fingerprint-platform=windows/macos` | Menyesuaikan User-Agent, `navigator.platform`, dan Client Hints. |
| `system_version` (`10` / `11`) | `--fingerprint-platform-version=<versi>` | Mengatur `platformVersion` pada Client Hints User-Agent. |
| `hardware_concurrency` | `--fingerprint-hardware-concurrency=N` | Nilai `navigator.hardwareConcurrency` (jumlah core CPU). |
| `device_memory` | `--fingerprint-memory=N` | Nilai `navigator.deviceMemory` (RAM dalam GB). |
| `language` | `--lang`, `--accept-lang` | Nilai `navigator.language` dan header HTTP `Accept-Language`. |
| `timezone` | `--timezone=<zona>` | Zona waktu JavaScript (contoh: `Asia/Jakarta`). |
| `seed` | `--fingerprint=<seed>` | Nilai acak konsisten yang mendasari isolasi Canvas, WebGL, dan GPU vendor/renderer. |
| `disable_spoofing` | `--disable-spoofing=<komponen>` | Mematikan penyamaran pada komponen terpilih (`canvas`, `gpu`, dll.) agar menggunakan perangkat asli. |

---

## Proxy dengan Autentikasi Gost

Chromium bawaan tidak mendukung autentikasi proxy HTTP/SOCKS5 secara langsung lewat flag baris perintah. CloakDesk secara otomatis mengelola proses jembatan lokal `gost` untuk setiap profil yang memiliki username dan password proxy:

```text
[Engine Chrome] --> [Listener Lokal gost: 127.0.0.1:<port>] --> [Proxy Hulu Otentikasi] --> [Internet]
```

Skrip pemasangan gost otomatis tersedia di:
```bash
scripts/fetch-gost.sh
```

---

## Impor Data ixBrowser

CloakDesk dapat membaca dan memigrasikan seluruh profil dari file ekspor ixBrowser:

```bash
# Pratinjau isi arsip ekspor
./venv/bin/python scripts/import-ixbrowser.py --zip "Export Profiles Cookies.zip"

# Menjalankan impor langsung ke database
./venv/bin/python scripts/import-ixbrowser.py --zip "Export Profiles Cookies.zip" --apply
```

Pada antarmuka GUI, tombol **Impor ixBrowser** di toolbar daftar profil menyediakan pemilih berkas untuk mengunggah ZIP cookie wajib dan ZIP metadata `Export Profile.zip` opsional, lengkap dengan pratinjau deteksi duplikat dan konfirmasi pembaruan.

---

## Pengujian Mandiri Otomatis

CloakDesk dilengkapi rangkain skrip pengujian otomatis untuk memverifikasi integritas sistem tanpa merusak data pengguna:

```bash
# Pengujian preservasi sesi, penutupan Browser.close, dan cooldown 30 detik (63 skenario)
./venv/bin/python scripts/test-session-preservation.py

# Pengujian preferensi ekstensi
./venv/bin/python scripts/test-extension-prefs.py

# Pengujian penggabungan sinkronisasi
./venv/bin/python scripts/test-sync-merge-notice.py

# Pengujian kontrak SDK resmi ixBrowser (server harus berjalan)
./venv/bin/python scripts/selftest.py

# Pengujian isolasi cookie dan penutupan bersih (DISPLAY=:0)
DISPLAY=:0 ./venv/bin/python scripts/isolation_test.py

# Pengujian jembatan autentikasi proxy gost lokal
./venv/bin/python scripts/test-auth-proxy.py

# Pengujian impor arsip ixBrowser
./venv/bin/python scripts/test-ixbrowser-import.py

# Pengujian sinkronisasi multi-perangkat
./venv/bin/python scripts/test-sync.py
```

---

## Struktur Berkas Proyek

```text
/path/ke/kerja/antidetect/
├── assets/                          # Asset visual (logo CloakDesk SVG dan PNG)
├── antidetect/                      # Paket utama Python
│   ├── __main__.py                  # Titik masuk perintah CLI
│   ├── api.py                       # HTTP server Local API v2 (:53200) & static UI server
│   ├── bookmarks.py                 # Ekstraksi dan injeksi Default/Bookmarks
│   ├── cookies.py                   # Dekripsi dan enkripsi SQLite Cookies v10
│   ├── extensions.py                # Manajemen paket ekstensi lokal
│   ├── iximport.py                  # Modul pengurai arsip ekspor ixBrowser
│   ├── kernels.py                   # Siklus hidup instalasi & manajemen kernel Chromium
│   ├── launcher.py                  # Orkestrasi proses Chromium, cooldown, journal, dan gost
│   ├── passwords.py                 # Dekripsi dan injeksi SQLite Login Data
│   ├── store.py                     # Lapisan database lokal SQLite & sync outbox
│   ├── sync_agent.py                # Agen sinkronisasi klien & thread heartbeat lease
│   ├── sync_crypto.py               # Enkripsi amplop AES-256-GCM & penurunan kunci
│   └── sync_server.py               # Server sinkronisasi mandiri (:53210)
├── docs/                            # Dokumentasi teknis dan rencana arsitektur
├── scripts/                         # Skrip utilitas, pemasangan kernel, dan pengujian
│   ├── build-deb.sh                 # Pembangun paket Debian (.deb)
│   ├── cloakdesk-app.sh             # Peluncur aplikasi desktop standalone
│   ├── fetch-gost.sh                # Pemasang binary gost
│   ├── install-kernel.sh            # Pemasang kernel Chromium tambahan
│   ├── test-session-preservation.py # Pengujian preservasi sesi & cooldown 30 detik
│   └── antidetect-sync-server.service # Berkas unit systemd untuk VPS
├── ui/                              # Frontend Vue 3 + Element Plus
│   ├── src/views/Profiles.vue       # Tabel profil, countdown cooldown, aksi buka/tutup
│   ├── src/views/Settings.vue       # Halaman pengaturan & kartu sync server
│   └── src/App.vue                  # Shell aplikasi & indikator sync topbar
├── Dockerfile.sync-server           # Kontainer Docker server sinkronisasi
└── README.md                        # Dokumentasi proyek
```

---

## Lisensi

Proyek ini dilisensikan di bawah **GNU Affero General Public License v3.0** (`AGPL-3.0-only`). Lihat berkas `LICENSE` untuk informasi selengkapnya.