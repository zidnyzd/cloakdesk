# Analisis Sebab Akar: Logout Gmail Multi-Akun Setelah Buka-Tutup Profil

Repo: cloakdesk
Branch: main, commit terakhir ac95ad2
Perbaikan sebelumnya: commit 87ce841 (preservasi sesi)
Tanggal analisis: 2026-11-03

## 1. Ringkasan Temuan

Pengguna melaporkan akun Gmail multi-login (10 akun dalam satu profil) masih sering ter-logout setelah buka tutup beberapa kali profil, meskipun perbaikan preservasi sesi di commit 87ce841 sudah diterapkan.

Analisis kode statis terhadap `antidetect/launcher.py`, `antidetect/cookies.py`, `antidetect/sync_agent.py`, `antidetect/api.py`, dan `antidetect/store.py` (tidak ada perubahan kode, tidak ada komit, tidak ada jalankan engine Chromium) menemukan lima masalah yang saling memperburuk:

| # | Masalah | Berkas dan baris | Tingkat dampak |
|---|---------|------------------|----------------|
| A | `restore_on_startup = 4` saja tidak memaksa Chromium menulis session cookie ke disk saat exit | launcher.py:881-922, launcher.py:928-950 | Tinggi |
| B | Penutupan via CDP `/json/close` per tab tidak memicu flush session cookie; jalur SIGTERM/SIGKILL via `_kill_pgid` bisa memotong penulisan SQLite | launcher.py:1225-1272, launcher.py:1196-1211, launcher.py:1516-1557 | Tinggi |
| C | `_insert_cookie` tidak menghapus baris lama dengan kombinasi kunci unik lengkap, menyebabkan duplikat cookie | cookies.py:358-406 | Tinggi |
| D | Guard anti-upload hanya memeriksa daftar kosong total; hilangnya GMAIL_AT dengan 139 cookie lainnya tetap dianggap aman untuk diunggah | launcher.py:1079-1151 | Tinggi |
| E | `download_and_inject_cookies` menimpa DB lokal dengan blob server melalui `update_cookies` yang rentan duplikat | sync_agent.py:1019-1112, cookies.py:409-444 | Sedang |

Bukti terukur dari mesin (dokumen tugas, jangan diulang pengukuran):
- Profil 328: `Default/Preferences` ADA dengan `session.restore_on_startup = 4`, `profile.exit_type = "Normal"`, mtime 05:53:19.
- Profil 328: `Default/Cookies` 139 baris, `GMAIL_AT = 0` baris (hilang semua).
- Profil 328: `Default/Cookies-journal` masih ada, ukuran 0 byte.
- Profil 328: log `chrome.log` puluhan baris `ERROR:net/cookies/cookie_monster.cc:1269] Found 1 duplicate cookies for key='google.com'` untuk SID, LSID, OSID, `__Host-1PLSID`, `__Host-3PLSID`, `__Secure-OSID`, AEC, OTZ, NID, SIDCC, `__Secure-STRP`, `__Secure-1PSIDTS`, `__Secure-3PSIDCC`, dan lainnya.
- Grup duplikat nyata di tabel `cookies`: `.google.com AEC /` x2, `.google.com __Secure-STRP /` x2, `accounts.google.com OTZ /` x2, `accounts.google.com __Host-GAPS /` x2.
- `top_frame_site_key` KOSONG/NULL pada 133 dari 139 baris.
- `has_cross_site_ancestor` terisi pada semua baris (tidak NULL).
- Cookie persistens biasa (SID, SAPISID, `__Secure-1PSID`, dll.) tetap `is_persistent = 1`, `has_expires = 1`, prefix `v10`. Enkripsi OSCrypt dan penulisan DB normal berfungsi.
- `antidetect.db` profil 328: `cookie_blob_id = NULL`, `cookie_revision = 0`, `cookie_sha256 = ''`. Tidak ada snapshot cookie tersimpan.
- `events` terakhir untuk 328 hanya `profile-open`, tidak ada `profile-close` tercatat.
- Profil 328 masih berjalan saat pemeriksaan (10 tab Gmail `/mail/u/0` s.d. `/mail/u/9`, 6 service worker `mail.google.com/mail/u/*/sw.js` aktif), isi cookie sesi hanya di memori.
- `build_args()` TIDAK menambahkan flag `--restore-last-session`.

