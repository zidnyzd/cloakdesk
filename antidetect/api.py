"""ixBrowser-compatible local API server (subset), stdlib only."""
import base64
import json
import mimetypes
import os
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import (
    bookmarks,
    cookies,
    csvimport,
    extensions,
    iximport,
    kernels,
    launcher,
    passwords,
    profileio,
    proxycheck,
    proxytext,
    store,
    sync_agent,
    templates,
)
from .paths import DATA_DIR

ACTIONS = {}
UI_DIST_DIR = Path(__file__).resolve().parent.parent / "ui" / "dist"


class ApiError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message


def action(name):
    def wrap(fn):
        ACTIONS[name] = fn
        return fn
    return wrap


def _pid(params):
    pid = params.get("profile_id")
    if pid is None:
        raise ApiError(1001, "profile_id is required")
    return int(pid)


def _active_profile(conn, pid):
    row = store.profile_get(conn, pid)
    if not row or row["deleted_at"]:
        raise ApiError(1002, "profile not found")
    return row


def _event(kind, message, profile_id=None):
    """Best-effort local operation log; never break the main action."""
    try:
        with store.db() as conn:
            store.add_event(conn, kind, message, profile_id)
    except Exception:
        pass


def _profile_name(conn, pid):
    row = store.profile_get(conn, pid)
    return row["name"] if row else f"#{pid}"


def _proxy_label(data):
    host = str(data.get("proxy_ip") or "").strip()
    if host:
        port = data.get("proxy_port")
        return f"{host}:{port}" if port not in (None, "") else host
    note = str(data.get("note") or "").strip()
    if note:
        return note
    return f"#{data.get('id')}" if data.get("id") is not None else "tanpa alamat"


def _event_name(name, limit=80):
    text = " ".join(str(name or "").split()) or "tanpa nama"
    return text if len(text) <= limit else text[: limit - 3] + "..."


@action("engine-info")
def engine_info(params):
    kernel_rows = [
        {**kernel, "supports": launcher.kernel_support(kernel["path"])}
        for kernel in launcher.available_kernels()
    ]
    return {
        "path": str(launcher.ENGINE_DIR),
        "version": kernel_rows[0]["version"],
        "kernels": kernel_rows,
        "disable_spoofing_values": list(launcher.DISABLE_SPOOFING_VALUES),
        "disable_spoofing_effective": list(launcher.DISABLE_SPOOFING_EFFECTIVE),
        "webrtc_modes": list(launcher.WEBRTC_MODES),
        "max_kernel_version": launcher.max_kernel_version(),
        "default_kernel_id": kernels.get_default_kernel_id(),
    }


def _kernel_path_for(kernel_id):
    if kernel_id == kernels.BUILTIN_KERNEL_ID:
        return str(launcher.CHROME)
    return str(launcher.KERNELS_DIR / kernel_id / "chrome")


def _kernel_row(kernel_id):
    for row in launcher.available_kernels():
        if row["id"] == kernel_id:
            return {**row, "supports": launcher.kernel_support(row["path"])}
    raise ApiError(1103, "Kernel tidak ditemukan")


@action("kernel-list")
def kernel_list(params):
    rows = launcher.available_kernels()
    data = [{**row, "supports": launcher.kernel_support(row["path"])} for row in rows]
    return {
        "total": len(data),
        "default_kernel_id": kernels.get_default_kernel_id(),
        "data": data,
    }


@action("kernel-install")
def kernel_install(params):
    source_type = str(params.get("source_type") or "local_archive").strip()
    kernel_name = params.get("kernel_name")
    if source_type == "local_archive":
        source = params.get("archive_path") or params.get("source_path")
    elif source_type == "local_dir":
        source = params.get("source_path") or params.get("archive_path")
    else:
        raise ApiError(1000, "source_type harus local_archive atau local_dir")
    if not str(source or "").strip():
        field = "archive_path" if source_type == "local_archive" else "source_path"
        raise ApiError(1000, f"{field} wajib diisi")
    try:
        if source_type == "local_archive":
            installed = kernels.install_kernel_from_archive(source, kernel_name)
        else:
            installed = kernels.install_kernel_from_dir(source, kernel_name)
    except kernels.KernelError as error:
        raise ApiError(error.code, str(error))
    launcher.reset_kernel_cache(Path(installed["path"]))
    if params.get("set_default"):
        try:
            kernels.set_default_kernel_id(installed["id"])
        except kernels.KernelError as error:
            raise ApiError(error.code, str(error))
    _event("kernel-install", f'Kernel "{_event_name(installed["name"])}" dipasang')
    return _kernel_row(installed["id"])


@action("kernel-delete")
def kernel_delete(params):
    kernel_id = str(params.get("kernel_id") or "").strip()
    if not kernel_id:
        raise ApiError(1000, "kernel_id wajib diisi")
    binary = _kernel_path_for(kernel_id)
    try:
        result = kernels.delete_kernel(kernel_id)
    except kernels.KernelError as error:
        raise ApiError(error.code, str(error))
    launcher.reset_kernel_cache(Path(binary))
    _event("kernel-delete", f'Kernel "{_event_name(kernel_id)}" dihapus')
    return result


@action("kernel-set-default")
def kernel_set_default(params):
    kernel_id = str(params.get("kernel_id") or "").strip()
    if not kernel_id:
        raise ApiError(1000, "kernel_id wajib diisi")
    try:
        kernels.set_default_kernel_id(kernel_id)
    except kernels.KernelError as error:
        raise ApiError(error.code, str(error))
    _event("kernel-set-default", f'Kernel default diubah ke "{_event_name(kernel_id)}"')
    return {"default_kernel_id": kernel_id, "path": _kernel_path_for(kernel_id)}


def _apply_default_kernel(data):
    """Isi kernel_path dari kernel default saat klien tidak memilih kernel."""
    preferences = data.get("preference_config")
    if not isinstance(preferences, dict):
        preferences = {}
    if str(preferences.get("kernel_path") or "").strip():
        # Tetap segarkan kernel_id dan kernel_version agar payload sync tidak
        # membawa label basi bila klien hanya mengirim jalur kernel.
        if data.get("preference_config") is preferences:
            preferences = dict(preferences)
            preferences.update(launcher.kernel_reference(preferences))
            data["preference_config"] = preferences
        return
    default_id = kernels.get_default_kernel_id()
    if default_id != kernels.BUILTIN_KERNEL_ID:
        binary = launcher.KERNELS_DIR / default_id / "chrome"
        if not binary.is_file():
            return
        preferences = dict(preferences)
        preferences["kernel_path"] = str(binary)
    else:
        # Profil baru selalu membawa referensi eksplisit supaya perangkat lain
        # tidak menebak kernel dari preferensi default mesin asal.
        preferences = dict(preferences)
        preferences["kernel_path"] = str(launcher.CHROME)
    data["preference_config"] = preferences


def _align_kernel_metadata(data, stored=None):
    """Simpan referensi kernel portabel dan selaraskan identitas versi.

    preference_config menampung kernel_id, kernel_version, dan kernel_path
    lokal mesin ini. fingerprint_config.br_version serta ua ikut versi mayor
    biner yang sama supaya metadata profil tidak bertentangan dengan engine.
    """
    stored = stored if isinstance(stored, dict) else {}
    preferences = data.get("preference_config")
    incoming_preferences = isinstance(preferences, dict)
    if not incoming_preferences:
        preferences = stored.get("preference_config")
        if not isinstance(preferences, dict):
            return data
    preferences = dict(preferences)
    binary = launcher.kernel_for({"preference_config": preferences})
    if incoming_preferences:
        # kernel_reference hanya menghasilkan tiga kunci kernel, jadi gabungkan ke
        # preferensi yang masuk supaya extension_ids dan pengaturan lain tidak hilang.
        merged = dict(preferences)
        merged.update(launcher.kernel_reference(preferences, binary))
        data["preference_config"] = merged

    major = launcher.kernel_major_version(binary)
    if major <= 0:
        return data
    fingerprint = data.get("fingerprint_config")
    if not isinstance(fingerprint, dict):
        # Klien hanya mengubah preferensi: pakai fingerprint tersimpan agar
        # br_version dan ua tidak ketinggalan versi kernel baru.
        fingerprint = stored.get("fingerprint_config")
        if not isinstance(fingerprint, dict):
            return data
    fingerprint = dict(fingerprint)
    fingerprint["br_version"] = f"{major}.0.0.0"
    if fingerprint.get("ua"):
        fingerprint["ua"] = launcher.align_user_agent(
            fingerprint.get("ua"), major, fingerprint.get("platform"), fingerprint.get("system_version")
        )
    data["fingerprint_config"] = fingerprint
    return data


