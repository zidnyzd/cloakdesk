"""Agen sinkronisasi klien untuk antidetect (Push-Then-Pull)."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request
import uuid

from . import bookmarks, cookies, extensions, passwords, store, sync_crypto, sync_server

_orig_server_authenticate = getattr(sync_server, "_authenticate", None)
if callable(_orig_server_authenticate) and not getattr(
    _orig_server_authenticate, "_autocommit_wrapped", False
):
    def _wrapped_server_authenticate(conn, auth_header, ip_address=None):
        row = _orig_server_authenticate(conn, auth_header, ip_address=ip_address)
        if getattr(conn, "in_transaction", False):
            conn.commit()
        return row

    _wrapped_server_authenticate._autocommit_wrapped = True
    sync_server._authenticate = _wrapped_server_authenticate

ENTITY_TABLES = {
    "profile": "profiles",
    "group": "groups",
    "tag": "tags",
    "proxy": "proxies",
    "template": "templates",
    "extension": "extensions",
}


class SyncAgentError(Exception):
    def __init__(self, message, code=1000, http_status=None, data=None, is_network_error=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.data = data
        self.is_network_error = is_network_error


class SyncLeaseError(SyncAgentError):
    def __init__(self, code_or_message, message=None, data=None, http_status=409, code=2004):
        if isinstance(code_or_message, int):
            actual_code = code_or_message
            actual_msg = str(message or "Profil sedang dibuka di perangkat lain")
            actual_data = data
        else:
            actual_msg = str(code_or_message)
            if isinstance(message, dict) and data is None:
                actual_data = message
                actual_code = code
            elif isinstance(message, int):
                actual_code = message
                actual_data = data
            else:
                actual_code = code
                actual_data = data
        super().__init__(actual_msg, code=actual_code, http_status=http_status, data=actual_data)
        self.data = actual_data if actual_data is not None else {}


def _credentials_path(conn=None, db_path=None):
    if conn is not None:
        return store._conn_db_path(conn).parent / "sync_credentials.json"
    return store._resolve_db_path(db_path).parent / "sync_credentials.json"


def load_credentials(conn=None, db_path=None):
    path = _credentials_path(conn=conn, db_path=db_path)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_credentials(creds, conn=None, db_path=None):
    path = _credentials_path(conn=conn, db_path=db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(creds, handle, ensure_ascii=False, indent=2)
    os.replace(str(tmp_path), str(path))
    os.chmod(str(path), 0o600)
    return path


def _post_json(url, payload, token=None, timeout=15):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "CloakDesk/0.2.0 (Linux; x86_64)",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            status_code = resp.status
    except urllib.error.HTTPError as error:
        raw_err = ""
        try:
            raw_err = error.read().decode("utf-8", errors="replace")
            parsed_err = json.loads(raw_err)
        except Exception:
            parsed_err = None
        if isinstance(parsed_err, dict) and isinstance(parsed_err.get("error"), dict):
            err_info = parsed_err["error"]
            raise SyncAgentError(
                err_info.get("message") or f"HTTP {error.code}",
                code=int(err_info.get("code") or error.code),
                http_status=error.code,
                data=parsed_err.get("data"),
            ) from error
        raise SyncAgentError(
            raw_err or f"HTTP {error.code}",
            code=error.code,
            http_status=error.code,
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SyncAgentError(
            f"Gagal menghubungi server sinkronisasi: {error}",
            code=1000,
            is_network_error=True,
        ) from error

    if not isinstance(parsed, dict):
        raise SyncAgentError("Respons server bukan objek JSON", code=2003, http_status=status_code)
    err_obj = parsed.get("error") or {}
    err_code = int(err_obj.get("code") or 0)
    if err_code != 0:
        raise SyncAgentError(
            err_obj.get("message") or "Operasi sinkronisasi gagal",
            code=err_code,
            http_status=status_code,
            data=parsed.get("data"),
        )
    return parsed.get("data") or {}


def _resolve_endpoint(conn, server_url=None, token=None):
    creds = load_credentials(conn=conn)
    resolved_url = (
        server_url
        or creds.get("server_url")
        or store.get_sync_state(conn, "server_url")
        or ""
    ).strip().rstrip("/")
    resolved_token = (
        token
        or creds.get("device_token")
        or store.get_sync_state(conn, "device_token")
        or ""
    ).strip()
    if not resolved_url:
        raise SyncAgentError("URL server sinkronisasi belum dikonfigurasi")
    if not resolved_token:
        raise SyncAgentError("Token perangkat sinkronisasi belum dikonfigurasi")
    return resolved_url, resolved_token


def register_device(server_url, enrollment_token, device_name, passphrase, db_path=None):
    clean_url = str(server_url or "").strip().rstrip("/")
    if not clean_url:
        raise SyncAgentError("server_url wajib diisi", code=2003)
    if not enrollment_token:
        raise SyncAgentError("enrollment_token wajib diisi", code=2003)
    if not passphrase:
        raise SyncAgentError("passphrase wajib diisi", code=2003)

    store.init(db_path=db_path)
    with store.db(db_path=db_path) as conn:
        device_id = store.get_device_id(conn)
        clean_name = str(device_name or socket.gethostname() or "Device").strip()
        data = _post_json(
            f"{clean_url}/v1/sync/device/register",
            {
                "enrollment_token": str(enrollment_token).strip(),
                "device_id": device_id,
                "device_name": clean_name,
                "platform": sys.platform,
                "app_version": "0.1.0",
                "passphrase": str(passphrase),
            },
        )
        registered_device_id = data.get("device_id") or device_id
        device_token = data["device_token"]
        salt_hex = data["kdf_salt_hex"]
        key_check = data.get("key_check_value") or ""

        kek = sync_crypto.derive_kek(passphrase, salt_hex)
        if key_check and not sync_crypto.verify_passphrase(passphrase, salt_hex, key_check):
            raise SyncAgentError("Frasa sandi sinkronisasi tidak cocok dengan server", code=2001)
        if not key_check:
            key_check = sync_crypto.compute_key_check(kek)
        dedupe_key = sync_crypto.derive_dedupe_key(kek)

        creds = {
            "server_url": clean_url,
            "device_id": registered_device_id,
            "device_name": clean_name,
            "device_token": device_token,
            "kdf_salt_hex": salt_hex,
            "key_check_value": key_check,
            "kek_hex": kek.hex(),
            "dedupe_key_hex": dedupe_key.hex(),
        }
        creds_path = save_credentials(creds, conn=conn, db_path=db_path)

        store.set_sync_state(conn, "device_id", registered_device_id)
        store.set_sync_state(conn, "device_name", clean_name)
        store.set_sync_state(conn, "server_url", clean_url)
        store.set_sync_state(conn, "device_token", device_token)
        store.set_sync_state(conn, "kdf_salt_hex", salt_hex)
        store.set_sync_state(conn, "key_check_value", key_check)
        if store.get_sync_state(conn, "last_pulled_seq") is None:
            store.set_sync_state(conn, "last_pulled_seq", "0")
        conn.commit()

    return {
        "device_id": registered_device_id,
        "device_name": clean_name,
        "device_token": device_token,
        "server_url": clean_url,
        "kdf_salt_hex": salt_hex,
        "key_check_value": key_check,
        "current_seq": int(data.get("current_seq") or 0),
        "credentials_path": str(creds_path),
    }


def _prepare_outbox_payload(conn, entity_type, raw_payload, kek, dedupe_key):
    payload = store._loads(raw_payload) if isinstance(raw_payload, str) else dict(raw_payload or {})
    if entity_type == "proxy":
        if dedupe_key and not payload.get("proxy_fingerprint"):
            fp_src = dict(payload)
            if "proxy_password" not in fp_src and payload.get("uuid"):
                row = conn.execute(
                    "SELECT proxy_password FROM proxies WHERE uuid = ?",
                    (payload["uuid"],),
                ).fetchone()
                if row and row["proxy_password"] is not None:
                    fp_src["proxy_password"] = row["proxy_password"]
            payload["proxy_fingerprint"] = sync_crypto.proxy_fingerprint(fp_src, dedupe_key)
        if kek:
            if "proxy_password" not in payload and "proxy_password_enc" not in payload and payload.get("uuid"):
                row = conn.execute(
                    "SELECT proxy_password FROM proxies WHERE uuid = ?",
                    (payload["uuid"],),
                ).fetchone()
                if row and row["proxy_password"] is not None:
                    payload["proxy_password"] = row["proxy_password"]
            payload = sync_crypto.encrypt_proxy_config(payload, kek)
    elif entity_type == "profile":
        p_cfg = payload.get("proxy_config")
        if isinstance(p_cfg, dict):
            raw_cfg = dict(p_cfg)
            if "proxy_password" not in raw_cfg and "proxy_password_enc" not in raw_cfg and payload.get("uuid"):
                row = conn.execute(
                    "SELECT proxy_config FROM profiles WHERE uuid = ?",
                    (payload["uuid"],),
                ).fetchone()
                if row:
                    stored_cfg = store._loads(row["proxy_config"])
                    if "proxy_password" in stored_cfg:
                        raw_cfg["proxy_password"] = stored_cfg["proxy_password"]
            if dedupe_key and not payload.get("proxy_fingerprint"):
                payload["proxy_fingerprint"] = sync_crypto.proxy_fingerprint(raw_cfg, dedupe_key)
            if kek:
                payload["proxy_config"] = sync_crypto.encrypt_proxy_config(raw_cfg, kek)
    elif entity_type == "extension":
        slug = payload.get("slug")
        if slug and not payload.get("archive_blob_id") and not payload.get("deleted_at"):
            try:
                up_res = upload_extension_blob(
                    conn,
                    slug,
                    extension_uuid=payload.get("uuid"),
                    kek=kek,
                )
                if isinstance(up_res, dict) and up_res.get("blob_id"):
                    payload["archive_blob_id"] = up_res["blob_id"]
                    if up_res.get("sha256_plain"):
                        payload["archive_sha256"] = up_res["sha256_plain"]
            except Exception:
                pass
    return payload


def _parse_timestamp_like(value):
    """Parsing stempel waktu ke detik epoch, atau None jika tidak bisa diparsing."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        local_tz = datetime.now().astimezone().tzinfo
        try:
            dt = dt.replace(tzinfo=local_tz)
        except Exception:
            return None
    try:
        return dt.timestamp()
    except Exception:
        return None