## 2. Analisis Sebab Akar

### 2.1 Sebab di sisi Chromium

**2.1.1 `restore_on_startup = 4` tidak memaksa penulisan session cookie saat exit**

`session.restore_on_startup` adalah preferensi Chromium yang mengatur perilaku saat browser dibuka kembali. Nilai 4 berarti restore tab dari sesi terakhir. Preferensi ini hanya berlaku pada waktu startup; ia tidak mengubah perilaku `CookieMonster` / `SessionService` saat browser ditutup.

Session cookie (cookie dengan `has_expires = 0` dan `is_persistent = 0`, seperti `GMAIL_AT` pada path `/mail/u/0` s.d. `/mail/u/9`) biasanya ditulis ke `Cookies` database oleh Chromium hanya saat browser ditutup secara benar-bersih (exit_type Normal melalui jalur `SessionService::Save`). Jika proses Chromium berhenti sebelum flush selesai, session cookie hanya tinggal di memori dan hilang saat proses mati.

Referensi kode Chromium yang relevan (tidak tersedia secara lokal di mesin ini, hanya berdasarkan pengetahuan umum upstream Chromium):
- `net/cookies/cookie_monster.cc`, `CookieMonster::SetCookie`, `LoadCookiesFromDB`, penulisan session cookie saat shutdown.
- `net/cookies/cookie_monster.cc:1269`, pesan error duplikat cookie yang muncul di log profil 328.
- `chrome/browser/sessions/session_service.cc` / `session_service.h`, `SessionService::Save`, pemulihan session pada startup.
- `chrome/browser/prefs/session_prefs.cc` / `prefs.cc`, preferensi `session.restore_on_startup` dan `profile.exit_type`.
- `components/sessions/core/tab_restore_service.cc`, restore tab dan cookie sesi.

Kesimpulan: `restore_on_startup = 4` di `Default/Preferences` (launcher.py:881-922) adalah syarat necessary tetapi tidak cukup. Tidak ada jaminan session cookie ditulis ke disk saat exit jika proses Chromium tidak ditutup dengan cara yang memicu `SessionService::Save`.

**2.1.2 Perbedaan jalur penutupan browser**

- CDP `/json/close/{targetId}` (dipakai `_graceful_close` launcher.py:1257) memanggil `Target.close` untuk satu halaman/page. Ini menutup satu tab, bukan seluruh browser. Setelah semua tab ditutup, Chromium bisa mati tanpa memanggil `SessionService::Save` dan tanpa menulis session cookie ke disk.
- `Browser.close` via CDP browser target menutup seluruh browser secara bersih, lebih dekat dengan penutupan jendela native.
- `SIGTERM` / `SIGKILL` via `_kill_pgid` (launcher.py:1196-1211) memaksa proses berhenti tanpa cleanup SQLite.
- Penutupan jendela native (tombol X pada window manager) biasanya dianggap penutupan normal dan bisa memicu `exit_type = Normal` dengan flush session cookie.

Bukti di mesin: profil 328 punya `profile.exit_type = "Normal"` tapi `GMAIL_AT` hilang dan `Cookies-journal` 0 byte masih ada. Ini mengindikasikan browser dianggap ditutup normal tetapi SQLite `Cookies` tidak selesai ditulis (journal tertinggal, ukuran 0 byte).

### 2.2 Sebab di sisi CloakDesk

**2.2.1 `build_args()` tidak menambahkan flag `--restore-last-session`**