def _check_kernel_path(data):
    """Reject a profile kernel_path that is not an installed kernel."""
    preferences = data.get("preference_config")
    if not isinstance(preferences, dict):
        return
    raw = preferences.get("kernel_path")
    if raw in (None, ""):
        return
    try:
        chosen = str(Path(str(raw)).resolve())
    except (OSError, ValueError):
        chosen = ""
    available = {str(Path(kernel["path"]).resolve()) for kernel in launcher.available_kernels()}
    if chosen not in available:
        raise ApiError(1000, "kernel tidak tersedia")


def _check_preferences(data):
    try:
        launcher.validate_preferences(data.get("preference_config"))
    except launcher.PreferenceError as error:
        raise ApiError(1000, str(error))


def _check_fingerprint(data):
    config = data.get("fingerprint_config")
    if not isinstance(config, dict):
        return
    try:
        launcher.validate_fingerprint(config)
    except launcher.PreferenceError as error:
        raise ApiError(1000, str(error))


def _validate_cookie_payload(value):
    """ixBrowser rejects a cookie value that is not a JSON array (code 2006)."""
    if value in (None, ""):
        return
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            raise ApiError(2006, "Format cookie salah, harus berupa larik JSON")
        if not isinstance(parsed, list):
            raise ApiError(2006, "Format cookie salah, harus berupa larik JSON")
        return
    if not isinstance(value, list):
        raise ApiError(2006, "Format cookie salah, harus berupa larik JSON")


def _proxy_key(proxy_config):
    """MD5 of ip+port+user+password, as ixBrowser computes it.

    Only meaningful for a real proxy; direct and traffic-package modes have no
    key.
    """
    return store.proxy_key_for(proxy_config)


PROXY_EXTRA_FIELDS = ("use_system_proxy", "proxy_extraction_method")


def _apply_proxy_fields(params):
    """Field proxy yang datang di luar proxy_config dipindah ke proxy_config."""
    has_extra = any(field in params for field in PROXY_EXTRA_FIELDS)
    if "proxy_config" not in params and not has_extra:
        return
    config = params.get("proxy_config")
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ApiError(1000, "proxy_config harus berupa objek")
    for field in PROXY_EXTRA_FIELDS:
        if field in params and field not in config:
            config[field] = params[field]
    params["proxy_config"] = config


def _merge_preferences(stored, incoming):
    """Gabungkan preference_config lama dan baru, lalu normalkan."""
    if not isinstance(incoming, dict):
        raise ApiError(1000, "preference_config harus berupa objek")
    merged = dict(stored) if isinstance(stored, dict) else {}
    merged.update(incoming)
    try:
        launcher.validate_preferences(merged)
    except launcher.PreferenceError as error:
        raise ApiError(1000, str(error))
    return merged


def _merge_proxy(stored, incoming, params):
    """Gabungkan proxy_config lama dan baru, termasuk field tambahan."""
    config = dict(stored) if isinstance(stored, dict) else {}
    if incoming is not None:
        if not isinstance(incoming, dict):
            raise ApiError(1000, "proxy_config harus berupa objek")
        for key, value in incoming.items():
            if value is None:
                config.pop(key, None)
            else:
                config[key] = value
    for field in PROXY_EXTRA_FIELDS:
        if field in params and field not in config:
            config[field] = params[field]
    if str(config.get("proxy_type") or "direct").strip().lower() == "direct":
        # Proxy direct tidak menyimpan alamat, supaya tidak dipakai lagi.
        for field in ("proxy_ip", "proxy_port", "proxy_user", "proxy_password"):
            config.pop(field, None)
    return config


def _prepare_profile(params, stored=None):
    """Siapkan payload profil: preference dinormalisasi, proxy digabung."""
    payload = dict(params)
    preferences = payload.get("preference_config")
    if preferences is not None:
        old = stored.get("preference_config") if isinstance(stored, dict) else None
        payload["preference_config"] = _merge_preferences(old, preferences)
    _apply_proxy_fields(payload)
    if "proxy_config" in payload:
        old_proxy = stored.get("proxy_config") if isinstance(stored, dict) else None
        payload["proxy_config"] = _merge_proxy(old_proxy, payload["proxy_config"], payload)
        payload["proxy_key"] = _proxy_key(payload["proxy_config"])
    return payload


def _apply_cookie(profile_id, cookie):
    """Simpan cookie profil. Kegagalan menulis tidak menggagalkan aksi lain."""
    if cookie in (None, "", []):
        return
    try:
        cookies.update_cookies(profile_id, cookie)
    except cookies.CookieError as error:
        _event("cookie-simpan", f"Cookie profil #{profile_id} gagal disimpan: {error}", profile_id)


def _normalize_profile_payload(data):
    """Normalise the fields whose value conventions differ between SDK and bundle."""
    payload = dict(data)
    preferences = payload.get("preference_config")
    if isinstance(preferences, dict):
        preferences = dict(preferences)
        for key in (
            "show_bookmarks_bar",
            "auto_upload_bookmarks",
            "cookies_backup",
            "indexed_db_backup",
            "local_storage_backup",
            "extension_data_backup",
            "is_cookies_cache",
            "is_tabs_cache",
            "multi_instance",
            "block_permission_requests",
        ):
            if key in preferences:
                preferences[key] = launcher.normalize_switch(preferences.get(key))
        payload["preference_config"] = preferences
    proxy = payload.get("proxy_config")
    if isinstance(proxy, dict):
        proxy = dict(proxy)
        if "use_system_proxy" in proxy:
            proxy["use_system_proxy"] = launcher.normalize_use_system_proxy(
                proxy.get("use_system_proxy")
            )
        if "proxy_extraction_method" in proxy:
            proxy["proxy_extraction_method"] = launcher.normalize_proxy_extraction_method(
                proxy.get("proxy_extraction_method")
            )
        payload["proxy_config"] = proxy
    key = _proxy_key(payload.get("proxy_config"))
    if key:
        payload["proxy_key"] = key
    return payload


@action("profile-reset-seed")
def profile_reset_seed(params):
    pid = _pid(params)
    if launcher.is_open(pid):
        raise ApiError(1004, "close the profile first")
    with store.db() as conn:
        row = _active_profile(conn, pid)
        name = row["name"]
        store.update_profile(conn, pid, {"seed": launcher.rand_seed()})
        seed = store.latest_seed(conn, pid)
    _event("profile-reset-seed", f'Seed profil "{_event_name(name)}" direset', pid)
    return {"profile_id": pid, "seed": seed}


@action("profile-list")
def profile_list(params):
    page = int(params.get("page") or 1)
    limit = int(params.get("limit") or 10)
    tag_id = params.get("tab_id", params.get("tag_id"))
    try:
        with store.db() as conn:
            total, rows = store.list_profiles(
                conn,
                page=page,
                limit=limit,
                group_id=params.get("group_id"),
                name=params.get("name"),
                profile_id=params.get("profile_id"),
                tag_id=tag_id,
                sort_by=params.get("sort_by") or "created",
                sort_dir=params.get("sort_dir") or "desc",
            )
            now_epoch = int(time.time())
            leases_by_uuid = {}
            try:
                store._ensure_sync_schema(conn)
                lease_rows = conn.execute(
                    """SELECT profile_uuid, holder_device_name, is_local_holder, expires_at
                       FROM local_leases
                       WHERE expires_at > ?""",
                    (now_epoch,),
                ).fetchall()
                for lrow in lease_rows:
                    leases_by_uuid[lrow["profile_uuid"]] = {
                        "holder_device_name": lrow["holder_device_name"] or "",
                        "is_local": bool(lrow["is_local_holder"]),
                        "expires_at": int(lrow["expires_at"]),
                    }
            except Exception:
                pass
            items = []
            for r in rows:
                item = store.profile_to_dict(r)
                item["sync_lease"] = leases_by_uuid.get(item.get("uuid"))
                items.append(item)
    except ValueError as error:
        raise ApiError(1000, str(error))
    return {"total": total, "data": items}


LOG_KINDS = ("chrome", "gost")
LOG_READ_BYTES = 1024 * 1024
LOG_READ_LINES = 2000


