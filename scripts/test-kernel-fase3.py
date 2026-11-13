#!/usr/bin/env python3
"""Uji mandiri Kernel Manager fase 3 tanpa membuka jendela Chrome.

Mencakup penyelarasan dinamis User-Agent dan brand version, metadata kernel
portabel saat profil dibuat atau diperbarui, serta resolusi kernel lintas
perangkat. Seluruh state ditulis ke direktori sementara di
~/.cache/antidetect/test-scratch/kernel-fase3 lalu dihapus setelah tes selesai.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRATCH = Path(
    os.environ.get("ANTIDETECT_KERNEL_TEST_DIR")
    or Path.home() / ".cache" / "antidetect" / "test-scratch" / "kernel-fase3"
)
SCRATCH.mkdir(parents=True, exist_ok=True)
ROOT = Path(tempfile.mkdtemp(prefix="run-", dir=SCRATCH))
HOME_DIR = ROOT / "home"
ENGINE_DIR = ROOT / "engine"
WORK = ROOT / "work"
for folder in (HOME_DIR, ENGINE_DIR, WORK):
    folder.mkdir(parents=True, exist_ok=True)

# Wrapper pengaman dilewati agar daftar argumen hasil build_args terbaca
# langsung, tanpa meluncurkan engine sungguhan.
os.environ["ANTIDETECT_HOME"] = str(HOME_DIR)
os.environ["ANTIDETECT_ENGINE_DIR"] = str(ENGINE_DIR)
os.environ["ANTIDETECT_ENGINE_GUARD"] = "0"
sys.path.insert(0, str(REPO))

from antidetect import api, launcher, store  # noqa: E402

CC = shutil.which("cc") or shutil.which("gcc")
FAILURES = []
BASE_URL = ""

UA_IMPOR = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)


def check(name, condition, detail=""):
    if condition:
        print(f"OK   {name}")
    else:
        print(f"GAGAL {name} {detail}".rstrip())
        FAILURES.append(name)


def build_mock(target, version_line):
    target.parent.mkdir(parents=True, exist_ok=True)
    if CC:
        source = target.with_suffix(".c")
        source.write_text(
            "#include <stdio.h>\n"
            'int main(void) { puts("%s"); return 0; }\n' % version_line
        )
        subprocess.run(
            [CC, str(source), "-o", str(target)], check=True, capture_output=True
        )
        source.unlink()
    else:
        # Tanpa kompilator: skrip shell yang mencetak baris versi.
        target.write_text(f"#!/bin/sh\necho '{version_line}'\n")
    target.chmod(0o755)
    return version_line


def call(action, payload=None):
    data = json.dumps(payload or {}).encode()
    request = urllib.request.Request(
        f"{BASE_URL}/api/v2/{action}",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.load(response)


def data_of(action, payload=None):
    return call(action, payload).get("data")


def error_of(action, payload=None):
    body = call(action, payload)
    error = body.get("error") or {}
    return int(error.get("code") or 0), str(error.get("message") or "")


def installed_kernel(folder, version_line):
    binary = launcher.KERNELS_DIR / folder / "chrome"
    build_mock(binary, version_line)
    return binary


def flag_value(args, prefix):
    for arg in args:
        if arg.startswith(prefix):
            return arg.split("=", 1)[1]
    return None


def launch_args(profile_id):
    with store.db() as conn:
        row = store.profile_get(conn, profile_id)
        profile = store.profile_to_dict(row)
    data_dir = WORK / "profiles" / str(profile_id) / "data"
    return launcher.build_args(profile, 0, data_dir=data_dir)


def stored_profile(profile_id):
    with store.db() as conn:
        row = store.profile_get(conn, profile_id)
        return store.profile_to_dict(row)


def main():
    global BASE_URL

    store.init()
    build_mock(ENGINE_DIR / "chrome", "Chromium 150.0.7839.0")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
    BASE_URL = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    try:
        run_tests()
    finally:
        httpd.shutdown()
        httpd.server_close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} tes gagal: {', '.join(FAILURES)}")
        return 1
    print("Semua tes fase 3 lulus.")
    return 0


def run_tests():
    print("== kernel_major_version")
    check(
        "builtin terbaca 150",
        launcher.kernel_major_version(launcher.CHROME) == 150,
        launcher.kernel_major_version(launcher.CHROME),
    )
    missing = WORK / "tidak-ada" / "chrome"
    check("biner hilang bernilai 0", launcher.kernel_major_version(missing) == 0)

    print("== align_user_agent")
    aligned = launcher.align_user_agent(UA_IMPOR, 152)
    check(
        "token Chrome diganti versi mayor",
        "Chrome/152.0.0.0" in aligned and "134" not in aligned,
        aligned,
    )
    check("format reduced UA dipertahankan", aligned.endswith("Safari/537.36"), aligned)
    check(
        "platform Windows menulis ulang token OS",
        "(Windows NT 10.0; Win64; x64)" in launcher.align_user_agent(UA_IMPOR, 152, "Windows"),
    )
    mac_ua = launcher.align_user_agent(UA_IMPOR, 151, "Macos", "14.5.0")
    check(
        "platform macOS memakai system_version",
        "Macintosh; Intel Mac OS X 14_5_0" in mac_ua and "Chrome/151.0.0.0" in mac_ua,
        mac_ua,
    )
    check(
        "platform android tidak diubah",
        launcher.align_user_agent(UA_IMPOR, 152, "Android") == UA_IMPOR,
    )
    check("ua kosong dikembalikan apa adanya", launcher.align_user_agent(None, 152) is None)
    check(
        "ua tanpa token Chrome dibiarkan",
        launcher.align_user_agent("Mozilla/5.0 tanpa token", 152) == "Mozilla/5.0 tanpa token",
    )
    check(
        "versi mayor tidak valid tidak mengubah ua",
        launcher.align_user_agent(UA_IMPOR, 0) == UA_IMPOR,
    )

    print("== kernel tambahan untuk uji")
    kernel_152 = installed_kernel("chromium-152", "Chromium 152.0.7950.0")
    kernel_153 = installed_kernel("chromium-153", "Chromium 153.0.8000.0")
    check("kernel 152 terpasang", kernel_152.is_file())
    check(
        "kernel_major_version kernel 152",
        launcher.kernel_major_version(kernel_152) == 152,
    )

    print("== build_args menyelaraskan brand version dan ua")
    custom = data_of(
        "profile-create",
        {
            "name": "Uji Selaras Fingerprint",
            "preference_config": {"kernel_path": str(kernel_152)},
            "fingerprint_config": {"ua": UA_IMPOR, "br_version": "134.0.0.0"},
        },
    )
    profile_id = custom["profile_id"]
    saved = stored_profile(profile_id)
    fp = saved["fingerprint_config"]
    check("br_version tersimpan versi kernel", fp.get("br_version") == "152.0.0.0", fp.get("br_version"))
    check(
        "ua tersimpan sudah selaras",
        "Chrome/152.0.0.0" in str(fp.get("ua")),
        fp.get("ua"),
    )
    args = launch_args(profile_id)
    check(
        "build_args memakai biner kernel 152",
        str(kernel_152.resolve()) in args,
        args[:2],
    )
    check(
        "brand version mengikuti kernel",
        flag_value(args, "--fingerprint-brand-version=") == "152.0.0.0",
        flag_value(args, "--fingerprint-brand-version="),
    )
    launch_ua = flag_value(args, "--user-agent=") or ""
    check("user-agent memakai versi kernel", "Chrome/152.0.0.0" in launch_ua, launch_ua)

    print("== build_args memaksa versi saat fingerprint lama")
    stale = dict(saved)
    stale["fingerprint_config"] = {"br_version": "134.0.0.0", "ua": UA_IMPOR}
    stale_args = launcher.build_args(stale, 0, data_dir=WORK / "profiles" / "stale" / "data")
    check(
        "brand version berbeda diganti",
        flag_value(stale_args, "--fingerprint-brand-version=") == "152.0.0.0",
        flag_value(stale_args, "--fingerprint-brand-version="),
    )
    check(
        "ua berbeda diganti",
        "Chrome/152.0.0.0" in (flag_value(stale_args, "--user-agent=") or ""),
        flag_value(stale_args, "--user-agent="),
    )
    no_brand = dict(saved)
    no_brand["fingerprint_config"] = {"platform": "Windows"}
    no_brand_args = launcher.build_args(
        no_brand, 0, data_dir=WORK / "profiles" / "brand" / "data"
    )
    check(
        "br_version kosong diisi versi kernel",
        flag_value(no_brand_args, "--fingerprint-brand-version=") == "152.0.0.0",
        flag_value(no_brand_args, "--fingerprint-brand-version="),
    )
    check(
        "tanpa ua tidak ada override user-agent",
        flag_value(no_brand_args, "--user-agent=") is None,
    )

    print("== build_args pada kernel bawaan")
    fallback = dict(saved)
    fallback["preference_config"] = {}
    fallback_args = launcher.build_args(
        fallback, 0, data_dir=WORK / "profiles" / "bawaan" / "data"
    )
    check(
        "brand version mengikuti biner bawaan",
        flag_value(fallback_args, "--fingerprint-brand-version=") == "150.0.0.0",
        flag_value(fallback_args, "--fingerprint-brand-version="),
    )
    check(
        "user-agent mengikuti biner bawaan",
        "Chrome/150.0.0.0" in (flag_value(fallback_args, "--user-agent=") or ""),
        flag_value(fallback_args, "--user-agent="),
    )

    print("== resolve_synced_kernel_preference")
    by_id = launcher.resolve_synced_kernel_preference(
        {
            "kernel_id": "chromium-152",
            "kernel_version": "152",
            "kernel_path": "/home/perangkat-lain/.local/share/antidetect/kernels/chromium-152/chrome",
        }
    )
    check("cocok kernel_id memakai path lokal", Path(by_id["kernel_path"]) == kernel_152.resolve())
    check("kernel_id asal dipertahankan", by_id["kernel_id"] == "chromium-152")
    by_version = launcher.resolve_synced_kernel_preference(
        {
            "kernel_id": "chromium-custom-b",
            "kernel_version": "153.0.8000.0",
            "kernel_path": "/home/perangkat-lain/.local/share/antidetect/kernels/chromium-custom-b/chrome",
        }
    )
    check("cocok versi mayor memakai kernel lokal", Path(by_version["kernel_path"]) == kernel_153.resolve())
    check("kernel_id asal tetap metadata", by_version["kernel_id"] == "chromium-custom-b")
    fallback_pref = launcher.resolve_synced_kernel_preference(
        {
            "kernel_id": "chromium-199",
            "kernel_version": "199",
            "kernel_path": "/home/perangkat-lain/.local/share/antidetect/kernels/chromium-199/chrome",
        }
    )
    check(
        "fallback mempertahankan metadata asal",
        fallback_pref["kernel_id"] == "chromium-199"
        and fallback_pref["kernel_version"] == "199"
        and fallback_pref["kernel_path"].endswith("chromium-199/chrome"),
        fallback_pref,
    )
    check(
        "kernel_for fallback ke bawaan",
        launcher.kernel_for({"preference_config": fallback_pref}) == launcher.CHROME.resolve()
        or launcher.kernel_for({"preference_config": fallback_pref}) == launcher.CHROME,
    )
    builtin_pref = launcher.resolve_synced_kernel_preference(
        {"kernel_id": "builtin", "kernel_version": "150", "kernel_path": "/home/lain/chrome"}
    )
    check(
        "kernel_id builtin dipetakan ke biner bawaan",
        Path(builtin_pref["kernel_path"]) == launcher.CHROME,
        builtin_pref["kernel_path"],
    )
    check(
        "preference tanpa referensi tidak berubah",
        launcher.resolve_synced_kernel_preference({"window_width": 1280}) == {"window_width": 1280},
    )

    print("== profile-create menyimpan metadata portabel")
    pref = saved["preference_config"]
    check("kernel_id tersimpan", pref.get("kernel_id") == "chromium-152", pref.get("kernel_id"))
    check("kernel_version tersimpan", pref.get("kernel_version") == "152", pref.get("kernel_version"))
    check(
        "kernel_path menunjuk biner lokal",
        Path(str(pref.get("kernel_path") or "")) == kernel_152.resolve(),
        pref.get("kernel_path"),
    )
    default_profile = data_of("profile-create", {"name": "Uji Kernel Default"})
    default_pref = stored_profile(default_profile["profile_id"])["preference_config"]
    check("profil default memakai kernel_id builtin", default_pref.get("kernel_id") == "builtin", default_pref)
    check(
        "profil default memakai biner bawaan",
        Path(str(default_pref.get("kernel_path") or "")) == launcher.CHROME,
        default_pref.get("kernel_path"),
    )

    print("== profile-update menyelaraskan ulang")
    data_of(
        "profile-update",
        {"profile_id": profile_id, "preference_config": {"kernel_path": str(kernel_153)}},
    )
    updated = stored_profile(profile_id)
    updated_pref = updated["preference_config"]
    updated_fp = updated["fingerprint_config"]
    check("kernel_id ikut berganti", updated_pref.get("kernel_id") == "chromium-153", updated_pref)
    check("kernel_version ikut berganti", updated_pref.get("kernel_version") == "153", updated_pref)
    check("br_version ikut berganti", updated_fp.get("br_version") == "153.0.0.0", updated_fp.get("br_version"))
    check(
        "ua ikut berganti",
        "Chrome/153.0.0.0" in str(updated_fp.get("ua")),
        updated_fp.get("ua"),
    )
    data_of(
        "profile-update",
        {
            "profile_id": profile_id,
            "fingerprint_config": {"ua": UA_IMPOR, "br_version": "134.0.0.0"},
        },
    )
    redo = stored_profile(profile_id)
    check(
        "update fingerprint tanpa kernel tetap selaras",
        redo["fingerprint_config"].get("br_version") == "153.0.0.0",
        redo["fingerprint_config"],
    )
    data_of(
        "profile-update",
        {"profile_id": profile_id, "preference_config": {"kernel_path": ""}},
    )
    back = stored_profile(profile_id)
    check("kernel_path kosong menjadi builtin", back["preference_config"].get("kernel_id") == "builtin", back["preference_config"])
    check(
        "br_version kembali ke bawaan",
        back["fingerprint_config"].get("br_version") == "150.0.0.0",
        back["fingerprint_config"].get("br_version"),
    )
    data_of(
        "profile-update",
        {"profile_id": profile_id, "preference_config": {"kernel_path": str(kernel_152)}},
    )

    print("== payload sync memuat referensi portabel")
    with store.db() as conn:
        store.set_sync_state(conn, "device_id", "device-a")
        conn.commit()
        row = store.profile_get(conn, profile_id)
        payload = store._profile_sync_payload(conn, row)
    sync_pref = payload.get("preference_config") or {}
    check("payload memuat kernel_id", sync_pref.get("kernel_id") == "chromium-152", sync_pref.get("kernel_id"))
    check("payload memuat kernel_version", sync_pref.get("kernel_version") == "152", sync_pref.get("kernel_version"))
    check("payload memuat kernel_path", bool(sync_pref.get("kernel_path")))
    check(
        "payload fingerprint selaras",
        payload["fingerprint_config"].get("br_version") == "152.0.0.0",
        payload["fingerprint_config"].get("br_version"),
    )

    print("== sync lintas perangkat")
    remote_uuid = str(uuid.uuid4())
    remote_change = {
        "entity_type": "profile",
        "entity_uuid": remote_uuid,
        "operation": "upsert",
        "revision": 1,
        "origin_device": "device-a",
        "changed_at": store.now_utc(),
        "payload": {
            **payload,
            "uuid": remote_uuid,
            "name": "Profil Dari Perangkat A",
            "group_uuid": store.DEFAULT_GROUP_UUID,
            "group_title": "Default",
            "preference_config": {
                "kernel_id": "chromium-152",
                "kernel_version": "152",
                "kernel_path": "/home/operator/.local/share/antidetect/kernels/chromium-152/chrome",
            },
        },
    }
    other = WORK / "device-b.db"
    store.init(db_path=other)
    with store.db(other) as conn:
        store.set_sync_state(conn, "device_id", "device-b")
        conn.commit()
        applied = store.apply_remote_change(conn, remote_change)
        row = conn.execute("SELECT * FROM profiles WHERE uuid = ?", (remote_uuid,)).fetchone()
        local_pref = store._loads(row["preference_config"])
        dirty = int(row["sync_dirty"] or 0)
        outbox = conn.execute(
            "SELECT COUNT(*) AS total FROM sync_outbox WHERE entity_uuid = ?", (remote_uuid,)
        ).fetchone()["total"]
    check("perubahan remote diterapkan", applied is True)
    check(
        "kernel_id cocok dipetakan ke path Perangkat B",
        Path(str(local_pref.get("kernel_path") or "")) == kernel_152.resolve(),
        local_pref.get("kernel_path"),
    )
    check("sync_dirty tetap 0", dirty == 0, dirty)
    check("tidak ada outbox baru", outbox == 0, outbox)

    version_uuid = str(uuid.uuid4())
    version_change = json.loads(json.dumps(remote_change))
    version_change["entity_uuid"] = version_uuid
    version_change["payload"]["uuid"] = version_uuid
    version_change["payload"]["preference_config"] = {
        "kernel_id": "chromium-lain",
        "kernel_version": "153",
        "kernel_path": "/home/operator/.local/share/antidetect/kernels/chromium-lain/chrome",
    }
    with store.db(other) as conn:
        applied = store.apply_remote_change(conn, version_change)
        row = conn.execute("SELECT * FROM profiles WHERE uuid = ?", (version_uuid,)).fetchone()
        version_pref = store._loads(row["preference_config"])
        dirty = int(row["sync_dirty"] or 0)
    check("perubahan berbasis versi diterapkan", applied is True)
    check(
        "versi mayor 153 memakai kernel lokal",
        Path(str(version_pref.get("kernel_path") or "")) == kernel_153.resolve(),
        version_pref.get("kernel_path"),
    )
    check("kernel_id asal tetap metadata", version_pref.get("kernel_id") == "chromium-lain")
    check("sync_dirty versi tetap 0", dirty == 0, dirty)

    missing_uuid = str(uuid.uuid4())
    missing_change = json.loads(json.dumps(remote_change))
    missing_change["entity_uuid"] = missing_uuid
    missing_change["payload"]["uuid"] = missing_uuid
    missing_change["payload"]["preference_config"] = {
        "kernel_id": "chromium-199",
        "kernel_version": "199",
        "kernel_path": "/home/operator/.local/share/antidetect/kernels/chromium-199/chrome",
    }
    with store.db(other) as conn:
        applied = store.apply_remote_change(conn, missing_change)
        row = conn.execute("SELECT * FROM profiles WHERE uuid = ?", (missing_uuid,)).fetchone()
        missing_pref = store._loads(row["preference_config"])
        dirty = int(row["sync_dirty"] or 0)
        row_dict = store.profile_to_dict(row)
    check("perubahan tanpa padanan diterapkan", applied is True)
    check(
        "metadata kernel asal dipertahankan",
        missing_pref.get("kernel_id") == "chromium-199" and str(missing_pref.get("kernel_version")) == "199",
        missing_pref,
    )
    check("sync_dirty fallback tetap 0", dirty == 0, dirty)
    resolved_binary = launcher.kernel_for(row_dict)
    check(
        "profil fallback dibuka dengan kernel bawaan",
        resolved_binary == launcher.CHROME.resolve() or resolved_binary == launcher.CHROME,
        resolved_binary,
    )
    fallback_profile = dict(row_dict)
    fallback_profile["fingerprint_config"] = {"br_version": "199.0.0.0"}
    fallback_args = launcher.build_args(
        fallback_profile,
        0,
        data_dir=WORK / "profiles" / "sync-fallback" / "data",
    )
    check(
        "brand version fallback ikut biner bawaan",
        flag_value(fallback_args, "--fingerprint-brand-version=") == "150.0.0.0",
        flag_value(fallback_args, "--fingerprint-brand-version="),
    )

    print("== resolusi selesai")


if __name__ == "__main__":
    keep = "--keep" in sys.argv[1:]
    try:
        exit_code = main()
    finally:
        if keep:
            print(f"Direktori uji disimpan: {ROOT}")
        else:
            shutil.rmtree(ROOT, ignore_errors=True)
    sys.exit(exit_code)
