# Rencana Arsitektur Sinkronisasi Antar Device

## 1. Ringkasan Eksekutif dan Tujuan

Dokumen ini mendefinisikan spesifikasi teknis dan rencana implementasi sinkronisasi antar device untuk `antidetect`, manajer profil browser anti-detect lokal yang menyediakan API kompatibel ixBrowser v2 di `127.0.0.1:53200`.

Arsitektur yang dirancang memisahkan eksekusi browser dari penyimpanan terpusat:
1. **Eksekusi Lokal Murni**: Engine `fingerprint-chromium` (versi 150), proses jembatan proxy `gost`, manipulasi jendela (`wmctrl`, `xdotool`), serta pembacaan dan penulisan database SQLite Chromium tetap berjalan sepenuhnya di device pengguna. Server sinkronisasi tidak pernah menjalankan proses browser atau melakukan rendering halaman.
2. **Server sebagai Penyimpanan dan Koordinator**: Server terpusat hanya menyimpan metadata entitas (profil, grup, tag, proxy, template, metadata extension), blob cookie terenkripsi, log perubahan berbasis cursor (`change_log`), dan status kunci eksklusif (`profile_leases`).
3. **Offline-First di Device**: Setiap device tetap menyimpan database lokal `~/.local/share/antidetect/antidetect.db` dan direktori profil `~/.local/share/antidetect/profiles/<id>/data`. Ketika koneksi ke server terputus, operasi CRUD metadata lokal tetap berjalan dan dicatat ke antrean keluar (`sync_outbox`) untuk dikirim saat kembali online.
4. **Kompatibilitas Penuh SDK ixBrowser**: Klien lokal seperti `ixbrowser-local-api` dan skrip otomasi Selenium tetap memanggil endpoint `/api/v2/<action>` dengan `profile_id` bertipe integer. Pemetaan ke identitas global (`uuid`) dilakukan secara transparan di lapisan penyimpanan lokal.
5. **Pencegahan Korupsi Sesi Akun**: Satu profil hanya boleh dibuka pada satu device dalam satu waktu melalui mekanisme *distributed lease* dengan *time-to-live* (TTL) dan perpanjangan berkala (*heartbeat*). Sinkronisasi cookie hanya dijalankan saat profil ditutup secara bersih (*graceful close*).

## 2. Batasan dan Non-Tujuan

### Batasan Sistem
1. **Batas Eksekusi Engine**: Server sinkronisasi berjalan sebagai proses ringan (satu binary atau satu container) dengan penyimpanan SQLite dalam mode WAL dan direktori objek lokal atau penyimpanan kompatibel S3. Tidak ada ketergantungan pada orkestrator kompleks seperti Kubernetes.
2. **Jaringan Privat**: Server dirancang untuk diakses melalui jaringan privat terenkripsi (seperti Tailscale atau WireGuard) atau di balik reverse proxy TLS dengan autentikasi token per device, bukan diekspos sebagai layanan multi-tenant publik.
3. **Ukuran Blob**: Sinkronisasi data sesi pada tahap awal (MVP) dibatasi pada daftar cookie JSON yang diekstrak dari SQLite Chromium, bukan salinan penuh direktori `--user-data-dir` yang berukuran ratusan megabita.

### Non-Tujuan (Di Luar Cakupan MVP)
1. **Eksekusi Browser Jarak Jauh**: Server tidak menyediakan VNC, WebRTC streaming, maupun remote WebDriver.
2. **Sinkronisasi Real-Time Saat Chrome Hidup**: File SQLite `Cookies` yang sedang dikunci oleh proses Chromium aktif tidak disinkronkan secara langsung saat browser berjalan untuk menghindari korupsi halaman SQLite dan konflik WAL.
3. **Sinkronisasi Password (`Login Data`) dan Site Data (`Local Storage`, `IndexedDB`) pada MVP**: Kredensial tersimpan dan penyimpanan lokal situs ditunda ke fase lanjutan setelah sinkronisasi metadata, lease, dan cookie terbukti stabil.
4. **Sinkronisasi Binary Engine dan Binary Gost**: File di `~/.local/share/antidetect/squashfs-root`, `~/.local/share/antidetect/kernels/`, dan `~/.local/bin/gost` dikelola per mesin karena bergantung pada arsitektur sistem operasi host.
5. **Sinkronisasi Log Lokal dan Riwayat Cek Proxy**: Tabel `events`, tabel `proxy_checks`, serta file `chrome.log` dan `gost.log` bersifat diagnostik lokal per mesin dan tidak dikirim ke server.

## 3. Kondisi Kode Saat Ini

Bagian ini merangkum struktur kode aktual di repositori `repositori/cloakdesk` berdasarkan pemeriksaan graf simbol dan pembacaan langsung file sumber.

### 3.1 Lokasi Data (`antidetect/paths.py` dan `antidetect/launcher.py`)
* `antidetect/paths.py` mendefinisikan `DATA_DIR` yang membaca variabel lingkungan `ANTIDETECT_HOME` atau kembali ke `~/.local/share/antidetect`.
* `antidetect/store.py` menyimpan seluruh metadata di `DB_PATH = DATA_DIR / "antidetect.db"`.
* `antidetect/launcher.py` mendefinisikan:
  * `PROFILES_DIR = DATA_DIR / "profiles"`.
  * Direktori user data Chromium per profil berada di `PROFILES_DIR / str(profile_id) / "data"`, atau `data-2`, `data-3` jika fitur multi-instance diaktifkan lewat `_instance_data_dir(profile_id, index)`.
  * File status proses aktif disimpan di `PROFILES_DIR / str(profile_id) / "state.json"` oleh `_write_state(profile_id, info)` dan dihapus oleh `_clear_state(profile_id)`.
  * Log proses ditulis ke `PROFILES_DIR / str(profile_id) / "chrome.log"` dan `gost.log`.

### 3.2 Skema Database dan Operasi CRUD (`antidetect/store.py`)
Fungsi `store.init()` menginisialisasi delapan tabel SQLite:
1. `profiles`: kolom `id` (`INTEGER PRIMARY KEY AUTOINCREMENT`), `name`, `group_id` (default `1`), `color`, `note`, `site_id`, `site_url`, `proxy_config` (teks JSON), `proxy_key` (MD5 dari `proxy_ip` + `proxy_port` + `proxy_user` + `proxy_password` yang dihitung oleh `proxy_key_for(proxy_config)`), `fingerprint_config` (teks JSON), `preference_config` (teks JSON), `tag_ids` (teks JSON array integer, default `'[]'`), `last_open_time` (integer epoch), `seed` (integer), `created_at`, `updated_at`, dan `deleted_at` (soft delete untuk keranjang sampah).
2. `groups`: kolom `id`, `title`, `sort`, `created_at`. Grup `Default` (`id = 1`) dibuat otomatis saat inisialisasi.
3. `tags`: kolom `id`, `title`.
4. `proxies`: kolom `id`, `proxy_type`, `proxy_ip`, `proxy_port`, `proxy_user`, `proxy_password`, `note`, `tag`.
5. `proxy_checks`: kolom `id`, `proxy_id`, `checked_at`, `ok`, `ip`, `latency_ms`, `error`, dibatasi 500 baris per proxy oleh `add_proxy_check()`.
6. `templates`: kolom `id`, `name`, `payload` (teks JSON snapshot profil), `created_at`.
7. `extensions`: kolom `id`, `slug` (`UNIQUE`), `title`, `version`, `path`, `created_at`.
8. `events`: kolom `id`, `kind`, `message`, `profile_id`, `created_at`, dibatasi 2000 baris oleh `add_event()`.

Fungsi CRUD utama di `antidetect/store.py` meliputi `profile_to_dict(row, include_seed=True)`, `profile_get(conn, profile_id)`, `list_profiles(...)`, `create_profile(conn, data)`, `update_profile(conn, profile_id, data)`, `mark_opened(conn, profile_id, timestamp=None)`, `soft_delete_profile(conn, profile_id)`, `restore_profile(conn, profile_id)`, `delete_profile_permanent(conn, profile_id)`, dan `empty_recycle_bin(conn)`. Saat ini seluruh tabel menggunakan `INTEGER PRIMARY KEY AUTOINCREMENT` lokal tanpa kolom UUID, tanpa nomor revisi, dan tanpa pencatatan penghapusan permanen (*tombstone*), sedangkan operasi CRUD grup, tag, dan update/delete proxy masih dieksekusi dengan query SQL langsung di `antidetect/api.py`.

### 3.3 Siklus Hidup Buka dan Tutup Profil (`antidetect/launcher.py`)
* `open_profile(profile, app_args=None)`:
  * Memeriksa apakah `profile_id` sudah ada di dictionary memori `_open`.
  * Jika proxy memerlukan autentikasi (`user` atau `password`), memanggil `_start_gost(profile_id, spec)` untuk menjalankan proses `gost` lokal di `127.0.0.1:<gost_port>`.
  * Memanggil `build_args(profile, port, app_args, proxy_arg, data_dir)` yang menyiapkan direktori data, memanggil `ensure_password_manager(data_dir)` untuk menyetel `"credentials_enable_service": True` pada `Default/Preferences`, menyusun flag fingerprint, lalu membungkus perintah dengan `_wrap_engine_args(args, binary)` (`antidetect-engine-wrapper.py`).
  * Menjalankan proses Chromium dan menunggu port DevTools siap melalui `_wait_for_devtools(port, deadline)`.
  * Menyimpan metadata proses ke `_open[profile_id]` dan menulis `state.json` melalui `_write_state()`.
* `close_profile(profile_id)`:
  * Mengambil entri dari `_open.pop(profile_id, None)` atau membaca fallback dari `_read_state(profile_id)`.
  * Memanggil `_graceful_close(debugging_address, proc=proc, pid=pid)` yang mengambil daftar target bertipe `"page"` dari `http://<debugging_address>/json/list`, memanggil `GET /json/close/<id>` untuk setiap tab agar Chromium mem-flush cookie dan state sesi ke disk sebelum proses keluar, lalu menunggu proses berhenti hingga batas waktu 10 detik.
  * Jika `_graceful_close` gagal atau melewati batas waktu, memanggil `_kill_pgid(pid)` yang mengirim `SIGTERM` diikuti `SIGKILL`.
  * Mematikan proses `gost` melalui `_stop_gost(info)` dan menghapus `state.json` melalui `_clear_state(profile_id)`.
* `is_open(profile_id)` memeriksa keberadaan `profile_id` di `_open`, sedangkan `forget(profile_id)` membersihkan state bila proses sudah mati.

### 3.4 Operasi Cookie (`antidetect/cookies.py`)
* Lokasi file database cookie ditentukan oleh `_database_path(profile_id)` yang memeriksa `PROFILES_DIR / str(profile_id) / "data" / "Default" / "Network" / "Cookies"` lebih dulu, lalu fallback ke `Default / "Cookies"`.
* Seluruh operasi baca dan tulis memanggil `_require_closed(profile_id)` yang menolak eksekusi dengan `CookieError("tutup profil dulu")` apabila `launcher.is_open(profile_id)` bernilai `True`.
* Enkripsi cookie Chromium di Linux dengan `--password-store=basic` ditangani secara mandiri tanpa library eksternal: kunci AES-128-CBC diturunkan melalui `PBKDF2-HMAC-SHA1(password=b"peanuts", salt=b"saltysalt", iterations=1, dklen=16)` dengan IV 16 byte spasi (`b" " * 16`) dan prefix `v10`. Nilai plaintext diawali digest SHA-256 32 byte dari `host_key` (skema versi 24).
* `get_cookies(profile_id)` membaca tabel `cookies`, mendekripsi `encrypted_value` lewat `_decode_value()`, dan mengembalikan string JSON berisi list objek dengan atribut `domain`, `name`, `path`, `value`, `expires`, `httpOnly`, `secure`, `sameSite`, dan `session`.
* `update_cookies(profile_id, cookie)` menyalin file database ke `Cookies.bak`, memastikan skema tabel `cookies` dan `meta` (`version = 24`) tersedia lewat `_create_schema(conn)`, lalu mengenkripsi ulang dan memasukkan setiap item lewat `_insert_cookie(conn, columns, cookie)`.

### 3.5 Operasi Password (`antidetect/passwords.py`)
* Lokasi file database password berada di `Default/Login Data` yang ditentukan oleh `_database_path(profile_id)`.
* `list_saved_passwords(profile_id)` dapat membaca tabel `logins` (`origin_url`, `username_value`, `password_value`) baik saat profil tertutup maupun saat profil terbuka (dengan menyalin database beserta `-wal`, `-shm`, dan `-journal` ke direktori sementara melalui `_read_copy(path)`). Dekripsi `password_value` memakai `cookies._cbc_decrypt(raw[3:])` untuk prefix `v10`/`v11`.
* `delete_password(profile_id, origin_url, username)` mewajibkan profil dalam keadaan tertutup, membuat cadangan `Login Data.bak`, dan menghapus baris yang cocok dari tabel `logins`. Belum ada fungsi penulisan atau impor password ke `Login Data`.

### 3.6 Format Ekspor dan Enkripsi Bundle (`antidetect/profileio.py`)
* `export_profile(profile_id)` menggabungkan output `store.profile_to_dict(row)` dan list hasil `cookies.get_cookies(profile_id)` ke dalam struktur JSON `{"version": 1, "exported_at": "<ISO-8601>", "profile": {...}, "cookies": [...]}`.
* `encrypt_bundle(bundle, password)` dan `decrypt_bundle(envelope, password)` mengenkripsi JSON bundle menggunakan subproses CLI `openssl enc -aes-256-cbc -pbkdf2 -salt -md sha256 -pass stdin` dan membungkus ciphertext base64 ke dalam amplop `{"format": "antidetect-profile-encrypted", "version": 1, "data": "<base64>"}`.
* `import_profile(bundle)` membuat profil baru dengan nama berakhiran `(impor)`, memberikan seed baru lewat `launcher.rand_seed()`, dan memanggil `cookies.update_cookies(profile_id, cookie_items)`.

### 3.7 Pola Action dan Kontrak API (`antidetect/api.py`)
* `Handler` (turunan `BaseHTTPRequestHandler`) melayani `POST /api/v2/<action>` dengan batas konkurensi `MAX_CONCURRENT_REQUESTS = 64` dan pemeriksaan CORS lokal (`_origin_allowed`).
* Semua respons menggunakan amplop standar `{"error": {"code": 0, "message": "success"}, "data": ...}` atau kode error non-nol saat `ApiError(code, message)` dilempar. Daftar berpaginasi mengembalikan `{"total": <int>, "data": [...]}`.
* Titik kait (*hook points*) utama untuk sinkronisasi berada pada action mutasi data: `profile-create`, `profile-update`, `profile-delete`, `profile-restore`, `profile-delete-permanent`, `empty-recycle-bin`, `profile-copy`, `profile-import`, `profile-import-csv`, `profile-import-ixbrowser`, `profile-open`, `profile-open-with-random-fingerprint`, `profile-close`, `profile-close-in-batches`, `profile-update-cookies`, `profile-clear-cache-and-cookies`, serta action CRUD untuk `group-*`, `tag-*`, `proxy-*`, dan `template-*`.

## 4. Model Data Sinkronisasi

### 4.1 Strategi Identitas Entitas: Pemetaan Integer Lokal ke UUID Global

SDK resmi `ixbrowser-local-api`, UI Vue, dan seluruh kontrak `/api/v2/*` mewajibkan `profile_id`, `group_id`, `tab_id`, `proxy_id`, dan `template_id` bertipe integer. Namun, sekuens `AUTOINCREMENT` lokal pada dua device yang membuat profil secara independen pasti menghasilkan tabrakan nomor integer.