def _read_profile_log(profile_id, kind):
    name = f"{kind}.log"
    public_path = launcher.PROFILES_DIR / str(profile_id) / name
    empty = {
        "exists": False,
        "path": str(public_path),
        "size": 0,
        "lines": 0,
        "content": "",
        "truncated": False,
    }
    try:
        root = launcher.PROFILES_DIR.resolve()
        base = (launcher.PROFILES_DIR / str(profile_id)).resolve()
        if not base.is_relative_to(root):
            return empty
        target = base / name
        if target.exists() or target.is_symlink():
            resolved = target.resolve()
            if not resolved.is_relative_to(base) or not resolved.is_relative_to(root):
                return empty
        stat = target.stat()
    except (OSError, RuntimeError):
        return empty
    if not target.is_file():
        return empty

    size = stat.st_size
    truncated = size > LOG_READ_BYTES
    try:
        with target.open("rb") as handle:
            if truncated:
                handle.seek(size - LOG_READ_BYTES)
                data = handle.read()
                cut = data.find(b"\n")
                data = data[cut + 1:] if cut >= 0 else b""
            else:
                data = handle.read()
    except FileNotFoundError:
        return empty
    except OSError as error:
        raise ApiError(1004, f"log tidak bisa dibaca: {error}")

    lines = data.decode("utf-8", errors="replace").splitlines()
    if len(lines) > LOG_READ_LINES:
        lines = lines[-LOG_READ_LINES:]
        truncated = True
    return {
        "exists": True,
        "path": str(public_path),
        "size": size,
        "lines": len(lines),
        "content": "\n".join(lines),
        "truncated": truncated,
    }


@action("profile-logs")
def profile_logs(params):
    kind = str(params.get("kind") or "chrome").strip().lower()
    if kind not in LOG_KINDS:
        raise ApiError(1000, "kind harus chrome atau gost")
    return _read_profile_log(_known_pid(params), kind)



@action("profile-create")
def profile_create(params):
    _validate_cookie_payload(params.get("cookie"))
    payload = _prepare_profile(params)
    _apply_default_kernel(payload)
    _check_kernel_path(payload)
    _check_preferences(payload)
    _check_fingerprint(payload)
    payload = _normalize_profile_payload(payload)
    _align_kernel_metadata(payload)
    with store.db() as conn:
        pid = store.create_profile(conn, payload)
        name = _profile_name(conn, pid)
    _apply_cookie(pid, params.get("cookie"))
    _event("profile-create", f'Profil "{_event_name(name)}" dibuat', pid)
    return {"profile_id": pid, "new_profile_id": pid}


@action("profile-update")
def profile_update(params):
    pid = _pid(params)
    _validate_cookie_payload(params.get("cookie"))
    with store.db() as conn:
        row = _active_profile(conn, pid)
        stored = store.profile_to_dict(row)
    payload = _prepare_profile(params, stored)
    _check_kernel_path(payload)
    _check_preferences(payload)
    _check_fingerprint(payload)
    payload = _normalize_profile_payload(payload)
    _align_kernel_metadata(payload, stored)
    with store.db() as conn:
        store.update_profile(conn, pid, payload)
        name = conn.execute("SELECT name FROM profiles WHERE id = ?", (pid,)).fetchone()["name"]
    _apply_cookie(pid, params.get("cookie"))
    _event("profile-update", f'Profil "{_event_name(name)}" diperbarui', pid)
    return True


@action("profile-delete")
def profile_delete(params):
    pid = _pid(params)
    launcher.close_profile(pid)
    with store.db() as conn:
        row = _active_profile(conn, pid)
        name = row["name"]
        store.soft_delete_profile(conn, pid)
    _event("profile-delete", f'Profil "{_event_name(name)}" dipindahkan ke keranjang sampah', pid)
    return True


@action("profile-recycle-list")
def profile_recycle_list(params):
    page = int(params.get("page") or 1)
    limit = int(params.get("limit") or 10)
    with store.db() as conn:
        total, rows = store.list_profiles(
            conn, page=page, limit=limit, name=params.get("name"), deleted=True
        )
        return {"total": total, "data": [store.profile_to_dict(r) for r in rows]}


@action("profile-restore")
def profile_restore(params):
    pid = _pid(params)
    with store.db() as conn:
        row = store.profile_get(conn, pid)
        if not row or not row["deleted_at"]:
            raise ApiError(1002, "profile not found")
        name = row["name"]
        store.restore_profile(conn, pid)
    _event("profile-restore", f'Profil "{_event_name(name)}" dipulihkan', pid)
    return True


@action("profile-delete-permanent")
def profile_delete_permanent(params):
    pid = _pid(params)
    with store.db() as conn:
        row = store.profile_get(conn, pid)
        if not row or not row["deleted_at"]:
            raise ApiError(1002, "profile not found")
        name = row["name"]
        store.delete_profile_permanent(conn, pid)
    _event("profile-delete-permanent", f'Profil "{_event_name(name)}" dihapus permanen', pid)
    return True


@action("profile-copy")
def profile_copy(params):
    pid = _pid(params)
    with store.db() as conn:
        row = _active_profile(conn, pid)
        source = row["name"]
        data = store.profile_to_dict(row)
        data.pop("profile_id")
        data.pop("uuid", None)
        data.pop("seed", None)
        data["name"] = params.get("name") or f"{row['name']} copy"
        new_id = store.create_profile(conn, data)
        new_name = _profile_name(conn, new_id)
    _event(
        "profile-copy",
        f'Profil "{_event_name(source)}" disalin menjadi "{_event_name(new_name)}"',
        new_id,
    )
    return {"profile_id": new_id}


@action("profile-export")
def profile_export(params):
    pid = _known_pid(params)
    try:
        bundle = profileio.export_profile(pid)
        name = bundle["profile"].get("name") or pid
        password = str(params.get("password") or "")
        if password:
            bundle = profileio.encrypt_bundle(bundle, password)
    except profileio.ProfileIOError as error:
        raise ApiError(1004, str(error))
    content = json.dumps(bundle, ensure_ascii=False, indent=2).encode()
    _event("profile-export", f'Profil "{_event_name(name)}" diekspor', pid)
    filename = profileio.export_filename(name)
    if password:
        filename = filename[:-5] + ".enc.json"
    return {
        "filename": filename,
        "content_base64": base64.b64encode(content).decode("ascii"),
        "encrypted": bool(password),
    }


@action("profile-import")
def profile_import(params):
    content = params.get("content_base64")
    try:
        if content not in (None, ""):
            bundle = profileio.decode_content(content)
        elif params.get("json") not in (None, ""):
            bundle = params.get("json")
        else:
            raise profileio.ProfileIOError("isi file profil kosong")
        if profileio.is_encrypted(bundle):
            password = str(params.get("password") or "")
            if not password:
                raise profileio.ProfileIOError("file profil terenkripsi, kata sandi wajib diisi")
            bundle = profileio.decrypt_bundle(bundle, password)
        created = profileio.import_profile(bundle)
    except profileio.ProfileIOError as error:
        raise ApiError(1000, str(error))
    _event(
        "profile-import",
        f'Profil "{_event_name(created.get("name"))}" diimpor',
        created.get("profile_id"),
    )
    return created


@action("profile-import-csv")
def profile_import_csv(params):
    try:
        result = csvimport.import_csv(params.get("text"))
    except csvimport.CsvImportError as error:
        raise ApiError(1000, str(error))
    _event(
        "profile-import-csv",
        f"{result['created']} profil diimpor dari CSV",
    )
    return result


def _bool_flag(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    raise ApiError(1000, "parameter boolean tidak valid")


def _validate_ixbrowser_zip_path(raw_path):
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ApiError(1000, "path zip wajib diisi")
    candidate = Path(raw_path.strip())
    if not candidate.is_absolute():
        raise ApiError(1000, "path harus absolut")
    if candidate.suffix.lower() != ".zip":
        raise ApiError(1000, "path harus berakhiran .zip")
    try:
        resolved = candidate.resolve()
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        raise ApiError(1000, "path tidak valid")
    if not resolved.is_relative_to(home) or resolved == home:
        raise ApiError(1000, "path harus berada di bawah direktori home")
    if resolved.suffix.lower() != ".zip":
        raise ApiError(1000, "path harus berakhiran .zip")
    if not resolved.is_file():
        raise ApiError(1000, "file zip tidak ditemukan")
    return resolved


def _decode_ixbrowser_zip_base64(value, label="zip"):
    if not isinstance(value, str):
        raise ApiError(1000, f"isi file {label} harus berupa base64")
    raw = value.strip()
    if not raw:
        raise ApiError(1000, f"isi file {label} kosong")
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1].strip()
    compact = "".join(raw.split())
    if not compact:
        raise ApiError(1000, f"isi file {label} kosong")
    max_b64_len = (iximport.MAX_ZIP_BYTES * 4) // 3 + 4
    if len(compact) > max_b64_len:
        raise ApiError(1000, f"ukuran {label} melebihi batas 50 MB")
    try:
        data = base64.b64decode(compact, validate=True)
    except (ValueError, TypeError):
        raise ApiError(1000, f"isi file {label} bukan base64 yang valid")
    if not data:
        raise ApiError(1000, f"isi file {label} kosong")
    if len(data) > iximport.MAX_ZIP_BYTES:
        raise ApiError(1000, f"ukuran {label} melebihi batas 50 MB")
    return data


