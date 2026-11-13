# Rencana Arsitektur Kernel Manager CloakDesk

## 1. Ringkasan Eksekutif dan Latar Belakang

### 1.1 Tujuan Arsitektur
Dokumen ini mendefinisikan spesifikasi teknis dan rencana implementasi fitur **Kernel Manager** pada CloakDesk (`antidetect`). Tujuan utama fitur ini adalah:
1. **Dukungan Siklus Rilis Chromium Baru**: Memungkinkan pengguna memasang dan memperbarui versi engine `fingerprint-chromium` (misalnya dari Chromium 150 ke Chromium 151 atau 152) tanpa harus membangun ulang paket `.deb` utama atau mengganti isi direktori engine bawaan di `~/.local/share/antidetect/squashfs-root` (`antidetect/launcher.py:21-24`).
2. **Eksekusi Multi-Kernel Berdampingan**: Memungkinkan profil browser yang berbeda berjalan secara bersamaan menggunakan versi biner kernel yang berbeda sesuai kebutuhan kompatibilitas situs target atau konsistensi sidik jari (*fingerprint*) akun lama.
3. **Manajemen Siklus Hidup Kernel Terpadu**: Menyediakan antarmuka grafis di halaman Pengaturan (`ui/src/views/Settings.vue`), pilihan kernel di dialog profil (`ui/src/components/ProfileDialog.vue`), kontrak API lokal (`/api/v2/kernel-*`), serta skrip instalasi CLI (`scripts/install-kernel.sh`) dengan validasi keamanan penuh.
4. **Penyelarasan Otomatis Fingerprint dengan Versi Kernel**: Memastikan profil yang memilih kernel tertentu secara konsisten menyajikan versi mayor browser yang selaras pada argumen `--fingerprint-brand-version`, header `User-Agent`, dan Client Hints (`Sec-CH-UA`), tanpa merusak kompatibilitas SDK `ixbrowser-local-api`.

### 1.2 Referensi Paritas ixBrowser dan Batasan Sistem Linux
Di ekosistem ixBrowser pada Windows, aplikasi menyediakan beberapa versi kernel Chromium yang dapat diunduh dan dipilih per profil. Ketika sebuah profil dikaitkan dengan kernel tertentu, ixBrowser mengunci versi mayor Chromium pada konfigurasi profil (`br_version`) agar nilai `navigator.userAgent` dan `navigator.userAgentData` tidak bertabrakan dengan perilaku internal mesin blink/v8 yang sedang berjalan.

Pada lingkungan Linux tempat CloakDesk beroperasi, terdapat batasan teknis dan aturan keselamatan sistem yang harus dipatuhi oleh arsitektur Kernel Manager:
1. **Imutabilitas Engine Bawaan**: Direktori engine bawaan di `~/.local/share/antidetect/squashfs-root/opt/ungoogled-chromium` (`antidetect/launcher.py:21-26`) bersifat baca-saja (*read-only* secara operasional) dan berfungsi sebagai jangkar *fallback* yang selalu tersedia apabila kernel kustom dihapus atau gagal dimuat.
2. **Isolasi Direktori Kernel Tambahan**: Seluruh kernel tambahan wajib ditempatkan di dalam subdirektori `~/.local/share/antidetect/kernels/<kernel_id>/` (`antidetect/launcher.py:27`). Launcher menolak mengeksekusi biner apa pun di luar direktori tersebut atau biner bawaan (`antidetect/launcher.py:129-134`, `antidetect/launcher.py:194-202`).
3. **Kewajiban Melewati Engine Guard Wrapper**: Seluruh peluncuran proses browser Chromium wajib melewati `~/.local/bin/antidetect-engine-wrapper.py` (`antidetect/launcher.py:33`, `antidetect/launcher.py:781-795`) yang membatasi jumlah instance hidup maksimal 6 jendela (`~/.local/bin/antidetect-engine-wrapper.py:32`), laju peluncuran maksimal 20 kali per menit (`~/.local/bin/antidetect-engine-wrapper.py:33`), dan lokasi `--user-data-dir` (`~/.local/bin/antidetect-engine-wrapper.py:28-31`).
4. **Ketergantungan Pustaka Dinamis Linux (ELF x86_64)**: Berbeda dengan paket bundel mandiri pada sistem operasi lain, biner `chrome` di Linux memerlukan struktur berkas pendamping (`chrome_100_percent.pak`, `chrome_200_percent.pak`, `resources.pak`, `icudtl.dat`, `v8_context_snapshot.bin`, `locales/`, `libEGL.so`, `libGLESv2.so`, dan `chromedriver`) di dalam satu direktori kerja yang utuh.

---

## 2. Analisis Fondasi Kode Saat Ini

Fondasi dasar pembacaan multi-kernel sebagian telah tersedia di kode sumber saat ini, namun belum memiliki mekanisme instalasi, penghapusan, pemilihan kernel bawaan (*default*), maupun antarmuka manajemen di UI. Berikut adalah pemetaan mendetail atas kode yang sudah berjalan.

### 2.1 Lapisan Peluncur dan Deteksi Kernel (`antidetect/launcher.py`)
1. **Konstanta Lokasi Biner dan Direktori Kernel**:
   - `ENGINE_DIR` didefinisikan dari variabel lingkungan `ANTIDETECT_ENGINE_DIR` atau fallback ke `DATA_DIR / "squashfs-root" / "opt" / "ungoogled-chromium"` (`antidetect/launcher.py:21-24`), dengan `DATA_DIR` mengarah ke `~/.local/share/antidetect` (`antidetect/paths.py:5-7`).
   - `CHROME = ENGINE_DIR / "chrome"` dan `CHROMEDRIVER = ENGINE_DIR / "chromedriver"` menunjuk ke biner utama (`antidetect/launcher.py:25-26`).
   - `KERNELS_DIR = DATA_DIR / "kernels"` dan `BUILTIN_KERNEL_ID = "builtin"` menetapkan ruang nama untuk kernel tambahan (`antidetect/launcher.py:27-28`).
2. **Deteksi Dukungan Flag per Biner (`flag_supported` dan `kernel_support`)**:
   - `flag_supported(name, binary=None)` membuka biner target (`binary or CHROME`) dalam mode baca biner dan melakukan pemindaian memori lewat `mmap.mmap` untuk mencari string nama flag, lalu menyimpan hasilnya di dictionary `_flag_cache` dengan kunci `(str(path), name)` (`antidetect/launcher.py:79-97`).
   - `kernel_support(binary=None)` memetakan keempat kunci `GPU_FLAG_NAMES` (`gpu_vendor`, `gpu_renderer`, `disable_gpu_fingerprint`, `disable_spoofing` pada `antidetect/launcher.py:35-40`) melalui `flag_supported(name, binary)` dan menyertakan daftar `disable_spoofing_effective` (`antidetect/launcher.py:100-103`). Hal ini memungkinkan CloakDesk mengetahui secara dinamis apakah suatu biner kernel (misalnya Chromium 138 lama vs Chromium 150/152 baru) mendukung flag GPU tertentu (`antidetect/launcher.py:577-604`) atau flag `--deny-permission-prompts` (`antidetect/launcher.py:483-486`).
