#!/usr/bin/env python3
"""Uji perbaikan preferensi extension dan judul manifest i18n.

Tanpa membuka engine browser. State ditulis ke direktori sementara di
~/.hermes/cache/scratch/extension-prefs lalu dihapus setelah tes selesai.
"""
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRATCH = Path(
    os.environ.get("ANTIDETECT_EXT_PREF_TEST_DIR")
    or Path.home() / ".hermes" / "cache" / "scratch" / "extension-prefs"
)
SCRATCH.mkdir(parents=True, exist_ok=True)
ROOT = Path(tempfile.mkdtemp(prefix="run-", dir=SCRATCH))
HOME_DIR = ROOT / "home"
ENGINE_DIR = ROOT / "engine"
for folder in (HOME_DIR, ENGINE_DIR):
    folder.mkdir(parents=True, exist_ok=True)

os.environ["ANTIDETECT_HOME"] = str(HOME_DIR)
os.environ["ANTIDETECT_ENGINE_DIR"] = str(ENGINE_DIR)
os.environ["ANTIDETECT_ENGINE_GUARD"] = "0"
sys.path.insert(0, str(REPO))

from antidetect import api, extensions, launcher, store  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"OK   {name}")
    else:
        print(f"GAGAL {name} {detail}".rstrip())
        FAILURES.append(name)


def _build_mock(binary, version_line):
    binary.parent.mkdir(parents=True, exist_ok=True)
    cc = shutil.which("cc") or shutil.which("gcc")
    if cc:
        source = binary.with_suffix(".c")
        source.write_text(
            "#include <stdio.h>\n"
            'int main(void) { puts("%s"); return 0; }\n' % version_line
        )
        subprocess.run([cc, str(source), "-o", str(binary)], check=True, capture_output=True)
        source.unlink()
    else:
        binary.write_text(f"#!/bin/sh\necho '{version_line}'\n")
    binary.chmod(0o755)


