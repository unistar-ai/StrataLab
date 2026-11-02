"""SQLite storage for the gateway audit, request contents and images.

One file (strata-lab.db) replaces gateway-audit.jsonl, gateway-content.jsonl and
the gateway-images folder. The API of this module is what GatewayService calls;
rows keep the same field names the UI already renders, so nothing above this
layer has to know the storage changed.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS requests (
        id TEXT PRIMARY KEY,
        ts REAL NOT NULL,
        key_id TEXT,
        key_name TEXT,
        route TEXT,
        method TEXT,
        model TEXT,
        requested_model TEXT,
        low_resource INTEGER,
        status INTEGER,
        ms INTEGER,
        context_tokens INTEGER,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        cached_tokens INTEGER,
        total_tokens INTEGER,
        cost_cny REAL,
        cost_usd REAL,
        stream INTEGER,
        error TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts)",
    "CREATE INDEX IF NOT EXISTS requests_key_ts ON requests(key_id, ts)",
    "CREATE INDEX IF NOT EXISTS requests_status_ts ON requests(status, ts)",
    """CREATE TABLE IF NOT EXISTS contents (
        id TEXT PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
        request TEXT,
        response TEXT,
        reasoning TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS images (
        request_id TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        mime TEXT,
        size INTEGER,
        bytes BLOB,
        PRIMARY KEY (request_id, name)
    )""",
    "CREATE INDEX IF NOT EXISTS images_request ON images(request_id)",
)

FIELDS = ("id", "ts", "key_id", "key_name", "route", "method", "model", "requested_model",
          "low_resource", "status", "ms", "context_tokens", "prompt_tokens",
          "completion_tokens", "cached_tokens", "total_tokens", "cost_cny", "cost_usd",
          "stream", "error")


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self.lock:
            if self.conn.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
                # auto_vacuum only takes effect before the first table exists, so a
                # database created without it needs one VACUUM to switch modes.
                self.conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
                self.conn.execute("VACUUM")
            for statement in SCHEMA:
                self.conn.execute(statement)
            self.conn.commit()

    # ------------------------------------------------------------------ writes
    def add(self, row, content=None):
        values = [row.get(name) for name in FIELDS]
        values = [int(value) if isinstance(value, bool) else value for value in values]
        updates = ", ".join(f"{name} = excluded.{name}" for name in FIELDS if name != "id")
        with self.lock, self.conn:
            self.conn.execute(
                f"INSERT INTO requests ({', '.join(FIELDS)}) "
                f"VALUES ({', '.join('?' for _ in FIELDS)}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}", values)
            if content is not None:
                self.conn.execute(
                    "INSERT INTO contents (id, request, response, reasoning) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET request = excluded.request, "
                    "response = excluded.response, reasoning = excluded.reasoning",
                    (row["id"], content.get("request") or "",
                     content.get("response") or "", content.get("reasoning") or ""))

    def add_image(self, request_id, name, mime, raw):
        # A picture is written while the request is still being handled, before
        # its audit row exists; the stub keeps the foreign key valid and the
        # audit row fills the same id in later.
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO requests (id, ts, status) VALUES (?, ?, 0)",
                (request_id, time.time()))
            self.conn.execute(
                "INSERT INTO images (request_id, name, mime, size, bytes) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(request_id, name) DO UPDATE SET mime = excluded.mime, "
                "size = excluded.size, bytes = excluded.bytes",
                (request_id, name, mime or "", len(raw), sqlite3.Binary(raw)))

    # ------------------------------------------------------------------- reads
    def recent(self, key_id=None, limit=100):
        sql = "SELECT * FROM requests"
        args = []
        if key_id:
            sql += " WHERE key_id = ?"
            args.append(key_id)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(int(limit))
        with self.lock:
            return [dict(row) for row in self.conn.execute(sql, args)]

    def content(self, audit_id):
        with self.lock:
            row = self.conn.execute(
                "SELECT r.id, r.ts, r.key_name, r.key_id, r.model, r.status, "
                "c.request, c.response, c.reasoning "
                "FROM requests r LEFT JOIN contents c ON c.id = r.id WHERE r.id = ?",
                (audit_id,)).fetchone()
            return dict(row) if row else None

    def image(self, request_id, name):
        with self.lock:
            row = self.conn.execute(
                "SELECT mime, bytes FROM images WHERE request_id = ? AND name = ?",
                (request_id, name)).fetchone()
        if row is None:
            return None
        return row["mime"] or "application/octet-stream", row["bytes"]

    def counts(self):
        with self.lock:
            requests = self.conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            contents = self.conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0]
            images = self.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
            size = self.conn.execute("SELECT COALESCE(SUM(size), 0) FROM images").fetchone()[0]
        return {"requests": requests, "contents": contents, "images": images, "image_bytes": size}

    # ------------------------------------------------------------------ prune
    def prune(self, text_days, max_rows, image_days, image_max_bytes):
        removed = {"requests": 0, "images": 0, "image_bytes": 0}
        now = time.time()
        with self.lock, self.conn:
            cutoff = now - max(1, int(text_days)) * 86400
            removed["requests"] += self.conn.execute(
                "DELETE FROM requests WHERE ts < ?", (cutoff,)).rowcount
            cutoff = now - max(1, int(image_days)) * 86400
            removed["requests"] += self.conn.execute(
                "DELETE FROM requests WHERE ts < ?", (cutoff,)).rowcount
            if max_rows > 0:
                edge = self.conn.execute(
                    "SELECT ts FROM requests ORDER BY ts DESC LIMIT 1 OFFSET ?",
                    (int(max_rows),)).fetchone()
                if edge is not None:
                    removed["requests"] += self.conn.execute(
                        "DELETE FROM requests WHERE ts <= ?", (edge["ts"],)).rowcount
            if image_max_bytes > 0:
                rows = self.conn.execute(
                    "SELECT i.request_id, MAX(r.ts) AS ts FROM images i "
                    "JOIN requests r ON r.id = i.request_id "
                    "GROUP BY i.request_id ORDER BY ts ASC").fetchall()
                total = self.conn.execute(
                    "SELECT COALESCE(SUM(size), 0) FROM images").fetchone()[0]
                for item in rows:
                    if total <= image_max_bytes:
                        break
                    size = self.conn.execute(
                        "SELECT COALESCE(SUM(size), 0) FROM images WHERE request_id = ?",
                        (item["request_id"],)).fetchone()[0]
                    removed["requests"] += self.conn.execute(
                        "DELETE FROM requests WHERE id = ?", (item["request_id"],)).rowcount
                    total -= size
                    removed["images"] += 1
            self.conn.execute("PRAGMA incremental_vacuum")
        return removed

    def clear(self):
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM images")
            self.conn.execute("DELETE FROM contents")
            self.conn.execute("DELETE FROM requests")
            self.conn.execute("PRAGMA incremental_vacuum")

    # --------------------------------------------------------------- migration
    def migrate(self, audit_files, content_files, image_dir):
        """Import the old JSONL audit/content files and the image folder once."""
        requests = {}
        for path in audit_files:
            try:
                handle = Path(path).open(encoding="utf-8")
            except OSError:
                continue
            with handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict) and row.get("id"):
                        requests[row["id"]] = row

        contents = {}
        for path in content_files:
            try:
                handle = Path(path).open(encoding="utf-8")
            except OSError:
                continue
            with handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict) and row.get("id"):
                        contents[row["id"]] = row

        for audit_id, row in contents.items():
            if audit_id not in requests:
                requests[audit_id] = {
                    "id": audit_id, "ts": row.get("ts") or 0.0,
                    "key_id": row.get("key_id"), "key_name": row.get("key_name"),
                    "model": row.get("model"), "status": row.get("status"),
                }

        images = []
        folder = Path(image_dir)
        if folder.is_dir():
            for request_dir in folder.iterdir():
                if not request_dir.is_dir():
                    continue
                for item in request_dir.iterdir():
                    if item.is_file() and item.name.startswith("img-"):
                        try:
                            images.append((request_dir.name, item.name, item.read_bytes(),
                                           item.stat().st_mtime))
                        except OSError:
                            continue

        # Pictures can outlive the audit row when the lab was killed mid-request;
        # keep them with a placeholder row so nothing is silently dropped.
        orphans = 0
        for request_id, _name, _raw, stamp in images:
            if request_id not in requests:
                requests[request_id] = {"id": request_id, "ts": stamp, "key_name": "—",
                                        "status": 0, "error": "无审计记录的图（导入）"}
                orphans += 1

        with self.lock, self.conn:
            for audit_id, row in requests.items():
                values = [row.get(name) for name in FIELDS]
                values = [int(value) if isinstance(value, bool) else value for value in values]
                self.conn.execute(
                    f"INSERT OR REPLACE INTO requests ({', '.join(FIELDS)}) "
                    f"VALUES ({', '.join('?' for _ in FIELDS)})", values)
            for audit_id, row in contents.items():
                self.conn.execute(
                    "INSERT OR REPLACE INTO contents (id, request, response, reasoning) "
                    "VALUES (?, ?, ?, ?)",
                    (audit_id, row.get("request") or "", row.get("response") or "",
                     row.get("reasoning") or ""))
            stored = 0
            for request_id, name, raw, _stamp in images:
                mime = "image/jpeg" if name.endswith(".jpg") else "image/png"
                self.conn.execute(
                    "INSERT OR REPLACE INTO images (request_id, name, mime, size, bytes) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (request_id, name, mime, len(raw), sqlite3.Binary(raw)))
                stored += 1
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"requests": len(requests), "contents": len(contents),
                "images": stored, "images_found": len(images), "orphan_images": orphans}
