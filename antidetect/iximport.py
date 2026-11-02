"""Import profiles and cookies from an ixBrowser export archive."""
from collections import Counter
import io
import json
from pathlib import Path
import re
import sqlite3
import xml.etree.ElementTree as ET
import zipfile

from . import cookies, store

MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_ENTRIES = 2000
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_TXT_BYTES = 5 * 1024 * 1024

DEFAULT_GROUP_NAME = "Default Group"
TXT_NAME_RE = re.compile(r"^(\d+)_(.+)\.txt$", re.IGNORECASE)
VALID_SAME_SITES = (-1, 0, 1, 2)

PLATFORM_PRIORITY = (
    ("facebook.com", 1, "https://www.facebook.com/"),
    ("tiktok.com", 2, "https://www.tiktok.com/"),
    ("instagram.com", 3, "https://www.instagram.com/"),
    ("threads.net", 8, "https://www.threads.net/"),
    ("threads.com", 8, "https://www.threads.net/"),
    ("shopee.co.id", 4, "https://shopee.co.id/"),
    ("tokopedia.com", 5, "https://www.tokopedia.com/"),
    ("lazada.co.id", 7, "https://www.lazada.co.id/"),
    ("google.com", 6, "https://www.google.com/"),
)
CUSTOM_SITE_ID = 22


class IxImportError(Exception):
    pass


def _validate_zip_file(zip_path):
    try:
        candidate = Path(zip_path)
        stat = candidate.stat()
    except (OSError, TypeError, ValueError) as error:
        raise IxImportError(f"file zip tidak bisa diakses: {error}")
    if not candidate.is_file():
        raise IxImportError("file zip tidak ditemukan")
    if stat.st_size > MAX_ZIP_BYTES:
        raise IxImportError("ukuran zip melebihi batas 50 MB")
    return candidate


def _check_archive_limits(archive, check_txt_limit=True):
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise IxImportError("jumlah entri zip melebihi batas 2000")
    total_size = 0
    for info in infos:
        if info.file_size < 0:
            raise IxImportError("ukuran entri zip tidak valid")
        total_size += info.file_size
        if total_size > MAX_UNCOMPRESSED_BYTES:
            raise IxImportError("total isi zip tidak terkompresi melebihi batas 100 MB")
        if (
            check_txt_limit
            and not info.is_dir()
            and info.filename.lower().endswith(".txt")
            and info.file_size > MAX_TXT_BYTES
        ):
            raise IxImportError(f"ukuran file TXT melebihi batas 5 MB: {info.filename}")
    return infos


def _read_bounded(archive, info, limit):
    with archive.open(info) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise IxImportError(f"ukuran entri melebihi batas: {info.filename}")
    return data


def _local_tag(tag):
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _column_index(cell_ref):
    letters = "".join(ch for ch in str(cell_ref or "") if ch.isalpha()).upper()
    if not letters:
        return None
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index - 1


def _parse_shared_strings(xml_bytes):
    root = ET.fromstring(xml_bytes)
    strings = []
    for item in root:
        if _local_tag(item.tag) != "si":
            continue
        parts = [
            node.text or ""
            for node in item.iter()
            if _local_tag(node.tag) == "t"
        ]
        strings.append("".join(parts))
    return strings


def _cell_value(cell, shared_strings):
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(
            node.text or ""
            for node in cell.iter()
            if _local_tag(node.tag) == "t"
        )
    value_text = ""
    for child in cell:
        if _local_tag(child.tag) == "v" and child.text is not None:
            value_text = child.text
            break
    if cell_type == "s":
        try:
            return shared_strings[int(value_text)]
        except (IndexError, ValueError):
            return ""
    return value_text


