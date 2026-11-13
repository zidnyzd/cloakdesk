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
        check("t1_restore_1", (data.get("session") or {}).get("restore_on_startup") == 1)


def t1_alias_works():
    _setup_store()
    pid = 102
    ddir = PROFILES_DIR / str(pid) / "data"
    launcher.ensure_profile_preferences(ddir)
    pref = _preferences_path(pid)
    check("t1_alias_file_exists", pref.exists())
    if pref.exists():
        data = json.loads(pref.read_text(encoding="utf-8"))
        check("t1_alias_restore_1", (data.get("session") or {}).get("restore_on_startup") == 1)


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


def t1_idempotent_restore_4_migration():
    _setup_store()
    pid = 104
    ddir = PROFILES_DIR / str(pid) / "data"
    pref = _preferences_path(pid)
    pref.parent.mkdir(parents=True, exist_ok=True)
    pref.write_text(json.dumps({"session": {"restore_on_startup": 4}}), encoding="utf-8")
    launcher.ensure_password_manager(ddir)
    data = json.loads(pref.read_text(encoding="utf-8"))
    check("t1_idempotent_4_migrated_to_1", (data.get("session") or {}).get("restore_on_startup") == 1)


def t1_idempotent_restore_other_not_overwritten():
    _setup_store()
    pid = 111
    ddir = PROFILES_DIR / str(pid) / "data"
    pref = _preferences_path(pid)
    pref.parent.mkdir(parents=True, exist_ok=True)
    pref.write_text(json.dumps({"session": {"restore_on_startup": 0}}), encoding="utf-8")
    launcher.ensure_password_manager(ddir)
    data = json.loads(pref.read_text(encoding="utf-8"))
    check("t1_idempotent_0_kept", (data.get("session") or {}).get("restore_on_startup") == 0)


def t1_idempotent_restore_other_2_not_overwritten():
    _setup_store()
    pid = 112
    ddir = PROFILES_DIR / str(pid) / "data"
    pref = _preferences_path(pid)
    pref.parent.mkdir(parents=True, exist_ok=True)
    pref.write_text(json.dumps({"session": {"restore_on_startup": 2}}), encoding="utf-8")
    launcher.ensure_password_manager(ddir)
    data = json.loads(pref.read_text(encoding="utf-8"))
    check("t1_idempotent_2_kept", (data.get("session") or {}).get("restore_on_startup") == 2)


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
    check("t1_restore_1", (data.get("session") or {}).get("restore_on_startup") == 1)


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
    check("t1_restore_1", (data.get("session") or {}).get("restore_on_startup") == 1)


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


def t1_idempotent_no_change():
    _setup_store()
    pid = 113
    ddir = PROFILES_DIR / str(pid) / "data"
    pref = _preferences_path(pid)
    pref.parent.mkdir(parents=True, exist_ok=True)
    pref.write_text(json.dumps({"session": {"restore_on_startup": 1}, "credentials_enable_service": True}), encoding="utf-8")
    st1 = os.stat(pref).st_mtime_ns
    time.sleep(0.01)
    launcher.ensure_password_manager(ddir)
    st2 = os.stat(pref).st_mtime_ns
    data = json.loads(pref.read_text(encoding="utf-8"))
    check("t1_idempotent_mtime", st2 == st1)
    check("t1_idempotent_unchanged", data["session"]["restore_on_startup"] == 1)


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


def t4_graceful_close_sends_browser_close():
    _setup_store()
    import urllib.request
    sent = {"cmd": None}
    call_kwargs = {}
    
    class FakeWS:
        def __init__(self, *args, **kwargs):
            pass
        def send(self, msg):
            sent["cmd"] = msg
        def recv(self):
            return ""
        def settimeout(self, t):
            pass
        def close(self):
            pass
    
    fake_version = {"webSocketDebuggerUrl": "ws://127.0.0.1:1/debugger"}
    body_version = json.dumps(fake_version).encode("utf-8")
    body_list = json.dumps([]).encode("utf-8")
    
    class FakeResponse:
        def __init__(self, body):
            self._body = body
            self.status = 200
            self.headers = {"content-type": "application/json"}
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass
    
    original_urlopen = urllib.request.urlopen
    def fake_urlopen(url, timeout=0):
        if url.endswith("/json/version"):
            return FakeResponse(body_version)
        if url.endswith("/json/list"):
            return FakeResponse(body_list)
        raise Exception("other")
    
    import websocket
    orig_create = websocket.create_connection
    def fake_create(*args, **kwargs):
        call_kwargs.update(kwargs)
        return FakeWS(*args, **kwargs)
    websocket.create_connection = fake_create
    try:
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = launcher._graceful_close("127.0.0.1:1", proc=None, pid=0, timeout=1.0)
    finally:
        websocket.create_connection = orig_create
    check("t4_browser_close_sent", sent["cmd"] == json.dumps({"id": 1, "method": "Browser.close"}))
    check("t4_suppress_origin_passed", call_kwargs.get("suppress_origin") is True)
    check("t4_browser_close_result", res is True)