Terdapat dua opsi desain identitas:
1. **Opsi A (Tabel Pemetaan Eksternal Terpisah)**: Membiarkan tabel `profiles`, `groups`, `tags`, `proxies`, dan `templates` tanpa kolom baru, lalu membuat tabel `entity_id_map(entity_type, local_id, uuid)`. Pendekatan ini membutuhkan `JOIN` tambahan pada setiap operasi baca/tulis dan rentan meninggalkan baris yatim (*orphan*) jika penghapusan tidak dibungkus trigger.
2. **Opsi B (Kolom `uuid` dan Metadata Sync Langsung di Tabel Entitas Lokal, Direkomendasikan)**: Menambahkan kolom `uuid TEXT UNIQUE NOT NULL` (UUIDv4) beserta kolom sinkronisasi (`revision`, `origin_device`, `sync_dirty`, `cookie_blob_id`, `cookie_revision`) langsung pada tabel lokal `profiles`, `groups`, `tags`, `proxies`, `templates`, dan `extensions`.

**Keputusan**: Memilih **Opsi B** dengan aturan pemetaan berikut:
* `id` (`INTEGER PRIMARY KEY AUTOINCREMENT`) tetap menjadi kunci lokal yang diekspos sebagai `profile_id` ke API `/api/v2/*` dan menjadi nama folder `profiles/<id>/data` di disk lokal.
* `uuid` menjadi kunci primer atau kunci unik global di seluruh komunikasi jaringan, payload server, dan log perubahan.
* Relasi antar entitas di dalam payload sinkronisasi selalu diterjemahkan ke UUID:
  * `group_id` lokal diterjemahkan menjadi `group_uuid` saat dikirim ke server, dan dipetakan kembali ke `groups.id` lokal saat diterima di device lain. Grup `Default` (`id = 1`) diberi UUID deterministik tetap: `00000000-0000-4000-8000-000000000001`.
  * `tag_ids` (array integer lokal) diterjemahkan menjadi `tag_uuids` (array UUID) di payload server, lalu dikonversi kembali menjadi JSON array integer lokal saat disimpan ke `profiles.tag_ids`.
* Dengan cara ini, klien SDK ixBrowser di Device A dapat memanggil `profile-open` dengan `profile_id: 5`, sedangkan di Device B profil yang sama mungkin memiliki `profile_id: 12`, tetapi keduanya menunjuk ke `uuid` yang sama di server.

### 4.2 Skema Database Server (`server_sync.db`)

Server menggunakan SQLite dengan `PRAGMA journal_mode = WAL`, `PRAGMA synchronous = NORMAL`, dan `PRAGMA foreign_keys = ON`.

```sql
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    device_name TEXT NOT NULL,
    platform TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    app_version TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_devices_token_hash ON devices (token_hash);

CREATE TABLE IF NOT EXISTS enrollment_tokens (
    token_hash TEXT PRIMARY KEY,
    label TEXT,
    expires_at INTEGER NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS server_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_groups (
    uuid TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    sort INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    origin_device TEXT NOT NULL REFERENCES devices(device_id)
);
CREATE INDEX IF NOT EXISTS idx_sync_groups_title ON sync_groups (lower(title));

CREATE TABLE IF NOT EXISTS sync_tags (
    uuid TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    origin_device TEXT NOT NULL REFERENCES devices(device_id)
);

CREATE TABLE IF NOT EXISTS sync_proxies (
    uuid TEXT PRIMARY KEY,
    proxy_type TEXT,
    proxy_ip TEXT,
    proxy_port INTEGER,
    proxy_user TEXT,
    proxy_password_enc TEXT,
    proxy_fingerprint TEXT,
    note TEXT,
    tag TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    origin_device TEXT NOT NULL REFERENCES devices(device_id)
);
CREATE INDEX IF NOT EXISTS idx_sync_proxies_fingerprint ON sync_proxies (proxy_fingerprint);

CREATE TABLE IF NOT EXISTS sync_templates (
    uuid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    payload TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    origin_device TEXT NOT NULL REFERENCES devices(device_id)
);

CREATE TABLE IF NOT EXISTS sync_extensions (
    uuid TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT,
    version TEXT,
    archive_blob_id TEXT,
    archive_sha256 TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    origin_device TEXT NOT NULL REFERENCES devices(device_id)
);

CREATE TABLE IF NOT EXISTS sync_profiles (
    uuid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    group_uuid TEXT NOT NULL DEFAULT '00000000-0000-4000-8000-000000000001',
    color TEXT,
    note TEXT,
    site_id INTEGER,
    site_url TEXT,
    proxy_config TEXT NOT NULL DEFAULT '{}',
    proxy_fingerprint TEXT,
    fingerprint_config TEXT NOT NULL DEFAULT '{}',
    preference_config TEXT NOT NULL DEFAULT '{}',
    tag_uuids TEXT NOT NULL DEFAULT '[]',
    seed INTEGER NOT NULL,
    last_open_time INTEGER,
    cookie_blob_id TEXT,
    cookie_revision INTEGER NOT NULL DEFAULT 0,
    cookie_updated_at TEXT,
    cookie_origin_device TEXT,
    last_fencing_token INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    purged_at TEXT,
    origin_device TEXT NOT NULL REFERENCES devices(device_id)
);
CREATE INDEX IF NOT EXISTS idx_sync_profiles_group ON sync_profiles (group_uuid);
CREATE INDEX IF NOT EXISTS idx_sync_profiles_deleted ON sync_profiles (deleted_at, purged_at);

CREATE TABLE IF NOT EXISTS profile_blobs (
    blob_id TEXT PRIMARY KEY,
    profile_uuid TEXT NOT NULL REFERENCES sync_profiles(uuid),
    blob_kind TEXT NOT NULL CHECK (blob_kind IN ('cookies', 'bookmarks', 'extension_pkg', 'snapshot')),
    revision INTEGER NOT NULL,
    sha256_plain TEXT NOT NULL,
    sha256_cipher TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    encryption_scheme TEXT NOT NULL DEFAULT 'aes-256-gcm-v1',
    storage_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    origin_device TEXT NOT NULL REFERENCES devices(device_id)
);
CREATE INDEX IF NOT EXISTS idx_profile_blobs_profile ON profile_blobs (profile_uuid, blob_kind, revision DESC);

CREATE TABLE IF NOT EXISTS profile_leases (
    profile_uuid TEXT PRIMARY KEY REFERENCES sync_profiles(uuid),
    holder_device_id TEXT NOT NULL REFERENCES devices(device_id),
    lease_token TEXT NOT NULL UNIQUE,
    fencing_token INTEGER NOT NULL,
    acquired_at INTEGER NOT NULL,
    renewed_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_profile_leases_expires ON profile_leases (expires_at);

CREATE TABLE IF NOT EXISTS change_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL CHECK (
        entity_type IN ('profile', 'group', 'tag', 'proxy', 'template', 'extension', 'cookie_blob')
    ),
    entity_uuid TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('upsert', 'soft_delete', 'purge')),
    revision INTEGER NOT NULL,
    origin_device TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_change_log_entity ON change_log (entity_type, entity_uuid, seq);

CREATE TABLE IF NOT EXISTS conflicts (
    conflict_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_uuid TEXT NOT NULL,
    base_revision INTEGER NOT NULL,
    server_revision INTEGER NOT NULL,
    losing_device TEXT NOT NULL,
    winning_device TEXT NOT NULL,
    losing_payload TEXT NOT NULL,
    winning_payload TEXT NOT NULL,
    reason TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by_device TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conflicts_unresolved ON conflicts (entity_uuid, resolved_at);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT,
    action TEXT NOT NULL,
    target_uuid TEXT,
    ip_address TEXT,
    status_code INTEGER NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs (created_at);
```

Catatan desain tabel server:
* `sync_profiles` memisahkan dua status penghapusan: `deleted_at` berarti profil masuk Keranjang Sampah (dapat dipulihkan lewat `profile-restore`), sedangkan `purged_at` adalah *tombstone* penghapusan permanen (`profile-delete-permanent` atau `empty-recycle-bin`) agar device lain yang baru online tahu bahwa direktori `profiles/<id>` lokalnya harus dihapus.
* `fencing_token` disimpan permanen di `sync_profiles.last_fencing_token`, bukan hanya di `profile_leases`. Saat lease dilepas, baris lease boleh dihapus, tetapi nilai terakhir tetap ada sehingga token berikutnya selalu lebih besar. Server menolak unggahan blob cookie yang membawa token lebih kecil daripada `sync_profiles.last_fencing_token`.
* Kolom `proxy_fingerprint` di `sync_proxies` dan `sync_profiles` berisi HMAC-SHA256 dengan kunci turunan `KEK`, bukan MD5 password. Ini menjaga deduplikasi proxy tanpa membocorkan material yang bisa diuji offline.

### 4.3 Penambahan Skema pada Database Lokal (`~/.local/share/antidetect/antidetect.db`)

Tabel eksisting (`profiles`, `groups`, `tags`, `proxies`, `templates`, `extensions`) diperluas dengan kolom pelacak sinkronisasi, ditambah empat tabel lokal baru:

```sql
-- Kolom tambahan pada tabel profiles eksisting:
ALTER TABLE profiles ADD COLUMN uuid TEXT;
ALTER TABLE profiles ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE profiles ADD COLUMN origin_device TEXT;
ALTER TABLE profiles ADD COLUMN purged_at TEXT;
ALTER TABLE profiles ADD COLUMN cookie_blob_id TEXT;
ALTER TABLE profiles ADD COLUMN cookie_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE profiles ADD COLUMN cookie_sha256 TEXT;
ALTER TABLE profiles ADD COLUMN sync_dirty INTEGER NOT NULL DEFAULT 1;
CREATE UNIQUE INDEX IF NOT EXISTS idx_profiles_uuid ON profiles (uuid);

-- Kolom tambahan serupa pada groups, tags, proxies, templates, extensions:
-- uuid TEXT UNIQUE, revision INTEGER DEFAULT 0, deleted_at TEXT, origin_device TEXT, sync_dirty INTEGER DEFAULT 1

-- Tabel lokal 1: Konfigurasi dan kursor sinkronisasi
CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Menyimpan: device_id, device_name, server_url, last_pulled_seq, last_sync_at, last_sync_error

-- Tabel lokal 2: Antrean perubahan keluar saat offline/online (Outbox Pattern)
CREATE TABLE IF NOT EXISTS sync_outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    entity_type TEXT NOT NULL,
    entity_uuid TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('upsert', 'soft_delete', 'purge', 'upload_cookie')),
    base_revision INTEGER NOT NULL,
    payload TEXT NOT NULL,
    blob_path TEXT,
    fencing_token INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_outbox_retry ON sync_outbox (next_retry_at, outbox_id);

-- Tabel lokal 3: Cache lease aktif untuk visibilitas UI dan perpanjangan heartbeat
CREATE TABLE IF NOT EXISTS local_leases (
    profile_uuid TEXT PRIMARY KEY,
    holder_device_id TEXT NOT NULL,
    holder_device_name TEXT,
    lease_token TEXT,
    fencing_token INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    is_local_holder INTEGER NOT NULL DEFAULT 0
);

-- Tabel lokal 4: Daftar konflik yang perlu ditinjau pengguna atau log resolusi otomatis
CREATE TABLE IF NOT EXISTS local_conflicts (
    conflict_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_uuid TEXT NOT NULL,
    reason TEXT NOT NULL,
    losing_payload TEXT NOT NULL,
    winning_payload TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
```

## 5. Arsitektur Komponen

### 5.1 Topologi Komponen

Arsitektur dirancang agar server dapat dideploy sebagai satu proses mandiri (binary Python/Go atau satu container Docker) tanpa ketergantungan layanan eksternal, sementara setiap device menjalankan daemon `antidetect serve` lokal.

```text
+-------------------------------------------------------------------------+
| DEVICE A (Linux Desktop, DISPLAY=:0)                                    |
|                                                                         |
|  [UI Vue :5180 / SDK ixBrowser / Selenium]                              |
|         |                                                               |
|         v  (HTTP POST 127.0.0.1:53200/api/v2/<action>)                  |
|  +-------------------------------------------------------------------+  |
|  | antidetect/api.py (Local API Server)                              |  |
|  +--------+------------------------+------------------------+--------+  |
|           |                        |                        |           |
|           v                        v                        v           |
|  +-----------------+    +--------------------+    +------------------+  |
|  | store.py        |    | launcher.py        |    | cookies.py       |  |
|  | (antidetect.db  |    | (Engine 150 + gost |    | (Extract/Inject  |  |
|  |  + sync_outbox) |    |  + Lease Hook)     |    |  JSON <-> SQLite)|  |
|  +--------+--------+    +---------+----------+    +--------+---------+  |
|           |                       |                        |            |
|           +-----------------------+------------------------+            |
|                                   |                                     |
|                                   v                                     |
|  +-------------------------------------------------------------------+  |
|  | antidetect/sync_agent.py (Background Worker, Crypto, Lease Heart) |  |
|  +--------------------------------+----------------------------------+  |
+-----------------------------------|-------------------------------------+
                                    |
                                    | HTTPS / Tailscale (Bearer Token)
                                    | Payload Metadata + AES-256-GCM Blobs
                                    v
+-------------------------------------------------------------------------+
| SERVER SINKRONISASI (Single Container / Binary)                         |
|                                                                         |
|  +-------------------------------------------------------------------+  |
|  | sync_server/http.py (REST API /v1/sync/*, Auth, Rate Limit)       |  |
|  +----------------+---------------------------------+----------------+  |
|                   |                                 |                   |
|                   v                                 v                   |
|  +--------------------------------+   +------------------------------+  |
|  | server_sync.db (SQLite WAL)    |   | Blob Storage Backend         |  |
|  | - devices, sync_profiles, ...  |   | - Local FS (/var/lib/ad/blob)|  |
|  | - change_log (monotonic seq)   |   | - atau S3 / MinIO / R2       |  |
|  | - profile_leases (TTL + fence) |   |   (Content-Addressed Cipher) |  |
|  +--------------------------------+   +------------------------------+  |
+-------------------------------------------------------------------------+
```

### 5.2 Komponen di Device Lokal (`antidetect`)
1. **Lapisan Penyimpanan & Outbox (`antidetect/store.py`)**: Setiap mutasi lokal (`create_profile`, `update_profile`, `soft_delete_profile`, dll.) menulis perubahan ke tabel utama dan menyisipkan baris ke `sync_outbox` di dalam satu transaksi SQLite yang sama. Ini menjamin tidak ada perubahan yang terlewat meskipun aplikasi dimatikan mendadak.
2. **Agen Sinkronisasi Latar Belakang (`antidetect/sync_agent.py`)**:
   * **Sync Loop**: Berjalan pada *daemon thread* setiap 10 detik (atau dipicu seketika lewat `threading.Event` saat ada item baru di `sync_outbox`). Melakukan siklus `push` diikuti `pull`.
   * **Lease Heartbeat Loop**: Berjalan pada thread terpisah setiap 20 detik selama ada profil yang terbuka di `launcher._open`. Memperpanjang TTL lease ke server untuk semua profil aktif.
   * **Crypto Engine**: Melakukan enkripsi dan dekripsi amplop (*envelope encryption*) menggunakan AES-256-GCM pada blob cookie serta field sensitif sebelum meninggalkan device.
3. **Kait Launcher (`antidetect/launcher.py`)**:
   * Sebelum meluncurkan proses Chromium pada `open_profile()`, meminta lease ke server (atau memverifikasi aturan offline) dan memastikan revisi cookie lokal sudah mutakhir.
   * Setelah `_graceful_close()` selesai pada `close_profile()`, mengekstrak cookie dari SQLite Chromium melalui `cookies.get_cookies(profile_id)`, menghitung hash SHA-256, dan bila berubah, mengenkripsi serta mengunggah blob baru sebelum melepas lease.

