"""Tes otomatis end-to-end untuk sinkronisasi Fase 1 sampai Fase 5 tanpa menjalankan engine Chrome."""
import base64
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest import mock
import zipfile

os.environ["ANTIDETECT_NO_COOLDOWN"] = "1"
SCRATCH_ROOT = Path(os.environ.get("ANTIDETECT_TEST_SCRATCH") or Path.home() / ".cache" / "antidetect" / "test-scratch")
SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
WORK_DIR = Path(tempfile.mkdtemp(prefix="sync-test-", dir=str(SCRATCH_ROOT)))
os.environ["ANTIDETECT_HOME"] = str(WORK_DIR / "default_home")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from antidetect import (  # noqa: E402
    api,
    bookmarks,
    cookies,
    extensions,
    launcher,
    passwords,
    store,
    sync_agent,
    sync_crypto,
    sync_server,
)

LEGACY_SCHEMA = """
CREATE TABLE profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    group_id INTEGER NOT NULL DEFAULT 1,
    color TEXT,
    note TEXT,
    site_id INTEGER,
    site_url TEXT,
    proxy_config TEXT,
    proxy_key TEXT,
    fingerprint_config TEXT,
    preference_config TEXT,
    tag_ids TEXT DEFAULT '[]',
    last_open_time INTEGER,
    seed INTEGER,
    created_at TEXT,
    updated_at TEXT,
    deleted_at TEXT
);
CREATE TABLE groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    sort INTEGER NOT NULL DEFAULT 0,
    created_at TEXT
);
CREATE TABLE tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL
);
CREATE TABLE proxies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proxy_type TEXT,
    proxy_ip TEXT,
    proxy_port INTEGER,
    proxy_user TEXT,
    proxy_password TEXT,
    note TEXT,
    tag TEXT
);
CREATE TABLE templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    payload TEXT,
    created_at TEXT
);
CREATE TABLE extensions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE,
    title TEXT,
    version TEXT,
    path TEXT,
    created_at TEXT
);
"""