3. **Pembacaan Versi dan Daftar Kernel (`kernel_version` dan `available_kernels`)**:
   - `_read_kernel_version(binary)` mengeksekusi `[str(binary), "--version"]` dengan batas waktu 15 detik dan mengambil baris teks non-kosong pertama dari `stdout` atau `stderr` (`antidetect/launcher.py:137-149`).
   - `kernel_version(binary)` membungkus pembacaan tersebut dengan kunci `threading.Lock()` (`_kernel_versions_lock`) dan menyimpannya ke cache memori `_kernel_versions` (`antidetect/launcher.py:121-122`, `antidetect/launcher.py:152-158`).
   - `_executable(path)` memeriksa `path.is_file() and os.access(path, os.X_OK)` (`antidetect/launcher.py:125-126`).
   - `_inside_kernels(path)` memvalidasi bahwa jalur hasil resolusi symlink (`Path(path).resolve()`) berada di dalam `KERNELS_DIR.resolve()` menggunakan `relative_to()` (`antidetect/launcher.py:129-134`).
   - `available_kernels()` selalu menempatkan kernel bawaan sebagai elemen pertama (`id: "builtin"`, `name: "Engine Bawaan"`, `active: True`, `builtin: True` pada `antidetect/launcher.py:161-172`), kemudian mengiterasi subdirektori terurut di `KERNELS_DIR` (`antidetect/launcher.py:173-177`). Untuk setiap subdirektori `entry`, fungsi mengecek keberadaan `binary = entry / "chrome"` dan melewati entri apabila `entry.name == BUILTIN_KERNEL_ID`, bukan berkas executable, atau keluar dari `KERNELS_DIR` (`antidetect/launcher.py:178-180`). Entri yang valid ditambahkan dengan `active: False` dan `builtin: False` (`antidetect/launcher.py:181-191`).
   - `max_kernel_version()` mengekstrak token angka pertama dari string `version` dan `name` pada setiap item `available_kernels()` untuk melaporkan nomor versi mayor tertinggi (`antidetect/launcher.py:106-118`).
4. **Resolusi Biner Saat Peluncuran Profil (`kernel_for` dan `build_args`)**:
   - `kernel_for(profile)` mengambil string `preference_config.kernel_path` dari objek profil (`antidetect/launcher.py:194-196`). Jika nilai kosong, di luar `KERNELS_DIR`, atau tidak executable, fungsi langsung mengembalikan `CHROME` bawaan (`antidetect/launcher.py:197-202`).
   - `build_args(profile, port, app_args=None, proxy_arg=None, data_dir=None)` memanggil `binary = kernel_for(profile)` (`antidetect/launcher.py:722`), menyusun flag dasar Chromium (`antidetect/launcher.py:729-758`), meneruskan `binary` ke `_gpu_args(fp, binary)` (`antidetect/launcher.py:760`) dan `_preference_args(preferences, binary)` (`antidetect/launcher.py:763`), lalu meneruskan `args` dan `binary` ke `_wrap_engine_args(args, binary)` (`antidetect/launcher.py:778`).

### 2.2 Kontrak API dan Validasi Saat Ini (`antidetect/api.py`)
1. **Action `engine-info`**:
   - Fungsi `engine_info(params)` (`antidetect/api.py:92-106`) memanggil `launcher.available_kernels()`, memperkaya setiap objek kernel dengan properti `"supports": launcher.kernel_support(kernel["path"])` (`antidetect/api.py:94-97`), dan mengembalikan informasi engine keseluruhan beserta `max_kernel_version` (`antidetect/api.py:98-106`).
2. **Validasi `_check_kernel_path`**:
   - Fungsi `_check_kernel_path(data)` (`antidetect/api.py:109-123`) memeriksa `data["preference_config"]["kernel_path"]`. Apabila diisi, nilainya di-resolve dengan `Path(str(raw)).resolve()` dan dibandingkan terhadap himpunan seluruh `kernel["path"]` dari `launcher.available_kernels()` (`antidetect/api.py:117-121`). Jika tidak ditemukan, API menolak permintaan dengan `ApiError(1000, "kernel tidak tersedia")` (`antidetect/api.py:122-123`).
   - Fungsi `_check_kernel_path` dipanggil di tiga titik mutasi profil:
     - `profile_create` (`antidetect/api.py:416-420`)
     - `profile_update` (`antidetect/api.py:432-440`)
     - `template_create_profile` (`antidetect/api.py:1513-1523`)
3. **Kesenjangan pada Lapisan API Saat Ini**:
   - Belum ada endpoint khusus untuk mengelola siklus hidup kernel (`kernel-list`, `kernel-install`, `kernel-delete`, `kernel-set-default`).
   - `available_kernels()` belum menghitung berapa banyak profil aktif yang sedang menggunakan masing-masing kernel (`profile_count`), ukuran disk (`disk_size_mb`), maupun waktu instalasi (`created_at`), serta nilai `active` masih dikodekan keras (`True` untuk builtin di `antidetect/launcher.py:169` dan `False` untuk kernel tambahan di `antidetect/launcher.py:187`).

### 2.3 Komponen Dialog Profil (`ui/src/components/ProfileDialog.vue`)
1. **Manajemen State Kernel di Form Profil**:
   - Komponen mendeklarasikan ref `kernels` (`ui/src/components/ProfileDialog.vue:126`), `kernelPath` (`ui/src/components/ProfileDialog.vue:127`), `kernelError` (`ui/src/components/ProfileDialog.vue:128`), dan `kernelSavedMissing` (`ui/src/components/ProfileDialog.vue:129`).
   - Computed `builtinKernelPath` mencari objek kernel yang memiliki `kernel.builtin`, atau `kernel.active`, atau elemen pertama `list[0]`, lalu mengembalikan `path`-nya (`ui/src/components/ProfileDialog.vue:201-205`).
   - Saat dialog dibuka (`watch(() => props.modelValue)` di `ui/src/components/ProfileDialog.vue:362-372`), `resetForm()` menyalin `preference.kernel_path` lewat `fillFromProfile(props.profile)` (`ui/src/components/ProfileDialog.vue:272`), kemudian `loadKernels()` memanggil `api.engineInfo()` (`ui/src/components/ProfileDialog.vue:350-360`, `ui/src/api.js:72`) dan menjalankan `syncKernelPath()` (`ui/src/components/ProfileDialog.vue:299-305`).
   - Apabila profil menyimpan `kernel_path` yang sudah tidak ada di daftar `kernels`, `syncKernelPath()` menyetel `kernelSavedMissing.value = true` dan mengembalikan `kernelPath.value` ke `builtinKernelPath.value` (`ui/src/components/ProfileDialog.vue:303-304`).
   - Saat disimpan, `preferencePayload()` hanya menulis `payload.kernel_path = kernelPath.value` jika nilainya berbeda dari `builtinKernelPath.value`; jika sama dengan builtin, properti `kernel_path` dihapus dari objek preferensi (`ui/src/components/ProfileDialog.vue:457-459`).
2. **Kondisi Render `kernels.length > 1` di Template**:
   - Pada tab Preferensi (`ui/src/components/ProfileDialog.vue:931-957`), dropdown `<el-select v-if="kernels.length > 1" v-model="kernelPath">` hanya dirender apabila terdapat lebih dari satu kernel terpasang (`ui/src/components/ProfileDialog.vue:934-946`), dengan label opsi dari `kernelLabel(kernel)` yang menghasilkan format `` `${kernel.name} · ${kernel.version || 'versi tidak terbaca'}` `` (`ui/src/components/ProfileDialog.vue:295-297`).
   - Jika hanya ada satu kernel (`v-else-if="kernels.length === 1"` pada `ui/src/components/ProfileDialog.vue:947-950`), UI menyembunyikan dropdown dan hanya menampilkan teks statis versi kernel tunggal beserta keterangan `"Hanya satu kernel terpasang."`.

### 2.4 Halaman Pengaturan (`ui/src/views/Settings.vue`)
1. **Pemuatan Data Engine**:
   - Halaman Pengaturan mendeklarasikan `engine = ref(null)` dan `engineError = ref('')` (`ui/src/views/Settings.vue:28-29`), yang diisi oleh fungsi `loadEngine()` melalui pemanggilan `await api.engineInfo()` (`ui/src/views/Settings.vue:72-80`).
