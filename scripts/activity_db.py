from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def configured_path(value: str, root: Path = ROOT) -> Path:
    path = Path(os.path.expandvars(value)).expanduser()
    return path if path.is_absolute() else root / path


def rel(path: Path, root: Path = ROOT) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def configured_timezone(config: dict[str, Any]) -> ZoneInfo | None:
    timezone = config.get("timezone")
    if not timezone:
        return None
    try:
        return ZoneInfo(str(timezone))
    except ZoneInfoNotFoundError:
        return None


def database_settings(config: dict[str, Any], root: Path = ROOT) -> dict[str, Any]:
    settings = config.get("database", {})
    if not isinstance(settings, dict):
        settings = {}
    return {
        "enabled": bool(settings.get("enabled", False)),
        "path": configured_path(str(settings.get("path", "journal/activity_journal.sqlite3")), root),
        "text_retention_days": int(settings.get("text_retention_days", 90)),
        "keep_metadata_after_text_prune": bool(settings.get("keep_metadata_after_text_prune", True)),
        "enable_fts": bool(settings.get("enable_fts", True)),
    }


def connect_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.Error:
        pass
    return conn


def open_configured_database(config: dict[str, Any], root: Path = ROOT) -> sqlite3.Connection:
    settings = database_settings(config, root)
    conn = connect_database(settings["path"])
    initialize_database(conn, enable_fts=settings["enable_fts"])
    return conn


def initialize_database(conn: sqlite3.Connection, *, enable_fts: bool = True) -> bool:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            occurred_at TEXT,
            local_date TEXT NOT NULL,
            app TEXT,
            process TEXT,
            title TEXT,
            url TEXT,
            domain TEXT,
            path TEXT,
            text_excerpt TEXT,
            text TEXT,
            content_hash TEXT,
            text_hash TEXT,
            raw_json TEXT,
            raw_path TEXT,
            text_pruned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_events_local_date_source ON events(local_date, source);
        CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events(occurred_at);
        CREATE INDEX IF NOT EXISTS idx_events_domain ON events(domain);
        CREATE INDEX IF NOT EXISTS idx_events_process ON events(process);
        CREATE INDEX IF NOT EXISTS idx_events_content_hash ON events(content_hash);

        CREATE TABLE IF NOT EXISTS daily_snapshots (
            local_date TEXT PRIMARY KEY,
            raw_path TEXT,
            daily_path TEXT,
            raw_hash TEXT,
            daily_hash TEXT,
            event_count INTEGER NOT NULL DEFAULT 0,
            generated_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notion_sync_state (
            state_key TEXT PRIMARY KEY,
            page_id TEXT,
            url TEXT,
            content_hash TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            finalized_at TEXT,
            failed_at TEXT,
            last_error TEXT,
            raw_json TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ingest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL,
            local_date TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            status TEXT NOT NULL,
            event_count INTEGER NOT NULL DEFAULT 0,
            warning_count INTEGER NOT NULL DEFAULT 0,
            warnings TEXT
        );
        """
    )
    fts_ready = False
    if enable_fts:
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS event_fts
                USING fts5(event_id UNINDEXED, title, text_excerpt, text)
                """
            )
            fts_ready = True
        except sqlite3.Error:
            fts_ready = False
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
        (SCHEMA_VERSION, "initial_activity_journal_sqlite", utc_now()),
    )
    conn.commit()
    return fts_ready


def current_schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
    except sqlite3.Error:
        return 0
    return int(row["version"] or 0) if row else 0


def has_table(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? AND type IN ('table', 'virtual table')",
        (table_name,),
    ).fetchone()
    return row is not None


def fts_available(conn: sqlite3.Connection) -> bool:
    return has_table(conn, "event_fts")


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def event_id_for(event: dict[str, Any]) -> str:
    explicit = str(event.get("id") or "").strip()
    if explicit:
        return explicit
    source = str(event.get("source") or "unknown")
    content_hash = str(event.get("content_hash") or event.get("text_hash") or "").strip()
    if content_hash:
        return stable_hash(f"{source}\n{event.get('local_date')}\n{content_hash}")
    basis = {
        "source": source,
        "local_date": event.get("local_date"),
        "occurred_at": event.get("occurred_at"),
        "title": event.get("title"),
        "url": event.get("url"),
        "path": event.get("path"),
        "process": event.get("process"),
        "raw_path": event.get("raw_path"),
    }
    return stable_hash(json_dumps(basis))


