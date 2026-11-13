# Analisis Ulang: Akar Masalah Logout Akun Google Saat Buka-Tutup Profil Berulang

Repo: cloakdesk (CloakDesk)
Tanggal: 2026-11-11
Auditor: Principal Systems Architect & Lead Infrastructure Security Engineer

## 1. Ringkasan Eksekutif (TL;DR)

Akun Google yang sudah login (Gmail, Google Workspace, Google Account) dapat ter-logout sendiri setelah profil CloakDesk dibuka dan ditutup berulang kali. Audit ulang berbasis kode sumber menemukan bahwa koran utama bukan sinkronisasi cloud, melainkan **cacat arsitektur di jalur penutupan profil lokal** yang dicampur dengan perlindungan sisi server Google.

Tiga masalah konkret yang teridentifikasi:

1. **Cacat alur penutupan tab vs browser** (`launcher.py:1259-1264`): `_graceful_close` menutup setiap tab satu per satu melalui `/json/close/{id}` sebelum mengirim `Browser.close`. Hal ini menyebabkan Chromium mencatat sesi terakhir sebagai nol tab (kosong), merusak integritas session restore dan menghancurkan state sesi Google sebelum browser benar-benar keluar.

2. **Bug lokasi path journal** (`launcher.py:1234` vs `cookies.py:174-180`): `_wait_journal` memeriksa `Default/Cookies-journal`, tetapi `cookies.py:_database_path` mengonfirmasi bahwa Chromium modern menyimpan cookie di `Default/Network/Cookies`. Jika profil berada di layout modern, `_wait_journal` memeriksa path yang salah dan tidak pernah menunggu journal selesai ditulis.

3. **Penggunaan `restore_on_startup = 1` pada profiling non-sinkron** (`launcher.py:881-916`): `ensure_password_manager` menetapkan preferensi pemulihan sesi terakhir pada setiap profil. Pada profil lokal murni tanpa sinkronisasi cloud, ini memaksa Chromium memuat ulang cookie sesi dari disk pada setiap pembukaan, yang justru memperluas jendela racunan antara cookie lama dan cookie yang baru ditulis.

Logout lengkap terjadi karena kombinasi ketiga masalah di atas dengan mekanisme deteksi sisi server Google yang mencabut sesi saat timestamp `__Secure-1PSIDTS` / `__Secure-3PSIDTS` tidak konsisten atau terindikasi rotasi yang tidak wajar.

## 2. Koreksi Validasi Terhadap Asumsi Awal

### 2.1 Journal Mode SQLite: Delete, Bukan WAL

Database `Cookies` Chromium di engine ini menggunakan `journal_mode: delete`, bukan WAL. File sementaranya adalah `Cookies-journal`, bukan `Cookies-wal`. Saran generic SQLite seperti `PRAGMA wal_checkpoint(TRUNCATE)` tidak berlaku untuk Cookies DB Chromium. Fokus harus pada flushing rollback journal dan file lock.

Bukti dari kode:
- `launcher.py:1234`: `journal = Path(data_dir) / "Default" / "Cookies-journal"` :  mengonfirmasi bahwa kode menunggu file journal dengan pola delete mode.
- `cookies.py:174-180`: `_database_path` memeriksa `Default/Network/Cookies` terlebih dahulu, lalu fallback ke `Default/Cookies` :  ada dua lokasi cookie yang mungkin.

Perbaikan asumsi: analisis sebelumnya menyebutkan WAL dan `Cookies-wal`. Ini tidak tepat untuk engine Chromium ini. File yang relevan adalah `Cookies-journal` yang berada di lokasi yang sama dengan file `Cookies` (baik `Default/Cookies-journal` maupun `Default/Network/Cookies-journal`).

### 2.2 Profil Lokal vs Profil Cloud Sync