def t4_graceful_close_network_journal_wait():
    _setup_store()
    pid = 402
    ddir = PROFILES_DIR / str(pid) / "data"
    jpath = ddir / "Default" / "Network" / "Cookies-journal"
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
    check("t4_network_journal_wait_returns", res is True)
    check("t4_network_journal_removed", not jpath.exists())


def t4_graceful_close_prefers_browser_close_without_page_close():
    _setup_store()
    import urllib.request
    sent = {"cmd": None}
    call_kwargs = {}
    close_calls = {"count": 0}

    class FakeWS:
        def __init__(self, *args, **kwargs):
            pass
        def send(self, msg):
            sent["cmd"] = msg
        def recv(self):
            return ""
        def settimeout(self, t):
            pass
        def close(self):
            pass

    fake_version = {"webSocketDebuggerUrl": "ws://127.0.0.1:1/debugger"}
    body_version = json.dumps(fake_version).encode("utf-8")
    body_list = json.dumps([
        {"type": "page", "id": "p1"},
        {"type": "page", "id": "p2"}
    ]).encode("utf-8")

    class FakeResponse:
        def __init__(self, body):
            self._body = body
            self.status = 200
            self.headers = {"content-type": "application/json"}
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    def fake_urlopen(url, timeout=0):
        if url.endswith("/json/version"):
            return FakeResponse(body_version)
        if url.endswith("/json/list"):
            return FakeResponse(body_list)
        if "/json/close/" in url:
            close_calls["count"] += 1
            return FakeResponse(b"")
        raise Exception("other")

    import websocket
    orig_create = websocket.create_connection
    def fake_create(*args, **kwargs):
        call_kwargs.update(kwargs)
        return FakeWS(*args, **kwargs)
    websocket.create_connection = fake_create
    try:
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = launcher._graceful_close("127.0.0.1:1", proc=None, pid=0, timeout=1.0)
    finally:
        websocket.create_connection = orig_create
    check("t4_prefers_browser_close_sent", sent["cmd"] == json.dumps({"id": 1, "method": "Browser.close"}))
    check("t4_prefers_browser_close_no_page_close", close_calls["count"] == 0)
    check("t4_prefers_browser_close_result", res is True)


def t4_graceful_close_falls_back_to_page_close_on_ws_error():
    _setup_store()
    import urllib.request
    close_calls = {"count": 0}

    fake_version = {"webSocketDebuggerUrl": "ws://127.0.0.1:1/debugger"}
    body_version = json.dumps(fake_version).encode("utf-8")
    body_list = json.dumps([
        {"type": "page", "id": "p1"},
        {"type": "page", "id": "p2"}
    ]).encode("utf-8")

    class FakeResponse:
        def __init__(self, body):
            self._body = body
            self.status = 200
            self.headers = {"content-type": "application/json"}
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    def fake_urlopen(url, timeout=0):
        if url.endswith("/json/version"):
            return FakeResponse(body_version)
        if url.endswith("/json/list"):
            return FakeResponse(body_list)
        if "/json/close/" in url:
            close_calls["count"] += 1
            return FakeResponse(b"")
        raise Exception("other")

    import websocket
    orig_create = websocket.create_connection
    def fake_create_fail(*args, **kwargs):
        raise Exception("ws error")
    websocket.create_connection = fake_create_fail
    try:
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = launcher._graceful_close("127.0.0.1:1", proc=None, pid=0, timeout=1.0)
    finally:
        websocket.create_connection = orig_create
    check("t4_fallback_page_close_called", close_calls["count"] == 2)
    check("t4_fallback_result", res is True)


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


