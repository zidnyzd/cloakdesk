"""Read and write Chromium cookie databases for a profile."""
import hashlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

from . import launcher

# Chromium stores timestamps as microseconds since 1601-01-01.
_EPOCH_DELTA = 11644473600
_IV = b" " * 16
_SAMESITE_TO_DB = {"unspecified": -1, "none": 0, "lax": 1, "strict": 2}
_SAMESITE_FROM_DB = {value: key for key, value in _SAMESITE_TO_DB.items()}

_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16"
)
_INV_SBOX = bytes(_SBOX.index(value) for value in range(256))

_G2 = bytes(((value << 1) ^ (0x1B if value & 0x80 else 0)) & 0xFF for value in range(256))


def _gmul(a, b):
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        high = a & 0x80
        a = (a << 1) & 0xFF
        if high:
            a ^= 0x1B
        b >>= 1
    return result


_G9 = bytes(_gmul(value, 9) for value in range(256))
_G11 = bytes(_gmul(value, 11) for value in range(256))
_G13 = bytes(_gmul(value, 13) for value in range(256))
_G14 = bytes(_gmul(value, 14) for value in range(256))


def _expand_key(key):
    words = [list(key[index:index + 4]) for index in range(0, 16, 4)]
    rcon = 1
    for index in range(4, 44):
        temp = words[index - 1][:]
        if index % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[byte] for byte in temp]
            temp[0] ^= rcon
            rcon = _G2[rcon]
        words.append([words[index - 4][slot] ^ temp[slot] for slot in range(4)])
    return [sum(words[round * 4:round * 4 + 4], []) for round in range(11)]


def _shift_rows(state):
    return [state[4 * ((column + row) % 4) + row] for column in range(4) for row in range(4)]


def _inv_shift_rows(state):
    return [state[4 * ((column - row) % 4) + row] for column in range(4) for row in range(4)]


def _mix_columns(state):
    for column in range(4):
        offset = column * 4
        a0, a1, a2, a3 = state[offset:offset + 4]
        state[offset] = _G2[a0] ^ (_G2[a1] ^ a1) ^ a2 ^ a3
        state[offset + 1] = a0 ^ _G2[a1] ^ (_G2[a2] ^ a2) ^ a3
        state[offset + 2] = a0 ^ a1 ^ _G2[a2] ^ (_G2[a3] ^ a3)
        state[offset + 3] = (_G2[a0] ^ a0) ^ a1 ^ a2 ^ _G2[a3]


def _inv_mix_columns(state):
    for column in range(4):
        offset = column * 4
        a0, a1, a2, a3 = state[offset:offset + 4]
        state[offset] = _G14[a0] ^ _G11[a1] ^ _G13[a2] ^ _G9[a3]
        state[offset + 1] = _G9[a0] ^ _G14[a1] ^ _G11[a2] ^ _G13[a3]
        state[offset + 2] = _G13[a0] ^ _G9[a1] ^ _G14[a2] ^ _G11[a3]
        state[offset + 3] = _G11[a0] ^ _G13[a1] ^ _G9[a2] ^ _G14[a3]


def _add_round_key(state, key):
    for index in range(16):
        state[index] ^= key[index]


def _encrypt_block(block, round_keys):
    state = list(block)
    _add_round_key(state, round_keys[0])
    for round in range(1, 10):
        state = [_SBOX[byte] for byte in state]
        state = _shift_rows(state)
        _mix_columns(state)
        _add_round_key(state, round_keys[round])
    state = [_SBOX[byte] for byte in state]
    state = _shift_rows(state)
    _add_round_key(state, round_keys[10])
    return bytes(state)


def _decrypt_block(block, round_keys):
    state = list(block)
    _add_round_key(state, round_keys[10])
    for round in range(9, 0, -1):
        state = _inv_shift_rows(state)
        state = [_INV_SBOX[byte] for byte in state]
        _add_round_key(state, round_keys[round])
        _inv_mix_columns(state)
    state = _inv_shift_rows(state)
    state = [_INV_SBOX[byte] for byte in state]
    _add_round_key(state, round_keys[0])
    return bytes(state)


def _cbc_encrypt(data):
    key = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, 16)
    round_keys = _expand_key(key)
    padding = 16 - len(data) % 16
    data += bytes([padding]) * padding
    previous = _IV
    output = bytearray()
    for index in range(0, len(data), 16):
        block = bytes(left ^ right for left, right in zip(data[index:index + 16], previous))
        previous = _encrypt_block(block, round_keys)
        output.extend(previous)
    return bytes(output)


