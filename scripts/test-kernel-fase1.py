#!/usr/bin/env python3
"""Uji mandiri Kernel Manager fase 1 tanpa membuka jendela Chrome.

Seluruh state ditulis ke direktori sementara di
~/.cache/antidetect/test-scratch/kernel-fase1, lalu dihapus setelah tes selesai.
"""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRATCH = Path(
    os.environ.get("ANTIDETECT_KERNEL_TEST_DIR")
    or Path.home() / ".cache" / "antidetect" / "test-scratch" / "kernel-fase1"
)
SCRATCH.mkdir(parents=True, exist_ok=True)
ROOT = Path(tempfile.mkdtemp(prefix="run-", dir=SCRATCH))
HOME_DIR = ROOT / "home"
ENGINE_DIR = ROOT / "engine"
WORK = ROOT / "work"
for folder in (HOME_DIR, ENGINE_DIR, WORK):
    folder.mkdir(parents=True, exist_ok=True)

os.environ["ANTIDETECT_HOME"] = str(HOME_DIR)
os.environ["ANTIDETECT_ENGINE_DIR"] = str(ENGINE_DIR)
sys.path.insert(0, str(REPO))

from antidetect import api, kernels, launcher, store  # noqa: E402

CC = shutil.which("cc") or shutil.which("gcc")
FAILURES = []
BASE_URL = ""


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
        shutil.copy2(sys.executable, target)
    target.chmod(0o755)
    return version_line


def build_sleep_mock(target):
    target.parent.mkdir(parents=True, exist_ok=True)
    source = target.with_suffix(".c")
    source.write_text("#include <unistd.h>\nint main(void) { sleep(30); return 0; }\n")
    subprocess.run(
        [CC, str(source), "-o", str(target)], check=True, capture_output=True
    )
    source.unlink()
    target.chmod(0o755)


def make_tar(path, files, compression="xz"):
    mode = {"xz": "w:xz", "gz": "w:gz", "bz2": "w:bz2"}[compression]
    with tarfile.open(path, mode) as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755 if name.endswith("/chrome") else 0o644
            archive.addfile(info, io.BytesIO(payload))


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
    print("Semua tes fase 1 lulus.")
    return 0