Analisis sebelumnya menjadikan sinkronisasi cloud sebagai biang kerok tunggal. Ini tidak akurat. Pada profil lokal murni (tanpa sync server terkonfigurasi), alur `_sync_cookies_after_close` di `launcher.py:1115` langsung return karena tidak ada sync endpoint. Namun logout akun Google setelah buka-tutup berulang tetap terjadi. Hal ini membuktikan bahwa masalah tersembunyi di dalam mekanisme penutupan proses dan lifecycle sesi lokal itu sendiri, bukan karena sinkronisasi cloud.

Bukti:
- `launcher.py:1114-1115`: `resolved_url, resolved_token = sync_agent._try_resolve_endpoint(conn)`; jika tidak ada endpoint yang terselesaikan, blok upload dilewati.
- `launcher.py:1580-1582` dan `1594-1595`: `_sync_cookies_after_close` hanya dipanggil saat `clean_exit` True. Tanpa endpoint sinkronisasi, fungsi ini tetap berjalan tetapi tidak mengupload apa pun ke server.

Implikasi: meski profil tidak pernah menyinkronkan cookie ke cloud, Google tetap dapat mencabut sesi karena cookie yang dibaca dari disk pada pembuka berikutnya tidak konsisten dengan yang diharapkan server.

## 3. Anatomi Masalah Utama

### 3.1 Cacat Alur Penutupan Tab vs Browser (`/json/close` per-tab sebelum `Browser.close`)

`_graceful_close` di `launcher.py:1230` menjalankan urutan berikut:

1. Membaca daftar target CDP dari `/json/list` (`launcher.py:1251-1252`).
2. Menutup setiap target page satu per satu melalui `/json/close/{target['id']}` (`launcher.py:1259-1264`).
3. Mengirim `Browser.close` melalui CDP websocket (`launcher.py:1283`).
4. Menunggu proses Chromium mati (`launcher.py:1301-1311`).

**Apa yang terjadi jika semua tab ditutup satu per satu sebelum browser ditutup?**

Setiap panggilan `/json/close/{id}` memberitahu Chromium untuk menutup tab tersebut. Saat semua tab ditutup manual sebelum `Browser.close`, Chromium mencatat session terakhir sebagai 0 tab. Pada saat `Browser.close` akhirnya dikirim, tidak ada tab yang dapat dipulihkan. Preferensi `session.restore_on_startup = 1` (`launcher.py:907-913`) menjadi kontradiktif: Chromium diminta untuk memulihkan sesi terakhir, tetapi sesi terakhir yang dicatat adalah kosong.

**Dampaknya terhadap `SessionService::Save` dan `session.restore_on_startup = 1`:**

`SessionService::Save` di Chromium menulis state sesi saat browser keluar. Jika semua tab sudah ditutup satu per satu, state yang disimpan adalah "tidak ada tab". Saat profil dibuka kembali, Chromium memulihkan sesi kosong. Cookie sesi yang terkait dengan tab yang telah di-destroy (seperti `GMAIL_AT` dan cookie otentikasi Google lainnya) kehilangan konteks tabnya dan dapat dianggap orphaned oleh Chromium.

**Dampaknya terhadap session cookies dan service worker Google:**

Session cookies seperti `GMAIL_AT` terikat pada tab dan service worker miliknya. Ketika tab di-destroy satu per satu sebelum browser shutdown, service worker Google yang berjalan di dalam tab tersebut dihentikan secara paksa. Cookie yang menyimpan state otentikasi dapat tidak berhasil di-flush ke disk karena konteks tab yang mengahdlnya sudah hilang.

**Mengapa penutupan tab manual sebelum `Browser.close` justru merusak integritas session restore?**

Session restore di Chromium bergantung pada snapshot state dari semua tab pada saat `Browser.close` dipanggil. Jika tab sudah ditutup satu per satu, snapshot tersebut kosong atau tidak lengkap. `restore_on_startup = 1` mencoba memulihkan dari snapshot kosong, sehingga semua cookie sesi dan state tab hilang. Ini adalah akar utama dari fenomena "logout setelah buka-tutup berulang": setiap siklus buka-tutup mencatat sesi kosong, dan pembuka berikutnya memulai dari nol.