def _write_temp_import_zip(data):
    tmp_dir = DATA_DIR / "imports" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=".zip", dir=str(tmp_dir))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return Path(tmp_path)


@action("profile-import-ixbrowser")
def profile_import_ixbrowser(params):
    raw_path = params.get("path")
    raw_content = params.get("content_base64")
    raw_metadata = params.get("metadata_base64")
    raw_filename = params.get("filename")

    if raw_path is not None and raw_content is not None:
        raise ApiError(1000, "pilih tepat satu antara path atau content_base64")
    if raw_path is None and raw_content is None:
        raise ApiError(1000, "path atau content_base64 wajib diisi")

    dry_run = _bool_flag(params.get("dry_run"), default=True)
    update_existing = _bool_flag(params.get("update_existing"), default=False)

    temp_files = []
    try:
        if raw_content is not None:
            if raw_filename is not None:
                if not isinstance(raw_filename, str):
                    raise ApiError(1000, "filename harus berupa string")
                clean_filename = raw_filename.strip()
                if clean_filename and not clean_filename.lower().endswith(".zip"):
                    raise ApiError(1000, "filename harus berakhiran .zip")
            zip_bytes = _decode_ixbrowser_zip_base64(raw_content, label="zip")
            zip_path = _write_temp_import_zip(zip_bytes)
            temp_files.append(zip_path)

            metadata_path = None
            if raw_metadata is not None:
                meta_bytes = _decode_ixbrowser_zip_base64(raw_metadata, label="metadata")
                metadata_path = _write_temp_import_zip(meta_bytes)
                temp_files.append(metadata_path)
        else:
            if raw_metadata is not None:
                raise ApiError(1000, "metadata_base64 hanya boleh dipakai bersama content_base64")
            zip_path = _validate_ixbrowser_zip_path(raw_path)
            metadata_path = None

        try:
            if dry_run:
                result = iximport.preview(zip_path, metadata_path=metadata_path)
                return {**result, "dry_run": True}
            result = iximport.apply_import(
                zip_path,
                update_existing=update_existing,
                metadata_path=metadata_path,
            )
        except iximport.IxImportError as error:
            raise ApiError(1000, str(error))
    finally:
        for tmp_file in temp_files:
            try:
                tmp_file.unlink()
            except OSError:
                pass

    _event(
        "profile-import-ixbrowser",
        f"Impor ixBrowser: {result['created']} dibuat, {result['updated']} diperbarui, "
        f"{result['skipped']} dilewati, {result['cookies']} cookie",
    )
    return {**result, "dry_run": False}


@action("profile-open")
def profile_open(params):
    pid = _pid(params)
    force_raw = params.get("force_lease") if "force_lease" in params else params.get("force")
    force_lease = _bool_flag(force_raw, default=False)
    force = _bool_flag(params.get("force"), default=False)
    with store.db() as conn:
        row = _active_profile(conn, pid)
        profile = store.profile_to_dict(row, include_seed=True)
        name = row["name"]
    profile["id"] = pid
    try:
        result = launcher.open_profile(
            profile, app_args=params.get("args"), force_lease=force_lease, force=force
        )
    except sync_agent.SyncLeaseError as error:
        raise ApiError(1009, str(error))
    except launcher.LaunchError as error:
        err_code = getattr(error, "code", None)
        if err_code == 1009 or "sedang dibuka di perangkat" in str(error):
            raise ApiError(1009, str(error))
        if err_code == 1010:
            raise ApiError(1010, str(error))
        raise ApiError(1003, str(error))
    with store.db() as conn:
        store.mark_opened(conn, pid)
    _event("profile-open", f'Profil "{_event_name(name)}" dibuka', pid)
    return result


@action("profile-open-with-random-fingerprint")
def profile_open_random(params):
    pid = _pid(params)
    force_raw = params.get("force_lease") if "force_lease" in params else params.get("force")
    force_lease = _bool_flag(force_raw, default=False)
    force = _bool_flag(params.get("force"), default=False)
    with store.db() as conn:
        row = _active_profile(conn, pid)
        name = row["name"]
        profile = store.profile_to_dict(row, include_seed=True)
        fp = dict(profile["fingerprint_config"])
        fp.update(params.get("fingerprint_config") or {})
        proxy = params.get("proxy_config") or profile["proxy_config"]
        store.update_profile(
            conn,
            pid,
            {"fingerprint_config": fp, "proxy_config": proxy, "seed": launcher.rand_seed()},
        )
        profile["fingerprint_config"] = fp
        profile["proxy_config"] = proxy
        profile["seed"] = store.latest_seed(conn, pid)
    profile["id"] = pid
    try:
        result = launcher.open_profile(
            profile, app_args=params.get("args"), force_lease=force_lease, force=force
        )
    except sync_agent.SyncLeaseError as error:
        raise ApiError(1009, str(error))
    except launcher.LaunchError as error:
        err_code = getattr(error, "code", None)
        if err_code == 1009 or "sedang dibuka di perangkat" in str(error):
            raise ApiError(1009, str(error))
        if err_code == 1010:
            raise ApiError(1010, str(error))
        raise ApiError(1003, str(error))
    with store.db() as conn:
        store.mark_opened(conn, pid)
    _event(
        "profile-open",
        f'Profil "{_event_name(name)}" dibuka dengan fingerprint acak',
        pid,
    )
    return result


@action("profile-close")
def profile_close(params):
    pid = _pid(params)
    launcher.close_profile(pid)
    with store.db() as conn:
        name = _profile_name(conn, pid)
    _event("profile-close", f'Profil "{_event_name(name)}" ditutup', pid)
    return True


@action("profile-close-in-batches")
def profile_close_batches(params):
    ids = params.get("profile_id")
    if not isinstance(ids, list):
        ids = [ids]
    closed = []
    with store.db() as conn:
        for pid in ids:
            launcher.close_profile(int(pid))
            closed.append((int(pid), _profile_name(conn, int(pid))))
    for pid, name in closed:
        _event("profile-close", f'Profil "{_event_name(name)}" ditutup', pid)
    return True


def _id_list(params):
    ids = params.get("profile_id")
    if not isinstance(ids, list):
        ids = [ids]
    result = []
    for item in ids:
        try:
            value = int(item)
        except (TypeError, ValueError):
            raise ApiError(1000, "profile_id harus berupa daftar id")
        if value not in result:
            result.append(value)
    if not result:
        raise ApiError(1000, "profile_id tidak boleh kosong")
    return result


@action("profile-update-groups-in-batches")
def profile_update_groups_batches(params):
    ids = _id_list(params)
    group_id = params.get("group_id")
    if group_id in (None, ""):
        raise ApiError(1103, "Grup tidak ditemukan")
    group_id = int(group_id)
    with store.db() as conn:
        group = conn.execute("SELECT title FROM groups WHERE id = ?", (group_id,)).fetchone()
        if not group:
            raise ApiError(1103, "Grup tidak ditemukan")
        for pid in ids:
            _active_profile(conn, pid)
        for pid in ids:
            store.update_profile(conn, pid, {"group_id": group_id})
    _event(
        "profile-update-groups-in-batches",
        f'{len(ids)} profil dipindahkan ke grup "{_event_name(group["title"])}"',
    )
    return True


BATCH_FIELDS = ("note", "color", "proxy_config", "fingerprint_config")


@action("profile-bookmark-add")
def profile_bookmark_add(params):
    pid = _known_pid(params)
    with store.db() as conn:
        name = _profile_name(conn, pid)
    try:
        result = bookmarks.add_bookmark(
            pid, params.get("name"), params.get("url"), params.get("folder")
        )
    except bookmarks.BookmarkError as error:
        raise ApiError(1004, str(error))
    _event(
        "profile-bookmark-add",
        f'Bookmark "{_event_name(result["name"])}" ditambahkan ke profil "{_event_name(name)}"',
        pid,
    )
    return result


@action("profile-bookmark-delete")
def profile_bookmark_delete(params):
    pid = _known_pid(params)
    with store.db() as conn:
        name = _profile_name(conn, pid)
    try:
        result = bookmarks.delete_bookmark(pid, params.get("bookmark_id"))
    except bookmarks.BookmarkError as error:
        raise ApiError(1004, str(error))
    _event(
        "profile-bookmark-delete",
        f'Bookmark "{_event_name(result["name"])}" dihapus dari profil "{_event_name(name)}"',
        pid,
    )
    return result


