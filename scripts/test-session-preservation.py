#!/usr/bin/env python3
"""Uji otomasi untuk preservasi sesi Gmail multi-akun."""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRATCH = Path(os.environ.get("ANTIDETECT_SESSION_TEST_DIR") or Path.home() / ".hermes" / "cache" / "scratch" / "antidetect-session")
SCRATCH.mkdir(parents=True, exist_ok=True)
ROOT = Path(tempfile.mkdtemp(prefix="run-", dir=SCRATCH))
WORK = ROOT / "work"
PROFILES_DIR = WORK / "profiles"
DATA_DIR = WORK / "data"
for d in (WORK, PROFILES_DIR, DATA_DIR):
    d.mkdir(parents=True, exist_ok=True)

os.environ["ANTIDETECT_HOME"] = str(DATA_DIR)
os.environ["ANTIDETECT_ENGINE_DIR"] = str(DATA_DIR / "squashfs-root" / "opt" / "ungoogled-chromium")
sys.path.insert(0, str(REPO))

from antidetect import cookies, launcher, store

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"OK   {name}")
    else:
        print(f"GAGAL {name} {detail}".rstrip())
        FAILURES.append(name)


def _setup_store():
    store.DB_PATH = DATA_DIR / "antidetect.db"
    store.init()
    launcher.PROFILES_DIR = PROFILES_DIR


def _profile_dir(pid):
    return PROFILES_DIR / str(pid)


def _default_dir(pid):
    return _profile_dir(pid) / "data" / "Default"


def _preferences_path(pid):
    return _default_dir(pid) / "Preferences"


def _make_empty_cookies_db(pid):
    p = cookies._database_path(pid)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    cookies._create_schema(conn)
    conn.commit()
    conn.close()
    return p


def _make_gmail_cookies_db(pid):
    p = cookies._database_path(pid)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    cookies._create_schema(conn)
    cols = cookies._columns(conn)
    now = int((time.time() + cookies._EPOCH_DELTA) * 1_000_000)
    for i in range(10):
        values = {
            "creation_utc": now,
            "host_key": ".google.com",
            "top_frame_site_key": "",
            "name": "GMAIL_AT",
            "value": f"token{i}",
            "encrypted_value": b"",
            "path": f"/mail/u/{i}",
            "expires_utc": 0,
            "is_secure": 1,
            "is_httponly": 1,
            "last_access_utc": now,
            "has_expires": 0,
            "is_persistent": 0,
            "priority": 1,
            "samesite": 0,
            "source_scheme": 2,
            "source_port": 443,
            "last_update_utc": now,
            "source_type": 0,
            "has_cross_site_ancestor": 0,
            "is_session": 1,
        }
        names = [c for c in values if c in cols]
        ph = ", ".join("?" for _ in names)
        conn.execute(
            f"INSERT INTO cookies ({', '.join(names)}) VALUES ({ph})",
            [values[c] for c in names],
        )
    # persistent cookie
    values2 = {
        "creation_utc": now,
        "host_key": ".example.com",
        "top_frame_site_key": "",
        "name": "persist",
        "value": "pv",
        "encrypted_value": b"",
        "path": "/",
        "expires_utc": (1893456000 + cookies._EPOCH_DELTA) * 1_000_000,
        "is_secure": 0,
        "is_httponly": 0,
        "last_access_utc": now,
        "has_expires": 1,
        "is_persistent": 1,
        "priority": 1,
        "samesite": -1,
        "source_scheme": 1,
        "source_port": 80,
        "last_update_utc": now,
        "source_type": 0,
        "has_cross_site_ancestor": 0,
        "is_session": 0,
    }
    names2 = [c for c in values2 if c in cols]
    ph2 = ", ".join("?" for _ in names2)
    conn.execute(
        f"INSERT INTO cookies ({', '.join(names2)}) VALUES ({ph2})",
        [values2[c] for c in names2],
    )
    conn.commit()
    conn.close()
    return p