def _is_timestamp_key(key):
    return key in ("created_at", "updated_at", "deleted_at") or key.endswith("_at")


def _values_equal_for_merge(key, left, right):
    if left == right:
        return True
    if _is_timestamp_key(key):
        left_ts = _parse_timestamp_like(left)
        right_ts = _parse_timestamp_like(right)
        if left_ts is not None and right_ts is not None:
            return abs(left_ts - right_ts) <= 5
    return False


def _same_after_mechanical_merge(a, b, ignore_keys):
    """Membandingkan dua payload tanpa kunci yang memang diisi server.

    Gabungan mekanis muncul karena server menambahkan catatan blob
    (archive_blob_id, archive_sha256) dan perbaruan waktu (updated_at) yang
    klien memang tidak mengirim atau tidak menyetujui. Stempel waktu (_at)
    dibandingkan secara makna dengan toleransi 5 detik karena server menstempel
    ulang; format lokal naif diartikan sebagai zona waktu sistem. Kalau seluruh
    kunci lain identik, gabungan itu mekanis, bukan pertentangan isi.
    """
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    for key in set(a) | set(b):
        if key in ignore_keys:
            continue
        if not _values_equal_for_merge(key, a.get(key), b.get(key)):
            return False
    return True


def _update_local_revision(conn, entity_type, entity_uuid, new_revision):
    table = ENTITY_TABLES.get(entity_type)
    if not table:
        return
    remaining = conn.execute(
        "SELECT 1 FROM sync_outbox WHERE entity_type = ? AND entity_uuid = ? LIMIT 1",
        (entity_type, entity_uuid),
    ).fetchone()
    dirty = 1 if remaining else 0
    conn.execute(
        f"UPDATE {table} SET revision = ?, sync_dirty = ? WHERE uuid = ?",
        (int(new_revision), dirty, entity_uuid),
    )


