from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any

import activity_db
import collect_daily


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "activity-journal.json"


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def date_range(start: dt.date, end: dt.date) -> list[dt.date]:
    if end < start:
        raise ValueError("--to-date must be on or after --from-date")
    days = []
    current = start
    while current <= end:
        days.append(current)
        current += dt.timedelta(days=1)
    return days


def available_daily_dates(root: Path = ROOT) -> list[dt.date]:
    dates: list[dt.date] = []
    raw_dir = root / "journal" / "raw"
    if not raw_dir.exists():
        return dates
    for path in raw_dir.glob("*.json"):
        try:
            dates.append(dt.date.fromisoformat(path.stem))
        except ValueError:
            continue
    return sorted(set(dates))


def parse_timestamp(value: Any) -> dt.datetime | None:
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    return None


def iso_timestamp(value: Any) -> str | None:
    parsed = parse_timestamp(value)
    return parsed.isoformat(timespec="seconds") if parsed else None


def local_date_for(value: Any, config: dict[str, Any], fallback: str) -> str:
    parsed = parse_timestamp(value)
    if not parsed:
        return fallback
    timezone = activity_db.configured_timezone(config)
    if timezone:
        parsed = parsed.astimezone(timezone)
    elif parsed.tzinfo:
        parsed = parsed.astimezone()
    return parsed.date().isoformat()


def text_excerpt(value: Any, max_chars: int = 700) -> str | None:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def domain_for_url(url: Any) -> str | None:
    text = str(url or "").strip()
    if not text:
        return None
    parsed = urllib.parse.urlparse(text)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain or None