### 5.3 Komponen di Server Sinkronisasi (`antidetect-sync-server`)
1. **HTTP API Handler**: Menyediakan endpoint `/v1/sync/*`. Memvalidasi header `Authorization: Bearer <device_token>` dengan mencocokkan SHA-256 token terhadap `devices.token_hash`.
2. **Coordinator & Change Log Engine**: Menangani transaksi `push` secara atomik, membandingkan `base_revision` dengan `revision` saat ini, menetapkan nomor urut `change_log.seq` baru yang monotonik, dan mencatat konflik bila diperlukan.
3. **Lease Manager**: Menjaga eksklusivitas buka profil di tabel `profile_leases`. Mengevaluasi kedaluwarsa lease berdasarkan waktu epoch server (`expires_at < current_epoch`) secara transaksional tanpa membutuhkan cron eksternal.
4. **Blob Storage Adapter**: Antarmuka penyimpanan objek sederhana dengan dua implementasi:
   * `LocalFilesystemBlobStore`: Menyimpan file terenkripsi di `<server_data_dir>/blobs/<prefix_2_char>/<blob_id>.bin` menggunakan penulisan file sementara diikuti `os.replace()`.
   * `S3CompatibleBlobStore`: Menyimpan objek ke S3/MinIO/Cloudflare R2 menggunakan kunci `blobs/<profile_uuid>/<blob_id>.bin`, mendukung *pre-signed URL* atau streaming melalui server.

## 6. Protokol dan Algoritma Sinkronisasi

### 6.1 Prinsip Urutan Push-Then-Pull, Batch, Cursor, dan Retry

1. **Urutan Push-Then-Pull**: Setiap siklus sinkronisasi yang berjalan di `sync_agent.py` selalu mengeksekusi **Push** seluruh entri `sync_outbox` terlebih dahulu, baru kemudian mengeksekusi **Pull** perubahan dari server sejak `last_pulled_seq`. Urutan ini mencegah kondisi di mana perubahan lokal yang belum terkirim tertimpa secara prematur oleh state lama dari server, serta memastikan resolusi konflik dilakukan oleh server dengan melihat `base_revision` yang akurat dari klien.
2. **Batching dan Cursor Monotonik**:
   * Operasi `push` mengirim maksimal 100 item `sync_outbox` per permintaan HTTP. Setiap item membawa `idempotency_key` (UUIDv4 yang dibuat saat mutasi lokal masuk ke `sync_outbox`).
   * Operasi `pull` mengirim parameter `since_seq` (integer, default `0`) dan `limit` (default `200`). Server mengembalikan daftar perubahan terurut berdasarkan `change_log.seq ASC`, nilai `next_seq`, dan boolean `has_more`. Selama `has_more == true`, device langsung melakukan `pull` halaman berikutnya hingga tuntas.
   * Koma atau deduplikasi log: bila satu entitas diubah berkali-kali di antara dua siklus `pull`, server dapat mengembalikan hanya baris `change_log` dengan `seq` tertinggi untuk pasangan `(entity_type, entity_uuid)` tersebut guna menghemat bandwidth.
3. **Sifat Idempoten dan Retry Eksponensial**:
   * Server menyimpan cache `idempotency_key` yang sudah diproses selama 24 jam. Jika jaringan putus setelah server memproses `push` namun sebelum respons diterima klien, pengiriman ulang batch yang sama tidak akan menggandakan revisi atau membuat profil duplikat.
   * Jika permintaan gagal karena kesalahan jaringan atau HTTP `5xx`/`429`, item tetap berada di `sync_outbox` dengan penambahan `attempts += 1` dan jadwal `next_retry_at = now + min(300, (2 ** attempts)) + jitter(0..3)` detik.

### 6.2 Algoritma Langkah demi Langkah

#### Alur 1: Aplikasi Start (`python -m antidetect serve`)
1. `store.init()` dijalankan untuk memastikan skema database lokal dan tabel `sync_state`, `sync_outbox`, `local_leases`, serta `local_conflicts` tersedia.
2. Pemeriksaan pemulihan crash lokal (*Crash Recovery*):
   * Sistem memindai seluruh direktori `PROFILES_DIR / <id> / state.json`.
   * Untuk setiap `state.json` yang ditemukan, sistem memeriksa apakah PID Chrome masih hidup dengan `launcher._process_gone(None, pid)` dan memeriksa `SingletonLock` via `_instance_dir_busy()`.
   * Jika proses Chrome sudah mati sementara `state.json` masih ada (pertanda aplikasi atau mesin sebelumnya mati tidak bersih), sistem menandai profil tersebut sebagai `unclean_exit = True`, membersihkan proses `gost` yatim lewat `_stop_gost(info)`, menghapus `state.json`, dan **tidak** menjadwalkan unggah cookie otomatis untuk sesi yang crash tersebut (lihat Bagian 6.3).
3. `sync_agent.start()` membaca `device_id`, `server_url`, dan token dari `sync_state`. Jika sinkronisasi aktif, agen segera memicu satu siklus `Push` diikuti `Pull` awal di latar belakang tanpa memblokir kesiapan port lokal `127.0.0.1:53200`.

#### Alur 2: Tarik Perubahan (Pull dari Server)
1. Agen membaca `last_pulled_seq` dari `sync_state` (default `0`).
2. Agen memanggil `POST /v1/sync/pull` dengan `{"since_seq": last_pulled_seq, "limit": 200}`.
3. Untuk setiap item di `changes` (diurutkan sehingga `group`, `tag`, `proxy`, dan `extension` diterapkan sebelum `profile` agar referensi foreign key valid):
   * Jika `origin_device == local_device_id`, perbarui `revision` lokal dan `last_pulled_seq` jika belum terperbarui, lalu lewati penulisan ulang data.
   * Jika entitas memiliki perubahan lokal yang belum terkirim di `sync_outbox` (`sync_dirty == 1`), tunda penimpaan lokal dan biarkan fase `push` berikutnya menyelesaikan perbandingan revisi di server.
   * Jika `entity_type == 'profile'` dan profil sedang terbuka secara lokal (`launcher.is_open(local_id)`):
     * Metadata non-runtime (`name`, `note`, `color`, `group_id`, `tag_ids`) langsung diperbarui di SQLite lokal.
     * Perubahan `cookie_blob_id` hanya dicatat sebagai `pending_cookie_blob_id` dan baru diterapkan setelah profil ditutup.
   * Jika profil tertutup dan `cookie_blob_id` pada payload server berbeda dari `profiles.cookie_blob_id` lokal, agen dapat mengunduh blob cookie secara *eager* (untuk profil yang baru diubah) atau menandainya untuk diunduh secara *just-in-time* tepat sebelum `profile-open`.
4. Setelah seluruh batch dalam satu halaman selesai ditulis dalam satu transaksi SQLite lokal, `sync_state.last_pulled_seq` diperbarui ke `next_seq`.

#### Alur 3: Buka Profil dan Ambil Lease (`profile-open`)
1. Pengguna atau SDK memanggil `POST /api/v2/profile-open` dengan `{"profile_id": 5}`.
2. `api.py` memuat baris profil dari SQLite lokal, memperoleh `uuid`, `cookie_blob_id`, dan `cookie_revision`.
3. `sync_agent.acquire_lease(profile_uuid)` memanggil `POST /v1/sync/lease/acquire` dengan batas waktu tunggu jaringan singkat (3 detik):
   * Jika server mengembalikan `LEASE_CONFLICT` (profil sedang dipegang oleh Device B dan `expires_at > now`), `profile-open` langsung gagal dengan `ApiError(1009, "Profil sedang dibuka di perangkat <nama_device>")`.
   * Jika server berhasil memberikan lease, server mengembalikan `lease_token`, `fencing_token`, `expires_at`, serta `latest_cookie_blob_id` dan `latest_cookie_revision` terbaru di server.
4. Validasi kemutakhiran cookie sebelum peluncuran:
   * Jika `latest_cookie_revision` di server lebih tinggi daripada `cookie_revision` lokal, agen mengunduh blob `latest_cookie_blob_id` via `POST /v1/sync/blob/download`, mendekripsi dan memverifikasi SHA-256 plaintext, lalu menulisnya ke database `Cookies` lokal menggunakan `cookies.update_cookies(pid, cookie_list)`.
5. `launcher.open_profile(profile)` dijalankan untuk menyalakan `gost` (bila perlu) dan meluncurkan `fingerprint-chromium`.
6. Informasi lease (`lease_token`, `fencing_token`, `expires_at`) disimpan di `local_leases` dan disertakan di `state.json`. Thread heartbeat mulai memperpanjang lease setiap 20 detik.

#### Alur 4: Tutup Profil, Ambil Snapshot Cookie, dan Unggah (`profile-close`)
1. Pengguna atau SDK memanggil `POST /api/v2/profile-close` dengan `{"profile_id": 5}`.
2. `launcher.close_profile(5)` memanggil `_graceful_close(debugging_address, proc, pid)` yang mengirim `GET /json/close/<id>` ke seluruh tab halaman dan menunggu proses Chromium keluar secara bersih agar SQLite `Cookies` selesai di-flush ke disk. Catat boolean `clean_exit` (bernilai `True` jika proses berhenti tanpa harus dipaksa dengan `SIGKILL`).
3. Jika `clean_exit == True`:
   * Panggil `cookies.get_cookies(5)` untuk mengekstrak seluruh daftar cookie ke JSON kanonik (terurut berdasarkan `host_key, name, path`).
   * Hitung `sha256_plain = sha256(canonical_json_bytes)`.
   * Bandingkan dengan `profiles.cookie_sha256` lokal. Jika berbeda:
     * Enkripsi `canonical_json_bytes` dengan AES-256-GCM menghasilkan `cipher_bytes` dan `sha256_cipher`.
     * Simpan file blob sementara di `DATA_DIR / "sync_blobs" / "<blob_id>.enc"` dan masukkan tugas `upload_cookie` beserta `fencing_token` lease saat ini ke `sync_outbox`.
4. Agen segera menjalankan pengiriman blob (`POST /v1/sync/blob/upload`) dengan menyertakan `fencing_token`, diikuti pelepasan lease (`POST /v1/sync/lease/release`). Jika jaringan sedang terputus saat profil ditutup, blob tetap tersimpan di `sync_outbox` untuk diunggah begitu koneksi pulih.

```text
Device A                       Server Sinkronisasi                  Device B
   |                                    |                               |
   |--- 1. lease/acquire (uuid) ------->|                               |
   |<-- lease_token, fencing=42 --------|                               |
   |                                    |<-- 2. lease/acquire (uuid) ---|
   |                                    |--- Error 409 LEASE_HELD ----->|
   |=== Chrome Berjalan di Device A ====|                               |
   |--- 3. lease/renew (every 20s) ---->|                               |
   |=== Graceful Close (/json/close) ===|                               |
   |--- 4. blob/upload (fencing=42) --->|                               |
   |<-- cookie_revision=8, seq=105 -----|                               |
   |--- 5. lease/release (token) ------>|                               |
   |                                    |<-- 6. sync/pull (since=104) --|
   |                                    |--- seq=105, cookie_blob_id -->|
   |                                    |<-- 7. lease/acquire (uuid) ---|
   |                                    |--- lease_token, fencing=43 -->|
   |                                    |<-- 8. blob/download ----------|
   |                                    |=== Chrome Jalan di Device B ==|
```

#### Alur 5: Device Lain Menarik Perubahan
1. Device B menjalankan siklus `pull` berkala atau memicunya saat pengguna menekan tombol segarkan / membuka profil.
2. Device B menerima entri `change_log` dengan `seq = 105` yang memperbarui `cookie_blob_id` dan `cookie_revision = 8` untuk profil tersebut.
3. Saat pengguna di Device B membuka profil tersebut, langkah validasi di Alur 3 mendeteksi bahwa `cookie_revision` perlu diterapkan, mengunduh blob bila belum ada di cache lokal, menjalankan `cookies.update_cookies()`, dan membuka Chrome dengan sesi login yang sama persis seperti saat Device A menutupnya.

#### Alur 6: Konflik dan Penghapusan
1. Jika Device A dan Device B sama-sama mengubah metadata profil dengan `base_revision = 3`:
   * Device A melakukan `push` lebih dulu; server menaikkan `revision` menjadi `4`.
   * Device B melakukan `push` dengan `base_revision = 3`; server mendeteksi bahwa `base_revision (3) < server_revision (4)`.
   * Server menjalankan penggabungan per-field (*field-level merge*) dan aturan *Last-Write-Wins* (LWW) yang didefinisikan pada Bagian 7, menyimpan revisi `5`, mencatat salinan payload yang kalah ke tabel `conflicts`, dan mengembalikan hasil gabungan ke Device B.
2. Untuk penghapusan:
   * `profile-delete` mengirim operasi `soft_delete` (`deleted_at` terisi). Di device lain, profil otomatis berpindah ke tampilan Keranjang Sampah.
   * `profile-delete-permanent` mengirim operasi `purge` (`purged_at` terisi). Di device lain, bila profil tidak sedang terbuka, baris profil dan direktori `profiles/<id>` dihapus dari disk lokal.

#### Alur 7: Device Offline Lalu Online Kembali
1. Saat Device A kehilangan koneksi ke server, semua mutasi profil/grup/tag/proxy/cookie tetap disimpan ke SQLite lokal dengan `sync_dirty = 1` dan masuk ke `sync_outbox`.
2. Jika berulang kali terjadi perubahan pada entitas yang sama di `sync_outbox` selama offline (misalnya pengguna mengedit catatan profil 3 kali), agen melakukan *outbox coalescing*: menggabungkan perubahan menjadi satu entri `upsert` dengan mempertahankan `base_revision` awal sebelum offline.
3. Saat koneksi ke server kembali tersedia:
   * Agen mengeksekusi `push` untuk mengosongkan `sync_outbox` secara berurutan.
   * Jika ada unggahan cookie di `sync_outbox` yang berasal dari profil yang ditutup saat offline, server memeriksa apakah `fencing_token` pada item outbox tersebut masih sama dengan `fencing_token` tertinggi yang pernah diterbitkan untuk profil itu (artinya tidak ada device lain yang mengambil alih dan membuka profil tersebut selama Device A offline). Jika belum ada device lain yang membuka profil itu, unggahan cookie diterima; jika sudah ada device lain yang membuka dan memperbarui cookie di server, unggahan cookie lama dari Device A ditolak sebagai konflik (`STALE_FENCING_TOKEN`) dan disimpan ke cadangan konflik lokal agar tidak menimpa sesi yang lebih baru.
   * Setelah `sync_outbox` bersih, agen mengeksekusi `pull` mulai dari `last_pulled_seq` hingga mencapai `seq` terbaru di server.

### 6.3 Aturan Pembuatan Snapshot, Eksklusi Direktori Profil, dan Penanganan Crash Chrome

1. **Kapan Snapshot Dibuat**:
   * Snapshot cookie **hanya** dibuat pada dua momen: (a) tepat setelah `launcher.close_profile()` berhasil menyelesaikan `_graceful_close()` dan proses Chrome sudah berhenti, atau (b) ketika pengguna memanggil mutasi cookie eksplisit lewat API (`profile-update-cookies`, `profile-create` dengan parameter `cookie`, `profile-update` dengan parameter `cookie`, `profile-import`, `profile-import-ixbrowser`, atau `profile-clear-cache-and-cookies`) saat profil dalam keadaan tertutup.
   * Tidak ada pembacaan file SQLite `Cookies` untuk sinkronisasi selama Chrome masih berjalan.