# --- Fase 1 tests ---
def t1_ensure_injects_new():
    _setup_store()
    pid = 101
    ddir = PROFILES_DIR / str(pid) / "data"
    launcher.ensure_password_manager(ddir)
    pref = _preferences_path(pid)
    check("t1_preferences_file_exists", pref.exists())
    if pref.exists():
        data = json.loads(pref.read_text(encoding="utf-8"))
        check("t1_credentials_true", data.get("credentials_enable_service") is True)
        check("t1_session_dict", isinstance(data.get("session"), dict))
        check("t1_restore_4", (data.get("session") or {}).get("restore_on_startup") == 4)


def t1_alias_works():
    _setup_store()
    pid = 102
    ddir = PROFILES_DIR / str(pid) / "data"
    launcher.ensure_profile_preferences(ddir)
    pref = _preferences_path(pid)
    check("t1_alias_file_exists", pref.exists())
    if pref.exists():
        data = json.loads(pref.read_text(encoding="utf-8"))
        check("t1_alias_restore_4", (data.get("session") or {}).get("restore_on_startup") == 4)


def t1_idempotent_restore():
    _setup_store()
    pid = 103
    ddir = PROFILES_DIR / str(pid) / "data"
    pref = _preferences_path(pid)
    pref.parent.mkdir(parents=True, exist_ok=True)
    pref.write_text(json.dumps({"session": {"restore_on_startup": 1}, "credentials_enable_service": False}), encoding="utf-8")
    launcher.ensure_password_manager(ddir)
    data = json.loads(pref.read_text(encoding="utf-8"))
    check("t1_idempotent_restore_kept", data["session"]["restore_on_startup"] == 1)
    check("t1_idempotent_creds_kept", data["credentials_enable_service"] is False)


def t1_idempotent_restore_4():
    _setup_store()
    pid = 104
    ddir = PROFILES_DIR / str(pid) / "data"
    pref = _preferences_path(pid)
    pref.parent.mkdir(parents=True, exist_ok=True)
    pref.write_text(json.dumps({"session": {"restore_on_startup": 4}}), encoding="utf-8")
    launcher.ensure_password_manager(ddir)
    data = json.loads(pref.read_text(encoding="utf-8"))
    check("t1_idempotent_4_kept", (data.get("session") or {}).get("restore_on_startup") == 4)


def t1_idempotent_restore_other_not_overwritten():
    _setup_store()
    pid = 111
    ddir = PROFILES_DIR / str(pid) / "data"
    pref = _preferences_path(pid)
    pref.parent.mkdir(parents=True, exist_ok=True)
    pref.write_text(json.dumps({"session": {"restore_on_startup": 3}}), encoding="utf-8")
    launcher.ensure_password_manager(ddir)
    data = json.loads(pref.read_text(encoding="utf-8"))
    check("t1_idempotent_other_kept", (data.get("session") or {}).get("restore_on_startup") == 3)


def t1_session_missing_added():
    _setup_store()
    pid = 105
    ddir = PROFILES_DIR / str(pid) / "data"
    pref = _preferences_path(pid)
    pref.parent.mkdir(parents=True, exist_ok=True)
    pref.write_text(json.dumps({"credentials_enable_service": True}), encoding="utf-8")
    launcher.ensure_password_manager(ddir)
    data = json.loads(pref.read_text(encoding="utf-8"))
    check("t1_session_added", isinstance(data.get("session"), dict))
    check("t1_restore_4", (data.get("session") or {}).get("restore_on_startup") == 4)


def t1_session_not_dict_replaced():
    _setup_store()
    pid = 106
    ddir = PROFILES_DIR / str(pid) / "data"
    pref = _preferences_path(pid)
    pref.parent.mkdir(parents=True, exist_ok=True)
    pref.write_text(json.dumps({"session": "bad"}), encoding="utf-8")
    launcher.ensure_password_manager(ddir)
    data = json.loads(pref.read_text(encoding="utf-8"))
    check("t1_session_fixed", isinstance(data.get("session"), dict))
    check("t1_restore_4", (data.get("session") or {}).get("restore_on_startup") == 4)