def event(
    *,
    source: str,
    local_date: str,
    raw_path: Path,
    root: Path,
    raw: dict[str, Any],
    occurred_at: Any = None,
    app: Any = None,
    process: Any = None,
    title: Any = None,
    url: Any = None,
    domain: Any = None,
    path: Any = None,
    text: Any = None,
    text_excerpt_value: Any = None,
    content_hash: Any = None,
    text_hash: Any = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    text_value = str(text or "").strip()
    return {
        "id": event_id,
        "source": source,
        "occurred_at": iso_timestamp(occurred_at),
        "local_date": local_date,
        "app": app,
        "process": process,
        "title": title,
        "url": url,
        "domain": domain or domain_for_url(url),
        "path": path,
        "text_excerpt": text_excerpt_value or text_excerpt(text_value),
        "text": text_value or None,
        "content_hash": content_hash,
        "text_hash": text_hash,
        "raw_json": raw,
        "raw_path": activity_db.rel(raw_path, root),
    }


def daily_events(raw: dict[str, Any], raw_path: Path, config: dict[str, Any], root: Path = ROOT) -> list[dict[str, Any]]:
    local_date = str(raw.get("date") or raw_path.stem)
    events: list[dict[str, Any]] = []

    codex = raw.get("codex", {}) if isinstance(raw.get("codex"), dict) else {}
    for row in codex.get("history", []) if isinstance(codex.get("history"), list) else []:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "")
        occurred = row.get("ts")
        events.append(
            event(
                source="codex_history",
                local_date=local_date_for(occurred, config, local_date),
                raw_path=raw_path,
                root=root,
                raw=row,
                occurred_at=occurred,
                app="Codex",
                title=text_excerpt(text, 160) or str(row.get("session_id") or "Codex history"),
                path=str(row.get("session_id") or ""),
                text=text,
                content_hash=activity_db.stable_hash(f"{row.get('session_id')}\n{row.get('ts')}\n{text}"),
            )
        )
    for row in codex.get("sessions", []) if isinstance(codex.get("sessions"), list) else []:
        if not isinstance(row, dict):
            continue
        session_id = str(row.get("id") or row.get("session_id") or "")
        occurred = row.get("updated_at")
        events.append(
            event(
                source="codex_session",
                local_date=local_date_for(occurred, config, local_date),
                raw_path=raw_path,
                root=root,
                raw=row,
                occurred_at=occurred,
                app="Codex",
                title=f"Codex session {session_id}" if session_id else "Codex session",
                path=session_id,
                content_hash=activity_db.stable_hash(activity_db.json_dumps(row)),
            )
        )

    external = raw.get("external_inputs", {}) if isinstance(raw.get("external_inputs"), dict) else {}
    for row in external.get("inbox", []) if isinstance(external.get("inbox"), list) else []:
        if isinstance(row, dict):
            events.append(
                event(
                    source="inbox",
                    local_date=local_date_for(row.get("modified_at"), config, local_date),
                    raw_path=raw_path,
                    root=root,
                    raw=row,
                    occurred_at=row.get("modified_at"),
                    title=row.get("title"),
                    path=row.get("path"),
                    text=row.get("text"),
                    content_hash=activity_db.stable_hash(activity_db.json_dumps(row)),
                )
            )
    for row in external.get("chatgpt", []) if isinstance(external.get("chatgpt"), list) else []:
        if isinstance(row, dict):
            occurred = row.get("update_time") or row.get("create_time")
            events.append(
                event(
                    source="chatgpt_export",
                    local_date=local_date_for(occurred, config, local_date),
                    raw_path=raw_path,
                    root=root,
                    raw=row,
                    occurred_at=occurred,
                    app="ChatGPT",
                    title=row.get("title"),
                    path=row.get("source_path"),
                    text=row.get("text"),
                    content_hash=activity_db.stable_hash(activity_db.json_dumps(row)),
                )
            )
    for row in external.get("chatgpt_live", []) if isinstance(external.get("chatgpt_live"), list) else []:
        if isinstance(row, dict):
            occurred = row.get("captured_at")
            events.append(
                event(
                    source="chatgpt_live",
                    local_date=local_date_for(occurred, config, local_date),
                    raw_path=raw_path,
                    root=root,
                    raw=row,
                    occurred_at=occurred,
                    app=row.get("app"),
                    title=row.get("title"),
                    url=row.get("url"),
                    path=row.get("conversation_id") or row.get("source_path"),
                    text=row.get("text"),
                    content_hash=row.get("content_hash") or activity_db.stable_hash(activity_db.json_dumps(row)),
                )
            )
    for row in external.get("browser_history", []) if isinstance(external.get("browser_history"), list) else []:
        if isinstance(row, dict):
            occurred = row.get("last_visit_time") or row.get("first_visit_time")
            events.append(
                event(
                    source="browser_history",
                    local_date=local_date_for(occurred, config, local_date),
                    raw_path=raw_path,
                    root=root,
                    raw=row,
                    occurred_at=occurred,
                    app=row.get("profile"),
                    title=row.get("title"),
                    url=row.get("url"),
                    domain=row.get("domain"),
                    path=row.get("source_path"),
                    text_excerpt_value=row.get("url"),
                    content_hash=activity_db.stable_hash(f"{row.get('url')}\n{occurred}"),
                )
            )
    for row in external.get("recent_files", []) if isinstance(external.get("recent_files"), list) else []:
        if isinstance(row, dict):
            occurred = row.get("modified_at")
            events.append(
                event(
                    source="recent_file",
                    local_date=local_date_for(occurred, config, local_date),
                    raw_path=raw_path,
                    root=root,
                    raw=row,
                    occurred_at=occurred,
                    title=row.get("title"),
                    path=row.get("path"),
                    text_excerpt_value=row.get("path"),
                    content_hash=activity_db.stable_hash(activity_db.json_dumps(row)),
                )
            )
    for row in external.get("activity_watch", []) if isinstance(external.get("activity_watch"), list) else []:
        if isinstance(row, dict):
            occurred = row.get("last_seen") or row.get("first_seen")
            text = row.get("text")
            events.append(
                event(
                    source="activity_watch",
                    local_date=local_date_for(occurred, config, local_date),
                    raw_path=raw_path,
                    root=root,
                    raw=row,
                    occurred_at=occurred,
                    process=row.get("process"),
                    title=row.get("title"),
                    text=text,
                    text_excerpt_value=row.get("text_excerpt"),
                    text_hash=",".join(str(value) for value in row.get("text_hashes", []) if value) or None,
                    content_hash=activity_db.stable_hash(activity_db.json_dumps(row)),
                )
            )

    for item in raw.get("git_activity", []) if isinstance(raw.get("git_activity"), list) else []:
        if not isinstance(item, dict):
            continue
        text = "\n".join(str(value) for value in [*item.get("commits", []), *item.get("changed_files", [])])
        title = f"{item.get('project') or item.get('repo')}: git activity"
        events.append(
            event(
                source="git_activity",
                local_date=local_date,
                raw_path=raw_path,
                root=root,
                raw=item,
                title=title,
                path=item.get("repo"),
                text=text,
                text_excerpt_value=text_excerpt(text),
                content_hash=activity_db.stable_hash(activity_db.json_dumps(item)),
            )
        )

    modified_files = raw.get("modified_files", {})
    if isinstance(modified_files, dict):
        for project, files in modified_files.items():
            if not isinstance(files, list) or not files:
                continue
            text = "\n".join(str(path) for path in files)
            item = {"project": project, "files": files}
            events.append(
                event(
                    source="modified_files",
                    local_date=local_date,
                    raw_path=raw_path,
                    root=root,
                    raw=item,
                    title=f"{project}: {len(files)} modified file(s)",
                    text=text,
                    text_excerpt_value=text_excerpt(text),
                    content_hash=activity_db.stable_hash(activity_db.json_dumps(item)),
                )
            )
    return events