def _cbc_decrypt(data):
    key = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, 16)
    round_keys = _expand_key(key)
    previous = _IV
    output = bytearray()
    for index in range(0, len(data), 16):
        block = data[index:index + 16]
        plain = _decrypt_block(block, round_keys)
        output.extend(left ^ right for left, right in zip(plain, previous))
        previous = block
    padding = output[-1]
    if padding < 1 or padding > 16 or output[-padding:] != bytes([padding]) * padding:
        raise ValueError("invalid padding")
    return bytes(output[:-padding])


class CookieError(Exception):
    pass


def _profiles_dir():
    from . import paths, store

    if launcher.PROFILES_DIR != paths.DATA_DIR / "profiles":
        return launcher.PROFILES_DIR
    if Path(store.DB_PATH) != store._INITIAL_DB_PATH:
        return Path(store.DB_PATH).parent / "profiles"
    env_home = os.environ.get("ANTIDETECT_HOME")
    if env_home:
        return Path(env_home) / "profiles"
    return launcher.PROFILES_DIR


def _database_path(profile_id):
    base = _profiles_dir() / str(profile_id) / "data" / "Default"
    network = base / "Network" / "Cookies"
    legacy = base / "Cookies"
    if network.exists():
        return network
    return legacy


def _require_closed(profile_id):
    if launcher.is_open(profile_id):
        raise CookieError("tutup profil dulu")


def _connect(path, create=False):
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path, timeout=5)


def _create_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta(
            key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY,
            value LONGVARCHAR
        );
        CREATE TABLE IF NOT EXISTS cookies(
            creation_utc INTEGER NOT NULL,
            host_key TEXT NOT NULL,
            top_frame_site_key TEXT NOT NULL,
            name TEXT NOT NULL,
            value TEXT NOT NULL,
            encrypted_value BLOB NOT NULL,
            path TEXT NOT NULL,
            expires_utc INTEGER NOT NULL,
            is_secure INTEGER NOT NULL,
            is_httponly INTEGER NOT NULL,
            last_access_utc INTEGER NOT NULL,
            has_expires INTEGER NOT NULL,
            is_persistent INTEGER NOT NULL,
            priority INTEGER NOT NULL,
            samesite INTEGER NOT NULL,
            source_scheme INTEGER NOT NULL,
            source_port INTEGER NOT NULL,
            last_update_utc INTEGER NOT NULL,
            source_type INTEGER NOT NULL,
            has_cross_site_ancestor INTEGER NOT NULL
        );
        """
    )
    columns = _columns(conn)
    index_columns = (
        "host_key", "top_frame_site_key", "has_cross_site_ancestor",
        "name", "path", "source_scheme", "source_port",
    )
    if all(column in columns for column in index_columns):
        try:
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS cookies_unique_index ON cookies(
                       host_key, top_frame_site_key, has_cross_site_ancestor, name,
                       path, source_scheme, source_port
                   )"""
            )
        except sqlite3.IntegrityError:
            # Basis data lama bisa sudah punya baris kembar. Index ditunda,
            # dedupe_cookies yang membersihkannya lalu membuat ulang index.
            pass
    if not conn.execute("SELECT 1 FROM meta WHERE key = 'version'").fetchone():
        conn.execute("INSERT INTO meta(key, value) VALUES('version', '24')")
        conn.execute("INSERT INTO meta(key, value) VALUES('last_compatible_version', '24')")


def _columns(conn):
    return {row[1] for row in conn.execute("PRAGMA table_info(cookies)")}


def _session_expression(columns):
    if "is_session" in columns:
        return "is_session"
    return "CASE WHEN is_persistent = 0 OR has_expires = 0 THEN 1 ELSE 0 END"


def _decode_value(host_key, value, encrypted):
    if value:
        return value
    if not encrypted:
        return ""
    try:
        raw = bytes(encrypted)
        if raw[:3] not in (b"v10", b"v11"):
            return ""
        plain = _cbc_decrypt(raw[3:])
        if len(plain) >= 32:
            plain = plain[32:]
        return plain.decode("utf-8", "replace")
    except Exception:
        return ""


