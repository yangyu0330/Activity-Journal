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

STATUS_LABELS = {
    "OK": "정상",
    "Skipped": "건너뜀",
    "Warning": "확인 필요",
    "Action Needed": "조치 필요",
}

SECTION_LABELS = {
    "daily_collection": "일일 기록",
    "codex_review": "Codex 검토",
    "question_quality": "질문 후보",
    "weekly_review": "주간 리뷰",
    "project_review": "프로젝트",
    "recovery": "자동 복구",
    "notion_sync": "Notion",
    "external_inputs": "수집 입력",
    "chrome_extension": "브라우저 캡처",
    "sqlite_database": "SQLite",
    "retention": "보존 정책",
    "tray": "트레이",
    "file_access": "파일 접근",
    "scheduler": "예약 작업",
}

DAILY_SECTION_LABELS = {
    "Projects": "프로젝트",
    "Studied": "공부/조사",
    "Built": "만든 것",
    "Decisions": "결정",
    "Problems": "문제",
    "Next Actions": "다음 행동",
    "Sources": "근거",
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
  --bg: #f4f6f8;
  --panel: #ffffff;
  --text: #18212b;
  --muted: #667784;
  --line: #d8e0e7;
  --accent: #0f766e;
  --accent-strong: #0b5f59;
  --ink: #142833;
  --soft: #eef4f6;
  --ok: #177245;
  --warning: #9f580a;
  --action: #b42318;
  --skipped: #68778a;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: "Malgun Gothic", "Segoe UI", Arial, Helvetica, sans-serif;
  background: var(--bg);
  color: var(--text);
}
header {
  background: var(--ink);
  color: #fff;
  padding: 24px 28px 20px;
}
header h1 {
  margin: 0;
  font-size: 25px;
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
.summary {
  display: grid;
  grid-template-columns: minmax(230px, 1.15fr) repeat(auto-fit, minmax(145px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.hero-card {
  background: var(--ink);
  color: #fff;
  border-radius: 8px;
  padding: 16px;
  min-height: 112px;
}
.hero-card .label {
  color: #b8c8d1;
  font-size: 12px;
}
.hero-card .headline {
  margin-top: 10px;
  font-size: 22px;
  font-weight: 700;
}
.hero-card .sub {
  margin-top: 8px;
  color: #d8e3e9;
  font-size: 13px;
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
.span-3 { grid-column: span 3; }
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
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 10px;
}
.mini {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px;
  min-height: 78px;
  background: #fbfcfd;
}
.mini .label {
  color: var(--muted);
  font-size: 12px;
}
.mini .value {
  margin-top: 8px;
  font-weight: 700;
}
.metric {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 14px;
  min-height: 112px;
}
.metric .label {
  color: var(--muted);
  font-size: 12px;
}
.metric .value {
  margin-top: 10px;
  font-size: 24px;
  font-weight: 700;
}
.metric .detail {
  margin-top: 8px;
  color: var(--muted);
  font-size: 13px;
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
.strong { font-weight: 700; }
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
.daily-card {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px;
  background: #fbfcfd;
  min-height: 160px;
}
.daily-card h3 {
  margin-top: 0;
}
.project-row {
  display: grid;
  grid-template-columns: minmax(150px, 0.9fr) minmax(120px, 1.4fr) auto;
  gap: 10px;
  align-items: start;
  border-bottom: 1px solid var(--line);
  padding: 10px 0;
}
.project-row:last-child { border-bottom: 0; }
.pill {
  display: inline-block;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 3px 8px;
  color: var(--muted);
  font-size: 12px;
  background: #fff;
}
.empty {
  color: var(--muted);
  border: 1px dashed var(--line);
  border-radius: 8px;
  padding: 14px;
  background: #fbfcfd;
}
@media (max-width: 760px) {
  main { padding: 14px; }
  .toolbar { grid-template-columns: 1fr; }
  .summary { grid-template-columns: 1fr; }
  .span-3, .span-4, .span-6, .span-8, .span-12 { grid-column: span 12; }
  .project-row { grid-template-columns: 1fr; }
  header { padding: 18px; }
}
"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def section(report: dict[str, Any], key: str) -> dict[str, Any]:
    value = report.get("sections", {}).get(key, {})
    return value if isinstance(value, dict) else {}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def status_badge(status: str) -> str:
    return f'<span class="status {STATUS_CLASS.get(status, "skipped")}">{esc(status_label(status))}</span>'


def plural_count(value: Any, unit: str) -> str:
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        count = 0
    return f"{count}{unit}"


def action_summary(action: str) -> tuple[str, str]:
    if "open_codex_review.ps1" in action:
        return ("Codex Review 열기", "프로젝트 목표나 남은 질문을 정리합니다.")
    if "question_quality.py" in action:
        return ("질문 후보 다시 계산", "누락된 맥락 후보를 새로 만듭니다.")
    if "Open ChatGPT or Gemini" in action:
        return ("ChatGPT/Gemini 캡처 확인", "브라우저 확장이 켜져 있는지 한 번 확인합니다.")
    if "sync_sqlite.py" in action:
        return ("SQLite 색인 갱신", "검색용 활동 인덱스를 최신 상태로 맞춥니다.")
    if "run_daily.ps1" in action:
        return ("일일 기록 생성", "선택한 날짜의 raw/daily/questions 파일을 만듭니다.")
    return ("권장 작업", action)


def build_attention_items(report: dict[str, Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    question_quality = section(report, "question_quality")
    unresolved = int(question_quality.get("unresolved_candidate_count", 0) or 0)
    if unresolved:
        items.append(
            {
                "title": f"질문 후보 {unresolved}개",
                "detail": "기록에 남길 프로젝트 목표나 맥락이 더 필요합니다.",
                "status": "Warning",
            }
        )

    project_review = section(report, "project_review")
    missing_goals = int(project_review.get("missing_goal_count", 0) or 0)
    if missing_goals:
        names = ", ".join(str(name) for name in project_review.get("missing_goal_projects", [])[:5])
        items.append(
            {
                "title": f"목표 미정 프로젝트 {missing_goals}개",
                "detail": names or "프로젝트 목표를 채워야 합니다.",
                "status": "Warning",
            }
        )

    external_inputs = section(report, "external_inputs")
    for warning in external_inputs.get("warnings", []) or []:
        items.append({"title": "수집 입력 확인", "detail": str(warning), "status": "Warning"})

    chrome_extension = section(report, "chrome_extension")
    if chrome_extension.get("status") == "Warning" and int(chrome_extension.get("recent_capture_count", 0) or 0) == 0:
        items.append(
            {
                "title": "최근 ChatGPT/Gemini 캡처 없음",
                "detail": "확장 프로그램과 로컬 receiver 상태를 확인하면 좋습니다.",
                "status": "Warning",
            }
        )

    return items


def render_actions(actions: list[str]) -> str:
    if not actions:
        return '<div class="empty">바로 처리할 권장 작업이 없습니다.</div>'
    rendered = []
    for action in actions:
        title, detail = action_summary(action)
        rendered.append(
            f"""
            <div class="row">
              <strong>{esc(title)}</strong>
              <span class="muted">{esc(detail)}</span>
              <code class="mono">{esc(action)}</code>
            </div>
            """
        )
    rows = "".join(rendered)
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


def metric_card(label: str, value: str, detail: str) -> str:
    return f"""
    <div class="metric">
      <div class="label">{esc(label)}</div>
      <div class="value">{esc(value)}</div>
      <div class="detail">{esc(detail)}</div>
    </div>
    """


def render_summary(data: dict[str, Any]) -> str:
    report = data["health"]
    daily = section(report, "daily_collection")
    codex = section(report, "codex_review")
    projects = section(report, "project_review")
    sqlite = section(report, "sqlite_database")
    external = section(report, "external_inputs")
    chrome = section(report, "chrome_extension")
    overall = str(report.get("overall", "Unknown"))
    file_state = "완료" if not daily.get("missing") else "누락"
    cards = [
        f"""
        <div class="hero-card">
          <div class="label">오늘 상태</div>
          <div class="headline">{status_badge(overall)}</div>
          <div class="sub">{esc(data["date"])} · {esc(data["week"])}</div>
        </div>
        """,
        metric_card("일일 파일", file_state, "raw / daily / questions"),
        metric_card("남은 질문", plural_count(codex.get("pending_questions"), "개"), f"답변됨 {codex.get('answered_questions', 0)}개"),
        metric_card("프로젝트", plural_count(projects.get("project_count"), "개"), f"목표 미정 {projects.get('missing_goal_count', 0)}개"),
        metric_card("검색 색인", plural_count(sqlite.get("checked_event_count"), "건"), f"전체 {sqlite.get('event_count', 0)}건"),
        metric_card("수집 신호", plural_count(external.get("browser_history_count"), "개"), f"파일 {external.get('recent_file_count', 0)} · 앱 {external.get('activity_watch_count', 0)}"),
        metric_card("ChatGPT 캡처", plural_count(chrome.get("recent_capture_count"), "건"), f"receiver {status_label(str(chrome.get('receiver', {}).get('status', 'Unknown')))}"),
    ]
    return '<div class="summary">' + "".join(cards) + "</div>"


def render_attention(report: dict[str, Any]) -> str:
    items = build_attention_items(report)
    if not items:
        return '<div class="empty">오늘 기록 기준으로 우선 확인할 항목이 없습니다.</div>'
    rows = []
    for item in items:
        rows.append(
            f"""
            <div class="row">
              <strong>{esc(item["title"])}</strong>
              <span>{esc(item["detail"])}</span>
              <span>{status_badge(item.get("status", "Warning"))}</span>
            </div>
            """
        )
    return '<div class="list">' + "".join(rows) + "</div>"


def render_daily_sections(sections: dict[str, str]) -> str:
    names = ["Studied", "Built", "Decisions", "Problems", "Next Actions", "Projects", "Sources"]
    cards = []
    for name in names:
        text = sections.get(name, "").strip()
        body = f"<pre>{esc(text)}</pre>" if text else '<div class="empty">기록 없음</div>'
        cards.append(f'<div class="daily-card"><h3>{esc(DAILY_SECTION_LABELS.get(name, name))}</h3>{body}</div>')
    return '<div class="section-columns">' + "".join(cards) + "</div>"


def render_projects(projects: dict[str, Any]) -> str:
    if projects.get("error"):
        return f'<div class="empty">{esc(projects["error"])}</div>'
    rows = projects.get("projects", [])
    if not rows:
        return '<div class="empty">이 날짜의 프로젝트 rollup이 없습니다.</div>'
    html_rows = []
    for row in rows:
        goal = row.get("goal") or "목표 미정"
        status = row.get("status") or "Active"
        html_rows.append(
            f"""
            <div class="project-row">
              <strong>{esc(row.get("name"))}</strong>
              <span>{esc(goal)}</span>
              <span class="pill">{esc(status)} · 근거 {esc(row.get("evidence_count", 0))}</span>
            </div>
            """
        )
    return '<div class="list">' + "".join(html_rows) + "</div>"


def render_recent(recent: dict[str, list[dict[str, str]]]) -> str:
    groups = []
    for title, rows in [("일일 기록", recent.get("daily", [])), ("주간 리뷰", recent.get("weekly", [])), ("프로젝트", recent.get("projects", []))]:
        if rows:
            body = "".join(
                f'<div class="row"><strong>{esc(item["name"])}</strong><span class="muted mono">{esc(item["path"])}</span></div>'
                for item in rows
            )
        else:
            body = '<div class="empty">파일 없음</div>'
        groups.append(f'<div><h3>{esc(title)}</h3><div class="list">{body}</div></div>')
    return '<div class="section-columns">' + "".join(groups) + "</div>"


def render_search(search: dict[str, Any]) -> str:
    query = search.get("query", "")
    if not query:
        return '<div class="empty">검색어를 입력하면 SQLite에 색인된 활동을 찾습니다.</div>'
    if search.get("error"):
        return f'<div class="empty">{esc(search["error"])}</div>'
    rows = search.get("results", [])
    if not rows:
        return '<div class="empty">검색 결과가 없습니다.</div>'
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


def render_capture_panel(report: dict[str, Any]) -> str:
    external = section(report, "external_inputs")
    chrome = section(report, "chrome_extension")
    notion = section(report, "notion_sync")
    rows = [
        ("브라우저 기록", plural_count(external.get("browser_history_count"), "개")),
        ("최근 파일", plural_count(external.get("recent_file_count"), "개")),
        ("앱 활동", plural_count(external.get("activity_watch_count"), "개")),
        ("보이는 텍스트", plural_count(external.get("activity_text_count"), "개")),
        ("ChatGPT live", plural_count(external.get("chatgpt_live_count"), "개")),
        ("확장 receiver", status_label(str(chrome.get("receiver", {}).get("status", "Unknown")))),
        ("Notion", status_label(str(notion.get("status", "Unknown")))),
        ("확정 대기", "있음" if notion.get("pending_finalization") else "없음"),
    ]
    body = "".join(
        f'<div class="row"><strong>{esc(label)}</strong><span>{esc(value)}</span></div>'
        for label, value in rows
    )
    warnings = "".join(f'<div class="row"><span>{esc(warning)}</span></div>' for warning in external.get("warnings", []) or [])
    return '<div class="list">' + body + (warnings or "") + "</div>"


def render_page(data: dict[str, Any], query: str) -> bytes:
    report = data["health"]
    sections = data["daily"]["sections"]
    date = data["date"]
    daily_header = f'{esc(data["daily"]["path"])}' if data["daily"]["exists"] else "일일 기록 없음"
    weekly_header = f'{esc(data["weekly"]["path"])}' if data["weekly"]["exists"] else "주간 리뷰 없음"
    page = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>활동 저널 대시보드</title>
  <style>{css()}</style>
</head>
<body>
  <header>
    <h1>활동 저널 대시보드</h1>
    <div class="meta">{esc(date)} · {esc(data["week"])} · 전체 상태 {esc(status_label(str(report.get("overall"))))}</div>
  </header>
  <main>
    <form class="toolbar" method="get" action="/">
      <input type="date" name="date" value="{esc(date)}" aria-label="날짜">
      <input type="search" name="q" value="{esc(query)}" placeholder="활동 검색: 프로젝트, 파일, 앱, 대화 주제" aria-label="활동 검색">
      <button type="submit">새로고침</button>
    </form>
    {render_summary(data)}
    <div class="grid">
      <section class="panel span-6">
        <h2>오늘 확인할 것</h2>
        {render_attention(report)}
      </section>
      <section class="panel span-6">
        <h2>권장 작업</h2>
        {render_actions(report.get("recommended_actions", []))}
      </section>
      <section class="panel span-12">
        <h2>오늘 기록</h2>
        <div class="muted mono">{daily_header}</div>
        {render_daily_sections(sections)}
      </section>
      <section class="panel span-6">
        <h2>프로젝트 현황</h2>
        <div class="muted mono">{esc(data["projects"].get("path"))}</div>
        {render_projects(data["projects"])}
      </section>
      <section class="panel span-6">
        <h2>활동 검색</h2>
        {render_search(data["search"])}
      </section>
      <section class="panel span-6">
        <h2>질문/검토</h2>
        <div class="muted mono">{esc(data["questions"]["path"])}</div>
        <pre>{esc(data["questions"]["excerpt"] or "이 날짜의 질문 파일이 없습니다.")}</pre>
      </section>
      <section class="panel span-6">
        <h2>이번 주 리뷰</h2>
        <div class="muted mono">{weekly_header}</div>
        <pre>{esc(data["weekly"]["excerpt"] or "이번 주 리뷰가 아직 없습니다.")}</pre>
      </section>
      <section class="panel span-6">
        <h2>수집/연동 상태</h2>
        {render_capture_panel(report)}
      </section>
      <section class="panel span-6">
        <h2>시스템 세부 상태</h2>
        {render_section_cards(report)}
      </section>
      <section class="panel span-12">
        <h2>최근 기록 파일</h2>
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