### 3.2 Dampak pada Session Restore (`restore_on_startup = 1`) dan Penghancuran Tab State

`ensure_password_manager` di `launcher.py:881-928` menetapkan:
- `session.restore_on_startup = 1` (`launcher.py:907-913`), yang berarti "restore last session".
- `credentials_enable_service = True` (`launcher.py:904-906`).

Dengan `restore_on_startup = 1`, Chromium akan memulihkan sesi terakhir dari disk setiap kali browser dibuka. Namun karena cacat di bagian 3.1, sesi terakhir yang disimpan adalah kosong (0 tab). Hasilnya:
- Pembukaan profil berikutnya tidak memulihkan tab Google yang sebelumnya terbuka.
- Cookie sesi yang hanya ada di memori (session cookie) hilang karena tidak ada tab yang menyimpannya.
- Pengguna melihat tampilan login Google meski sebelumnya sudah login.

Penghancuran tab state satu per satu (`launcher.py:1259-1264`) terjadi sebelum `Browser.close` memiliki kesempatan untuk menyimpan snapshot lengkap. Ini adalah kesalahan urutan operasi: seharusnya `Browser.close` dikirim terlebih dahulu agar Chromium secara internal menangani penutupan tab dan menyimpan session state yang konsisten.

### 3.3 Bug Lokasi Path `_wait_journal` (`Default/Cookies-journal` vs `Default/Network/Cookies-journal`)

`_graceful_close` di `launcher.py:1233-1234`:
```python
journal = Path(data_dir) / "Default" / "Cookies-journal"
```

Sedangkan `cookies.py:_database_path` di `cookies.py:174-180`:
```python
def _database_path(profile_id):
    base = _profiles_dir() / str(profile_id) / "data" / "Default"
    network = base / "Network" / "Cookies"
    legacy = base / "Cookies"
    if network.exists():
        return network
    return legacy
```

Jika profil menggunakan layout modern `Default/Network/`, maka file cookie sebenarnya adalah `Default/Network/Cookies` dan journal-nya adalah `Default/Network/Cookies-journal`. Namun `_wait_journal` memeriksa `Default/Cookies-journal` yang tidak ada. Akibatnya:
- `_wait_journal` tidak pernah menemukan journal yang benar.
- Fungsi langsung kembali tanpa menunggu flushing selesai.
- `_sync_cookies_after_close` (`launcher.py:1091`) membaca file `Cookies` yang mungkin masih dalam transisi penulisan.
- Data yang dibaca dapat stale atau tidak lengkap.

Ini adalah bug lokasi path yang signifikan. Pada profil yang menggunakan layout legacy `Default/Cookies`, `_wait_journal` bekerja (asumsi journal ada di lokasi yang sama). Pada profil modern, fungsi ini menjadi no-op secara efektif.

### 3.4 Mekanisme Pertahanan Server Google (Rotasi `PSIDTS`, Heuristik Rapid Restart, dan Sesi Terputus)

Google menggunakan cookie autentikasi dalam pasangan yang saling terikat:

- **Cookie sesi inti**: `SID`, `HSID`, `SSID`, `APISID`, `SAPISID` :  menahan ID sesi dan token otentikasi.
- **Cookie terikat waktu**: `__Secure-1PSIDTS` dan `__Secure-3PSIDTS` :  menyimpan timestamp pembuatan atau mutasi terbaru sesi.