2. **Tampilan Kartu Engine (`engine.kernels`)**:
   - Pada template bagian `<section class="info-card engine-card">` (`ui/src/views/Settings.vue:477-498`), UI hanya menampilkan informasi pasif berupa path direktori engine (`ui/src/views/Settings.vue:479`), versi utama dan jumlah `engine.kernels.length` (`ui/src/views/Settings.vue:481-483`), serta elemen daftar `<ul v-if="engine" class="kernel-list">` (`ui/src/views/Settings.vue:487-496`).
   - Setiap elemen `<li v-for="kernel in engine.kernels">` hanya menampilkan `kernel.name || kernel.id`, `kernel.version`, badge `Aktif` jika `kernel.active` bernilai benar, dan `kernel.path` (`ui/src/views/Settings.vue:488-495`), diikuti teks `"Hanya satu kernel terpasang."` bila `engine.kernels.length === 1` (`ui/src/views/Settings.vue:497`).
   - Belum ada tombol aksi untuk memasang kernel baru, menetapkan kernel default, maupun menghapus kernel tambahan.

---

## 3. Spesifikasi Kontrak API Baru

Seluruh action baru didaftarkan menggunakan dekorator `@action("<nama>")` di `antidetect/api.py` (`antidetect/api.py:41-45`) dan mematuhi amplop respons standar ixBrowser v2:
```json
{
  "error": { "code": 0, "message": "success" },
  "data": { ... }
}
```
Logika bisnis manajemen kernel akan diisolasi ke dalam modul baru `antidetect/kernels.py` yang bekerja sama dengan `antidetect/launcher.py` dan `antidetect/store.py`.

### 3.1 Penyimpanan Metadata dan Konfigurasi Kernel Default
1. **Struktur Direktori Disk**:
   - Kernel bawaan tetap berada di `launcher.ENGINE_DIR` (`antidetect/launcher.py:21-24`).
   - Setiap kernel tambahan disimpan di `~/.local/share/antidetect/kernels/<kernel_id>/` (`antidetect/launcher.py:27`), lengkap dengan berkas metadata lokal `~/.local/share/antidetect/kernels/<kernel_id>/kernel.json` yang mencatat `id`, `name`, `version`, `major_version`, `disk_size_mb`, dan `created_at`.
2. **State Kernel Default**:
   - Pilihan kernel bawaan (*default kernel*) untuk profil baru disimpan pada tabel `sync_state` di SQLite lokal melalui helper `store.get_sync_state(conn, "default_kernel_id", "builtin")` dan `store.set_sync_state(conn, "default_kernel_id", kernel_id)` (`antidetect/store.py:241-250`, `antidetect/store.py:336-341`), atau pada berkas `~/.local/share/antidetect/kernels/default.json` agar dapat dibaca cepat oleh `launcher.available_kernels()` tanpa membuka transaksi tulis database.
   - Properti `active` (dan alias `is_default`) pada daftar kernel mencerminkan kernel mana yang saat ini menjadi pilihan bawaan untuk pembuatan profil baru. Jika kernel default yang tersimpan sudah dihapus atau tidak valid, sistem otomatis kembali ke `"builtin"` (`antidetect/launcher.py:28`).

### 3.2 Action `kernel-list`
Mengembalikan daftar seluruh kernel yang terpasang (baik kernel builtin maupun seluruh kernel tambahan di `KERNELS_DIR`) dalam format terpaginasi atau daftar lengkap yang selaras dengan `engine-info` (`antidetect/api.py:92-106`).

- **Endpoint**: `POST /api/v2/kernel-list`
- **Parameter Input**: `{}` (tidak memerlukan parameter wajib).
- **Perhitungan `profile_count`**:
  - Melakukan kueri tunggal ke tabel `profiles` (`WHERE deleted_at IS NULL`) dengan mengambil kolom `preference_config` (`antidetect/store.py:971`).
  - Untuk setiap profil aktif, membaca `preference_config.kernel_path` (`antidetect/launcher.py:196`). Jika `kernel_path` cocok dengan `path` suatu kernel tambahan yang terpasang, tambahkan hitungan pada kernel tersebut. Jika `kernel_path` kosong atau mengarah ke biner bawaan, tambahkan hitungan pada kernel `builtin`.
- **Perhitungan `disk_size_mb`**:
  - Menghitung total ukuran berkas di dalam direktori kernel (`ENGINE_DIR` untuk builtin, `KERNELS_DIR / <id>` untuk kernel tambahan) dalam satuan megabita (dibulatkan ke 1 desimal, misalnya `412.5`), lalu menyimpannya di cache memori dan `kernel.json` agar pemanggilan `kernel-list` berulang tidak membebani I/O disk.
- **Format Respons `data`**:
```json
{
  "total": 2,
  "default_kernel_id": "chromium-152",
  "data": [
    {
      "id": "builtin",
      "name": "Engine Bawaan",
      "version": "Chromium 150.0.7839.0",
      "major_version": 150,
      "path": "~/.local/share/antidetect/squashfs-root/opt/ungoogled-chromium/chrome",
      "profile_count": 48,
      "is_builtin": true,
      "builtin": true,
      "is_default": false,
      "active": false,
      "disk_size_mb": 398.4,
      "created_at": "2026-01-01 00:00:00",
      "supports": {
        "gpu_vendor": false,
        "gpu_renderer": false,
        "disable_gpu_fingerprint": false,
        "disable_spoofing": true,
        "disable_spoofing_effective": ["canvas", "gpu"]
      }
    },
    {
      "id": "chromium-152",
      "name": "Chromium 152",
      "version": "Chromium 152.0.7950.0",
      "major_version": 152,
      "path": "~/.local/share/antidetect/kernels/chromium-152/chrome",
      "profile_count": 3,
      "is_builtin": false,
      "builtin": false,
      "is_default": true,
      "active": true,
      "disk_size_mb": 415.2,
      "created_at": "2026-10-28 14:30:00",
      "supports": {
        "gpu_vendor": false,
        "gpu_renderer": false,
        "disable_gpu_fingerprint": false,
        "disable_spoofing": true,
        "disable_spoofing_effective": ["canvas", "gpu"]
      }
    }
  ]
}
```
*Catatan kompatibilitas*: Field `builtin` dan `active` tetap dipertahankan berdampingan dengan `is_builtin` dan `is_default` agar kode UI yang sudah ada di `ui/src/components/ProfileDialog.vue:203` dan `ui/src/views/Settings.vue:492` tidak mengalami regresi. Action `engine-info` (`antidetect/api.py:92-106`) juga akan menggunakan struktur objek kernel yang diperkaya ini.

### 3.3 Action `kernel-install`
Memasang kernel Chromium baru ke dalam `KERNELS_DIR` (`~/.local/share/antidetect/kernels/<kernel_id>/`) dari berkas arsip lokal atau direktori lokal yang sudah diekstrak.

- **Endpoint**: `POST /api/v2/kernel-install`
- **Parameter Input**:
  - `source_type` (wajib, string): `'local_archive'` atau `'local_dir'`.
  - `archive_path` (wajib bila `source_type == 'local_archive'`, string): jalur absolut ke berkas arsip di disk lokal (mendukung `.tar.gz`, `.tgz`, `.tar.xz`, `.tar.zst`, atau `.zip`).
  - `source_path` (wajib bila `source_type == 'local_dir'`, string): jalur absolut ke direktori lokal yang berisi biner `chrome` hasil ekstraksi. Sebagai kenyamanan, jika pengirim hanya mengisi `source_path` untuk kedua mode, backend menerima `archive_path = params.get("archive_path") or params.get("source_path")`.
  - `kernel_name` (opsional, string): nama tampilan kernel (maksimal 60 karakter). Jika dikosongkan, nama diambil otomatis dari hasil pembacaan `chrome --version` (misalnya `"Chromium 152"`).
  - `set_default` (opsional, boolean, default `false`): jika `true`, langsung menjadikan kernel yang baru dipasang sebagai kernel default.