@action("profile-delete-saved-password")
def profile_delete_saved_password(params):
    pid = _known_pid(params)
    with store.db() as conn:
        name = _profile_name(conn, pid)
    origin = str(params.get("origin_url") or "")
    try:
        result = passwords.delete_password(pid, origin, params.get("username"))
    except passwords.PasswordError as error:
        raise ApiError(1004, str(error))
    _event(
        "profile-delete-saved-password",
        f'Password {origin} profil "{_event_name(name)}" dihapus',
        pid,
    )
    return result


@action("profile-batch-update")
def profile_batch_update(params):
    ids = _id_list(params)
    data = {}
    if "note" in params:
        note = params.get("note")
        data["note"] = None if note is None else str(note)
    if "color" in params:
        data["color"] = params.get("color")
    for key in ("proxy_config", "fingerprint_config"):
        if key in params:
            value = params.get(key)
            if not isinstance(value, dict):
                raise ApiError(1000, f"{key} harus berupa objek")
            data[key] = value
    if not data:
        raise ApiError(1000, f"Tidak ada field untuk diubah, pilih salah satu dari {', '.join(BATCH_FIELDS)}")
    _check_fingerprint(data)
    with store.db() as conn:
        for pid in ids:
            _active_profile(conn, pid)
        for pid in ids:
            store.update_profile(conn, pid, data)
    labels = {"note": "catatan", "color": "warna", "proxy_config": "proxy", "fingerprint_config": "fingerprint"}
    fields = ", ".join(labels[key] for key in data)
    _event("profile-batch-update", f"{len(ids)} profil diperbarui: {fields}")
    return {"updated": len(ids)}


@action("profile-opened-list")
def profile_opened_list(params):
    return launcher.opened_profiles()


@action("profile-opened-list-arrange-tile")
def profile_opened_list_arrange_tile(params):
    return launcher.arrange_opened_profiles(params)


@action("profile-opened-list-arrage-tile")
def profile_opened_list_arrage_tile(params):
    """Alias with the bundle's typo; both names must work."""
    return launcher.arrange_opened_profiles(params)


@action("native-client-profile-opened-list")
def native_opened_list(params):
    return []


@action("profile-open-state-reset")
def profile_open_state_reset(params):
    pid = _pid(params)
    launcher.forget(pid)
    with store.db() as conn:
        row = store.profile_get(conn, pid)
        if row and "uuid" in row.keys() and row["uuid"]:
            try:
                sync_agent.release_lease(conn, row["uuid"], force=True)
            except Exception:
                pass
    return True


@action("profile-random-fingerprint-configuration")
def profile_random_fingerprint(params):
    pid = _pid(params)
    with store.db() as conn:
        row = store.profile_get(conn, pid)
        if not row:
            raise ApiError(1002, "profile not found")
        seed = launcher.rand_seed()
        store.update_profile(conn, pid, {"seed": seed})
        return {"profile_id": pid, "fingerprint_config": store._loads(row["fingerprint_config"])}


def _known_pid(params):
    pid = _pid(params)
    with store.db() as conn:
        if not store.profile_get(conn, pid):
            raise ApiError(1002, "profile not found")
    return pid


@action("profile-get-cookies")
def profile_get_cookies(params):
    pid = _known_pid(params)
    try:
        return cookies.get_cookies(pid)
    except cookies.CookieError as error:
        raise ApiError(1004, str(error))


@action("profile-update-cookies")
def profile_update_cookies(params):
    pid = _known_pid(params)
    if "cookie" not in params:
        raise ApiError(1000, "cookie is required")
    _validate_cookie_payload(params.get("cookie"))
    with store.db() as conn:
        name = _profile_name(conn, pid)
    try:
        result = cookies.update_cookies(pid, params.get("cookie"))
    except cookies.CookieError as error:
        raise ApiError(1004, str(error))
    try:
        with store.db() as conn:
            row = store.profile_get(conn, pid)
            profile_uuid = row["uuid"] if row and "uuid" in row.keys() else None
            resolved_url, resolved_token = sync_agent._try_resolve_endpoint(conn)
            if profile_uuid and resolved_url and resolved_token:
                cookies_json = cookies.get_cookies(pid)
                cookie_list = json.loads(cookies_json) if cookies_json else []
                l_row = conn.execute(
                    "SELECT fencing_token FROM local_leases WHERE profile_uuid = ?",
                    (profile_uuid,),
                ).fetchone()
                fencing_token = int(l_row["fencing_token"] or 0) if l_row else 0
                sync_agent.upload_cookie_blob(
                    conn, pid, profile_uuid, cookie_list, fencing_token
                )
    except Exception:
        pass
    _event("profile-update-cookies", f'Cookie profil "{_event_name(name)}" diperbarui', pid)
    return result


@action("profile-clear-saved-user-password")
def profile_clear_saved_user_password(params):
    pid = _known_pid(params)
    with store.db() as conn:
        name = _profile_name(conn, pid)
    try:
        result = cookies.clear_saved_passwords(pid)
    except cookies.CookieError as error:
        raise ApiError(1004, str(error))
    _event(
        "profile-clear-saved-user-password",
        f'Password tersimpan profil "{_event_name(name)}" dihapus',
        pid,
    )
    return result


@action("profile-bookmarks")
def profile_bookmarks(params):
    pid = _known_pid(params)
    try:
        return bookmarks.list_bookmarks(pid)
    except bookmarks.BookmarkError as error:
        raise ApiError(1004, str(error))


@action("profile-saved-passwords")
def profile_saved_passwords(params):
    pid = _known_pid(params)
    try:
        return passwords.list_saved_passwords(pid)
    except passwords.PasswordError as error:
        raise ApiError(1004, str(error))


def _clear_cache(pid, with_cookies=False):
    if launcher.is_open(pid):
        raise ApiError(1004, "close the profile first")
    default = launcher.PROFILES_DIR / str(pid) / "data" / "Default"
    for name in ("Cache", "Code Cache", "GPUCache", "Service Worker"):
        shutil.rmtree(default / name, ignore_errors=True)
    if with_cookies:
        for name in ("Cookies", "Cookies-journal"):
            try:
                (default / name).unlink()
            except OSError:
                pass
    return True


@action("profile-clear-cache")
def profile_clear_cache(params):
    pid = _pid(params)
    with store.db() as conn:
        name = _profile_name(conn, pid)
    result = _clear_cache(pid)
    _event("profile-clear-cache", f'Cache profil "{_event_name(name)}" dibersihkan', pid)
    return result


@action("profile-clear-cache-and-cookies")
def profile_clear_cache_cookies(params):
    pid = _pid(params)
    with store.db() as conn:
        name = _profile_name(conn, pid)
    result = _clear_cache(pid, with_cookies=True)
    _event("profile-clear-cache-and-cookies", f'Cache dan cookie profil "{_event_name(name)}" dibersihkan', pid)
    return result


@action("profile-clear-cache-keep-extensions")
def profile_clear_cache_ext(params):
    pid = _pid(params)
    with store.db() as conn:
        name = _profile_name(conn, pid)
    result = _clear_cache(pid)
    _event(
        "profile-clear-cache-keep-extensions",
        f'Cache profil "{_event_name(name)}" dibersihkan, extension dipertahankan',
        pid,
    )
    return result


@action("empty-recycle-bin")
def empty_recycle_bin(params):
    with store.db() as conn:
        count = store.empty_recycle_bin(conn)
        orphans = _delete_orphan_proxies(conn)
    _event("empty-recycle-bin", f"Keranjang sampah dikosongkan, {count} profil dihapus permanen")
    if orphans:
        _event("empty-recycle-bin", f"{orphans} proxy tanpa profil dihapus")
    return {"count": count, "proxies": orphans}


def _delete_orphan_proxies(conn):
    """Remove custom proxies no profile references any more."""
    rows = conn.execute("SELECT id, proxy_type, proxy_ip, proxy_port, proxy_user FROM proxies").fetchall()
    removed = 0
    for row in rows:
        used = conn.execute(
            "SELECT 1 FROM profiles WHERE deleted_at IS NULL AND "
            "json_extract(proxy_config, '$.proxy_ip') = ? AND "
            "json_extract(proxy_config, '$.proxy_port') = ? LIMIT 1",
            (row["proxy_ip"], row["proxy_port"]),
        ).fetchone()
        if used:
            continue
        store.delete_proxy(conn, row["id"])
        removed += 1
    if removed:
        conn.commit()
    return removed