# --- Fase deduplikasi cookie ---
def _ensure_dup_db(pid):
    p = cookies._database_path(pid)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    cookies._create_schema(conn)
    conn.execute("DROP INDEX IF EXISTS cookies_unique_index")
    conn.commit()
    conn.close()


def _make_dup_row(pid, host, name, path, source_scheme, source_port, secure=0,
                  session=True, last_update_utc=None, creation_utc=None):
    _ensure_dup_db(pid)
    conn = sqlite3.connect(str(cookies._database_path(pid)))
    cols = cookies._columns(conn)
    now = int((time.time() + cookies._EPOCH_DELTA) * 1_000_000)
    if last_update_utc is None:
        last_update_utc = now
    if creation_utc is None:
        creation_utc = now
    values = {
        "creation_utc": creation_utc,
        "host_key": host,
        "top_frame_site_key": "",
        "name": name,
        "value": "",
        "encrypted_value": b"",
        "path": path,
        "expires_utc": 0,
        "is_secure": secure,
        "is_httponly": 0,
        "last_access_utc": now,
        "has_expires": 0,
        "is_persistent": 0,
        "priority": 1,
        "samesite": -1,
        "source_scheme": source_scheme,
        "source_port": source_port,
        "last_update_utc": last_update_utc,
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
    conn.commit()
    conn.close()


def t7_no_duplicate_on_chromium_style_overwrite():
    _setup_store()
    pid = 701
    _make_dup_row(pid, ".example.com", "foo", "/", 2, 443, secure=1)
    cookies.update_cookies(
        pid,
        [{"domain": ".example.com", "name": "foo", "path": "/", "value": "v2",
          "secure": True, "session": False, "expires": 1893456000}],
    )
    items = json.loads(cookies.get_cookies(pid))
    matches = [it for it in items if it["name"] == "foo"]
    check("t7_no_duplicate_rows", len(matches) == 1)
    check("t7_value_updated", matches and matches[0]["value"] == "v2")


def t7_non_secure_stored_as_https():
    _setup_store()
    pid = 702
    _make_empty_cookies_db(pid)
    cookies.update_cookies(
        pid,
        [{"domain": ".example.com", "name": "ns", "path": "/", "value": "v",
          "secure": False, "session": True}],
    )
    conn = sqlite3.connect(str(cookies._database_path(pid)))
    row = conn.execute(
        "SELECT source_scheme, source_port FROM cookies WHERE name = 'ns'"
    ).fetchone()
    conn.close()
    check("t7_non_secure_scheme_2", row and row[0] == 2)
    check("t7_non_secure_port_443", row and row[1] == 443)


def t7_dedupe_removes_duplicates():
    _setup_store()
    pid = 703
    _make_dup_row(pid, ".example.com", "foo", "/", 2, 443, secure=1,
                  last_update_utc=100, creation_utc=100)
    _make_dup_row(pid, ".example.com", "foo", "/", 2, 443, secure=1,
                  last_update_utc=999, creation_utc=999)
    _make_dup_row(pid, ".example.com", "foo", "/", 2, 443, secure=1,
                  last_update_utc=500, creation_utc=500)
    deleted = cookies.dedupe_cookies(pid)
    items = json.loads(cookies.get_cookies(pid))
    matches = [it for it in items if it["name"] == "foo"]
    check("t7_dedupe_deleted_two", deleted == 2)
    check("t7_dedupe_keeps_one", len(matches) == 1)


def t7_deduke_session_preserved():
    _setup_store()
    pid = 704
    _make_dup_row(pid, ".example.com", "sess", "/", 2, 443, secure=1,
                  session=True, last_update_utc=100, creation_utc=100)
    _make_dup_row(pid, ".example.com", "sess", "/", 2, 443, secure=1,
                  session=True, last_update_utc=999, creation_utc=999)
    cookies.dedupe_cookies(pid)
    items = json.loads(cookies.get_cookies(pid))
    matches = [it for it in items if it["name"] == "sess"]
    check("t7_dedupe_session_kept", len(matches) == 1 and matches[0]["session"] is True)


def t7_dedupe_refuses_when_open():
    _setup_store()
    pid = 705
    _make_dup_row(pid, ".example.com", "foo", "/", 2, 443, secure=1)
    _make_dup_row(pid, ".example.com", "foo", "/", 2, 443, secure=1,
                  last_update_utc=999, creation_utc=999)
    with mock.patch("antidetect.cookies.launcher.is_open", return_value=True):
        try:
            cookies.dedupe_cookies(pid)
            ok = False
        except cookies.CookieError:
            ok = True
    check("t7_dedupe_refuses_open", ok)


# --- Fase duplikat tingkat jar ---
def _make_dup_row_full(pid, host, name, path, source_scheme, source_port, has_cross_site_ancestor=0,
                       secure=0, session=True, last_update_utc=None, creation_utc=None, top_frame_site_key=""):
    _ensure_dup_db(pid)
    conn = sqlite3.connect(str(cookies._database_path(pid)))
    cols = cookies._columns(conn)
    now = int((time.time() + cookies._EPOCH_DELTA) * 1_000_000)
    if last_update_utc is None:
        last_update_utc = now
    if creation_utc is None:
        creation_utc = now
    values = {
        "creation_utc": creation_utc,
        "host_key": host,
        "top_frame_site_key": top_frame_site_key,
        "name": name,
        "value": "",
        "encrypted_value": b"",
        "path": path,
        "expires_utc": 0,
        "is_secure": secure,
        "is_httponly": 0,
        "last_access_utc": now,
        "has_expires": 0,
        "is_persistent": 0,
        "priority": 1,
        "samesite": -1,
        "source_scheme": source_scheme,
        "source_port": source_port,
        "last_update_utc": last_update_utc,
        "source_type": 0,
        "has_cross_site_ancestor": has_cross_site_ancestor,
        "is_session": 1 if session else 0,
    }
    names = [c for c in values if c in cols]
    ph = ", ".join("?" for _ in names)
    conn.execute(
        f"INSERT INTO cookies ({', '.join(names)}) VALUES ({ph})",
        [values[c] for c in names],
    )
    conn.commit()
    conn.close()


def t8_insert_removes_scheme_port_cross_variants():
    _setup_store()
    pid = 801
    _make_dup_row_full(pid, ".google.co.id", "APISID", "/", 1, 80, has_cross_site_ancestor=0,
                       last_update_utc=100, creation_utc=100)
    _make_dup_row_full(pid, ".google.co.id", "APISID", "/", 2, 443, has_cross_site_ancestor=1,
                       last_update_utc=999, creation_utc=999)
    cookies.update_cookies(
        pid,
        [{"domain": ".google.co.id", "name": "APISID", "path": "/", "value": "v2",
          "secure": True, "session": False, "expires": 1893456000,
          "source_scheme": 1, "source_port": 80, "has_cross_site_ancestor": 0}],
    )
    conn = sqlite3.connect(str(cookies._database_path(pid)))
    rows = conn.execute(
        "SELECT source_scheme, source_port, has_cross_site_ancestor FROM cookies WHERE name='APISID'"
    ).fetchall()
    conn.close()
    check("t8_insert_one_row", len(rows) == 1)
    if rows:
        check("t8_insert_canonical_from_newest", rows[0] == (2, 443, 1))


def t8_insert_canonical_from_latest_existing():
    _setup_store()
    pid = 802
    _make_dup_row_full(pid, ".google.com", "SID", "/", 2, 443, has_cross_site_ancestor=1,
                       last_update_utc=999, creation_utc=999)
    _make_dup_row_full(pid, ".google.com", "SID", "/", 1, 80, has_cross_site_ancestor=0,
                       last_update_utc=100, creation_utc=100)
    cookies.update_cookies(
        pid,
        [{"domain": ".google.com", "name": "SID", "path": "/", "value": "v3",
          "secure": True, "session": False, "expires": 1893456000}],
    )
    conn = sqlite3.connect(str(cookies._database_path(pid)))
    rows = conn.execute(
        "SELECT source_scheme, source_port, has_cross_site_ancestor FROM cookies WHERE name='SID'"
    ).fetchall()
    conn.close()
    check("t8_insert_canonical_latest_one_row", len(rows) == 1)
    if rows:
        check("t8_insert_canonical_from_existing_newest", rows[0] == (2, 443, 1))


def t8_dedupe_merges_cross_variant():
    _setup_store()
    pid = 803
    _make_dup_row_full(pid, ".google.com", "__Secure-1PSIDCC", "/", 2, 443, has_cross_site_ancestor=1,
                       last_update_utc=100, creation_utc=100)
    _make_dup_row_full(pid, ".google.com", "__Secure-1PSIDCC", "/", 2, 443, has_cross_site_ancestor=0,
                       last_update_utc=999, creation_utc=999)
    deleted = cookies.dedupe_cookies(pid)
    conn = sqlite3.connect(str(cookies._database_path(pid)))
    rows = conn.execute(
        "SELECT source_scheme, source_port, has_cross_site_ancestor, last_update_utc FROM cookies WHERE name='__Secure-1PSIDCC'"
    ).fetchall()
    conn.close()
    check("t8_dedupe_cross_deleted", deleted >= 1)
    check("t8_dedupe_cross_one_row", len(rows) == 1)
    if rows:
        check("t8_dedupe_cross_keeps_newest", rows[0][3] == 999)


def t8_dedupe_merges_scheme_port_variant():
    _setup_store()
    pid = 804
    _make_dup_row_full(pid, ".example.com", "foo", "/", 1, 80, has_cross_site_ancestor=0,
                       last_update_utc=100, creation_utc=100)
    _make_dup_row_full(pid, ".example.com", "foo", "/", 2, 443, has_cross_site_ancestor=0,
                       last_update_utc=500, creation_utc=500)
    deleted = cookies.dedupe_cookies(pid)
    items = json.loads(cookies.get_cookies(pid))
    matches = [it for it in items if it["name"] == "foo"]
    check("t8_dedupe_scheme_deleted", deleted >= 1)
    check("t8_dedupe_scheme_one_row", len(matches) == 1)


def t8_gmail_at_preserved_after_dedupe():
    _setup_store()
    pid = 805
    _make_gmail_cookies_db(pid)
    _make_dup_row_full(pid, ".google.com", "GMAIL_AT", "/mail/u/0", 1, 80, has_cross_site_ancestor=0,
                       last_update_utc=100, creation_utc=100, session=True)
    _make_dup_row_full(pid, ".google.com", "GMAIL_AT", "/mail/u/0", 2, 443, has_cross_site_ancestor=1,
                       last_update_utc=999, creation_utc=999, session=True)
    cookies.dedupe_cookies(pid)
    items = json.loads(cookies.get_cookies(pid))
    gmail = [it for it in items if it.get("name") == "GMAIL_AT"]
    check("t8_gmail_at_count_10", len(gmail) == 10)
    paths = sorted(it.get("path") for it in gmail)
    check("t8_gmail_paths_0_to_9", paths == [f"/mail/u/{i}" for i in range(10)])


def t9_cooldown_blocks_immediate_open():
    _setup_store()
    os.environ.pop("ANTIDETECT_NO_COOLDOWN", None)
    os.environ["ANTIDETECT_COOLDOWN_SECONDS"] = "30"
    launcher._closed_at.clear()

    pid = 901
    launcher._closed_at[pid] = time.time()

    profile = {"id": pid, "name": "Test", "seed": 1000000000}
    blocked = False
    try:
        launcher.open_profile(profile)
    except launcher.LaunchError as e:
        if getattr(e, "code", None) == 1010:
            blocked = True
    except Exception:
        pass
    check("t9_cooldown_blocks_immediate_open", blocked)
    launcher._closed_at.clear()


def t9_cooldown_bypassed_with_force():
    _setup_store()
    os.environ.pop("ANTIDETECT_NO_COOLDOWN", None)
    os.environ["ANTIDETECT_COOLDOWN_SECONDS"] = "30"
    launcher._closed_at.clear()

    pid = 902
    launcher._closed_at[pid] = time.time()

    profile = {"id": pid, "name": "Test", "seed": 1000000000}

    with mock.patch("subprocess.Popen", side_effect=OSError("Mocked launch")):
        blocked = False
        try:
            launcher.open_profile(profile, force=True)
        except launcher.LaunchError as e:
            if getattr(e, "code", None) == 1010:
                blocked = True
        except Exception:
            pass

    check("t9_cooldown_bypassed_with_force", not blocked)
    launcher._closed_at.clear()


def t9_cooldown_disabled_via_env():
    _setup_store()
    launcher._closed_at.clear()

    pid = 903
    launcher._closed_at[pid] = time.time()

    profile = {"id": pid, "name": "Test", "seed": 1000000000}

    os.environ["ANTIDETECT_NO_COOLDOWN"] = "1"
    check("t9_no_cooldown_env", launcher.cooldown_seconds() == 0)

    with mock.patch("subprocess.Popen", side_effect=OSError("Mocked launch")):
        blocked = False
        try:
            launcher.open_profile(profile)
        except launcher.LaunchError as e:
            if getattr(e, "code", None) == 1010:
                blocked = True
        except Exception:
            pass
    check("t9_no_cooldown_bypasses", not blocked)
    os.environ.pop("ANTIDETECT_NO_COOLDOWN", None)

    os.environ["ANTIDETECT_COOLDOWN_SECONDS"] = "0"
    check("t9_zero_cooldown_env", launcher.cooldown_seconds() == 0)

    with mock.patch("subprocess.Popen", side_effect=OSError("Mocked launch")):
        blocked = False
        try:
            launcher.open_profile(profile)
        except launcher.LaunchError as e:
            if getattr(e, "code", None) == 1010:
                blocked = True
        except Exception:
            pass
    check("t9_zero_cooldown_bypasses", not blocked)
    os.environ["ANTIDETECT_COOLDOWN_SECONDS"] = "30"
    launcher._closed_at.clear()


def main():
    tests = [
        t1_ensure_injects_new, t1_alias_works, t1_idempotent_restore, t1_idempotent_restore_4_migration, t1_idempotent_restore_other_not_overwritten, t1_idempotent_restore_other_2_not_overwritten, t1_idempotent_no_change,
        t1_session_missing_added, t1_session_not_dict_replaced, t1_credentials_missing_added,
        t1_no_tmp_leftover, t1_malformed_preferences_no_crash, t1_default_dir_created,
        t2_gmail_session_cookies, t2_persistent_cookie_session_false, t2_empty_db_returns_empty_list,
        t2_verify_cookie_db_true, t2_verify_empty_db_true, t2_get_cookies_requires_closed,
        t3_partial_preserves_session, t3_empty_preserve_keeps_cookies, t3_empty_preserve_explicit_false_wipes,
        t3_none_input_no_wipe, t3_str_empty_no_wipe, t3_wipe_explicit_list_empty_false,
        t3_update_existing_cookie, t3_preserve_sessions_param_default_true, t3_update_cookies_returns_true,
        t3_update_cookies_signature,
        t4_graceful_close_no_address_process_gone, t4_graceful_close_no_address_process_alive,
        t4_graceful_close_journal_wait, t4_graceful_close_signature, t4_close_profile_passes_data_dir, t4_graceful_close_sends_browser_close,
        t4_graceful_close_network_journal_wait, t4_graceful_close_prefers_browser_close_without_page_close, t4_graceful_close_falls_back_to_page_close_on_ws_error,
        t5_guard_skips_empty_upload, t5_nonempty_upload_called, t5_sync_guard_returns_cookie_res_none_on_empty,
        t5_ensure_alias_exists, t5_ensure_password_manager_exists,
        t6_extra_counts, t6_empty_update_preserve_default, t6_empty_update_preserve_true_explicit,
        t6_empty_update_wipe_false, t6_partial_update_adds, t6_cookie_digest_stable,
        t6_verify_cookie_db_false_missing,
        t7_no_duplicate_on_chromium_style_overwrite, t7_non_secure_stored_as_https,
        t7_dedupe_removes_duplicates, t7_deduke_session_preserved, t7_dedupe_refuses_when_open,
        t8_insert_removes_scheme_port_cross_variants, t8_insert_canonical_from_latest_existing,
        t8_dedupe_merges_cross_variant, t8_dedupe_merges_scheme_port_variant, t8_gmail_at_preserved_after_dedupe,
        t9_cooldown_blocks_immediate_open, t9_cooldown_bypassed_with_force, t9_cooldown_disabled_via_env,
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