def read_jsonl_events(path: Path, source: str, day: dt.date, config: dict[str, Any], root: Path = ROOT) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.exists():
        return [], []
    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [], [f"JSONL unreadable: {path} ({exc})"]
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            warnings.append(f"Malformed JSONL skipped: {activity_db.rel(path, root)}:{line_number}")
            continue
        if not isinstance(row, dict):
            warnings.append(f"Non-object JSONL skipped: {activity_db.rel(path, root)}:{line_number}")
            continue
        if source == "chatgpt_live_raw":
            occurred = row.get("captured_at") or row.get("received_at")
            if local_date_for(occurred, config, day.isoformat()) != day.isoformat():
                continue
            text = str(row.get("text") or "")
            events.append(
                event(
                    source=source,
                    local_date=day.isoformat(),
                    raw_path=path,
                    root=root,
                    raw=row,
                    occurred_at=occurred,
                    app=row.get("app"),
                    title=row.get("title"),
                    url=row.get("url"),
                    path=row.get("conversation_id"),
                    text=text,
                    content_hash=row.get("content_hash") or activity_db.stable_hash(f"{row.get('url')}\n{text}"),
                )
            )
        elif source == "activity_watch_sample":
            occurred = row.get("ts")
            if local_date_for(occurred, config, day.isoformat()) != day.isoformat():
                continue
            text = str(row.get("text") or "")
            events.append(
                event(
                    source=source,
                    local_date=day.isoformat(),
                    raw_path=path,
                    root=root,
                    raw=row,
                    occurred_at=occurred,
                    process=row.get("process"),
                    title=row.get("title"),
                    text=text,
                    text_excerpt_value=text_excerpt(text),
                    text_hash=row.get("text_hash"),
                    content_hash=activity_db.stable_hash(f"{occurred}\n{row.get('process')}\n{row.get('title')}\n{row.get('text_hash') or text}"),
                )
            )
    return events, warnings


def configured_log_path(config: dict[str, Any], source: str, root: Path = ROOT) -> Path:
    external = config.get("external_inputs", {})
    settings = external.get(source, {}) if isinstance(external, dict) else {}
    default = f"journal/raw/{source}.jsonl"
    if isinstance(settings, dict):
        return collect_daily.configured_path(str(settings.get("log_path", default)), root)
    return collect_daily.configured_path(default, root)