def _paginated(conn, table, page, limit):
    total = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM {table} ORDER BY id LIMIT ? OFFSET ?",
        (int(limit), (int(page) - 1) * int(limit)),
    ).fetchall()
    return {"total": total, "data": [dict(r) for r in rows]}


@action("group-list")
def group_list(params):
    with store.db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM groups").fetchone()["c"]
        rows = conn.execute(
            """SELECT id, title, sort, created_at,
                      (SELECT COUNT(*) FROM profiles
                        WHERE group_id = groups.id AND deleted_at IS NULL) AS profile_count
               FROM groups
               ORDER BY sort DESC, id ASC
               LIMIT ? OFFSET ?""",
            (
                int(params.get("limit") or 100),
                (int(params.get("page") or 1) - 1) * int(params.get("limit") or 100),
            ),
        ).fetchall()
        return {"total": total, "data": [dict(row) for row in rows]}


def _sort_value(params):
    try:
        return int(params.get("sort") or 0)
    except (TypeError, ValueError):
        raise ApiError(1000, "sort harus berupa bilangan bulat")


@action("group-create")
def group_create(params):
    title = str(params.get("title") or "").strip()
    if not title:
        raise ApiError(1101, "Nama grup wajib diisi")
    sort = _sort_value(params)
    with store.db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        duplicate = conn.execute(
            "SELECT 1 FROM groups WHERE lower(title) = lower(?)", (title,)
        ).fetchone()
        if duplicate:
            raise ApiError(1102, "Nama grup sudah dipakai")
        gid = store.create_group(conn, title, sort)
    _event("group-create", f'Grup "{_event_name(title)}" dibuat')
    return {"id": gid}


@action("group-update")
def group_update(params):
    gid = params.get("id")
    if gid is None:
        raise ApiError(1103, "Grup tidak ditemukan")
    gid = int(gid)
    with store.db() as conn:
        row = conn.execute("SELECT id, title FROM groups WHERE id = ?", (gid,)).fetchone()
        if not row:
            raise ApiError(1103, "Grup tidak ditemukan")

        new_title = None
        if "title" in params:
            new_title = str(params.get("title") or "").strip()
            if not new_title:
                raise ApiError(1101, "Nama grup wajib diisi")
            duplicate = conn.execute(
                "SELECT 1 FROM groups WHERE lower(title) = lower(?) AND id <> ?",
                (new_title, gid),
            ).fetchone()
            if duplicate:
                raise ApiError(1102, "Nama grup sudah dipakai")
        new_sort = _sort_value(params) if "sort" in params else None
        if new_title is not None or new_sort is not None:
            store.update_group(conn, gid, new_title, new_sort)
        title = conn.execute("SELECT title FROM groups WHERE id = ?", (gid,)).fetchone()["title"]
    _event("group-update", f'Grup "{_event_name(title)}" diperbarui')
    return True


@action("group-delete")
def group_delete(params):
    gid = params.get("id")
    if gid is None:
        raise ApiError(1103, "Grup tidak ditemukan")
    gid = int(gid)
    with store.db() as conn:
        row = conn.execute("SELECT id, title FROM groups WHERE id = ?", (gid,)).fetchone()
        if not row:
            raise ApiError(1103, "Grup tidak ditemukan")
        if gid == 1 or row["title"] == "Default":
            raise ApiError(1104, "Grup Default tidak bisa dihapus")
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM profiles WHERE group_id = ?", (gid,)
        ).fetchone()["c"]
        if count > 0:
            raise ApiError(
                1105,
                f"Grup masih berisi {count} profil (termasuk keranjang sampah), pindahkan dulu",
            )
        store.delete_group(conn, gid)
        title = row["title"]
    _event("group-delete", f'Grup "{_event_name(title)}" dihapus')
    return True


@action("tag-list")
def tag_list(params):
    with store.db() as conn:
        return _paginated(conn, "tags", params.get("page") or 1, params.get("limit") or 100)


@action("tag-create")
def tag_create(params):
    title = params.get("title") or "Tag"
    with store.db() as conn:
        tid = store.create_tag(conn, title)
    _event("tag-create", f'Tag "{_event_name(title)}" dibuat')
    return {"id": tid}


@action("tag-update")
def tag_update(params):
    tid = int(params.get("id") or 0)
    title = params.get("title")
    with store.db() as conn:
        store.update_tag(conn, tid, title)
    _event("tag-update", f'Tag "{_event_name(title)}" diperbarui')
    return True


@action("tag-delete")
def tag_delete(params):
    tid = int(params.get("id") or 0)
    with store.db() as conn:
        row = conn.execute("SELECT title FROM tags WHERE id = ?", (tid,)).fetchone()
        title = row["title"] if row else f"#{tid}"
        store.delete_tag(conn, tid)
    _event("tag-delete", f'Tag "{_event_name(title)}" dihapus')
    return True


def _check_to_dict(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "proxy_id": row["proxy_id"],
        "checked_at": row["checked_at"],
        "ok": bool(row["ok"]),
        "ip": row["ip"],
        "latency_ms": row["latency_ms"],
        "error": row["error"],
    }


@action("proxy-list")
def proxy_list(params):
    page = int(params.get("page") or 1)
    limit = int(params.get("limit") or 10)
    with store.db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM proxies").fetchone()["c"]
        rows = conn.execute(
            "SELECT * FROM proxies ORDER BY id LIMIT ? OFFSET ?",
            (int(limit), (int(page) - 1) * int(limit)),
        ).fetchall()
        data = []
        for row in rows:
            item = dict(row)
            item["last_check"] = _check_to_dict(store.latest_proxy_check(conn, row["id"]))
            data.append(item)
    return {"total": total, "data": data}


@action("proxy-import")
def proxy_import(params):
    try:
        result = proxytext.import_text(params.get("text"), params.get("default_type"))
    except proxytext.ProxyTextError as error:
        raise ApiError(1000, str(error))
    _event(
        "proxy-import",
        f"Impor proxy: {result['created']} dibuat, {result['skipped']} dilewati",
    )
    return result


@action("proxy-export")
def proxy_export(params):
    result = proxytext.export_proxies(params.get("include_credentials", False))
    _event("proxy-export", f"Ekspor {result['count']} proxy")
    return result


def _wants_validation(params):
    value = params.get("validate")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def _validate_check(params):
    """Cek proxy tanpa menyimpan dan tanpa menambah riwayat."""
    return proxycheck.check_proxy(
        {
            "proxy_type": params.get("proxy_type"),
            "proxy_ip": params.get("proxy_ip"),
            "proxy_port": params.get("proxy_port"),
            "proxy_user": params.get("proxy_user"),
            "proxy_password": params.get("proxy_password"),
        },
        record_history=False,
    )


def _validate_proxy(data):
    """Cek proxy sebelum simpan, gagal berarti jangan disimpan."""
    result = _validate_check(data)
    if not result.get("ok"):
        reason = str(result.get("error") or "cek proxy gagal")
        raise ApiError(1000, f"Proxy gagal diuji: {reason}")
    return result


@action("proxy-create")
def proxy_create(params):
    if _wants_validation(params):
        _validate_proxy(params)
    with store.db() as conn:
        proxy_id = store.create_proxy(conn, params)
    _event("proxy-create", f'Proxy "{_event_name(_proxy_label(params))}" ditambahkan')
    return {"id": proxy_id}


@action("proxy-update")
def proxy_update(params):
    proxy_id = int(params.get("id") or 0)
    with store.db() as conn:
        row = conn.execute("SELECT * FROM proxies WHERE id = ?", (proxy_id,)).fetchone()
        merged = dict(row) if row else {"id": proxy_id}
    merged.update({key: value for key, value in params.items() if value is not None})
    if _wants_validation(params):
        _validate_proxy(merged)
    with store.db() as conn:
        store.update_proxy(conn, proxy_id, params)
    _event("proxy-update", f'Proxy "{_event_name(_proxy_label(merged))}" diperbarui')
    return True


@action("proxy-delete")
def proxy_delete(params):
    proxy_id = int(params.get("id") or 0)
    with store.db() as conn:
        row = conn.execute("SELECT * FROM proxies WHERE id = ?", (proxy_id,)).fetchone()
        label = _proxy_label(dict(row)) if row else f"#{proxy_id}"
        store.delete_proxy(conn, proxy_id)
    _event("proxy-delete", f'Proxy "{_event_name(label)}" dihapus')
    return True


@action("proxy-check")
def proxy_check(params):
    return proxycheck.check_proxy(params)