- **Tahapan Validasi dan Ekstraksi Aman**:
  1. **Validasi Sumber**:
     - Memastikan `source_type` bernilai `'local_archive'` atau `'local_dir'`. Jika tidak valid, lempar `ApiError(1000, "source_type harus local_archive atau local_dir")`.
     - Memastikan path sumber ada di disk dan bukan di dalam `PROFILES_DIR` (`antidetect/launcher.py:29`).
  2. **Staging Direktori Atomik**:
     - Mengadopsi pola staging seperti pada `extensions.upload` (`antidetect/extensions.py:183-192`), yaitu membuat direktori sementara `.install-<pid>-<hex>` di dalam `KERNELS_DIR` (`~/.local/share/antidetect/kernels/.install-...`) agar operasi pemindahan akhir (`os.replace` / `shutil.move`) berada pada *filesystem* yang sama dan bersifat atomik.
  3. **Path Traversal Guard dan Keamanan Arsip (untuk `local_archive`)**:
     - Sebelum mengekstrak item dari `tarfile` atau `zipfile`, setiap entri diperiksa:
       - Menolak jalur absolut (diawali `/`) atau segmen yang mengandung `..` (mengikuti pola `_safe_parts` di `antidetect/extensions.py:140-161`).
       - Menolak tipe entri perangkat blok/karakter dan FIFO (`tarinfo.isdev()`, `tarinfo.isfifo()`, `tarinfo.ischr()`, `tarinfo.isblk()`).
       - Untuk symlink atau hardlink di dalam arsip (`tarinfo.issym()` atau `tarinfo.islnk()`), target tautan wajib diverifikasi agar setelah di-resolve tetap berada di dalam direktori staging (`Path(target).resolve().relative_to(staging.resolve())`). Symlink yang menunjuk keluar dari direktori kernel langsung ditolak dan menggagalkan instalasi.
       - Membatasi ukuran total hasil ekstraksi maksimal 2.5 GB untuk mencegah *zip/tar bomb* memenuhi disk.
  4. **Normalisasi Struktur Folder (Strip Top-Level Wrapper)**:
     - Apabila arsip atau direktori sumber membungkus isinya di dalam satu subdirektori tunggal (misalnya `ungoogled-chromium_152.0.7950.0_linux/chrome` atau `squashfs-root/opt/ungoogled-chromium/chrome`), installer secara otomatis mencari lokasi berkas `chrome` utama dan memindahkan isi direktori tempat `chrome` berada ke akar direktori staging sehingga jalur akhirnya selalu seragam di `KERNELS_DIR/<kernel_id>/chrome` (`antidetect/launcher.py:178`).
  5. **Validasi Biner ELF x86_64 dan Berkas Pendamping**:
     - Memeriksa keberadaan berkas `chrome` di akar direktori staging.
     - Membaca 20 byte pertama header berkas `chrome` untuk memverifikasi *magic bytes* ELF (`\x7fELF`), kelas 64-bit (`EI_CLASS == 2`), endianness *little-endian* (`EI_DATA == 1`), dan arsitektur mesin `EM_X86_64` (`e_machine == 0x3E` / 62). Jika berkas adalah skrip shell pembungkus (`#!`) seperti pada beberapa paket distribusi Chromium, installer memvalidasi bahwa biner target sesungguhnya (`chrome-bin` atau `chromium-bin`) di folder yang sama merupakan ELF x86_64 yang valid.
     - Memverifikasi keberadaan berkas sumber daya wajib Chromium (`icudtl.dat`, `resources.pak`, dan direktori `locales/`) agar direktori yang tidak lengkap ditolak lebih awal.
  6. **Pengaturan Izin Eksekusi (`chmod +x`)**:
     - Menambahkan bit izin eksekusi (`0o755`) pada `chrome`, `chrome-wrapper`, `chrome_crashpad_handler`, dan `chromedriver` (bila ada), serta mengatur `chrome_sandbox` (jika ada) sesuai kebutuhan izin lokal.
  7. **Pembacaan Versi Otomatis dan Penentuan `kernel_id`**:
     - Menjalankan `launcher._read_kernel_version(staging / "chrome")` (`antidetect/launcher.py:137-149`) yang memanggil `chrome --version` (tidak membuka jendela browser dan diizinkan langsung oleh wrapper di `~/.local/bin/antidetect-engine-wrapper.py:132-134`).
     - Jika `chrome --version` gagal dieksekusi atau tidak mengeluarkan output versi yang valid, direktori staging dihapus dan API melempar `ApiError(1302, "Biner chrome gagal dijalankan untuk pembacaan versi")`.
     - Mengekstrak versi mayor integer (misalnya `152` dari `"Chromium 152.0.7950.0"`).
     - Membentuk `kernel_id` berupa slug bersih dari `kernel_name` atau `chromium-<major_version>` (hanya huruf kecil `a-z`, angka `0-9`, titik, dan tanda hubung; menolak nama `"builtin"` sesuai `antidetect/launcher.py:179`). Jika direktori `KERNELS_DIR / kernel_id` sudah ada, tambahkan sufiks angka (`chromium-152-2`) kecuali versi binernya identik.
  8. **Finalisasi dan Pencatatan Log**:
     - Menulis `kernel.json`, memindahkan direktori staging ke `KERNELS_DIR / kernel_id`, memperbarui cache `launcher._kernel_versions` (`antidetect/launcher.py:121`), dan mencatat aktivitas lewat `_event("kernel-install", ...)` (`antidetect/api.py:62-68`).
- **Format Respons `data`**:
  Mengembalikan objek detail kernel yang baru dipasang (format identik dengan elemen `data` pada `kernel-list`).

### 3.4 Action `kernel-delete`
Menghapus kernel tambahan dari `KERNELS_DIR` setelah memverifikasi bahwa kernel tersebut bukan kernel bawaan dan tidak sedang digunakan oleh profil mana pun.

- **Endpoint**: `POST /api/v2/kernel-delete`
- **Parameter Input**:
  - `kernel_id` (wajib, string): ID folder kernel di `KERNELS_DIR` (misalnya `"chromium-152"`).
- **Aturan Validasi dan Kode Error**:
  1. Jika `kernel_id` kosong, lempar `ApiError(1000, "kernel_id wajib diisi")`.
  2. Jika `kernel_id == "builtin"` (`antidetect/launcher.py:28`), tolak dengan `ApiError(1104, "Engine bawaan tidak bisa dihapus")` (selaras dengan proteksi entitas bawaan di `antidetect/api.py:1253-1254`).
  3. Cari kernel di `launcher.available_kernels()` (`antidetect/launcher.py:161-191`). Jika tidak ditemukan atau berada di luar `KERNELS_DIR` (`antidetect/launcher.py:129-134`), lempar `ApiError(1103, "Kernel tidak ditemukan")`.
  4. **Validasi Penggunaan oleh Profil (Kode Error 1105)**:
     - Periksa seluruh baris di tabel `profiles` (`WHERE deleted_at IS NULL`) yang memiliki `preference_config.kernel_path` mengarah ke `kernel["path"]` (setelah `Path.resolve()`).
     - Periksa juga apakah ada proses profil terbuka di `launcher._open` (`antidetect/launcher.py:958-962`) yang sedang menjalankan biner dari path kernel tersebut.
     - Apabila masih ada profil aktif yang mereferensikan kernel tersebut atau proses yang masih berjalan, tolak penghapusan dengan **kode error `1105`** (konsisten dengan penolakan penghapusan grup yang masih memiliki profil di `antidetect/api.py:1258-1262`):
       `raise ApiError(1105, f"Kernel masih digunakan oleh {count} profil aktif, ubah kernel profil tersebut terlebih dahulu")`.
  5. **Penghapusan dan Pembersihan Cache**:
     - Jika kernel yang dihapus saat ini berstatus sebagai kernel default, kembalikan `default_kernel_id` ke `"builtin"`.
     - Hapus direktori `KERNELS_DIR / kernel_id` dengan `shutil.rmtree()`.
     - Bersihkan entri terkait dari `launcher._kernel_versions` (`antidetect/launcher.py:121`) dan `launcher._flag_cache` (`antidetect/launcher.py:79`).
     - Catat ke log aktivitas `_event("kernel-delete", f'Kernel "{kernel_id}" dihapus')`.
