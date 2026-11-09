#!/usr/bin/env python3
"""Uji gabungan sinkronisasi mekanis: jangan catat konflik palsu."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRATCH_ROOT = Path("/home/zdna/.hermes/cache/scratch/antigravity")
SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
WORK_DIR = Path(tempfile.mkdtemp(prefix="sync-merge-notice-", dir=str(SCRATCH_ROOT)))
os.environ["ANTIDETECT_HOME"] = str(WORK_DIR / "home")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from antidetect import store, sync_agent  # noqa: E402


def _new_extension(conn):
    ext_uuid = "11111111-2222-3333-4444-555555555555"
    ts = store.now_utc()
    conn.execute(
        """INSERT INTO extensions
           (slug, title, version, path, created_at, uuid, revision,
            deleted_at, origin_device, sync_dirty)
           VALUES (?, ?, ?, NULL, ?, ?, 0, NULL, ?, 1)""",
        (
            "test-ext",
            "Test Extension",
            "1.0.0",
            ts,
            ext_uuid,
            store.get_device_id(conn),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM extensions WHERE uuid = ?", (ext_uuid,)).fetchone()
    return dict(row)


def _extension_sync_payload(ext_row, ext_uuid):
    """Mirip store._extension_sync_payload."""
    return {
        "uuid": ext_uuid,
        "slug": "test-ext",
        "title": "Test Extension",
        "version": "1.0.0",
        "archive_blob_id": None,
        "archive_sha256": None,
        "created_at": ext_row["created_at"],
        "updated_at": store.now_utc(),
        "deleted_at": None,
    }


def _enqueue_extension(conn, ext_uuid, title="Test Extension", ext_row=None):
    if ext_row is None:
        ext_row = conn.execute("SELECT * FROM extensions WHERE uuid = ?", (ext_uuid,)).fetchone()
    payload = _extension_sync_payload(ext_row, ext_uuid)
    if title != "Test Extension":
        payload["title"] = title
    conn.execute(
        """INSERT INTO sync_outbox
           (idempotency_key, entity_type, entity_uuid, operation,
            base_revision, payload, created_at)
           VALUES (?, 'extension', ?, 'upsert', 0, ?, ?)""",
        ("idem-ext-1", ext_uuid, json.dumps(payload, ensure_ascii=False), store.now_utc()),
    )
    conn.commit()


def test_mechanical_merge_no_conflict(conn):
    ext = _new_extension(conn)
    ext_uuid = ext["uuid"]
    _enqueue_extension(conn, ext_uuid, ext_row=ext)

    client_payload = _extension_sync_payload(ext, ext_uuid)
    merged_payload = {
        **client_payload,
        "archive_blob_id": "blob_abc123",
        "archive_sha256": "deadbeef",
        "updated_at": "2026-11-10T00:00:00Z",
    }

    def fake_post_json(url, payload, token=None, timeout=15):
        assert url.endswith("/v1/sync/push")
        mutation = payload["mutations"][0]
        return {
            "results": [
                {
                    "idempotency_key": mutation["idempotency_key"],
                    "entity_uuid": mutation["entity_uuid"],
                    "status": "merged",
                    "new_revision": 2,
                    "seq": 10,
                    "conflict_id": "conf_test_1",
                    "merged_payload": merged_payload,
                }
            ]
        }

    original = sync_agent._post_json
    sync_agent._post_json = fake_post_json
    try:
        res = sync_agent.push_outbox(conn, server_url="http://127.0.0.1:1", token="t")
    finally:
        sync_agent._post_json = original

    assert res["pushed"] == 1
    conflicts = conn.execute("SELECT * FROM local_conflicts").fetchall()
    assert len(conflicts) == 0, f"Gabungan mekanis tidak boleh mencatat konflik: {[dict(r) for r in conflicts]}"
    outbox = conn.execute("SELECT * FROM sync_outbox").fetchall()
    assert len(outbox) == 0, "Baris outbox harus terhapus"
    row = conn.execute(
        "SELECT revision, sync_dirty, archive_blob_id, archive_sha256 FROM extensions WHERE uuid = ?",
        (ext_uuid,),
    ).fetchone()
    assert row["revision"] == 2
    assert row["sync_dirty"] == 0
    assert row["archive_blob_id"] == "blob_abc123"
    assert row["archive_sha256"] == "deadbeef"
    print("OK: gabungan mekanis tidak mencatat konflik, revisi lokal diperbarui")


def test_real_merge_still_conflicts(conn):
    # Bersihkan sisa data dari tes sebelumnya
    conn.execute("DELETE FROM local_conflicts")
    conn.execute("DELETE FROM sync_outbox")
    conn.execute("DELETE FROM extensions")
    conn.commit()

    ext = _new_extension(conn)
    ext_uuid = ext["uuid"]
    _enqueue_extension(conn, ext_uuid)

    client_payload = _extension_sync_payload(ext, ext_uuid)
    merged_payload = {
        **client_payload,
        "title": "Judul Dari Server",  # perubahan isi nyata, kunci lain identik
        "archive_blob_id": "blob_abc123",
        "updated_at": "2026-11-10T00:00:00Z",
    }

    def fake_post_json(url, payload, token=None, timeout=15):
        mutation = payload["mutations"][0]
        return {
            "results": [
                {
                    "idempotency_key": mutation["idempotency_key"],
                    "entity_uuid": mutation["entity_uuid"],
                    "status": "merged",
                    "new_revision": 2,
                    "seq": 11,
                    "conflict_id": "conf_test_2",
                    "merged_payload": merged_payload,
                }
            ]
        }

    original = sync_agent._post_json
    sync_agent._post_json = fake_post_json
    try:
        res = sync_agent.push_outbox(conn, server_url="http://127.0.0.1:1", token="t")
    finally:
        sync_agent._post_json = original

    assert res["pushed"] == 1
    conflicts = conn.execute("SELECT * FROM local_conflicts").fetchall()
    assert len(conflicts) == 1, f"Perubahan isi nyata harus tetap mencatat konflik: {len(conflicts)}"
    conflict = dict(conflicts[0])
    assert conflict["reason"] == "MERGED"
    assert conflict["entity_uuid"] == ext_uuid
    outbox = conn.execute("SELECT * FROM sync_outbox").fetchall()
    assert len(outbox) == 0, "Baris outbox harus terhapus meski konflik dicatat"
    row = conn.execute("SELECT revision, title FROM extensions WHERE uuid = ?", (ext_uuid,)).fetchone()
    assert row["revision"] == 2
    assert row["title"] == "Judul Dari Server"
    print("OK: perubahan isi nyata tetap mencatat konflik dan menerapkan payload gabungan")


def main():
    home = Path(os.environ["ANTIDETECT_HOME"])
    store.DB_PATH = home / "antidetect.db"
    store.init()
    with store.db() as conn:
        test_mechanical_merge_no_conflict(conn)
        test_real_merge_still_conflicts(conn)
    print("\nSemua tes lulus: scripts/test-sync-merge-notice.py")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(WORK_DIR, ignore_errors=True)