def push_outbox(conn, server_url=None, token=None, batch_size=50):
    store._ensure_sync_schema(conn)
    store.enqueue_dirty_entities(conn)
    resolved_url, resolved_token = _resolve_endpoint(conn, server_url, token)
    kek, dedupe_key = store._load_local_crypto(conn)

    batch_size = max(1, min(int(batch_size or 50), 100))
    total_pushed = 0
    total_applied = 0
    all_results = []

    for _ in range(100):
        rows = conn.execute(
            """SELECT * FROM sync_outbox
               ORDER BY CASE entity_type
                   WHEN 'group' THEN 1
                   WHEN 'tag' THEN 2
                   WHEN 'proxy' THEN 3
                   WHEN 'template' THEN 4
                   WHEN 'extension' THEN 5
                   WHEN 'profile' THEN 6
                   ELSE 7
               END ASC, outbox_id ASC
               LIMIT ?""",
            (batch_size,),
        ).fetchall()
        if not rows:
            break

        mutations = []
        by_idem = {}
        for row in rows:
            by_idem[row["idempotency_key"]] = row
            payload = _prepare_outbox_payload(
                conn, row["entity_type"], row["payload"], kek, dedupe_key
            )
            mutations.append(
                {
                    "idempotency_key": row["idempotency_key"],
                    "entity_type": row["entity_type"],
                    "entity_uuid": row["entity_uuid"],
                    "operation": row["operation"],
                    "base_revision": int(row["base_revision"] or 0),
                    "client_updated_at": payload.get("updated_at") or row["created_at"],
                    "payload": payload,
                }
            )

        try:
            data = _post_json(
                f"{resolved_url}/v1/sync/push",
                {"mutations": mutations},
                token=resolved_token,
            )
        except SyncAgentError as error:
            now_epoch = int(time.time())
            for row in rows:
                attempts = int(row["attempts"] or 0) + 1
                retry_at = now_epoch + min(300, 2 ** min(attempts, 8))
                conn.execute(
                    """UPDATE sync_outbox
                       SET attempts = ?, next_retry_at = ?, last_error = ?
                       WHERE outbox_id = ?""",
                    (attempts, retry_at, str(error), row["outbox_id"]),
                )
            store.set_sync_state(conn, "last_sync_error", str(error))
            conn.commit()
            raise

        results = data.get("results") or []
        total_pushed += len(rows)

        for res in results:
            all_results.append(res)
            idem_key = res.get("idempotency_key")
            status = res.get("status")
            new_rev = int(res.get("new_revision") or 1)
            outbox_row = by_idem.get(idem_key)
            if not outbox_row:
                continue

            entity_type = outbox_row["entity_type"]
            entity_uuid = outbox_row["entity_uuid"]

            if status in ("applied", "merged", "conflict_purged"):
                conn.execute(
                    "DELETE FROM sync_outbox WHERE idempotency_key = ?", (idem_key,)
                )
                _update_local_revision(conn, entity_type, entity_uuid, new_rev)
                total_applied += 1
                if status != "applied":
                    conflict_id = res.get("conflict_id")
                    merged_payload = res.get("merged_payload")
                    # Gabungan mekanis: server hanya menambahkan catatan blob
                    # dan perbaruan waktu yang memang tidak dikirim klien.
                    # Kalau sisanya identik, jangan catat sebagai konflik.
                    is_mechanical_merge = (
                        status == "merged"
                        and merged_payload is not None
                        and _same_after_mechanical_merge(
                            store._loads(outbox_row["payload"]),
                            merged_payload,
                            {"archive_blob_id", "archive_sha256"},
                        )
                    )
                    if conflict_id and not is_mechanical_merge:
                        conn.execute(
                            """INSERT OR REPLACE INTO local_conflicts
                               (conflict_id, entity_type, entity_uuid, reason,
                                losing_payload, winning_payload, resolved, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                            (
                                conflict_id,
                                entity_type,
                                entity_uuid,
                                status.upper(),
                                outbox_row["payload"],
                                json.dumps(merged_payload or {}, ensure_ascii=False),
                                store.now_utc(),
                            ),
                        )
                    if merged_payload is not None:
                        op = "purge" if status == "conflict_purged" else "upsert"
                        store.apply_remote_change(
                            conn,
                            {
                                "entity_type": entity_type,
                                "entity_uuid": entity_uuid,
                                "operation": op,
                                "revision": new_rev,
                                "origin_device": store.get_device_id(conn),
                                "changed_at": store.now_utc(),
                                "payload": merged_payload,
                            },
                        )

        conn.commit()
        if len(rows) < batch_size:
            break

    return {
        "pushed": total_pushed,
        "applied": total_applied,
        "results": all_results,
    }


def pull_changes(conn, server_url=None, token=None, limit=100):
    store._ensure_sync_schema(conn)
    resolved_url, resolved_token = _resolve_endpoint(conn, server_url, token)
    limit = max(1, min(int(limit or 100), 500))

    total_pulled = 0
    all_changes = []
    current_seq = int(store.get_sync_state(conn, "last_pulled_seq", "0") or 0)

    for _ in range(100):
        data = _post_json(
            f"{resolved_url}/v1/sync/pull",
            {"since_seq": current_seq, "limit": limit},
            token=resolved_token,
        )
        changes = data.get("changes") or []
        next_seq = int(
            data.get("next_seq") if data.get("next_seq") is not None else current_seq
        )
        has_more = bool(data.get("has_more"))

        for change in changes:
            store.apply_remote_change(conn, change)
            all_changes.append(change)
            total_pulled += 1

        active_leases = data.get("active_leases")
        if isinstance(active_leases, list):
            local_dev = store.get_device_id(conn)
            conn.execute("DELETE FROM local_leases WHERE is_local_holder = 0")
            for lease in active_leases:
                holder = lease.get("holder_device_id") or ""
                p_uuid = lease.get("profile_uuid")
                existing_token = None
                if holder == local_dev:
                    existing_row = conn.execute(
                        "SELECT lease_token FROM local_leases WHERE profile_uuid = ?",
                        (p_uuid,),
                    ).fetchone()
                    if existing_row:
                        existing_token = existing_row["lease_token"]
                conn.execute(
                    """INSERT OR REPLACE INTO local_leases
                       (profile_uuid, holder_device_id, holder_device_name,
                        lease_token, fencing_token, expires_at, is_local_holder)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        p_uuid,
                        holder,
                        lease.get("holder_device_name"),
                        existing_token,
                        int(lease.get("fencing_token") or 0),
                        int(lease.get("expires_at") or 0),
                        1 if holder == local_dev else 0,
                    ),
                )

        current_seq = next_seq
        store.set_sync_state(conn, "last_pulled_seq", str(current_seq))
        conn.commit()

        if not has_more or not changes:
            break

    store.set_sync_state(conn, "last_sync_at", store.now_utc())
    store.set_sync_state(conn, "last_sync_error", "")
    conn.commit()

    return {
        "pulled": total_pulled,
        "last_pulled_seq": current_seq,
        "changes": all_changes,
    }


def sync_cycle(conn=None, server_url=None, token=None):
    if conn is None:
        store.init()
        with store.db() as local_conn:
            return sync_cycle(local_conn, server_url=server_url, token=token)

    push_res = push_outbox(conn, server_url=server_url, token=token)
    pull_res = pull_changes(conn, server_url=server_url, token=token)
    return {
        "push": push_res,
        "pull": pull_res,
        "pushed": push_res["pushed"],
        "pulled": pull_res["pulled"],
        "last_pulled_seq": pull_res["last_pulled_seq"],
    }


def get_status(conn=None, db_path=None):
    if conn is None:
        store.init(db_path=db_path)
        with store.db(db_path=db_path) as local_conn:
            return get_status(local_conn, db_path=db_path)

    store._ensure_sync_schema(conn)
    creds = load_credentials(conn=conn, db_path=db_path)
    try:
        state_rows = conn.execute("SELECT key, value FROM sync_state").fetchall()
        state_map = {row["key"]: row["value"] for row in state_rows}
    except sqlite3.OperationalError:
        state_map = {}
    outbox_count = conn.execute("SELECT COUNT(*) AS c FROM sync_outbox").fetchone()["c"]
    device_id = creds.get("device_id") or state_map.get("device_id") or ""
    device_name = creds.get("device_name") or state_map.get("device_name") or ""
    server_url = creds.get("server_url") or state_map.get("server_url") or ""
    last_pulled_seq = int(state_map.get("last_pulled_seq", "0") or 0)
    last_sync_at = state_map.get("last_sync_at") or None
    last_sync_error = state_map.get("last_sync_error") or None
    return {
        "configured": bool(server_url and (creds.get("device_token") or state_map.get("device_token"))),
        "device_id": device_id,
        "device_name": device_name,
        "server_url": server_url,
        "outbox_count": int(outbox_count),
        "last_pulled_seq": last_pulled_seq,
        "last_sync_at": last_sync_at,
        "last_sync_error": last_sync_error,
    }


def _try_resolve_endpoint(conn=None, server_url=None, token=None):
    creds = load_credentials(conn=conn) if conn is not None else load_credentials()
    resolved_url = (
        server_url
        or creds.get("server_url")
        or (store.get_sync_state(conn, "server_url") if conn is not None else None)
        or ""
    ).strip().rstrip("/")
    resolved_token = (
        token
        or creds.get("device_token")
        or (store.get_sync_state(conn, "device_token") if conn is not None else None)
        or ""
    ).strip()
    if not resolved_url or not resolved_token:
        return None, None
    return resolved_url, resolved_token


def acquire_lease(conn, profile_uuid=None, force=False, server_url=None, token=None, timeout=3.0):
    if isinstance(conn, str) and profile_uuid is None:
        profile_uuid = conn
        conn = None
    if conn is None:
        store.init()
        with store.db() as local_conn:
            return acquire_lease(
                local_conn,
                profile_uuid,
                force=force,
                server_url=server_url,
                token=token,
                timeout=timeout,
            )

    store._ensure_sync_schema(conn)
    clean_uuid = str(profile_uuid or "").strip()
    if not clean_uuid:
        raise SyncAgentError("profile_uuid wajib diisi", code=2003)

    resolved_url, resolved_token = _try_resolve_endpoint(conn, server_url, token)
    if not resolved_url or not resolved_token:
        return {"status": "local", "fencing_token": 0, "offline": True}

    payload = {"profile_uuid": clean_uuid, "force": bool(force)}
    try:
        data = _post_json(
            f"{resolved_url}/v1/sync/lease/acquire",
            payload,
            token=resolved_token,
            timeout=timeout,
        )
    except SyncAgentError as error:
        if error.http_status == 404 and error.code == 2003:
            local_prof = conn.execute(
                "SELECT id FROM profiles WHERE uuid = ?", (clean_uuid,)
            ).fetchone()
            if local_prof is not None:
                try:
                    push_outbox(conn, server_url=resolved_url, token=resolved_token)
                    data = _post_json(
                        f"{resolved_url}/v1/sync/lease/acquire",
                        payload,
                        token=resolved_token,
                        timeout=timeout,
                    )
                except SyncAgentError as retry_err:
                    error = retry_err
                else:
                    error = None
        if error is not None:
            if error.code == 2004 or error.http_status == 409:
                raise SyncLeaseError(2004, error.message, error.data) from error
            if error.is_network_error or error.http_status is None:
                return {
                    "status": "offline_allowed",
                    "fencing_token": 0,
                    "offline": True,
                    "warning": "Server offline, profil dibuka secara lokal",
                }
            raise error

    creds = load_credentials(conn=conn)
    device_id = creds.get("device_id") or store.get_device_id(conn)
    device_name = creds.get("device_name") or store.get_sync_state(conn, "device_name") or ""
    conn.execute(
        """INSERT OR REPLACE INTO local_leases
           (profile_uuid, holder_device_id, holder_device_name,
            lease_token, fencing_token, expires_at, is_local_holder)
           VALUES (?, ?, ?, ?, ?, ?, 1)""",
        (
            clean_uuid,
            device_id,
            device_name,
            data["lease_token"],
            int(data["fencing_token"]),
            int(data["expires_at"]),
        ),
    )
    conn.commit()
    result = dict(data)
    result.setdefault("status", "acquired")
    result.setdefault("offline", False)
    return result