2. **Apa yang Dieksklusi dari Direktori Profil (`profiles/<id>/`)**:
   * Pada MVP, sinkronisasi sesi menggunakan ekstraksi logis lewat `cookies.get_cookies()` dan injeksi lewat `cookies.update_cookies()`, sehingga file mentah di disk tidak disalin bulat-bulat.
   * Untuk fitur *snapshot* opsional atau fase lanjutan (sinkronisasi bookmark/preferensi/ekstensi), jalur berikut di dalam `profiles/<id>/` **wajib dieksklusi**:
     * Cache besar dan artefak kompilasi: `data/Default/Cache/`, `data/Default/Code Cache/`, `data/Default/GPUCache/`, `data/Default/DawnCache/`, `data/Default/ShaderCache/`, `data/GrShaderCache/`, `data/ShaderCache/`.
     * Penyimpanan transien dan worker: `data/Default/Service Worker/CacheStorage/`, `data/Default/Service Worker/ScriptCache/`, `data/Default/Storage/ext/`, `data/Default/blob_storage/`.
     * File pengunci dan soket OS spesifik mesin: `data/SingletonLock`, `data/SingletonSocket`, `data/SingletonCookie`, `state.json`.
     * File telemetri dan crash: `data/Crashpad/`, `data/BrowserMetrics/`, `chrome.log`, `gost.log`.
     * Direktori instance sekunder sementara: `data-2/`, `data-3/`.
     * File jurnal SQLite mentah tanpa database utamanya: `*-wal`, `*-shm`, `*-journal`.
3. **Menangani Chrome yang Tidak Tertutup Bersih (Crash / SIGKILL / Mati Listrik)**:
   * Ketika `_graceful_close()` gagal menutup tab dan terpaksa memanggil `_kill_pgid(pid)` dengan `SIGKILL`, atau ketika komputer mati mendadak, Chromium mungkin belum mem-flush perubahan cookie dari memori ke disk, atau meninggalkan file `Cookies-journal` / `Cookies-wal` yang belum di-checkpoint.
   * Jika `_graceful_close()` mengembalikan `False` (proses dihentikan paksa dengan `SIGKILL`), agen tetap melepas lease ke server tetapi menandai sesi tersebut dengan flag `dirty_shutdown`. Agen mencoba membuka koneksi SQLite singkat dalam mode baca ke `Default/Network/Cookies` untuk menjalankan `PRAGMA integrity_check(1)`:
     * Jika `integrity_check` mengembalikan `"ok"`, cookie tetap diekstrak dan diunggah.
     * Jika `integrity_check` gagal (`sqlite3.DatabaseError`), unggahan dibatalkan agar file yang rusak tidak menyebar ke server dan merusak device lain; sistem memulihkan dari `Cookies.bak` atau mengunduh ulang blob cookie terakhir yang sehat dari server, serta mencatat peringatan di tabel `events` lokal.

## 7. Penanganan Konflik dan Penghapusan

### 7.1 Strategi Penggabungan Metadata dan Last-Write-Wins (LWW)

Ketika Device B mengirim operasi `upsert` dengan `base_revision < server_revision`, server tidak langsung membuang seluruh baris, melainkan menerapkan resolusi dua tingkat:

1. **Field-Level Merge untuk Atribut Independen**:
   * Server membandingkan payload masuk dengan state server saat ini. Jika Device A hanya mengubah `note` dan `color`, sedangkan Device B hanya mengubah `name` dan `tag_uuids`, kedua perubahan digabungkan tanpa kehilangan data.
   * Untuk field `last_open_time`, server selalu mengambil nilai maksimum: `MAX(server.last_open_time, incoming.last_open_time)`.
2. **Last-Write-Wins (LWW) Berbasis Timestamp + Tie-Breaker Deterministik**:
   * Jika kedua device mengubah atribut yang sama (misalnya keduanya mengubah `name` atau `fingerprint_config`), pemenang ditentukan oleh tuple `(client_updated_at_utc, device_id)` yang lebih besar secara leksikografis.
   * Payload yang kalah tidak hilang: seluruh snapshot JSON yang kalah disimpan ke tabel `conflicts` di server dan dikirim balik di respons `push` agar disimpan ke tabel `local_conflicts` di device untuk peninjauan manual bila diinginkan.
3. **Imutabilitas Seed Fingerprint**:
   * Kolom `seed` dan `fingerprint_config.platform` tidak boleh berubah secara tidak sengaja akibat merge karena perubahan seed akan mengubah sidik jari Canvas/WebGL/GPU yang terlihat oleh situs web target. Perubahan `seed` hanya diterima jika operasi secara eksplisit menandai `seed_reset: true` (dari aksi `profile-random-fingerprint-configuration` atau `profile-open-with-random-fingerprint`).

### 7.2 Aturan Khusus untuk Proxy dan Cookie

1. **Aturan Konflik Proxy (`proxy_config` dan `sync_proxies`)**:
   * `proxy_config` di dalam profil (`proxy_type`, `proxy_ip`, `proxy_port`, `proxy_user`, `proxy_password`) harus diperlakukan sebagai **satu kesatuan atomik**, bukan digabung per sub-field. Menggabungkan `proxy_ip` dari Device A dengan `proxy_port` atau `proxy_password` dari Device B akan menghasilkan konfigurasi proxy yang tidak valid dan gagal terhubung.
   * Oleh karena itu, konflik pada `proxy_config` diselesaikan secara atomik per objek `proxy_config` menggunakan LWW, lalu `proxy_key` dihitung ulang dengan `proxy_key_for(winning_proxy_config)`.
   * Untuk daftar proxy tersimpan (`sync_proxies`), dedup saat sinkronisasi awal menggunakan `proxy_fingerprint`, yaitu `HMAC-SHA256(dedupe_key, canonical_proxy_fields)` dengan `dedupe_key` diturunkan dari `KEK`. Hash MD5 lokal `proxy_key` tidak dikirim ke server karena MD5 dari password proxy dapat diuji secara offline.
2. **Aturan Konflik Cookie (`profile_blobs` jenis `cookies`)**:
   * Cookie sesi autentikasi (seperti token rotasi OAuth/session cookie) sangat sensitif terhadap urutan waktu dan tidak aman digabungkan per baris cookie secara buta jika berasal dari dua sesi browser yang berjalan terpisah, karena satu situs sering menaruh pasangan cookie yang saling bergantung (misalnya `session_id` + `csrf_token` + `device_verifier`).
   * Dalam operasi normal, konflik cookie **dicegah di hulu** oleh mekanisme `profile_leases`: hanya pemegang lease aktif dengan `fencing_token` tertinggi yang boleh mengunggah blob cookie baru.
   * Konflik cookie hanya dapat terjadi apabila:
     * Device A membuka profil dalam mode *offline* (atau lease-nya kedaluwarsa saat jaringan terputus), lalu Device B mengambil lease secara sah, membuka profil, dan mengunggah cookie baru sebelum Device A kembali online.
   * Dalam kondisi tersebut:
     * Server **menolak** unggahan blob cookie dari Device A dengan kode `STALE_FENCING_TOKEN` dan mempertahankan blob cookie milik Device B sebagai pemenang utama (karena sesi Device B adalah sesi terkoordinasi terbaru).
     * Device A menyimpan daftar cookie miliknya yang ditolak ke dalam `local_conflicts` (dan menulis file cadangan lokal `profiles/<id>/cookies-conflict-<timestamp>.json`) serta menampilkan peringatan di UI agar pengguna dapat memilih apakah ingin mempertahankan cookie server atau memulihkan cookie lokal tersebut lewat satu klik.

### 7.3 Kapan Resolusi Manual Diperlukan

Resolusi otomatis (Field-Level Merge + LWW) menangani seluruh konflik metadata rutin tanpa menghentikan alur kerja pengguna. Resolusi manual hanya dipicu dan ditampilkan sebagai lencana (*badge*) peringatan di UI pada tiga skenario:
1. **Konflik Cookie Akibat Sesi Offline Bersamaan (`STALE_FENCING_TOKEN`)**: Pengguna dihadapkan pada pilihan di UI: *"Gunakan Sesi Server (Device B)"* atau *"Timpa dengan Sesi Lokal (Device A)"* atau *"Buat Profil Salinan dari Sesi Lokal"*.
2. **Edit Profil pada Entitas yang Sudah Dihapus Permanen (`PURGE_CONFLICT`)**: Device A mengedit profil saat offline, sementara Device B telah menjalankan `profile-delete-permanent`. Secara default penghapusan permanen menang, tetapi payload lengkap milik Device A disimpan di `local_conflicts` sehingga dapat dipulihkan sebagai profil baru dengan satu aksi.
3. **Perubahan Seed Fingerprint Saat Offline di Dua Device**: Karena seed menentukan identitas perangkat keras di mata situs, jika dua device sama-sama mereset seed saat offline, sistem meminta konfirmasi seed mana yang ingin dikunci.

### 7.4 Siklus Penghapusan: Soft Delete, Restore, dan Tombstone Purge

1. **Soft Delete (Keranjang Sampah)**:
   * Saat `profile-delete` dipanggil, kolom `deleted_at` diisi timestamp UTC dan `revision` dinaikkan.
   * Di semua device, profil tetap ada di tabel `profiles` (muncul di halaman Keranjang Sampah / `profile-recycle-list`).
   * Jika `profile-restore` dipanggil di salah satu device, `deleted_at` dikembalikan ke `NULL` dan `revision` dinaikkan.
2. **Permanent Delete (Purge Tombstone)**:
   * Saat `profile-delete-permanent` atau `empty-recycle-bin` dipanggil, baris di `sync_profiles` **tidak** langsung dihapus dengan `DELETE FROM` dari database server, melainkan kolom `purged_at` diisi timestamp UTC, kolom JSON dikosongkan ke `'{}'`, seluruh blob cookie terkait di `profile_blobs` dihapus dari disk/S3, dan operasi `purge` dicatat ke `change_log`.
   * Ketika device lain menarik entri `purge` tersebut, device tersebut memanggil `store.delete_profile_permanent(conn, local_id)` yang menghapus baris dari tabel `profiles` lokal dan menghapus direktori `PROFILES_DIR / str(local_id)`.
   * Tombstone `purged_at` disimpan di server selama periode retensi 90 hari sebelum dibersihkan oleh tugas *garbage collection*. Jika sebuah device sudah offline lebih dari 90 hari dan mencoba `pull` dengan `since_seq` yang sudah dipangkas, server mengembalikan error `CURSOR_TOO_OLD` yang menginstruksikan device untuk melakukan *full resync* snapshot metadata.

## 8. Kunci Profil Antar Device (Distributed Profile Lease)

### 8.1 Tujuan dan Invarian

Membuka profil anti-detect yang sama secara bersamaan di dua device akan menyebabkan dua hal berbahaya: (1) deteksi anomali oleh situs target karena akun yang sama aktif dari dua sesi TCP/TLS berbeda secara paralel, dan (2) kehilangan cookie karena sesi yang ditutup belakangan akan menimpa sesi yang ditutup lebih dulu. Oleh karena itu, sistem menegakkan invarian: **maksimal satu device yang boleh memegang lease aktif untuk satu `profile_uuid` pada satu waktu**.

### 8.2 Parameter Lease, TTL, dan Heartbeat

| Parameter | Nilai Default | Keterangan |
| --- | --- | --- |
| `LEASE_TTL_SECONDS` | `60` detik | Durasi masa berlaku lease di server sejak `acquire` atau `renew` terakhir. |
| `HEARTBEAT_INTERVAL_SECONDS` | `20` detik | Interval pengiriman `lease/renew` dari device pemegang lease (3 kali kesempatan sebelum TTL habis). |
| `CLOCK_AUTHORITY` | Waktu Server | Seluruh perhitungan `acquired_at`, `renewed_at`, dan `expires_at` menggunakan `int(time.time())` milik server, sehingga kebal terhadap ketidaksinkronan jam (*clock skew*) antar laptop pengguna. |
| `FENCING_TOKEN` | Integer Monotonik | Bertambah `+1` setiap kali lease baru diakuisisi; wajib disertakan saat mengunggah blob cookie. |

### 8.3 Mekanisme Akuisisi, Perpanjangan, dan Pelepasan

1. **Akuisisi (`POST /v1/sync/lease/acquire`)**:
   * Server menjalankan transaksi `BEGIN IMMEDIATE`:
   * Memeriksa baris `profile_leases` untuk `profile_uuid`.
   * Jika baris tidak ada, atau `expires_at <= now_epoch`, atau `holder_device_id == requesting_device_id`:
     * Server membaca `sync_profiles.last_fencing_token`, menaikkannya menjadi `last_fencing_token + 1`, menyimpan nilai baru itu ke `sync_profiles` dan `profile_leases`, membuat `lease_token` acak baru kecuali jika holder sama dan `reacquire == false`, lalu menyetel `expires_at = now_epoch + 60`.
   * Jika baris ada, `holder_device_id != requesting_device_id`, dan `expires_at > now_epoch`:
     * Server menolak permintaan dengan kode `409 LEASE_HELD`, mengembalikan `holder_device_id`, `holder_device_name`, dan sisa waktu `retry_after_seconds = expires_at - now_epoch`.
2. **Perpanjangan (`POST /v1/sync/lease/renew`)**:
   * Thread heartbeat di device mengumpulkan semua `(profile_uuid, lease_token)` dari profil yang masih benar-benar hidup di `launcher.opened_profiles()` (diverifikasi dengan `_process_gone(proc, pid) == False`).
   * Mengirim satu permintaan batch `POST /v1/sync/lease/renew` setiap 20 detik.
   * Jika sebuah proses Chrome ternyata sudah ditutup manual oleh pengguna (dengan menekan tombol X pada jendela browser, bukan lewat tombol Tutup di UI), pendeteksi di thread heartbeat mendeteksi `_process_gone() == True`, tidak memperpanjang lease-nya, melainkan memicu alur ekstraksi cookie pasca-tutup dan memanggil `lease/release`.
3. **Pelepasan (`POST /v1/sync/lease/release`)**:
   * Dipanggil segera setelah profil ditutup dan blob cookie selesai diunggah.
   * Server menghapus baris dari `profile_leases` (atau menyetel `expires_at = now_epoch`) dengan tetap mempertahankan nilai `fencing_token` terakhir pada `sync_profiles` agar tetap monotonik.

### 8.4 Pemulihan Saat Device Mati, Hang, atau Putus Jaringan

1. **Device Mati Mendadak (Kernel Panic / Mati Listrik / Proses Terbunuh)**:
   * Device yang mati berhenti mengirim heartbeat `lease/renew`.
   * Paling lambat dalam **60 detik** (`LEASE_TTL_SECONDS`), `expires_at` di server terlampaui.
   * Device lain dapat langsung membuka profil tersebut secara normal tanpa intervensi manual.
2. **Device Suspend / Laptop Tidur Lalu Bangun Kembali (*Zombie Lease Holder*)**:
   * Skenario: Laptop A membuka profil (`fencing_token = 10`), lalu pengguna menutup layar laptop (suspend selama 30 menit). Setelah 60 detik, lease Laptop A kedaluwarsa. Pengguna pindah ke PC B, membuka profil yang sama (`fencing_token = 11`), bekerja, menutupnya, dan mengunggah cookie (`fencing_token = 11`). Kemudian Laptop A dibuka kembali dan bangun dari tidur.
   * Penanganan di Laptop A saat bangun:
     * Begitu thread heartbeat di Laptop A mencoba mengirim `lease/renew` dengan `lease_token` lama, server menolak dengan kode `409 LEASE_STOLEN` (`current_fencing_token: 11`).
     * Agen di Laptop A menerima sinyal `LEASE_STOLEN`, menutup proses Chrome lokal untuk mencegah penulisan lebih lanjut, membatalkan unggahan cookie otomatis ke jalur utama, menyimpan cookie lokal Laptop A ke berkas cadangan konflik, dan menarik cookie terbaru (`fencing_token = 11`) dari server.