`build_args()` di launcher.py:928-950 hanya menambahkan `--no-first-run`, `--no-default-browser-check`, `--disable-features=Translate`, `--password-store=basic`. Tidak ada `--restore-last-session`. Meskipun pref `restore_on_startup = 4` sudah diinject via `ensure_password_manager` (launcher.py:881-922, dipanggil di build_args:936), tidak ada flag tambahan yang memaksa restore session di startup.

**2.2.2 `_graceful_close` dan jalur SIGKILL memotong penulisan cookie**

`_graceful_close` (launcher.py:1225-1272) menutup setiap page target via CDP `/json/close/{id}` (baris 1257), lalu menunggu proses mati dengan timeout default 10 detik (baris 1262). Jika proses belum mati setelah timeout, `close_profile` (launcher.py:1516-1557) memanggil `_kill_pgid(item_pid)` (baris 1537) yang mengirim SIGTERM lalu SIGKILL (launcher.py:1196-1211) setelah timeout 8 detik. SIGKILL memotong penulisan SQLite `Cookies` dan meninggalkan `Cookies-journal` 0 byte.

Pada profil dengan banyak tab (10 tab Gmail + 6 service worker), 10 detik mungkin tidak cukup untuk flush semua cookie sesi. Ini adalah jalur utama mengapa session cookie hanya tinggal di memori dan hilang setelah restart.

**2.2.3 `_insert_cookie` menghasilkan duplikat karena DELETE tidak mencakup semua kolom kunci unik**

`_insert_cookie` (cookies.py:358-406) melakukan:
```sql
DELETE FROM cookies WHERE host_key = ? AND path = ? AND name = ?
```
Tapi indeks unik Chromium modern adalah:
```sql
cookies_unique_index ON cookies(host_key, top_frame_site_key, has_cross_site_ancestor, name, path, source_scheme, source_port)
```
(`_create_schema` cookies.py:194-240, khususnya baris 231-236.)

`_insert_cookie` selalu mengisi `top_frame_site_key = ""` (baris 374) dan `has_cross_site_ancestor = 0` (baris 393), dan `source_scheme`/`source_port` bervariasi menurut secure flag (baris 368-369). Karena `DELETE` hanya cocokkan 3 kolom, baris lama dengan `source_scheme` berbeda atau kombinasi kunci lain tidak terhapus. `INSERT` baru menambah baris duplikat.

Bukti di mesin: grup duplikat `.google.com AEC /` x2, `.google.com __Secure-STRP /` x2, `accounts.google.com OTZ /` x2, `accounts.google.com __Host-GAPS /` x2. `top_frame_site_key` kosong pada 133/139 baris.

Duplikat cookie autentikasi Google (AEC, OTZ, `__Secure-STRP`, `__Host-GAPS`, dan varian SID/LSID/OSID) dapat mengakibatkan Google menganggap sesi tidak valid atau memicu tantangan verifikasi, sehingga pengguna terpaksa login ulang.

**2.2.4 `update_cookies` menimpa seluruh DB tanpa merge aman**

`update_cookies` (cookies.py:409-444) memanggil `_insert_cookie` untuk setiap item dan `conn.commit()`. Tidak ada logika merge yang mempertahankan cookie lokal yang masih valid jika data server tidak memilikinya. Saat sinkromasi mengunduh blob server yang telah kehilangan session cookie (karena upload terjadi setelah session cookie sudah hilang di disk), `download_and_inject_cookies` (sync_agent.py:1019-1112) menulis kembali data tersebut ke DB lokal melalui `update_cookies`, mempermanentkan hilangnya session cookie.

**2.2.5 Guard anti-upload tidak menangkap kehilangan session cookie parsial**