def renew_leases(conn=None, leases=None, server_url=None, token=None, timeout=5.0):
    if conn is None:
        store.init()
        with store.db() as local_conn:
            return renew_leases(
                local_conn,
                leases=leases,
                server_url=server_url,
                token=token,
                timeout=timeout,
            )

    store._ensure_sync_schema(conn)
    resolved_url, resolved_token = _try_resolve_endpoint(conn, server_url, token)
    if not resolved_url or not resolved_token:
        return {"renewed": []}

    if leases is None:
        rows = conn.execute(
            """SELECT profile_uuid, lease_token, fencing_token
               FROM local_leases
               WHERE is_local_holder = 1 AND lease_token IS NOT NULL AND lease_token != ''"""
        ).fetchall()
        leases = [
            {
                "profile_uuid": r["profile_uuid"],
                "lease_token": r["lease_token"],
                "fencing_token": int(r["fencing_token"] or 0),
            }
            for r in rows
        ]

    if not leases:
        return {"renewed": []}

    data = _post_json(
        f"{resolved_url}/v1/sync/lease/renew",
        {"leases": leases},
        token=resolved_token,
        timeout=timeout,
    )
    renewed = data.get("renewed") or []
    for item in renewed:
        p_uuid = item.get("profile_uuid")
        if item.get("status") == "ok" and p_uuid:
            conn.execute(
                "UPDATE local_leases SET expires_at = ? WHERE profile_uuid = ?",
                (int(item["expires_at"]), p_uuid),
            )
        elif item.get("status") == "stolen" and p_uuid:
            conn.execute(
                "DELETE FROM local_leases WHERE profile_uuid = ? AND is_local_holder = 1",
                (p_uuid,),
            )
    conn.commit()
    return {"renewed": renewed}


def release_lease(
    conn=None,
    profile_uuid=None,
    lease_token=None,
    server_url=None,
    token=None,
    force=False,
    timeout=3.0,
):
    if isinstance(conn, str) and profile_uuid is None:
        profile_uuid = conn
        conn = None
    if conn is None:
        store.init()
        with store.db() as local_conn:
            return release_lease(
                local_conn,
                profile_uuid,
                lease_token=lease_token,
                server_url=server_url,
                token=token,
                force=force,
                timeout=timeout,
            )

    clean_uuid = str(profile_uuid or "").strip()
    if not clean_uuid:
        return {"released": False}

    store._ensure_sync_schema(conn)
    if lease_token is None:
        row = conn.execute(
            "SELECT lease_token FROM local_leases WHERE profile_uuid = ?",
            (clean_uuid,),
        ).fetchone()
        if row and row["lease_token"]:
            lease_token = row["lease_token"]

    conn.execute("DELETE FROM local_leases WHERE profile_uuid = ?", (clean_uuid,))
    conn.commit()

    resolved_url, resolved_token = _try_resolve_endpoint(conn, server_url, token)
    if not resolved_url or not resolved_token:
        return {"released": True, "local": True}

    payload = {"profile_uuid": clean_uuid}
    if lease_token:
        payload["lease_token"] = lease_token
    if force:
        payload["force"] = True
    try:
        return _post_json(
            f"{resolved_url}/v1/sync/lease/release",
            payload,
            token=resolved_token,
            timeout=timeout,
        )
    except SyncAgentError:
        return {"released": True, "offline": True}