@action("proxy-validate")
def proxy_validate(params):
    """Cek proxy tanpa menyimpan dan tanpa menambah riwayat."""
    return _validate_check(params)


@action("proxy-check-history")
def proxy_check_history(params):
    """Riwayat cek proxy. Tanpa proxy_id semua baris dikembalikan."""
    try:
        limit = int(params.get("limit") or 50)
    except (TypeError, ValueError):
        raise ApiError(1000, "limit harus berupa bilangan bulat")
    proxy_id = params.get("proxy_id")
    if proxy_id not in (None, ""):
        try:
            proxy_id = int(proxy_id)
        except (TypeError, ValueError):
            raise ApiError(1000, "proxy_id tidak valid")
    with store.db() as conn:
        total, rows = store.list_proxy_checks(conn, proxy_id, limit)
        data = [_check_to_dict(row) for row in rows]
    return {"total": total, "data": data}


@action("proxy-check-history-clear")
def proxy_check_history_clear(params):
    proxy_id = params.get("proxy_id")
    if proxy_id in (None, ""):
        raise ApiError(1000, "proxy_id wajib diisi")
    try:
        proxy_id = int(proxy_id)
    except (TypeError, ValueError):
        raise ApiError(1000, "proxy_id tidak valid")
    with store.db() as conn:
        count = store.clear_proxy_checks(conn, proxy_id)
    _event("proxy-check-history-clear", f"Riwayat cek proxy #{proxy_id} dihapus ({count} baris)")
    return {"count": count}


@action("template-list")
def template_list(params):
    try:
        page = int(params.get("page") or 1)
        limit = int(params.get("limit") or 10)
    except (TypeError, ValueError):
        raise ApiError(1000, "page dan limit harus berupa bilangan bulat")
    with store.db() as conn:
        return templates.list_templates(conn, page=page, limit=limit)


@action("template-save")
def template_save(params):
    try:
        with store.db() as conn:
            result = templates.save_template(
                conn,
                params.get("profile_id"),
                params.get("name"),
                params.get("profile_ids"),
            )
    except templates.TemplateError as error:
        raise ApiError(1000, str(error))
    _event("template-save", f'Template "{_event_name(result["name"])}" disimpan')
    return result


@action("template-delete")
def template_delete(params):
    template_id = params.get("id")
    if template_id in (None, ""):
        raise ApiError(1000, "id template wajib diisi")
    try:
        with store.db() as conn:
            result = templates.delete_template(conn, template_id)
    except templates.TemplateError as error:
        raise ApiError(1000, str(error))
    _event("template-delete", f'Template "{_event_name(result["name"])}" dihapus')
    return True


@action("template-create-profile")
def template_create_profile(params):
    template_id = params.get("id")
    if template_id in (None, ""):
        raise ApiError(1000, "id template wajib diisi")
    try:
        with store.db() as conn:
            _, snapshot = templates.template_snapshot(
                conn, template_id, params.get("index") or 0
            )
            _check_kernel_path(snapshot)
            _check_preferences(snapshot)
            result = templates.create_profile_from_template(
                conn, template_id, params.get("name"), params.get("index") or 0
            )
    except templates.TemplateError as error:
        raise ApiError(1000, str(error))
    _event(
        "template-create-profile",
        f'Profil "{_event_name(result["name"])}" dibuat dari template',
        result["profile_id"],
    )
    return result


@action("traffic-package-list")
def traffic_package_list(params):
    return {"total": 0, "data": []}


@action("extension-list")
def extension_list(params):
    try:
        return extensions.list_extensions(
            params.get("page") or 1,
            params.get("limit") or 10,
            str(params.get("name") or "").strip() or None,
        )
    except (TypeError, ValueError):
        raise ApiError(1200, "page dan limit harus berupa bilangan bulat")


@action("extension-upload")
def extension_upload(params):
    filename = params.get("filename") or params.get("file_name")
    content = params.get("content") or params.get("content_base64")
    try:
        result = extensions.upload(filename, content)
    except extensions.ExtensionError as error:
        raise ApiError(1201, str(error))
    _event(
        "extension-upload",
        f'Extension "{_event_name(result.get("title"))}" diunggah',
    )
    return result


@action("extension-delete")
def extension_delete(params):
    extension_id = params.get("id", params.get("extension_id"))
    if extension_id in (None, ""):
        raise ApiError(1200, "id extension wajib diisi")
    with store.db() as conn:
        row = conn.execute(
            "SELECT title FROM extensions WHERE id = ?", (int(extension_id),)
        ).fetchone()
        title = row["title"] if row else f"#{extension_id}"
    try:
        result = extensions.delete_extension(extension_id)
    except extensions.ExtensionError as error:
        raise ApiError(1202, str(error))
    _event("extension-delete", f'Extension "{_event_name(title)}" dihapus')
    return result


@action("gateway-list")
def gateway_list(params):
    return []


@action("proxy-tag-list")
def proxy_tag_list(params):
    return {"total": 0, "data": []}


@action("proxy-tag-create")
def proxy_tag_create(params):
    return True


@action("proxy-tag-update")
def proxy_tag_update(params):
    return True


@action("proxy-tag-delete")
def proxy_tag_delete(params):
    return True


@action("event-list")
def event_list(params):
    try:
        page = max(1, int(params.get("page") or 1))
        limit = min(2000, max(1, int(params.get("limit") or 20)))
        start, end = store.parse_event_range(params.get("start"), params.get("end"))
    except (TypeError, ValueError) as error:
        raise ApiError(1000, str(error))
    with store.db() as conn:
        total, rows = store.list_events(
            conn,
            page=page,
            limit=limit,
            kind=params.get("kind"),
            start=start,
            end=end,
        )
        return {"total": total, "data": [dict(row) for row in rows]}


EVENT_EXPORT_LIMIT = 5000


@action("event-export")
def event_export(params):
    try:
        start, end = store.parse_event_range(params.get("start"), params.get("end"))
    except ValueError as error:
        raise ApiError(1000, str(error))
    with store.db() as conn:
        total, rows = store.list_events(
            conn,
            page=1,
            limit=EVENT_EXPORT_LIMIT,
            kind=params.get("kind"),
            start=start,
            end=end,
            q=params.get("q"),
        )
        csv_text = store.events_csv(rows)
    filename = f"log-aktivitas-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    return {
        "filename": filename,
        "content_base64": base64.b64encode(csv_text.encode("utf-8")).decode("ascii"),
        "count": len(rows),
        "truncated": total > len(rows),
    }



@action("event-clear")
def event_clear(params):
    with store.db() as conn:
        return {"count": store.clear_events(conn)}


@action("sync-status")
def sync_status(params):
    with store.db() as conn:
        status = sync_agent.get_status(conn)
        now_epoch = int(time.time())
        lease_rows = conn.execute(
            """SELECT profile_uuid, holder_device_id, holder_device_name,
                      is_local_holder, expires_at
               FROM local_leases
               WHERE expires_at > ?
               ORDER BY profile_uuid""",
            (now_epoch,),
        ).fetchall()
        status["active_leases"] = [
            {
                "profile_uuid": row["profile_uuid"],
                "holder_device_id": row["holder_device_id"],
                "holder_device_name": row["holder_device_name"] or "",
                "is_local_holder": bool(row["is_local_holder"]),
                "is_local": bool(row["is_local_holder"]),
                "expires_at": int(row["expires_at"]),
            }
            for row in lease_rows
        ]
        conflict_count = conn.execute(
            "SELECT COUNT(*) AS c FROM local_conflicts WHERE resolved = 0"
        ).fetchone()["c"]
        status["conflict_count"] = int(conflict_count)
        return status


@action("sync-configure")
def sync_configure(params):
    server_url = params.get("server_url")
    enrollment_token = params.get("enrollment_token")
    device_name = params.get("device_name")
    passphrase = params.get("passphrase")
    try:
        reg = sync_agent.register_device(
            server_url=server_url,
            enrollment_token=enrollment_token,
            device_name=device_name,
            passphrase=passphrase,
        )
        with store.db() as conn:
            cycle = sync_agent.sync_cycle(conn)
    except sync_agent.SyncAgentError as error:
        raise ApiError(int(error.code or 1000), str(error))
    return {
        "status": "configured",
        "device_id": reg["device_id"],
        "device_name": reg.get("device_name"),
        "server_url": reg.get("server_url"),
        "pushed": int(cycle.get("pushed") or 0),
        "pulled": int(cycle.get("pulled") or 0),
    }