3. **Pelepasan Paksa oleh Pengguna (*Force Takeover*)**:
   * Jika pengguna baru saja mematikan Device A dan ingin langsung membuka profil di Device B tanpa menunggu 60 detik, UI dan API menyediakan parameter `force_lease: true` (setara dengan aksi `profile-open-state-reset` yang sudah ada di `antidetect/api.py:923`).
   * Permintaan `lease/acquire` dengan `force: true` dari device terautentikasi milik pengguna yang sama akan langsung mencabut lease lama, menaikkan `fencing_token`, dan mencatat peristiwa `force_takeover` di `audit_logs`.
4. **Kebijakan Buka Profil Saat Device Offline**:
   * Secara default (`offline_open_policy = "allow_with_warning"`), jika server tidak dapat dihubungi sama sekali (timeout koneksi), device tetap mengizinkan peluncuran Chrome lokal agar pekerjaan pengguna tidak terhenti saat internet/Tailscale gangguan. Device mencatat `base_fencing_token`, yaitu `last_fencing_token` terakhir yang pernah dilihatnya untuk profil itu (0 bila belum pernah ada lease). Saat kembali online, unggahan cookie membawa `base_fencing_token` tersebut dan server hanya menerimanya bila `sync_profiles.last_fencing_token` masih sama. Jika device lain sudah menaikkan token, unggahan ditolak sebagai `STALE_FENCING_TOKEN`. Pengguna juga dapat mengubah konfigurasi ke `strict_lease` bila ingin melarang pembukaan profil tanpa lease server. Jika sinkronisasi belum dikonfigurasi, jalur buka profil tetap berjalan lokal seperti sekarang tanpa lease.

## 9. Keamanan dan Enkripsi

### 9.1 Model Ancaman

Cookie sesi aktif dan kredensial proxy (`proxy_password`) setara dengan akses langsung ke akun pengguna tanpa melewati tantangan login atau 2FA. Ancaman utama yang harus dimitigasi:
1. **Kebocoran Penyimpanan Server / S3**: Jika disk VPS atau bucket S3 tempat server menyimpan blob disadap atau bocor, penyerang tidak boleh bisa membaca isi cookie maupun password proxy.
2. **Pencurian Satu Device**: Jika salah satu laptop hilang atau disusupi, akses device tersebut ke server harus bisa dicabut seketika tanpa merusak device lain.
3. **Kebocoran Tidak Sengaja di Log atau Git**: Nilai cookie, token sinkronisasi, kunci enkripsi, dan kredensial proxy **dilarang keras** pernah ditulis ke `events` database, `chrome.log`, output stdout server, ataupun repositori Git.

### 9.2 Perbandingan Enkripsi At-Rest: Server-Side vs Client-Side Envelope Encryption

| Aspek | Opsi 1: Server-Side Encryption (SSE) | Opsi 2: Client-Side Envelope Encryption (E2EE) |
| --- | --- | --- |
| Lokasi Kunci Dekripsi | Disimpan di memori/konfigurasi server. | Hanya ada di device pengguna (*Sync Passphrase* / Master Key lokal). Server hanya melihat ciphertext. |
| Dampak Jika Server / Bucket S3 Bocor | Penyerang yang menguasai server dapat mendekripsi seluruh cookie dan password proxy. | Penyerang hanya memperoleh ciphertext AES-256-GCM acak tanpa kunci dekripsi. |
| Beban Komputasi | Di server. | Di device (sangat ringan, ekstraksi JSON cookie rata-rata 50 KB sampai 500 KB per profil). |
| Kemudahan Query Metadata di Server | Server bisa membaca semua kolom. | Server tetap membaca UUID, revisi, dan timestamp untuk koordinasi lease/cursor, sementara blob cookie dan kredensial proxy terenkripsi penuh dari sisi klien. |

**Keputusan untuk MVP**: Memilih **Client-Side Envelope Encryption (Opsi 2)** untuk seluruh blob (`profile_blobs`) dan field kredensial sensitif (`proxy_password` di `sync_proxies` dan `sync_profiles.proxy_config`).
* **Alasan**: Tujuan utama pengguna adalah menjadikan server semata-mata sebagai titik simpan dan sinkronisasi. Dengan *Client-Side Envelope Encryption*, meskipun server ditempatkan di VPS murah atau memakai bucket objek pihak ketiga, server bersifat *zero-knowledge* terhadap isi cookie dan password proxy.
* **Skema Kriptografi**:
  1. Pengguna mengatur satu *Sync Master Key* (atau frasa sandi sinkronisasi minimal 12 karakter) saat mengonfigurasi sinkronisasi di setiap device.
  2. Device menurunkan `KEK` (*Key Encryption Key*, 32 byte) menggunakan `PBKDF2-HMAC-SHA256` (600.000 iterasi, atau `scrypt` dari pustaka standar Python `hashlib.scrypt` yang tersedia di Python 3 tanpa paket tambahan) dengan `salt` global 16 byte yang disimpan di profil akun server.
  3. Untuk setiap blob cookie baru, device membangkitkan `DEK` (*Data Encryption Key*, 32 byte acak dari `secrets.token_bytes(32)`) dan `nonce` 12 byte.
  4. Plaintext JSON cookie dikompresi dengan `zlib`/`gzip`, lalu dienkripsi menggunakan **AES-256-GCM** (atau `ChaCha20-Poly1305`) dengan `AAD` (*Associated Authenticated Data*) berisi `f"{profile_uuid}:{blob_kind}:{revision}"`. Ikatan `AAD` ini mencegah serangan penukaran blob (*ciphertext swapping*) antar profil atau antar revisi.
  5. `DEK` dibungkus (*wrapped*) menggunakan `KEK` dengan AES-256-GCM dan disimpan di header blob.
  6. `sync_templates.payload` dan setiap `proxy_password` di dalam `proxy_config` dienkripsi di device sebelum dikirim. Server hanya menyimpan ciphertext, sehingga template yang berisi snapshot proxy tidak membocorkan kredensial.

### 9.3 Autentikasi Token Device, Rotasi, dan Pencabutan

1. **Identitas dan Token per Device**:
   * Setiap device memiliki `device_id` (UUIDv4 yang dibuat saat pertama kali inisialisasi dan disimpan di `sync_state`) serta `device_token` tersendiri (`ad_tok_<43_karakter_base64url>` dari 32 byte `secrets.token_urlsafe(32)`).
   * Pendaftaran device baru menggunakan *Bootstrap Enrollment Token* sekali pakai yang digenerate lewat CLI di server (`antidetect-sync-server token create --name "Laptop Kerja" --ttl 15m`). Server menyimpan hash token di tabel `enrollment_tokens` beserta `expires_at` dan `used_at`; token yang sudah dipakai, kedaluwarsa, atau tidak dikenal ditolak.
   * `kdf_salt` global 16 byte dan nilai uji `key_check` (HMAC dari frasa sandi) disimpan di tabel `server_meta`, bukan di setiap baris device. Nilai ini dipakai untuk memastikan device baru memakai frasa sandi yang sama sebelum mengunduh blob.
   * Server tidak pernah menyimpan `device_token` dalam bentuk plaintext; server hanya menyimpan `SHA-256(device_token)` di kolom `devices.token_hash`.
   * Di device lokal, `device_token` dan `KEK` disimpan di file `~/.local/share/antidetect/sync_credentials.json` dengan izin file ketat `chmod 0600`.
2. **Rotasi dan Pencabutan (*Revocation*)**:
   * Device dapat memutar tokennya secara berkala lewat endpoint `POST /v1/sync/device/rotate-token` yang mengganti `token_hash` di server secara atomik.
   * Jika sebuah device hilang, pengguna menjalankan `antidetect-sync-server device revoke <device_id>` (atau dari UI device lain). Server mengisi `devices.revoked_at`, menghapus seluruh baris di `profile_leases` yang dipegang `device_id` tersebut, dan menolak semua permintaan berikutnya dari token tersebut dengan kode HTTP `401 DEVICE_REVOKED`.

### 9.4 Batas Ukuran dan Siklus Umur Blob (Retention & GC)

1. **Batas Ukuran**:
   * Maksimal ukuran blob `cookies` terkompresi per profil: **10 MB** (dalam praktik, 1.000 cookie hanya berukuran sekitar 80 KB setelah kompresi). Permintaan unggah di atas 10 MB ditolak dengan kode `413 BLOB_TOO_LARGE`.
   * Maksimal ukuran paket `extension_pkg`: **50 MB**.
2. **Umur dan Pemangkasan Blob (*Garbage Collection*)**:
   * Server menyimpan **3 revisi blob cookie terakhir** per profil (`revision` aktif + 2 revisi sebelumnya sebagai cadangan *rollback* jika terjadi korupsi sesi).
   * Revisi yang lebih tua dari 3 versi terakhir dan berusia lebih dari 7 hari dihapus otomatis dari tabel `profile_blobs` dan dari penyimpanan objek lokal/S3 oleh tugas pembersih harian di server.
   * Saat profil dihapus permanen (`purge`), seluruh blob milik `profile_uuid` tersebut langsung dihapus seketika.

### 9.5 Keamanan Jaringan dan Audit Log

1. **Transport dan Jaringan**:
   * Server sinkronisasi hanya mendengarkan di antarmuka Tailscale (`100.x.y.z`) atau loopback di belakang reverse proxy (Caddy/Nginx) dengan TLS 1.3 wajib.
   * API lokal `antidetect` (`127.0.0.1:53200`) tetap hanya mendengarkan di loopback lokal tanpa perubahan perilaku untuk menjaga kompatibilitas SDK `ixbrowser-local-api`.
2. **Audit Log Server**:
   * Setiap aksi autentikasi, pendaftaran device, pencabutan token, akuisisi/pelepasan paksa lease, unggah/unduh blob, dan penghapusan permanen dicatat ke tabel `audit_logs` (`device_id`, `action`, `target_uuid`, `ip_address`, `status_code`, `created_at`).
   * Audit log tidak pernah mencatat isi payload cookie, password proxy, maupun token.

### 9.6 Jalur Pengembangan Pasca-MVP untuk Password dan Site Data

Password (`Default/Login Data`) dan Site Data (`Default/Local Storage/leveldb`, `IndexedDB`) sengaja dikeluarkan dari MVP karena alasan keamanan dan teknis berikut, beserta rencana pengembangannya di Fase 5:
1. **Password Tersimpan (`Login Data`)**:
   * Saat ini `antidetect/passwords.py` baru memiliki fungsi baca (`list_saved_passwords`) dan hapus (`delete_password`), tetapi belum memiliki fungsi tulis/impor yang merekonstruksi tabel `logins` beserta seluruh 30+ kolom skema Chromium.
   * Jalur pengembangan: Menambahkan fungsi `update_passwords(profile_id, items)` di `antidetect/passwords.py` yang mengenkripsi `password_value` dengan `cookies._cbc_encrypt(raw)` (`v10`) saat profil tertutup, lalu membungkusnya sebagai `blob_kind = 'passwords'` dengan enkripsi amplop AES-256-GCM terpisah yang membutuhkan konfirmasi eksplisit di pengaturan sinkronisasi.
2. **Site Data (`Local Storage` & `IndexedDB`)**:
   * Banyak situs modern (misalnya WhatsApp Web, Telegram Web, Discord) menyimpan token sesi di `Local Storage` atau `IndexedDB` berbasis LevelDB, bukan hanya di `Cookies`.
   * Jalur pengembangan: Menambahkan `blob_kind = 'site_storage'` yang mengarsipkan secara selektif direktori `Default/Local Storage/leveldb` dan `Default/Session Storage` (setelah Chrome tertutup bersih dan file `LOCK` dilepas) ke dalam arsip `.tar.zst` terenkripsi dengan batas ukuran 25 MB per profil.

## 10. API Server dan Kontrak Payload

Seluruh endpoint server menggunakan prefix `/v1/sync/`, metode `POST`, header `Content-Type: application/json`, dan header `Authorization: Bearer <device_token>` (kecuali `device/register` yang memakai bootstrap token). Semua respons menggunakan amplop konsisten:

```json
{
  "error": { "code": 0, "message": "success" },
  "data": { ... }
}
```

### 10.1 Daftar Kode Error Server

| Kode | Nama | HTTP Status | Arti dan Tindakan Klien |
| --- | --- | --- | --- |
| `0` | `OK` | 200 | Berhasil. |
| `2001` | `UNAUTHORIZED` | 401 | Token tidak valid atau header Authorization kosong. Hentikan sync dan minta login ulang. |
| `2002` | `DEVICE_REVOKED` | 401 | Device telah dicabut aksesnya. Hentikan sync permanen. |
| `2003` | `INVALID_PAYLOAD` | 400 | Format JSON atau field wajib tidak valid. Jangan di-retry otomatis. |
| `2004` | `LEASE_HELD` | 409 | Profil sedang dibuka oleh device lain. Tampilkan nama device pemegang ke pengguna. |
| `2005` | `LEASE_STOLEN` | 409 | Lease lokal sudah kedaluwarsa dan diambil alih device lain. Tutup Chrome lokal dan simpan cadangan. |
| `2006` | `STALE_FENCING_TOKEN` | 409 | Unggahan blob memakai `fencing_token` lebih rendah daripada token terbaru di server. Simpan ke `local_conflicts`. |
| `2007` | `REVISION_CONFLICT` | 409 | Konflik revisi yang tidak bisa digabung otomatis. |
| `2008` | `BLOB_NOT_FOUND` | 404 | Blob ID tidak ditemukan di penyimpanan objek. |
| `2009` | `BLOB_HASH_MISMATCH` | 422 | SHA-256 ciphertext yang diterima tidak cocok dengan header. Ulangi unggahan. |
| `2010` | `BLOB_TOO_LARGE` | 413 | Ukuran blob melebihi batas (10 MB untuk cookie). |
| `2011` | `CURSOR_TOO_OLD` | 410 | `since_seq` sudah dipangkas (>90 hari). Lakukan penarikan snapshot penuh (`since_seq = 0`). |

### 10.2 Kontrak Operasi Endpoint Server

#### 1. Register Device (`POST /v1/sync/device/register`)
* **Sifat Idempoten**: Idempoten berdasarkan `device_id` selama `enrollment_token` masih berlaku.
* **Request**:
```json
{
  "enrollment_token": "ad_enroll_9f8a7b6c5d4e3f2a1b",
  "device_id": "d4f1a2b3-1111-4222-8333-abcdef123456",
  "device_name": "ThinkPad-X1-Linux",
  "platform": "linux",
  "app_version": "0.1.0"
}
```
* **Response**:
```json
{
  "error": { "code": 0, "message": "success" },
  "data": {
    "device_id": "d4f1a2b3-1111-4222-8333-abcdef123456",
    "device_token": "ad_tok_X9kLmP2qR7vW4nJ8sT1yB6cF3hG5zD0aE",
    "kdf_salt_hex": "7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d",
    "server_time": 1793001600,
    "current_seq": 104
  }
}
```

