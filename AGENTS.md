# Aturan proyek antidetect

## Bahasa & gaya
- Bahasa Indonesia. Tanpa em dash, gunakan koma, titik, titik dua.
- Dilarang karakter CJK (Tionghoa, Hiragana, Katakana, Hangul) dan Cyrillic di kode, komentar, string UI, dan dokumen. Balas dalam bahasa Indonesia, atau bahasa Inggris untuk istilah teknis saja. Jangan menyisipkan kata dari bahasa lain secara tak sengaja.
- Komentar kode minimal: jelaskan alasan bila tidak jelas, jangan mengulang kode.
- Anti slop: tanpa klise marketing, tanpa kalimat pengisi.

## Lingkup kerja
- Repo: CloakDesk (root direktori repositori ini). Tulis hanya di dalam repo ini, kecuali catatan hasil yang ditulis ke direktori scratch lokal (di luar repo) dan alat pihak ketiga (mis. gost) ke ~/.local/bin.
- Jangan mengubah engine di ~/.local/share/antidetect/squashfs-root maupun venv/site-packages.
- Database pengguna di ~/.local/share/antidetect/antidetect.db: boleh tambah profil uji, jangan hapus profil milik pengguna tanpa alasan.

## Lingkungan
- Engine: ~/.local/share/antidetect/squashfs-root/opt/ungoogled-chromium/chrome (fingerprint-chromium 150, chromedriver di folder sama).
- Venv: ./venv (sudah berisi ixbrowser-local-api dan selenium).
- DISPLAY=:0 (desktop nyata). Boleh membuka Chrome headed, selalu tutup kembali setelah tes.
- Frontend di `ui/` (Vue 3 + Element Plus + Vite). Dev: `cd ui && npm install && npm run dev` di 127.0.0.1:5180; dev server mem-proxy `/api` dan `/health` ke server.
- Server dev berjalan di 127.0.0.1:53200. Setelah mengubah antidetect/*.py, restart server:
  `PID=$(ss -ltnp | grep ':53200' | grep -oP 'pid=[0-9]+' | head -1 | cut -d= -f2); [ -n "$PID" ] && kill "$PID"; sleep 1; ./venv/bin/python -m antidetect serve >/dev/null 2>&1 & sleep 2; curl -s http://127.0.0.1:53200/health`

## Kontrak API
- SDK resmi ixBrowser (ixbrowser-local-api) adalah klien acuan. Jangan patch venv.
- Envelope respons wajib: {"error":{"code":0,"message":"..."},"data":...}; list terpaginasi {total,data}.
- Nama action tidak boleh berubah; perilaku harus cocok dengan SDK.

## Beban mesin saat menjalankan engine (sudah pernah menyebabkan laptop hang)

Pada 21 Oktober 2026 sebuah run mendelegasikan membuka 1280 Chrome headless
(8 paralel) dan laptop hang sampai harus reboot. Penyebabnya skrip probe
dengan parameter `--workers` tanpa batas, dijalankan lewat `nohup`.

- **Satu Chrome pada satu waktu.** Jangan pakai thread pool, jangan pakai
  `--workers`, jangan pakai `nohup` untuk menjalankan engine.
- **Maksimal 10 kali launch engine per batch**, dan hanya satu batch per
  run.
- **`user-data-dir` di tmpfs**, bukan di disk root. Disk root sudah sering
  macet saat random write kecil yang banyak.
- **Skrip probe baru harus dijalankan manual sekali oleh Hermes lebih dulu**
  untuk mengukur durasi dan jumlah proses, baru boleh diserahkan ke worker.
- **Worker tidak boleh menjalankan apa pun di latar belakang.** Kalau belum
  selesai, worker berhenti dan melapor, tidak melanjutkan sendiri.
- **Brief untuk worker harus menyebut batasnya secara eksplisit.** Worker
  memperlakukan skrip sebagai cetakan dan akan memperluas parameternya.
- Setelah run selesai, periksa sisa: `pgrep -c chrome`, `free -h`, dan
  `journalctl -b -1 | grep 'memory pressure'`.

## Bukti
- Setiap klaim harus diverifikasi dengan menjalankan perintah nyata dan menempelkan output mentah di catatan hasil.
- Tes acuan: scripts/selftest.py dan scripts/isolation_test.py (server harus jalan).