def _parse_xlsx_bytes(xlsx_bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as xlsx_zip:
            _check_archive_limits(xlsx_zip, check_txt_limit=False)
            names = set(xlsx_zip.namelist())
            shared_strings = []
            if "xl/sharedStrings.xml" in names:
                shared_strings = _parse_shared_strings(
                    xlsx_zip.read("xl/sharedStrings.xml")
                )
            sheet_name = None
            if "xl/worksheets/sheet1.xml" in names:
                sheet_name = "xl/worksheets/sheet1.xml"
            else:
                worksheets = sorted(
                    name
                    for name in names
                    if name.startswith("xl/worksheets/")
                    and name.endswith(".xml")
                    and "/_rels/" not in name
                )
                if worksheets:
                    sheet_name = worksheets[0]
            if not sheet_name:
                return {}
            sheet_xml = xlsx_zip.read(sheet_name)
    except (zipfile.BadZipFile, ET.ParseError, KeyError, OSError):
        return {}

    try:
        root = ET.fromstring(sheet_xml)
    except ET.ParseError:
        return {}

    header_cols = None
    rows_by_serial = {}
    for elem in root.iter():
        if _local_tag(elem.tag) != "row":
            continue
        cells = {}
        next_col = 0
        for cell in elem:
            if _local_tag(cell.tag) != "c":
                continue
            col_idx = _column_index(cell.attrib.get("r"))
            if col_idx is None:
                col_idx = next_col
            next_col = col_idx + 1
            cells[col_idx] = _cell_value(cell, shared_strings).strip()

        if header_cols is None:
            if any(value == "Profile Title" for value in cells.values()):
                header_cols = {value: idx for idx, value in cells.items() if value}
            continue

        no_col = header_cols.get("No")
        no_raw = cells.get(no_col, "") if no_col is not None else ""
        if not no_raw:
            continue
        try:
            serial = int(float(no_raw))
        except ValueError:
            continue

        title_col = header_cols.get("Profile Title")
        group_col = header_cols.get("Group")
        note_col = header_cols.get("Profile Notes")
        ua_col = header_cols.get("UA")

        rows_by_serial[serial] = {
            "name": cells.get(title_col, "") if title_col is not None else "",
            "group": cells.get(group_col, "") if group_col is not None else "",
            "note": cells.get(note_col, "") if note_col is not None else "",
            "ua": cells.get(ua_col, "") if ua_col is not None else "",
        }
    return rows_by_serial


def _extract_excel_rows(archive, infos):
    for info in infos:
        if not info.is_dir() and info.filename.lower().endswith(".xlsx"):
            raw = _read_bounded(archive, info, MAX_UNCOMPRESSED_BYTES)
            rows = _parse_xlsx_bytes(raw)
            if rows:
                return rows
    return None


def _load_excel_metadata(archive, infos, zip_path, metadata_path=None):
    rows = _extract_excel_rows(archive, infos)
    if rows is not None:
        return rows, True

    if metadata_path is not None and str(metadata_path).strip():
        meta_candidate = _validate_zip_file(metadata_path)
        try:
            with zipfile.ZipFile(meta_candidate) as meta_archive:
                meta_infos = _check_archive_limits(meta_archive, check_txt_limit=False)
                rows = _extract_excel_rows(meta_archive, meta_infos)
                if rows is not None:
                    return rows, True
        except (zipfile.BadZipFile, OSError) as error:
            raise IxImportError(f"file metadata bukan arsip zip yang valid: {error}")

    sibling_zip = zip_path.parent / "Export Profile.zip"
    if sibling_zip.is_file() and sibling_zip.resolve() != zip_path.resolve():
        try:
            if sibling_zip.stat().st_size <= MAX_ZIP_BYTES:
                with zipfile.ZipFile(sibling_zip) as sib_archive:
                    sib_infos = _check_archive_limits(sib_archive, check_txt_limit=False)
                    rows = _extract_excel_rows(sib_archive, sib_infos)
                    if rows is not None:
                        return rows, True
        except (IxImportError, zipfile.BadZipFile, OSError):
            return {}, False
    return {}, False


def _parse_same_site(value):
    if isinstance(value, bool):
        return -1
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return -1
    return number if number in VALID_SAME_SITES else -1


def _parse_expires(value):
    if value in (None, "", False):
        return 0
    if isinstance(value, bool):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _map_cookie(raw):
    if not isinstance(raw, dict):
        return None
    domain = str(raw.get("domain") or "").strip()
    name = str(raw.get("name") or "").strip()
    if not domain or not name:
        return None
    path = str(raw.get("path") or "").strip() or "/"
    value = raw.get("value")
    expires = _parse_expires(raw.get("expiration_time"))
    return {
        "domain": domain,
        "name": name,
        "path": path,
        "value": "" if value is None else str(value),
        "expires": expires,
        "httpOnly": bool(raw.get("http_only")),
        "secure": bool(raw.get("secure")),
        "sameSite": _parse_same_site(raw.get("same_site")),
        "session": expires <= 0,
    }


def infer_platform(cookie_list):
    hosts = {
        str(item.get("domain") or "").strip().lower().lstrip(".")
        for item in (cookie_list or ())
        if isinstance(item, dict)
    }
    hosts.discard("")
    for target, site_id, site_url in PLATFORM_PRIORITY:
        if any(host == target or host.endswith("." + target) for host in hosts):
            return site_id, site_url
    return CUSTOM_SITE_ID, None


def read_export(zip_path, metadata_path=None):
    candidate = _validate_zip_file(zip_path)
    try:
        archive = zipfile.ZipFile(candidate)
    except (zipfile.BadZipFile, OSError) as error:
        raise IxImportError(f"file bukan arsip zip yang valid: {error}")

    with archive:
        infos = _check_archive_limits(archive, check_txt_limit=True)
        excel_rows, has_metadata = _load_excel_metadata(
            archive, infos, candidate, metadata_path=metadata_path
        )
        profiles = []
        errors = []

        for info in infos:
            if info.is_dir() or not info.filename.lower().endswith(".txt"):
                continue
            basename = Path(info.filename).name
            match = TXT_NAME_RE.match(basename)
            if not match:
                errors.append({
                    "file": info.filename,
                    "reason": "nama file tidak sesuai pola <angka>_<judul>.txt",
                })
                continue

            serial = int(match.group(1))
            file_title = match.group(2).strip()
            if not file_title:
                errors.append({
                    "file": info.filename,
                    "reason": "judul profil pada nama file kosong",
                })
                continue

            raw_bytes = _read_bounded(archive, info, MAX_TXT_BYTES)
            try:
                parsed = json.loads(raw_bytes.decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError):
                errors.append({
                    "file": info.filename,
                    "reason": "isi file bukan JSON yang valid",
                })
                continue

            if not isinstance(parsed, list):
                errors.append({
                    "file": info.filename,
                    "reason": "isi file harus berupa array JSON cookie",
                })
                continue

            mapped_cookies = []
            invalid_count = 0
            for raw_cookie in parsed:
                mapped = _map_cookie(raw_cookie)
                if mapped is None:
                    invalid_count += 1
                else:
                    mapped_cookies.append(mapped)

            if invalid_count:
                errors.append({
                    "file": info.filename,
                    "reason": f"{invalid_count} cookie tanpa domain atau name",
                })

            meta = excel_rows.get(serial) or {}
            name = meta.get("name") or file_title
            group = meta.get("group") or DEFAULT_GROUP_NAME
            note = meta.get("note") or ""
            ua = meta.get("ua") or ""

            profiles.append({
                "serial": serial,
                "name": name,
                "group": group,
                "note": note,
                "ua": ua,
                "cookies": mapped_cookies,
            })

    profiles.sort(key=lambda item: (item["serial"], item["name"]))
    return {
        "source": str(candidate),
        "metadata": has_metadata,
        "profiles": profiles,
        "errors": errors,
    }


def _stored_profile_names():
    if not store.DB_PATH.is_file():
        return set()
    try:
        conn = sqlite3.connect(f"file:{store.DB_PATH}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT name FROM profiles WHERE deleted_at IS NULL"
            ).fetchall()
            return {str(row[0]) for row in rows if row[0]}
        finally:
            conn.close()
    except sqlite3.Error:
        return set()


def preview(zip_path, existing_names=(), metadata_path=None):
    exported = read_export(zip_path, metadata_path=metadata_path)
    profiles = exported["profiles"]

    if existing_names:
        known_existing = {str(name) for name in existing_names if name}
    else:
        known_existing = _stored_profile_names()

    group_counts = Counter(item["group"] for item in profiles)
    name_counts = Counter(item["name"] for item in profiles)

    duplicates = [name for name, count in name_counts.items() if count > 1]
    existing_matches = []
    seen_existing = set()
    for item in profiles:
        name = item["name"]
        if name in known_existing and name not in seen_existing:
            existing_matches.append(name)
            seen_existing.add(name)

    serials = [item["serial"] for item in profiles]
    return {
        "total": len(profiles),
        "cookies": sum(len(item["cookies"]) for item in profiles),
        "groups": dict(group_counts.most_common()),
        "duplicate_names": duplicates,
        "existing_names": existing_matches,
        "empty_cookie_files": sum(1 for item in profiles if not item["cookies"]),
        "errors": list(exported["errors"]),
        "serials": {
            "first": min(serials) if serials else 0,
            "last": max(serials) if serials else 0,
        },
        "metadata": exported["metadata"],
    }


def _resolve_group(conn, group_title, group_cache):
    clean = str(group_title or "").strip()
    if not clean or clean.lower() in ("default", "default group"):
        return 1, False
    key = clean.lower()
    if key in group_cache:
        return group_cache[key], False
    row = conn.execute(
        "SELECT id FROM groups WHERE lower(title) = lower(?) ORDER BY id LIMIT 1",
        (clean,),
    ).fetchone()
    if row:
        group_cache[key] = row["id"]
        return row["id"], False
    cur = conn.execute(
        "INSERT INTO groups (title, sort, created_at) VALUES (?, ?, ?)",
        (clean, 0, store.now()),
    )
    conn.commit()
    group_cache[key] = cur.lastrowid
    return cur.lastrowid, True


def apply_import(zip_path, update_existing=False, metadata_path=None):
    exported = read_export(zip_path, metadata_path=metadata_path)
    store.init()

    with store.db() as conn:
        rows = conn.execute(
            "SELECT id, name FROM profiles WHERE deleted_at IS NULL ORDER BY id"
        ).fetchall()
        existing_by_name = {}
        for row in rows:
            existing_by_name.setdefault(row["name"], row["id"])

    created = 0
    updated = 0
    skipped = 0
    cookies_written = 0
    groups_created = 0
    errors = list(exported["errors"])
    group_cache = {}
    name_counts = Counter(item["name"] for item in exported["profiles"])
    updated_names = set()

    for item in exported["profiles"]:
        name = item["name"]
        cookie_list = item["cookies"]
        if name in existing_by_name:
            if not update_existing:
                skipped += 1
                continue
            if name_counts[name] > 1 and name in updated_names:
                errors.append({
                    "file": name,
                    "reason": "judul duplikat, hanya profil pertama yang diupdate",
                })
                continue
            target_id = existing_by_name[name]
            try:
                cookies.update_cookies(target_id, cookie_list)
                updated += 1
                cookies_written += len(cookie_list)
                updated_names.add(name)
            except Exception as error:
                errors.append({"file": name, "reason": str(error)})
            continue

        try:
            site_id, site_url = infer_platform(cookie_list)
            fp_config = {"ua": item["ua"]} if item.get("ua") else {}
            with store.db() as conn:
                group_id, is_new_group = _resolve_group(
                    conn, item["group"], group_cache
                )
                if is_new_group:
                    groups_created += 1
                profile_id = store.create_profile(
                    conn,
                    {
                        "name": name,
                        "group_id": group_id,
                        "note": item["note"] or None,
                        "site_id": site_id,
                        "site_url": site_url,
                        "fingerprint_config": fp_config,
                    },
                )
            try:
                cookies.update_cookies(profile_id, cookie_list)
            except Exception:
                # Jangan tinggalkan profil tanpa cookie saat penulisan gagal.
                try:
                    with store.db() as conn:
                        store.delete_profile_permanent(conn, profile_id)
                except Exception:
                    pass
                raise
            created += 1
            cookies_written += len(cookie_list)
        except Exception as error:
            errors.append({"file": name, "reason": str(error)})

    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "cookies": cookies_written,
        "groups_created": groups_created,
        "errors": errors,
        "metadata": exported["metadata"],
    }