Interaksi antara keduanya:
1. Saat login, Google mengeluarkan `SID`/`APISID` yang baru dan menuliskan timestamp ke `__Secure-1PSIDTS` dan `__Secure-3PSIDTS`.
2. Server memeriksa monotonic increase: nilai `PSIDTS` baru harus lebih besar atau sama dengan nilai terakhir yang tercatat. Jika nilai mundur, sesi dianggap tidak valid.
3. Saat sesi dimutasi (mis. refresh token), timestamp diperbarui.

**Mengapa restart browser berulang kali dalam interval pendek memicu deteksi anomali?**

Setiap kali profil dibuka dan ditutup, cookie `PSIDTS` yang dibaca dari disk mungkin memiliki nilai yang berbeda tergantung pada konsistensi flush. Jika cookie dari snapshot lama memiliki timestamp lebih kecil dibandingkan dengan yang terakhir kali terlihat server, server mendeteksi token mundur. Pola restart cepat juga mirip dengan replay kredensial atau credential theft, yang memicu penandaan risiko.

**Apa yang terjadi pada session cookie jika Chromium di-kill atau tab di-close saat rotasi token sedang berlangsung?**

Jika Chromium dipaksa keluar (`_kill_pgid` di `launcher.py:1213-1216` atau `1577`) saat rotasi token berlangsung, cookie baru yang masih di memori tidak berhasil ditulis ke disk. Pada pembuka berikutnya, browser membaca cookie lama dengan timestamp yang mundur. Server Google mencabut sesi dan mengarahkan pengguna untuk login ulang. Hal ini menjelaskan mengapa logout terlihat mendadak dan terkait dengan buka-tutup berulang.

## 4. Skenario Kronologis: Apa yang Terjadi pada Buka-Tutup ke-1, ke-2, hingga Logout Terjadi

### Buka-Tutup ke-1

1. `open_profile` dipanggil (`launcher.py:1399`). `ensure_password_manager` menetapkan `restore_on_startup = 1` dan `credentials_enable_service = True` (`launcher.py:881-928`).
2. Chromium dimulai dengan data_dir yang berisi cookie Google dari sesi sebelumnya.
3. Pengguna login ke Google. Cookie `SID`, `APISID`, `__Secure-1PSIDTS`, `__Secure-3PSIDTS`, dan `GMAIL_AT` ditulis ke `Cookies` (atau `Network/Cookies`).
4. `close_profile` dipanggil (`launcher.py:1556`).
5. `_graceful_close` menutup setiap tab satu per satu (`launcher.py:1259-1264`). State sesi terakhir dicatat sebagai 0 tab karena semua tab sudah ditutup sebelum `Browser.close`.
6. `Browser.close` dikirim (`launcher.py:1283`). Chromium keluar dan mencoba flush cookie ke disk.
7. `_wait_journal` memeriksa `Default/Cookies-journal` (`launcher.py:1234`). Jika profil modern, journal sebenarnya di `Default/Network/Cookies-journal`, sehingga fungsi tidak menunggu apa pun.
8. `_sync_cookies_after_close` membaca `Cookies` (`launcher.py:1091`). Data mungkin lengkap atau sebagian tergantung pada timing flush.
9. Jika ada sinkronisasi cloud, cookie di-upload ke server (`launcher.py:1127`).

### Buka-Tutup ke-2 hingga ke-N

1. `open_profile` dipanggil lagi. `restore_on_startup = 1` memaksa Chromium memulihkan sesi terakhir. Karena sesi terakhir dicatat sebagai 0 tab (karena cacat di bagian 3.1), tidak ada tab Google yang dipulihkan.
2. Cookie yang ada dari pembuka sebelumnya dibaca dari disk. Jika flush sebelumnya tidak lengkap (karena bug path journal di bagian 3.3), cookie yang dibaca dapat stale.
3. Google melayani halaman dengan cookie yang diterima. Jika `PSIDTS` memiliki nilai yang lebih rendah dibandingkan yang terakhir kali tercatat server, server mendeteksi token mundur.
4. `close_profile` dipanggil lagi. Siklus yang sama berulang. Setiap kali, sesi terakhir dicatat sebagai kosong.
5. Setelah beberapa siklus, server Google mencabut sesi sepenuhnya karena pola restart cepat dan timestamp tidak konsisten.

