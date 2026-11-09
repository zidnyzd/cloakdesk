"""Local extension storage: upload, list, delete unpacked Chromium extensions."""
import base64
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import struct
import uuid
import zipfile
from pathlib import Path

from . import store
from .paths import DATA_DIR

EXTENSIONS_DIR = DATA_DIR / "extensions"
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
CRX_MAGIC = b"Cr24"


class ExtensionError(Exception):
    pass


def _extensions_dir(conn=None):
    from . import launcher, paths

    if EXTENSIONS_DIR != paths.DATA_DIR / "extensions":
        return EXTENSIONS_DIR
    if conn is not None:
        db_file = store._conn_db_path(conn)
        if db_file and db_file != store._INITIAL_DB_PATH:
            return db_file.parent / "extensions"
    if Path(store.DB_PATH) != store._INITIAL_DB_PATH:
        return Path(store.DB_PATH).parent / "extensions"
    if launcher.PROFILES_DIR != paths.DATA_DIR / "profiles":
        return launcher.PROFILES_DIR.parent / "extensions"
    env_home = os.environ.get("ANTIDETECT_HOME")
    if env_home:
        return Path(env_home) / "extensions"
    return EXTENSIONS_DIR


_MSG_PATTERN = re.compile(r"^__MSG_(.+?)__$")


def _resolve_manifest_name(archive, member, manifest):
    """Selesaikan placeholder __MSG_key__ di manifest.name pakai _locales/<default_locale>/messages.json."""
    raw_name = str(manifest.get("name") or "").strip()
    if not raw_name:
        return raw_name
    match = _MSG_PATTERN.match(raw_name)
    if not match:
        return raw_name
    default_locale = str(manifest.get("default_locale") or "").strip()
    if not default_locale or "/" in default_locale or "\\" in default_locale or ".." in default_locale:
        return raw_name
    prefix = member[: -len("manifest.json")]
    locales_member = f"{prefix}_locales/{default_locale}/messages.json"
    try:
        raw = archive.read(locales_member)
    except KeyError:
        return raw_name
    except Exception:
        return raw_name
    try:
        messages = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return raw_name
    if not isinstance(messages, dict):
        return raw_name
    entry = messages.get(match.group(1))
    if not isinstance(entry, dict):
        return raw_name
    resolved = str(entry.get("message") or "").strip()
    return resolved or raw_name


def _slugify(name):
    slug = "".join(char if char.isalnum() else "-" for char in str(name or "").lower())
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "extension"


def _unique_slug(base):
    ext_dir = _extensions_dir()
    with store.db() as conn:
        taken = {row["slug"] for row in conn.execute("SELECT slug FROM extensions")}
    slug = base
    index = 2
    while slug in taken or (ext_dir / slug).exists():
        slug = f"{base}-{index}"
        index += 1
    return slug


def _decode(content):
    raw = str(content or "").strip()
    if not raw:
        raise ExtensionError("isi file extension kosong")
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    if len(raw) > (MAX_UPLOAD_BYTES * 4) // 3 + 4:
        raise ExtensionError("ukuran extension melebihi 30 MB")
    try:
        data = base64.b64decode(raw, validate=False)
    except (ValueError, TypeError):
        raise ExtensionError("isi file bukan base64 yang valid")
    if not data:
        raise ExtensionError("isi file extension kosong")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ExtensionError("ukuran extension melebihi 30 MB")
    return data


def _zip_payload(filename, data):
    """Strip the CRX2/CRX3 header; plain zips are returned unchanged."""
    if data[:4] == CRX_MAGIC:
        if len(data) < 12:
            raise ExtensionError("header CRX tidak lengkap")
        version = struct.unpack("<I", data[4:8])[0]
        if version == 2:
            if len(data) < 16:
                raise ExtensionError("header CRX2 tidak lengkap")
            public_key_len, signature_len = struct.unpack("<II", data[8:16])
            offset = 16 + public_key_len + signature_len
        elif version == 3:
            header_len = struct.unpack("<I", data[8:12])[0]
            offset = 12 + header_len
        else:
            raise ExtensionError(f"versi CRX {version} tidak didukung")
        if offset >= len(data):
            raise ExtensionError("header CRX tidak sesuai ukuran file")
        data = data[offset:]
    elif str(filename or "").lower().endswith(".crx"):
        raise ExtensionError("file .crx tidak memakai header Cr24")
    if data[:2] != b"PK":
        raise ExtensionError("file bukan zip atau crx yang valid")
    return data


def _manifest_member(names):
    if "manifest.json" in names:
        return "manifest.json"
    tops = {name.split("/", 1)[0] for name in names if "/" in name}
    if len(tops) == 1:
        wrapper = f"{next(iter(tops))}/manifest.json"
        if wrapper in names:
            return wrapper
    return None


