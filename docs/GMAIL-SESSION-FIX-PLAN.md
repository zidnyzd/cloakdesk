# Rencana Perbaikan Preservasi Sesi Akun Google (Multi-Login Gmail) di CloakDesk

## 1. Ringkasan Masalah & Dampak

### Gejala
- Pengguna yang login ke banyak akun Gmail dalam satu profil CloakDesk (hingga 10 akun) mengalami sign-out otomatis seluruh akun setelah browser ditutup dan dibuka kembali.
- Cookie sesi Gmail multi-akun (`GMAIL_AT` pada path `/mail/u/0` sampai `/mail/u/9` dan `__Host-GMAIL_SCH` pada path `/`) hilang secara tiba-tiba tanpa interaksi sign-out eksplisit dari pengguna.

### Akar Masalah
1. **Preferensi startup tidak dijamin**: `session.restore_on_startup` tidak disetel ke `4` di `Default/Preferences`, sehingga Chromium menghapus seluruh session cookie saat browser ditutup normal.
2. **Penutupan tidak bersih**: Timeout 10 detik di `_graceful_close` memaksa pematian proses via `_kill_pgid` (SIGTERM/SIGKILL), memotong penulisan SQLite dan menyisakan file `Cookies-journal`.
3. **Sinkronisasi menghapus data sesi**: `_sync_cookies_after_close` mengunggah kumpulan cookie yang sudah kehilangan session cookie, lalu `download_and_inject_cookies` menimpa database lokal dengan versi server yang kosong.
4. **Enkripsi cookie tidak kompatibel**: Skema AES-CBC manual di `cookies.py` tidak cocok dengan OSCrypt asli Chromium Linux, membuat cookie hasil suntikan berisiko tidak terbaca oleh browser.

### Metrik Keberhasilan
- Persentase session cookie Gmail yang bertahan lintas siklus buka-tutup browser: >= 95%.
- Jumlah instance `Cookies-journal` yang tertinggal setelah penutupan: 0.
- Jumlah fals positif guard sinkronisasi yang memblokir update cookie non-sesi: 0.

---

## 2. Tahapan Perbaikan Bertahap

### Fase 1: Preservasi Session Cookie di Tingkat Mesin Browser

**Lokasi berkas yang diubah**: `antidetect/launcher.py`

**Fungsi yang diubah**: `ensure_password_manager` (atau direfaktor menjadi `ensure_profile_preferences`)

**Mekanisme**
- Setelah profil dibuat atau dibuka, sisipkan preferensi berikut ke `Default/Preferences`:
  ```json
  {
    "session": {
      "restore_on_startup": 4
    }
  }
  ```
- Preferensi ini memaksa Chromium memulihkan session cookie dari disk saat browser dijalankan kembali.

**Aturan Idempotensi**
- Jangan menimpa nilai yang sudah disetel pengguna secara manual.
- Implementasi: baca `Preferences` terlebih dahulu, periksa apakah kunci `session.restore_on_startup` sudah ada dan bernilai non-default. Jika sudah, lewati penulisan.
- Catat perubahan di log dengan level INFO agar mudah di-audit.

**Verifikasi Intermediet**
- Setelah launch profil, baca `Default/Preferences` dan pastikan nilai `restore_on_startup` tepat `4`.

---

### Fase 2: Siklus Penutupan Bersih (Clean Graceful Close & Cookie Flush)

**Lokasi berkas yang diubah**: `antidetect/launcher.py`

**Fungsi yang diubah**: `_graceful_close()`, `close_profile()`

**Mekanisme**
1. **Tutup page secara berurutan**: Kirim perintah `Browser.close` melalui DevTools target browser untuk setiap page sebelum mematikan proses utama. Ini memberi kesempatan Chromium menyelesaikan flush SQLite.
2. **Tunggu selesai flush**: Setelah semua page ditutup, tunggu hingga file `Cookies-journal` tidak ada atau berhenti berubah selama 2 detik.
3. **Hentikan proses setelah flush**: Matikan proses Chromium hanya setelah kondisi flush terpenuhi.
4. **Perpanjang batas toleransi**: Tingkatkan timeout toleransi flush dari 10 detik menjadi 20 detik sebagai buffer aman, terutama pada I/O yang lambat.