- **Format Respons `data`**:
```json
{
  "deleted": true,
  "kernel_id": "chromium-152",
  "default_kernel_id": "builtin"
}
```

### 3.5 Action `kernel-set-default`
Mengubah kernel bawaan yang otomatis dipilih saat pengguna membuat profil baru.

- **Endpoint**: `POST /api/v2/kernel-set-default`
- **Parameter Input**:
  - `kernel_id` (wajib, string): `"builtin"` atau ID kernel yang terpasang di `KERNELS_DIR`.
- **Aturan Validasi dan Perilaku**:
  1. Memeriksa apakah `kernel_id` ada di dalam `launcher.available_kernels()` (`antidetect/launcher.py:161-191`). Jika tidak ada, lempar `ApiError(1000, "kernel tidak tersedia")` (konsisten dengan `_check_kernel_path` di `antidetect/api.py:123`).
  2. Menyimpan `default_kernel_id` ke penyimpanan konfigurasi lokal (`sync_state` dan `KERNELS_DIR / "default.json"`).
  3. Sejak perubahan disimpan:
     - `launcher.available_kernels()` menandai kernel yang cocok dengan `active: True` dan `is_default: True`.
     - Saat `profile-create` (`antidetect/api.py:416-429`) dipanggil tanpa menyertakan `preference_config.kernel_path` secara eksplisit dan `default_kernel_id != "builtin"`, backend menyisipkan `kernel_path` dari kernel default tersebut ke `preference_config`.
- **Format Respons `data`**:
```json
{
  "default_kernel_id": "chromium-152",
  "path": "~/.local/share/antidetect/kernels/chromium-152/chrome"
}
```

---

## 4. Penyelarasan Fingerprint dan Keamanan Mesin

Pemilihan kernel pada suatu profil tidak boleh hanya mengganti path biner eksekusi secara buta. Apabila biner yang dijalankan adalah Chromium 152 namun konfigurasi fingerprint atau argumen baris perintah masih mengirimkan identitas Chromium 150 (atau sebaliknya), sistem deteksi bot modern akan mendeteksi ketidakcocokan antara fitur mesin JS/CSS/WebGL dan identitas HTTP/Navigator.

### 4.1 Penyelarasan Otomatis User-Agent dan Client Hints (`Sec-CH-UA`)
1. **Perilaku Engine `fingerprint-chromium` Saat Ini**:
   - Di `antidetect/launcher.py:732-733`, fungsi `build_args` selalu menyertakan `--fingerprint={profile['seed']}` dan `--fingerprint-brand=Chrome`.
   - Di `antidetect/launcher.py:751-752`, jika `fp.get("br_version")` ada di dalam `fingerprint_config`, launcher menambahkan `--fingerprint-brand-version={fp['br_version']}`.
   - Selain itu, profil hasil impor dari ixBrowser menyimpan string User-Agent mentah di `fingerprint_config["ua"]` (`antidetect/iximport.py:523`), namun saat ini `build_args` (`antidetect/launcher.py:719-778`) belum menyelaraskan `ua` atau `br_version` dengan versi mayor dari biner kernel yang dipilih oleh `kernel_for(profile)` (`antidetect/launcher.py:722`).
2. **Ekstraksi Nomor Versi Mayor Kernel (`kernel_major_version`)**:
   - Menambahkan fungsi helper `kernel_major_version(binary)` di `antidetect/launcher.py` yang membaca output `kernel_version(binary)` (`antidetect/launcher.py:152-158`), misalnya `"Chromium 152.0.7950.0"`, dan mengekstrak angka mayor `152` menggunakan pencocokan pola `r"(\d+)\.\d+\.\d+\.\d+"` (atau fallback token digit pertama seperti pada `max_kernel_version` di `antidetect/launcher.py:110-113`).
3. **Sinkronisasi `--fingerprint-brand-version` dan Client Hints (`Sec-CH-UA`)**:
   - Di dalam `fingerprint-chromium`, switch `--fingerprint-brand=Chrome` dan `--fingerprint-brand-version=<full_version>` mengatur daftar merek pada header `Sec-CH-UA`, `Sec-CH-UA-Full-Version-List`, dan objek JavaScript `navigator.userAgentData.brands` serta `navigator.userAgentData.getHighEntropyValues()`.
   - Saat `build_args(profile, ...)` (`antidetect/launcher.py:719`) menyusun argumen untuk `binary = kernel_for(profile)`:
     - Sistem membaca versi lengkap biner kernel (misalnya `152.0.7950.0`) dan versi mayornya (`152`).
     - Jika `fp.get("br_version")` kosong, atau jika versi mayor pada `fp["br_version"]` berbeda dari versi mayor `binary` yang dipilih, launcher secara otomatis menyelaraskan `--fingerprint-brand-version` ke `<major>.0.0.0` (atau versi lengkap biner `<major>.0.<build>.0` jika tersedia) agar `Sec-CH-UA` selalu berisi `"Chromium";v="<major>", "Google Chrome";v="<major>"`.
4. **Penyesuaian String `User-Agent` (`Chrome/<version>.0.0.0`)**:
   - Sesuai spesifikasi paritas User-Agent direduksi (*reduced User-Agent* Chromium modern), segmen versi Chrome pada header `User-Agent` wajib berformat `Chrome/<major>.0.0.0`.
   - Apabila profil memiliki `fingerprint_config.ua` (misalnya dari impor ixBrowser di `antidetect/iximport.py:523` yang tertulis `Mozilla/5.0 ... Chrome/134.0.0.0 Safari/537.36`), fungsi baru `align_user_agent(ua, major_version, platform, system_version)` di `antidetect/launcher.py` akan:
     - Mengganti token `Chrome/\d+\.\d+\.\d+\.\d+` di dalam string `ua` menjadi `Chrome/{major_version}.0.0.0`.
     - Meneruskan flag `--user-agent={aligned_ua}` ke daftar argumen peluncuran jika dan hanya jika biner engine membutuhkan override eksplisit, atau membiarkan generator internal `fingerprint-chromium` membentuk User-Agent dari `--fingerprint-platform` (`antidetect/launcher.py:744`), `--fingerprint-platform-version` (`antidetect/launcher.py:746`), dan `--fingerprint-brand-version` (`antidetect/launcher.py:752`) yang semuanya sudah diselaraskan ke `major_version` kernel aktif.
   - Saat pengguna menyimpan profil di `profile-create` (`antidetect/api.py:416`) atau `profile-update` (`antidetect/api.py:432`), backend juga memperbarui field `fingerprint_config["br_version"] = f"{major_version}.0.0.0"` dan menyelaraskan `fingerprint_config["ua"]` (jika ada) di database SQLite agar metadata profil yang ditampilkan di UI atau diekspor lewat `profileio.export_profile` (`antidetect/profileio.py:30-51`) selalu mencerminkan versi kernel yang dipilih.

### 4.2 Integrasi Pengaman `~/.local/bin/antidetect-engine-wrapper.py`
Setiap peluncuran profil di `antidetect/launcher.py:781-795` melewati `_wrap_engine_args(args, binary)` yang memanggil `~/.local/bin/antidetect-engine-wrapper.py --engine <binary> -- <engine_args>`. Pemeriksaan terhadap kode `~/.local/bin/antidetect-engine-wrapper.py` saat ini mengungkap dua celah penting ketika multi-kernel digunakan:

1. **Validasi Keamanan Path `--engine`**:
   - Saat ini `main()` di `~/.local/bin/antidetect-engine-wrapper.py:119-135` menerima nilai apa pun pada argumen `--engine <path>` dan langsung mengeksekusinya lewat `os.execv` (baris `133`) atau `subprocess.Popen` (baris `157`) tanpa memeriksa apakah path tersebut merupakan biner engine CloakDesk yang sah.
   - **Rencana Penguatan**: Menambahkan fungsi `validate_engine_binary(engine_path)` di `~/.local/bin/antidetect-engine-wrapper.py` yang memastikan `os.path.realpath(engine_path)` berada di bawah salah satu dari dua lokasi tepercaya:
     1. Direktori engine bawaan: `~/.local/share/antidetect/squashfs-root/opt/ungoogled-chromium/` (atau nilai `ANTIDETECT_ENGINE_DIR` bila disetel, sesuai `antidetect/launcher.py:21-24`).
     2. Direktori koleksi kernel: `~/.local/share/antidetect/kernels/` (`KERNELS_DIR` di `antidetect/launcher.py:27`).
   - Selain memeriksa awalan direktori hasil `os.path.realpath()`, wrapper juga memverifikasi bahwa nama berkas adalah `chrome`, berkas tersebut reguler (`os.path.isfile`), dan memiliki izin eksekusi (`os.access(..., os.X_OK)`). Jika tidak memenuhi syarat, wrapper menolak peluncuran dengan `fail("path engine di luar lokasi yang diizinkan")`.
2. **Perhitungan Instance Hidup Lintas Kernel (`count_live_instances`)**:
   - Saat ini fungsi `count_live_instances(engine)` di `~/.local/bin/antidetect-engine-wrapper.py:64-102` menyaring proses `/proc/<pid>/exe` dengan kondisi `if os.path.realpath(exe) != target: continue` (`~/.local/bin/antidetect-engine-wrapper.py:85`).
   - Akibatnya, jika pengguna membuka 6 profil dengan kernel bawaan (Chromium 150) dan 6 profil lain dengan kernel baru (Chromium 152), `count_live_instances` menghitungnya secara terpisah per biner sehingga batas keras `MAX_INSTANCES = 6` (`~/.local/bin/antidetect-engine-wrapper.py:32`) dapat ditembus hingga `6 x N_kernel` jendela Chrome sekaligus, yang berisiko memicu kehabisan memori (OOM) pada mesin.
   - **Rencana Perbaikan**: Mengubah `count_live_instances` di `~/.local/bin/antidetect-engine-wrapper.py` agar memeriksa apakah `os.path.realpath(exe)` berada di bawah `ENGINE_DIR` **atau** di bawah `KERNELS_DIR` (seluruh keluarga kernel CloakDesk), serta menghitung jumlah direktori `--user-data-dir` unik di bawah `ALLOWED_DATA_PREFIXES` (`~/.local/bin/antidetect-engine-wrapper.py:28-31`). Dengan demikian batas global maksimal 6 instance Chrome hidup tetap terjaga secara ketat meskipun profil yang dibuka memakai versi kernel yang berbeda-beda.

## 5. Desain Antarmuka Pengguna (UI/UX)

Desain antarmuka mematuhi prinsip teknik bersih (*clean engineering*): permukaan rata (*flat*), garis batas setipis rambut (*hairline border*), kompatibel penuh dengan mode gelap dan mode terang (`ui/src/views/Settings.vue:291-311`), tanpa tumpukan kotak bersarang yang berlebihan.

### 5.1 Pembaruan Klien API Frontend (`ui/src/api.js`)
Di `ui/src/api.js` (di sekitar definisi `engineInfo: () => call('engine-info')` pada `ui/src/api.js:72`), tambahkan metode pemanggilan untuk keempat action manajemen kernel:
```javascript
  kernelList: () => call('kernel-list'),
  kernelInstall: (payload) => call('kernel-install', payload),
  kernelDelete: (kernelId) => call('kernel-delete', { kernel_id: kernelId }),
  kernelSetDefault: (kernelId) => call('kernel-set-default', { kernel_id: kernelId }),
```

### 5.2 Kartu "Kernel Browser" di `ui/src/views/Settings.vue`
Bagian `<section class="info-card engine-card">` yang saat ini hanya menampilkan daftar statis di `ui/src/views/Settings.vue:477-498` ditingkatkan menjadi kartu manajemen **Kernel Browser** yang menempati lebar penuh grid (`grid-column: 1 / -1`, sejajar dengan kartu Sinkronisasi Antar Perangkat di `ui/src/views/Settings.vue:313`).

1. **Header Kartu**:
   - Judul kiri: `Kernel Browser`.
   - Sisi kanan header: badge ringkasan jumlah kernel terpasang (misalnya `2 Kernel Terpasang · Maks v152`) dan tombol aksi utama `<el-button type="primary" size="small" @click="openKernelInstallDialog">Pasang Kernel Baru</el-button>`.
2. **Tabel Kernel Terpasang**:
   - Menggantikan `<ul class="kernel-list">` (`ui/src/views/Settings.vue:487-496`) dengan tabel bersih (`<el-table>` atau tabel CSS grid hairline) yang memuat kolom-kolom berikut:
     - **Nama & Status**: Menampilkan `kernel.name`, disertai badge `Bawaan` jika `kernel.is_builtin` bernilai benar, dan badge status aktif `Default` (`class="kernel-badge"`) jika `kernel.is_default` / `kernel.active` bernilai benar (`ui/src/views/Settings.vue:492`).
     - **Versi Engine**: Menampilkan `kernel.version` dengan fon monospace (`Chromium 152.0.7950.0`), dilengkapi indikator dukungan flag (`Canvas/GPU Spoofing`).
     - **Profil Pemakai**: Menampilkan `kernel.profile_count` profil (misalnya `48 profil` atau `0 profil`).
     - **Ukuran & Lokasi**: Menampilkan `kernel.disk_size_mb` MB dan `kernel.path` dalam teks monospace ringkas (`ui/src/views/Settings.vue:494`).
     - **Aksi**:
       - Tombol `Jadikan Default` (`<el-button size="small" ...>`): memanggil `api.kernelSetDefault(kernel.id)`. Tombol ini dinonaktifkan bila kernel tersebut sudah berstatus default (`kernel.is_default`).
       - Tombol `Hapus` (`<el-button size="small" type="danger" plain ...>`): hanya ditampilkan untuk kernel non-builtin (`!kernel.is_builtin`). Tombol dinonaktifkan (`:disabled="kernel.profile_count > 0"`) dengan tooltip penjelasan `"Tidak dapat dihapus karena masih digunakan oleh profil aktif"`, dan saat diklik menampilkan konfirmasi `ElMessageBox.confirm` sebelum memanggil `api.kernelDelete(kernel.id)`. Jika backend mengembalikan error `1105`, pesan error ditampilkan secara jelas melalui `ElMessage.error`.
3. **Dialog Instalasi Kernel Baru (`Pasang Kernel Baru`)**:
   - Menggunakan `<el-dialog>` berukuran `520px` dengan `<el-form label-position="top">`:
     - **Pilihan Mode Sumber (`source_type`)**: `<el-radio-group>` dengan dua opsi:
       1. `Arsip Lokal (.tar.gz, .tar.xz, .tar.zst, .zip)` (`value="local_archive"`)
       2. `Folder Ekstraksi Lokal` (`value="local_dir"`)
     - **Input Path Sumber (`archive_path` / `source_path`)**: `<el-input>` monospace dengan placeholder contoh `/home/user/Downloads/ungoogled-chromium-152-linux.tar.xz` atau `/home/user/Downloads/chromium-152/`.
     - **Input Nama Kernel (`kernel_name`)**: `<el-input>` opsional dengan placeholder `Kosongkan untuk nama otomatis dari versi Chrome (cth: Chromium 152)`.
     - **Checkbox Default**: `<el-checkbox v-model="installForm.set_default">Jadikan kernel default untuk profil baru setelah terpasang</el-checkbox>`.
   - Saat proses instalasi berlangsung (`installBusy = true`), tombol `Pasang` menampilkan status loading `"Memvalidasi & mengekstrak biner..."` karena ekstraksi arsip berukuran ratusan megabita membutuhkan waktu beberapa detik. Setelah berhasil, `loadEngine()` (`ui/src/views/Settings.vue:72-80`) dipanggil ulang untuk menyegarkan tabel.