#### 2. Push Perubahan (`POST /v1/sync/push`)
* **Sifat Idempoten**: Idempoten per item melalui `idempotency_key`.
* **Request**:
```json
{
  "mutations": [
    {
      "idempotency_key": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
      "entity_type": "profile",
      "entity_uuid": "a1b2c3d4-5555-4666-9777-000000000005",
      "operation": "upsert",
      "base_revision": 3,
      "client_updated_at": "2026-10-26T08:15:00Z",
      "payload": {
        "name": "Tokopedia Seller 01",
        "group_uuid": "00000000-0000-4000-8000-000000000001",
        "color": "#409EFF",
        "note": "Akun utama cabang Jakarta",
        "site_id": 6,
        "site_url": "https://seller.tokopedia.com",
        "proxy_config": {
          "proxy_type": "socks5",
          "proxy_ip": "203.0.113.10",
          "proxy_port": 1080,
          "proxy_user": "user01",
          "proxy_password_enc": "v1:gcm:..."
        },
        "proxy_fingerprint": "b1946ac92492d2347c6235b4d2611184a1b2c3d4e5f60718293a4b5c6d7e8f90",
        "fingerprint_config": {
          "platform": "Windows",
          "system_version": "11",
          "hardware_concurrency": 8,
          "device_memory": 8,
          "language": "id-ID",
          "timezone": "Asia/Jakarta"
        },
        "preference_config": { "window_width": 1280, "window_height": 800 },
        "tag_uuids": ["f0000000-0000-4000-8000-000000000010"],
        "seed": 1000000005,
        "last_open_time": 1793001200,
        "deleted_at": null
      }
    }
  ]
}
```
* **Response**:
```json
{
  "error": { "code": 0, "message": "success" },
  "data": {
    "results": [
      {
        "idempotency_key": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "entity_uuid": "a1b2c3d4-5555-4666-9777-000000000005",
        "status": "applied",
        "new_revision": 4,
        "seq": 105,
        "conflict_id": null,
        "merged_payload": null
      }
    ]
  }
}
```

#### 3. Pull Perubahan Sejak Cursor (`POST /v1/sync/pull`)
* **Sifat Idempoten**: Operasi baca murni, aman diulang kapan saja dengan `since_seq` yang sama.
* **Request**:
```json
{
  "since_seq": 104,
  "limit": 200
}
```
* **Response**:
```json
{
  "error": { "code": 0, "message": "success" },
  "data": {
    "changes": [
      {
        "seq": 105,
        "entity_type": "profile",
        "entity_uuid": "a1b2c3d4-5555-4666-9777-000000000005",
        "operation": "upsert",
        "revision": 4,
        "origin_device": "d4f1a2b3-1111-4222-8333-abcdef123456",
        "changed_at": "2026-10-26T08:15:01Z",
        "payload": {
          "name": "Tokopedia Seller 01",
          "group_uuid": "00000000-0000-4000-8000-000000000001",
          "cookie_blob_id": "blob_01jc8x9m2n",
          "cookie_revision": 8,
          "seed": 1000000005,
          "deleted_at": null
        }
      }
    ],
    "active_leases": [
      {
        "profile_uuid": "b2c3d4e5-6666-4777-8888-000000000009",
        "holder_device_id": "e5f6a7b8-2222-4333-9444-111111222222",
        "holder_device_name": "PC-Rumah",
        "fencing_token": 19,
        "expires_at": 1793001660
      }
    ],
    "next_seq": 105,
    "has_more": false
  }
}
```

#### 4. Ambil Lease (`POST /v1/sync/lease/acquire`)
* **Sifat Idempoten**: Jika dipanggil ulang oleh `device_id` yang sedang memegang lease aktif yang sama dengan `force: false`, server memperpanjang TTL dan mengembalikan token yang sama.
* **Request**:
```json
{
  "profile_uuid": "a1b2c3d4-5555-4666-9777-000000000005",
  "force": false
}
```
* **Response (Berhasil)**:
```json
{
  "error": { "code": 0, "message": "success" },
  "data": {
    "profile_uuid": "a1b2c3d4-5555-4666-9777-000000000005",
    "lease_token": "ls_4f9d2a1c8b7e6f5a",
    "fencing_token": 42,
    "expires_at": 1793001660,
    "latest_cookie_blob_id": "blob_01jc8x9m2n",
    "latest_cookie_revision": 8
  }
}
```
* **Response (Gagal karena Dipegang Device Lain)**:
```json
{
  "error": {
    "code": 2004,
    "message": "Profil sedang dibuka di perangkat PC-Rumah"
  },
  "data": {
    "holder_device_id": "e5f6a7b8-2222-4333-9444-111111222222",
    "holder_device_name": "PC-Rumah",
    "expires_at": 1793001645,
    "retry_after_seconds": 45
  }
}
```

#### 5. Perpanjang Lease (`POST /v1/sync/lease/renew`)
* **Sifat Idempoten**: Idempoten; menyetel ulang `expires_at = server_now + 60`.
* **Request**:
```json
{
  "leases": [
    {
      "profile_uuid": "a1b2c3d4-5555-4666-9777-000000000005",
      "lease_token": "ls_4f9d2a1c8b7e6f5a",
      "fencing_token": 42
    }
  ]
}
```
* **Response**:
```json
{
  "error": { "code": 0, "message": "success" },
  "data": {
    "renewed": [
      {
        "profile_uuid": "a1b2c3d4-5555-4666-9777-000000000005",
        "status": "ok",
        "expires_at": 1793001680
      }
    ]
  }
}
```

#### 6. Lepas Lease (`POST /v1/sync/lease/release`)
* **Sifat Idempoten**: Idempoten; jika lease sudah tidak ada atau sudah dilepas sebelumnya, tetap mengembalikan `code: 0`.
* **Request**:
```json
{
  "profile_uuid": "a1b2c3d4-5555-4666-9777-000000000005",
  "lease_token": "ls_4f9d2a1c8b7e6f5a"
}
```
* **Response**:
```json
{
  "error": { "code": 0, "message": "success" },
  "data": { "released": true }
}
```

#### 7. Unggah Blob (`POST /v1/sync/blob/upload`)
* **Sifat Idempoten**: Idempoten berdasarkan `(profile_uuid, blob_kind, sha256_plain, fencing_token)`. Jika blob dengan hash plaintext yang sama sudah menjadi revisi aktif, server langsung mengembalikan `blob_id` yang ada tanpa menyimpan duplikat.
* **Request**:
```json
{
  "profile_uuid": "a1b2c3d4-5555-4666-9777-000000000005",
  "blob_kind": "cookies",
  "fencing_token": 42,
  "encryption_scheme": "aes-256-gcm-v1",
  "sha256_plain": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "sha256_cipher": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
  "size_bytes": 18432,
  "ciphertext_base64": "base64_encoded_envelope_header_and_aes_gcm_ciphertext..."
}
```
* **Response**:
```json
{
  "error": { "code": 0, "message": "success" },
  "data": {
    "blob_id": "blob_01jc8x9m2n",
    "profile_uuid": "a1b2c3d4-5555-4666-9777-000000000005",
    "cookie_revision": 8,
    "seq": 106
  }
}
```

#### 8. Unduh Blob (`POST /v1/sync/blob/download`)
* **Sifat Idempoten**: Operasi baca murni.
* **Request**:
```json
{
  "blob_id": "blob_01jc8x9m2n"
}
```
* **Response**:
```json
{
  "error": { "code": 0, "message": "success" },
  "data": {
    "blob_id": "blob_01jc8x9m2n",
    "profile_uuid": "a1b2c3d4-5555-4666-9777-000000000005",
    "blob_kind": "cookies",
    "revision": 8,
    "encryption_scheme": "aes-256-gcm-v1",
    "sha256_plain": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "sha256_cipher": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "ciphertext_base64": "base64_encoded_envelope_header_and_aes_gcm_ciphertext..."
  }
}
```

#### 9. Resolusi Konflik (`POST /v1/sync/conflict/resolve`)
* **Sifat Idempoten**: Idempoten berdasarkan `conflict_id`.
* **Request**:
```json
{
  "conflict_id": "conf_778899aabb",
  "resolution": "keep_losing",
  "override_payload": null
}
```
* **Response**:
```json
{
  "error": { "code": 0, "message": "success" },
  "data": {
    "conflict_id": "conf_778899aabb",
    "resolved": true,
    "new_revision": 6,
    "seq": 107
  }
}
```

## 11. Perubahan yang Dibutuhkan di Aplikasi Lokal

Agar kode yang sudah ada tetap bersih dan kontrak SDK ixBrowser tidak terganggu, perubahan di aplikasi lokal dibagi menurut modul:

### 11.1 `antidetect/store.py`
1. **Migrasi Skema di `init()`**:
   * Menambahkan kolom `uuid`, `revision`, `origin_device`, `purged_at`, `cookie_blob_id`, `cookie_revision`, `cookie_sha256`, dan `sync_dirty` pada `profiles`, serta kolom `uuid`, `revision`, `deleted_at`, `origin_device`, dan `sync_dirty` pada `groups`, `tags`, `proxies`, `templates`, dan `extensions`.
   * Membuat tabel `sync_state`, `sync_outbox`, `local_leases`, dan `local_conflicts`.
2. **Sentralisasi CRUD Grup, Tag, dan Proxy**:
   * Saat ini query `INSERT/UPDATE/DELETE` untuk `groups`, `tags`, dan `proxies` masih tersebar di dalam `antidetect/api.py` (misalnya `group_create` di baris 1125, `tag_create` di baris 1216, `proxy_update` di baris 1346). Query tersebut dipindahkan menjadi fungsi di `store.py` (`create_group`, `update_group`, `delete_group`, `create_tag`, `update_tag`, `delete_tag`, `update_proxy`, `delete_proxy`).
3. **Pencatatan Outbox Transaksional**:
   * Setiap fungsi mutasi di `store.py` otomatis mengisi `uuid` (bila baru), menyetel `sync_dirty = 1`, dan memanggil `_enqueue_outbox(conn, entity_type, entity_uuid, operation, payload)` dalam satu transaksi sebelum `conn.commit()`.
   * Menyediakan fungsi `apply_remote_change(conn, change)` yang menulis data hasil `pull` dari server dengan menyetel `sync_dirty = 0` tanpa memicu penambahan ke `sync_outbox` (mencegah *infinite sync loop*).

### 11.2 `antidetect/launcher.py`
1. **Hook Akuisisi Lease dan Tarik Cookie pada `open_profile()`**:
   * Sebelum menyalakan `gost` atau memanggil `build_args()`, memanggil `sync_agent.prepare_profile_open(profile)` yang mengambil lease ke server dan memperbarui SQLite `Cookies` lokal jika `latest_cookie_revision` di server lebih baru.
2. **Pelacakan Kebersihan Tutup Proses pada `close_profile()`**:
   * Mengubah `close_profile(profile_id)` agar membedakan apakah proses Chrome keluar melalui `_graceful_close()` (`clean_exit = True`) atau melalui `_kill_pgid()` (`clean_exit = False`).
   * Memanggil `sync_agent.on_profile_closed(profile_id, clean_exit=clean_exit)` setelah `_stop_gost()` dan `_clear_state()`.
3. **Deteksi Tutup Jendela Manual**:
   * Saat pengguna menutup jendela Chrome secara langsung dari desktop (`DISPLAY=:0`) tanpa memanggil `POST /api/v2/profile-close`, proses Chrome sudah berhenti dengan bersih karena penutupan jendela X11 memicu siklus shutdown normal Chromium. Thread pemantau di `sync_agent.py` mendeteksi `_process_gone() == True` pada entri `_open`, memanggil ekstraksi cookie, mengunggah blob, membersihkan `_open[profile_id]`, dan melepas lease.

### 11.3 `antidetect/cookies.py`
1. **Kanonikalisasi dan Hash Cookie**:
   * Menambahkan fungsi `cookie_digest(profile_id)` atau mengembalikan tuple `(cookies_json, sha256_hex)` dengan urutan baris yang deterministik (`ORDER BY host_key, name, path`) dan tanpa menyertakan timestamp akses internal (`last_access_utc`) di dalam hash, sehingga pembukaan profil yang tidak mengubah cookie tidak memicu unggahan blob sia-sia.
2. **Pemeriksaan Integritas Pasca-Crash**:
   * Menambahkan fungsi `verify_cookie_db(profile_id)` yang mengeksekusi `PRAGMA quick_check` pada file SQLite `Cookies` sebelum diekstrak.

### 11.4 Modul Baru: `antidetect/sync_agent.py` dan `antidetect/sync_crypto.py`
1. `antidetect/sync_crypto.py`: Mengimplementasikan penurunan kunci `scrypt`/`PBKDF2`, pembungkusan `DEK`, serta enkripsi dan dekripsi `AES-256-GCM` untuk blob dan kredensial proxy tanpa bergantung pada subproses `openssl` eksternal (atau menggunakan paket `cryptography` yang sudah ada di `./venv`).
2. `antidetect/sync_agent.py`: Mengelola thread sinkronisasi latar belakang, thread heartbeat lease, antrean `sync_outbox`, klien HTTP ke `/v1/sync/*` dengan `urllib.request`, serta penanganan mode offline.

### 11.5 `antidetect/api.py` dan Frontend `ui/`
1. **Action Lokal Baru di `antidetect/api.py`** (untuk dikonsumsi UI tanpa mengubah action SDK ixBrowser):
   * `sync-status`: Mengembalikan status koneksi server, `device_id`, `device_name`, jumlah item di `sync_outbox`, `last_sync_at`, daftar lease aktif, dan jumlah konflik yang belum diselesaikan.
   * `sync-configure`: Menyimpan `server_url`, `enrollment_token`, `device_name`, dan *Sync Master Passphrase*, lalu memanggil `device/register`.
   * `sync-trigger`: Memicu siklus `push` + `pull` seketika.
   * `sync-conflicts` dan `sync-conflict-resolve`: Menampilkan dan menyelesaikan konflik dari UI.
   * Memperluas respons `profile-list` dengan atribut opsional `sync_lease` (`null` jika bebas, atau `{"holder_device_name": "PC-Rumah", "is_local": false, "expires_at": ...}`) dan `cookie_revision`.
2. **Perubahan Minimal di `ui/`**:
   * Pada Fase 1 (MVP awal), konfigurasi sinkronisasi dapat diatur sepenuhnya lewat CLI (`python -m antidetect sync setup ...`) dan variabel lingkungan tanpa wajib merombak UI.
   * Pada Fase selanjutnya, menambahkan indikator status sinkronisasi di topbar `App.vue`, badge *"Dibuka di <nama device>"* beserta tombol *"Ambil Alih Kunci"* pada baris profil di `ui/src/views/Profiles.vue`, dan kartu Konfigurasi Sinkronisasi di `ui/src/views/Settings.vue`.

## 12. Migrasi Data Lama

Pengguna saat ini sudah memiliki database produksi di `~/.local/share/antidetect/antidetect.db` (misalnya hasil impor 200 profil dan 12.009 cookie dari arsip ekspor ixBrowser) serta direktori profil di `~/.local/share/antidetect/profiles/<id>/data`. Migrasi harus berjalan tanpa merusak profil yang sudah ada dan tanpa mengubah `profile_id` integer lokal.

### 12.1 Langkah Migrasi Skema dan Backfill Lokal

Saat `store.init()` dijalankan pada pembaruan pertama yang memuat fitur sinkronisasi:
1. **Backup Otomatis Sebelum Migrasi**:
   * Sebelum menjalankan `ALTER TABLE`, jika kolom `uuid` belum ada di tabel `profiles`, `store.init()` membuat salinan utuh `antidetect.db` ke `antidetect.db.pre-sync-<YYYYMMDD-HHMMSS>.bak` menggunakan `sqlite3.Connection.backup()`.
2. **Penambahan Kolom dan Tabel Baru**:
   * Menambahkan kolom sinkronisasi pada `profiles`, `groups`, `tags`, `proxies`, `templates`, dan `extensions`.
   * Membuat tabel `sync_state`, `sync_outbox`, `local_leases`, dan `local_conflicts`.
3. **Penetapan Identitas Device (`device_id`)**:
   * Jika `sync_state` belum memiliki `device_id`, sistem membangkitkan UUIDv4 baru dan menyetel `device_name` dari `socket.gethostname()`.