**Alur yang Diharapkan**
- `close_profile()` dipanggil oleh pengguna.
- Iterasi page, kirim `Browser.close` via DevTools.
- Tunggu `Cookies-journal` hilang atau stabil.
- Kirim SIGTERM ke proses jika masih hidup setelah timeout.
- Hapus sisa proses jika SIGTERM tidak mematikan dalam 3 detik.

**Verifikasi Intermediet**
- Setelah tutup profil, cek direktori profil: tidak ada file `Cookies-journal` yang tertinggal.
- Cek dengan `pgrep chrome` atau PID yang dikelola: proses harus sudah hilang sebelum fungsi mengembalikan kontrol.

---

### Fase 3: Pengaman Sinkronisasi Cookie (Anti-Regression Sync Guard & Safe Merge)

**Lokasi berkas yang diubah**: `antidetect/launcher.py`, `antidetect/sync_agent.py`, `antidetect/cookies.py`

#### 3.1 Guard Sinkronisasi (Anti-Regression)
**Lokasi**: `antidetect/sync_agent.py`, `antidetect/launcher.py`

**Mekanisme**
- Sebelum `_sync_cookies_after_close` mengunggah cookie ke server, hitung jumlah session cookie penting yang terdeteksi dalam database lokal.
- Jika jumlah session cookie turun drastis dibandingkan baseline sebelum penutupan (misalnya turun lebih dari 30% atau jumlahnya 0 untuk domain Gmail), batalkan upload dan log peringatan.
- Baseline diambil sebelum eksekusi `_graceful_close` dijalankan.

**Implementasi Konseptual**
```python
# Pseudocode konseptual, bukan patch
before_count = count_session_cookies(profile_db)
run_graceful_close()
after_count = count_session_cookies(profile_db)
if after_count < before_count * 0.7:
    logger.warning("Session cookie hilang drastis, lewati upload sinkronisasi")
    return
sync_cookies_to_server()
```

#### 3.2 Strategi Merge Non-Destruktif (Safe Merge)
**Lokasi**: `antidetect/cookies.py`, fungsi `update_cookies()`

**Mekanisme**
- Ganti logika penimpaan penuh dengan merge berbasis prioritas:
  - Cookie lokal yang masih ada dan belum expired diprioritaskan.
  - Cookie dari server ditambahkan jika domain dan path cocok, tetapi tidak menghapus cookie lokal yang masih valid.
  - Session cookie (`is_persistent = 0` dan `has_expires = 0`) tidak dihapus kecuali ada cookie baru dari server dengan identitas yang sama dan belum expired.
- Tambahkan flag `safe_merge = True` ke parameter `update_cookies()` agar perubahan ini bisa diuji secara terpisah.

**Verifikasi Intermediet**
- Jalankan skrip sinkronisasi pada profil uji dengan session cookie Gmail yang sudah ada.
- Pastikan jumlah session cookie setelah merge >= jumlah session cookie sebelum merge, atau setidaknya tidak berkurang drastis tanpa alasan.

---

### Fase 4: Rencana Pengujian Otomatis & Verifikasi

**Skrip pengujian utama**: `scripts/test-session-preservation.py`

**Cakupan Pengujian**

1. **Verifikasi penulisan Preferences**
   - Siapkan profil uji baru.
   - Panggil fungsi pengaturan preferensi yang relevan.
   - Baca `Default/Preferences` dan pastikan `session.restore_on_startup` bernilai `4`.
   - Ulangi pada profil yang sudah ada dengan preferensi berbeda untuk menguji idempotensi.

2. **Pengujian persistensi session cookie lintas siklus open-close tiruan**
   - Inject cookie sesi Gmail tiruan (persistent = 0, has_expires = 0) ke database profil uji.
   - Jalankan siklus buka-tutup profil menggunakan mekanisme yang sama dengan produksi.
   - Setelah buka kembali, verifikasi cookie sesi Gmail masih ada di database.

3. **Pengujian guard sinkronisasi saat cookie berkurang**
   - Simulasikan penurunan session cookie sebelum penutupan.
   - Jalankan sinkronisasi dan pastikan upload dicegah oleh guard.
   - Verifikasi log peringatan tercatat.