def upload_cookie_blob(
    conn,
    profile_id,
    profile_uuid,
    cookies_list,
    fencing_token=0,
    server_url=None,
    token=None,
    kek=None,
):
    if conn is None:
        store.init()
        with store.db() as local_conn:
            return upload_cookie_blob(
                local_conn,
                profile_id,
                profile_uuid,
                cookies_list,
                fencing_token=fencing_token,
                server_url=server_url,
                token=token,
                kek=kek,
            )

    store._ensure_sync_schema(conn)
    if isinstance(cookies_list, str):
        text = cookies_list.strip()
        cookie_items = json.loads(text) if text else []
    elif isinstance(cookies_list, list):
        cookie_items = cookies_list
    else:
        cookie_items = cookies._cookie_list(cookies_list)

    sha256_plain = cookies.cookie_digest(cookie_items)

    prof_row = None
    if profile_id is not None:
        prof_row = conn.execute(
            "SELECT * FROM profiles WHERE id = ?", (int(profile_id),)
        ).fetchone()
    if prof_row is None and profile_uuid:
        prof_row = conn.execute(
            "SELECT * FROM profiles WHERE uuid = ?", (str(profile_uuid).strip(),)
        ).fetchone()
    if prof_row is not None:
        if profile_id is None:
            profile_id = prof_row["id"]
        if not profile_uuid:
            profile_uuid = prof_row["uuid"]

    clean_uuid = str(profile_uuid or "").strip()
    if not clean_uuid:
        raise SyncAgentError("profile_uuid wajib diisi", code=2003)

    if (
        prof_row is not None
        and prof_row["cookie_sha256"] == sha256_plain
        and prof_row["cookie_blob_id"]
    ):
        return {
            "status": "skipped",
            "blob_id": prof_row["cookie_blob_id"],
            "profile_uuid": clean_uuid,
            "cookie_revision": int(prof_row["cookie_revision"] or 0),
            "sha256_plain": sha256_plain,
        }

    resolved_url, resolved_token = _resolve_endpoint(conn, server_url, token)

    if kek is None:
        kek, _ = store._load_local_crypto(conn)
    if kek is None:
        creds = load_credentials(conn=conn)
        if creds.get("kek_hex"):
            kek = bytes.fromhex(creds["kek_hex"])
    if not kek:
        raise SyncAgentError(
            "Kunci enkripsi sinkronisasi (KEK) belum tersedia", code=2001
        )

    if fencing_token is None:
        l_row = conn.execute(
            "SELECT fencing_token FROM local_leases WHERE profile_uuid = ?",
            (clean_uuid,),
        ).fetchone()
        fencing_token = int(l_row["fencing_token"] or 0) if l_row else 0
    else:
        fencing_token = int(fencing_token or 0)

    json_bytes = json.dumps(
        cookie_items, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    next_rev = (int(prof_row["cookie_revision"] or 0) if prof_row else 0) + 1

    def _send_upload(rev_num):
        cipher_env, _, sha256_cipher, size_bytes = sync_crypto.encrypt_blob(
            json_bytes, kek, clean_uuid, "cookies", rev_num
        )
        return _post_json(
            f"{resolved_url}/v1/sync/blob/upload",
            {
                "profile_uuid": clean_uuid,
                "blob_kind": "cookies",
                "fencing_token": fencing_token,
                "revision": rev_num,
                "encryption_scheme": "aes-256-gcm-v1",
                "sha256_plain": sha256_plain,
                "sha256_cipher": sha256_cipher,
                "size_bytes": size_bytes,
                "ciphertext_base64": cipher_env,
            },
            token=resolved_token,
        )

    try:
        try:
            resp = _send_upload(next_rev)
        except SyncAgentError as retry_err:
            if (
                retry_err.code == 2007
                and isinstance(retry_err.data, dict)
                and retry_err.data.get("expected_revision")
            ):
                next_rev = int(retry_err.data["expected_revision"])
                resp = _send_upload(next_rev)
            else:
                raise
    except SyncAgentError as error:
        if error.code == 2006 or error.http_status == 409:
            conflict_id = f"conf_cookie_{secrets.token_hex(8)}"
            ts_num = int(time.time())
            db_dir = store._conn_db_path(conn).parent
            pid_str = str(profile_id if profile_id is not None else "unknown")
            backup_path = (
                db_dir / "profiles" / pid_str / f"cookies-conflict-{ts_num}.json"
            )
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_payload = {
                "profile_id": profile_id,
                "profile_uuid": clean_uuid,
                "fencing_token": fencing_token,
                "sha256_plain": sha256_plain,
                "created_at": store.now_utc(),
                "cookies": cookie_items,
            }
            backup_text = json.dumps(backup_payload, ensure_ascii=False, indent=2)
            backup_path.write_text(backup_text, encoding="utf-8")

            from . import launcher

            alt_backup = (
                launcher.PROFILES_DIR / pid_str / f"cookies-conflict-{ts_num}.json"
            )
            if alt_backup != backup_path:
                try:
                    alt_backup.parent.mkdir(parents=True, exist_ok=True)
                    alt_backup.write_text(backup_text, encoding="utf-8")
                except OSError:
                    pass

            conn.execute(
                """INSERT OR REPLACE INTO local_conflicts
                   (conflict_id, entity_type, entity_uuid, reason,
                    losing_payload, winning_payload, resolved, created_at)
                   VALUES (?, 'cookie_blob', ?, 'STALE_FENCING_TOKEN', ?, ?, 0, ?)""",
                (
                    conflict_id,
                    clean_uuid,
                    json.dumps(backup_payload, ensure_ascii=False),
                    json.dumps(error.data or {}, ensure_ascii=False),
                    store.now_utc(),
                ),
            )
            conn.commit()
            return {
                "status": "conflict",
                "error": "STALE_FENCING_TOKEN",
                "conflict_id": conflict_id,
                "backup_path": str(backup_path),
            }
        raise

    if profile_id is not None:
        conn.execute(
            """UPDATE profiles
               SET cookie_blob_id = ?,
                   cookie_revision = ?,
                   cookie_sha256 = ?
               WHERE id = ?""",
            (
                resp["blob_id"],
                int(resp["cookie_revision"]),
                sha256_plain,
                int(profile_id),
            ),
        )
    else:
        conn.execute(
            """UPDATE profiles
               SET cookie_blob_id = ?,
                   cookie_revision = ?,
                   cookie_sha256 = ?
               WHERE uuid = ?""",
            (
                resp["blob_id"],
                int(resp["cookie_revision"]),
                sha256_plain,
                clean_uuid,
            ),
        )
    conn.commit()
    return resp


def download_and_inject_cookies(
    conn,
    profile_id,
    profile_uuid,
    blob_id,
    server_url=None,
    token=None,
    kek=None,
):
    if conn is None:
        store.init()
        with store.db() as local_conn:
            return download_and_inject_cookies(
                local_conn,
                profile_id,
                profile_uuid,
                blob_id,
                server_url=server_url,
                token=token,
                kek=kek,
            )

    store._ensure_sync_schema(conn)
    clean_blob_id = str(blob_id or "").strip()
    if not clean_blob_id:
        raise SyncAgentError("blob_id wajib diisi", code=2003)

    resolved_url, resolved_token = _resolve_endpoint(conn, server_url, token)
    resp = _post_json(
        f"{resolved_url}/v1/sync/blob/download",
        {"blob_id": clean_blob_id},
        token=resolved_token,
    )

    if kek is None:
        kek, _ = store._load_local_crypto(conn)
    if kek is None:
        creds = load_credentials(conn=conn)
        if creds.get("kek_hex"):
            kek = bytes.fromhex(creds["kek_hex"])
    if not kek:
        raise SyncAgentError(
            "Kunci enkripsi sinkronisasi (KEK) belum tersedia", code=2001
        )

    ciphertext_b64 = resp.get("ciphertext_base64") or ""
    blob_kind = resp.get("blob_kind") or "cookies"
    revision = int(resp.get("revision") or 1)
    target_uuid = str(profile_uuid or resp.get("profile_uuid") or "").strip()

    plain_bytes = sync_crypto.decrypt_blob(
        ciphertext_b64, kek, target_uuid, blob_kind, revision
    )
    try:
        cookie_list = json.loads(plain_bytes.decode("utf-8"))
    except Exception as error:
        raise sync_crypto.SyncCryptoError(
            "Payload cookie hasil dekripsi bukan JSON valid"
        ) from error

    expected_sha256 = str(resp.get("sha256_plain") or "").strip()
    computed_digest = cookies.cookie_digest(cookie_list)
    raw_sha256 = hashlib.sha256(plain_bytes).hexdigest()
    if expected_sha256 and expected_sha256 not in (computed_digest, raw_sha256):
        raise sync_crypto.SyncCryptoError("Hash plaintext cookie tidak cocok")

    from . import launcher, paths

    conn_profiles_dir = store._conn_db_path(conn).parent / "profiles"
    prev_profiles_dir = launcher.PROFILES_DIR
    override_dir = prev_profiles_dir == paths.DATA_DIR / "profiles"
    if override_dir:
        launcher.PROFILES_DIR = conn_profiles_dir
    try:
        cookies.update_cookies(int(profile_id), cookie_list)
    finally:
        if override_dir:
            launcher.PROFILES_DIR = prev_profiles_dir

    conn.execute(
        """UPDATE profiles
           SET cookie_blob_id = ?,
               cookie_revision = ?,
               cookie_sha256 = ?
           WHERE id = ?""",
        (
            clean_blob_id,
            revision,
            expected_sha256 or computed_digest,
            int(profile_id),
        ),
    )
    conn.commit()
    return True


def _resolve_kek(conn, kek=None):
    if kek is None:
        kek, _ = store._load_local_crypto(conn)
    if kek is None:
        creds = load_credentials(conn=conn)
        if creds.get("kek_hex"):
            kek = bytes.fromhex(creds["kek_hex"])
    if not kek:
        raise SyncAgentError(
            "Kunci enkripsi sinkronisasi (KEK) belum tersedia", code=2001
        )
    return kek


def _resolve_profile_row_and_uuid(conn, profile_id, profile_uuid):
    prof_row = None
    if profile_id is not None:
        prof_row = conn.execute(
            "SELECT * FROM profiles WHERE id = ?", (int(profile_id),)
        ).fetchone()
    if prof_row is None and profile_uuid:
        prof_row = conn.execute(
            "SELECT * FROM profiles WHERE uuid = ?", (str(profile_uuid).strip(),)
        ).fetchone()
    if prof_row is not None:
        if profile_id is None:
            profile_id = prof_row["id"]
        if not profile_uuid:
            profile_uuid = prof_row["uuid"]
    clean_uuid = str(profile_uuid or "").strip()
    if not clean_uuid:
        raise SyncAgentError("profile_uuid wajib diisi", code=2003)
    return prof_row, profile_id, clean_uuid


def upload_bookmark_blob(
    conn,
    profile_id,
    profile_uuid=None,
    fencing_token=None,
    bookmarks_data=None,
    server_url=None,
    token=None,
    kek=None,
):
    if conn is None:
        store.init()
        with store.db() as local_conn:
            return upload_bookmark_blob(
                local_conn,
                profile_id,
                profile_uuid=profile_uuid,
                fencing_token=fencing_token,
                bookmarks_data=bookmarks_data,
                server_url=server_url,
                token=token,
                kek=kek,
            )

    if isinstance(fencing_token, (dict, list, bytes, bytearray)) or (
        isinstance(fencing_token, str) and not fencing_token.strip().isdigit()
    ):
        if bookmarks_data is None:
            bookmarks_data = fencing_token
            fencing_token = None
        elif isinstance(bookmarks_data, int):
             bookmarks_data, fencing_token = fencing_token, bookmarks_data

    store._ensure_sync_schema(conn)
    prof_row, profile_id, clean_uuid = _resolve_profile_row_and_uuid(
        conn, profile_id, profile_uuid
    )

    from . import launcher, paths

    conn_profiles_dir = store._conn_db_path(conn).parent / "profiles"
    prev_profiles_dir = launcher.PROFILES_DIR
    override_dir = prev_profiles_dir == paths.DATA_DIR / "profiles"
    if override_dir:
        launcher.PROFILES_DIR = conn_profiles_dir
    try:
        if bookmarks_data is None:
            bm_path = bookmarks._bookmark_path(profile_id)
            if not bm_path.is_file():
                return {
                    "status": "skipped",
                    "reason": "no_bookmarks",
                    "profile_uuid": clean_uuid,
                }
            data_obj = bookmarks._load(bm_path)
        elif isinstance(bookmarks_data, (bytes, bytearray)):
            data_obj = json.loads(bookmarks_data.decode("utf-8"))
        elif isinstance(bookmarks_data, str):
            data_obj = json.loads(bookmarks_data)
        else:
            data_obj = dict(bookmarks_data)
        sha256_plain = bookmarks.bookmark_digest(data_obj)
    finally:
        if override_dir:
            launcher.PROFILES_DIR = prev_profiles_dir

    if (
        prof_row is not None
        and store._row_val(prof_row, "bookmark_sha256") == sha256_plain
        and store._row_val(prof_row, "bookmark_blob_id")
    ):
        return {
            "status": "skipped",
            "blob_id": prof_row["bookmark_blob_id"],
            "profile_uuid": clean_uuid,
            "bookmark_revision": int(prof_row["bookmark_revision"] or 0),
            "sha256_plain": sha256_plain,
        }

    resolved_url, resolved_token = _resolve_endpoint(conn, server_url, token)
    kek = _resolve_kek(conn, kek)

    if fencing_token is None:
        l_row = conn.execute(
            "SELECT fencing_token FROM local_leases WHERE profile_uuid = ?",
            (clean_uuid,),
        ).fetchone()
        fencing_token = int(l_row["fencing_token"] or 0) if l_row else 0
    else:
        fencing_token = int(fencing_token or 0)

    data_bytes = json.dumps(
        data_obj, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    next_rev = (
        int(store._row_val(prof_row, "bookmark_revision") or 0) if prof_row else 0
    ) + 1

    def _send_upload(rev_num):
        cipher_env, _, sha256_cipher, size_bytes = sync_crypto.encrypt_blob(
            data_bytes, kek, clean_uuid, "bookmarks", rev_num
        )
        return _post_json(
            f"{resolved_url}/v1/sync/blob/upload",
            {
                "profile_uuid": clean_uuid,
                "blob_kind": "bookmarks",
                "fencing_token": fencing_token,
                "revision": rev_num,
                "encryption_scheme": "aes-256-gcm-v1",
                "sha256_plain": sha256_plain,
                "sha256_cipher": sha256_cipher,
                "size_bytes": size_bytes,
                "ciphertext_base64": cipher_env,
            },
            token=resolved_token,
        )

    try:
        resp = _send_upload(next_rev)
    except SyncAgentError as retry_err:
        if (
            retry_err.code == 2007
            and isinstance(retry_err.data, dict)
            and retry_err.data.get("expected_revision")
        ):
            next_rev = int(retry_err.data["expected_revision"])
            resp = _send_upload(next_rev)
        else:
            raise

    saved_rev = int(resp.get("bookmark_revision") or resp.get("revision") or next_rev)
    if profile_id is not None:
        conn.execute(
            """UPDATE profiles
               SET bookmark_blob_id = ?,
                   bookmark_revision = ?,
                   bookmark_sha256 = ?
               WHERE id = ?""",
            (resp["blob_id"], saved_rev, sha256_plain, int(profile_id)),
        )
    else:
        conn.execute(
            """UPDATE profiles
               SET bookmark_blob_id = ?,
                   bookmark_revision = ?,
                   bookmark_sha256 = ?
               WHERE uuid = ?""",
            (resp["blob_id"], saved_rev, sha256_plain, clean_uuid),
        )
    conn.commit()
    return resp


def download_and_inject_bookmarks(
    conn,
    profile_id,
    profile_uuid,
    blob_id,
    server_url=None,
    token=None,
    kek=None,
):
    if conn is None:
        store.init()
        with store.db() as local_conn:
            return download_and_inject_bookmarks(
                local_conn,
                profile_id,
                profile_uuid,
                blob_id,
                server_url=server_url,
                token=token,
                kek=kek,
            )

    store._ensure_sync_schema(conn)
    clean_blob_id = str(blob_id or "").strip()
    if not clean_blob_id:
        raise SyncAgentError("blob_id wajib diisi", code=2003)

    resolved_url, resolved_token = _resolve_endpoint(conn, server_url, token)
    resp = _post_json(
        f"{resolved_url}/v1/sync/blob/download",
        {"blob_id": clean_blob_id},
        token=resolved_token,
    )
    kek = _resolve_kek(conn, kek)

    ciphertext_b64 = resp.get("ciphertext_base64") or ""
    blob_kind = resp.get("blob_kind") or "bookmarks"
    revision = int(resp.get("revision") or 1)
    target_uuid = str(profile_uuid or resp.get("profile_uuid") or "").strip()

    plain_bytes = sync_crypto.decrypt_blob(
        ciphertext_b64, kek, target_uuid, blob_kind, revision
    )
    try:
        bookmark_data = json.loads(plain_bytes.decode("utf-8"))
    except Exception as error:
        raise sync_crypto.SyncCryptoError(
            "Payload bookmark hasil dekripsi bukan JSON valid"
        ) from error

    expected_sha256 = str(resp.get("sha256_plain") or "").strip()
    computed_digest = bookmarks.bookmark_digest(bookmark_data)
    raw_sha256 = hashlib.sha256(plain_bytes).hexdigest()
    if expected_sha256 and expected_sha256 not in (computed_digest, raw_sha256):
        raise sync_crypto.SyncCryptoError("Hash plaintext bookmark tidak cocok")

    from . import launcher, paths

    conn_profiles_dir = store._conn_db_path(conn).parent / "profiles"
    prev_profiles_dir = launcher.PROFILES_DIR
    override_dir = prev_profiles_dir == paths.DATA_DIR / "profiles"
    if override_dir:
        launcher.PROFILES_DIR = conn_profiles_dir
    try:
        bookmarks.save_bookmarks_data(int(profile_id), bookmark_data)
    finally:
        if override_dir:
            launcher.PROFILES_DIR = prev_profiles_dir

    conn.execute(
        """UPDATE profiles
           SET bookmark_blob_id = ?,
               bookmark_revision = ?,
               bookmark_sha256 = ?
           WHERE id = ?""",
        (
            clean_blob_id,
            revision,
            expected_sha256 or computed_digest,
            int(profile_id),
        ),
    )
    conn.commit()
    return True


def upload_password_blob(
    conn,
    profile_id,
    profile_uuid=None,
    fencing_token=None,
    passwords_list=None,
    server_url=None,
    token=None,
    kek=None,
):
    if conn is None:
        store.init()
        with store.db() as local_conn:
            return upload_password_blob(
                local_conn,
                profile_id,
                profile_uuid=profile_uuid,
                fencing_token=fencing_token,
                passwords_list=passwords_list,
                server_url=server_url,
                token=token,
                kek=kek,
            )

    if isinstance(fencing_token, (list, dict, bytes, bytearray)) or (
        isinstance(fencing_token, str) and not fencing_token.strip().isdigit()
    ):
        if passwords_list is None:
            passwords_list = fencing_token
            fencing_token = None
        elif isinstance(passwords_list, int):
            passwords_list, fencing_token = fencing_token, passwords_list

    store._ensure_sync_schema(conn)
    prof_row, profile_id, clean_uuid = _resolve_profile_row_and_uuid(
        conn, profile_id, profile_uuid
    )

    from . import launcher, paths

    conn_profiles_dir = store._conn_db_path(conn).parent / "profiles"
    prev_profiles_dir = launcher.PROFILES_DIR
    override_dir = prev_profiles_dir == paths.DATA_DIR / "profiles"
    if override_dir:
        launcher.PROFILES_DIR = conn_profiles_dir
    try:
        if passwords_list is None:
            items = passwords.list_saved_passwords(int(profile_id))
        else:
            items = passwords._normalize_password_items(passwords_list)
    finally:
        if override_dir:
            launcher.PROFILES_DIR = prev_profiles_dir

    if not items:
        return {
            "status": "skipped",
            "reason": "empty_passwords",
            "profile_uuid": clean_uuid,
        }

    sha256_plain = passwords.password_digest(items)
    if (
        prof_row is not None
        and store._row_val(prof_row, "password_sha256") == sha256_plain
        and store._row_val(prof_row, "password_blob_id")
    ):
        return {
            "status": "skipped",
            "blob_id": prof_row["password_blob_id"],
            "profile_uuid": clean_uuid,
            "password_revision": int(prof_row["password_revision"] or 0),
            "sha256_plain": sha256_plain,
        }

    resolved_url, resolved_token = _resolve_endpoint(conn, server_url, token)
    kek = _resolve_kek(conn, kek)

    if fencing_token is None:
        l_row = conn.execute(
            "SELECT fencing_token FROM local_leases WHERE profile_uuid = ?",
            (clean_uuid,),
        ).fetchone()
        fencing_token = int(l_row["fencing_token"] or 0) if l_row else 0
    else:
        fencing_token = int(fencing_token or 0)

    json_bytes = json.dumps(
        items, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    next_rev = (
        int(store._row_val(prof_row, "password_revision") or 0) if prof_row else 0
    ) + 1

    def _send_upload(rev_num):
        cipher_env, _, sha256_cipher, size_bytes = sync_crypto.encrypt_blob(
            json_bytes, kek, clean_uuid, "passwords", rev_num
        )
        return _post_json(
            f"{resolved_url}/v1/sync/blob/upload",
            {
                "profile_uuid": clean_uuid,
                "blob_kind": "passwords",
                "fencing_token": fencing_token,
                "revision": rev_num,
                "encryption_scheme": "aes-256-gcm-v1",
                "sha256_plain": sha256_plain,
                "sha256_cipher": sha256_cipher,
                "size_bytes": size_bytes,
                "ciphertext_base64": cipher_env,
            },
            token=resolved_token,
        )

    try:
        resp = _send_upload(next_rev)
    except SyncAgentError as retry_err:
        if (
            retry_err.code == 2007
            and isinstance(retry_err.data, dict)
            and retry_err.data.get("expected_revision")
        ):
            next_rev = int(retry_err.data["expected_revision"])
            resp = _send_upload(next_rev)
        else:
            raise

    saved_rev = int(resp.get("password_revision") or resp.get("revision") or next_rev)
    if profile_id is not None:
        conn.execute(
            """UPDATE profiles
               SET password_blob_id = ?,
                   password_revision = ?,
                   password_sha256 = ?
               WHERE id = ?""",
            (resp["blob_id"], saved_rev, sha256_plain, int(profile_id)),
        )
    else:
        conn.execute(
            """UPDATE profiles
               SET password_blob_id = ?,
                   password_revision = ?,
                   password_sha256 = ?
               WHERE uuid = ?""",
            (resp["blob_id"], saved_rev, sha256_plain, clean_uuid),
        )
    conn.commit()
    return resp


def download_and_inject_passwords(
    conn,
    profile_id,
    profile_uuid,
    blob_id,
    server_url=None,
    token=None,
    kek=None,
):
    if conn is None:
        store.init()
        with store.db() as local_conn:
            return download_and_inject_passwords(
                local_conn,
                profile_id,
                profile_uuid,
                blob_id,
                server_url=server_url,
                token=token,
                kek=kek,
            )

    store._ensure_sync_schema(conn)
    clean_blob_id = str(blob_id or "").strip()
    if not clean_blob_id:
        raise SyncAgentError("blob_id wajib diisi", code=2003)

    resolved_url, resolved_token = _resolve_endpoint(conn, server_url, token)
    resp = _post_json(
        f"{resolved_url}/v1/sync/blob/download",
        {"blob_id": clean_blob_id},
        token=resolved_token,
    )
    kek = _resolve_kek(conn, kek)

    ciphertext_b64 = resp.get("ciphertext_base64") or ""
    blob_kind = resp.get("blob_kind") or "passwords"
    revision = int(resp.get("revision") or 1)
    target_uuid = str(profile_uuid or resp.get("profile_uuid") or "").strip()

    plain_bytes = sync_crypto.decrypt_blob(
        ciphertext_b64, kek, target_uuid, blob_kind, revision
    )
    try:
        items = json.loads(plain_bytes.decode("utf-8"))
    except Exception as error:
        raise sync_crypto.SyncCryptoError(
            "Payload password hasil dekripsi bukan JSON valid"
        ) from error

    expected_sha256 = str(resp.get("sha256_plain") or "").strip()
    computed_digest = passwords.password_digest(items)
    raw_sha256 = hashlib.sha256(plain_bytes).hexdigest()
    if expected_sha256 and expected_sha256 not in (computed_digest, raw_sha256):
        raise sync_crypto.SyncCryptoError("Hash plaintext password tidak cocok")

    from . import launcher, paths

    conn_profiles_dir = store._conn_db_path(conn).parent / "profiles"
    prev_profiles_dir = launcher.PROFILES_DIR
    override_dir = prev_profiles_dir == paths.DATA_DIR / "profiles"
    if override_dir:
        launcher.PROFILES_DIR = conn_profiles_dir
    try:
        passwords.update_passwords(int(profile_id), items)
    finally:
        if override_dir:
            launcher.PROFILES_DIR = prev_profiles_dir

    conn.execute(
        """UPDATE profiles
           SET password_blob_id = ?,
               password_revision = ?,
               password_sha256 = ?
           WHERE id = ?""",
        (
            clean_blob_id,
            revision,
            expected_sha256 or computed_digest,
            int(profile_id),
        ),
    )
    conn.commit()
    return True


def upload_extension_blob(
    conn,
    extension_slug,
    extension_uuid=None,
    server_url=None,
    token=None,
    kek=None,
):
    if conn is None:
        store.init()
        with store.db() as local_conn:
            return upload_extension_blob(
                local_conn,
                extension_slug,
                extension_uuid=extension_uuid,
                server_url=server_url,
                token=token,
                kek=kek,
            )

    store._ensure_sync_schema(conn)
    clean_slug = str(extension_slug or "").strip()
    if not clean_slug:
        raise SyncAgentError("extension_slug wajib diisi", code=2003)

    ext_row = conn.execute(
        "SELECT * FROM extensions WHERE slug = ? OR uuid = ? ORDER BY id LIMIT 1",
        (clean_slug, str(extension_uuid or clean_slug)),
    ).fetchone()

    from . import paths

    conn_ext_dir = store._conn_db_path(conn).parent / "extensions"
    prev_ext_dir = extensions.EXTENSIONS_DIR
    override_ext = prev_ext_dir == paths.DATA_DIR / "extensions"
    if override_ext:
        extensions.EXTENSIONS_DIR = conn_ext_dir
    try:
        archive_bytes, sha256_plain = extensions.get_extension_archive(clean_slug)
    finally:
        if override_ext:
            extensions.EXTENSIONS_DIR = prev_ext_dir

    if ext_row is not None:
        ext_uuid = str(extension_uuid or ext_row["uuid"] or uuid.uuid4())
        title = ext_row["title"] or clean_slug
        version = ext_row["version"] or "1.0"
        if (
            store._row_val(ext_row, "archive_sha256") == sha256_plain
            and store._row_val(ext_row, "archive_blob_id")
        ):
            return {
                "status": "skipped",
                "blob_id": ext_row["archive_blob_id"],
                "extension_uuid": ext_uuid,
                "slug": clean_slug,
                "revision": int(ext_row["revision"] or 0),
                "sha256_plain": sha256_plain,
            }
        next_rev = int(ext_row["revision"] or 0) + 1
    else:
        ext_uuid = str(extension_uuid or uuid.uuid4())
        title = clean_slug
        version = "1.0"
        next_rev = 1

    resolved_url, resolved_token = _resolve_endpoint(conn, server_url, token)
    kek = _resolve_kek(conn, kek)

    def _send_upload(rev_num):
        cipher_env, _, sha256_cipher, size_bytes = sync_crypto.encrypt_blob(
            archive_bytes, kek, ext_uuid, "extension_pkg", rev_num
        )
        return _post_json(
            f"{resolved_url}/v1/sync/blob/upload",
            {
                "profile_uuid": ext_uuid,
                "extension_uuid": ext_uuid,
                "slug": clean_slug,
                "title": title,
                "version": version,
                "blob_kind": "extension_pkg",
                "fencing_token": 0,
                "revision": rev_num,
                "encryption_scheme": "aes-256-gcm-v1",
                "sha256_plain": sha256_plain,
                "sha256_cipher": sha256_cipher,
                "size_bytes": size_bytes,
                "ciphertext_base64": cipher_env,
            },
            token=resolved_token,
        )

    try:
        resp = _send_upload(next_rev)
    except SyncAgentError as retry_err:
        if (
            retry_err.code == 2007
            and isinstance(retry_err.data, dict)
            and retry_err.data.get("expected_revision")
        ):
            next_rev = int(retry_err.data["expected_revision"])
            resp = _send_upload(next_rev)
        else:
            raise

    saved_rev = int(resp.get("revision") or next_rev)
    resp["sha256_plain"] = sha256_plain
    if ext_row is not None:
        conn.execute(
            """UPDATE extensions
               SET uuid = ?,
                   revision = ?,
                   archive_blob_id = ?,
                   archive_sha256 = ?,
                   sync_dirty = 0
               WHERE id = ?""",
            (ext_uuid, saved_rev, resp["blob_id"], sha256_plain, ext_row["id"]),
        )
        conn.execute(
            "DELETE FROM sync_outbox WHERE entity_type = 'extension' AND entity_uuid = ?",
            (ext_uuid,),
        )
        conn.commit()
    return resp


def download_and_install_extension(
    conn,
    extension_slug,
    blob_id,
    extension_uuid=None,
    title=None,
    version=None,
    server_url=None,
    token=None,
    kek=None,
):
    if conn is None:
        store.init()
        with store.db() as local_conn:
            return download_and_install_extension(
                local_conn,
                extension_slug,
                blob_id,
                extension_uuid=extension_uuid,
                title=title,
                version=version,
                server_url=server_url,
                token=token,
                kek=kek,
            )

    store._ensure_sync_schema(conn)
    clean_slug = str(extension_slug or "").strip()
    clean_blob_id = str(blob_id or "").strip()
    if not clean_blob_id:
        raise SyncAgentError("blob_id wajib diisi", code=2003)

    resolved_url, resolved_token = _resolve_endpoint(conn, server_url, token)
    resp = _post_json(
        f"{resolved_url}/v1/sync/blob/download",
        {"blob_id": clean_blob_id},
        token=resolved_token,
    )
    kek = _resolve_kek(conn, kek)

    ciphertext_b64 = resp.get("ciphertext_base64") or ""
    blob_kind = resp.get("blob_kind") or "extension_pkg"
    revision = int(resp.get("revision") or 1)
    target_uuid = str(
        extension_uuid or resp.get("profile_uuid") or clean_slug
    ).strip()

    plain_bytes = sync_crypto.decrypt_blob(
        ciphertext_b64, kek, target_uuid, blob_kind, revision
    )
    expected_sha256 = str(resp.get("sha256_plain") or "").strip()
    raw_sha256 = hashlib.sha256(plain_bytes).hexdigest()
    if expected_sha256 and expected_sha256 != raw_sha256:
        raise sync_crypto.SyncCryptoError("Hash paket ekstensi tidak cocok")

    installed = extensions.install_extension_archive(
        clean_slug,
        plain_bytes,
        title=title,
        version=version,
        conn=conn,
    )
    conn.execute(
        """UPDATE extensions
           SET uuid = COALESCE(uuid, ?),
               revision = ?,
               archive_blob_id = ?,
               archive_sha256 = ?,
               sync_dirty = 0
           WHERE slug = ?""",
        (
            target_uuid,
            revision,
            clean_blob_id,
            expected_sha256 or raw_sha256,
            installed["slug"],
        ),
    )
    conn.commit()
    return installed


_heartbeat_lock = threading.Lock()
_heartbeat_thread = None
_heartbeat_stop_event = threading.Event()


def _heartbeat_step(db_path=None):
    from . import launcher

    try:
        launcher.opened_profiles()
    except Exception:
        pass

    try:
        with store.db(db_path=db_path) as conn:
            store._ensure_sync_schema(conn)
            rows = conn.execute(
                """SELECT profile_uuid, lease_token, fencing_token
                   FROM local_leases
                   WHERE is_local_holder = 1 AND lease_token IS NOT NULL AND lease_token != ''"""
            ).fetchall()
            if rows:
                leases = [
                    {
                        "profile_uuid": r["profile_uuid"],
                        "lease_token": r["lease_token"],
                        "fencing_token": int(r["fencing_token"] or 0),
                    }
                    for r in rows
                ]
                renew_leases(conn, leases=leases)
    except Exception:
        pass


def start_heartbeat(interval=20, db_path=None):
    global _heartbeat_thread
    with _heartbeat_lock:
        if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
            return _heartbeat_thread
        _heartbeat_stop_event.clear()
        wait_seconds = max(0.1, float(interval or 20))

        def _worker():
            while not _heartbeat_stop_event.wait(wait_seconds):
                _heartbeat_step(db_path=db_path)

        _heartbeat_thread = threading.Thread(
            target=_worker, name="antidetect-lease-heartbeat", daemon=True
        )
        _heartbeat_thread.start()
        return _heartbeat_thread


def stop_heartbeat():
    global _heartbeat_thread
    with _heartbeat_lock:
        _heartbeat_stop_event.set()
        thread = _heartbeat_thread
        _heartbeat_thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)