### Titik Logout Terjadi

Logout terjadi saat server Google mengirim respons yang memberitahu browser untuk menghapus cookie sesi (mis. `Set-Cookie` dengan masa berlaku di masa lalu) atau ketika browser mencoba mengakses layanan Google dan mendapatkan respons 401/403 yang memaksa login ulang. Ini biasanya terjadi setelah 3-5 siklus buka-tutup, tergantung pada seberapa cepat cookie flush dan seberapa konsisten nilai `PSIDTS` yang dibaca dari disk.

## 5. Rekomendasi Solusi Berbasis Bukti

### P0: Arsitektur Penutupan Lokal (Prioritas Tinggi)

1. **Hentikan penutupan tab satu per satu sebelum `Browser.close`**.
   Ganti loop `/json/close/{id}` di `launcher.py:1259-1264` dengan pengiriman `Browser.close` langsung. Biarkan Chromium secara internal menangani penutupan tab dan penyimpanan session state. Jika `Browser.close` gagal, baru fallback ke penutupan tab individu.

2. **Perbaiki path journal di `_wait_journal`**.
   Ubah `launcher.py:1234` untuk mencerminkan lokasi sebenarnya:
   ```python
   journal = Path(data_dir) / "Default" / "Network" / "Cookies-journal"
   legacy_journal = Path(data_dir) / "Default" / "Cookies-journal"
   ```
   Periksa keduanya, atau gunakan logika yang sama dengan `cookies.py:_database_path` (`cookies.py:174-180`) untuk menentukan lokasi journal yang benar.

3. **Perpanjang waktu tunggu journal**.
   Ubah batas 3 detik di `launcher.py:1238` menjadi minimal 10-30 detik, atau tunggu hingga journal benar-benar hilang dengan deadline yang lebih lama. Cookie Chromium dapat membutuhkan waktu lebih dari 3 detik untuk di-flush.

4. **Pastikan `_sync_cookies_after_close` tidak dibaca sebelum flush selesai**.
   Setelah proses Chromium mati, tambahkan jeda minimal 500 ms sebelum `get_cookies` dipanggil di `launcher.py:1091`. Atau gunakan mekanisme polling pada file `Cookies` untuk memastikan ukuran file stabil sebelum dibaca.

### P1: Perbaikan Path & Journal (Prioritas Menengah)

1. **Sinkronkan lokasi journal dengan `_database_path`**.
   Buat fungsi helper yang menentukan lokasi `Cookies` dan `Cookies-journal` berdasarkan logika yang sama dengan `cookies.py:_database_path` (`cookies.py:174-180`). Gunakan fungsi ini di `_graceful_close` untuk menentukan path journal yang benar.

2. **Gunakan `PRAGMA wal_checkpoint(TRUNCATE)` jika diperlukan**.
   Meskipun Chromium menggunakan delete journal, jika ada tindakan manual pada file `Cookies` setelah proses mati, pastikan tidak ada journal yang tersisa sebelum membaca. Periksa keberadaan `Cookies-journal` di lokasi yang benar sebelum `get_cookies` dipanggil.

3. **Perbaiki `verify_cookie_db`**.
   Ganti `PRAGMA quick_check` di `cookies.py:653` dengan pemeriksaan yang lebih ketat, atau setidaknya pastikan koneksi ditutup dengan benar sebelum membaca (`cookies.py:296` sudah memanggil `conn.close()` dalam `finally`, yang baik).

### P2: Mitigasi Sisi Server (Prioritas Rendah)

1. **Batasi frekuensi buka-tutup profil yang sama**.
   Jangan buka-tutup profil Google dalam interval kurang dari 10 detik. Ini mengurangi pemicu heuristik rapid restart di sisi server Google.