def run_cli(args, home_dir):
    python_bin = REPO / "venv" / "bin" / "python"
    if not python_bin.exists():
        python_bin = Path(sys.executable)
    env = dict(os.environ)
    env["ANTIDETECT_HOME"] = str(home_dir)
    proc = subprocess.run(
        [str(python_bin), "-m", "antidetect", *args],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"CLI gagal ({proc.returncode}): {' '.join(args)}\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_1_legacy_migration():
    print("=== Tes 1: Migrasi Database Lama ===")
    legacy_home = WORK_DIR / "legacy_home"
    legacy_home.mkdir(parents=True, exist_ok=True)
    legacy_db = legacy_home / "antidetect.db"

    conn = sqlite3.connect(str(legacy_db))
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("INSERT INTO groups (id, title, sort) VALUES (1, 'Default', 0)")
    conn.execute("INSERT INTO groups (id, title, sort) VALUES (2, 'Marketplace', 10)")
    conn.execute("INSERT INTO tags (id, title) VALUES (1, 'VIP')")
    conn.execute(
        "INSERT INTO proxies (id, proxy_type, proxy_ip, proxy_port) VALUES (1, 'socks5', '10.0.0.1', 1080)"
    )
    conn.execute(
        "INSERT INTO templates (id, name, payload) VALUES (1, 'Tpl Lama', '{\"profiles\":[]}')"
    )
    for pid, name, gid in [(1, "Profil Satu", 1), (2, "Profil Dua", 2), (7, "Profil Tujuh", 2)]:
        conn.execute(
            """INSERT INTO profiles (id, name, group_id, tag_ids, seed, created_at, updated_at)
               VALUES (?, ?, ?, '[1]', ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00')""",
            (pid, name, gid, 1000 + pid),
        )
    conn.commit()
    conn.close()

    store.init(db_path=legacy_db)

    backups = list(legacy_home.glob("antidetect.db.pre-sync-*.bak"))
    assert len(backups) == 1, f"Backup pre-sync harus ada 1, ditemukan: {backups}"
    assert backups[0].stat().st_size > 0

    with store.db(db_path=legacy_db) as mconn:
        prof_rows = mconn.execute("SELECT * FROM profiles ORDER BY id").fetchall()
        assert [r["id"] for r in prof_rows] == [1, 2, 7], "ID integer profil tidak boleh berubah"
        for r in prof_rows:
            assert r["uuid"] and len(r["uuid"]) == 36
            assert r["revision"] == 0
            assert r["sync_dirty"] == 1

        grp_default = mconn.execute("SELECT * FROM groups WHERE id = 1").fetchone()
        grp_mp = mconn.execute("SELECT * FROM groups WHERE id = 2").fetchone()
        assert grp_default["uuid"] == "00000000-0000-4000-8000-000000000001"
        assert grp_mp["uuid"] == store.group_uuid_for_title("Marketplace")

        tag_vip = mconn.execute("SELECT * FROM tags WHERE id = 1").fetchone()
        assert tag_vip["uuid"] == store.tag_uuid_for_title("VIP")

        proxy_row = mconn.execute("SELECT * FROM proxies WHERE id = 1").fetchone()
        assert proxy_row["uuid"] and len(proxy_row["uuid"]) == 36

        tpl_row = mconn.execute("SELECT * FROM templates WHERE id = 1").fetchone()
        assert tpl_row["uuid"] and len(tpl_row["uuid"]) == 36

        tables = {
            r["name"]
            for r in mconn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for required_table in ("sync_state", "sync_outbox", "local_leases", "local_conflicts"):
            assert required_table in tables, f"Tabel {required_table} belum dibuat"

    print(
        json.dumps(
            {
                "backup_file": backups[0].name,
                "preserved_profile_ids": [r["id"] for r in prof_rows],
                "default_group_uuid": grp_default["uuid"],
                "marketplace_group_uuid": grp_mp["uuid"],
            },
            indent=2,
        )
    )


def test_2_and_3_and_4():
    server_dir = WORK_DIR / "server_data"
    home_a = WORK_DIR / "home_a"
    home_b = WORK_DIR / "home_b"
    server_dir.mkdir(parents=True, exist_ok=True)
    home_a.mkdir(parents=True, exist_ok=True)
    home_b.mkdir(parents=True, exist_ok=True)

    httpd = sync_server.start_background_server("127.0.0.1", 0, data_dir=server_dir)
    port = httpd.server_address[1]
    server_url = f"http://127.0.0.1:{port}"
    passphrase = "FrasaSandiRahasia2026!"

    try:
        print("=== Tes 2: Sinkronisasi Dua Device (Device A & Device B) ===")
        tok_a = run_cli(
            [
                "sync-server",
                "token",
                "create",
                "--label",
                "Device-A",
                "--ttl",
                "30",
                "--data-dir",
                str(server_dir),
            ],
            home_a,
        )
        tok_b = run_cli(
            [
                "sync-server",
                "token",
                "create",
                "--label",
                "Device-B",
                "--ttl",
                "30",
                "--data-dir",
                str(server_dir),
            ],
            home_b,
        )

        reg_a = run_cli(
            [
                "sync",
                "setup",
                "--server",
                server_url,
                "--token",
                tok_a["token"],
                "--name",
                "Laptop-A",
                "--passphrase",
                passphrase,
            ],
            home_a,
        )
        reg_b = run_cli(
            [
                "sync",
                "setup",
                "--server",
                server_url,
                "--token",
                tok_b["token"],
                "--name",
                "Desktop-B",
                "--passphrase",
                passphrase,
            ],
            home_b,
        )
        assert reg_a["kdf_salt_hex"] == reg_b["kdf_salt_hex"]
        assert (home_a / "sync_credentials.json").stat().st_mode & 0o777 == 0o600
        assert (home_b / "sync_credentials.json").stat().st_mode & 0o777 == 0o600

        # Buat 1 grup dan 1 profil lokal di Device B agar autoincrement ID di Device B bergeser
        with store.db(db_path=home_b / "antidetect.db") as conn_b:
            gid_b_local = store.create_group(conn_b, "Grup Lokal B", sort=1)
            store.create_profile(conn_b, {"name": "Profil Lokal B", "group_id": gid_b_local})

        # Device A membuat grup "Grup Cabang", tag "Penting", proxy, dan profil "Akun 01"
        with store.db(db_path=home_a / "antidetect.db") as conn_a:
            gid_a = store.create_group(conn_a, "Grup Cabang", sort=5)
            tid_a = store.create_tag(conn_a, "Penting")
            pxid_a = store.create_proxy(
                conn_a,
                {
                    "proxy_type": "socks5",
                    "proxy_ip": "198.51.100.25",
                    "proxy_port": 1080,
                    "proxy_user": "user_cabang",
                    "proxy_password": "rahasia_proxy_123",
                    "note": "Proxy Cabang Utama",
                    "tag": "cabang",
                },
            )
            pid_a = store.create_profile(
                conn_a,
                {
                    "name": "Akun 01",
                    "group_id": gid_a,
                    "color": "#409EFF",
                    "note": "Catatan awal Akun 01",
                    "site_id": 6,
                    "site_url": "https://www.google.com/",
                    "proxy_config": {
                        "proxy_type": "socks5",
                        "proxy_ip": "198.51.100.25",
                        "proxy_port": 1080,
                        "proxy_user": "user_cabang",
                        "proxy_password": "rahasia_proxy_123",
                    },
                    "fingerprint_config": {"platform": "Windows", "hardware_concurrency": 8},
                    "preference_config": {"cookies_backup": 1},
                    "tab_id": [tid_a],
                    "seed": 777888999,
                },
            )
            prof_a_row = store.profile_get(conn_a, pid_a)
            grp_a_row = conn_a.execute("SELECT * FROM groups WHERE id = ?", (gid_a,)).fetchone()
            tag_a_row = conn_a.execute("SELECT * FROM tags WHERE id = ?", (tid_a,)).fetchone()
            px_a_row = conn_a.execute("SELECT * FROM proxies WHERE id = ?", (pxid_a,)).fetchone()

        status_before_a = run_cli(["sync", "status"], home_a)
        assert status_before_a["outbox_count"] == 4

        # Device A sync now; Device B sync now; lalu Device A sync lagi untuk menarik Profil Lokal B
        sync_res_a = run_cli(["sync", "now"], home_a)
        sync_res_b = run_cli(["sync", "now"], home_b)
        run_cli(["sync", "now"], home_a)

        status_after_a = run_cli(["sync", "status"], home_a)
        assert status_after_a["outbox_count"] == 0
        assert status_after_a["last_pulled_seq"] > 0

        # Verifikasi password proxy di server terenkripsi (bukan plaintext)
        with sync_server.connect_server_db(server_dir / "server_sync.db") as sconn:
            s_proxy = sconn.execute(
                "SELECT * FROM sync_proxies WHERE uuid = ?", (px_a_row["uuid"],)
            ).fetchone()
            assert s_proxy is not None
            assert s_proxy["proxy_password_enc"].startswith("v1:enc:")
            assert "rahasia_proxy_123" not in s_proxy["proxy_password_enc"]

            s_prof = sconn.execute(
                "SELECT * FROM sync_profiles WHERE uuid = ?", (prof_a_row["uuid"],)
            ).fetchone()
            assert s_prof is not None
            assert "rahasia_proxy_123" not in s_prof["proxy_config"]
            assert "proxy_password_enc" in s_prof["proxy_config"]

        # Verifikasi data di Device B
        with store.db(db_path=home_b / "antidetect.db") as conn_b:
            prof_b_row = conn_b.execute(
                "SELECT * FROM profiles WHERE uuid = ?", (prof_a_row["uuid"],)
            ).fetchone()
            assert prof_b_row is not None, "Profil Akun 01 harus muncul di Device B"
            assert prof_b_row["name"] == "Akun 01"
            assert prof_b_row["uuid"] == prof_a_row["uuid"]
            assert prof_b_row["id"] != pid_a, "ID integer lokal di Device B berbeda dari Device A"
            assert prof_b_row["note"] == "Catatan awal Akun 01"
            assert prof_b_row["seed"] == 777888999

            grp_b_row = conn_b.execute(
                "SELECT * FROM groups WHERE id = ?", (prof_b_row["group_id"],)
            ).fetchone()
            assert grp_b_row is not None
            assert grp_b_row["title"] == "Grup Cabang"
            assert grp_b_row["uuid"] == grp_a_row["uuid"]

            b_tag_ids = json.loads(prof_b_row["tag_ids"])
            assert len(b_tag_ids) == 1
            tag_b_row = conn_b.execute(
                "SELECT * FROM tags WHERE id = ?", (b_tag_ids[0],)
            ).fetchone()
            assert tag_b_row is not None
            assert tag_b_row["title"] == "Penting"
            assert tag_b_row["uuid"] == tag_a_row["uuid"]

            px_b_row = conn_b.execute(
                "SELECT * FROM proxies WHERE uuid = ?", (px_a_row["uuid"],)
            ).fetchone()
            assert px_b_row is not None
            assert px_b_row["proxy_ip"] == "198.51.100.25"
            assert px_b_row["proxy_password"] == "rahasia_proxy_123"

            b_proxy_cfg = json.loads(prof_b_row["proxy_config"])
            assert b_proxy_cfg["proxy_password"] == "rahasia_proxy_123"
            pid_b = prof_b_row["id"]

        print(
            json.dumps(
                {
                    "profile_uuid": prof_a_row["uuid"],
                    "device_a_local_id": pid_a,
                    "device_b_local_id": pid_b,
                    "group_uuid": grp_a_row["uuid"],
                    "tag_uuid": tag_a_row["uuid"],
                    "sync_a": {"pushed": sync_res_a["pushed"], "pulled": sync_res_a["pulled"]},
                    "sync_b": {"pushed": sync_res_b["pushed"], "pulled": sync_res_b["pulled"]},
                },
                indent=2,
            )
        )

        print("=== Tes 3: Sinkronisasi Dua Arah dan Penghapusan (Soft Delete & Purge) ===")
        # Buat direktori profil lokal di Device B untuk menguji penghapusan direktori saat purge
        prof_dir_b = home_b / "profiles" / str(pid_b) / "data"
        prof_dir_b.mkdir(parents=True, exist_ok=True)
        (prof_dir_b / "Preferences").write_text("{}", encoding="utf-8")

        # Device B mengubah catatan profil "Akun 01"
        with store.db(db_path=home_b / "antidetect.db") as conn_b:
            store.update_profile(
                conn_b, pid_b, {"note": "Catatan diperbarui oleh Device B"}
            )

        run_cli(["sync", "now"], home_b)
        run_cli(["sync", "now"], home_a)

        with store.db(db_path=home_a / "antidetect.db") as conn_a:
            prof_a_updated = store.profile_get(conn_a, pid_a)
            assert prof_a_updated["note"] == "Catatan diperbarui oleh Device B"

        # Device A menghapus profil ke keranjang sampah (soft_delete), sync ke B
        with store.db(db_path=home_a / "antidetect.db") as conn_a:
            store.soft_delete_profile(conn_a, pid_a)

        run_cli(["sync", "now"], home_a)
        run_cli(["sync", "now"], home_b)

        with store.db(db_path=home_b / "antidetect.db") as conn_b:
            prof_b_trashed = store.profile_get(conn_b, pid_b)
            assert prof_b_trashed is not None
            assert prof_b_trashed["deleted_at"] is not None, "Profil di B harus masuk keranjang sampah"

        # Device A menghapus profil secara permanen (purge), sync ke B
        with store.db(db_path=home_a / "antidetect.db") as conn_a:
            store.delete_profile_permanent(conn_a, pid_a)

        run_cli(["sync", "now"], home_a)
        run_cli(["sync", "now"], home_b)

        with store.db(db_path=home_b / "antidetect.db") as conn_b:
            prof_b_purged = store.profile_get(conn_b, pid_b)
            assert prof_b_purged is None, "Profil di Device B harus terhapus permanen"
            assert not (home_b / "profiles" / str(pid_b)).exists(), (
                "Direktori profil lokal di Device B harus ikut terhapus"
            )

        with sync_server.connect_server_db(server_dir / "server_sync.db") as sconn:
            tombstone = sconn.execute(
                "SELECT purged_at FROM sync_profiles WHERE uuid = ?",
                (prof_a_row["uuid"],),
            ).fetchone()
            assert tombstone is not None and tombstone["purged_at"] is not None

        print(
            json.dumps(
                {
                    "bidirectional_note_verified": True,
                    "soft_delete_verified": True,
                    "permanent_purge_verified": True,
                    "server_tombstone_purged_at": tombstone["purged_at"],
                },
                indent=2,
            )
        )
    finally:
        httpd.shutdown()
        httpd.server_close()

    print("=== Tes 4: Offline Outbox dan Coalescing ===")
    # Saat ini server sudah dimatikan (offline). Lakukan mutasi di Device A.
    with store.db(db_path=home_a / "antidetect.db") as conn_a:
        pid_off = store.create_profile(
            conn_a, {"name": "Akun Offline 01", "note": "Draft 1"}
        )
        store.update_profile(conn_a, pid_off, {"note": "Draft 2"})
        store.update_profile(conn_a, pid_off, {"note": "Draft Final Saat Offline"})
        prof_off_row = store.profile_get(conn_a, pid_off)

        outbox_rows = conn_a.execute(
            "SELECT * FROM sync_outbox WHERE entity_uuid = ?",
            (prof_off_row["uuid"],),
        ).fetchall()
        assert len(outbox_rows) == 1, (
            f"Coalescing outbox harus menyatukan upsert berurutan menjadi 1 baris, ditemukan: {len(outbox_rows)}"
        )

        # Coba jalankan push saat server mati; harus gagal namun item tetap di outbox
        failed_as_expected = False
        try:
            sync_agent.sync_cycle(conn_a)
        except sync_agent.SyncAgentError:
            failed_as_expected = True
        assert failed_as_expected, "Sync saat server offline harus melempar SyncAgentError"

        outbox_after_fail = conn_a.execute(
            "SELECT * FROM sync_outbox WHERE entity_uuid = ?",
            (prof_off_row["uuid"],),
        ).fetchone()
        assert outbox_after_fail is not None
        assert outbox_after_fail["attempts"] >= 1

    # Nyalakan kembali server di port yang sama
    httpd2 = sync_server.start_background_server("127.0.0.1", port, data_dir=server_dir)
    try:
        res_online_a = run_cli(["sync", "now"], home_a)
        res_online_b = run_cli(["sync", "now"], home_b)

        status_online_a = run_cli(["sync", "status"], home_a)
        assert status_online_a["outbox_count"] == 0, "Outbox harus kosong setelah server kembali online"

        with store.db(db_path=home_b / "antidetect.db") as conn_b:
            prof_off_b = conn_b.execute(
                "SELECT * FROM profiles WHERE uuid = ?", (prof_off_row["uuid"],)
            ).fetchone()
            assert prof_off_b is not None
            assert prof_off_b["name"] == "Akun Offline 01"
            assert prof_off_b["note"] == "Draft Final Saat Offline"

        print(
            json.dumps(
                {
                    "coalesced_outbox_rows": 1,
                    "outbox_after_reconnect": status_online_a["outbox_count"],
                    "synced_offline_profile_uuid": prof_off_row["uuid"],
                    "synced_note_on_device_b": prof_off_b["note"],
                    "push_online_a": res_online_a["pushed"],
                    "pull_online_b": res_online_b["pulled"],
                },
                indent=2,
            )
        )

        print("=== Tes 5: Distributed Profile Lease ===")
        profile_uuid = prof_off_row["uuid"]
        pid_a_off = prof_off_row["id"]
        pid_b_off = prof_off_b["id"]

        lease_a = test_lease_acquire_and_conflict(home_a, home_b, profile_uuid, pid_b_off)
        test_lease_renew_heartbeat(home_a, profile_uuid, lease_a)
        lease_b = test_lease_release_and_reacquire(
            home_a, home_b, profile_uuid, lease_a, pid_a_off, pid_b_off
        )
        lease_a2 = test_force_takeover(home_a, home_b, profile_uuid, lease_b)
        test_lease_expiration(home_a, home_b, profile_uuid, lease_a2)

        print("=== Tes 6: Sinkronisasi Cookie Terenkripsi ===")
        blob_res = test_cookie_blob_upload_encrypted(
            home_a, server_dir, profile_uuid, pid_a_off
        )
        test_cookie_blob_download_and_integrity(
            home_a,
            home_b,
            profile_uuid,
            pid_a_off,
            pid_b_off,
            blob_res["blob_id"],
        )
        test_cookie_tampering_rejected(
            home_a,
            home_b,
            server_dir,
            profile_uuid,
            pid_b_off,
            blob_res["blob_id"],
        )
        test_stale_fencing_token_rejected(
            home_a, home_b, profile_uuid, pid_a_off
        )
        test_e2e_launch_and_close_cookie_sync(
            home_a, home_b, profile_uuid, pid_a_off, pid_b_off
        )
        test_blob_retention_gc(
            home_a, server_dir, profile_uuid, pid_a_off
        )
        test_phase4_api_actions(
            home_a,
            home_b,
            server_dir,
            server_url,
            passphrase,
            profile_uuid,
            pid_a_off,
            pid_b_off,
        )
        print("=== Tes 8: Sinkronisasi Lanjutan Fase 5 (Bookmarks, Extensions, Passwords) ===")
        test_bookmark_sync(home_a, home_b, profile_uuid, pid_a_off, pid_b_off)
        test_password_sync(
            home_a, home_b, server_dir, profile_uuid, pid_a_off, pid_b_off
        )
        test_extension_sync(home_a, home_b, server_dir)
    finally:
        httpd2.shutdown()
        httpd2.server_close()

    test_offline_open_policy(home_a, prof_off_row["uuid"])


def test_lease_acquire_and_conflict(home_a, home_b, profile_uuid, pid_b):
    print("--- 5.1 test_lease_acquire_and_conflict ---")
    with store.db(db_path=home_a / "antidetect.db") as conn_a:
        lease_a = sync_agent.acquire_lease(conn_a, profile_uuid)
        assert lease_a["profile_uuid"] == profile_uuid
        assert lease_a["lease_token"].startswith("ls_")
        assert lease_a["fencing_token"] == 1
        assert lease_a["expires_at"] > int(time.time())

        local_row = conn_a.execute(
            "SELECT * FROM local_leases WHERE profile_uuid = ?", (profile_uuid,)
        ).fetchone()
        assert local_row is not None
        assert local_row["is_local_holder"] == 1
        assert local_row["lease_token"] == lease_a["lease_token"]

    # Device B mencoba mengakuisisi profil yang sama -> ditolak dengan error 2004 (LEASE_HELD)
    conflict_caught = None
    with store.db(db_path=home_b / "antidetect.db") as conn_b:
        try:
            sync_agent.acquire_lease(conn_b, profile_uuid)
        except sync_agent.SyncLeaseError as err:
            conflict_caught = err

    assert conflict_caught is not None, "Device B harus ditolak dengan SyncLeaseError"
    assert conflict_caught.code == 2004
    assert "Laptop-A" in conflict_caught.message
    assert conflict_caught.data.get("holder_device_name") == "Laptop-A"
    assert conflict_caught.data.get("retry_after_seconds", 0) >= 1

    # Uji integrasi launcher.open_profile dan api.profile_open di Device B (tanpa membuka Chrome)
    prev_db_path = store.DB_PATH
    store.DB_PATH = home_b / "antidetect.db"
    try:
        api_err_caught = None
        try:
            api.profile_open({"profile_id": pid_b})
        except api.ApiError as api_err:
            api_err_caught = api_err
        assert api_err_caught is not None
        assert api_err_caught.code == 1009
        assert "Laptop-A" in api_err_caught.message
    finally:
        store.DB_PATH = prev_db_path

    print(
        json.dumps(
            {
                "acquired_by": "Laptop-A",
                "fencing_token": lease_a["fencing_token"],
                "conflict_code": conflict_caught.code,
                "conflict_message": conflict_caught.message,
                "api_error_code": 1009,
            },
            indent=2,
        )
    )
    return lease_a


def test_lease_renew_heartbeat(home_a, profile_uuid, lease_a):
    print("--- 5.2 test_lease_renew_heartbeat ---")
    base_time = int(time.time()) + 25
    with mock.patch("antidetect.sync_server.time.time", return_value=base_time):
        with store.db(db_path=home_a / "antidetect.db") as conn_a:
            renew_res = sync_agent.renew_leases(conn_a)
            assert len(renew_res["renewed"]) == 1
            entry = renew_res["renewed"][0]
            assert entry["profile_uuid"] == profile_uuid
            assert entry["status"] == "ok"
            assert entry["expires_at"] == base_time + 60
            assert entry["expires_at"] > lease_a["expires_at"]

            local_row = conn_a.execute(
                "SELECT expires_at FROM local_leases WHERE profile_uuid = ?",
                (profile_uuid,),
            ).fetchone()
            assert local_row["expires_at"] == base_time + 60

    # Uji thread background heartbeat
    hb_time = base_time + 20
    with mock.patch("antidetect.sync_server.time.time", return_value=hb_time):
        sync_agent.start_heartbeat(interval=0.1, db_path=home_a / "antidetect.db")
        try:
            time.sleep(0.25)
        finally:
            sync_agent.stop_heartbeat()

    with store.db(db_path=home_a / "antidetect.db") as conn_a:
        local_row = conn_a.execute(
            "SELECT expires_at FROM local_leases WHERE profile_uuid = ?",
            (profile_uuid,),
        ).fetchone()
        assert local_row["expires_at"] == hb_time + 60

    print(
        json.dumps(
            {
                "previous_expires_at": lease_a["expires_at"],
                "renewed_expires_at": local_row["expires_at"],
                "heartbeat_verified": True,
            },
            indent=2,
        )
    )


def test_lease_release_and_reacquire(
    home_a, home_b, profile_uuid, lease_a, pid_a=None, pid_b=None
):
    print("--- 5.3 test_lease_release_and_reacquire ---")
    with store.db(db_path=home_a / "antidetect.db") as conn_a:
        rel_res = sync_agent.release_lease(
            conn_a, profile_uuid, lease_token=lease_a["lease_token"]
        )
        assert rel_res.get("released") is True
        local_row = conn_a.execute(
            "SELECT * FROM local_leases WHERE profile_uuid = ?", (profile_uuid,)
        ).fetchone()
        assert local_row is None

    with store.db(db_path=home_b / "antidetect.db") as conn_b:
        lease_b = sync_agent.acquire_lease(conn_b, profile_uuid)
        assert lease_b["profile_uuid"] == profile_uuid
        assert lease_b["fencing_token"] == 2
        assert lease_b["lease_token"] != lease_a["lease_token"]

    # Uji pembersihan proses mati (_prune_entry) dan profile-open-state-reset tanpa Chrome
    if pid_b is not None:
        prev_db_path = store.DB_PATH
        store.DB_PATH = home_b / "antidetect.db"
        try:
            dead_entry = {
                "proc": None,
                "info": {
                    "profile_id": pid_b,
                    "uuid": profile_uuid,
                    "pid": 99999999,
                    "lease_token": lease_b["lease_token"],
                    "fencing_token": lease_b["fencing_token"],
                },
            }
            launcher._open[pid_b] = dead_entry
            alive = launcher._prune_entry(pid_b, dead_entry)
            assert alive is False
            assert pid_b not in launcher._open

            # Setelah _prune_entry melepas lease B, Device B mengakuisisi ulang untuk tes berikutnya
            with store.db(db_path=home_b / "antidetect.db") as conn_b:
                lease_b = sync_agent.acquire_lease(conn_b, profile_uuid)
                assert lease_b["fencing_token"] == 3
        finally:
            store.DB_PATH = prev_db_path

    print(
        json.dumps(
            {
                "released_by_a": True,
                "reacquired_by_b": True,
                "prune_dead_process_released_lease": True,
                "new_fencing_token": lease_b["fencing_token"],
            },
            indent=2,
        )
    )
    return lease_b


def test_force_takeover(home_a, home_b, profile_uuid, lease_b):
    print("--- 5.4 test_force_takeover ---")
    # Device B sedang memegang lease aktif (fencing_token=2).
    # Device A melakukan acquire dengan force=True.
    with store.db(db_path=home_a / "antidetect.db") as conn_a:
        lease_a2 = sync_agent.acquire_lease(conn_a, profile_uuid, force=True)
        assert lease_a2["profile_uuid"] == profile_uuid
        assert lease_a2["fencing_token"] == lease_b["fencing_token"] + 1
        assert lease_a2["lease_token"] != lease_b["lease_token"]

    # Device B mencoba memperpanjang lease lamanya -> harus mendapat status "stolen"
    with store.db(db_path=home_b / "antidetect.db") as conn_b:
        renew_b = sync_agent.renew_leases(
            conn_b,
            leases=[
                {
                    "profile_uuid": profile_uuid,
                    "lease_token": lease_b["lease_token"],
                    "fencing_token": lease_b["fencing_token"],
                }
            ],
        )
        assert len(renew_b["renewed"]) == 1
        assert renew_b["renewed"][0]["status"] == "stolen"

        stolen_row = conn_b.execute(
            "SELECT * FROM local_leases WHERE profile_uuid = ? AND is_local_holder = 1",
            (profile_uuid,),
        ).fetchone()
        assert stolen_row is None

    print(
        json.dumps(
            {
                "force_takeover_by_a": True,
                "fencing_token_after_takeover": lease_a2["fencing_token"],
                "device_b_renew_status": renew_b["renewed"][0]["status"],
            },
            indent=2,
        )
    )
    return lease_a2


def test_lease_expiration(home_a, home_b, profile_uuid, lease_a2):
    print("--- 5.5 test_lease_expiration ---")
    # Device A memegang lease (lease_a2). Waktu server dimajukan melampaui TTL 60 detik.
    future_epoch = int(lease_a2["expires_at"]) + 10
    with mock.patch("antidetect.sync_server.time.time", return_value=future_epoch):
        with store.db(db_path=home_b / "antidetect.db") as conn_b:
            lease_b2 = sync_agent.acquire_lease(conn_b, profile_uuid, force=False)
            assert lease_b2["profile_uuid"] == profile_uuid
            assert lease_b2["fencing_token"] == lease_a2["fencing_token"] + 1
            assert lease_b2["expires_at"] == future_epoch + 60

            # Bersihkan lease di akhir tes online
            sync_agent.release_lease(
                conn_b, profile_uuid, lease_token=lease_b2["lease_token"]
            )

    print(
        json.dumps(
            {
                "expired_lease_token": lease_a2["lease_token"],
                "reacquired_without_force": True,
                "new_fencing_token": lease_b2["fencing_token"],
            },
            indent=2,
        )
    )


def test_offline_open_policy(home_a, profile_uuid):
    print("--- 5.6 test_offline_open_policy ---")
    # Server sudah dimatikan (httpd2.shutdown()).
    with store.db(db_path=home_a / "antidetect.db") as conn_a:
        res_offline = sync_agent.acquire_lease(conn_a, profile_uuid, timeout=1.0)
        assert res_offline["status"] == "offline_allowed"
        assert res_offline["offline"] is True
        assert res_offline["fencing_token"] == 0
        assert "warning" in res_offline

    print(
        json.dumps(
            {
                "offline_open_status": res_offline["status"],
                "offline_flag": res_offline["offline"],
                "warning": res_offline["warning"],
            },
            indent=2,
        )
    )


def test_cookie_blob_upload_encrypted(home_a, server_dir, profile_uuid, pid_a):
    print("--- 6.1 test_cookie_blob_upload_encrypted ---")
    sample_cookies = [
        {
            "domain": ".tokopedia.com",
            "name": "_SID_Tokopedia_",
            "path": "/",
            "value": "rahasia_tokopedia_val_001",
            "expires": 1893456000,
            "httpOnly": True,
            "secure": True,
            "sameSite": "lax",
            "session": False,
        },
        {
            "domain": ".shopee.co.id",
            "name": "SPC_EC",
            "path": "/",
            "value": "rahasia_shopee_val_002",
            "expires": 1893456000,
            "httpOnly": True,
            "secure": True,
            "sameSite": "none",
            "session": False,
        },
        {
            "domain": "zidstore.net",
            "name": "zs_session_tok",
            "path": "/dashboard",
            "value": "sesi_internal_zidstore_003",
            "expires": 0,
            "httpOnly": False,
            "secure": True,
            "sameSite": "strict",
            "session": True,
        },
    ]

    # Verifikasi determinisme dan pengabaian last_access_utc pada cookie_digest
    digest_1 = cookies.cookie_digest(sample_cookies)
    with_access_ts = []
    for item in reversed(sample_cookies):
        copied = dict(item)
        copied["last_access_utc"] = 13350000000000000
        copied["last_access_time"] = 1790000000
        with_access_ts.append(copied)
    digest_2 = cookies.cookie_digest(with_access_ts)
    assert digest_1 == digest_2, "Digest harus identik walau urutan berubah atau ada timestamp akses"

    prev_db = store.DB_PATH
    prev_prof_dir = launcher.PROFILES_DIR
    store.DB_PATH = home_a / "antidetect.db"
    launcher.PROFILES_DIR = home_a / "profiles"
    try:
        cookies.update_cookies(pid_a, sample_cookies)
        assert cookies.verify_cookie_db(pid_a) is True
        extracted_json = cookies.get_cookies(pid_a)
        assert cookies.cookie_digest(extracted_json) == digest_1
    finally:
        store.DB_PATH = prev_db
        launcher.PROFILES_DIR = prev_prof_dir

    with store.db(db_path=home_a / "antidetect.db") as conn_a:
        lease = sync_agent.acquire_lease(conn_a, profile_uuid)
        upload_res = sync_agent.upload_cookie_blob(
            conn_a, pid_a, profile_uuid, sample_cookies, lease["fencing_token"]
        )
        assert upload_res["blob_id"].startswith("blob_")
        assert upload_res["cookie_revision"] == 1
        assert upload_res["seq"] > 0

        # Uji deduplikasi jika cookie tidak berubah
        dup_res = sync_agent.upload_cookie_blob(
            conn_a, pid_a, profile_uuid, sample_cookies, lease["fencing_token"]
        )
        assert dup_res.get("status") == "skipped"
        assert dup_res["blob_id"] == upload_res["blob_id"]

        sync_agent.release_lease(
            conn_a, profile_uuid, lease_token=lease["lease_token"]
        )

    blob_store = sync_server.LocalFilesystemBlobStore(server_dir)
    blob_path = blob_store.blob_path(upload_res["blob_id"])
    assert blob_path.is_file(), f"Berkas blob harus ada di disk: {blob_path}"
    raw_file_bytes = blob_path.read_bytes()
    assert len(raw_file_bytes) > 0

    decoded_outer = base64.b64decode(raw_file_bytes)
    for forbidden in (
        b"_SID_Tokopedia_",
        b"SPC_EC",
        b"zs_session_tok",
        b"tokopedia.com",
        b"shopee.co.id",
        b"zidstore.net",
        b"rahasia_tokopedia_val_001",
        b"rahasia_shopee_val_002",
        b"sesi_internal_zidstore_003",
    ):
        assert forbidden not in raw_file_bytes, f"Plaintext bocor di file blob: {forbidden}"
        assert forbidden not in decoded_outer, f"Plaintext bocor di amplop JSON: {forbidden}"

    print(
        json.dumps(
            {
                "blob_id": upload_res["blob_id"],
                "cookie_revision": upload_res["cookie_revision"],
                "blob_file_size": len(raw_file_bytes),
                "encrypted_at_rest_verified": True,
                "deduplication_verified": True,
            },
            indent=2,
        )
    )
    return upload_res


def test_cookie_blob_download_and_integrity(
    home_a, home_b, profile_uuid, pid_a, pid_b, blob_id
):
    print("--- 6.2 test_cookie_blob_download_and_integrity ---")
    with store.db(db_path=home_b / "antidetect.db") as conn_b:
        injected = sync_agent.download_and_inject_cookies(
            conn_b, pid_b, profile_uuid, blob_id
        )
        assert injected is True
        prof_b = store.profile_get(conn_b, pid_b)
        assert prof_b["cookie_blob_id"] == blob_id
        assert prof_b["cookie_revision"] == 1
        assert prof_b["cookie_sha256"] is not None

    prev_prof_dir = launcher.PROFILES_DIR
    try:
        launcher.PROFILES_DIR = home_a / "profiles"
        cookies_a = json.loads(cookies.get_cookies(pid_a))

        launcher.PROFILES_DIR = home_b / "profiles"
        assert cookies.verify_cookie_db(pid_b) is True
        cookies_b = json.loads(cookies.get_cookies(pid_b))
    finally:
        launcher.PROFILES_DIR = prev_prof_dir

    assert len(cookies_b) == len(cookies_a) == 3
    assert cookies_b == cookies_a, f"Cookie di B tidak identik dengan A: {cookies_b} vs {cookies_a}"
    assert cookies.cookie_digest(cookies_b) == cookies.cookie_digest(cookies_a)

    print(
        json.dumps(
            {
                "downloaded_blob_id": blob_id,
                "cookies_count": len(cookies_b),
                "sha256_digest": cookies.cookie_digest(cookies_b),
                "exact_match_100_percent": True,
            },
            indent=2,
        )
    )


def test_cookie_tampering_rejected(
    home_a, home_b, server_dir, profile_uuid, pid_b, blob_id
):
    print("--- 6.3 test_cookie_tampering_rejected ---")
    prev_prof_dir = launcher.PROFILES_DIR
    launcher.PROFILES_DIR = home_b / "profiles"
    try:
        before_cookies = cookies.get_cookies(pid_b)
    finally:
        launcher.PROFILES_DIR = prev_prof_dir

    blob_store = sync_server.LocalFilesystemBlobStore(server_dir)
    blob_path = blob_store.blob_path(blob_id)
    original_bytes = blob_path.read_bytes()

    with store.db(db_path=home_b / "antidetect.db") as conn_b:
        kek, _ = store._load_local_crypto(conn_b)

        # 1) Ubah profile_uuid pada AAD -> harus gagal dengan SyncCryptoError
        aad_error_caught = False
        try:
            sync_crypto.decrypt_blob(
                original_bytes.decode("utf-8"),
                kek,
                "00000000-0000-4000-8000-999999999999",
                "cookies",
                1,
            )
        except sync_crypto.SyncCryptoError:
            aad_error_caught = True
        assert aad_error_caught, "Perubahan AAD wajib melempar SyncCryptoError"

        # 2) Modifikasi 1 byte pada ciphertext di dalam amplop blob di server
        envelope_obj = json.loads(base64.b64decode(original_bytes).decode("utf-8"))
        raw_cipher = bytearray(base64.b64decode(envelope_obj["ciphertext_base64"]))
        raw_cipher[0] ^= 0xFF
        envelope_obj["ciphertext_base64"] = base64.b64encode(bytes(raw_cipher)).decode("ascii")
        tampered_b64 = base64.b64encode(
            json.dumps(envelope_obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        blob_path.write_bytes(tampered_b64)
        try:
            tamper_caught = False
            try:
                sync_agent.download_and_inject_cookies(
                    conn_b, pid_b, profile_uuid, blob_id
                )
            except sync_crypto.SyncCryptoError:
                tamper_caught = True
            assert tamper_caught, "Ciphertext rusak wajib melempar SyncCryptoError"
        finally:
            blob_path.write_bytes(original_bytes)

    launcher.PROFILES_DIR = home_b / "profiles"
    try:
        after_cookies = cookies.get_cookies(pid_b)
    finally:
        launcher.PROFILES_DIR = prev_prof_dir

    assert after_cookies == before_cookies, "Database SQLite tidak boleh berubah saat dekripsi gagal"

    print(
        json.dumps(
            {
                "aad_tampering_rejected": True,
                "ciphertext_byte_tampering_rejected": True,
                "sqlite_intact": True,
            },
            indent=2,
        )
    )


def test_stale_fencing_token_rejected(home_a, home_b, profile_uuid, pid_a):
    print("--- 6.4 test_stale_fencing_token_rejected ---")
    # Naikkan fencing token di server melalui Device B
    with store.db(db_path=home_b / "antidetect.db") as conn_b:
        lease_b = sync_agent.acquire_lease(conn_b, profile_uuid)
        current_fencing = lease_b["fencing_token"]
        assert current_fencing >= 2
        sync_agent.release_lease(
            conn_b, profile_uuid, lease_token=lease_b["lease_token"]
        )

    stale_cookies = [
        {
            "domain": ".konflik.example.com",
            "name": "zombie_session",
            "path": "/",
            "value": "nilai_dari_zombie_writer",
            "expires": 1893456000,
            "httpOnly": True,
            "secure": True,
            "sameSite": "lax",
            "session": False,
        }
    ]

    with store.db(db_path=home_a / "antidetect.db") as conn_a:
        res_conflict = sync_agent.upload_cookie_blob(
            conn_a,
            pid_a,
            profile_uuid,
            stale_cookies,
            fencing_token=1,
        )
        assert res_conflict["status"] == "conflict"
        assert res_conflict["error"] == "STALE_FENCING_TOKEN"

        conflict_row = conn_a.execute(
            """SELECT * FROM local_conflicts
               WHERE entity_uuid = ? AND reason = 'STALE_FENCING_TOKEN'
               ORDER BY created_at DESC LIMIT 1""",
            (profile_uuid,),
        ).fetchone()
        assert conflict_row is not None
        losing = json.loads(conflict_row["losing_payload"])
        assert losing["cookies"][0]["name"] == "zombie_session"

    conflict_files = list(
        (home_a / "profiles" / str(pid_a)).glob("cookies-conflict-*.json")
    )
    assert len(conflict_files) >= 1, "Berkas cadangan cookies-conflict-*.json harus tertulis"
    backup_data = json.loads(conflict_files[0].read_text(encoding="utf-8"))
    assert backup_data["cookies"][0]["value"] == "nilai_dari_zombie_writer"

    print(
        json.dumps(
            {
                "rejected_fencing_token": 1,
                "server_current_fencing_token": current_fencing,
                "status": res_conflict["status"],
                "error": res_conflict["error"],
                "conflict_saved_in_db": True,
                "backup_file_created": conflict_files[0].name,
            },
            indent=2,
        )
    )


def test_e2e_launch_and_close_cookie_sync(home_a, home_b, profile_uuid, pid_a, pid_b):
    print("--- 6.5 test_e2e_launch_and_close_cookie_sync ---")

    class _DummyProc:
        pid = 0

        @staticmethod
        def poll():
            return 0

    e2e_cookies = [
        {
            "domain": ".e2e-login.id",
            "name": "auth_session_e2e",
            "path": "/",
            "value": "token_login_baru_setelah_tutup_profil_777",
            "expires": 1893456000,
            "httpOnly": True,
            "secure": True,
            "sameSite": "lax",
            "session": False,
        },
        {
            "domain": ".e2e-login.id",
            "name": "csrf_e2e",
            "path": "/",
            "value": "csrf_token_888",
            "expires": 0,
            "httpOnly": False,
            "secure": True,
            "sameSite": "strict",
            "session": True,
        },
    ]

    prev_db = store.DB_PATH
    prev_prof_dir = launcher.PROFILES_DIR
    try:
        with mock.patch("antidetect.launcher.subprocess.Popen", return_value=_DummyProc()), mock.patch(
            "antidetect.launcher._wait_for_devtools", return_value=True
        ):
            # 1) Device A membuka profil, menulis cookie sesi baru, lalu menutup profil secara bersih
            store.DB_PATH = home_a / "antidetect.db"
            launcher.PROFILES_DIR = home_a / "profiles"
            with store.db(db_path=home_a / "antidetect.db") as conn_a:
                row_a = store.profile_get(conn_a, pid_a)
                prof_a = store.profile_to_dict(row_a, include_seed=True)
                prof_a["id"] = pid_a

            open_info_a = launcher.open_profile(prof_a)
            assert open_info_a["fencing_token"] > 0

            # Simulasikan penulisan cookie selama sesi browser Device A sebelum proses keluar
            launcher._open.pop(pid_a, None)
            cookies.update_cookies(pid_a, e2e_cookies)
            expected_after_a = json.loads(cookies.get_cookies(pid_a))
            launcher._open[pid_a] = {"proc": _DummyProc(), "info": open_info_a}

            closed_a = launcher.close_profile(pid_a)
            assert closed_a is True

            with store.db(db_path=home_a / "antidetect.db") as conn_a:
                row_a_after = store.profile_get(conn_a, pid_a)
                assert row_a_after["cookie_revision"] == 2
                assert row_a_after["cookie_sha256"] == cookies.cookie_digest(expected_after_a)

            # 2) Device B membuka profil yang sama -> otomatis mengunduh dan menginjeksi cookie terbaru
            store.DB_PATH = home_b / "antidetect.db"
            launcher.PROFILES_DIR = home_b / "profiles"
            with store.db(db_path=home_b / "antidetect.db") as conn_b:
                row_b = store.profile_get(conn_b, pid_b)
                prof_b = store.profile_to_dict(row_b, include_seed=True)
                prof_b["id"] = pid_b
                assert prof_b["cookie_revision"] < 2

            open_info_b = launcher.open_profile(prof_b)
            assert open_info_b["fencing_token"] > open_info_a["fencing_token"]
            launcher.close_profile(pid_b)

            injected_b = json.loads(cookies.get_cookies(pid_b))
            assert injected_b == expected_after_a
            assert cookies.cookie_digest(injected_b) == cookies.cookie_digest(expected_after_a)
            names_b = {c["name"] for c in injected_b}
            assert "auth_session_e2e" in names_b and "csrf_e2e" in names_b
            with store.db(db_path=home_b / "antidetect.db") as conn_b:
                row_b_after = store.profile_get(conn_b, pid_b)
                assert row_b_after["cookie_revision"] == 2

            # 3) Uji aksi API profile-update-cookies juga otomatis menyinkronkan cookie ke server
            store.DB_PATH = home_a / "antidetect.db"
            launcher.PROFILES_DIR = home_a / "profiles"
            with store.db(db_path=home_a / "antidetect.db") as conn_a:
                lease_api = sync_agent.acquire_lease(conn_a, profile_uuid)
            try:
                api_updated = api.profile_update_cookies(
                    {
                        "profile_id": pid_a,
                        "cookie": [
                            {
                                "domain": ".api-manual.id",
                                "name": "manual_cookie_sync",
                                "path": "/",
                                "value": "dari_profile_update_cookies_api",
                                "expires": 1893456000,
                                "httpOnly": True,
                                "secure": True,
                                "sameSite": "lax",
                                "session": False,
                            }
                        ],
                    }
                )
                assert api_updated is True
            finally:
                with store.db(db_path=home_a / "antidetect.db") as conn_a:
                    sync_agent.release_lease(
                        conn_a, profile_uuid, lease_token=lease_api["lease_token"]
                    )
                    row_a_api = store.profile_get(conn_a, pid_a)
                    assert row_a_api["cookie_revision"] == 3
    finally:
        store.DB_PATH = prev_db
        launcher.PROFILES_DIR = prev_prof_dir

    print(
        json.dumps(
            {
                "device_a_uploaded_revision": 2,
                "device_b_auto_injected_on_open": True,
                "device_b_cookie_revision": 2,
                "api_profile_update_cookies_revision": 3,
                "synced_cookie_count": len(injected_b),
            },
            indent=2,
        )
    )


def test_blob_retention_gc(home_a, server_dir, profile_uuid, pid_a):
    print("--- 6.6 test_blob_retention_gc ---")
    blob_store = sync_server.LocalFilesystemBlobStore(server_dir)
    uploaded_ids = []

    with store.db(db_path=home_a / "antidetect.db") as conn_a:
        lease = sync_agent.acquire_lease(conn_a, profile_uuid)
        try:
            for idx in range(1, 5):
                rev_cookies = [
                    {
                        "domain": ".gc-test.id",
                        "name": f"rev_cookie_{idx}",
                        "path": "/",
                        "value": f"val_revisi_{idx}_{time.time()}",
                        "expires": 1893456000,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "lax",
                        "session": False,
                    }
                ]
                res = sync_agent.upload_cookie_blob(
                    conn_a, pid_a, profile_uuid, rev_cookies, lease["fencing_token"]
                )
                uploaded_ids.append(res["blob_id"])
        finally:
            sync_agent.release_lease(
                conn_a, profile_uuid, lease_token=lease["lease_token"]
            )

    assert len(uploaded_ids) == 4
    first_blob_id = uploaded_ids[0]
    kept_blob_ids = uploaded_ids[1:]

    with sync_server.connect_server_db(server_dir / "server_sync.db") as sconn:
        rows = sconn.execute(
            """SELECT blob_id, revision FROM profile_blobs
               WHERE profile_uuid = ? AND blob_kind = 'cookies'
               ORDER BY revision DESC""",
            (profile_uuid,),
        ).fetchall()
        db_blob_ids = [r["blob_id"] for r in rows]

    assert len(db_blob_ids) == 3, f"Server hanya boleh menyimpan 3 revisi terakhir, ditemukan: {db_blob_ids}"
    assert db_blob_ids == list(reversed(kept_blob_ids))
    assert first_blob_id not in db_blob_ids
    assert not blob_store.exists(first_blob_id), "File revisi lama harus terhapus dari disk"
    for kept_id in kept_blob_ids:
        assert blob_store.exists(kept_id), f"File revisi terbaru {kept_id} harus tetap ada di disk"

    all_bin_files = list((server_dir / "blobs").glob("**/*.bin"))
    assert len(all_bin_files) == 3, f"Total file .bin di disk harus 3, ditemukan: {len(all_bin_files)}"

    print(
        json.dumps(
            {
                "uploaded_revisions_in_batch": 4,
                "retained_blobs_in_db": len(db_blob_ids),
                "retained_files_on_disk": len(all_bin_files),
                "evicted_blob_id": first_blob_id,
                "retained_blob_ids": db_blob_ids,
            },
            indent=2,
        )
    )


def test_phase4_api_actions(
    home_a,
    home_b,
    server_dir,
    server_url,
    passphrase,
    profile_uuid,
    pid_a,
    pid_b,
):
    print("=== Tes 7: Action API Lokal Fase 4 (sync-status, sync-configure, sync-trigger, sync-conflicts, profile-list) ===")
    from contextlib import contextmanager

    @contextmanager
    def _use_home(target_home):
        prev_db = store.DB_PATH
        prev_prof = launcher.PROFILES_DIR
        store.DB_PATH = target_home / "antidetect.db"
        launcher.PROFILES_DIR = target_home / "profiles"
        try:
            yield
        finally:
            store.DB_PATH = prev_db
            launcher.PROFILES_DIR = prev_prof

    # 1. Device A mengambil lease pada profile_uuid, lalu Device B melakukan sync-trigger agar local_leases terisi
    with _use_home(home_a):
        with store.db() as conn_a:
            lease = sync_agent.acquire_lease(conn_a, profile_uuid)
        plist_a = api.ACTIONS["profile-list"]({"page": 1, "limit": 50})
        item_a = next(p for p in plist_a["data"] if p["uuid"] == profile_uuid)
        assert item_a["sync_lease"] is not None
        assert item_a["sync_lease"]["is_local"] is True
        assert item_a["sync_lease"]["holder_device_name"] == "Laptop-A"

    with _use_home(home_b):
        trig_b = api.ACTIONS["sync-trigger"]({})
        assert "pushed" in trig_b and "pulled" in trig_b
        st_b = api.ACTIONS["sync-status"]({})
        assert st_b["configured"] is True
        assert any(
            l["profile_uuid"] == profile_uuid and not l["is_local_holder"]
            for l in st_b["active_leases"]
        )
        plist_b = api.ACTIONS["profile-list"]({"page": 1, "limit": 50})
        item_b = next(p for p in plist_b["data"] if p["uuid"] == profile_uuid)
        assert item_b["sync_lease"] is not None
        assert item_b["sync_lease"]["is_local"] is False
        assert item_b["sync_lease"]["holder_device_name"] == "Laptop-A"

    # Lepas lease di Device A dan tarik di Device B -> sync_lease menjadi None
    with _use_home(home_a):
        with store.db() as conn_a:
            sync_agent.release_lease(
                conn_a, profile_uuid, lease_token=lease["lease_token"]
            )
        plist_a_after = api.ACTIONS["profile-list"]({"page": 1, "limit": 50})
        item_a_after = next(p for p in plist_a_after["data"] if p["uuid"] == profile_uuid)
        assert item_a_after["sync_lease"] is None

    # 2. Uji sync-conflicts dan sync-conflict-resolve di Device A (dari konflik yang tercatat sebelumnya)
    with _use_home(home_a):
        st_a = api.ACTIONS["sync-status"]({})
        assert st_a["conflict_count"] >= 1
        conflicts_a = api.ACTIONS["sync-conflicts"]({})
        assert len(conflicts_a) == st_a["conflict_count"]
        first_conf = conflicts_a[0]
        res_resolve = api.ACTIONS["sync-conflict-resolve"](
            {
                "conflict_id": first_conf["conflict_id"],
                "resolution": "keep_local",
            }
        )
        assert res_resolve["resolved"] is True
        for rest_conf in conflicts_a[1:]:
            api.ACTIONS["sync-conflict-resolve"](
                {
                    "conflict_id": rest_conf["conflict_id"],
                    "resolution": "keep_server",
                }
            )
        st_a_after = api.ACTIONS["sync-status"]({})
        assert st_a_after["conflict_count"] == 0
        assert st_a_after["outbox_count"] >= 1
        trig_a = api.ACTIONS["sync-trigger"]({})
        assert trig_a["pushed"] >= 1

    # 3. Uji sync-configure pada Device C baru
    home_c = WORK_DIR / "home_c"
    home_c.mkdir(parents=True, exist_ok=True)
    tok_c = sync_server.create_enrollment_token(
        db_path=server_dir, label="Device-C", ttl_minutes=30
    )
    with _use_home(home_c):
        store.init()
        st_c_before = api.ACTIONS["sync-status"]({})
        assert st_c_before["configured"] is False
        cfg_c = api.ACTIONS["sync-configure"](
            {
                "server_url": server_url,
                "enrollment_token": tok_c.token,
                "device_name": "Laptop-C",
                "passphrase": passphrase,
            }
        )
        assert cfg_c["status"] == "configured"
        assert cfg_c["device_id"]
        st_c_after = api.ACTIONS["sync-status"]({})
        assert st_c_after["configured"] is True
        assert st_c_after["device_name"] == "Laptop-C"
        plist_c = api.ACTIONS["profile-list"]({"page": 1, "limit": 50})
        assert plist_c["total"] >= 1

    print(
        json.dumps(
            {
                "profile_list_remote_lease": item_b["sync_lease"],
                "resolved_conflicts": len(conflicts_a),
                "device_c_configured": cfg_c["device_id"],
                "device_c_pulled_profiles": plist_c["total"],
            },
            indent=2,
        )
    )


def test_bookmark_sync(home_a, home_b, profile_uuid, pid_a, pid_b):
    print("--- 8.1 test_bookmark_sync ---")

    class _DummyProc:
        pid = 0

        @staticmethod
        def poll():
            return 0

    prev_db = store.DB_PATH
    prev_prof_dir = launcher.PROFILES_DIR
    try:
        store.DB_PATH = home_a / "antidetect.db"
        launcher.PROFILES_DIR = home_a / "profiles"
        bookmarks.add_bookmark(
            pid_a,
            "Contoh Dashboard",
            "https://zidstore.net/dashboard",
            folder="Bookmarks bar",
        )
        bookmarks.add_bookmark(
            pid_a,
            "Tokopedia Seller",
            "https://seller.tokopedia.com/home",
            folder="Bookmarks bar/Marketplace",
        )
        bm_list_a = bookmarks.list_bookmarks(pid_a)
        assert len(bm_list_a) == 2
        digest_a = bookmarks.bookmark_digest(pid_a)

        with store.db(db_path=home_a / "antidetect.db") as conn_a:
            lease = sync_agent.acquire_lease(conn_a, profile_uuid)
            try:
                up_res = sync_agent.upload_bookmark_blob(
                    conn_a, pid_a, profile_uuid, lease["fencing_token"]
                )
                assert up_res["blob_id"].startswith("blob_")
                assert up_res["bookmark_revision"] == 1

                dup_res = sync_agent.upload_bookmark_blob(
                    conn_a, pid_a, profile_uuid, lease["fencing_token"]
                )
                assert dup_res.get("status") == "skipped"
            finally:
                sync_agent.release_lease(
                    conn_a, profile_uuid, lease_token=lease["lease_token"]
                )

        # Device B membuka profil -> otomatis mengunduh dan menyuntikkan Bookmarks
        store.DB_PATH = home_b / "antidetect.db"
        launcher.PROFILES_DIR = home_b / "profiles"
        with mock.patch(
            "antidetect.launcher.subprocess.Popen", return_value=_DummyProc()
        ), mock.patch("antidetect.launcher._wait_for_devtools", return_value=True):
            with store.db(db_path=home_b / "antidetect.db") as conn_b:
                row_b = store.profile_get(conn_b, pid_b)
                prof_b = store.profile_to_dict(row_b, include_seed=True)
                prof_b["id"] = pid_b
            launcher.open_profile(prof_b)
            launcher.close_profile(pid_b)

        bm_path_b = bookmarks._bookmark_path(pid_b)
        assert bm_path_b.is_file(), f"File Bookmarks harus ada di Device B: {bm_path_b}"
        raw_bm_b = json.loads(bm_path_b.read_text(encoding="utf-8"))
        expected_md5 = bookmarks._checksum(raw_bm_b["roots"])
        assert raw_bm_b.get("checksum") == expected_md5, (
            f"Checksum MD5 Chromium tidak valid: {raw_bm_b.get('checksum')} != {expected_md5}"
        )

        bm_list_b = bookmarks.list_bookmarks(pid_b)
        assert len(bm_list_b) == 2
        assert [(b["name"], b["url"], b["folder"]) for b in bm_list_b] == [
            (a["name"], a["url"], a["folder"]) for a in bm_list_a
        ]
        assert bookmarks.bookmark_digest(pid_b) == digest_a

        with store.db(db_path=home_b / "antidetect.db") as conn_b:
            prof_b_after = store.profile_get(conn_b, pid_b)
            assert prof_b_after["bookmark_revision"] == 1
            assert prof_b_after["bookmark_blob_id"] == up_res["blob_id"]
    finally:
        store.DB_PATH = prev_db
        launcher.PROFILES_DIR = prev_prof_dir

    print(
        json.dumps(
            {
                "bookmark_blob_id": up_res["blob_id"],
                "bookmark_revision": up_res["bookmark_revision"],
                "bookmarks_count": len(bm_list_b),
                "md5_checksum_valid": True,
                "sha256_digest_match": True,
            },
            indent=2,
        )
    )


def test_password_sync(home_a, home_b, server_dir, profile_uuid, pid_a, pid_b):
    print("--- 8.2 test_password_sync ---")

    class _DummyProc:
        pid = 0

        @staticmethod
        def poll():
            return 0

    sample_passwords = [
        {
            "origin_url": "https://seller.tokopedia.com/",
            "username": "seller_cabang_01@contoh.test",
            "password": "ContohTokopedia#2026!",
        },
        {
            "origin_url": "https://seller.shopee.co.id/",
            "username": "shopee_utama@contoh.test",
            "password": "ContohShopee#9988!",
        },
        {
            "origin_url": "https://contoh.test/login",
            "username": "admin_panel",
            "password": "ContohInternal#777!",
        },
    ]

    prev_db = store.DB_PATH
    prev_prof_dir = launcher.PROFILES_DIR
    try:
        store.DB_PATH = home_a / "antidetect.db"
        launcher.PROFILES_DIR = home_a / "profiles"

        written = passwords.update_passwords(pid_a, sample_passwords)
        assert written == 3
        saved_a = passwords.list_saved_passwords(pid_a)
        assert len(saved_a) == 3
        digest_a = passwords.password_digest(saved_a)
        assert digest_a == passwords.password_digest(list(reversed(sample_passwords)))

        # Uji unggah otomatis saat profil ditutup di Device A
        with mock.patch(
            "antidetect.launcher.subprocess.Popen", return_value=_DummyProc()
        ), mock.patch("antidetect.launcher._wait_for_devtools", return_value=True):
            with store.db(db_path=home_a / "antidetect.db") as conn_a:
                row_a = store.profile_get(conn_a, pid_a)
                prof_a = store.profile_to_dict(row_a, include_seed=True)
                prof_a["id"] = pid_a
            launcher.open_profile(prof_a)
            launcher.close_profile(pid_a)

        with store.db(db_path=home_a / "antidetect.db") as conn_a:
            prof_a_after = store.profile_get(conn_a, pid_a)
            assert prof_a_after["password_revision"] == 1
            assert prof_a_after["password_blob_id"].startswith("blob_")
            pw_blob_id = prof_a_after["password_blob_id"]

        # Pastikan file blob password di server terenkripsi
        blob_store = sync_server.LocalFilesystemBlobStore(server_dir)
        raw_blob = blob_store.blob_path(pw_blob_id).read_bytes()
        for secret_bytes in (
            b"ContohTokopedia#2026!",
            b"ContohShopee#9988!",
            b"ContohInternal#777!",
            b"seller_cabang_01@contoh.test",
        ):
            assert secret_bytes not in raw_blob
            assert secret_bytes not in base64.b64decode(raw_blob)

        # Device B membuka profil -> otomatis mengunduh dan menyuntikkan Login Data
        store.DB_PATH = home_b / "antidetect.db"
        launcher.PROFILES_DIR = home_b / "profiles"
        with mock.patch(
            "antidetect.launcher.subprocess.Popen", return_value=_DummyProc()
        ), mock.patch("antidetect.launcher._wait_for_devtools", return_value=True):
            with store.db(db_path=home_b / "antidetect.db") as conn_b:
                row_b = store.profile_get(conn_b, pid_b)
                prof_b = store.profile_to_dict(row_b, include_seed=True)
                prof_b["id"] = pid_b
            launcher.open_profile(prof_b)
            launcher.close_profile(pid_b)

        saved_b = passwords.list_saved_passwords(pid_b)
        assert len(saved_b) == 3
        assert saved_b == saved_a, f"Password di Device B tidak cocok: {saved_b} vs {saved_a}"
        assert passwords.password_digest(saved_b) == digest_a

        with store.db(db_path=home_b / "antidetect.db") as conn_b:
            prof_b_after = store.profile_get(conn_b, pid_b)
            assert prof_b_after["password_revision"] == 1
            assert prof_b_after["password_blob_id"] == pw_blob_id
    finally:
        store.DB_PATH = prev_db
        launcher.PROFILES_DIR = prev_prof_dir

    print(
        json.dumps(
            {
                "password_blob_id": pw_blob_id,
                "password_revision": 1,
                "passwords_count": len(saved_b),
                "encrypted_at_rest_verified": True,
                "exact_match_100_percent": True,
            },
            indent=2,
        )
    )


def test_extension_sync(home_a, home_b, server_dir):
    print("--- 8.3 test_extension_sync ---")
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "manifest_version": 3,
                    "name": "Contoh Sync Helper",
                    "version": "1.2.0",
                    "description": "Ekstensi uji sinkronisasi Fase 5",
                }
            ),
        )
        zf.writestr("background.js", "console.log('contoh extension ready');\n")
    ext_b64 = base64.b64encode(zip_buf.getvalue()).decode("ascii")

    prev_db = store.DB_PATH
    prev_prof_dir = launcher.PROFILES_DIR
    prev_ext_dir = extensions.EXTENSIONS_DIR
    try:
        # 1) Pasang ekstensi di Device A dan unggah paketnya ke server
        store.DB_PATH = home_a / "antidetect.db"
        launcher.PROFILES_DIR = home_a / "profiles"
        extensions.EXTENSIONS_DIR = home_a / "extensions"

        uploaded_ext = extensions.upload("contoh-helper.zip", ext_b64)
        slug = uploaded_ext["slug"]
        assert slug == "contoh-sync-helper"
        assert (home_a / "extensions" / slug / "manifest.json").is_file()

        archive_bytes, archive_sha = extensions.get_extension_archive(slug)
        assert len(archive_bytes) > 0 and len(archive_sha) == 64

        with store.db(db_path=home_a / "antidetect.db") as conn_a:
            ext_blob_res = sync_agent.upload_extension_blob(conn_a, slug)
            assert ext_blob_res["blob_id"].startswith("blob_")
            assert ext_blob_res["slug"] == slug

        # 2) Device B menarik perubahan dari server -> ekstensi otomatis diunduh dan dipasang
        store.DB_PATH = home_b / "antidetect.db"
        launcher.PROFILES_DIR = home_b / "profiles"
        extensions.EXTENSIONS_DIR = home_b / "extensions"

        assert not (home_b / "extensions" / slug).exists()

        with store.db(db_path=home_b / "antidetect.db") as conn_b:
            pull_res = sync_agent.pull_changes(conn_b)
            assert pull_res["pulled"] >= 1
            ext_row_b = conn_b.execute(
                "SELECT * FROM extensions WHERE slug = ?", (slug,)
            ).fetchone()
            assert ext_row_b is not None
            assert ext_row_b["title"] == "Contoh Sync Helper"
            assert ext_row_b["version"] == "1.2.0"

        ext_dir_b = home_b / "extensions" / slug
        assert ext_dir_b.is_dir(), f"Direktori ekstensi harus terpasang di Device B: {ext_dir_b}"
        assert (ext_dir_b / "manifest.json").is_file()
        assert (ext_dir_b / "background.js").is_file()
        assert (
            (ext_dir_b / "background.js").read_text(encoding="utf-8")
            == "console.log('contoh extension ready');\n"
        )

        listed_b = extensions.list_extensions()
        assert any(item["slug"] == slug for item in listed_b["data"])
        resolved_paths = extensions.extension_paths([ext_row_b["id"]])
        assert ext_dir_b in resolved_paths
    finally:
        store.DB_PATH = prev_db
        launcher.PROFILES_DIR = prev_prof_dir
        extensions.EXTENSIONS_DIR = prev_ext_dir

    print(
        json.dumps(
            {
                "extension_slug": slug,
                "extension_blob_id": ext_blob_res["blob_id"],
                "installed_on_device_b_dir": str(ext_dir_b),
                "manifest_and_script_verified": True,
                "db_registered_on_device_b": True,
            },
            indent=2,
        )
    )


def main():
    try:
        test_1_legacy_migration()
        test_2_and_3_and_4()
        print("ALL SYNC PHASE 1, PHASE 2, PHASE 3, PHASE 4 & PHASE 5 TESTS PASSED")
    finally:
        shutil.rmtree(WORK_DIR, ignore_errors=True)



if __name__ == "__main__":
    main()