`_sync_cookies_after_close` (launcher.py:1079-1151) memiliki guard anti-upload (baris 1112-1119) yang hanya memeriksa `cookie_list == []` (daftar kosong total). Jika `GMAIL_AT` hilang tetapi 139 cookie persistens lain masih ada, daftar tidak kosong, guard tidak menyala, dan snapshot yang kehilangan session cookie Gmail diunggah ke server.

Bukti di mesin: `antidetect.db` profil 328 `cookie_blob_id = NULL`, `cookie_revision = 0`, `cookie_sha256 = ''`, tidak ada snapshot cookie tersimpan, tetapi ini bisa berubah pada sinkronisasi berikutnya jika data parsial diunggah.

**2.2.6 Tidak ada event `profile-close` tercatat**

`events` terakhir untuk profil 328 hanya `profile-open` (05:46:37 dan 05:51:42), tidak ada `profile-close`. Ini mengindikasikan siklus buka-tutup tidak selalu selesai dengan catatan penutupan yang benar, sejalan dengan proses yang dihentikan paksa sebelum flush selesai.

## 3. Tabel Bukti Pendukung

| Item | Nilai / Observasi | Sumber |
|---|---|---|
| `session.restore_on_startup` | 4 | launcher.py:907-910, Bukti mesin profil 328 |
| `profile.exit_type` | "Normal" | Bukti mesin profil 328 |
| Jumlah baris `Default/Cookies` profil 328 | 139 | Bukti mesin |
| Jumlah baris `GMAIL_AT` profil 328 | 0 | Bukti mesin |
| `Default/Cookies-journal` | Ada, 0 byte | Bukti mesin |
| Duplikat cookie | `.google.com AEC /` x2, `.google.com __Secure-STRP /` x2, `accounts.google.com OTZ /` x2, `accounts.google.com __Host-GAPS /` x2 | Bukti mesin |
| `top_frame_site_key` kosong/NULL | 133 dari 139 baris | Bukti mesin |
| `has_cross_site_ancestor` | Terisi semua baris | Bukti mesin |
| Cookie persistens SID/SAPISID/`__Secure-1PSID` | `is_persistent = 1`, `has_expires = 1`, prefix `v10` | Bukti mesin |
| `cookie_blob_id` profil 328 | NULL | Bukti mesin antidetect.db |
| `cookie_revision` profil 328 | 0 | Bukti mesin antidetect.db |
| `cookie_sha256` profil 328 | '' (kosong) | Bukti mesin antidetect.db |
| Event terakhir profil 328 | `profile-open` saja, tidak ada `profile-close` | Bukti mesin |
| Status proses profil 328 | Masih berjalan, 10 tab Gmail, 6 service worker | Bukti mesin |
| Flag `--restore-last-session` | Tidak ada di `build_args()` | launcher.py:928-950 |
| Timeout `_graceful_close` default | 10 detik | launcher.py:1262 |
| Timeout `_kill_pgid` sebelum SIGKILL | 8 detik | launcher.py:1196 |
| Guard upload kosong | Hanya `cookie_list == []` | launcher.py:1112-1119 |
| Indeks unik cookies | `(host_key, top_frame_site_key, has_cross_site_ancestor, name, path, source_scheme, source_port)` | cookies.py:231-235 |
| DELETE di `_insert_cookie` | Hanya `(host_key, path, name)` | cookies.py:398-400 |
| `top_frame_site_key` di insert | Selalu `""` | cookies.py:374 |
| `has_cross_site_ancestor` di insert | Selalu `0` | cookies.py:393 |

## 4. Rencana Perbaikan Bertahap

Semua rekomendasi di bawah ini hanya untuk dokumen perbaikan. Tidak ada perubahan kode yang dilakukan dalam tugas ini.

### Fase 1: Pastikan session cookie ditulis ke disk saat exit (dampak tertinggi)

**Masalah:** Chromium hanya menulis session cookie ke `Cookies` saat browser ditutup benar-bersih melalui jalur `SessionService::Save`. `restore_on_startup = 4` saja tidak cukup.