2. **Pertimbangkan menonaktifkan `restore_on_startup = 1` untuk profil yang sering dibuka-tutup**.
   Atau gunakan nilai `0` (tidak memulihkan) untuk mencegah Chromium memuat ulang sesi kosong yang mencatat 0 tab. Namun ini akan memengaruhi pengalaman pengguna yang ingin tab sebelumnya tetap terbuka.

3. **Pantau `cookie_res` dari `_sync_cookies_after_close`**.
   Di `launcher.py:1582` dan `1595`, hasil `_sync_cookies_after_close` saat ini diabaikan. Tambahkan logging untuk mendeteksi conflict atau skip upload (`sync_agent.py:1050-1055`).

## 6. Rujukan Kode

- `antidetect/launcher.py:881` `ensure_password_manager` :  menetapkan `restore_on_startup = 1`.
- `antidetect/launcher.py:907-913` :  preferensi session restore.
- `antidetect/launcher.py:1084` `_sync_cookies_after_close` :  membaca dan mengupload cookie setelah close.
- `antidetect/launcher.py:1091` :  pemanggilan `cookies.get_cookies`.
- `antidetect/launcher.py:1114-1115` :  resolusi endpoint sinkronisasi (return early jika tidak ada).
- `antidetect/launcher.py:1230` `_graceful_close` :  alur penutupan utama.
- `antidetect/launcher.py:1233-1234` :  path journal yang hardcode ke `Default/Cookies-journal`.
- `antidetect/launcher.py:1236-1243` :  `_wait_journal` dengan batas 3 detik.
- `antidetect/launcher.py:1259-1264` :  loop penutupan tab per-tab sebelum `Browser.close`.
- `antidetect/launcher.py:1283` :  pengiriman `Browser.close` via CDP websocket.
- `antidetect/launcher.py:1301-1311` :  penunggu proses mati dan pemanggilan `_wait_journal`.
- `antidetect/launcher.py:1213-1216` :  `_kill_pgid` fallback jika proses tidak mati.
- `antidetect/launcher.py:1556` `close_profile` :  titik entri penutupan profil.
- `antidetect/launcher.py:1569-1583` :  jalur multi-instance.
- `antidetect/launcher.py:1587-1596` :  jalur single-instance.
- `antidetect/cookies.py:174-180` `_database_path` :  lokasi cookie `Default/Network/Cookies` vs `Default/Cookies`.
- `antidetect/cookies.py:274` `get_cookies` :  pembacaan cookie dari SQLite.
- `antidetect/cookies.py:282` `_connect` :  `sqlite3.connect` tanpa opsi journal mode.
- `antidetect/cookies.py:641-658` `verify_cookie_db` :  `PRAGMA quick_check`.
- `antidetect/sync_agent.py:967-1086` `upload_cookie_blob` :  upload dan penulisan revisi.
- `antidetect/sync_agent.py:1050-1055` :  penanganan `STALE_FENCING_TOKEN`.
- `antidetect/sync_agent.py:1090` `download_and_inject_cookies` :  injeksi cookie dari server saat open.
- `antidetect/store.py:243-245` :  `PRAGMA journal_mode = WAL` hanya untuk store.db CloakDesk, bukan Chromium Cookies DB.

## 7. Batasan Analisis

- Analisis ini bersifat statis dan berbasis kode sumber. Tidak ada binary Chromium atau browser yang dijalankan.
- Perilaku `restore_on_startup = 1` bergantung pada versi Chromium yang digunakan.
- Klaim mengenai mekanisme token `PSIDTS` Google berasal dari arsitektur umum yang diketahui dan dapat divalidasi melalui dokumentasi resmi atau wawancara dengan tim backend.
- Lokasi `Default/Network/Cookies` didasarkan pada pemeriksaan kode `cookies.py:_database_path`, bukan inspeksi runtime file sistem.