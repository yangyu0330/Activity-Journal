from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import activity_db
import collect_daily
import project_health


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "activity-journal.json"
MAX_EXCERPT_CHARS = 4200


STATUS_CLASS = {
    "OK": "ok",
    "Skipped": "skipped",
    "Warning": "warning",
    "Action Needed": "action",
}

SECTION_LABELS = {
    "daily_collection": "Daily",
    "codex_review": "Review",
    "question_quality": "Questions",
    "weekly_review": "Weekly",
    "project_review": "Projects",
    "recovery": "Recovery",
    "notion_sync": "Notion",
    "external_inputs": "Inputs",
    "chrome_extension": "Extension",
    "sqlite_database": "SQLite",
    "retention": "Retention",
    "tray": "Tray",
    "file_access": "Files",
    "scheduler": "Scheduler",
}


def configure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    example = ROOT / "config" / "activity-journal.example.json"
    if example.exists():
        return json.loads(example.read_text(encoding="utf-8"))
    return {"timezone": "UTC"}


def rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def read_text(path: Path, limit: int = MAX_EXCERPT_CHARS) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n[truncated]"


def parse_markdown_sections(markdown: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = "Summary"
    sections[current] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            current = line[3:].strip() or "Untitled"
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def list_recent_markdown(directory: Path, limit: int = 8) -> list[dict[str, str]]:
    if not directory.exists():
        return []
    files = sorted(directory.glob("*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    return [
        {
            "name": path.stem,
            "path": rel(path),
            "modified": dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="minutes"),
        }
        for path in files[:limit]
    ]


def load_project_rollup(day: dt.date) -> dict[str, Any]:
    path = ROOT / "journal" / "raw" / f"project_rollup_{day.isoformat()}.json"
    if not path.exists():
        return {"path": rel(path), "exists": False, "projects": []}
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"path": rel(path), "exists": True, "error": str(exc), "projects": []}
    projects = report.get("projects", {})
    if not isinstance(projects, dict):
        projects = {}
    rows = []
    for name, payload in sorted(projects.items()):
        item = payload if isinstance(payload, dict) else {}
        rows.append(
            {
                "name": str(name),
                "status": str(item.get("status") or ""),
                "goal": str(item.get("goal") or ""),
                "evidence_count": len(item.get("evidence", [])) if isinstance(item.get("evidence"), list) else 0,
            }
        )
    return {"path": rel(path), "exists": True, "projects": rows}


def database_search(config: dict[str, Any], query: str, limit: int = 20) -> tuple[list[dict[str, Any]], str | None]:
    if not query.strip():
        return [], None
    settings = activity_db.database_settings(config, ROOT)
    path = settings["path"]
    if not settings["enabled"]:
        return [], "SQLite indexing is disabled."
    if not path.exists():
        return [], f"SQLite database is missing: {rel(path)}"
    conn = activity_db.connect_database(path)
    try:
        rows = activity_db.search_events(conn, query, limit=limit)
    except Exception as exc:
        return [], str(exc)
    finally:
        conn.close()
    return rows, None


def dashboard_data(day: dt.date, query: str = "") -> dict[str, Any]:
    config = load_config()
    report = project_health.build_report(day, config, include_scheduler=True)
    daily_path = ROOT / "journal" / "daily" / f"{day.isoformat()}.md"
    weekly_path = ROOT / "journal" / "weekly" / f"{project_health.iso_week(day)}.md"
    questions_path = ROOT / "journal" / "questions" / f"{day.isoformat()}.md"
    daily_text = read_text(daily_path)
    sections = parse_markdown_sections(daily_text) if daily_text else {}
    search_rows, search_error = database_search(config, query)
    return {
        "date": day.isoformat(),
        "week": project_health.iso_week(day),
        "health": report,
        "daily": {
            "path": rel(daily_path),
            "exists": daily_path.exists(),
            "sections": sections,
            "excerpt": daily_text,
        },
        "weekly": {
            "path": rel(weekly_path),
            "exists": weekly_path.exists(),
            "excerpt": read_text(weekly_path, 2400),
        },
        "questions": {
            "path": rel(questions_path),
            "exists": questions_path.exists(),
            "excerpt": read_text(questions_path, 2400),
        },
        "projects": load_project_rollup(day),
        "recent": {
            "daily": list_recent_markdown(ROOT / "journal" / "daily"),
            "weekly": list_recent_markdown(ROOT / "journal" / "weekly"),
            "projects": list_recent_markdown(ROOT / "journal" / "projects"),
        },
        "search": {
            "query": query,
            "error": search_error,
            "results": search_rows,
        },
    }