### 5.3 Integrasi Pilihan Kernel di `ui/src/components/ProfileDialog.vue`
Pada tab **Preferensi** di `ui/src/components/ProfileDialog.vue:931-957`, pengalaman pemilihan kernel disempurnakan sebagai berikut:

1. **Aktivasi Dropdown "Pilih kernel" yang Konsisten**:
   - Saat ini `<el-select v-if="kernels.length > 1" v-model="kernelPath">` (`ui/src/components/ProfileDialog.vue:934-946`) disembunyikan ketika baru ada satu kernel terpasang.
   - Agar pengguna selalu melihat kontrol yang konsisten dan memahami kernel mana yang sedang dipilih beserta versinya, `<el-select v-model="kernelPath" class="field-320" placeholder="Pilih kernel">` ditampilkan setiap kali `kernels.length >= 1`. Jika `kernels.length === 1`, dropdown tetap aktif menampilkan kernel bawaan beserta petunjuk ringkas di bawahnya bahwa kernel tambahan dapat dipasang melalui menu **Pengaturan > Kernel Browser**.
2. **Format Label Versi yang Jelas (`kernelLabel`)**:
   - Fungsi `kernelLabel(kernel)` di `ui/src/components/ProfileDialog.vue:295-297` diperbarui agar menampilkan informasi versi mayor, nama kernel, serta penanda `Default` atau `Bawaan`:
     ```javascript
     function kernelLabel(kernel) {
       const ver = kernel.version || 'versi tidak terbaca'
       const badge = kernel.is_default || kernel.active ? ' (Default)' : kernel.builtin ? ' (Bawaan)' : ''
       return `${kernel.name}${badge} · ${ver}`
     }
     ```
3. **Pemilihan Otomatis Kernel Default pada Profil Baru**:
   - Computed `builtinKernelPath` (`ui/src/components/ProfileDialog.vue:201-205`) dan `syncKernelPath()` (`ui/src/components/ProfileDialog.vue:299-305`) diperjelas menjadi pemisahan antara:
     - `defaultKernelPath`: mencari kernel dengan `kernel.is_default || kernel.active`, lalu fallback ke `kernel.builtin` atau `list[0]`. Digunakan sebagai nilai awal `kernelPath.value` saat membuat profil baru (`!isEdit.value`).
     - `builtinKernelPath`: khusus mencari `kernel.builtin`. Digunakan saat membuka profil lama yang tidak memiliki `preference_config.kernel_path` agar profil lama tetap memakai kernel tempat profil tersebut dibuat kecuali pengguna mengubahnya secara eksplisit.
4. **Indikator Penyelarasan User-Agent di Tab Fingerprint**:
   - Ketika pengguna mengganti pilihan kernel di tab Preferensi (misalnya ke `Chromium 152`), tab Fingerprint (`ui/src/components/ProfileDialog.vue:804-909`) menampilkan keterangan kontekstual bahwa identitas `User-Agent` dan `Sec-CH-UA` otomatis diselaraskan ke versi mayor kernel terpilih (`Chrome/152.0.0.0`).

---

## 6. Strategi Sinkronisasi Multi-Perangkat (Sync Engine)

CloakDesk memiliki arsitektur sinkronisasi antar perangkat yang diatur di `antidetect/store.py` dan `antidetect/sync_agent.py` (serta didokumentasikan pada `docs/SYNC-PLAN.md`). Manajemen multi-kernel harus bekerja mulus pada skenario multi-perangkat tanpa membebani jaringan maupun merusak profil di perangkat tujuan.

### 6.1 Alasan Biner Kernel Tidak Disinkronkan ke Server VPS
Sesuai prinsip yang telah ditetapkan pada `docs/SYNC-PLAN.md:25`, berkas biner engine di `~/.local/share/antidetect/squashfs-root` dan `~/.local/share/antidetect/kernels/` **tidak pernah diunggah atau disinkronkan** ke server sinkronisasi pusat karena tiga alasan mendasar:
1. **Ketergantungan Arsitektur dan Distribusi OS Host**: Biner `chrome` adalah ELF x86_64 yang terikat pada versi `glibc`, pustaka grafis (`libgbm`, `libdrm`, `libnss3`, `libasound2`), dan struktur path sistem pengguna per mesin (contoh: `/home/alice/.local/...` di Perangkat A berbeda dari `/home/bob/.local/...` di Perangkat B).
2. **Beban Ukuran Penyimpanan dan Bandwidth**: Satu bundel kernel Chromium tidak terkompresi berukuran 350 MB hingga 450 MB. Mengunggah biner ini sebagai blob ke server VPS akan melanggar batasan penyimpanan ringan server sinkronisasi (`docs/SYNC-PLAN.md:17-19`).
3. **Keamanan Eksekusi Kode Jarak Jauh**: Mencegah vektor serangan di mana kompromi pada server sinkronisasi atau *man-in-the-middle* dapat menyuntikkan biner executable berbahaya ke perangkat klien saat melakukan `pull_changes` (`antidetect/sync_agent.py:455-527`).

### 6.2 Representasi Referensi Kernel di Payload Sinkronisasi
Saat ini `_profile_sync_payload(conn, row)` di `antidetect/store.py:767-840` menyertakan seluruh isi `preference_config` (baris `825`) ke dalam payload sinkronisasi. Jika `preference_config` hanya menyimpan `"kernel_path": "~/.local/share/antidetect/kernels/chromium-152/chrome"`, maka saat profil tersebut ditarik oleh Perangkat B (yang mungkin menggunakan nama pengguna Linux berbeda seperti `/home/operator/...`), path absolut tersebut pasti gagal dicocokkan.

Untuk mengatasi masalah tersebut, referensi kernel di dalam `preference_config` diperkaya dengan identitas portabel:
1. **Field Portabel di `preference_config`**:
   - Saat profil disimpan (`profile-create` di `antidetect/api.py:416`, `profile-update` di `antidetect/api.py:432`) atau disiapkan untuk outbox di `_profile_sync_payload` (`antidetect/store.py:825`), jika profil memakai kernel tertentu, objek `preference_config` menyimpan tiga atribut pendamping:
     - `kernel_id`: ID direktori kernel, misalnya `"chromium-152"` (atau `"builtin"`).
     - `kernel_version`: Nomor versi mayor atau string versi kernel, misalnya `"152"` (atau `"152.0.7950.0"`).
     - `kernel_path`: Jalur lokal di mesin asal, misalnya `"~/.local/share/antidetect/kernels/chromium-152/chrome"`.