def normalize_text(value: Any, max_chars: int | None = None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if max_chars is not None and max_chars > 0 and len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


def normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    local_date = normalize_text(event.get("local_date"))
    source = normalize_text(event.get("source"))
    if not local_date:
        raise ValueError("event.local_date is required")
    if not source:
        raise ValueError("event.source is required")
    raw_json = event.get("raw_json")
    if raw_json is not None and not isinstance(raw_json, str):
        raw_json = json_dumps(raw_json)
    normalized = {
        "id": event_id_for(event),
        "source": source,
        "occurred_at": normalize_text(event.get("occurred_at")),
        "local_date": local_date,
        "app": normalize_text(event.get("app"), 120),
        "process": normalize_text(event.get("process"), 120),
        "title": normalize_text(event.get("title"), 300),
        "url": normalize_text(event.get("url"), 3000),
        "domain": normalize_text(event.get("domain"), 300),
        "path": normalize_text(event.get("path"), 3000),
        "text_excerpt": normalize_text(event.get("text_excerpt"), 1200),
        "text": normalize_text(event.get("text")),
        "content_hash": normalize_text(event.get("content_hash")),
        "text_hash": normalize_text(event.get("text_hash")),
        "raw_json": raw_json,
        "raw_path": normalize_text(event.get("raw_path"), 3000),
        "text_pruned": int(bool(event.get("text_pruned", False))),
    }
    if normalized["text"] is None and normalized["text_excerpt"] is None:
        normalized["text_excerpt"] = normalize_text(event.get("summary"), 1200)
    return normalized


def upsert_event(conn: sqlite3.Connection, event: dict[str, Any]) -> str:
    normalized = normalize_event(event)
    now_value = utc_now()
    conn.execute(
        """
        INSERT INTO events (
            id, source, occurred_at, local_date, app, process, title, url, domain, path,
            text_excerpt, text, content_hash, text_hash, raw_json, raw_path, text_pruned,
            created_at, updated_at
        )
        VALUES (
            :id, :source, :occurred_at, :local_date, :app, :process, :title, :url, :domain, :path,
            :text_excerpt, :text, :content_hash, :text_hash, :raw_json, :raw_path, :text_pruned,
            :created_at, :updated_at
        )
        ON CONFLICT(id) DO UPDATE SET
            source = excluded.source,
            occurred_at = excluded.occurred_at,
            local_date = excluded.local_date,
            app = excluded.app,
            process = excluded.process,
            title = excluded.title,
            url = excluded.url,
            domain = excluded.domain,
            path = excluded.path,
            text_excerpt = excluded.text_excerpt,
            text = excluded.text,
            content_hash = excluded.content_hash,
            text_hash = excluded.text_hash,
            raw_json = excluded.raw_json,
            raw_path = excluded.raw_path,
            text_pruned = excluded.text_pruned,
            updated_at = excluded.updated_at
        """,
        {**normalized, "created_at": now_value, "updated_at": now_value},
    )
    refresh_fts(conn, normalized)
    return str(normalized["id"])


def refresh_fts(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    if not fts_available(conn):
        return
    try:
        conn.execute("DELETE FROM event_fts WHERE event_id = ?", (event["id"],))
        if event.get("text_pruned"):
            return
        if any(event.get(key) for key in ("title", "text_excerpt", "text")):
            conn.execute(
                "INSERT INTO event_fts(event_id, title, text_excerpt, text) VALUES (?, ?, ?, ?)",
                (
                    event["id"],
                    event.get("title") or "",
                    event.get("text_excerpt") or "",
                    event.get("text") or "",
                ),
            )
    except sqlite3.Error:
        return


def upsert_events(conn: sqlite3.Connection, events: list[dict[str, Any]]) -> int:
    count = 0
    with conn:
        for event in events:
            upsert_event(conn, event)
            count += 1
    return count


def file_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    return stable_hash(path.read_text(encoding="utf-8", errors="replace"))


def upsert_daily_snapshot(
    conn: sqlite3.Connection,
    *,
    local_date: str,
    raw_path: Path,
    daily_path: Path,
    event_count: int,
    root: Path = ROOT,
) -> None:
    now_value = utc_now()
    conn.execute(
        """
        INSERT INTO daily_snapshots (
            local_date, raw_path, daily_path, raw_hash, daily_hash, event_count, generated_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(local_date) DO UPDATE SET
            raw_path = excluded.raw_path,
            daily_path = excluded.daily_path,
            raw_hash = excluded.raw_hash,
            daily_hash = excluded.daily_hash,
            event_count = excluded.event_count,
            updated_at = excluded.updated_at
        """,
        (
            local_date,
            rel(raw_path, root),
            rel(daily_path, root),
            file_hash(raw_path),
            file_hash(daily_path),
            event_count,
            now_value,
            now_value,
        ),
    )
    conn.commit()


def mirror_notion_state(conn: sqlite3.Connection, state_path: Path) -> int:
    if not state_path.is_file():
        return 0
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(state, dict):
        return 0
    count = 0
    now_value = utc_now()
    with conn:
        for key, value in state.items():
            if not isinstance(key, str) or key.startswith(("hash:", "url:", "finalized:", "failed:", "failed_at:")):
                continue
            page_id = str(value) if value else None
            status = "pending"
            if state.get(f"finalized:{key}"):
                status = "finalized"
            elif state.get(f"failed:{key}"):
                status = "failed"
            raw = {
                "state_key": key,
                "page_id": page_id,
                "hash": state.get(f"hash:{key}"),
                "url": state.get(f"url:{key}"),
                "finalized_at": state.get(f"finalized:{key}"),
                "failed": state.get(f"failed:{key}"),
                "failed_at": state.get(f"failed_at:{key}"),
            }
            conn.execute(
                """
                INSERT INTO notion_sync_state (
                    state_key, page_id, url, content_hash, status, finalized_at,
                    failed_at, last_error, raw_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    page_id = excluded.page_id,
                    url = excluded.url,
                    content_hash = excluded.content_hash,
                    status = excluded.status,
                    finalized_at = excluded.finalized_at,
                    failed_at = excluded.failed_at,
                    last_error = excluded.last_error,
                    raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                """,
                (
                    key,
                    page_id,
                    state.get(f"url:{key}"),
                    state.get(f"hash:{key}"),
                    status,
                    state.get(f"finalized:{key}"),
                    state.get(f"failed_at:{key}"),
                    state.get(f"failed:{key}"),
                    json_dumps(raw),
                    now_value,
                ),
            )
            count += 1
    return count


def record_ingest_run(
    conn: sqlite3.Connection,
    *,
    mode: str,
    local_date: str | None,
    started_at: str,
    status: str,
    event_count: int,
    warnings: list[str],
) -> None:
    conn.execute(
        """
        INSERT INTO ingest_runs (
            mode, local_date, started_at, finished_at, status, event_count, warning_count, warnings
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (mode, local_date, started_at, utc_now(), status, event_count, len(warnings), json_dumps(warnings)),
    )
    conn.commit()


def prune_old_text(conn: sqlite3.Connection, *, cutoff_date: dt.date) -> int:
    rows = conn.execute(
        """
        SELECT id FROM events
        WHERE local_date < ?
          AND text_pruned = 0
          AND (text IS NOT NULL OR raw_json IS NOT NULL)
        """,
        (cutoff_date.isoformat(),),
    ).fetchall()
    ids = [str(row["id"]) for row in rows]
    if not ids:
        return 0
    has_fts = fts_available(conn)
    with conn:
        if has_fts:
            for event_id in ids:
                conn.execute("DELETE FROM event_fts WHERE event_id = ?", (event_id,))
        conn.executemany(
            "UPDATE events SET text = NULL, raw_json = NULL, text_pruned = 1, updated_at = ? WHERE id = ?",
            [(utc_now(), event_id) for event_id in ids],
        )
    return len(ids)


def count_prunable_text(conn: sqlite3.Connection, *, cutoff_date: dt.date) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count FROM events
        WHERE local_date < ?
          AND text_pruned = 0
          AND (text IS NOT NULL OR raw_json IS NOT NULL)
        """,
        (cutoff_date.isoformat(),),
    ).fetchone()
    return int(row["count"] or 0) if row else 0


def retention_cutoff(config: dict[str, Any], root: Path = ROOT, now_value: dt.datetime | None = None) -> dt.date | None:
    settings = database_settings(config, root)
    days = int(settings["text_retention_days"])
    if days <= 0:
        return None
    timezone = configured_timezone(config)
    current = now_value or dt.datetime.now(timezone or dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone or dt.timezone.utc)
    elif timezone:
        current = current.astimezone(timezone)
    return current.date() - dt.timedelta(days=days)


def apply_configured_retention(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    root: Path = ROOT,
    now_value: dt.datetime | None = None,
) -> int:
    cutoff = retention_cutoff(config, root, now_value)
    if cutoff is None:
        return 0
    return prune_old_text(conn, cutoff_date=cutoff)


def quote_fts_query(query: str) -> str:
    terms = [term.strip() for term in query.split() if term.strip()]
    if not terms:
        return '""'
    return " ".join(f'"{term.replace(chr(34), chr(34) + chr(34))}"' for term in terms)


def row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def search_events(
    conn: sqlite3.Connection,
    query: str,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    date_clauses: list[str] = []
    date_params: list[Any] = []
    if from_date:
        date_clauses.append("e.local_date >= ?")
        date_params.append(from_date)
    if to_date:
        date_clauses.append("e.local_date <= ?")
        date_params.append(to_date)
    where_dates = (" AND " + " AND ".join(date_clauses)) if date_clauses else ""
    if fts_available(conn):
        try:
            rows = conn.execute(
                f"""
                SELECT e.* FROM event_fts f
                JOIN events e ON e.id = f.event_id
                WHERE event_fts MATCH ?{where_dates}
                ORDER BY COALESCE(e.occurred_at, e.local_date) DESC
                LIMIT ?
                """,
                [quote_fts_query(query), *date_params, limit],
            ).fetchall()
            return [row_to_event(row) for row in rows]
        except sqlite3.Error:
            pass

    like = f"%{query}%"
    clauses = [
        "(e.title LIKE ? OR e.text_excerpt LIKE ? OR e.text LIKE ? OR e.url LIKE ? OR e.domain LIKE ? OR e.app LIKE ? OR e.process LIKE ? OR e.path LIKE ?)"
    ]
    params: list[Any] = [like] * 8
    if from_date:
        clauses.append("e.local_date >= ?")
        params.append(from_date)
    if to_date:
        clauses.append("e.local_date <= ?")
        params.append(to_date)
    rows = conn.execute(
        f"""
        SELECT e.* FROM events e
        WHERE {' AND '.join(clauses)}
        ORDER BY COALESCE(e.occurred_at, e.local_date) DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    return [row_to_event(row) for row in rows]


def database_size_mb(path: Path) -> float:
    size = 0
    for candidate in [path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")]:
        if candidate.exists():
            try:
                size += candidate.stat().st_size
            except OSError:
                pass
    return round(size / 1_048_576, 3)


def inspect_database(config: dict[str, Any], *, root: Path = ROOT, day: dt.date | None = None) -> dict[str, Any]:
    settings = database_settings(config, root)
    path = settings["path"]
    if not settings["enabled"]:
        return {
            "status": "Skipped",
            "enabled": False,
            "path": rel(path, root),
            "recommended_actions": [],
        }
    if not path.exists():
        action = "python scripts\\sync_sqlite.py --backfill"
        if day:
            action = f"python scripts\\sync_sqlite.py --date {day.isoformat()}"
        return {
            "status": "Warning",
            "enabled": True,
            "path": rel(path, root),
            "exists": False,
            "recommended_actions": [action],
        }
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            version = current_schema_version(conn)
            missing_tables = [
                table
                for table in ["events", "daily_snapshots", "notion_sync_state", "ingest_runs"]
                if not has_table(conn, table)
            ]
            event_count = int(conn.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]) if not missing_tables else 0
            checked_event_count = None
            snapshot = None
            if day and not missing_tables:
                checked_date = day.isoformat()
                checked_event_count = int(
                    conn.execute("SELECT COUNT(*) AS count FROM events WHERE local_date = ?", (checked_date,)).fetchone()["count"]
                )
                snapshot_row = conn.execute(
                    "SELECT * FROM daily_snapshots WHERE local_date = ?",
                    (checked_date,),
                ).fetchone()
                snapshot = row_to_event(snapshot_row) if snapshot_row else None
            last_run = None
            if has_table(conn, "ingest_runs"):
                row = conn.execute("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT 1").fetchone()
                last_run = row_to_event(row) if row else None
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {
            "status": "Action Needed",
            "enabled": True,
            "path": rel(path, root),
            "exists": True,
            "error": str(exc),
            "recommended_actions": ["Inspect or rebuild the SQLite database with python scripts\\sync_sqlite.py --backfill"],
        }

    status = "OK"
    actions: list[str] = []
    if version < SCHEMA_VERSION or missing_tables:
        status = "Action Needed"
        actions.append("Rebuild or migrate the SQLite database with python scripts\\sync_sqlite.py --backfill")
    if day and snapshot is None:
        status = "Warning" if status == "OK" else status
        actions.append(f"python scripts\\sync_sqlite.py --date {day.isoformat()}")
    if last_run and last_run.get("status") == "failed":
        status = "Warning" if status == "OK" else status
        date_arg = f" --date {last_run['local_date']}" if last_run.get("local_date") else ""
        actions.append(f"python scripts\\sync_sqlite.py{date_arg}".strip())

    return {
        "status": status,
        "enabled": True,
        "path": rel(path, root),
        "exists": True,
        "schema_version": version,
        "expected_schema_version": SCHEMA_VERSION,
        "missing_tables": missing_tables,
        "fts_available": not missing_tables and bool(path.exists()) and inspect_fts_available(path),
        "event_count": event_count,
        "checked_event_count": checked_event_count,
        "daily_snapshot": snapshot,
        "last_ingest_run": last_run,
        "text_retention_days": settings["text_retention_days"],
        "size_mb": database_size_mb(path),
        "recommended_actions": actions,
    }


def inspect_fts_available(path: Path) -> bool:
    try:
        conn = sqlite3.connect(path)
        try:
            return has_table(conn, "event_fts")
        finally:
            conn.close()
    except sqlite3.Error:
        return False