4. **Backfill UUID Deterministik vs UUIDv4 Acak**:
   * Grup `Default` (`id = 1` atau `lower(title) = 'default'`) selalu diberi UUID tetap: `00000000-0000-4000-8000-000000000001`.
   * Grup lain diberi UUIDv5 deterministik dari namespace grup dan judul terknormalisasi: `uuid.uuid5(GROUP_NS, title.strip().lower())`. Dengan cara ini, jika Device A dan Device B sebelum disinkronkan sama-sama sudah memiliki grup bernama `"Shopee"`, keduanya menghasilkan `group_uuid` yang sama dan otomatis menyatu saat sinkronisasi pertama.
   * Tag diberi UUIDv5 deterministik berdasarkan judul: `uuid.uuid5(TAG_NS, title.strip().lower())`.
   * Proxy di tabel `proxies` diberi UUIDv4 lokal. Dedupe lintas device dilakukan saat sync memakai `proxy_fingerprint` (HMAC-SHA256 dengan `dedupe_key`), bukan UUID deterministik dari MD5 password.
   * Profil di tabel `profiles` diberi UUIDv4 baru (`str(uuid.uuid4())`), `revision = 0`, `origin_device = device_id`, dan `sync_dirty = 1`.
5. **Pengantrean Awal (*Initial Seeding*)**:
   * Saat pengguna mengaktifkan sinkronisasi untuk pertama kalinya pada Device Pertama (Primary Seed Device), seluruh baris yang memiliki `revision == 0` dan `sync_dirty == 1` dimasukkan ke `sync_outbox`:
     * Metadata grup, tag, proxy, dan profil masuk sebagai operasi `upsert` dengan `base_revision = 0`.
     * Untuk setiap profil yang belum dihapus permanen dan memiliki file `Cookies` di disk yang tidak kosong, agen mengekstrak cookie lewat `cookies.get_cookies(pid)`, menghitung `cookie_sha256`, mengenkripsi blob, dan menjadwalkan `upload_cookie` secara bertahap (maksimal 10 blob per batch agar tidak membebani CPU/jaringan sekaligus).

### 12.2 Penanganan Kasus Dua Device yang Sudah Punya Profil Sebelum Sync

Jika pengguna sebelumnya sudah menyalin database atau mengimpor ZIP ixBrowser yang sama di dua laptop berbeda (Device A dan Device B) sebelum server sinkronisasi dipasang, lalu mengaktifkan sync di keduanya:
1. **Rekomendasi Utama (Bootstrap dari Device Primer)**: Device A melakukan `push` awal ke server hingga tuntas. Di Device B, sebelum `push` dijalankan, CLI menyediakan perintah `python -m antidetect sync link-existing` yang melakukan `pull` daftar profil dari server lebih dulu, lalu mencocokkan profil lokal berdasarkan pasangan `(name, seed)` atau `(name, site_url, proxy_fingerprint)`.
2. **Rekonsiliasi Tanpa Duplikasi**:
   * Jika profil di Device B memiliki `(name, seed)` yang identik dengan profil yang sudah ada di server, Device B mengadopsi `uuid` milik server untuk baris `id` lokal tersebut, bukan membuat profil ganda.
   * Untuk cookie pada profil yang beririsan tersebut, sistem membandingkan timestamp `last_open_time` lokal vs server: versi dari device yang terakhir kali membuka profil tersebut dijadikan revisi aktif, sedangkan versi lainnya disimpan ke `local_conflicts` sebagai cadangan.

## 13. Rencana Bertahap dengan Kriteria Penerimaan

Implementasi dibagi menjadi 5 fase berurutan. **Fase 1 dan Fase 2 membentuk MVP awal** (metadata + grup + lease), diikuti **Fase 3 untuk melengkapi MVP penuh dengan sinkronisasi cookie**. Password dan Site Data berada di Fase 5 (pasca-MVP).

### Fase 1: Pondasi Skema, Server Sinkronisasi Dasar, dan Sync Metadata via CLI (Tanpa Perubahan UI Besar)
* **Tujuan**: Membangun layanan `antidetect-sync-server`, migrasi skema lokal (`uuid`, `revision`, `sync_outbox`), enkripsi kredensial proxy, dan sinkronisasi dua arah untuk metadata `profiles`, `groups`, `tags`, `proxies`, dan `templates` yang dikonfigurasi sepenuhnya lewat CLI/env tanpa merombak UI Vue.
* **Pekerjaan**:
  1. Membuat paket/modul `antidetect/sync_server.py` (atau subperintah `python -m antidetect sync-server`) dengan database SQLite WAL `server_sync.db` dan endpoint `device/register`, `push`, dan `pull`.
  2. Memperbarui `antidetect/store.py` untuk migrasi otomatis kolom `uuid`, `revision`, `sync_outbox`, `sync_state`, serta memindahkan SQL langsung grup/tag/proxy dari `api.py` ke fungsi `store.py`.
  3. Membuat `antidetect/sync_crypto.py` dan `antidetect/sync_agent.py` untuk siklus Push-Then-Pull, LWW field-level merge, dan tombstone `purged_at`.
  4. Menambahkan subperintah CLI `python -m antidetect sync init`, `sync status`, dan `sync now`.
* **Kriteria Penerimaan yang Bisa Diuji**:
  1. Database lama dengan 200 profil berhasil dimigrasi otomatis tanpa mengubah `profile_id` integer lokal, dan `antidetect.db.pre-sync-*.bak` terbentuk.
  2. Dua instance lokal dengan `ANTIDETECT_HOME` berbeda yang terhubung ke server uji dapat menyinkronkan pembuatan, pengeditan, pemindahan ke keranjang sampah, pemulihan, dan penghapusan permanen profil, grup, tag, serta proxy dalam waktu kurang dari 2 detik saat `sync now` dipanggil.
  3. Seluruh tes regresi SDK ixBrowser (`scripts/selftest.py` dan `scripts/test-ixbrowser-import.py`) tetap lulus 100 persen tanpa perubahan kode tes.
* **Perkiraan Usaha**: 4 hari kerja.
* **Risiko Utama**: Mutasi grup/tag/proxy di `api.py` terlewat belum melewati `store.py` sehingga tidak tercatat ke `sync_outbox`. Mitigasi: audit seluruh pemanggilan `conn.execute` di `antidetect/api.py` dengan `rg`.

### Fase 2: Distributed Profile Lease dan Proteksi Buka Bersamaan
* **Tujuan**: Menjamin satu profil hanya dapat dibuka di satu device pada satu waktu melalui lease berbasis TTL, perpanjangan heartbeat, dan `fencing_token`.
* **Pekerjaan**:
  1. Mengimplementasikan tabel `profile_leases` dan endpoint `lease/acquire`, `lease/renew`, serta `lease/release` di server.
  2. Memasang hook di `antidetect/launcher.py` (`open_profile`, `close_profile`, `forget`) dan `antidetect/api.py` (`profile-open`, `profile-open-with-random-fingerprint`, `profile-close`, `profile-open-state-reset`).
  3. Menambahkan thread heartbeat 20 detik di `sync_agent.py` yang juga mendeteksi penutupan jendela Chrome manual (`_process_gone`).
* **Kriteria Penerimaan yang Bisa Diuji**:
  1. Saat Device A memegang lease aktif pada profil X, panggilan `profile-open` untuk profil X di Device B ditolak dengan kode error `1009` dan pesan yang menyebut nama Device A.
  2. Saat proses Device A dihentikan paksa (`kill -9`) tanpa melepas lease, Device B dapat membuka profil X setelah TTL 60 detik berakhir.
  3. Panggilan `profile-open` dengan `force_lease: true` (atau setelah `profile-open-state-reset`) berhasil mengambil alih lease dan menaikkan `fencing_token`.
* **Perkiraan Usaha**: 2,5 hari kerja.
* **Risiko Utama**: Proses Chrome ditutup langsung dari tombol X jendela desktop sehingga `profile-close` API tidak terpanggil dan lease menggantung selama 60 detik. Mitigasi: thread heartbeat memeriksa `launcher._process_gone()` setiap 5 detik untuk segera melepas lease begitu proses Chrome berhenti.

### Fase 3: Sinkronisasi Cookie Terenkripsi (Penyelesaian MVP)
* **Tujuan**: Menyinkronkan cookie Chromium secara otomatis dan aman (enkripsi klien AES-256-GCM) saat profil ditutup bersih, serta mengunduhnya secara *just-in-time* sebelum profil dibuka di device lain.
* **Pekerjaan**:
  1. Mengimplementasikan `LocalFilesystemBlobStore` dan opsional `S3CompatibleBlobStore` di server beserta endpoint `blob/upload`, `blob/download`, dan retensi 3 revisi terakhir.
  2. Menambahkan fungsi digest kanonik dan `verify_cookie_db()` di `antidetect/cookies.py`.
  3. Mengintegrasikan ekstraksi + enkripsi + unggah cookie saat `close_profile()` (`clean_exit == True`) dan saat `profile-update-cookies` dipanggil.
  4. Mengintegrasikan unduh + verifikasi SHA-256 + dekripsi + `cookies.update_cookies()` sebelum Chrome diluncurkan pada `open_profile()`.
  5. Menangani penolakan `STALE_FENCING_TOKEN` dengan menyimpan cadangan cookie konflik ke `local_conflicts`.
* **Kriteria Penerimaan yang Bisa Diuji**:
  1. Cookie yang ditulis di Device A (termasuk cookie terenkripsi `v10` dengan domain, path, expires, httpOnly, secure, sameSite) muncul identik byte-for-byte (diverifikasi dengan SHA-256 nilai cookie) di Device B setelah profil ditutup di Device A dan dibuka di Device B.
  2. Isi file blob di disk server terbukti terenkripsi (tidak memuat string plaintext nama domain atau nilai cookie) dan gagal didekripsi bila `AAD` atau passphrase diubah.
  3. Unggahan blob dengan `fencing_token` usang ditolak oleh server dengan kode `2006` dan tersimpan di `local_conflicts` lokal tanpa menimpa cookie server.
* **Perkiraan Usaha**: 3,5 hari kerja.
* **Risiko Utama**: Database SQLite `Cookies` rusak apabila Chrome mati mendadak saat proses tulis berlangsung. Mitigasi: jalankan `PRAGMA quick_check` sebelum ekstraksi dan buat cadangan `Cookies.bak` sebelum menulis blob yang diunduh.

### Fase 4: Integrasi UI Sinkronisasi, Resolusi Konflik Visual, dan Kemasan Deployment Server
* **Tujuan**: Menampilkan status sinkronisasi, indikator lease perangkat lain, pengaturan server, dan dialog resolusi konflik di frontend Vue (`ui/`), serta menyediakan `Dockerfile` dan unit `systemd` untuk server.
* **Pekerjaan**:
  1. Menambahkan action API lokal `sync-status`, `sync-configure`, `sync-trigger`, `sync-devices`, `sync-conflicts`, dan `sync-conflict-resolve` di `antidetect/api.py`.
  2. Menambahkan komponen indikator status sync di `ui/src/App.vue`, badge lease dan tombol *Ambil Alih* di `ui/src/views/Profiles.vue`, dialog resolusi konflik, dan panel konfigurasi sinkronisasi di `ui/src/views/Settings.vue`.
  3. Menyediakan skrip deployment satu container (`Dockerfile.sync-server`) dan template `systemd` untuk server.
* **Kriteria Penerimaan yang Bisa Diuji**:
  1. Pengguna dapat mendaftarkan device baru, melihat status sinkronisasi (*Tersinkronisasi*, *Offline - 3 antrean*, atau *Konflik*), melihat profil mana yang sedang dibuka di device lain dari tabel profil, dan menyelesaikan konflik cookie dari UI.
  2. Build frontend `cd ui && npm run build` berhasil tanpa warning/error.
* **Perkiraan Usaha**: 3 hari kerja.
* **Risiko Utama**: Polling status di UI membebani server jika terlalu rapat. Mitigasi: gabungkan informasi `sync_lease` ke dalam polling `profile-opened-list` yang sudah ada di `Profiles.vue` tanpa membuat timer HTTP terpisah yang berlebihan.

### Fase 5: Ekspansi Pasca-MVP (Bookmark, Extension Package, Password, dan Site Storage)
* **Tujuan**: Memperluas sinkronisasi ke file `Bookmarks`, paket zip/crx `extensions`, database `Login Data` (password), dan `Local Storage` secara opsional per profil.
* **Pekerjaan**:
  1. Menyinkronkan pohon bookmark (`antidetect/bookmarks.py`) sebagai `blob_kind = 'bookmarks'`.
  2. Menyinkronkan arsip extension di `~/.local/share/antidetect/extensions/<slug>` melalui `blob_kind = 'extension_pkg'`.
  3. Menambahkan fungsi tulis `update_passwords()` di `antidetect/passwords.py` dan sinkronisasi opsional `Login Data`.
  4. Menambahkan pengarsipan selektif `Default/Local Storage/leveldb` pasca-tutup untuk situs yang bergantung pada Web Storage.
* **Kriteria Penerimaan yang Bisa Diuji**:
  1. Bookmark dan extension yang dipasang di Device A otomatis terpasang dan siap dipakai saat profil dibuka di Device B.
  2. Password tersimpan yang disinkronkan dapat didekripsi oleh `list_saved_passwords()` di Device B.
* **Perkiraan Usaha**: 4 hari kerja.
* **Risiko Utama**: Ukuran `Local Storage` atau `IndexedDB` membengkak pada situs tertentu. Mitigasi: fitur site storage bersifat *opt-in* per profil dengan kuota keras 25 MB.

## 14. Strategi Tes

Seluruh rangkaian tes sinkronisasi harus mengikuti pola isolasi seperti `scripts/test-ixbrowser-import.py`: menggunakan direktori sementara di `~/.cache/antidetect/test-scratch`, menyetel `ANTIDETECT_HOME` terpisah untuk setiap device simulasi, **tidak menyentuh database asli pengguna** di `~/.local/share/antidetect/antidetect.db`, **tidak mencetak nilai cookie plaintext ke log/output**, dan **tidak perlu meluncurkan proses Chrome kecuali untuk satu tes integrasi akhir yang dibatasi ketat**.

### 14.1 Alat Tes yang Sudah Ada di Repo Sebagai Acuan
1. `scripts/test-ixbrowser-import.py`: Kerangka acuan untuk membuat `ANTIDETECT_HOME` terisolasi di `~/.cache/antidetect/test-scratch`, menjalankan server lokal di port khusus, dan memverifikasi integritas cookie memakai hash SHA-256 tanpa membuka browser.
2. `scripts/selftest.py`: Tes kontrak SDK resmi `ixbrowser-local-api` terhadap API lokal (wajib tetap lulus setelah perubahan `store.py` dan `api.py`).
3. `scripts/isolation_test.py`: Tes isolasi cookie antar profil dan verifikasi `_graceful_close` (flush cookie saat tab ditutup lewat `/json/close/<id>`).
4. `scripts/test-auth-proxy.py`: Tes end-to-end jembatan autentikasi proxy `gost`.

### 14.2 Rancangan Skrip Tes Baru (`scripts/test-sync.py`)

Skrip `scripts/test-sync.py` akan menjalankan 1 server sinkronisasi (`server_sync.db` di scratch) dan 2 instance klien lokal (`TEST_HOME_A` dan `TEST_HOME_B` di scratch) dalam satu proses orkestrasi pengujian tanpa menjalankan engine Chrome:

1. **Tes Unit Revision, Idempotency, dan Tombstone**:
   * Membuat profil, mengubahnya 3 kali, memverifikasi `revision` naik monotonik (`1 -> 2 -> 3`) dan `change_log.seq` bertambah.
   * Mengirim ulang payload `push` dengan `idempotency_key` yang sama dua kali; memverifikasi `revision` dan `seq` tidak bertambah dua kali.
   * Menjalankan `soft_delete_profile`, `restore_profile`, lalu `delete_profile_permanent`; memverifikasi `purged_at` tercatat sebagai tombstone di server dan memicu penghapusan direktori `profiles/<id>` di klien lain.