def sync_date(day: dt.date, config: dict[str, Any], root: Path = ROOT) -> dict[str, Any]:
    settings = activity_db.database_settings(config, root)
    if not settings["enabled"]:
        return {"status": "Skipped", "date": day.isoformat(), "event_count": 0, "warnings": ["SQLite database is disabled."]}

    started_at = activity_db.utc_now()
    warnings: list[str] = []
    event_count = 0
    conn = activity_db.open_configured_database(config, root)
    try:
        raw_path = root / "journal" / "raw" / f"{day.isoformat()}.json"
        daily_path = root / "journal" / "daily" / f"{day.isoformat()}.md"
        events: list[dict[str, Any]] = []
        if raw_path.is_file():
            try:
                raw = json.loads(raw_path.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError as exc:
                warnings.append(f"Daily raw JSON unreadable: {activity_db.rel(raw_path, root)} ({exc})")
            else:
                if isinstance(raw, dict):
                    events.extend(daily_events(raw, raw_path, config, root))
                else:
                    warnings.append(f"Daily raw JSON has unexpected shape: {activity_db.rel(raw_path, root)}")
        else:
            warnings.append(f"Daily raw JSON missing: {activity_db.rel(raw_path, root)}")

        chat_log = configured_log_path(config, "chatgpt_live", root)
        watch_log = configured_log_path(config, "activity_watch", root)
        chat_events, chat_warnings = read_jsonl_events(chat_log, "chatgpt_live_raw", day, config, root)
        watch_events, watch_warnings = read_jsonl_events(watch_log, "activity_watch_sample", day, config, root)
        events.extend(chat_events)
        events.extend(watch_events)
        warnings.extend(chat_warnings)
        warnings.extend(watch_warnings)

        activity_db.upsert_events(conn, events)
        event_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM events WHERE local_date = ?",
                (day.isoformat(),),
            ).fetchone()[0]
        )
        if raw_path.is_file():
            activity_db.upsert_daily_snapshot(
                conn,
                local_date=day.isoformat(),
                raw_path=raw_path,
                daily_path=daily_path,
                event_count=event_count,
                root=root,
            )
        notion_count = activity_db.mirror_notion_state(conn, root / "journal" / "raw" / "notion_pages.json")
        pruned_count = activity_db.apply_configured_retention(conn, config, root)
        status = "OK" if not warnings else "Warning"
        activity_db.record_ingest_run(
            conn,
            mode="date",
            local_date=day.isoformat(),
            started_at=started_at,
            status=status.lower(),
            event_count=event_count,
            warnings=warnings,
        )
        return {
            "status": status,
            "date": day.isoformat(),
            "event_count": event_count,
            "notion_state_count": notion_count,
            "pruned_text_count": pruned_count,
            "warnings": warnings,
            "database": activity_db.rel(settings["path"], root),
        }
    except Exception as exc:
        activity_db.record_ingest_run(
            conn,
            mode="date",
            local_date=day.isoformat(),
            started_at=started_at,
            status="failed",
            event_count=event_count,
            warnings=[*warnings, str(exc)],
        )
        raise
    finally:
        conn.close()


def backfill_dates(config: dict[str, Any], from_date: str | None, to_date: str | None, root: Path = ROOT) -> list[dt.date]:
    if from_date or to_date:
        if from_date:
            start = parse_date(from_date)
        else:
            dates = available_daily_dates(root)
            start = dates[0] if dates else collect_daily.parse_date(None, config)
        end = parse_date(to_date) if to_date else collect_daily.parse_date(None, config)
        return date_range(start, end)
    return available_daily_dates(root)