**Rekomendasi:**
1. Ganti penutupan per-tab via CDP `/json/close/{id}` dengan penutupan seluruh browser via CDP `Browser.close` (atau setidaknya coba `Browser.close` sebelum fallback ke per-tab), sehingga Chromium menganggap sesi berakhir secara normal dan memanggil `SessionService::Save`.
2. Perpanjang timeout flush di `_graceful_close` dari 10 detik menjadi lebih lama (mis. 20-30 detik) untuk profil dengan banyak tab, dan tunggu `Cookies-journal` hilang atau stabil sebelum mematikan proses.
3. Hindari SIGKILL melalui `_kill_pgid` kecuali sebagai last resort setelah SIGTERM gagal dalam waktu yang cukup lama. SIGKILL memotong penulisan SQLite dan menyebabkan `Cookies-journal` tertinggal.
4. Pertimbangkan menambahkan flag `--restore-last-session` di `build_args()` sebagai lapisan tambahan meskipun pref sudah diatur, untuk konsistensi perilaku restore di startup.

**Risiko:** Penutupan browser yang lebih lama bisa menunda `close_profile` pada profil dengan banyak tab. Mitigasi: batas waktu maksimal dengan fallback SIGTERM (bukan SIGKILL langsung) jika terlalu lama.

**Verifikasi:** Setelah tutup profil, pastikan tidak ada file `Cookies-journal` tertinggal di direktori profil, dan `profile.exit_type = "Normal"` tercatat di `Preferences`.

### Fase 2: Perbaikan skema dan penulisan cookie agar bebas duplikat

**Masalah:** `_insert_cookie` (cookies.py:358-406) melakukan `DELETE` hanya pada 3 kolom, tidak mencakup semua kolom indeks unik Chromium modern `(host_key, top_frame_site_key, has_cross_site_ancestor, name, path, source_scheme, source_port)`. Ini menyebabkan baris duplikat saat cookie ditulis ulang dengan kombinasi kunci berbeda.

**Rekomendasi:**
1. Ubah `DELETE` di `_insert_cookie` agar mencakup semua kolom kunci unik indeks: `host_key, top_frame_site_key, has_cross_site_ancestor, name, path, source_scheme, source_port`.
2. Atau, ganti pendekatan `DELETE` + `INSERT` dengan `INSERT OR REPLACE` yang menggunakan primary key / unique index yang lengkap, sehingga baris lama dengan kunci yang sama digantikan, tidak ditambahkan.
3. Pastikan `top_frame_site_key` dan `has_cross_site_ancestor` diisi dengan nilai yang konsisten dengan nilai yang dibaca dari Chromium, bukan selalu kosong / nol.

**Risiko:** Mengubah skema penulisan cookie bisa mempengaruhi kompatibilitas dengan database Cookie yang sudah ada. Mitigasi: uji pada profil isolasi, pastikan tidak ada barus orphan setelah migrasi.

**Verifikasi:** Setelah menjalankan `update_cookies` pada database uji, pastikan tidak ada duplikat `(host_key, name, path)` dan jumlah baris sesuai dengan input yang diberikan.

### Fase 3: Perbaikan guard sinkronisasi agar tidak mengunggah state yang kehilangan cookie autentikasi

**Masalah:** Guard di `_sync_cookies_after_close` (launcher.py:1112-1119) hanya memeriksa apakah daftar cookie kosong total. Hilangnya `GMAIL_AT` dengan 139 cookie lainnya tetap dianggap aman.

**Rekomendasi:**
1. Sebelum upload, hitung jumlah session cookie penting (domain google.com / accounts.google.com / mail.google.com, nama cookie autentikasi seperti GMAIL_AT, SID, LSID, OSID, `__Secure-1PSID`, `__Host-GAPS`, dll.) dalam database lokal.
2. Bandingkan dengan baseline yang diambil sebelum `_graceful_close` dijalankan. Jika penurunan drastis (mis. lebih dari 30% atau jumlah session cookie jadi 0 untuk domain Gmail), batalkan upload dan catat peringatan.
3. Atau, simpan digest cookie sebelum penutupan dan bandingkan setelah penutupan; jika digest berubah karena hilangnya session cookie, jangan unggah.

