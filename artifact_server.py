#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import tempfile
import time
import urllib.parse
import uuid
import zipfile
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_NAME = "Artifact"
FORMAT_VERSION = 1
DATABASE_VERSION = 5
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("ARTIFACT_DATA_DIR", BASE_DIR / "data")).resolve()
DB_PATH = DATA_DIR / "artifact.sqlite3"
PHOTOS_DIR = DATA_DIR / "photos"
TOKEN = os.environ.get("ARTIFACT_SYNC_TOKEN", "").strip()
AUTH_COOKIE = "artifact_token"
REQUIRED_JSON_ENTRIES = {
    "manifest.json",
    "collection_settings.json",
    "items.json",
    "photos.json",
    "uid_allocations.json",
}
CATEGORIES = [
    "Laptop",
    "Desktop",
    "Palmtop",
    "Handheld",
    "Workstation",
    "Server",
    "Terminal",
    "Monitor",
    "Keyboard",
    "Mouse",
    "Printer",
    "Storage",
    "Expansion card",
    "Peripheral",
    "Software",
    "Manual",
    "Cable",
    "Memorabilia",
    "Prototype",
    "Other",
]


def now_ms() -> int:
    return int(time.time() * 1000)


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS collection_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                collectionName TEXT NOT NULL DEFAULT '',
                ownerName TEXT NOT NULL DEFAULT '',
                ownerEmail TEXT NOT NULL DEFAULT '',
                ownerPhone TEXT NOT NULL DEFAULT '',
                ownerNotes TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                catalogUid INTEGER NOT NULL UNIQUE,
                category TEXT NOT NULL,
                manufacturerId TEXT,
                manufacturer TEXT NOT NULL,
                modelId TEXT,
                itemType TEXT NOT NULL,
                productionYearStart INTEGER,
                productionYearEnd INTEGER,
                estimatedValue REAL,
                serialNumber TEXT NOT NULL,
                location TEXT NOT NULL,
                notes TEXT NOT NULL,
                createdAt INTEGER NOT NULL,
                updatedAt INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS photos (
                id TEXT PRIMARY KEY,
                itemId TEXT NOT NULL,
                fileName TEXT NOT NULL,
                originalFileName TEXT NOT NULL,
                createdAt INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS index_photos_itemId ON photos(itemId);

            CREATE TABLE IF NOT EXISTS catalog_uid_allocations (
                uid INTEGER PRIMARY KEY,
                itemId TEXT NOT NULL,
                allocatedAt INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO collection_settings(id)
            VALUES (1)
            """
        )
        set_meta(connection, "revision", get_meta(connection, "revision", "0"))
        set_meta(connection, "updatedAt", get_meta(connection, "updatedAt", str(now_ms())))


def get_meta(connection: sqlite3.Connection, key: str, default: str = "") -> str:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key = ?",
        (key,),
    ).fetchone()
    return row["value"] if row else default


def set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        """
        INSERT INTO metadata(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def bump_revision(connection: sqlite3.Connection) -> None:
    revision = int(get_meta(connection, "revision", "0")) + 1
    set_meta(connection, "revision", str(revision))
    set_meta(connection, "updatedAt", str(now_ms()))


def load_settings(connection: sqlite3.Connection) -> sqlite3.Row:
    return connection.execute(
        "SELECT * FROM collection_settings WHERE id = 1",
    ).fetchone()


def collection_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        "itemCount": connection.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        "photoCount": connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0],
        "uidAllocationCount": connection.execute(
            "SELECT COUNT(*) FROM catalog_uid_allocations",
        ).fetchone()[0],
    }


def require_safe_zip_name(entry_name: str) -> None:
    parts = entry_name.split("/")
    if (
        not entry_name
        or entry_name.startswith("/")
        or "\\" in entry_name
        or any(part == ".." for part in parts)
    ):
        raise ValueError(f"Unsafe backup entry name: {entry_name}")


def safe_extension(path: str) -> str:
    extension = Path(path).suffix.lower().lstrip(".")
    if 1 <= len(extension) <= 8 and extension.isalnum():
        return extension
    return "jpg"


def json_or_empty(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    parsed = float(value)
    if not (parsed >= 0.0 and parsed != float("inf")):
        raise ValueError("Estimated value must be finite and non-negative.")
    return parsed


def validate_archive_payload(
    items: list[dict[str, Any]],
    photos: list[dict[str, Any]],
    uid_allocations: list[dict[str, Any]],
) -> None:
    item_ids: set[str] = set()
    item_uids: set[int] = set()
    for item in items:
        item_id = str(item.get("id", "")).strip()
        if not item_id:
            raise ValueError("Backup contains an item with a blank id.")
        if item_id in item_ids:
            raise ValueError(f"Backup contains duplicate item id {item_id}.")
        item_ids.add(item_id)

        uid = int(item.get("catalogUid", 0))
        if uid <= 0:
            raise ValueError("Backup contains an invalid UID.")
        if uid in item_uids:
            raise ValueError(f"Backup contains duplicate item UID #{uid}.")
        item_uids.add(uid)

        if not str(item.get("category", "")).strip():
            raise ValueError(f"Backup item #{uid} has a blank category.")
        start = optional_int(item.get("productionYearStart"))
        end = optional_int(item.get("productionYearEnd"))
        if start is not None and end is not None and start > end:
            raise ValueError(f"Backup item #{uid} has invalid production years.")
        optional_float(item.get("estimatedValue"))

    allocated_uids: set[int] = set()
    for allocation in uid_allocations:
        uid = int(allocation.get("uid", 0))
        if uid <= 0:
            raise ValueError("Backup contains an invalid allocated UID.")
        if uid in allocated_uids:
            raise ValueError(f"Backup contains duplicate allocated UID #{uid}.")
        allocated_uids.add(uid)
    for uid in item_uids:
        if uid not in allocated_uids:
            raise ValueError(f"Backup is missing UID allocation for item #{uid}.")

    photo_ids: set[str] = set()
    for photo in photos:
        photo_id = str(photo.get("id", "")).strip()
        if not photo_id:
            raise ValueError("Backup contains a photo with a blank id.")
        if photo_id in photo_ids:
            raise ValueError(f"Backup contains duplicate photo id {photo_id}.")
        photo_ids.add(photo_id)
        if str(photo.get("itemId", "")).strip() not in item_ids:
            raise ValueError("Backup contains a photo for an unknown item.")
        archive_path = str(photo.get("archivePath", ""))
        require_safe_zip_name(archive_path)
        if not archive_path.startswith("photos/"):
            raise ValueError(f"Backup photo has an invalid archive path: {archive_path}")


def load_archive(backup_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(backup_path, "r") as archive:
        names = set(archive.namelist())
        for name in names:
            require_safe_zip_name(name)
        missing = REQUIRED_JSON_ENTRIES - names
        if missing:
            raise ValueError(f"Backup is missing {', '.join(sorted(missing))}.")

        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("app") != APP_NAME:
            raise ValueError("Backup is not an Artifact backup.")
        if int(manifest.get("formatVersion", 0)) != FORMAT_VERSION:
            raise ValueError("Unsupported backup format version.")

        settings = json.loads(archive.read("collection_settings.json"))
        items = json.loads(archive.read("items.json"))
        photos = json.loads(archive.read("photos.json"))
        uid_allocations = json.loads(archive.read("uid_allocations.json"))

        if int(manifest.get("itemCount", -1)) != len(items):
            raise ValueError("Backup item count does not match its manifest.")
        if int(manifest.get("photoCount", -1)) != len(photos):
            raise ValueError("Backup photo count does not match its manifest.")
        if int(manifest.get("uidAllocationCount", -1)) != len(uid_allocations):
            raise ValueError("Backup UID count does not match its manifest.")

        validate_archive_payload(items, photos, uid_allocations)

        photo_files: list[dict[str, Any]] = []
        for photo in photos:
            archive_path = str(photo["archivePath"])
            if archive_path not in names:
                raise ValueError(f"Backup is missing photo file {archive_path}.")
            extension = safe_extension(archive_path)
            file_name = f"{photo['id']}.{extension}"
            photo_files.append(
                {
                    "photo": photo,
                    "archivePath": archive_path,
                    "fileName": file_name,
                    "originalFileName": Path(
                        str(photo.get("originalFileName") or archive_path),
                    ).name,
                    "content": archive.read(archive_path),
                }
            )

    return {
        "settings": settings,
        "items": items,
        "photos": photos,
        "uidAllocations": uid_allocations,
        "photoFiles": photo_files,
    }


def replace_collection_from_archive(backup_path: Path) -> dict[str, int]:
    payload = load_archive(backup_path)
    tmp_photos = Path(tempfile.mkdtemp(prefix="photos-import-", dir=DATA_DIR))
    old_photos = DATA_DIR / f"photos-old-{uuid.uuid4().hex}"
    photos_swapped = False

    try:
        for entry in payload["photoFiles"]:
            (tmp_photos / entry["fileName"]).write_bytes(entry["content"])

        if PHOTOS_DIR.exists():
            PHOTOS_DIR.rename(old_photos)
        tmp_photos.rename(PHOTOS_DIR)
        photos_swapped = True

        with connect() as connection:
            connection.execute("DELETE FROM photos")
            connection.execute("DELETE FROM items")
            connection.execute("DELETE FROM catalog_uid_allocations")
            connection.execute(
                """
                UPDATE collection_settings
                SET collectionName = ?,
                    ownerName = ?,
                    ownerEmail = ?,
                    ownerPhone = ?,
                    ownerNotes = ?
                WHERE id = 1
                """,
                (
                    json_or_empty(payload["settings"].get("collectionName")).strip(),
                    json_or_empty(payload["settings"].get("ownerName")).strip(),
                    json_or_empty(payload["settings"].get("ownerEmail")).strip(),
                    json_or_empty(payload["settings"].get("ownerPhone")).strip(),
                    json_or_empty(payload["settings"].get("ownerNotes")).strip(),
                ),
            )
            connection.executemany(
                """
                INSERT INTO catalog_uid_allocations(uid, itemId, allocatedAt)
                VALUES (?, ?, ?)
                """,
                [
                    (
                        int(allocation["uid"]),
                        str(allocation["itemId"]),
                        int(allocation["allocatedAt"]),
                    )
                    for allocation in payload["uidAllocations"]
                ],
            )
            connection.executemany(
                """
                INSERT INTO items(
                    id, catalogUid, category, manufacturerId, manufacturer,
                    modelId, itemType, productionYearStart, productionYearEnd,
                    estimatedValue, serialNumber, location, notes, createdAt, updatedAt
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(item["id"]),
                        int(item["catalogUid"]),
                        normalize_category(item.get("category")),
                        optional_text(item.get("manufacturerId")),
                        json_or_empty(item.get("manufacturer")).strip(),
                        optional_text(item.get("modelId")),
                        json_or_empty(item.get("itemType")).strip(),
                        optional_int(item.get("productionYearStart")),
                        optional_int(item.get("productionYearEnd")),
                        optional_float(item.get("estimatedValue")),
                        json_or_empty(item.get("serialNumber")).strip(),
                        json_or_empty(item.get("location")).strip(),
                        json_or_empty(item.get("notes")).strip(),
                        int(item["createdAt"]),
                        int(item["updatedAt"]),
                    )
                    for item in payload["items"]
                ],
            )
            connection.executemany(
                """
                INSERT INTO photos(id, itemId, fileName, originalFileName, createdAt)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(entry["photo"]["id"]),
                        str(entry["photo"]["itemId"]),
                        entry["fileName"],
                        entry["originalFileName"],
                        int(entry["photo"]["createdAt"]),
                    )
                    for entry in payload["photoFiles"]
                ],
            )
            bump_revision(connection)
            counts = collection_counts(connection)
    except Exception:
        if photos_swapped:
            shutil.rmtree(PHOTOS_DIR, ignore_errors=True)
            if old_photos.exists():
                old_photos.rename(PHOTOS_DIR)
        else:
            shutil.rmtree(tmp_photos, ignore_errors=True)
        raise
    else:
        shutil.rmtree(old_photos, ignore_errors=True)
        return counts


def normalize_category(value: Any) -> str:
    text = str(value or "").strip()
    for category in CATEGORIES:
        if category.lower() == text.lower():
            return category
    return "Other"


def build_backup_zip(target_path: Path) -> dict[str, int]:
    with connect() as connection:
        settings = dict(load_settings(connection))
        items = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM items ORDER BY createdAt ASC, id ASC",
            )
        ]
        photos = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM photos ORDER BY createdAt ASC, id ASC",
            )
        ]
        uid_allocations = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM catalog_uid_allocations ORDER BY uid ASC",
            )
        ]

    photo_json = []
    for photo in photos:
        file_name = photo["fileName"]
        source = PHOTOS_DIR / file_name
        if not source.is_file():
            raise FileNotFoundError(f"Missing photo file for backup: {file_name}")
        archive_path = f"photos/{file_name}"
        photo_json.append(
            {
                "id": photo["id"],
                "itemId": photo["itemId"],
                "archivePath": archive_path,
                "originalFileName": photo["originalFileName"],
                "createdAt": photo["createdAt"],
            }
        )

    manifest = {
        "app": APP_NAME,
        "formatVersion": FORMAT_VERSION,
        "databaseVersion": DATABASE_VERSION,
        "createdAt": now_ms(),
        "itemCount": len(items),
        "photoCount": len(photo_json),
        "uidAllocationCount": len(uid_allocations),
    }

    with zipfile.ZipFile(target_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        archive.writestr("collection_settings.json", json.dumps(settings_for_archive(settings), indent=2))
        archive.writestr("items.json", json.dumps(items_for_archive(items), indent=2))
        archive.writestr("photos.json", json.dumps(photo_json, indent=2))
        archive.writestr("uid_allocations.json", json.dumps(uid_allocations, indent=2))
        for photo in photos:
            archive.write(PHOTOS_DIR / photo["fileName"], f"photos/{photo['fileName']}")

    return {
        "itemCount": len(items),
        "photoCount": len(photo_json),
        "uidAllocationCount": len(uid_allocations),
    }


def settings_for_archive(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "collectionName": settings.get("collectionName", ""),
        "ownerName": settings.get("ownerName", ""),
        "ownerEmail": settings.get("ownerEmail", ""),
        "ownerPhone": settings.get("ownerPhone", ""),
        "ownerNotes": settings.get("ownerNotes", ""),
    }


def items_for_archive(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": item["id"],
            "catalogUid": item["catalogUid"],
            "category": normalize_category(item["category"]),
            "manufacturerId": item["manufacturerId"],
            "manufacturer": item["manufacturer"],
            "modelId": item["modelId"],
            "itemType": item["itemType"],
            "productionYearStart": item["productionYearStart"],
            "productionYearEnd": item["productionYearEnd"],
            "estimatedValue": item["estimatedValue"],
            "serialNumber": item["serialNumber"],
            "location": item["location"],
            "notes": item["notes"],
            "createdAt": item["createdAt"],
            "updatedAt": item["updatedAt"],
        }
        for item in items
    ]