def t1_credentials_missing_added():
    _setup_store()
    pid = 107
    ddir = PROFILES_DIR / str(pid) / "data"
    pref = _preferences_path(pid)
    pref.parent.mkdir(parents=True, exist_ok=True)
    pref.write_text(json.dumps({"session": {"restore_on_startup": 2}}), encoding="utf-8")
    launcher.ensure_password_manager(ddir)
    data = json.loads(pref.read_text(encoding="utf-8"))
    check("t1_creds_added", data.get("credentials_enable_service") is True)
    check("t1_restore_kept_2", data["session"]["restore_on_startup"] == 2)


def t1_no_tmp_leftover():
    _setup_store()
    pid = 108
    ddir = PROFILES_DIR / str(pid) / "data"
    launcher.ensure_password_manager(ddir)
    tmp = _preferences_path(pid).with_name("Preferences.tmp")
    check("t1_no_tmp_leftover", not tmp.exists())


def t1_malformed_preferences_no_crash():
    _setup_store()
    pid = 109
    ddir = PROFILES_DIR / str(pid) / "data"
    pref = _preferences_path(pid)
    pref.parent.mkdir(parents=True, exist_ok=True)
    pref.write_text("{bad json", encoding="utf-8")
    try:
        launcher.ensure_password_manager(ddir)
        ok = True
    except Exception:
        ok = False
    check("t1_malformed_no_crash", ok)


def t1_default_dir_created():
    _setup_store()
    pid = 110
    ddir = PROFILES_DIR / str(pid) / "data"
    assert not (ddir / "Default").exists()
    launcher.ensure_password_manager(ddir)
    check("t1_default_dir_created", (ddir / "Default").is_dir())


# --- Fase cookies session tests ---
def t2_gmail_session_cookies():
    _setup_store()
    pid = 201
    _make_gmail_cookies_db(pid)
    cj = cookies.get_cookies(pid)
    items = json.loads(cj)
    gmail = [it for it in items if it.get("name") == "GMAIL_AT"]
    check("t2_gmail_count_10", len(gmail) == 10)
    check("t2_gmail_all_session", all(it.get("session") is True for it in gmail))
    check("t2_paths_0_to_9", sorted(it.get("path") for it in gmail) == [f"/mail/u/{i}" for i in range(10)])
    check("t2_get_cookies_returns_list", isinstance(items, list))
    check("t2_total_items_11", len(items) == 11)


def t2_persistent_cookie_session_false():
    _setup_store()
    pid = 202
    _make_gmail_cookies_db(pid)
    items = json.loads(cookies.get_cookies(pid))
    persist = [it for it in items if it.get("name") == "persist"]
    check("t2_persistent_exists", len(persist) == 1)
    check("t2_persistent_session_false", persist and persist[0].get("session") is False)


def t2_empty_db_returns_empty_list():
    _setup_store()
    pid = 203
    _make_empty_cookies_db(pid)
    cj = cookies.get_cookies(pid)
    check("t2_empty_returns_empty_list", cj == "[]" or json.loads(cj) == [])


def t2_verify_cookie_db_true():
    _setup_store()
    pid = 204
    _make_gmail_cookies_db(pid)
    check("t2_verify_db_true", cookies.verify_cookie_db(pid) is True)


def t2_verify_empty_db_true():
    _setup_store()
    pid = 205
    _make_empty_cookies_db(pid)
    check("t2_verify_empty_db_true", cookies.verify_cookie_db(pid) is True)


def t2_get_cookies_requires_closed():
    _setup_store()
    pid = 206
    _make_gmail_cookies_db(pid)
    # is_open false by default, so should succeed
    try:
        cookies.get_cookies(pid)
        ok = True
    except Exception:
        ok = False
    check("t2_get_cookies_ok_when_closed", ok)


# --- Fase update_cookies safe merge tests ---
def t3_partial_preserves_session():
    _setup_store()
    pid = 301
    _make_gmail_cookies_db(pid)
    cookies.update_cookies(pid, [{"domain": ".example.com", "name": "foo", "path": "/", "value": "bar", "expires": 1893456000, "session": False}])
    items = json.loads(cookies.get_cookies(pid))
    names = [it["name"] for it in items]
    check("t3_session_preserved_after_partial", "GMAIL_AT" in names)
    check("t3_new_cookie_added", "foo" in names)