4. **Pengujian bebas file `Cookies-journal` menggantung**
   - Tutup profil dengan cara normal.
   - Tunggu hingga timeout yang ditentukan habis.
   - Periksa direktori profil: tidak ada file `Cookies-journal` yang tertinggal.

5. **Pengujian merge non-destruktif**
   - Siapkan database lokal dengan cookie sesi Gmail valid.
   - Jalankan `update_cookies()` dengan data server yang tidak mengandung cookie sesi tersebut.
   - Pastikan cookie sesi lokal tetap ada setelah merge.

**Siklus Pengujian**
- Semua tes dijalankan pada profil uji terisolasi, bukan profil pengguna nyata.
- Setiap tes harus membersihkan artefak yang dibuat setelah selesai.
- Jalankan seluruh skrip dengan satu perintah: `python scripts/test-session-preservation.py`.
- Hasil keluar harus berupa ringkasan PASS/FAIL per kasus dan jumlah error ringkas.

---

## 3. Analisis Risiko & Mitigasi

### Potensi Efek Samping pada Profil Non-Gmail
- **Risiko**: Fitur `restore_on_startup = 4` memulihkan seluruh session cookie, bukan hanya Gmail. Bisa memunculkan sesi login lain yang pengguna tidak inginkan.
- **Mitigasi**: Preferensi ini sudah menjadi standar Chromium untuk mode "continue where you left off". Efeknya terbatas pada session cookie yang memang masih ada di disk. Tambahkan catatan di dokumentasi pengguna bahwa perubahan ini dapat memulihkan sesi login situs lain yang pernah dibuka.

### Konsumsi Disk/Memori jika Session Tab Dipulihkan
- **Risiko**: Chromium bisa memulihkan seluruh tab terbuka saat crash atau penutupan tidak normal, meningkatkan konsumsi RAM dan disk.
- **Mitigasi**: Preferensi hanya berlaku untuk session cookie, tidak memaksa restore tab kecuali ada page-state tersimpan. Pastikan `session.exit_type` tetap diatur ke `Normal` ketika penutupan berhasil. Batasi jumlah page yang direstore dengan kebijakan lain jika diperlukan.

### Kompatibilitas Multi-Perangkat
- **Risiko**: Session cookie Gmail bisa terikat dengan perangkat atau IP tertentu. Pemulihan cookie di perangkat lain bisa memicu tantangan verifikasi keamanan Google.
- **Mitigasi**: Rencana ini hanya menyentuh preservasi cookie di perangkat yang sama. Tindakan sinkronisasi antar perangkat tetap mengikuti mekanisme yang ada, dengan guard tambahan untuk mencegah pengiriman session cookie yang sudah expired.

### Kompatibilitas Versi Chromium
- **Risiko**: Preferensi `restore_on_startup` atau perilaku `Cookies-journal` bisa berubah antar versi Chromium.
- **Mitigasi**: Batasiuji pada versi Chromium yang didukung (fingerprint-chromium 150). Jika ada pergeseran nama preferensi atau format database, tambahkan deteksi versi di `launcher.py`.

---

## 4. Kriteria Keberhasilan (Acceptance Criteria)

1. `Default/Preferences` pada setiap profil berisi `"restore_on_startup": 4` di bagian `session`.
2. Setelah penutupan normal profil, file `Cookies-journal` tidak ada di direktori profil.
3. Session cookie Gmail (`GMAIL_AT` dan `__Host-GMAIL_SCH`) bertahan di database lokal setelah siklus buka-tutup tiruan.
4. Guard sinkronisasi aktif mencegah upload cookie saat terdeteksi penurunan drastis session cookie.
5. `update_cookies()` tidak lagi menghapus session cookie lokal yang masih valid hanya karena data server tidak memilikinya.
6. Semua kasus uji di `scripts/test-session-preservation.py` lulus tanpa kegagalan.
7. Tidak ada regresi pada fitur sinkronisasi cookie untuk situs non-Gmail.
8. Perubahan hanya terjadi di dokumen perencanaan dan kode terkait yang disebutkan; tidak ada modifikasi engine Chromium, venv, atau berkas di luar cakupan.