**Risiko:** Guard yang terlalu sensitif bisa mencegah upload cookie yang sah (mis. setelah logout sengaja). Mitigasi: gunakan ambang batas penurunan drastis, bukan nol mutlak, dan biarkan pengguna dapat memaksa upload melalui UI jika diperlukan.

**Verifikasi:** Simulasikan profil uji dengan session cookie Gmail, hapus beberapa session cookie, lalu jalankan sinkronisasi. Pastikan upload dicegah dan peringatan tercatat.

### Fase 4: Perbaikan `download_and_inject_cookies` dan `update_cookies` agar tidak menghapus session cookie lokal yang masih valid

**Masalah:** `download_and_inject_cookies` (sync_agent.py:1019-1112) menimpa DB lokal dengan blob server melalui `update_cookies` yang tidak punya logika merge aman. Jika server memiliki snapshot yang kehilangan session cookie, data lokal juga akan kehilangannya.

**Rekomendasi:**
1. Tambahkan parameter `safe_merge` ke `update_cookies()` yang, saat aktif, mempertahankan cookie lokal yang masih valid dan belum expired jika data server tidak memilikinya.
2. Jika `safe_merge` aktif, session cookie (`is_persistent = 0` dan `has_expires = 0`) tidak dihapus kecuali ada cookie baru dari server dengan identitas yang sama dan belum expired.
3. Di `download_and_inject_cookies`, gunakan `safe_merge = True` default untuk cookie blob agar sinkronisasi tidak secara tidak sengaja menghapus session cookie lokal.

**Risiko:** Merge aman bisa menyebabkan cookie yang sudah expired di server tetap ada di lokal. Mitigasi: tetap hapus cookie yang expired berdasarkan `expires_utc`.

**Verifikasi:** Siapkan database lokal dengan session cookie Gmail valid, jalankan `update_cookies` dengan data server yang tidak mengandung session cookie tersebut, pastikan session cookie lokal tetap ada setelah merge.

## 5. Prioritas dan Estimasi Dampak

| Prioritas | Aksi | Dampak diharapkan |
|---|---|---|
| 1 | Ganti penutupan per-tab menjadi `Browser.close` / penutuhan browser bersih, perpanjang timeout flush, hindari SIGKILL | Session cookie ditulis ke disk saat exit, mengurangi logout drastis |
| 2 | Perbaiki `DELETE` di `_insert_cookie` agar mencakup semua kolom kunci unik | Menghilangkan duplikat cookie autentikasi Google |
| 3 | Perbaiki guard sinkronisasi agar tidak mengunggah state yang kehilangan session cookie | Mencegah snapshot rusak menyebar ke server |
| 4 | Tambahkan `safe_merge` ke `update_cookies` dan gunakan di `download_and_inject_cookies` | Mencegah sinkromasi menghapus session cookie lokal yang masih valid |

## 6. Catatan

- Tidak ada perubahan kode yang dilakukan dalam tugas ini. Hanya analisis dan dokumentasi.
- Tidak ada komit dan tidak ada push.
- Tidak ada engine Chromium yang dijalankan.
- Tidak ada nilai cookie, token, sandi, atau isi blob yang dicetak. Hanya nama cookie, domain, path, jumlah, tipe, dan hash disebutkan.
- Referensi kode Chromium (cookie_monster.cc, session_service.cc, dll.) berdasarkan pengetahuan umum upstream Chromium dan tidak dapat diverifikasi secara lokal karena hanya binary engine yang tersedia, tidak ada sumber kode Chromium di mesin ini.