def css() -> str:
    return """
:root {
  color-scheme: light;
  --bg: #f5f7f8;
  --panel: #ffffff;
  --text: #172026;
  --muted: #61707a;
  --line: #d9e0e5;
  --accent: #0f766e;
  --accent-strong: #115e59;
  --ok: #147d4f;
  --warning: #a15c05;
  --action: #b42318;
  --skipped: #65758b;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: Arial, Helvetica, sans-serif;
  background: var(--bg);
  color: var(--text);
}
header {
  background: #11232d;
  color: #fff;
  padding: 22px 28px 18px;
}
header h1 {
  margin: 0;
  font-size: 24px;
  font-weight: 700;
}
header .meta {
  margin-top: 8px;
  color: #c7d2da;
  font-size: 14px;
}
main {
  max-width: 1220px;
  margin: 0 auto;
  padding: 22px;
}
.toolbar {
  display: grid;
  grid-template-columns: minmax(170px, 220px) 1fr auto;
  gap: 10px;
  align-items: center;
  margin-bottom: 18px;
}
input, button {
  font: inherit;
}
input[type="date"], input[type="search"] {
  width: 100%;
  min-height: 40px;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 8px 10px;
  background: #fff;
}
button {
  min-height: 40px;
  border: 0;
  border-radius: 6px;
  padding: 8px 14px;
  background: var(--accent);
  color: #fff;
  cursor: pointer;
}
button:hover {
  background: var(--accent-strong);
}
.grid {
  display: grid;
  grid-template-columns: repeat(12, 1fr);
  gap: 14px;
}
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 16px;
  min-width: 0;
}
.span-4 { grid-column: span 4; }
.span-6 { grid-column: span 6; }
.span-8 { grid-column: span 8; }
.span-12 { grid-column: span 12; }
h2 {
  margin: 0 0 12px;
  font-size: 17px;
}
h3 {
  margin: 16px 0 8px;
  font-size: 14px;
}
.status {
  display: inline-flex;
  align-items: center;
  min-height: 26px;
  border-radius: 999px;
  padding: 4px 9px;
  font-size: 13px;
  font-weight: 700;
  background: #eef2f5;
}
.status.ok { color: var(--ok); background: #e8f5ee; }
.status.warning { color: var(--warning); background: #fff3df; }
.status.action { color: var(--action); background: #fde8e7; }
.status.skipped { color: var(--skipped); background: #eef2f5; }
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(148px, 1fr));
  gap: 10px;
}
.mini {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px;
  min-height: 78px;
}
.mini .label {
  color: var(--muted);
  font-size: 12px;
}
.mini .value {
  margin-top: 8px;
}
.list {
  display: grid;
  gap: 8px;
}
.row {
  display: grid;
  gap: 3px;
  border-bottom: 1px solid var(--line);
  padding: 8px 0;
}
.row:last-child { border-bottom: 0; }
.muted { color: var(--muted); }
.mono {
  font-family: Consolas, "Courier New", monospace;
  font-size: 12px;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  margin: 0;
  line-height: 1.45;
  font-family: Consolas, "Courier New", monospace;
  font-size: 13px;
}
.section-columns {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}
.empty {
  color: var(--muted);
  border: 1px dashed var(--line);
  border-radius: 8px;
  padding: 14px;
}
@media (max-width: 760px) {
  main { padding: 14px; }
  .toolbar { grid-template-columns: 1fr; }
  .span-4, .span-6, .span-8, .span-12 { grid-column: span 12; }
  header { padding: 18px; }
}
"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def status_badge(status: str) -> str:
    return f'<span class="status {STATUS_CLASS.get(status, "skipped")}">{esc(status)}</span>'


def render_actions(actions: list[str]) -> str:
    if not actions:
        return '<div class="empty">No recommended actions.</div>'
    rows = "".join(f'<div class="row"><code class="mono">{esc(action)}</code></div>' for action in actions)
    return f'<div class="list">{rows}</div>'


def render_section_cards(report: dict[str, Any]) -> str:
    cards = []
    sections = report.get("sections", {})
    for key, section in sections.items():
        label = SECTION_LABELS.get(key, key.replace("_", " ").title())
        status = str(section.get("status", "Unknown"))
        cards.append(
            f"""
            <div class="mini">
              <div class="label">{esc(label)}</div>
              <div class="value">{status_badge(status)}</div>
            </div>
            """
        )
    return '<div class="cards">' + "".join(cards) + "</div>"


def render_daily_sections(sections: dict[str, str]) -> str:
    names = ["Studied", "Built", "Decisions", "Problems", "Next Actions", "Projects"]
    cards = []
    for name in names:
        text = sections.get(name, "").strip()
        body = f"<pre>{esc(text)}</pre>" if text else '<div class="empty">No entries.</div>'
        cards.append(f'<div class="panel"><h3>{esc(name)}</h3>{body}</div>')
    return '<div class="section-columns">' + "".join(cards) + "</div>"


def render_projects(projects: dict[str, Any]) -> str:
    if projects.get("error"):
        return f'<div class="empty">{esc(projects["error"])}</div>'
    rows = projects.get("projects", [])
    if not rows:
        return '<div class="empty">No project rollup for this date.</div>'
    html_rows = []
    for row in rows:
        html_rows.append(
            f"""
            <div class="row">
              <strong>{esc(row.get("name"))}</strong>
              <span class="muted">{esc(row.get("status") or "Active")} · evidence {esc(row.get("evidence_count", 0))}</span>
              <span>{esc(row.get("goal") or "Goal not set")}</span>
            </div>
            """
        )
    return '<div class="list">' + "".join(html_rows) + "</div>"


def render_recent(recent: dict[str, list[dict[str, str]]]) -> str:
    groups = []
    for title, rows in [("Daily", recent.get("daily", [])), ("Weekly", recent.get("weekly", [])), ("Projects", recent.get("projects", []))]:
        if rows:
            body = "".join(
                f'<div class="row"><strong>{esc(item["name"])}</strong><span class="muted mono">{esc(item["path"])}</span></div>'
                for item in rows
            )
        else:
            body = '<div class="empty">No files.</div>'
        groups.append(f'<div><h3>{esc(title)}</h3><div class="list">{body}</div></div>')
    return '<div class="section-columns">' + "".join(groups) + "</div>"


def render_search(search: dict[str, Any]) -> str:
    query = search.get("query", "")
    if not query:
        return '<div class="empty">Enter a search term to query indexed events.</div>'
    if search.get("error"):
        return f'<div class="empty">{esc(search["error"])}</div>'
    rows = search.get("results", [])
    if not rows:
        return '<div class="empty">No matches.</div>'
    rendered = []
    for row in rows:
        title = row.get("title") or row.get("path") or row.get("url") or row.get("source")
        meta = " · ".join(str(item) for item in [row.get("local_date"), row.get("source"), row.get("app") or row.get("domain")] if item)
        excerpt = row.get("text_excerpt") or row.get("url") or row.get("path") or ""
        rendered.append(
            f"""
            <div class="row">
              <strong>{esc(title)}</strong>
              <span class="muted">{esc(meta)}</span>
              <span>{esc(excerpt)}</span>
            </div>
            """
        )
    return '<div class="list">' + "".join(rendered) + "</div>"


def render_page(data: dict[str, Any], query: str) -> bytes:
    report = data["health"]
    sections = data["daily"]["sections"]
    date = data["date"]
    daily_header = f'{esc(data["daily"]["path"])}' if data["daily"]["exists"] else "Missing daily log"
    weekly_header = f'{esc(data["weekly"]["path"])}' if data["weekly"]["exists"] else "Missing weekly review"
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Activity Journal Dashboard</title>
  <style>{css()}</style>
</head>
<body>
  <header>
    <h1>Activity Journal Dashboard</h1>
    <div class="meta">{esc(date)} · {esc(data["week"])} · Overall {esc(report.get("overall"))}</div>
  </header>
  <main>
    <form class="toolbar" method="get" action="/">
      <input type="date" name="date" value="{esc(date)}" aria-label="Date">
      <input type="search" name="q" value="{esc(query)}" placeholder="Search indexed activity" aria-label="Search indexed activity">
      <button type="submit">Refresh</button>
    </form>
    <div class="grid">
      <section class="panel span-8">
        <h2>System Status</h2>
        {render_section_cards(report)}
      </section>
      <section class="panel span-4">
        <h2>Recommended Actions</h2>
        {render_actions(report.get("recommended_actions", []))}
      </section>
      <section class="panel span-12">
        <h2>Daily Sections</h2>
        <div class="muted mono">{daily_header}</div>
        {render_daily_sections(sections)}
      </section>
      <section class="panel span-6">
        <h2>Projects</h2>
        <div class="muted mono">{esc(data["projects"].get("path"))}</div>
        {render_projects(data["projects"])}
      </section>
      <section class="panel span-6">
        <h2>Search</h2>
        {render_search(data["search"])}
      </section>
      <section class="panel span-6">
        <h2>Questions</h2>
        <div class="muted mono">{esc(data["questions"]["path"])}</div>
        <pre>{esc(data["questions"]["excerpt"] or "No questions file for this date.")}</pre>
      </section>
      <section class="panel span-6">
        <h2>Weekly Review</h2>
        <div class="muted mono">{weekly_header}</div>
        <pre>{esc(data["weekly"]["excerpt"] or "No weekly review for this week.")}</pre>
      </section>
      <section class="panel span-12">
        <h2>Recent Files</h2>
        {render_recent(data["recent"])}
      </section>
    </div>
  </main>
</body>
</html>
"""
    return page.encode("utf-8")


