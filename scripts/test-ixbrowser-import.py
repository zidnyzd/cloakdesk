"""Tes end-to-end untuk impor profil dan cookie dari ekspor ixBrowser.

Seluruh tes memakai ANTIDETECT_HOME di direktori scratch terisolasi,
tanpa menyentuh data asli dan tanpa menjalankan engine browser.
Nilai cookie tidak pernah dicetak; verifikasi memakai hash SHA-256.
"""
from collections import Counter
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

SCRATCH_ROOT = Path(os.environ.get("ANTIDETECT_TEST_SCRATCH") or Path.home() / ".cache" / "antidetect" / "test-scratch")
SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
TEST_HOME = Path(tempfile.mkdtemp(prefix="ixb-test-home-", dir=str(SCRATCH_ROOT)))
os.environ["ANTIDETECT_HOME"] = str(TEST_HOME)

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from antidetect import cookies, iximport, store  # noqa: E402

MAIN_REPO = REPO
COOKIES_ZIP = MAIN_REPO / "Export Profiles Cookies.zip"
PROFILE_ZIP = MAIN_REPO / "Export Profile.zip"
API_PORT = 53360


def sha256_text(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def post_api(port, action_name, payload):
    url = f"http://127.0.0.1:{port}/api/v2/{action_name}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_1_read_export():
    print("=== Tes 1: read_export pada Export Profiles Cookies.zip ===")
    exported = iximport.read_export(COOKIES_ZIP)
    profiles = exported["profiles"]
    total_cookies = sum(len(p["cookies"]) for p in profiles)
    empty_files = sum(1 for p in profiles if len(p["cookies"]) == 0)
    name_counts = Counter(p["name"] for p in profiles)
    duplicates = [name for name, count in name_counts.items() if count > 1]

    print(
        json.dumps(
            {
                "profiles": len(profiles),
                "total_cookies": total_cookies,
                "empty_cookie_files": empty_files,
                "duplicate_names": duplicates,
                "errors": len(exported["errors"]),
                "first_serial": profiles[0]["serial"],
                "last_serial": profiles[-1]["serial"],
            }
        )
    )
    assert len(profiles) == 200, f"expected 200 profiles, got {len(profiles)}"
    assert exported["metadata"] is True, "expected metadata=True"
    assert total_cookies == 12009, f"expected 12009 cookies, got {total_cookies}"
    assert empty_files == 2, f"expected 2 empty files, got {empty_files}"
    assert duplicates == ["LINK 78"], f"expected ['LINK 78'], got {duplicates}"
    assert exported["errors"] == [], f"expected 0 errors, got {exported['errors']}"
    assert profiles[0]["serial"] == 279
    assert profiles[-1]["serial"] == 481
    assert sum(1 for p in profiles if p["ua"]) == 200
    assert sum(1 for p in profiles if p["note"]) == 21
    return exported


def test_2_compare_serial_364(exported):
    print("=== Tes 2: Bandingkan profil serial 364 dengan sumber ===")
    profile_364 = next(p for p in exported["profiles"] if p["serial"] == 364)
    with zipfile.ZipFile(COOKIES_ZIP) as archive:
        raw_364 = json.loads(archive.read("364_LINK 59 0.txt").decode("utf-8-sig"))

    assert len(profile_364["cookies"]) == 87
    assert len(raw_364) == 87

    comparisons = []
    for idx in range(3):
        src = raw_364[idx]
        mapped = profile_364["cookies"][idx]
        expected_expires = int(float(src.get("expiration_time") or 0))
        expected_same_site = int(src.get("same_site"))
        src_hash = sha256_text(src.get("value", ""))
        mapped_hash = sha256_text(mapped["value"])

        assert mapped["domain"] == src["domain"]
        assert mapped["name"] == src["name"]
        assert mapped["path"] == (src.get("path") or "/")
        assert mapped["expires"] == expected_expires
        assert mapped["sameSite"] == expected_same_site
        assert mapped["secure"] is bool(src["secure"])
        assert mapped["httpOnly"] is bool(src["http_only"])
        assert mapped_hash == src_hash

        comparisons.append(
            {
                "index": idx,
                "domain": mapped["domain"],
                "name": mapped["name"],
                "path": mapped["path"],
                "expires": mapped["expires"],
                "sameSite": mapped["sameSite"],
                "secure": mapped["secure"],
                "httpOnly": mapped["httpOnly"],
                "value_sha256": mapped_hash,
                "hash_match": mapped_hash == src_hash,
            }
        )

    print(
        json.dumps(
            {
                "serial": profile_364["serial"],
                "name": profile_364["name"],
                "cookie_count": len(profile_364["cookies"]),
                "first_three": comparisons,
            },
            indent=2,
        )
    )


def test_3_preview():
    print("=== Tes 3: preview pada database kosong ===")
    result = iximport.preview(COOKIES_ZIP)
    print(json.dumps(result, indent=2))
    assert not store.DB_PATH.exists(), "preview tidak boleh menulis database"
    assert result["metadata"] is True
    assert result["total"] == 200
    assert result["cookies"] == 12009
    assert result["empty_cookie_files"] == 2
    assert result["duplicate_names"] == ["LINK 78"]
    assert result["existing_names"] == []
    assert result["errors"] == []
    assert result["serials"] == {"first": 279, "last": 481}
    expected_top_groups = {
        "IDCLOUD": 70,
        "LINKGO": 33,
        "DIGITAL OCEAN": 23,
        "Default Group": 18,
        "MAXCLOUD": 10,
        "NEVA": 8,
        "DEPA DEWA": 6,
        "GMAIL": 5,
    }
    for group_name, count in expected_top_groups.items():
        assert result["groups"].get(group_name) == count, (
            f"group {group_name}: expected {count}, got {result['groups'].get(group_name)}"
        )


def test_4_apply_import(exported):
    print("=== Tes 4: apply_import pada ANTIDETECT_HOME kosong ===")
    result = iximport.apply_import(COOKIES_ZIP, update_existing=False)
    print(json.dumps(result, indent=2))
    assert result["created"] == 200
    assert result["updated"] == 0
    assert result["skipped"] == 0
    assert result["cookies"] == 12009
    assert result["errors"] == []

    with store.db() as conn:
        total_profiles = conn.execute(
            "SELECT COUNT(*) AS c FROM profiles WHERE deleted_at IS NULL"
        ).fetchone()["c"]
        groups = conn.execute(
            "SELECT id, title FROM groups ORDER BY id"
        ).fetchall()
        row_364 = conn.execute(
            "SELECT * FROM profiles WHERE name = ? AND deleted_at IS NULL",
            ("LINK 59 0",),
        ).fetchone()

    assert total_profiles == 200
    group_titles = [g["title"] for g in groups]
    assert group_titles[0] == "Default"
    assert len(groups) == result["groups_created"] + 1

    profile_364_dict = store.profile_to_dict(row_364)
    assert profile_364_dict["fingerprint_config"].get("ua"), "UA harus tersimpan di fingerprint_config"
    assert profile_364_dict["site_id"] == 6
    assert profile_364_dict["site_url"] == "https://www.google.com/"

    stored_cookies = json.loads(cookies.get_cookies(row_364["id"]))
    src_cookies = next(p["cookies"] for p in exported["profiles"] if p["serial"] == 364)
    assert len(stored_cookies) == 87

    src_by_key = {
        (c["domain"], c["name"], c["path"]): sha256_text(c["value"])
        for c in src_cookies
    }
    stored_by_key = {
        (c["domain"], c["name"], c["path"]): sha256_text(c["value"])
        for c in stored_cookies
    }
    assert src_by_key == stored_by_key, "hash nilai cookie di DB harus cocok dengan sumber"
    print(
        json.dumps(
            {
                "db_profiles": total_profiles,
                "db_groups_total": len(groups),
                "db_groups_created": result["groups_created"],
                "verified_profile_id": row_364["id"],
                "verified_profile_name": row_364["name"],
                "verified_cookie_count": len(stored_cookies),
                "all_87_cookie_sha256_match": src_by_key == stored_by_key,
            },
            indent=2,
        )
    )


def test_5_apply_second_time():
    print("=== Tes 5: apply_import kedua kali tanpa update_existing ===")
    result = iximport.apply_import(COOKIES_ZIP, update_existing=False)
    print(json.dumps(result, indent=2))
    assert result["created"] == 0
    assert result["updated"] == 0
    assert result["skipped"] == 200
    assert result["cookies"] == 0
    assert result["groups_created"] == 0
    assert result["errors"] == []

    with store.db() as conn:
        total_profiles = conn.execute(
            "SELECT COUNT(*) AS c FROM profiles WHERE deleted_at IS NULL"
        ).fetchone()["c"]
    assert total_profiles == 200


def test_edge_cases():
    print("=== Tes tambahan: edge cases (file non-pola, cookie tanpa domain, tanpa Excel, update_existing) ===")
    custom_zip = TEST_HOME / "custom_edge.zip"
    with zipfile.ZipFile(custom_zip, "w") as zf:
        zf.writestr("readme.md", "diabaikan")
        zf.writestr("bukan_angka.txt", "[]")
        zf.writestr(
            "901_Profil Baru Uji.txt",
            json.dumps(
                [
                    {
                        "domain": ".facebook.com",
                        "name": "c_user",
                        "value": "dummy_val_1",
                        "path": "/",
                        "expiration_time": "0",
                        "secure": True,
                        "http_only": False,
                        "same_site": "invalid_val",
                    },
                    {
                        "domain": "",
                        "name": "tanpa_domain",
                        "value": "x",
                    },
                ]
            ),
        )
        zf.writestr(
            "364_LINK 59 0.txt",
            json.dumps(
                [
                    {
                        "domain": ".google.com",
                        "name": "SID",
                        "value": "updated_dummy_sid",
                        "path": "/",
                        "expiration_time": "1800000000",
                        "secure": True,
                        "http_only": True,
                        "same_site": "1",
                    }
                ]
            ),
        )

    read_res = iximport.read_export(custom_zip)
    assert read_res["metadata"] is False
    assert len(read_res["profiles"]) == 2
    assert len(read_res["errors"]) == 2
    p901 = next(p for p in read_res["profiles"] if p["serial"] == 901)
    assert p901["group"] == "Default Group"
    assert p901["cookies"][0]["sameSite"] == -1
    assert p901["cookies"][0]["session"] is True

    read_with_meta = iximport.read_export(custom_zip, metadata_path=PROFILE_ZIP)
    assert read_with_meta["metadata"] is True
    p364_meta = next(p for p in read_with_meta["profiles"] if p["serial"] == 364)
    assert p364_meta["group"] != "Default Group"
    assert bool(p364_meta["ua"]) is True

    upd_res = iximport.apply_import(custom_zip, update_existing=True)
    assert upd_res["created"] == 1
    assert upd_res["updated"] == 1
    print(
        json.dumps(
            {
                "edge_created": upd_res["created"],
                "edge_updated": upd_res["updated"],
                "edge_errors": upd_res["errors"],
            },
            indent=2,
        )
    )


def test_6_api():
    print(f"=== Tes 6: API server terisolasi di port {API_PORT} ===")
    api_home = Path(tempfile.mkdtemp(prefix="ixb-api-home-", dir=str(SCRATCH_ROOT)))
    non_zip_file = api_home / "bukan.txt"
    non_zip_file.write_text("halo", encoding="utf-8")

    python_bin = REPO / "venv" / "bin" / "python"
    if not python_bin.exists():
        python_bin = Path(sys.executable)

    env = dict(os.environ)
    env["ANTIDETECT_HOME"] = str(api_home)
    proc = subprocess.Popen(
        [
            str(python_bin),
            "-m",
            "antidetect",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(API_PORT),
        ],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready = False
        for _ in range(30):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{API_PORT}/health", timeout=1
                ) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.2)
        assert ready, "Server API gagal start di port 53360"

        # 5. Path lama masih berfungsi untuk dry run.
        status_dry, body_dry = post_api(
            API_PORT,
            "profile-import-ixbrowser",
            {"path": str(COOKIES_ZIP), "dry_run": True},
        )
        print("API path dry_run=True:", json.dumps({"http_status": status_dry, "body": body_dry}))
        assert status_dry == 200
        assert body_dry["error"]["code"] == 0
        assert body_dry["data"]["dry_run"] is True
        assert body_dry["data"]["metadata"] is True
        assert body_dry["data"]["total"] == 200
        assert body_dry["data"]["cookies"] == 12009

        cookies_b64 = base64.b64encode(COOKIES_ZIP.read_bytes()).decode("ascii")
        profile_b64 = base64.b64encode(PROFILE_ZIP.read_bytes()).decode("ascii")

        # 1. Base64 dry run dengan kedua zip: 200 profil, 12009 cookie, metadata: true, dry_run: true.
        status_b64_dry, body_b64_dry = post_api(
            API_PORT,
            "profile-import-ixbrowser",
            {
                "content_base64": cookies_b64,
                "filename": "Export Profiles Cookies.zip",
                "metadata_base64": profile_b64,
                "dry_run": True,
            },
        )
        print(
            "API base64 dry_run=True (kedua zip):",
            json.dumps({"http_status": status_b64_dry, "body": body_b64_dry}),
        )
        assert status_b64_dry == 200
        assert body_b64_dry["error"]["code"] == 0
        assert body_b64_dry["data"]["total"] == 200
        assert body_b64_dry["data"]["cookies"] == 12009
        assert body_b64_dry["data"]["metadata"] is True
        assert body_b64_dry["data"]["dry_run"] is True

        # 3. Export Profile.zip saja lewat base64: 62 profil, metadata: true.
        status_prof_only, body_prof_only = post_api(
            API_PORT,
            "profile-import-ixbrowser",
            {
                "content_base64": profile_b64,
                "filename": "Export Profile.zip",
                "dry_run": True,
            },
        )
        print(
            "API base64 Export Profile.zip saja:",
            json.dumps({"http_status": status_prof_only, "body": body_prof_only}),
        )
        assert status_prof_only == 200
        assert body_prof_only["error"]["code"] == 0
        assert body_prof_only["data"]["total"] == 62
        assert body_prof_only["data"]["metadata"] is True
        assert body_prof_only["data"]["dry_run"] is True

        # 4. content_base64 dan path bersamaan ditolak kode 1000. Base64 rusak ditolak. Isi kosong ditolak.
        status_both, body_both = post_api(
            API_PORT,
            "profile-import-ixbrowser",
            {
                "path": str(COOKIES_ZIP),
                "content_base64": cookies_b64,
                "dry_run": True,
            },
        )
        print("API path + content_base64:", json.dumps({"http_status": status_both, "body": body_both}))
        assert status_both == 200
        assert body_both["error"]["code"] == 1000

        status_bad_b64, body_bad_b64 = post_api(
            API_PORT,
            "profile-import-ixbrowser",
            {"content_base64": "!!!bukan-base64!!!", "dry_run": True},
        )
        print("API invalid base64:", json.dumps({"http_status": status_bad_b64, "body": body_bad_b64}))
        assert status_bad_b64 == 200
        assert body_bad_b64["error"]["code"] == 1000

        status_empty_b64, body_empty_b64 = post_api(
            API_PORT,
            "profile-import-ixbrowser",
            {"content_base64": "", "dry_run": True},
        )
        print("API empty content_base64:", json.dumps({"http_status": status_empty_b64, "body": body_empty_b64}))
        assert status_empty_b64 == 200
        assert body_empty_b64["error"]["code"] == 1000

        status_none, body_none = post_api(
            API_PORT,
            "profile-import-ixbrowser",
            {"dry_run": True},
        )
        print("API neither path nor content_base64:", json.dumps({"http_status": status_none, "body": body_none}))
        assert status_none == 200
        assert body_none["error"]["code"] == 1000

        status_meta_path, body_meta_path = post_api(
            API_PORT,
            "profile-import-ixbrowser",
            {"path": str(COOKIES_ZIP), "metadata_base64": profile_b64, "dry_run": True},
        )
        print("API path + metadata_base64:", json.dumps({"http_status": status_meta_path, "body": body_meta_path}))
        assert status_meta_path == 200
        assert body_meta_path["error"]["code"] == 1000

        # 2. Base64 apply pada home kosong: 200 profil dibuat dan 12009 cookie tertulis.
        status_b64_apply, body_b64_apply = post_api(
            API_PORT,
            "profile-import-ixbrowser",
            {
                "content_base64": cookies_b64,
                "filename": "Export Profiles Cookies.zip",
                "metadata_base64": profile_b64,
                "dry_run": False,
            },
        )
        print(
            "API base64 apply (dry_run=False):",
            json.dumps({"http_status": status_b64_apply, "body": body_b64_apply}),
        )
        assert status_b64_apply == 200
        assert body_b64_apply["error"]["code"] == 0
        assert body_b64_apply["data"]["dry_run"] is False
        assert body_b64_apply["data"]["metadata"] is True
        assert body_b64_apply["data"]["created"] == 200
        assert body_b64_apply["data"]["cookies"] == 12009
        assert body_b64_apply["data"]["errors"] == []

        tmp_dir = api_home / "imports" / "tmp"
        leftover_tmp = list(tmp_dir.glob("*.zip")) if tmp_dir.exists() else []
        assert leftover_tmp == [], f"file tmp tidak terhapus: {leftover_tmp}"

        status_rel, body_rel = post_api(
            API_PORT,
            "profile-import-ixbrowser",
            {"path": "Export Profiles Cookies.zip"},
        )
        print("API relative path:", json.dumps({"http_status": status_rel, "body": body_rel}))
        assert status_rel == 200
        assert body_rel["error"]["code"] == 1000

        status_nonzip, body_nonzip = post_api(
            API_PORT,
            "profile-import-ixbrowser",
            {"path": str(non_zip_file)},
        )
        print("API non-zip file:", json.dumps({"http_status": status_nonzip, "body": body_nonzip}))
        assert status_nonzip == 200
        assert body_nonzip["error"]["code"] == 1000

        status_outside, body_outside = post_api(
            API_PORT,
            "profile-import-ixbrowser",
            {"path": "/tmp/outside.zip"},
        )
        print("API outside home:", json.dumps({"http_status": status_outside, "body": body_outside}))
        assert status_outside == 200
        assert body_outside["error"]["code"] == 1000
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(api_home, ignore_errors=True)


def main():
    try:
        exported = test_1_read_export()
        test_2_compare_serial_364(exported)
        test_3_preview()
        test_4_apply_import(exported)
        test_5_apply_second_time()
        test_edge_cases()
        test_6_api()
        print("ALL TESTS PASSED")
    finally:
        shutil.rmtree(TEST_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