def _read_manifest(archive, member):
    try:
        raw = archive.read(member)
    except KeyError:
        raise ExtensionError("manifest.json tidak bisa dibaca dari zip")
    except zipfile.BadZipFile:
        raise ExtensionError("zip rusak, manifest.json gagal dibaca")
    try:
        manifest = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise ExtensionError("manifest.json bukan JSON valid")
    if not isinstance(manifest, dict):
        raise ExtensionError("manifest.json harus berisi objek JSON")
    raw_title = str(manifest.get("name") or "").strip()
    if not raw_title:
        raise ExtensionError("manifest.json tidak memuat name")
    title = _resolve_manifest_name(archive, member, manifest)
    if not title:
        title = raw_title
    return title, str(manifest.get("version") or "").strip()


def _safe_parts(relative):
    parts = []
    for part in relative.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == ".." or part.endswith(":"):
            return None
        parts.append(part)
    return parts or None


def _extract(archive, member, target):
    prefix = member[: -len("manifest.json")]
    for info in archive.infolist():
        if not info.filename.startswith(prefix):
            continue
        relative = info.filename[len(prefix):]
        if not relative or relative.endswith("/"):
            continue
        parts = _safe_parts(relative)
        if parts is None:
            raise ExtensionError(f"entri zip tidak aman: {info.filename}")
        destination = target.joinpath(*parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, open(destination, "wb") as handle:
            shutil.copyfileobj(source, handle)


def upload(filename, content):
    """Unpack a .zip/.crx upload into EXTENSIONS_DIR and register it."""
    data = _decode(content)
    payload = _zip_payload(filename, data)
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile:
        raise ExtensionError("file bukan zip yang valid")
    ext_dir = _extensions_dir()
    with archive:
        member = _manifest_member(archive.namelist())
        if member is None:
            raise ExtensionError("manifest.json tidak ditemukan di akar zip")
        title, version = _read_manifest(archive, member)
        slug = _unique_slug(_slugify(title))
        ext_dir.mkdir(parents=True, exist_ok=True)
        staging = ext_dir / f".upload-{os.getpid()}-{secrets.token_hex(4)}"
        target = ext_dir / slug
        try:
            _extract(archive, member, staging)
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(staging), str(target))
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    with store.db() as conn:
        store._ensure_sync_schema(conn)
        ext_uuid = str(uuid.uuid4())
        origin_dev = store.get_device_id(conn)
        cur = conn.execute(
            """INSERT INTO extensions
               (slug, title, version, path, created_at, uuid, revision, deleted_at, origin_device, sync_dirty)
               VALUES (?,?,?,?,?,?,0,NULL,?,1)""",
            (slug, title, version, str(target), store.now(), ext_uuid, origin_dev),
        )
        conn.commit()
        extension_id = cur.lastrowid
    return {"id": extension_id, "slug": slug, "title": title, "version": version, "uuid": ext_uuid}


def _dir_size(folder):
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def list_extensions(page=1, limit=10, name=None):
    page = max(1, int(page or 1))
    limit = max(1, int(limit or 10))
    where, args = "", []
    if name:
        where = "WHERE title LIKE ?"
        args.append(f"%{name}%")
    ext_dir = _extensions_dir()
    with store.db() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS c FROM extensions {where}", args).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM extensions {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*args, limit, (page - 1) * limit],
        ).fetchall()
    data = []
    for row in rows:
        folder = Path(row["path"] or ext_dir / row["slug"])
        data.append(
            {
                "id": row["id"],
                "title": row["title"],
                "version": row["version"],
                "slug": row["slug"],
                "path": str(folder),
                "size": _dir_size(folder),
                "created_at": row["created_at"],
            }
        )
    return {"total": total, "data": data}


def _profiles_using(conn, extension_id):
    users = []
    rows = conn.execute("SELECT id, name, preference_config FROM profiles ORDER BY id").fetchall()
    for row in rows:
        config = store._loads(row["preference_config"])
        ids = config.get("extension_ids") if isinstance(config, dict) else None
        if not isinstance(ids, list):
            continue
        try:
            used = {int(value) for value in ids}
        except (TypeError, ValueError):
            continue
        if extension_id in used:
            users.append(row["name"] or f"id {row['id']}")
    return users


def _inside_extensions(folder):
    try:
        Path(folder).resolve().relative_to(_extensions_dir().resolve())
        return True
    except (OSError, ValueError):
        return False


def delete_extension(extension_id):
    try:
        extension_id = int(extension_id)
    except (TypeError, ValueError):
        raise ExtensionError("id extension tidak valid")
    ext_dir = _extensions_dir()
    with store.db() as conn:
        row = conn.execute("SELECT * FROM extensions WHERE id = ?", (extension_id,)).fetchone()
        if row is None:
            raise ExtensionError("Extension tidak ditemukan")
        users = _profiles_using(conn, extension_id)
        if users:
            raise ExtensionError(f"Extension masih dipakai profil {users[0]}")
        conn.execute("DELETE FROM extensions WHERE id = ?", (extension_id,))
        conn.commit()
    folder = Path(row["path"] or ext_dir / row["slug"])
    if not _inside_extensions(folder):
        folder = ext_dir / str(row["slug"] or "")
    shutil.rmtree(folder, ignore_errors=True)
    return True