def run_tests():
    print("== kernel-list awal")
    listing = data_of("kernel-list")
    check("kernel-list mengembalikan total", listing["total"] == len(listing["data"]))
    builtin = next((row for row in listing["data"] if row["id"] == "builtin"), None)
    check("kernel-list memuat builtin", builtin is not None)
    check("default awal adalah builtin", listing["default_kernel_id"] == "builtin")
    if builtin:
        check("builtin menandai is_builtin", builtin["is_builtin"] is True)
        check(
            "builtin aktif sebagai default",
            builtin["is_default"] is True and builtin["active"] is True,
        )
        check("major_version builtin", builtin["major_version"] == 150, builtin["major_version"])
        check("disk_size_mb builtin berupa float", isinstance(builtin["disk_size_mb"], float))
        check("created_at builtin terisi", bool(builtin["created_at"]))
        check("profile_count builtin nol", builtin["profile_count"] == 0)

    engine = data_of("engine-info")
    check("engine-info tetap memuat kernels", bool(engine.get("kernels")))
    check("engine-info max_kernel_version", engine["max_kernel_version"] == 150)

    print("== validasi biner")
    text_file = WORK / "bukan-elf.bin"
    text_file.write_bytes(b"#!/bin/sh\necho palsu\n")
    check("validate_binary_elf menolak skrip", kernels.validate_binary_elf(text_file) is False)
    check(
        "validate_binary_elf menerima mock ELF",
        kernels.validate_binary_elf(ENGINE_DIR / "chrome") is True,
    )
    size_dir = WORK / "size"
    size_dir.mkdir()
    (size_dir / "blob.bin").write_bytes(b"\0" * (1024 * 1024))
    check("calculate_dir_size_mb satu MB", kernels.calculate_dir_size_mb(size_dir) == 1.0)

    print("== kernel-install dari arsip tar.xz")
    mock = WORK / "mock-152" / "chrome"
    build_mock(mock, "Chromium 152.0.7950.0")
    archive = WORK / "chromium-152.tar.xz"
    make_tar(
        archive,
        {
            "chromium-152/chrome": mock.read_bytes(),
            "chromium-152/resources.pak": b"\0" * (1536 * 1024),
        },
    )
    installed = data_of(
        "kernel-install", {"source_type": "local_archive", "archive_path": str(archive)}
    )
    kernel_id = installed["id"]
    binary = Path(installed["path"])
    check("kernel-install mengembalikan id", bool(kernel_id), kernel_id)
    check("nama otomatis dari versi", installed["name"] == "Chromium 152", installed["name"])
    check(
        "major_version hasil pasang",
        installed["major_version"] == 152,
        installed["major_version"],
    )
    check("versi lengkap terbaca", installed["version"] == "Chromium 152.0.7950.0")
    check("biner berada di akar kernel", binary == launcher.KERNELS_DIR / kernel_id / "chrome")
    check("biner dapat dieksekusi", binary.is_file() and os.access(binary, os.X_OK))
    check("wrapper folder sudah diratakan", (binary.parent / "resources.pak").is_file())
    check("kernel.json tertulis", (binary.parent / kernels.METADATA_FILE).is_file())
    check("disk_size_mb lebih dari nol", installed["disk_size_mb"] > 0, installed["disk_size_mb"])
    check("is_builtin false", installed["is_builtin"] is False)
    check("supports tersedia", "supports" in installed)
    check("tidak ada staging tersisa", not list(launcher.KERNELS_DIR.glob(".install-*")))

    print("== path traversal arsip ditolak")
    evil_tar = WORK / "evil.tar"
    make_tar(evil_tar, {"../escape.txt": b"jangan sampai tertulis"})
    code, _ = error_of(
        "kernel-install", {"source_type": "local_archive", "archive_path": str(evil_tar)}
    )
    check("tar traversal ditolak", code == 1300, code)
    check("tidak ada berkas escape dari tar", not (launcher.KERNELS_DIR / "escape.txt").exists())
    evil_zip = WORK / "evil.zip"
    with zipfile.ZipFile(evil_zip, "w") as archive_zip:
        archive_zip.writestr("../escape-zip.txt", b"x")
    code, _ = error_of(
        "kernel-install", {"source_type": "local_archive", "archive_path": str(evil_zip)}
    )
    check("zip traversal ditolak", code == 1300, code)
    check(
        "tidak ada staging tersisa setelah traversal",
        not list(launcher.KERNELS_DIR.glob(".install-*")),
    )

    evil_link = WORK / "evil-link.tar"
    with tarfile.open(evil_link, "w") as archive_link:
        info = tarfile.TarInfo("evil/chrome")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive_link.addfile(info)
    code, _ = error_of(
        "kernel-install", {"source_type": "local_archive", "archive_path": str(evil_link)}
    )
    check("symlink keluar ditolak", code == 1300, code)

    print("== kernel-install dari tar.gz dengan symlink internal")
    linked_src = WORK / "linked-src"
    build_mock(linked_src / "chrome-bin", "Chromium 157.0.8400.0")
    linked_tar = WORK / "linked.tar.gz"
    with tarfile.open(linked_tar, "w:gz") as archive_link:
        archive_link.add(linked_src / "chrome-bin", arcname="linked/chrome-bin")
        info = tarfile.TarInfo("linked/chrome")
        info.type = tarfile.SYMTYPE
        info.linkname = "chrome-bin"
        archive_link.addfile(info)
    linked = data_of(
        "kernel-install",
        {
            "source_type": "local_archive",
            "archive_path": str(linked_tar),
            "kernel_name": "Kernel Simlink",
        },
    )
    check("arsip tar.gz terpasang", linked["id"] == "kernel-simlink", linked["id"])
    check("symlink chrome dipertahankan", Path(linked["path"]).is_symlink())
    check("versi dari binari symlink", linked["major_version"] == 157, linked["major_version"])
    data_of("kernel-delete", {"kernel_id": linked["id"]})

    print("== kernel-set-default")
    result = data_of("kernel-set-default", {"kernel_id": kernel_id})
    check("default_kernel_id berubah", result["default_kernel_id"] == kernel_id)
    check("path default menunjuk biner", Path(result["path"]) == binary)
    listing = data_of("kernel-list")
    chosen = next(row for row in listing["data"] if row["id"] == kernel_id)
    check("kernel terpilih is_default", chosen["is_default"] is True and chosen["active"] is True)
    builtin = next(row for row in listing["data"] if row["id"] == "builtin")
    check("builtin tidak lagi default", builtin["is_default"] is False)
    check(
        "kernel_for memakai kernel default",
        launcher.kernel_for({"preference_config": {}}) == binary.resolve(),
    )
    code, _ = error_of("kernel-set-default", {"kernel_id": "tidak-ada"})
    check("kernel-set-default menolak id asing", code == 1000, code)

    print("== kernel-delete proteksi")
    code, message = error_of("kernel-delete", {"kernel_id": "builtin"})
    check("delete builtin ditolak 1104", code == 1104, f"{code} {message}")
    check("biner builtin masih ada", launcher.CHROME.is_file())
    code, _ = error_of("kernel-delete", {"kernel_id": "tidak-ada"})
    check("delete kernel asing ditolak 1103", code == 1103, code)

    print("== profil baru memakai kernel default")
    created = data_of("profile-create", {"name": "Uji Kernel Fase 1"})
    profile_id = created["profile_id"]
    with store.db() as conn:
        row = store.profile_get(conn, profile_id)
        preferences = store._loads(row["preference_config"])
    check(
        "profile-create menyisipkan kernel_path",
        Path(str(preferences.get("kernel_path") or "")) == binary,
        preferences.get("kernel_path"),
    )
    listing = data_of("kernel-list")
    chosen = next(row for row in listing["data"] if row["id"] == kernel_id)
    check("profile_count menghitung profil", chosen["profile_count"] == 1, chosen["profile_count"])

    code, message = error_of("kernel-delete", {"kernel_id": kernel_id})
    check("delete kernel terpakai ditolak 1105", code == 1105, f"{code} {message}")
    check("pesan 1105 menyebut profil", "1 profil" in message, message)
    check("folder kernel masih ada", binary.parent.is_dir())

    print("== penghapusan sukses")
    data_of("profile-delete", {"profile_id": profile_id})
    result = data_of("kernel-delete", {"kernel_id": kernel_id})
    check("hapus kernel berhasil", result["deleted"] is True)
    check("default kembali ke builtin", result["default_kernel_id"] == "builtin")
    check("folder kernel hilang", not binary.parent.exists())
    listing = data_of("kernel-list")
    check("kernel-list hanya menyisakan builtin", listing["total"] == 1, listing["total"])
    check(
        "kernel_for kembali ke builtin",
        launcher.kernel_for({"preference_config": {}}) == launcher.CHROME,
    )

    print("== kernel-install dari folder lokal")
    source_dir = WORK / "kernelsrc-153"
    build_mock(source_dir / "chrome", "Chromium 153.0.8000.0")
    (source_dir / "icudtl.dat").write_bytes(b"\0" * 4096)
    local = data_of(
        "kernel-install",
        {
            "source_type": "local_dir",
            "source_path": str(source_dir),
            "kernel_name": "Kernel Lokal 153",
        },
    )
    check("nama kernel dipakai", local["name"] == "Kernel Lokal 153", local["name"])
    check("id dari nama kernel", local["id"] == "kernel-lokal-153", local["id"])
    deleted = data_of("kernel-delete", {"kernel_id": local["id"]})
    check("hapus kernel lokal berhasil", deleted["deleted"] is True)

    print("== wrapper pengaman")
    wrapper_path = REPO / "scripts" / "antidetect-engine-wrapper.py"
    spec = importlib.util.spec_from_file_location("engine_wrapper", wrapper_path)
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    check("wrapper menolak biner di luar izin", wrapper.validate_engine_binary("/bin/true") is None)
    inside = launcher.KERNELS_DIR / "wrapper-check" / "chrome"
    build_mock(inside, "Chromium 154.0.8100.0")
    check(
        "wrapper menerima biner di KERNELS_DIR",
        wrapper.validate_engine_binary(str(inside)) == os.path.realpath(inside),
    )
    outside = WORK / "luar" / "chrome"
    build_mock(outside, "Chromium 155.0.8200.0")
    check(
        "wrapper menolak biner di luar ENGINE_DIR",
        wrapper.validate_engine_binary(str(outside)) is None,
    )
    shutil.rmtree(inside.parent, ignore_errors=True)

    if CC:
        print("== hitung instance gabungan lintas kernel")
        processes = []
        try:
            for name, data_name in (("sleep-a", "uji-a"), ("sleep-b", "uji-b")):
                target = launcher.KERNELS_DIR / name / "chrome"
                build_sleep_mock(target)
                processes.append(
                    subprocess.Popen(
                        [
                            str(target),
                            f"--user-data-dir=/dev/shm/antidetect/{data_name}",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                )
            time.sleep(0.4)
            live = wrapper.count_live_instances()
            check("dua kernel dihitung gabungan", live >= 2, live)
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


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