def _zip_b64(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            if isinstance(content, (dict, list)):
                content = json.dumps(content)
            zf.writestr(name, content)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_align_kernel_metadata_merges():
    print("== kernel_reference digabung, bukan mengganti")
    _build_mock(launcher.CHROME, "Chromium 150.0.7839.0")
    data = {
        "preference_config": {
            "extension_ids": [12, 99],
            "window_width": 1280,
            "window_height": 800,
            "startup_args": "--foo",
            "is_tabs_cache": 1,
        }
    }
    api._align_kernel_metadata(data)
    pref = data["preference_config"]
    check("extension_ids tetap ada", pref.get("extension_ids") == [12, 99], pref)
    check("window_width tetap ada", pref.get("window_width") == 1280, pref)
    check("window_height tetap ada", pref.get("window_height") == 800, pref)
    check("startup_args tetap ada", pref.get("startup_args") == "--foo", pref)
    check("is_tabs_cache tetap ada", pref.get("is_tabs_cache") == 1, pref)
    check("kernel_id terisi", bool(pref.get("kernel_id")), pref)
    check("kernel_version terisi", bool(pref.get("kernel_version")), pref)
    check("kernel_path terisi", bool(pref.get("kernel_path")), pref)


def test_profile_update_preserves_preferences():
    print("== update preferensi lewat api dan store mempertahankan kunci")
    store.init()
    created = api.profile_create(
        {
            "name": "Uji Pref Extension",
            "preference_config": {"extension_ids": [1], "window_width": 1024, "window_height": 768},
        }
    )
    pid = created["profile_id"]
    with store.db() as conn:
        row = store.profile_get(conn, pid)
        stored = store.profile_to_dict(row)
    pref = stored["preference_config"]
    check("create: extension_ids ada", pref.get("extension_ids") == [1], pref)
    check("create: window_width ada", pref.get("window_width") == 1024, pref)
    check("create: kernel_id ada", bool(pref.get("kernel_id")), pref)
    check("create: kernel_version ada", bool(pref.get("kernel_version")), pref)

    # Update hanya mengirim sebagian preferensi; merge harus mempertahankan sisanya + kernel.
    api.profile_update({"profile_id": pid, "preference_config": {"window_width": 1280}})
    with store.db() as conn:
        row = store.profile_get(conn, pid)
        stored2 = store.profile_to_dict(row)
    pref2 = stored2["preference_config"]
    check("update: extension_ids tidak hilang", pref2.get("extension_ids") == [1], pref2)
    check("update: window_width diperbarui", pref2.get("window_width") == 1280, pref2)
    check("update: window_height tidak hilang", pref2.get("window_height") == 768, pref2)
    check("update: kernel_id tetap ada", bool(pref2.get("kernel_id")), pref2)
    check("update: kernel_version tetap ada", bool(pref2.get("kernel_version")), pref2)

    # Update dengan extension_ids dan window_width bersamaan (kasus dari task).
    api.profile_update(
        {"profile_id": pid, "preference_config": {"extension_ids": [12], "window_width": 1280}}
    )
    with store.db() as conn:
        row = store.profile_get(conn, pid)
        stored3 = store.profile_to_dict(row)
    pref3 = stored3["preference_config"]
    for key in ("extension_ids", "window_width", "kernel_id", "kernel_version"):
        check(f"kasus task: {key} ada", key in pref3 and pref3.get(key) not in (None, ""), pref3)
    check("kasus task: extension_ids benar", pref3.get("extension_ids") == [12], pref3)
    check("kasus task: window_width benar", pref3.get("window_width") == 1280, pref3)


def test_extension_i18n_title():
    print("== judul extension dari placeholder __MSG_")
    extensions.EXTENSIONS_DIR = HOME_DIR / "extensions"
    extensions.EXTENSIONS_DIR.mkdir(parents=True, exist_ok=True)

    content = _zip_b64(
        {
            "manifest.json": {
                "manifest_version": 3,
                "name": "__MSG_extName__",
                "version": "5.5.1",
                "default_locale": "en",
            },
            "_locales/en/messages.json": {
                "extName": {"message": "Tampermonkey", "description": "Name"}
            },
        }
    )
    result = extensions.upload("tampermonkey.zip", content)
    check("title diselesaikan", result.get("title") == "Tampermonkey", result)
    check("slug wajar", result.get("slug") == "tampermonkey", result)
    check("slug bukan msg-extname", result.get("slug") != "msg-extname", result)

    # Tanpa _locales: pakai nilai mentah.
    content2 = _zip_b64(
        {
            "manifest.json": {
                "manifest_version": 3,
                "name": "__MSG_missing__",
                "version": "1.0.0",
                "default_locale": "en",
            }
        }
    )
    result2 = extensions.upload("missing-locale.zip", content2)
    check("tanpa locales: title mentah", result2.get("title") == "__MSG_missing__", result2)
    check("tanpa locales: slug dari mentah", result2.get("slug") == "msg-missing", result2)

    # Nama biasa tanpa placeholder tetap normal.
    content3 = _zip_b64(
        {"manifest.json": {"manifest_version": 3, "name": "My Normal Ext", "version": "1.0"}}
    )
    result3 = extensions.upload("normal.zip", content3)
    check("tanpa placeholder: title normal", result3.get("title") == "My Normal Ext", result3)
    check("tanpa placeholder: slug normal", result3.get("slug") == "my-normal-ext", result3)


def main():
    try:
        test_align_kernel_metadata_merges()
        test_profile_update_preserves_preferences()
        test_extension_i18n_title()
    except Exception as exc:
        import traceback

        traceback.print_exc()
        FAILURES.append(f"exception: {exc}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} tes gagal: {', '.join(FAILURES)}")
        return 1
    print("Semua tes extension-prefs lulus.")
    return 0


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