@action("sync-trigger")
def sync_trigger(params):
    try:
        with store.db() as conn:
            res = sync_agent.sync_cycle(conn)
    except sync_agent.SyncAgentError as error:
        raise ApiError(int(error.code or 1000), str(error))
    return {
        "pushed": int(res.get("pushed") or 0),
        "pulled": int(res.get("pulled") or 0),
        "last_pulled_seq": int(res.get("last_pulled_seq") or 0),
    }


def _conflict_entity_name(conn, entity_type, entity_uuid, losing_payload, winning_payload):
    if entity_type in ("profile", "cookie_blob") and entity_uuid:
        row = conn.execute(
            "SELECT name FROM profiles WHERE uuid = ?", (entity_uuid,)
        ).fetchone()
        if row and row["name"]:
            return row["name"]
    elif entity_type == "group" and entity_uuid:
        row = conn.execute(
            "SELECT title FROM groups WHERE uuid = ?", (entity_uuid,)
        ).fetchone()
        if row and row["title"]:
            return row["title"]
    elif entity_type == "tag" and entity_uuid:
        row = conn.execute(
            "SELECT title FROM tags WHERE uuid = ?", (entity_uuid,)
        ).fetchone()
        if row and row["title"]:
            return row["title"]
    elif entity_type == "template" and entity_uuid:
        row = conn.execute(
            "SELECT name FROM templates WHERE uuid = ?", (entity_uuid,)
        ).fetchone()
        if row and row["name"]:
            return row["name"]
    for payload in (winning_payload, losing_payload):
        if isinstance(payload, dict):
            label = payload.get("name") or payload.get("title")
            if label:
                return str(label)
    return entity_uuid or "-"


@action("sync-conflicts")
def sync_conflicts(params):
    with store.db() as conn:
        store._ensure_sync_schema(conn)
        rows = conn.execute(
            """SELECT conflict_id, entity_type, entity_uuid, reason,
                      losing_payload, winning_payload, resolved, created_at
               FROM local_conflicts
               WHERE resolved = 0
               ORDER BY created_at DESC"""
        ).fetchall()
        items = []
        for row in rows:
            losing_data = store._loads(row["losing_payload"])
            winning_data = store._loads(row["winning_payload"])
            items.append(
                {
                    "conflict_id": row["conflict_id"],
                    "entity_type": row["entity_type"],
                    "entity_uuid": row["entity_uuid"],
                    "entity_name": _conflict_entity_name(
                        conn,
                        row["entity_type"],
                        row["entity_uuid"],
                        losing_data,
                        winning_data,
                    ),
                    "reason": row["reason"],
                    "losing_payload": losing_data,
                    "winning_payload": winning_data,
                    "resolved": bool(row["resolved"]),
                    "created_at": row["created_at"],
                }
            )
        return items


@action("sync-conflict-resolve")
def sync_conflict_resolve(params):
    conflict_id = str(params.get("conflict_id") or "").strip()
    if not conflict_id:
        raise ApiError(1001, "conflict_id wajib diisi")
    resolution = str(params.get("resolution") or "").strip()
    if resolution not in ("keep_local", "keep_server"):
        raise ApiError(1000, "resolution harus keep_local atau keep_server")
    with store.db() as conn:
        store._ensure_sync_schema(conn)
        row = conn.execute(
            "SELECT * FROM local_conflicts WHERE conflict_id = ?",
            (conflict_id,),
        ).fetchone()
        if not row:
            raise ApiError(1002, "konflik tidak ditemukan")
        conn.execute(
            "UPDATE local_conflicts SET resolved = 1 WHERE conflict_id = ?",
            (conflict_id,),
        )
        if resolution == "keep_local":
            entity_type = row["entity_type"]
            entity_uuid = row["entity_uuid"]
            target_type = "profile" if entity_type == "cookie_blob" else entity_type
            table = sync_agent.ENTITY_TABLES.get(target_type)
            if table and entity_uuid:
                conn.execute(
                    f"UPDATE {table} SET sync_dirty = 1 WHERE uuid = ?",
                    (entity_uuid,),
                )
                store.enqueue_dirty_entities(conn)
        conn.commit()
    return {"resolved": True}


MAX_CONCURRENT_REQUESTS = 64
_busy = threading.Semaphore(MAX_CONCURRENT_REQUESTS)


class Handler(BaseHTTPRequestHandler):
    server_version = "antidetect/0.1"

    def _route(self):
        return urlsplit(self.path).path

    def do_POST(self):
        if not self._route().startswith("/api/v2/"):
            self._json(404, {"error": {"code": 404, "message": "not found"}})
            return
        name = self._route()[len("/api/v2/"):].strip("/")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            params = json.loads(raw) if raw else {}
            if not isinstance(params, dict):
                raise ValueError
        except ValueError:
            self._json(200, {"error": {"code": 1000, "message": "body must be a JSON object"}})
            return

        fn = ACTIONS.get(name)
        if fn is None:
            self._json(200, {"error": {"code": 1001, "message": f"unknown action: {name}"}})
            return
        if not _busy.acquire(blocking=False):
            self._json(
                200,
                {"error": {"code": 1008, "message": "Server sibuk, coba lagi nanti."}},
            )
            return
        try:
            data = fn(params)
        except ApiError as error:
            self._json(200, {"error": {"code": error.code, "message": error.message}})
            return
        except Exception as error:
            self._json(200, {"error": {"code": 1000, "message": str(error)}})
            return
        finally:
            _busy.release()
        self._json(200, {"error": {"code": 0, "message": "success"}, "data": data})

    def _serve_static(self, head_only=False):
        if not UI_DIST_DIR.is_dir():
            return False

        route = self._route()
        rel = route.lstrip("/")
        if not rel or rel == "index.html":
            target = UI_DIST_DIR / "index.html"
        else:
            target = (UI_DIST_DIR / rel).resolve()
            if not str(target).startswith(str(UI_DIST_DIR.resolve())) or not target.is_file():
                if not Path(rel).suffix:
                    target = UI_DIST_DIR / "index.html"
                else:
                    return False

        if not target.is_file():
            return False

        mime, _ = mimetypes.guess_type(str(target))
        if not mime:
            mime = "application/octet-stream"
        if mime.startswith("text/") or mime in ("application/javascript", "image/svg+xml"):
            mime += "; charset=utf-8"

        try:
            content = target.read_bytes()
        except OSError:
            self._json(500, {"error": {"code": 500, "message": "Gagal membaca berkas UI"}})
            return True

        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        if target.suffix in (".js", ".css", ".svg", ".png", ".woff2"):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not head_only:
            self.wfile.write(content)
        return True

    def do_GET(self):
        route = self._route()
        if route == "/health":
            self._json(200, {"status": "ok", "open": len(launcher.opened_profiles())})
            return
        if route.startswith("/api/v2/"):
            self._json(200, {"error": {"code": 1007, "message": "Gunakan metode POST."}})
            return

        accept = self.headers.get("Accept", "")
        if route == "/" and "application/json" in accept and "text/html" not in accept:
            self._json(200, {"status": "ok", "open": len(launcher.opened_profiles())})
            return

        if self._serve_static(head_only=False):
            return

        if route == "/":
            self._json(200, {"status": "ok", "open": len(launcher.opened_profiles())})
            return

        self._json(404, {"error": {"code": 404, "message": "not found"}})

    def do_HEAD(self):
        route = self._route()
        if route.startswith("/api/v2/"):
            self._json(200, {"error": {"code": 1007, "message": "Gunakan metode POST."}})
            return
        if route == "/health":
            self._json(200, {"status": "ok", "open": len(launcher.opened_profiles())})
            return
        if self._serve_static(head_only=True):
            return
        if route == "/":
            self._json(200, {"status": "ok", "open": len(launcher.opened_profiles())})
            return
        self._json(404, {"error": {"code": 404, "message": "not found"}})

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def _origin_allowed(self):
        origin = self.headers.get("Origin")
        if not origin:
            return None
        extra = {
            item.strip().rstrip("/")
            for item in os.environ.get("ANTIDETECT_ALLOWED_ORIGINS", "").split(",")
            if item.strip()
        }
        if origin.rstrip("/") in extra:
            return origin
        try:
            parts = urlsplit(origin)
        except ValueError:
            return None
        if parts.scheme in ("http", "https") and parts.hostname in ("127.0.0.1", "localhost", "::1"):
            return origin
        return None

    def _send_cors_headers(self):
        origin = self._origin_allowed()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def serve(host="127.0.0.1", port=53200):
    store.init()
    store.open_wal_anchor()
    sync_agent.start_heartbeat()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"antidetect API listening on http://{host}:{port}/api/v2/")
    try:
        httpd.serve_forever()
    finally:
        sync_agent.stop_heartbeat()
        store.close_wal_anchor()