def parse_nullable_int_form(form: dict[str, str], key: str) -> int | None:
    value = form.get(key, "").strip()
    if not value:
        return None
    return int(value)


def parse_nullable_float_form(form: dict[str, str], key: str) -> float | None:
    value = form.get(key, "").strip().replace(",", ".")
    if not value:
        return None
    parsed = float(value)
    if parsed < 0.0:
        raise ValueError("Estimated value must be non-negative.")
    return parsed


def html_escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def format_timestamp(value: Any) -> str:
    try:
        seconds = int(value) / 1000
    except (TypeError, ValueError):
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds))


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_escape(title)} · Artifact</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f7f7f4;
      --surface: #ffffff;
      --text: #202124;
      --muted: #676b70;
      --line: #d9d9d4;
      --accent: #256f65;
      --danger: #b3261e;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #181a1b;
        --surface: #222526;
        --text: #edecea;
        --muted: #b7babd;
        --line: #3d4143;
        --accent: #75c8ba;
        --danger: #ffb4ab;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 15px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      border-bottom: 1px solid var(--line);
      background: var(--surface);
    }}
    main, .bar {{
      width: min(1120px, calc(100vw - 32px));
      margin: 0 auto;
    }}
    .bar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      min-height: 58px;
    }}
    h1, h2, h3 {{ line-height: 1.15; margin: 0; }}
    h1 {{ font-size: 22px; }}
    h2 {{ font-size: 18px; margin-bottom: 12px; }}
    h3 {{ font-size: 16px; }}
    main {{ padding: 24px 0 40px; }}
    section {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      margin-bottom: 18px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
      gap: 14px;
    }}
    label {{ display: block; color: var(--muted); font-size: 13px; margin-bottom: 5px; }}
    input, textarea, select {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--bg);
      color: var(--text);
      padding: 9px 10px;
      font: inherit;
    }}
    textarea {{ min-height: 96px; resize: vertical; }}
    button, .button {{
      appearance: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 38px;
      border: 1px solid var(--accent);
      border-radius: 6px;
      background: var(--accent);
      color: #ffffff;
      padding: 8px 12px;
      font: inherit;
      text-decoration: none;
      cursor: pointer;
    }}
    .button.secondary, button.secondary {{
      background: transparent;
      color: var(--accent);
    }}
    .danger {{
      border-color: var(--danger);
      color: var(--danger);
      background: transparent;
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      margin-top: 12px;
    }}
    .muted {{ color: var(--muted); }}
    .error {{ color: var(--danger); }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    th, td {{
      text-align: left;
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    th {{ color: var(--muted); font-weight: 600; }}
    .nowrap {{ white-space: nowrap; }}
    .photos {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
      gap: 12px;
    }}
    .photos img {{
      display: block;
      width: 100%;
      aspect-ratio: 4 / 3;
      object-fit: cover;
      border-radius: 6px;
      border: 1px solid var(--line);
      background: var(--bg);
    }}
  </style>
</head>
<body>
  <header>
    <div class="bar">
      <h1>Artifact</h1>
      <nav><a class="button secondary" href="/">Collection</a></nav>
    </div>
  </header>
  <main>{body}</main>
</body>
</html>"""

def login_page(error: str = "") -> str:
    message = f'<p class="error">{html_escape(error)}</p>' if error else ""
    return page(
        "Login",
        f"""
        <section>
          <h2>Server Login</h2>
          {message}
          <form method="post" action="/login">
            <label for="token">Access token</label>
            <input id="token" name="token" type="password" autofocus>
            <div class="actions"><button type="submit">Login</button></div>
          </form>
        </section>
        """,
    )


class ArtifactHandler(BaseHTTPRequestHandler):
    server_version = "ArtifactServer/0.1"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/login":
            self.send_html(login_page())
            return
        if not self.authorized(parsed):
            self.send_unauthorized(parsed.path)
            return

        try:
            if parsed.path == "/":
                self.handle_home()
            elif parsed.path == "/api/status":
                self.handle_api_status()
            elif parsed.path == "/api/backup":
                self.handle_download(api=True)
            elif parsed.path == "/download":
                self.handle_download(api=False)
            elif parsed.path.startswith("/items/"):
                self.handle_edit_item(parsed.path.removeprefix("/items/"))
            elif parsed.path.startswith("/photos/"):
                self.handle_photo(parsed.path.removeprefix("/photos/"))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as error:
            self.send_problem(error, api=parsed.path.startswith("/api/"))

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/login":
            self.handle_login()
            return
        if not self.authorized(parsed):
            self.send_unauthorized(parsed.path)
            return

        try:
            if parsed.path == "/api/backup":
                self.handle_api_upload()
            elif parsed.path == "/upload":
                self.handle_web_upload()
            elif parsed.path == "/settings":
                self.handle_save_settings()
            elif parsed.path.startswith("/items/") and parsed.path.endswith("/delete"):
                item_id = parsed.path.removeprefix("/items/").removesuffix("/delete").strip("/")
                self.handle_delete_item(item_id)
            elif parsed.path.startswith("/items/"):
                self.handle_save_item(parsed.path.removeprefix("/items/"))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as error:
            self.send_problem(error, api=parsed.path.startswith("/api/"))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def authorized(self, parsed: urllib.parse.ParseResult) -> bool:
        if not TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {TOKEN}":
            return True
        if self.headers.get("X-Artifact-Token", "") == TOKEN:
            return True
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("token", [""])[0] == TOKEN:
            return True
        cookie_header = self.headers.get("Cookie")
        if cookie_header:
            cookies = SimpleCookie(cookie_header)
            morsel = cookies.get(AUTH_COOKIE)
            if morsel and morsel.value == TOKEN:
                return True
        return False

    def handle_login(self) -> None:
        form = self.read_urlencoded_form()
        if form.get("token", "") != TOKEN:
            self.send_html(login_page("Invalid token."), status=HTTPStatus.UNAUTHORIZED)
            return
        body = b""
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", f"{AUTH_COOKIE}={TOKEN}; HttpOnly; SameSite=Lax; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_home(self) -> None:
        with connect() as connection:
            settings = dict(load_settings(connection))
            counts = collection_counts(connection)
            revision = get_meta(connection, "revision", "0")
            updated_at = get_meta(connection, "updatedAt", "0")
            items = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT i.*,
                           COUNT(p.id) AS photoCount
                    FROM items i
                    LEFT JOIN photos p ON p.itemId = i.id
                    GROUP BY i.id
                    ORDER BY i.updatedAt DESC, i.createdAt DESC
                    """
                )
            ]

        rows = "\n".join(self.item_row(item) for item in items)
        if not rows:
            rows = '<tr><td colspan="7" class="muted">No collection data on this server yet.</td></tr>'

        token_note = (
            '<p class="muted">Token authentication is enabled.</p>'
            if TOKEN
            else '<p class="muted">Token authentication is disabled. Set ARTIFACT_SYNC_TOKEN before exposing this server.</p>'
        )
        body = f"""
        <section>
          <h2>Server Status</h2>
          <div class="grid">
            <div><strong>{counts['itemCount']}</strong><br><span class="muted">Items</span></div>
            <div><strong>{counts['photoCount']}</strong><br><span class="muted">Photos</span></div>
            <div><strong>{counts['uidAllocationCount']}</strong><br><span class="muted">UID allocations</span></div>
            <div><strong>{html_escape(revision)}</strong><br><span class="muted">Revision</span></div>
          </div>
          <p class="muted">Last changed: {html_escape(format_timestamp(updated_at))}</p>
          {token_note}
          <div class="actions">
            <a class="button" href="/download">Download Artifact Backup</a>
          </div>
        </section>
        <section>
          <h2>Collection Settings</h2>
          <form method="post" action="/settings">
            <div class="grid">
              {self.input_field("collectionName", "Collection name", settings.get("collectionName", ""))}
              {self.input_field("ownerName", "Owner name", settings.get("ownerName", ""))}
              {self.input_field("ownerEmail", "Owner email", settings.get("ownerEmail", ""))}
              {self.input_field("ownerPhone", "Owner phone", settings.get("ownerPhone", ""))}
            </div>
            {self.textarea_field("ownerNotes", "Owner notes", settings.get("ownerNotes", ""))}
            <div class="actions"><button type="submit">Save Settings</button></div>
          </form>
        </section>
        <section>
          <h2>Upload Backup</h2>
          <form method="post" action="/upload" enctype="multipart/form-data">
            <label for="backup">Artifact backup archive</label>
            <input id="backup" name="backup" type="file" accept=".artifactbackup,.zip,application/zip">
            <div class="actions"><button type="submit">Upload and Replace Server Collection</button></div>
          </form>
        </section>
        <section>
          <h2>Items</h2>
          <table>
            <thead>
              <tr>
                <th class="nowrap">UID</th>
                <th>Category</th>
                <th>Manufacturer</th>
                <th>Type</th>
                <th>Location</th>
                <th class="nowrap">Photos</th>
                <th></th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </section>
        """
        self.send_html(page("Collection", body))

    def item_row(self, item: dict[str, Any]) -> str:
        return f"""
        <tr>
          <td class="nowrap">#{html_escape(item['catalogUid'])}</td>
          <td>{html_escape(item['category'])}</td>
          <td>{html_escape(item['manufacturer'])}</td>
          <td>{html_escape(item['itemType'])}</td>
          <td>{html_escape(item['location'])}</td>
          <td class="nowrap">{html_escape(item['photoCount'])}</td>
          <td class="nowrap"><a class="button secondary" href="/items/{urllib.parse.quote(item['id'])}">Edit</a></td>
        </tr>
        """

    def handle_edit_item(self, raw_item_id: str) -> None:
        item_id = urllib.parse.unquote(raw_item_id.strip("/"))
        with connect() as connection:
            item = connection.execute(
                "SELECT * FROM items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if item is None:
                self.send_error(HTTPStatus.NOT_FOUND, "Item not found")
                return
            photos = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM photos WHERE itemId = ? ORDER BY createdAt ASC, id ASC",
                    (item_id,),
                )
            ]

        photo_markup = "\n".join(
            f'<a href="/photos/{urllib.parse.quote(photo["id"])}"><img src="/photos/{urllib.parse.quote(photo["id"])}" alt=""></a>'
            for photo in photos
        )
        if not photo_markup:
            photo_markup = '<p class="muted">No photos for this item.</p>'

        body = f"""
        <section>
          <h2>Edit Item #{html_escape(item['catalogUid'])}</h2>
          <form method="post" action="/items/{urllib.parse.quote(item_id)}">
            <div class="grid">
              {self.select_category("category", item["category"])}
              {self.input_field("manufacturer", "Manufacturer", item["manufacturer"])}
              {self.input_field("itemType", "Type", item["itemType"])}
              {self.input_field("serialNumber", "Serial number", item["serialNumber"])}
              {self.input_field("location", "Location", item["location"])}
              {self.input_field("estimatedValue", "Estimated value", "" if item["estimatedValue"] is None else item["estimatedValue"])}
              {self.input_field("productionYearStart", "Production start year", "" if item["productionYearStart"] is None else item["productionYearStart"])}
              {self.input_field("productionYearEnd", "Production end year", "" if item["productionYearEnd"] is None else item["productionYearEnd"])}
              {self.input_field("manufacturerId", "Manufacturer ID", item["manufacturerId"] or "")}
              {self.input_field("modelId", "Model ID", item["modelId"] or "")}
            </div>
            {self.textarea_field("notes", "Notes", item["notes"])}
            <div class="actions">
              <button type="submit">Save Item</button>
              <a class="button secondary" href="/">Cancel</a>
            </div>
          </form>
        </section>
        <section>
          <h2>Photos</h2>
          <div class="photos">{photo_markup}</div>
        </section>
        <section>
          <h2>Delete Item</h2>
          <p class="muted">Deleting removes this item and its photos. Existing UID allocations are kept so future UIDs do not get reused.</p>
          <form method="post" action="/items/{urllib.parse.quote(item_id)}/delete">
            <button class="danger" type="submit">Delete Item</button>
          </form>
        </section>
        """
        self.send_html(page(f"Item #{item['catalogUid']}", body))

    def handle_save_item(self, raw_item_id: str) -> None:
        item_id = urllib.parse.unquote(raw_item_id.strip("/"))
        form = self.read_urlencoded_form()
        start = parse_nullable_int_form(form, "productionYearStart")
        end = parse_nullable_int_form(form, "productionYearEnd")
        if start is not None and end is not None and start > end:
            raise ValueError("Production start year must not be after the end year.")
        estimated_value = parse_nullable_float_form(form, "estimatedValue")
        with connect() as connection:
            result = connection.execute(
                """
                UPDATE items
                SET category = ?,
                    manufacturerId = ?,
                    manufacturer = ?,
                    modelId = ?,
                    itemType = ?,
                    productionYearStart = ?,
                    productionYearEnd = ?,
                    estimatedValue = ?,
                    serialNumber = ?,
                    location = ?,
                    notes = ?,
                    updatedAt = ?
                WHERE id = ?
                """,
                (
                    normalize_category(form.get("category")),
                    optional_text(form.get("manufacturerId")),
                    form.get("manufacturer", "").strip(),
                    optional_text(form.get("modelId")),
                    form.get("itemType", "").strip(),
                    start,
                    end,
                    estimated_value,
                    form.get("serialNumber", "").strip(),
                    form.get("location", "").strip(),
                    form.get("notes", "").strip(),
                    now_ms(),
                    item_id,
                ),
            )
            if result.rowcount != 1:
                raise ValueError("Item not found.")
            bump_revision(connection)
        self.redirect(f"/items/{urllib.parse.quote(item_id)}")

    def handle_delete_item(self, item_id: str) -> None:
        item_id = urllib.parse.unquote(item_id)
        with connect() as connection:
            photos = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM photos WHERE itemId = ?",
                    (item_id,),
                )
            ]
            result = connection.execute("DELETE FROM items WHERE id = ?", (item_id,))
            if result.rowcount != 1:
                raise ValueError("Item not found.")
            connection.execute("DELETE FROM photos WHERE itemId = ?", (item_id,))
            bump_revision(connection)
        for photo in photos:
            (PHOTOS_DIR / photo["fileName"]).unlink(missing_ok=True)
        self.redirect("/")

    def handle_save_settings(self) -> None:
        form = self.read_urlencoded_form()
        with connect() as connection:
            connection.execute(
                """
                UPDATE collection_settings
                SET collectionName = ?,
                    ownerName = ?,
                    ownerEmail = ?,
                    ownerPhone = ?,
                    ownerNotes = ?
                WHERE id = 1
                """,
                (
                    form.get("collectionName", "").strip(),
                    form.get("ownerName", "").strip(),
                    form.get("ownerEmail", "").strip(),
                    form.get("ownerPhone", "").strip(),
                    form.get("ownerNotes", "").strip(),
                ),
            )
            bump_revision(connection)
        self.redirect("/")

    def handle_api_status(self) -> None:
        with connect() as connection:
            payload = collection_counts(connection)
            payload["revision"] = int(get_meta(connection, "revision", "0"))
            payload["updatedAt"] = int(get_meta(connection, "updatedAt", "0"))
            payload["collectionName"] = load_settings(connection)["collectionName"]
        self.send_json(payload)

    def handle_api_upload(self) -> None:
        length = self.content_length()
        if length <= 0:
            raise ValueError("Missing backup body.")
        with tempfile.NamedTemporaryFile(
            prefix="artifact-upload-",
            suffix=".artifactbackup",
            dir=DATA_DIR,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("Upload ended before the declared content length.")
                handle.write(chunk)
                remaining -= len(chunk)
        try:
            counts = replace_collection_from_archive(temp_path)
        finally:
            temp_path.unlink(missing_ok=True)
        self.send_json(
            {
                "message": (
                    f"Imported {counts['itemCount']} items and "
                    f"{counts['photoCount']} photos on the server."
                ),
                **counts,
            }
        )

    def handle_web_upload(self) -> None:
        content = self.read_multipart_file("backup")
        if not content:
            raise ValueError("Upload form did not contain a backup file.")
        temp_path = DATA_DIR / f"artifact-web-upload-{uuid.uuid4().hex}.artifactbackup"
        temp_path.write_bytes(content)
        try:
            replace_collection_from_archive(temp_path)
        finally:
            temp_path.unlink(missing_ok=True)
        self.redirect("/")

    def handle_download(self, api: bool) -> None:
        with tempfile.NamedTemporaryFile(
            prefix="artifact-download-",
            suffix=".artifactbackup",
            dir=DATA_DIR,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
        try:
            build_backup_zip(temp_path)
            data = temp_path.read_bytes()
        finally:
            temp_path.unlink(missing_ok=True)
        filename = f"artifact-server-{time.strftime('%Y-%m-%d-%H%M')}.artifactbackup"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_photo(self, raw_photo_id: str) -> None:
        photo_id = urllib.parse.unquote(raw_photo_id.strip("/"))
        with connect() as connection:
            photo = connection.execute(
                "SELECT * FROM photos WHERE id = ?",
                (photo_id,),
            ).fetchone()
        if photo is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Photo not found")
            return
        path = PHOTOS_DIR / photo["fileName"]
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Photo file not found")
            return
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def input_field(self, name: str, label: str, value: Any) -> str:
        return f"""
        <div>
          <label for="{html_escape(name)}">{html_escape(label)}</label>
          <input id="{html_escape(name)}" name="{html_escape(name)}" value="{html_escape(value)}">
        </div>
        """

    def textarea_field(self, name: str, label: str, value: Any) -> str:
        return f"""
        <div style="margin-top: 14px">
          <label for="{html_escape(name)}">{html_escape(label)}</label>
          <textarea id="{html_escape(name)}" name="{html_escape(name)}">{html_escape(value)}</textarea>
        </div>
        """

    def select_category(self, name: str, current_value: Any) -> str:
        current = normalize_category(current_value)
        options = "\n".join(
            f'<option value="{html_escape(category)}"{" selected" if category == current else ""}>{html_escape(category)}</option>'
            for category in CATEGORIES
        )
        return f"""
        <div>
          <label for="{html_escape(name)}">Category</label>
          <select id="{html_escape(name)}" name="{html_escape(name)}">{options}</select>
        </div>
        """

    def content_length(self) -> int:
        return int(self.headers.get("Content-Length", "0") or "0")

    def read_urlencoded_form(self) -> dict[str, str]:
        length = self.content_length()
        body = self.rfile.read(length).decode("utf-8") if length else ""
        parsed = urllib.parse.parse_qs(body, keep_blank_values=True)
        return {key: values[0] if values else "" for key, values in parsed.items()}

    def read_multipart_file(self, field_name: str) -> bytes:
        content_type = self.headers.get("Content-Type", "")
        match = re.search(r"boundary=(?P<boundary>[^;]+)", content_type)
        if not match:
            raise ValueError("Upload must use multipart/form-data.")
        boundary = match.group("boundary").strip('"').encode("utf-8")
        body = self.rfile.read(self.content_length())
        delimiter = b"--" + boundary
        for part in body.split(delimiter):
            if not part or part in {b"--\r\n", b"--"}:
                continue
            header_blob, separator, content = part.partition(b"\r\n\r\n")
            if not separator:
                continue
            headers = header_blob.decode("utf-8", errors="replace")
            if f'name="{field_name}"' not in headers:
                continue
            if content.endswith(b"\r\n"):
                content = content[:-2]
            if content.endswith(b"--"):
                content = content[:-2]
            return content
        return b""

    def send_html(self, document: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = document.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_unauthorized(self, path: str) -> None:
        if path.startswith("/api/"):
            self.send_json({"error": "Unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
        else:
            self.send_html(login_page(), status=HTTPStatus.UNAUTHORIZED)

    def send_problem(self, error: Exception, api: bool) -> None:
        if api:
            self.send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
        else:
            self.send_html(
                page(
                    "Error",
                    f"""
                    <section>
                      <h2>Request Failed</h2>
                      <p class="error">{html_escape(error)}</p>
                      <div class="actions"><a class="button secondary" href="/">Back</a></div>
                    </section>
                    """,
                ),
                status=HTTPStatus.BAD_REQUEST,
            )

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Artifact collection sync server.")
    parser.add_argument("--host", default=os.environ.get("ARTIFACT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ARTIFACT_PORT", "8080")))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    init_db()
    address = (args.host, args.port)
    server = ThreadingHTTPServer(address, ArtifactHandler)
    print(f"Artifact server listening on http://{args.host}:{args.port}")
    print(f"Data directory: {DATA_DIR}")
    if TOKEN:
        print("Access token authentication is enabled.")
    else:
        print("Access token authentication is disabled.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Artifact server.")


if __name__ == "__main__":
    main()