2. **Resolusi dan Fallback Otomatis Saat Sinkronisasi (`apply_remote_change`) dan Peluncuran (`kernel_for`)**:
   - Ketika Perangkat B menerima perubahan profil melalui `store.apply_remote_change(conn, change)` (`antidetect/store.py:1853-2043`), fungsi resolusi kernel lokal `launcher.resolve_synced_kernel_preference(pref_config)` dijalankan terhadap `pref_config` (`antidetect/store.py:1890-1892`):
     1. **Pencocokan berdasar `kernel_id`**: Jika Perangkat B memiliki kernel terpasang di `KERNELS_DIR / kernel_id / "chrome"`, maka `pref_config["kernel_path"]` diperbarui ke jalur lokal milik Perangkat B tanpa mengubah `sync_dirty` (`sync_dirty = 0` di `antidetect/store.py:1962`).
     2. **Pencocokan berdasar `kernel_version` (Versi Mayor)**: Jika nama folder berbeda tetapi Perangkat B memiliki kernel terpasang dengan `major_version` yang sama dengan `pref_config["kernel_version"]`, gunakan path kernel lokal yang memiliki versi mayor tersebut.
     3. **Fallback Aman ke Kernel Bawaan (`builtin`)**: Apabila Perangkat B belum memasang kernel dengan ID maupun versi tersebut, atribut `kernel_version` dan `kernel_id` tetap dipertahankan sebagai metadata di `pref_config`, sedangkan `launcher.kernel_for(profile)` (`antidetect/launcher.py:194-202`) secara otomatis melakukan fallback ke engine bawaan (`CHROME` di `antidetect/launcher.py:25`) saat profil dibuka, dan `ProfileDialog.vue` menampilkan peringatan ramah `kernelSavedMissing` (`ui/src/components/ProfileDialog.vue:303`, `ui/src/components/ProfileDialog.vue:955-957`).
   - Selain itu, saat profil dengan fallback dibuka di Perangkat B, penyelarasan dinamis pada `build_args` (Bab 4.1) memastikan `--fingerprint-brand-version` mengikuti biner aktual yang benar-benar dijalankan di Perangkat B sehingga tidak terjadi kebocoran ketidakcocokan versi browser selama sesi berlangsung.

---

## 7. Rencana Pembagian Fase Implementasi

Implementasi fitur Kernel Manager dibagi menjadi tiga fase berurutan yang terisolasi dengan baik agar setiap tahap dapat diuji secara independen tanpa mengganggu stabilitas server produksi lokal.

### 7.1 Fase 1: Backend API, Pengaman Wrapper, dan Skrip Installer CLI
**Cakupan Pekerjaan**:
1. **Modul `antidetect/kernels.py` dan Pembaruan `antidetect/launcher.py`**:
   - Mengimplementasikan pembacaan dan penulisan `kernel.json` serta `default.json` di bawah `KERNELS_DIR` (`antidetect/launcher.py:27`).
   - Memperkaya `available_kernels()` (`antidetect/launcher.py:161-191`) dengan `major_version`, `profile_count`, `is_builtin`, `is_default`, `disk_size_mb`, dan `created_at`.
   - Menambahkan validasi header ELF x86_64, ekstraksi arsip dengan *path traversal guard* dan *symlink guard* (mengadaptasi pola `antidetect/extensions.py:140-192`), normalisasi folder tingkat atas, pengaturan `chmod +x`, dan pembacaan `chrome --version`.
2. **Endpoint Baru di `antidetect/api.py`**:
   - Mendaftarkan action `kernel-list`, `kernel-install`, `kernel-delete` (dengan penolakan kode error `1104` untuk builtin dan `1105` jika sedang digunakan profil aktif, selaras dengan `antidetect/api.py:1253-1262`), dan `kernel-set-default`.
   - Memperbarui `profile_create` (`antidetect/api.py:416-429`) agar menerapkan kernel default bila `kernel_path` tidak dikirim oleh klien.
3. **Penguatan `~/.local/bin/antidetect-engine-wrapper.py`**:
   - Menambahkan validasi bahwa argumen `--engine` wajib menunjuk ke biner di bawah `ENGINE_DIR` atau `KERNELS_DIR` (`~/.local/bin/antidetect-engine-wrapper.py:119-135`).
   - Memperbaiki `count_live_instances` (`~/.local/bin/antidetect-engine-wrapper.py:64-102`) agar menghitung seluruh proses Chrome di bawah `ENGINE_DIR` maupun `KERNELS_DIR` secara gabungan terhadap batas `MAX_INSTANCES = 6`.
4. **Skrip CLI `scripts/install-kernel.sh`**:
   - Menyediakan skrip utilitas baris perintah untuk administrator/pengguna yang dapat memasang arsip kernel lokal (`.tar.xz`, `.tar.gz`, `.zip`) atau direktori ke `~/.local/share/antidetect/kernels/<id>` baik melalui pemanggilan API `http://127.0.0.1:53200/api/v2/kernel-install` maupun secara langsung lewat `./venv/bin/python -m antidetect`.

**Kriteria Verifikasi Fase 1**:
- Pengujian unit/integrasi API menggunakan skrip Python (tanpa membuka jendela Chrome penuh, cukup mock atau biner uji `--version`) memverifikasi `kernel-list`, `kernel-install`, penolakan path traversal, penolakan `kernel-delete` dengan kode `1105` saat profil terikat, dan `kernel-set-default`.

### 7.2 Fase 2: Antarmuka UI (Manajemen Kernel di Pengaturan & Form Profil)
**Cakupan Pekerjaan**:
1. **Klien API (`ui/src/api.js`)**:
   - Menambahkan fungsi `kernelList`, `kernelInstall`, `kernelDelete`, dan `kernelSetDefault` di `ui/src/api.js:72`.
2. **Halaman Pengaturan (`ui/src/views/Settings.vue`)**:
   - Merombak kartu `.engine-card` (`ui/src/views/Settings.vue:477-498`) menjadi kartu **Kernel Browser** lengkap dengan tabel daftar kernel, badge `Default` dan `Bawaan`, informasi ukuran disk dan jumlah profil pemakai, tombol `Jadikan Default`, tombol `Hapus` dengan konfirmasi, serta dialog `Pasang Kernel Baru`.
3. **Dialog Profil (`ui/src/components/ProfileDialog.vue`)**:
   - Memperbarui `kernelLabel(kernel)` (`ui/src/components/ProfileDialog.vue:295-297`), logika `defaultKernelPath` vs `builtinKernelPath` (`ui/src/components/ProfileDialog.vue:201-205`), dan kondisi render dropdown pemilihan kernel di tab Preferensi (`ui/src/components/ProfileDialog.vue:933-957`).

**Kriteria Verifikasi Fase 2**:
- `cd ui && npm run build` berhasil tanpa warning/error kompilasi Vue/Vite.
- Tampilan mode gelap dan mode terang pada kartu Kernel Browser dan tab Preferensi diuji secara visual dan mematuhi standar anti-slop.

### 7.3 Fase 3: Penyelarasan Dinamis Fingerprint, Sinkronisasi, dan Pengujian Regresi
**Cakupan Pekerjaan**:
1. **Penyelarasan Fingerprint (`antidetect/launcher.py` dan `antidetect/api.py`)**:
   - Mengimplementasikan `kernel_major_version(binary)` dan `align_user_agent(...)` di `antidetect/launcher.py`.
   - Memastikan `build_args` (`antidetect/launcher.py:719-778`) menyelaraskan `--fingerprint-brand-version` dan User-Agent (`Chrome/<version>.0.0.0`) serta Client Hints `Sec-CH-UA` dengan versi mayor kernel yang dipilih.
2. **Resolusi Kernel Lintas Perangkat (`antidetect/store.py`)**:
   - Menyimpan atribut portabel `kernel_id` dan `kernel_version` di `preference_config` pada `_profile_sync_payload` (`antidetect/store.py:825`) dan menerapkan resolusi lokal serta fallback otomatis di `apply_remote_change` (`antidetect/store.py:1890-1893`).
3. **Pengujian Regresi End-to-End**:
   - Menjalankan `scripts/selftest.py` dan `scripts/test-sync.py` untuk memastikan seluruh kontrak SDK ixBrowser v2 dan sinkronisasi multi-perangkat tetap lulus 100%.
   - Bila melakukan verifikasi peluncuran engine nyata pada tahap implementasi nantinya, wajib mematuhi aturan keselamatan mesin: maksimal satu instance Chrome pada satu waktu, `--user-data-dir` di tmpfs (`/dev/shm/antidetect`), dan memeriksa `pgrep -c chrome` serta `free -h` setelah pengujian selesai.