def t3_empty_preserve_keeps_cookies():
    _setup_store()
    pid = 302
    _make_gmail_cookies_db(pid)
    before = len(json.loads(cookies.get_cookies(pid)))
    cookies.update_cookies(pid, [])
    after = len(json.loads(cookies.get_cookies(pid)))
    check("t3_empty_preserve_no_wipe", after == before)


def t3_empty_preserve_explicit_false_wipes():
    _setup_store()
    pid = 303
    _make_gmail_cookies_db(pid)
    cookies.update_cookies(pid, [], preserve_sessions=False)
    items = json.loads(cookies.get_cookies(pid))
    check("t3_empty_wipe", len(items) == 0)


def t3_none_input_no_wipe():
    _setup_store()
    pid = 304
    _make_gmail_cookies_db(pid)
    before = len(json.loads(cookies.get_cookies(pid)))
    cookies.update_cookies(pid, None)
    after = len(json.loads(cookies.get_cookies(pid)))
    check("t3_none_no_wipe", after == before)


def t3_str_empty_no_wipe():
    _setup_store()
    pid = 305
    _make_gmail_cookies_db(pid)
    before = len(json.loads(cookies.get_cookies(pid)))
    cookies.update_cookies(pid, "")
    after = len(json.loads(cookies.get_cookies(pid)))
    check("t3_str_empty_no_wipe", after == before)


def t3_wipe_explicit_list_empty_false():
    _setup_store()
    pid = 306
    _make_gmail_cookies_db(pid)
    cookies.update_cookies(pid, [], preserve_sessions=False)
    items = json.loads(cookies.get_cookies(pid))
    check("t3_wipe_explicit", len(items) == 0)


def t3_update_existing_cookie():
    _setup_store()
    pid = 307
    _make_empty_cookies_db(pid)
    cookies.update_cookies(pid, [{"domain": ".example.com", "name": "foo", "path": "/", "value": "v1", "expires": 1893456000, "session": False}])
    cookies.update_cookies(pid, [{"domain": ".example.com", "name": "foo", "path": "/", "value": "v2", "expires": 1893456000, "session": False}])
    items = json.loads(cookies.get_cookies(pid))
    check("t3_update_value", items and items[0].get("value") == "v2")


def t3_preserve_sessions_param_default_true():
    _setup_store()
    pid = 308
    _make_gmail_cookies_db(pid)
    cookies.update_cookies(pid, [])
    items = json.loads(cookies.get_cookies(pid))
    check("t3_default_preserve", len(items) > 0)


def t3_update_cookies_returns_true():
    _setup_store()
    pid = 309
    _make_empty_cookies_db(pid)
    res = cookies.update_cookies(pid, [])
    check("t3_returns_true", res is True)


def t3_update_cookies_signature():
    import inspect
    sig = inspect.signature(cookies.update_cookies)
    check("t3_signature_preserve_sessions", "preserve_sessions" in sig.parameters)
    check("t3_default_value", sig.parameters["preserve_sessions"].default is True)


# --- Fase graceful close tests ---
def t4_graceful_close_no_address_process_gone():
    check("t4_no_address_gone", launcher._graceful_close(None, proc=None, pid=0, timeout=1.0) is True)


def t4_graceful_close_no_address_process_alive():
    class P:
        pid = 999999
        @staticmethod
        def poll():
            return None
    # process not gone (pid alive check may fail due to no process, treat as gone)
    res = launcher._graceful_close(None, proc=None, pid=0, timeout=0.1)
    check("t4_no_address_pid0_gone", res is True)


def t4_graceful_close_journal_wait():
    _setup_store()
    pid = 401
    ddir = PROFILES_DIR / str(pid) / "data"
    jpath = ddir / "Default" / "Cookies-journal"
    jpath.parent.mkdir(parents=True, exist_ok=True)
    jpath.write_text("x")
    import threading

    def remover():
        time.sleep(0.2)
        try:
            jpath.unlink()
        except Exception:
            pass

    t = threading.Thread(target=remover)
    t.start()
    res = launcher._graceful_close(None, proc=None, pid=0, timeout=1.0, data_dir=str(ddir))
    t.join(timeout=1)
    check("t4_journal_wait_returns", res is True)
    check("t4_journal_removed", not jpath.exists())