def extension_paths(ids):
    """Resolve stored extension ids to unpacked folders that still exist."""
    wanted = []
    for value in ids or []:
        try:
            wanted.append(int(value))
        except (TypeError, ValueError):
            continue
    if not wanted:
        return []
    placeholders = ",".join("?" for _ in wanted)
    ext_dir = _extensions_dir()
    with store.db() as conn:
        rows = conn.execute(
            f"SELECT id, path, slug FROM extensions WHERE id IN ({placeholders}) ORDER BY id",
            wanted,
        ).fetchall()
    paths = []
    for row in rows:
        folder = Path(row["path"] or ext_dir / row["slug"])
        if folder.is_dir() and folder not in paths:
            paths.append(folder)
    return paths


def get_extension_archive(slug):
    """Kemas direktori ekstensi menjadi bytes arsip .zip dan kembalikan (bytes, sha256)."""
    clean_slug = str(slug or "").strip()
    if not clean_slug:
        raise ExtensionError("slug extension wajib diisi")
    ext_dir = _extensions_dir()
    candidates = [ext_dir / clean_slug, EXTENSIONS_DIR / clean_slug]
    try:
        with store.db() as conn:
            row = conn.execute(
                "SELECT path FROM extensions WHERE slug = ?", (clean_slug,)
            ).fetchone()
            if row and row["path"]:
                candidates.insert(0, Path(row["path"]))
    except Exception:
        pass

    folder = None
    for cand in candidates:
        if cand.is_dir():
            folder = cand
            break
    if folder is None:
        raise ExtensionError(f"ekstensi {clean_slug} tidak ditemukan")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(folder):
            dirs.sort()
            files.sort()
            for file_name in files:
                full_path = Path(root) / file_name
                rel_path = full_path.relative_to(folder).as_posix()
                zf.write(full_path, arcname=rel_path)
    archive_bytes = buf.getvalue()
    sha256_hex = hashlib.sha256(archive_bytes).hexdigest()
    return archive_bytes, sha256_hex


def install_extension_archive(slug, archive_bytes, title=None, version=None, conn=None):
    """Ekstrak arsip zip ke EXTENSIONS_DIR / slug dan pastikan tercatat di tabel extensions."""
    clean_slug = str(slug or "").strip()
    if isinstance(archive_bytes, str):
        raw_bytes = _decode(archive_bytes)
    elif isinstance(archive_bytes, (bytes, bytearray)):
        raw_bytes = bytes(archive_bytes)
    else:
        raise ExtensionError("isi arsip extension tidak valid")

    payload = _zip_payload(f"{clean_slug or 'extension'}.zip", raw_bytes)
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as error:
        raise ExtensionError("file bukan zip yang valid") from error

    ext_dir = _extensions_dir(conn)
    with archive:
        member = _manifest_member(archive.namelist())
        if member is not None:
            manifest_title, manifest_version = _read_manifest(archive, member)
            if not title:
                title = manifest_title
            if not version:
                version = manifest_version
        else:
            member = "manifest.json"
        if not title:
            title = clean_slug or "Extension"
        if not version:
            version = "1.0"
        if not clean_slug:
            clean_slug = _slugify(title)

        ext_dir.mkdir(parents=True, exist_ok=True)
        staging = ext_dir / f".install-{os.getpid()}-{secrets.token_hex(4)}"
        target = ext_dir / clean_slug
        try:
            _extract(archive, member, staging)
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(staging), str(target))
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _register(db_conn):
        store._ensure_sync_schema(db_conn)
        existing = db_conn.execute(
            "SELECT * FROM extensions WHERE slug = ?", (clean_slug,)
        ).fetchone()
        if existing:
            db_conn.execute(
                """UPDATE extensions
                   SET title = COALESCE(?, title),
                       version = COALESCE(?, version),
                       path = ?
                   WHERE id = ?""",
                (title, version, str(target), existing["id"]),
            )
            ext_id = existing["id"]
        else:
            ext_uuid = str(uuid.uuid4())
            origin_dev = store.get_device_id(db_conn)
            cur = db_conn.execute(
                """INSERT INTO extensions
                   (slug, title, version, path, created_at, uuid, revision, deleted_at, origin_device, sync_dirty)
                   VALUES (?,?,?,?,?,?,1,NULL,?,0)""",
                (clean_slug, title, version, str(target), store.now(), ext_uuid, origin_dev),
            )
            ext_id = cur.lastrowid
        return ext_id

    if conn is not None:
        ext_id = _register(conn)
        conn.commit()
    else:
        with store.db() as local_conn:
            ext_id = _register(local_conn)
            local_conn.commit()

    return {
        "id": ext_id,
        "slug": clean_slug,
        "title": title,
        "version": version,
        "path": str(target),
    }