2. **Tes Dua Device dengan Dua Data Directory (`TEST_HOME_A` & `TEST_HOME_B`)**:
   * Device A membuat grup `"Marketplace"`, tag `"Utama"`, proxy SOCKS5, dan 5 profil.
   * Device A menjalankan `push`; Device B menjalankan `pull`.
   * Memverifikasi bahwa ke-5 profil hadir di Device B dengan `uuid` yang sama, `group_id` lokal Device B terpetakan dengan benar ke grup `"Marketplace"`, dan `proxy_password` berhasil didekripsi dengan kunci bersama.
3. **Tes Offline Lalu Reconnect (Outbox Coalescing)**:
   * Mematikan koneksi ke server (atau mengarahkan `server_url` ke port tertutup).
   * Device A membuat 2 profil baru, mengubah catatan profil pertama 4 kali berturut-turut, dan menghapus 1 profil ke keranjang sampah saat offline.
   * Memverifikasi perubahan tersimpan di `sync_outbox` dan operasi lokal tetap merespons instan.
   * Menghidupkan kembali akses server, memicu siklus sync, dan memverifikasi `sync_outbox` menjadi kosong (`0` baris) serta state akhir di server dan Device B identik dengan Device A.
4. **Tes Konflik Bersamaan (Field-Level Merge & LWW)**:
   * Device A dan Device B menarik profil yang sama pada `revision = 2`.
   * Kasus 4a (Field berbeda): Device A mengubah `note = "Catatan A"`, Device B mengubah `color = "#FF0000"`. Keduanya melakukan `push`. Memverifikasi hasil akhir di server (`revision = 4`) memiliki `note == "Catatan A"` dan `color == "#FF0000"`.
   * Kasus 4b (Field sama): Device A dan Device B sama-sama mengubah `name` dan `proxy_config`. Memverifikasi pemenang dipilih secara deterministik berdasarkan `(client_updated_at, device_id)`, `proxy_config` tidak tercampur separuh-separuh, dan baris yang kalah tercatat utuh di tabel `conflicts` serta `local_conflicts`.
5. **Tes Lease Eksklusif dan Lease Kedaluwarsa**:
   * Device A memanggil `acquire_lease(profile_uuid)`.
   * Device B mencoba `acquire_lease(profile_uuid)` dan memverifikasi ditolak dengan kode `2004` (`LEASE_HELD`).
   * Memajukan waktu uji melebihi `LEASE_TTL_SECONDS` (60 detik) tanpa perpanjangan heartbeat dari Device A.
   * Device B kembali memanggil `acquire_lease(profile_uuid)` dan memverifikasi berhasil dengan `fencing_token` yang naik `+1`.
   * Device B melepas lease secara eksplisit, lalu Device B atau Device A memanggil `acquire_lease` lagi dan memverifikasi `fencing_token` tetap naik `+1` setelah baris `profile_leases` dihapus. Ini membuktikan `sync_profiles.last_fencing_token` benar-benar persisten.
6. **Tes Device Mati / Zombie Writer Saat Memegang Lease**:
   * Device A mengakuisisi lease (`fencing_token = 10`).
   * Simulasikan Device A hang/tidur hingga lease kedaluwarsa, lalu Device B mengakuisisi lease (`fencing_token = 11`), mengunggah cookie revisi baru, dan melepas lease.
   * Device A "bangun" dan mencoba memperpanjang lease serta mengunggah blob cookie dengan `fencing_token = 10`.
   * Memverifikasi server menolak keduanya dengan `LEASE_STOLEN` (`2005`) dan `STALE_FENCING_TOKEN` (`2006`), cookie milik Device B di server tetap utuh, dan Device A menyimpan cookie miliknya ke `local_conflicts`.
7. **Tes Integritas Cookie Setelah Sync (End-to-End SQLite `Cookies`)**:
   * Di Device A, tulis 87 cookie nyata (menggunakan struktur dari sampel pengujian `cookies.update_cookies`) yang mencakup cookie session, persistent, `httpOnly`, `secure`, `sameSite=lax/none/strict`, dan karakter UTF-8.
   * Jalankan alur penutupan profil di Device A: ekstraksi `cookies.get_cookies()`, enkripsi amplop AES-256-GCM, unggah blob ke server.
   * Di Device B, jalankan alur pra-buka profil: unduh blob dari server, dekripsi, tulis ke SQLite `Default/Network/Cookies` milik Device B menggunakan `cookies.update_cookies()`, lalu baca kembali dengan `cookies.get_cookies()`.
   * Verifikasi jumlah cookie (87 item) dan hash SHA-256 dari setiap tuple `(domain, name, path, value, expires, httpOnly, secure, sameSite, session)` di Device B sama persis 100 persen dengan Device A.
8. **Tes Migrasi Database Lama**:
   * Membuat database SQLite menggunakan skema lama (sebelum ada kolom `uuid`, `revision`, dan tabel `sync_*`), mengisi 20 profil, 3 grup, 2 tag, dan 2 proxy.
   * Menjalankan `store.init()` baru.
   * Memverifikasi seluruh 20 profil masih memiliki `id` integer 1..20 yang sama, seluruh baris telah memiliki `uuid` valid, grup `Default` memiliki UUID tetap `00000000-0000-4000-8000-000000000001`, dan file backup `.pre-sync-*.bak` tersedia.
9. **Tes Keamanan Token, Tampering Ciphertext, dan Akses Tidak Sah**:
   * Memanggil endpoint `/v1/sync/pull` dan `/v1/sync/blob/download` tanpa token, dengan token salah, dan dengan token dari device yang sudah di-`revoke`; memverifikasi semuanya ditolak dengan `401` (`2001`/`2002`).
   * Membalik 1 bit pada `ciphertext_base64` atau mengubah `profile_uuid` pada `AAD` blob; memverifikasi dekripsi gagal dengan error autentikasi tag GCM dan tidak pernah menulis data rusak ke SQLite `Cookies`.

## 15. Risiko dan Mitigasi

| No | Risiko | Dampak | Probabilitas | Strategi Mitigasi |
| --- | --- | --- | --- | --- |
| 1 | **Sesi Akun Terkeluar (*Logout*) karena Cookie Hanya Sebagian dari State Situs**: Beberapa situs modern menyimpan token sesi di `Local Storage` atau `IndexedDB` selain di `Cookies`. | Pengguna harus login ulang pada situs yang bergantung pada `Local Storage` saat berpindah device di tahap MVP. | Sedang | Mendokumentasikan secara jelas bahwa MVP menyinkronkan `Cookies` terlebih dahulu (yang sudah mencakup mayoritas marketplace dan media sosial yang diuji pada impor ixBrowser), serta menyiapkan arsip selektif `Default/Local Storage/leveldb` di Fase 5. |
| 2 | **Korupsi Database SQLite `Cookies` Akibat Chrome Mati Tidak Bersih**: Jika proses Chrome terkena `SIGKILL` atau listrik padam, file `Cookies` bisa belum ter-flush atau rusak. | Mengunggah file `Cookies` yang rusak atau kosong akan menimpa sesi sehat di server dan merusak device lain. | Sedang | Hanya mengunggah cookie secara otomatis jika `_graceful_close()` mengembalikan `True`; menjalankan `PRAGMA quick_check` sebelum ekstraksi; menolak mengunggah daftar cookie kosong apabila sebelumnya profil memiliki >0 cookie kecuali dipicu oleh aksi eksplisit `profile-clear-cache-and-cookies`; serta menyimpan 3 revisi blob terakhir di server. |
| 3 | **Perbedaan Versi Engine / Kernel Antar Device**: Device A memakai engine 150 bawaan, sedangkan Device B memilih custom kernel di `~/.local/share/antidetect/kernels/<nama>/chrome` yang belum terpasang di Device B. | Profil gagal dibuka atau menghasilkan sidik jari berbeda saat dibuka di Device B. | Rendah | `antidetect/launcher.py` sudah memiliki fallback ke engine bawaan bila kernel kustom tidak ditemukan; selain itu endpoint `sync-status` menampilkan peringatan jika versi engine bawaan antar device berbeda. |
| 4 | **Kehilangan *Sync Master Passphrase* oleh Pengguna**: Karena menggunakan *Client-Side Envelope Encryption*, server tidak memiliki salinan kunci dekripsi. | Blob cookie di server tidak bisa didekripsi saat mendaftarkan device baru jika pengguna lupa passphrase dan semua device lama hilang. | Rendah | Saat inisialisasi pertama, CLI/UI wajib menampilkan *Recovery Key* (representasi hex/base32 dari `KEK`) untuk disimpan pengguna di pengelola kata sandi mereka, serta memvalidasi kecocokan passphrase terhadap nilai uji (*key check value*) di server sebelum mengizinkan pendaftaran device baru. |
| 5 | **Kontensi Lock SQLite Lokal Saat Sync Berjalan Bersamaan dengan Operasi UI/SDK**: `sync_agent.py` menulis hasil `pull` di latar belakang saat pengguna sedang mengklik tombol di UI. | Error `sqlite3.OperationalError: database is locked`. | Sedang | Mengaktifkan `PRAGMA journal_mode = WAL` dan `PRAGMA busy_timeout = 5000` pada setiap koneksi di `store.connect()`, serta menjaga transaksi tulis di dalam `store.py` sesingkat mungkin (tanpa melakukan I/O jaringan di dalam blok transaksi `with store.db()`). |
| 6 | **Kebocoran Kredensial atau Cookie di Log / Git**: Kesalahan pemformatan log saat debugging mencatat payload request/response. | Kebocoran sesi akun. | Rendah | Melarang pencatatan body request pada endpoint `/v1/sync/blob/*` dan menyensor field `cookie`, `ciphertext_base64`, `proxy_password`, serta `device_token` di seluruh logger lokal maupun server. |

## 16. Keputusan Desain yang Telah Disepakati Bersama Pengguna

Sebelum implementasi kode dimulai pada Fase 1, keputusan arsitektur berikut telah dikonfirmasi dan disepakati bersama pengguna:

1. **Kebijakan Buka Profil Saat Server Tidak Dapat Dihubungi (*Offline Open Policy*)**:
   * **Keputusan: Allow with Warning (Buka Offline)**. Jika server mati atau laptop sedang tanpa internet, profil tetap boleh dibuka dan diedit secara lokal agar pekerjaan pengguna tidak terhenti. Seluruh mutasi metadata dan cookie disimpan ke antrean lokal `sync_outbox`, lalu otomatis dikirimkan ke server saat laptop kembali online.

2. **Perilaku Mode Multi-Instance**:
   * **Keputusan: Normal (Satu Profil = Satu Jendela)**. Sistem difokuskan pada mode normal: satu profil membuka satu jendela browser tunggal yang terikat pada direktori `data` utama. Tidak ada percabangan multi-instance (`data-2`, `data-3`) pada profil yang disinkronkan, sehingga cookie sesi selalu konsisten dan tidak saling menimpa.

3. **Target Hosting dan Penyimpanan Server**:
   * **Keputusan: VPS Linux Mandiri dengan Domain Publik dan HTTPS**. Server sinkronisasi dijalankan di VPS milik pengguna dengan IP publik atau subdomain resmi berbalut HTTPS (reverse proxy Nginx atau Caddy). Penyimpanan blob cookie menggunakan disk lokal VPS (`LocalFilesystemBlobStore`) dan database SQLite WAL, sehingga mandiri tanpa perlu langganan cloud storage pihak ketiga seperti AWS S3 atau Cloudflare R2.

4. **Alur Autentikasi dan Penghubungan Perangkat**:
   * **Keputusan: Hubungkan Sekali (*One-Time Enrollment*), Tanpa Login Berulang**. Pengguna tidak perlu login dengan username dan password setiap kali membuka antidetect. Perangkat dihubungkan sekali saja lewat menu Pengaturan UI (memasukkan URL server, token pendaftaran dari server, nama perangkat, dan frasa sandi enkripsi). Setelah terhubung, sinkronisasi berjalan otomatis di latar belakang dengan indikator status di topbar.

5. **Kondisi Data Awal Saat Ini**:
   * **Keputusan: Kondisi A (Laptop Utama Sebagai Sumber Tunggal / *Primary Seed*)**. Seluruh 206 profil aktif (termasuk 200 profil hasil impor arsip ixBrowser) saat ini hanya berada di laptop utama ini. Laptop ini bertindak sebagai sumber data awal yang mengunggah metadata dan cookie ke server. Perangkat kedua nantinya mulai dari kondisi kosong dan langsung mengunduh (*pull*) seluruh data dari server secara bersih tanpa risiko bentrokan ID atau profil beririsan.

## 17. Perkiraan Usaha dan Urutan Pengerjaan

### 17.1 Tabel Ringkasan Usaha per Fase

| Urutan | Fase | Cakupan Utama | Estimasi Usaha | Ketergantungan |
| --- | --- | --- | --- | --- |
| 1 | **Fase 1 (MVP Bagian 1)** | Migrasi skema lokal (`uuid`, `revision`, `sync_outbox`), sentralisasi CRUD di `store.py`, server `/v1/sync/{device/register,push,pull}`, LWW + field merge, CLI `sync`. | **4,0 hari** | Tidak ada |
| 2 | **Fase 2 (MVP Bagian 2)** | Tabel `profile_leases`, endpoint `/v1/sync/lease/{acquire,renew,release}`, hook `launcher.py`, thread heartbeat 20 detik, deteksi proses mati. | **2,5 hari** | Fase 1 |
| 3 | **Fase 3 (MVP Bagian 3)** | Enkripsi amplop AES-256-GCM klien (`sync_crypto.py`), penyimpanan blob lokal/S3, ekstraksi & injeksi cookie otomatis saat tutup/buka profil, tes `scripts/test-sync.py`. | **3,5 hari** | Fase 1, Fase 2 |
| 4 | **Fase 4 (Pasca-MVP UI & Ops)** | Action API lokal `sync-*`, indikator UI di `App.vue`, badge lease & tombol ambil alih di `Profiles.vue`, halaman pengaturan sync di `Settings.vue`, `Dockerfile`. | **3,0 hari** | Fase 2, Fase 3 |
| 5 | **Fase 5 (Ekspansi Lanjutan)** | Sinkronisasi `Bookmarks`, paket arsip `extensions`, penulisan `Login Data` (`passwords.py`), dan arsip selektif `Local Storage`. | **4,0 hari** | Fase 3 |
| **Total** | **Seluruh Rencana** | **MVP Selesai di Akhir Fase 3 (10 hari kerja); Paket Lengkap Selesai di Fase 5** | **17,0 hari** | |

### 17.2 Urutan Pengerjaan Teknis (Critical Path)

```text
[1. Refactor CRUD group/tag/proxy di store.py + Migrasi Skema UUID & Outbox]
                                     |
                                     v
[2. Bangun sync_server.py (SQLite WAL: devices, sync_*, change_log) + Auth]
                                     |
                                     v
[3. Bangun sync_crypto.py (AES-256-GCM Envelope) + sync_agent.py (Push/Pull)]
                                     |
                                     v
[4. Implementasi profile_leases + Hook open/close di launcher.py + Heartbeat]
                                     |
                                     v
[5. Implementasi profile_blobs + Sync Cookie Otomatis saat Graceful Close]
                                     |
                                     v
[6. Verifikasi Penuh dengan scripts/test-sync.py & scripts/selftest.py]
                                     |
                                     v
[7. Integrasi UI Vue (Profiles.vue, Settings.vue) + Ekspansi Fase 5]
```

Urutan ini memastikan bahwa setiap tahap selalu menghasilkan sistem yang dapat diuji secara mandiri tanpa merusak kestabilan API lokal `127.0.0.1:53200` maupun kompatibilitas SDK `ixbrowser-local-api`.