def get_cookies(profile_id):
    profile_id = int(profile_id)
    _require_closed(profile_id)
    path = _database_path(profile_id)
    if not path.exists():
        return "[]"

    try:
        conn = _connect(path)
        try:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cookies'"
            ).fetchone():
                return "[]"
            columns = _columns(conn)
            rows = conn.execute(
                f"""SELECT host_key, name, path, expires_utc, is_secure, is_httponly,
                           encrypted_value, value, samesite, {_session_expression(columns)}
                    FROM cookies
                    ORDER BY host_key, name, path"""
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as error:
        raise CookieError(f"gagal membaca cookie: {error}")

    cookies = []
    for host, name, cookie_path, expires_utc, secure, http_only, encrypted, value, samesite, session in rows:
        session = bool(session)
        cookies.append({
            "domain": host,
            "name": name,
            "path": cookie_path,
            "value": _decode_value(host, value, encrypted),
            "expires": 0 if session else int(expires_utc // 1_000_000 - _EPOCH_DELTA),
            "httpOnly": bool(http_only),
            "secure": bool(secure),
            "sameSite": _SAMESITE_FROM_DB.get(int(samesite), "unspecified"),
            "session": session,
        })
    return json.dumps(cookies)


def _cookie_list(payload):
    if payload is None:
        return []
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except ValueError:
            raise CookieError("format cookie bukan JSON yang valid")
    if isinstance(payload, dict):
        if "cookies" not in payload:
            raise CookieError("cookie harus berupa list JSON atau objek dengan key cookies")
        payload = payload["cookies"]
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise CookieError("cookie harus berupa list JSON")
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise CookieError(f"cookie ke-{index} bukan objek")
        if not str(item.get("domain") or "").strip():
            raise CookieError(f"cookie ke-{index} tidak punya domain")
        if not str(item.get("name") or "").strip():
            raise CookieError(f"cookie ke-{index} tidak punya name")
    return payload


def _same_site_value(value):
    if isinstance(value, str):
        return _SAMESITE_TO_DB.get(value.strip().lower(), -1)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return -1
    return number if number in _SAMESITE_FROM_DB else -1


def _int_value(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _insert_cookie(conn, columns, cookie):
    now = int((time.time() + _EPOCH_DELTA) * 1_000_000)
    domain = str(cookie.get("domain") or "").strip()
    name = str(cookie.get("name") or "").strip()
    path = str(cookie.get("path") or "/")
    value = "" if cookie.get("value") is None else str(cookie.get("value"))
    expires = _int_value(cookie.get("expires"))
    session = bool(cookie.get("session")) or expires <= 0
    expires_utc = 0 if session else (expires + _EPOCH_DELTA) * 1_000_000
    secure = 1 if cookie.get("secure") else 0

    scheme_raw = cookie.get("source_scheme")
    port_raw = cookie.get("source_port")
    scheme_provided = scheme_raw is not None
    port_provided = port_raw is not None
    source_scheme = _int_value(scheme_raw, 2) if scheme_provided else None
    source_port = _int_value(port_raw, 443) if port_provided else None
    if source_scheme not in (1, 2):
        source_scheme = None
    if source_port not in (80, 443):
        source_port = None
    if source_scheme is None and source_port is None:
        source_scheme, source_port = 2, 443
    elif source_scheme is None:
        source_scheme = 1 if source_port == 80 else 2
    elif source_port is None:
        source_port = 80 if source_scheme == 1 else 443

    top_frame_site_key = cookie.get("top_frame_site_key")
    if top_frame_site_key is None:
        top_frame_site_key = ""
    else:
        top_frame_site_key = str(top_frame_site_key)
    has_cross_site_ancestor = _int_value(cookie.get("has_cross_site_ancestor"), 0)

    values = {
        "creation_utc": now,
        "host_key": domain,
        "top_frame_site_key": top_frame_site_key,
        "name": name,
        "value": "",
        "encrypted_value": b"v10" + _cbc_encrypt(
            hashlib.sha256(domain.encode()).digest() + value.encode("utf-8")
        ),
        "path": path,
        "expires_utc": expires_utc,
        "is_secure": secure,
        "is_httponly": 1 if cookie.get("httpOnly") else 0,
        "last_access_utc": now,
        "has_expires": 0 if session else 1,
        "is_persistent": 0 if session else 1,
        "priority": 1,
        "samesite": _same_site_value(cookie.get("sameSite")),
        "source_scheme": source_scheme,
        "source_port": source_port,
        "last_update_utc": now,
        "source_type": 0,
        "has_cross_site_ancestor": has_cross_site_ancestor,
        "is_session": 1 if session else 0,
    }

    key_columns = (
        "host_key", "top_frame_site_key", "has_cross_site_ancestor",
        "name", "path", "source_scheme", "source_port",
    )
    key_values = {
        "host_key": domain,
        "top_frame_site_key": top_frame_site_key,
        "has_cross_site_ancestor": has_cross_site_ancestor,
        "name": name,
        "path": path,
        "source_scheme": source_scheme,
        "source_port": source_port,
    }
    if all(column in columns for column in key_columns):
        where = " AND ".join(f"{column} = ?" for column in key_columns)
        params = [key_values[column] for column in key_columns]
    else:
        where = "host_key = ? AND path = ? AND name = ?"
        params = [domain, path, name]
    conn.execute(f"DELETE FROM cookies WHERE {where}", params)

    names = [column for column in values if column in columns]
    placeholders = ", ".join("?" for _ in names)
    conn.execute(
        f"INSERT INTO cookies ({', '.join(names)}) VALUES ({placeholders})",
        [values[column] for column in names],
    )


def dedupe_cookies(profile_id):
    profile_id = int(profile_id)
    _require_closed(profile_id)
    path = _database_path(profile_id)
    if not path.exists():
        return 0
    backup = path.with_name(path.name + ".bak")
    if path.exists():
        try:
            shutil.copy2(path, backup)
        except OSError as error:
            raise CookieError(f"gagal membuat cadangan cookie: {error}")
    deleted = 0
    try:
        conn = _connect(path)
        try:
            columns = _columns(conn)
            key_columns = (
                "host_key", "top_frame_site_key", "has_cross_site_ancestor",
                "name", "path", "source_scheme", "source_port",
            )
            if not all(column in columns for column in key_columns):
                conn.close()
                return 0
            rows = conn.execute(
                f"SELECT rowid, {', '.join(key_columns)}, last_update_utc, creation_utc "
                f"FROM cookies"
            ).fetchall()
            groups = {}
            for row in rows:
                key = tuple(row[1:8])
                groups.setdefault(key, []).append(row)
            for members in groups.values():
                if len(members) <= 1:
                    continue
                members.sort(key=lambda row: (row[8], row[9]))
                for member in members[:-1]:
                    conn.execute("DELETE FROM cookies WHERE rowid = ?", (member[0],))
                    deleted += 1
            conn.commit()
            try:
                conn.execute(
                    """CREATE UNIQUE INDEX IF NOT EXISTS cookies_unique_index ON cookies(
                           host_key, top_frame_site_key, has_cross_site_ancestor, name,
                           path, source_scheme, source_port
                       )"""
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
        finally:
            conn.close()
    except sqlite3.DatabaseError as error:
        raise CookieError(f"gagal mendedup cookie: {error}")
    return deleted


def update_cookies(profile_id, cookie, preserve_sessions=True):
    profile_id = int(profile_id)
    _require_closed(profile_id)
    items = _cookie_list(cookie)
    path = _database_path(profile_id)

    if not path.exists() and not items:
        return True

    backup = path.with_name(path.name + ".bak")
    if path.exists():
        try:
            shutil.copy2(path, backup)
        except OSError as error:
            raise CookieError(f"gagal membuat backup cookie: {error}")

    try:
        conn = _connect(path, create=True)
        try:
            _create_schema(conn)
            columns = _columns(conn)
            if not items:
                if cookie == [] and not preserve_sessions:
                    conn.execute("DELETE FROM cookies")
                else:
                    conn.rollback()
            else:
                for item in items:
                    _insert_cookie(conn, columns, item)
            conn.commit()
        finally:
            conn.close()
    except sqlite3.DatabaseError as error:
        raise CookieError(f"gagal menulis cookie: {error}")
    dedupe_cookies(profile_id)
    return True


def clear_saved_passwords(profile_id):
    profile_id = int(profile_id)
    _require_closed(profile_id)
    folder = _database_path(profile_id).parent
    folders = [folder]
    if folder.name == "Network":
        folders.append(folder.parent)
    for directory in folders:
        for name in (
            "Login Data",
            "Login Data-journal",
            "Login Data For Account",
            "Login Data For Account-journal",
        ):
            try:
                (directory / name).unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                raise CookieError(f"gagal menghapus {name}: {error}")
    return True


def cookie_digest(cookies_list):
    if isinstance(cookies_list, int):
        items = _cookie_list(get_cookies(cookies_list))
    elif isinstance(cookies_list, str) and cookies_list.strip().isdigit():
        items = _cookie_list(get_cookies(int(cookies_list.strip())))
    else:
        items = _cookie_list(cookies_list)

    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "").strip()
        name = str(item.get("name") or "").strip()
        path = str(item.get("path") or "/")
        value = "" if item.get("value") is None else str(item.get("value"))
        expires = _int_value(item.get("expires"))
        session = bool(item.get("session")) or expires <= 0
        normalized_expires = 0 if session else expires
        same_site = _SAMESITE_FROM_DB.get(
            _same_site_value(item.get("sameSite")), "unspecified"
        )
        normalized.append(
            {
                "domain": domain,
                "name": name,
                "path": path,
                "value": value,
                "expires": normalized_expires,
                "httpOnly": bool(item.get("httpOnly")),
                "secure": bool(item.get("secure")),
                "sameSite": same_site,
                "session": session,
            }
        )

    normalized.sort(
        key=lambda c: (
            c["domain"],
            c["path"],
            c["name"],
            c["value"],
            c["expires"],
        )
    )
    canonical_bytes = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


def verify_cookie_db(profile_id):
    try:
        path = _database_path(int(profile_id))
    except (TypeError, ValueError):
        return False
    if not path.is_file():
        return False
    try:
        if path.stat().st_size == 0:
            return False
        conn = sqlite3.connect(str(path), timeout=5)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            return bool(row and str(row[0] or "").strip().lower() == "ok")
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return False