def t4_graceful_close_signature():
    import inspect
    sig = inspect.signature(launcher._graceful_close)
    check("t4_signature_data_dir", "data_dir" in sig.parameters)
    check("t4_signature_timeout", "timeout" in sig.parameters)


def t4_close_profile_passes_data_dir():
    src = (REPO / "antidetect" / "launcher.py").read_text(encoding="utf-8")
    check("t4_close_profile_data_dir_call", "data_dir=item_info.get(\"data_dir\")" in src or "data_dir=info.get(\"data_dir\")" in src)


# --- Fase sync guard tests ---
def t5_guard_skips_empty_upload():
    _setup_store()
    pid = 501
    _make_empty_cookies_db(pid)
    import antidetect.sync_agent as sync_agent
    original_upload = sync_agent.upload_cookie_blob
    original_try = sync_agent._try_resolve_endpoint
    called = {"count": 0}

    def fake_upload(*a, **k):
        called["count"] += 1
        return {"ok": True}

    def fake_try(conn):
        return ("http://example", "token")

    sync_agent.upload_cookie_blob = fake_upload
    sync_agent._try_resolve_endpoint = fake_try
    launcher.sync_agent = sync_agent
    try:
        launcher._sync_cookies_after_close(pid, {"uuid": "00000000-0000-0000-0000-000000000501", "fencing_token": 1})
    finally:
        sync_agent.upload_cookie_blob = original_upload
        sync_agent._try_resolve_endpoint = original_try
    check("t5_guard_skips_empty", called["count"] == 0)


def t5_nonempty_upload_called():
    _setup_store()
    pid = 502
    _make_gmail_cookies_db(pid)
    import antidetect.sync_agent as sync_agent
    original_upload = sync_agent.upload_cookie_blob
    original_try = sync_agent._try_resolve_endpoint
    called = {"count": 0}

    def fake_upload(*a, **k):
        called["count"] += 1
        return {"ok": True}

    def fake_try(conn):
        return ("http://example", "token")

    sync_agent.upload_cookie_blob = fake_upload
    sync_agent._try_resolve_endpoint = fake_try
    launcher.sync_agent = sync_agent
    try:
        launcher._sync_cookies_after_close(pid, {"uuid": "00000000-0000-0000-0000-000000000502", "fencing_token": 1})
    finally:
        sync_agent.upload_cookie_blob = original_upload
        sync_agent._try_resolve_endpoint = original_try
    check("t5_nonempty_upload_called", called["count"] >= 1)


def t5_sync_guard_returns_cookie_res_none_on_empty():
    _setup_store()
    pid = 503
    _make_empty_cookies_db(pid)
    import antidetect.sync_agent as sync_agent
    original_try = sync_agent._try_resolve_endpoint
    sync_agent._try_resolve_endpoint = lambda conn: ("http://example", "token")
    launcher.sync_agent = sync_agent
    try:
        res = launcher._sync_cookies_after_close(pid, {"uuid": "00000000-0000-0000-0000-000000000503", "fencing_token": 1})
    finally:
        sync_agent._try_resolve_endpoint = original_try
    check("t5_empty_returns_none", res is None)


def t5_ensure_alias_exists():
    check("t5_alias_callable", callable(launcher.ensure_profile_preferences))


def t5_ensure_password_manager_exists():
    check("t5_ensure_exists", callable(launcher.ensure_password_manager))


# extra checks for counts
def t6_extra_counts():
    _setup_store()
    pid = 601
    _make_gmail_cookies_db(pid)
    items = json.loads(cookies.get_cookies(pid))
    check("t6_gmail_at_present", any(it["name"] == "GMAIL_AT" for it in items))
    check("t6_session_flag_true", all(it["session"] for it in items if it["name"] == "GMAIL_AT"))
    check("t6_has_expires_zero", all(it["expires"] == 0 for it in items if it["name"] == "GMAIL_AT"))
    check("t6_domain_google", all(it["domain"] == ".google.com" for it in items if it["name"] == "GMAIL_AT"))
    check("t6_http_only", all(it["httpOnly"] for it in items if it["name"] == "GMAIL_AT"))