def sync_backfill(config: dict[str, Any], from_date: str | None, to_date: str | None, root: Path = ROOT) -> dict[str, Any]:
    reports = [sync_date(day, config, root) for day in backfill_dates(config, from_date, to_date, root)]
    warnings = [warning for report in reports for warning in report.get("warnings", [])]
    status = "OK" if all(report["status"] in {"OK", "Skipped"} for report in reports) else "Warning"
    return {
        "status": status,
        "date_count": len(reports),
        "event_count": sum(int(report.get("event_count") or 0) for report in reports),
        "warnings": warnings,
        "reports": reports,
    }


def archive_paths(config: dict[str, Any], root: Path = ROOT) -> list[Path]:
    retention = config.get("retention", {})
    archive_dir = activity_db.configured_path(str(retention.get("archive_dir", "journal/archive/raw")), root) if isinstance(retention, dict) else root / "journal" / "archive" / "raw"
    if not archive_dir.exists():
        return []
    return sorted(archive_dir.rglob("*.gz"), key=lambda path: str(path).lower())


def search_archives(config: dict[str, Any], query: str, *, limit: int, root: Path = ROOT) -> list[dict[str, Any]]:
    needle = query.casefold()
    matches: list[dict[str, Any]] = []
    for path in archive_paths(config, root):
        try:
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if needle not in line.casefold():
                        continue
                    matches.append(
                        {
                            "archive_path": activity_db.rel(path, root),
                            "line_number": line_number,
                            "excerpt": text_excerpt(line, 500),
                        }
                    )
                    if len(matches) >= limit:
                        return matches
        except OSError:
            continue
    return matches


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def print_search_results(results: list[dict[str, Any]]) -> None:
    if not results:
        print("No matches.")
        return
    for item in results:
        occurred = item.get("occurred_at") or item.get("local_date")
        label = item.get("title") or item.get("url") or item.get("path") or item.get("id")
        source = item.get("source") or "archive"
        print(f"{occurred} [{source}] {label}")
        detail = item.get("text_excerpt") or item.get("excerpt") or item.get("url") or item.get("path")
        if detail:
            print(f"  {detail}")


def configure_output_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass


def main() -> None:
    configure_output_encoding()
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--date", help="Sync one date, YYYY-MM-DD.")
    group.add_argument("--backfill", action="store_true", help="Sync all available daily raw files, or a date range.")
    group.add_argument("--search", help="Search the SQLite event index.")
    group.add_argument("--archive-search", help="Search compressed raw archives.")
    parser.add_argument("--from-date", help="Start date for backfill/search, YYYY-MM-DD.")
    parser.add_argument("--to-date", help="End date for backfill/search, YYYY-MM-DD.")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--json-only", action="store_true")
    args = parser.parse_args()

    config = load_config()
    try:
        if args.date:
            report = sync_date(parse_date(args.date), config)
            print_json(report) if args.json_only else print(f"SQLite sync {report['status']}: {report['event_count']} event(s)")
        elif args.backfill:
            report = sync_backfill(config, args.from_date, args.to_date)
            print_json(report) if args.json_only else print(f"SQLite backfill {report['status']}: {report['event_count']} event(s) across {report['date_count']} date(s)")
        elif args.search:
            settings = activity_db.database_settings(config)
            if not settings["path"].exists():
                raise RuntimeError("SQLite database does not exist. Run python scripts\\sync_sqlite.py --backfill first.")
            conn = activity_db.connect_database(settings["path"])
            try:
                results = activity_db.search_events(
                    conn,
                    args.search,
                    from_date=args.from_date,
                    to_date=args.to_date,
                    limit=max(1, args.limit),
                )
            finally:
                conn.close()
            print_json(results) if args.json_only else print_search_results(results)
        elif args.archive_search:
            results = search_archives(config, args.archive_search, limit=max(1, args.limit))
            print_json(results) if args.json_only else print_search_results(results)
    except ValueError as exc:
        parser.error(str(exc))
    except Exception as exc:
        print(f"SQLite sync failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