class DashboardHandler(BaseHTTPRequestHandler):
    server: "DashboardServer"

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.server.quiet:
            return
        super().log_message(fmt, *args)

    def send_bytes(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_bytes(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), "application/json; charset=utf-8", status)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        config = load_config()
        try:
            day = project_health.parse_date(params.get("date", [self.server.default_date])[0], config)
        except ValueError:
            self.send_json({"error": "date must be YYYY-MM-DD"}, HTTPStatus.BAD_REQUEST)
            return
        query = params.get("q", [""])[0]
        if parsed.path == "/api/health":
            self.send_json(project_health.build_report(day, config, include_scheduler=True))
            return
        if parsed.path == "/api/search":
            rows, error = database_search(config, query)
            self.send_json({"query": query, "error": error, "results": rows})
            return
        if parsed.path not in {"/", ""}:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        data = dashboard_data(day, query)
        self.send_bytes(render_page(data, query), "text/html; charset=utf-8")


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler: type[DashboardHandler], *, default_date: str, quiet: bool = False) -> None:
        super().__init__(server_address, handler)
        self.default_date = default_date
        self.quiet = quiet


def serve(host: str, port: int, default_date: str, *, open_browser: bool, quiet: bool) -> int:
    try:
        server = DashboardServer((host, port), DashboardHandler, default_date=default_date, quiet=quiet)
    except OSError:
        if port == 0:
            raise
        server = DashboardServer((host, 0), DashboardHandler, default_date=default_date, quiet=quiet)
    url = f"http://{host}:{server.server_port}/?date={default_date}"
    print(f"Activity Journal dashboard: {url}")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_utf8_console()
    config = load_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8776)
    parser.add_argument("--date", default=project_health.parse_date(None, config).isoformat())
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    try:
        day = project_health.parse_date(args.date, config)
    except ValueError:
        parser.error(f"--date must be YYYY-MM-DD, got: {args.date}")
    return serve(args.host, args.port, day.isoformat(), open_browser=not args.no_browser, quiet=args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