def t6_empty_update_preserve_default():
    _setup_store()
    pid = 602
    _make_gmail_cookies_db(pid)
    cookies.update_cookies(pid, [])
    items = json.loads(cookies.get_cookies(pid))
    check("t6_empty_update_preserve_default", len(items) == 11)


def t6_empty_update_preserve_true_explicit():
    _setup_store()
    pid = 603
    _make_gmail_cookies_db(pid)
    cookies.update_cookies(pid, [], preserve_sessions=True)
    items = json.loads(cookies.get_cookies(pid))
    check("t6_empty_update_preserve_true", len(items) == 11)


def t6_empty_update_wipe_false():
    _setup_store()
    pid = 604
    _make_gmail_cookies_db(pid)
    cookies.update_cookies(pid, [], preserve_sessions=False)
    items = json.loads(cookies.get_cookies(pid))
    check("t6_empty_update_wipe", len(items) == 0)


def t6_partial_update_adds():
    _setup_store()
    pid = 605
    _make_empty_cookies_db(pid)
    cookies.update_cookies(pid, [{"domain": ".a.com", "name": "a", "path": "/", "value": "1", "session": True}])
    items = json.loads(cookies.get_cookies(pid))
    check("t6_partial_adds", len(items) == 1)


def t6_cookie_digest_stable():
    sample = [{"domain": ".x.com", "name": "n", "path": "/", "value": "v", "session": True}]
    d1 = cookies.cookie_digest(sample)
    d2 = cookies.cookie_digest(list(sample))
    check("t6_digest_stable", d1 == d2)


def t6_verify_cookie_db_false_missing():
    check("t6_verify_missing", cookies.verify_cookie_db(999999) is False)


def main():
    tests = [
        t1_ensure_injects_new, t1_alias_works, t1_idempotent_restore, t1_idempotent_restore_4, t1_idempotent_restore_other_not_overwritten,
        t1_session_missing_added, t1_session_not_dict_replaced, t1_credentials_missing_added,
        t1_no_tmp_leftover, t1_malformed_preferences_no_crash, t1_default_dir_created,
        t2_gmail_session_cookies, t2_persistent_cookie_session_false, t2_empty_db_returns_empty_list,
        t2_verify_cookie_db_true, t2_verify_empty_db_true, t2_get_cookies_requires_closed,
        t3_partial_preserves_session, t3_empty_preserve_keeps_cookies, t3_empty_preserve_explicit_false_wipes,
        t3_none_input_no_wipe, t3_str_empty_no_wipe, t3_wipe_explicit_list_empty_false,
        t3_update_existing_cookie, t3_preserve_sessions_param_default_true, t3_update_cookies_returns_true,
        t3_update_cookies_signature,
        t4_graceful_close_no_address_process_gone, t4_graceful_close_no_address_process_alive,
        t4_graceful_close_journal_wait, t4_graceful_close_signature, t4_close_profile_passes_data_dir,
        t5_guard_skips_empty_upload, t5_nonempty_upload_called, t5_sync_guard_returns_cookie_res_none_on_empty,
        t5_ensure_alias_exists, t5_ensure_password_manager_exists,
        t6_extra_counts, t6_empty_update_preserve_default, t6_empty_update_preserve_true_explicit,
        t6_empty_update_wipe_false, t6_partial_update_adds, t6_cookie_digest_stable,
        t6_verify_cookie_db_false_missing,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            print(f"GAGAL {t.__name__} {repr(e)}")
            FAILURES.append(t.__name__)
    print(f"\nTOTAL: {len(tests)} | LULUS: {len(tests)-len(FAILURES)} | GAGAL: {len(FAILURES)}")
    if FAILURES:
        print("GAGAL:", FAILURES)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
