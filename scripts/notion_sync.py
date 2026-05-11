from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "activity-journal.json"
STATE_PATH = ROOT / "journal" / "raw" / "notion_pages.json"
NOTION_VERSION = "2026-03-11"
DETAIL_BODY_HEADINGS = {
    "Browser Activity",
    "ChatGPT Live Captures",
    "Recent Files",
    "App Activity",
    "Git Details",
}
RECREATE_BLOCK_THRESHOLD = 60
DEFAULT_SYNC_POLICY = {
    "mode": "immediate",
    "finalize_after_days": 3,
    "update_finalized": False,
    "max_request_rate_per_second": 2.0,
    "split_large_markdown": True,
    "max_blocks_per_page": 80,
    "upload_mode": "markdown_api",
    "final_body_mode": "full",
}
REQUEST_INTERVAL_SECONDS = 0.0
LAST_REQUEST_AT = 0.0


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict[str, str]:
    if not STATE_PATH.exists():
        return {}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Notion state file is not valid JSON: {STATE_PATH}") from exc
    if not isinstance(state, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in state.items()):
        raise RuntimeError(f"Notion state file must be a JSON object of string keys and string values: {STATE_PATH}")
    return state


def sync_policy(config: dict[str, Any]) -> dict[str, Any]:
    notion = config.get("notion", {})
    policy = notion.get("sync_policy", {}) if isinstance(notion, dict) else {}
    merged = dict(DEFAULT_SYNC_POLICY)
    if isinstance(policy, dict):
        merged.update(policy)
    merged["mode"] = "delayed_final" if merged.get("mode") == "delayed_final" else "immediate"
    try:
        merged["finalize_after_days"] = max(0, int(merged.get("finalize_after_days", 3)))
    except (TypeError, ValueError):
        merged["finalize_after_days"] = 3
    try:
        merged["max_request_rate_per_second"] = max(0.1, float(merged.get("max_request_rate_per_second", 2.0)))
    except (TypeError, ValueError):
        merged["max_request_rate_per_second"] = 2.0
    try:
        merged["max_blocks_per_page"] = max(20, int(merged.get("max_blocks_per_page", 80)))
    except (TypeError, ValueError):
        merged["max_blocks_per_page"] = 80
    merged["update_finalized"] = bool(merged.get("update_finalized", False))
    merged["split_large_markdown"] = bool(merged.get("split_large_markdown", True))
    merged["upload_mode"] = "block_api" if merged.get("upload_mode") == "block_api" else "markdown_api"
    merged["final_body_mode"] = "summary" if merged.get("final_body_mode") == "summary" else "full"
    return merged


def configure_request_rate(policy: dict[str, Any]) -> None:
    global REQUEST_INTERVAL_SECONDS
    REQUEST_INTERVAL_SECONDS = 1.0 / float(policy.get("max_request_rate_per_second", 2.0))


def save_state(state: dict[str, str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_notion_token() -> str | None:
    token = os.environ.get("NOTION_TOKEN")
    if token:
        return token
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _value_type = winreg.QueryValueEx(key, "NOTION_TOKEN")
            return value or None
    except OSError:
        return None


def request_json(method: str, path: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.notion.com/v1{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    for attempt in range(3):
        throttle_request()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                sleep_seconds = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else 0.5 * (attempt + 1)
                time.sleep(sleep_seconds)
                continue
            raise RuntimeError(f"Notion API error {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise RuntimeError(f"Notion network error: {exc}") from exc
    raise RuntimeError("Notion network error: retry attempts exhausted")


def throttle_request() -> None:
    global LAST_REQUEST_AT
    if REQUEST_INTERVAL_SECONDS <= 0:
        return
    now = time.monotonic()
    elapsed = now - LAST_REQUEST_AT
    if LAST_REQUEST_AT and elapsed < REQUEST_INTERVAL_SECONDS:
        time.sleep(REQUEST_INTERVAL_SECONDS - elapsed)
    LAST_REQUEST_AT = time.monotonic()


def rich_text(text: str) -> list[dict[str, Any]]:
    if len(text) > 1900:
        text = text[:1897] + "..."
    return [{"type": "text", "text": {"content": text}}]


def block(block_type: str, text: str = "") -> dict[str, Any]:
    return {"object": "block", "type": block_type, block_type: {"rich_text": rich_text(text) if text else []}}


def markdown_to_blocks(markdown: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if not line:
            blocks.append(block("paragraph"))
        elif line.startswith("# "):
            blocks.append(block("heading_1", line[2:].strip()))
        elif line.startswith("## "):
            blocks.append(block("heading_2", line[3:].strip()))
        elif line.startswith("### "):
            blocks.append(block("heading_3", line[4:].strip()))
        elif line.startswith("- "):
            blocks.append(block("bulleted_list_item", strip_inline_code(line[2:].strip())))
        elif re.match(r"^\d+\. ", line):
            blocks.append(block("numbered_list_item", strip_inline_code(re.sub(r"^\d+\. ", "", line).strip())))
        elif line.startswith("`") and line.endswith("`"):
            blocks.append({"object": "block", "type": "code", "code": {"rich_text": rich_text(line.strip("`")), "language": "plain text"}})
        else:
            blocks.append(block("paragraph", strip_inline_code(line)))
    return blocks


def strip_inline_code(text: str) -> str:
    return text.replace("`", "")


def chunks(values: list[dict[str, Any]], size: int = 90) -> list[list[dict[str, Any]]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def markdown_hash(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def notion_body_mode(config: dict[str, Any] | None = None) -> str:
    notion = (config or {}).get("notion", {})
    policy = sync_policy(config or {})
    if policy["mode"] == "delayed_final":
        return str(policy["final_body_mode"])
    mode = notion.get("body_mode", "summary") if isinstance(notion, dict) else "summary"
    return "full" if mode == "full" else "summary"


def notion_body_markdown(markdown: str, body_mode: str = "summary") -> str:
    if body_mode == "full":
        return markdown
    lines: list[str] = []
    for line in markdown.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match and match.group(1) in DETAIL_BODY_HEADINGS:
            break
        lines.append(line)
    if not lines:
        return markdown
    return "\n".join(lines).rstrip() + "\n"


def notion_content_hash(markdown: str, body_mode: str = "summary") -> str:
    return markdown_hash(notion_body_markdown(markdown, body_mode))


def content_hash_key(key: str) -> str:
    return f"hash:{key}"


def list_child_blocks(token: str, page_id: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    cursor = None
    while True:
        query = f"?start_cursor={urllib.parse.quote(cursor)}" if cursor else ""
        response = request_json("GET", f"/blocks/{page_id}/children{query}", token)
        blocks.extend(response.get("results", []))
        cursor = response.get("next_cursor")
        if not cursor:
            return blocks


def replace_children(token: str, page_id: str, markdown: str) -> None:
    for child in list_child_blocks(token, page_id):
        if child.get("archived"):
            continue
        request_json("PATCH", f"/blocks/{child['id']}", token, {"archived": True})
    for block_chunk in chunks(markdown_to_blocks(markdown)):
        request_json("PATCH", f"/blocks/{page_id}/children", token, {"children": block_chunk})


def archive_page(token: str, page_id: str) -> None:
    request_json("PATCH", f"/pages/{page_id}", token, {"archived": True})


def replace_children_or_recreate_database_page(
    token: str,
    page_id: str,
    database_id: str,
    properties: dict[str, Any],
    markdown: str,
) -> dict[str, Any] | None:
    existing_children = [child for child in list_child_blocks(token, page_id) if not child.get("archived")]
    new_blocks = markdown_to_blocks(markdown)
    if len(existing_children) > RECREATE_BLOCK_THRESHOLD or len(new_blocks) > 90:
        archive_page(token, page_id)
        return create_page(token, {"type": "database_id", "database_id": database_id}, properties, markdown)
    for child in existing_children:
        request_json("PATCH", f"/blocks/{child['id']}", token, {"archived": True})
    for block_chunk in chunks(new_blocks):
        request_json("PATCH", f"/blocks/{page_id}/children", token, {"children": block_chunk})
    return None


def replace_children_or_recreate_child_page(
    token: str,
    page_id: str,
    parent_page_id: str,
    title: str,
    markdown: str,
) -> dict[str, Any] | None:
    existing_children = [child for child in list_child_blocks(token, page_id) if not child.get("archived")]
    new_blocks = markdown_to_blocks(markdown)
    if len(existing_children) > RECREATE_BLOCK_THRESHOLD or len(new_blocks) > 90:
        archive_page(token, page_id)
        return create_page(token, {"type": "page_id", "page_id": parent_page_id}, {"title": {"title": rich_text(title)}}, markdown)
    for child in existing_children:
        request_json("PATCH", f"/blocks/{child['id']}", token, {"archived": True})
    for block_chunk in chunks(new_blocks):
        request_json("PATCH", f"/blocks/{page_id}/children", token, {"children": block_chunk})
    return None


def append_markdown_children(token: str, page_id: str, markdown: str, start: int = 0) -> None:
    for block_chunk in chunks(markdown_to_blocks(markdown)[start:]):
        request_json("PATCH", f"/blocks/{page_id}/children", token, {"children": block_chunk})


def create_page(token: str, parent: dict[str, Any], properties: dict[str, Any], markdown: str) -> dict[str, Any]:
    return request_json(
        "POST",
        "/pages",
        token,
        {"parent": parent, "properties": properties, "children": markdown_to_blocks(markdown)[:90]},
    )


def create_page_markdown(token: str, parent: dict[str, Any], properties: dict[str, Any], markdown: str) -> dict[str, Any]:
    return request_json(
        "POST",
        "/pages",
        token,
        {"parent": parent, "properties": properties, "markdown": markdown},
    )


def create_page_preferred(
    token: str,
    parent: dict[str, Any],
    properties: dict[str, Any],
    markdown: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    if policy.get("upload_mode") == "markdown_api":
        try:
            return create_page_markdown(token, parent, properties, markdown)
        except RuntimeError as exc:
            if not markdown_api_fallback_allowed(exc):
                raise
    page = create_page(token, parent, properties, markdown)
    append_markdown_children(token, page["id"], markdown, start=90)
    return page


def markdown_api_fallback_allowed(exc: RuntimeError) -> bool:
    message = str(exc)
    return "Notion API error 400:" in message or "Notion API error 403:" in message


def create_database(token: str, parent_page_id: str, title: str, properties: dict[str, Any]) -> str:
    response = request_json(
        "POST",
        "/databases",
        token,
        {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": rich_text(title),
            "properties": properties,
        },
    )
    return response["id"]


def ensure_databases(token: str, state: dict[str, str], parent_page_id: str) -> None:
    if "db:daily" not in state:
        state["db:daily"] = create_database(
            token,
            parent_page_id,
            "Daily Activity Log",
            {
                "Name": {"title": {}},
                "Date": {"date": {}},
                "Status": {"select": {"options": [{"name": "Draft", "color": "yellow"}, {"name": "Confirmed", "color": "green"}]}},
                "Projects": {"rich_text": {}},
                "Studied": {"rich_text": {}},
                "Built": {"rich_text": {}},
                "Decisions": {"rich_text": {}},
                "Problems": {"rich_text": {}},
                "Next Actions": {"rich_text": {}},
                "Sources": {"rich_text": {}},
            },
        )
        save_state(state)
    if "db:weekly" not in state:
        state["db:weekly"] = create_database(
            token,
            parent_page_id,
            "Weekly Review",
            {
                "Name": {"title": {}},
                "Week": {"rich_text": {}},
                "Status": {"select": {"options": [{"name": "Draft", "color": "yellow"}, {"name": "Confirmed", "color": "green"}]}},
                "Main Projects": {"rich_text": {}},
                "What I Studied": {"rich_text": {}},
                "What I Built": {"rich_text": {}},
                "Key Decisions": {"rich_text": {}},
                "Repeated Problems": {"rich_text": {}},
                "Next Week Priorities": {"rich_text": {}},
            },
        )
        save_state(state)
    if "db:projects" not in state:
        state["db:projects"] = create_database(
            token,
            parent_page_id,
            "Project Log",
            {
                "Name": {"title": {}},
                "Status": {"select": {"options": [{"name": "Active", "color": "green"}, {"name": "Paused", "color": "yellow"}, {"name": "Done", "color": "blue"}]}},
                "Goal": {"rich_text": {}},
                "Repos / Links": {"rich_text": {}},
                "Milestones": {"rich_text": {}},
                "Open Questions": {"rich_text": {}},
            },
        )
        save_state(state)


def section(markdown: str, heading: str) -> str:
    pattern = re.compile(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", re.M | re.S)
    match = pattern.search(markdown)
    if not match:
        return ""
    lines = []
    for line in match.group(1).splitlines():
        cleaned = line.strip()
        if is_placeholder_line(cleaned):
            continue
        if cleaned.startswith("- "):
            lines.append(cleaned[2:])
        elif cleaned and not cleaned.startswith("#"):
            lines.append(cleaned)
    return "\n".join(lines).strip()


def is_placeholder_line(line: str) -> bool:
    return line in {
        "-",
        "- No project inferred.",
        "- No study signal inferred.",
        "- No build signal inferred.",
        "- No decisions captured.",
        "- Codex Review has not finalized this draft yet.",
        "- Open Codex Review and answer only the missing clarification questions.",
        "- Answer pending clarification questions when prompted.",
        "- No unresolved problems captured.",
        "- No progress captured.",
        "- No open problems captured.",
        "- No next actions captured.",
        "- No links captured.",
        "- No daily logs linked.",
        "- No weekly reviews linked.",
    }


def page_title(title: str) -> dict[str, Any]:
    return {"title": rich_text(title)}


def rich_property(text: str) -> dict[str, Any]:
    return {"rich_text": rich_text(text)}


def status_property(markdown: str) -> dict[str, Any]:
    match = re.search(r"^Status:\s*(\S+)\s*$", markdown, re.M)
    status = "Confirmed" if match and match.group(1) == "Confirmed" else "Draft"
    return {"select": {"name": status}}


def daily_properties(date: str, markdown: str) -> dict[str, Any]:
    return {
        "Name": page_title(f"Daily Activity Log - {date}"),
        "Date": {"date": {"start": date}},
        "Status": status_property(markdown),
        "Projects": rich_property(section(markdown, "Projects")),
        "Studied": rich_property(section(markdown, "Studied")),
        "Built": rich_property(section(markdown, "Built")),
        "Decisions": rich_property(section(markdown, "Decisions")),
        "Problems": rich_property(section(markdown, "Problems")),
        "Next Actions": rich_property(section(markdown, "Next Actions")),
        "Sources": rich_property(section(markdown, "Sources")),
    }


def weekly_properties(week: str, markdown: str) -> dict[str, Any]:
    return {
        "Name": page_title(f"Weekly Review - {week}"),
        "Week": rich_property(week),
        "Status": {"select": {"name": "Draft"}},
        "Main Projects": rich_property(section(markdown, "Main Projects")),
        "What I Studied": rich_property(section(markdown, "What I Studied")),
        "What I Built": rich_property(section(markdown, "What I Built")),
        "Key Decisions": rich_property(section(markdown, "Key Decisions")),
        "Repeated Problems": rich_property(section(markdown, "Repeated Problems")),
        "Next Week Priorities": rich_property(section(markdown, "Next Week Priorities")),
    }


def project_status_property(markdown: str) -> dict[str, Any]:
    match = re.search(r"^Status:\s*(\S+)\s*$", markdown, re.M)
    status = match.group(1) if match and match.group(1) in {"Active", "Paused", "Done"} else "Active"
    return {"select": {"name": status}}


def project_line_value(markdown: str, key: str, fallback: str = "") -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.*?)\s*$", markdown, re.M)
    return match.group(1).strip() if match else fallback


def project_title(path: Path, markdown: str) -> str:
    match = re.search(r"^# Project Log -\s*(.+?)\s*$", markdown, re.M)
    return match.group(1).strip() if match else path.stem


def project_properties(path: Path, markdown: str) -> dict[str, Any]:
    return {
        "Name": page_title(project_title(path, markdown)),
        "Status": project_status_property(markdown),
        "Goal": rich_property(project_line_value(markdown, "Goal", "Unknown")),
        "Repos / Links": rich_property(section(markdown, "Repos / Links")),
        "Milestones": rich_property(section(markdown, "Recent Progress")),
        "Open Questions": rich_property(section(markdown, "Open Problems")),
    }


def project_log_paths(root: Path = ROOT) -> list[Path]:
    projects_dir = root / "journal" / "projects"
    if not projects_dir.exists():
        return []
    return sorted(
        path
        for path in projects_dir.glob("*.md")
        if path.is_file() and path.name != "project_metadata.json"
    )


def project_state_key(path: Path) -> str:
    return f"dbproject:{path.stem}"


def finalized_key(key: str) -> str:
    return f"finalized:{key}"


def failed_key(key: str) -> str:
    return f"failed:{key}"


def failed_at_key(key: str) -> str:
    return f"failed_at:{key}"


def slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or "detail"


def split_markdown_by_detail_headings(markdown: str) -> tuple[str, list[tuple[str, str]]]:
    main_lines: list[str] = []
    detail_lines: dict[str, list[str]] = {}
    current_detail: str | None = None
    for line in markdown.splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            title = heading.group(1)
            current_detail = title if title in DETAIL_BODY_HEADINGS else None
            if current_detail:
                detail_lines.setdefault(current_detail, []).append(line)
                continue
        if current_detail:
            detail_lines[current_detail].append(line)
        else:
            main_lines.append(line)
    details = [(title, "\n".join(lines).rstrip() + "\n") for title, lines in detail_lines.items() if any(line.strip() for line in lines)]
    main = "\n".join(main_lines).rstrip() + "\n"
    return main, details


def split_markdown_by_block_limit(title: str, markdown: str, max_blocks: int) -> list[tuple[str, str]]:
    lines = markdown.splitlines()
    if len(markdown_to_blocks(markdown)) <= max_blocks:
        return [(title, markdown.rstrip() + "\n")]
    chunks_out: list[tuple[str, str]] = []
    for index in range(0, len(lines), max_blocks):
        part_lines = lines[index : index + max_blocks]
        part_title = title if index == 0 else f"{title} Part {len(chunks_out) + 1}"
        chunks_out.append((part_title, "\n".join(part_lines).rstrip() + "\n"))
    return chunks_out


def daily_markdown_parts(date: str, markdown: str, policy: dict[str, Any]) -> tuple[str, list[tuple[str, str]]]:
    if not policy.get("split_large_markdown", True):
        return markdown.rstrip() + "\n", []
    max_blocks = int(policy.get("max_blocks_per_page", 80))
    if len(markdown_to_blocks(markdown)) <= max_blocks:
        return markdown.rstrip() + "\n", []
    main, details = split_markdown_by_detail_headings(markdown)
    main_chunks = split_markdown_by_block_limit(f"Daily Activity Log - {date} Summary", main, max_blocks)
    detail_chunks: list[tuple[str, str]] = []
    for detail_title, detail_markdown in details:
        detail_chunks.extend(split_markdown_by_block_limit(detail_title, detail_markdown, max_blocks))
    main_markdown = main_chunks[0][1]
    if len(main_chunks) > 1:
        detail_chunks = main_chunks[1:] + detail_chunks
    return main_markdown, detail_chunks


def append_detail_links(markdown: str, detail_pages: list[tuple[str, str]]) -> str:
    if not detail_pages:
        return markdown.rstrip() + "\n"
    lines = [markdown.rstrip(), "", "## Detailed Logs"]
    for title, url in detail_pages:
        lines.append(f"- [{title}]({url})")
    return "\n".join(lines).rstrip() + "\n"


def clear_failure(state: dict[str, str], key: str) -> None:
    state.pop(failed_key(key), None)
    state.pop(failed_at_key(key), None)


def record_failure(state: dict[str, str], key: str, exc: Exception) -> None:
    state[failed_key(key)] = str(exc)[:500]
    state[failed_at_key(key)] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def clear_daily_failures(state: dict[str, str], key: str) -> None:
    prefix = f"{key}:"
    for state_key in list(state):
        if state_key in {failed_key(key), failed_at_key(key)}:
            state.pop(state_key, None)
        elif state_key.startswith("failed:") and state_key.removeprefix("failed:").startswith(prefix):
            state.pop(state_key, None)
        elif state_key.startswith("failed_at:") and state_key.removeprefix("failed_at:").startswith(prefix):
            state.pop(state_key, None)


def page_url(page: dict[str, Any], page_id: str) -> str:
    url = page.get("url")
    if isinstance(url, str) and url:
        return url
    return f"https://www.notion.so/{page_id.replace('-', '')}"


def daily_finalized(state: dict[str, str], key: str) -> bool:
    return finalized_key(key) in state


def due_date_for(through_date: dt.date, policy: dict[str, Any]) -> dt.date:
    return through_date - dt.timedelta(days=int(policy.get("finalize_after_days", 3)))


def eligible_daily_dates(through_date: dt.date, policy: dict[str, Any], root: Path = ROOT) -> list[dt.date]:
    cutoff = due_date_for(through_date, policy)
    daily_dir = root / "journal" / "daily"
    if not daily_dir.exists():
        return []
    dates: list[dt.date] = []
    for path in daily_dir.glob("*.md"):
        try:
            day = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if day <= cutoff:
            dates.append(day)
    return sorted(dates)


def upsert_database_page(
    token: str,
    state: dict[str, str],
    key: str,
    database_id: str,
    properties: dict[str, Any],
    markdown: str,
    body_mode: str = "summary",
) -> str:
    hash_key = content_hash_key(key)
    body_markdown = notion_body_markdown(markdown, body_mode)
    digest = notion_content_hash(markdown, body_mode)
    if key in state:
        page_id = state[key]
        request_json("PATCH", f"/pages/{page_id}", token, {"properties": properties})
        if state.get(hash_key) == digest:
            return page_id
        recreated = replace_children_or_recreate_database_page(token, page_id, database_id, properties, body_markdown)
        if recreated:
            state[key] = recreated["id"]
            page_id = recreated["id"]
            append_markdown_children(token, page_id, body_markdown, start=90)
        state[hash_key] = digest
        save_state(state)
        return page_id
    page = create_page(token, {"type": "database_id", "database_id": database_id}, properties, body_markdown)
    state[key] = page["id"]
    state[hash_key] = digest
    save_state(state)
    append_markdown_children(token, page["id"], body_markdown, start=90)
    return page["id"]


def upsert_child_page(
    token: str,
    state: dict[str, str],
    key: str,
    parent_page_id: str,
    title: str,
    markdown: str,
    body_mode: str = "summary",
) -> str:
    properties = {"title": {"title": rich_text(title)}}
    hash_key = content_hash_key(key)
    body_markdown = notion_body_markdown(markdown, body_mode)
    digest = notion_content_hash(markdown, body_mode)
    if key in state:
        page_id = state[key]
        request_json("PATCH", f"/pages/{page_id}", token, {"properties": properties})
        if state.get(hash_key) == digest:
            return page_id
        recreated = replace_children_or_recreate_child_page(token, page_id, parent_page_id, title, body_markdown)
        if recreated:
            state[key] = recreated["id"]
            page_id = recreated["id"]
            append_markdown_children(token, page_id, body_markdown, start=90)
        state[hash_key] = digest
        save_state(state)
        return page_id
    page = create_page(token, {"type": "page_id", "page_id": parent_page_id}, properties, body_markdown)
    state[key] = page["id"]
    state[hash_key] = digest
    save_state(state)
    append_markdown_children(token, page["id"], body_markdown, start=90)
    return page["id"]


def sync_daily_final(
    token: str,
    state: dict[str, str],
    parent_page_id: str,
    date: str,
    markdown: str,
    database_id: str | None,
    policy: dict[str, Any],
    *,
    force: bool = False,
) -> bool:
    key = f"dbdaily:{date}" if database_id else f"daily:{date}"
    source_digest = markdown_hash(markdown)
    is_finalized = daily_finalized(state, key)
    if is_finalized and not force:
        if state.get(content_hash_key(key)) != source_digest:
            print(f"Notion final sync skipped for {date}: finalized content changed locally; use --force to replace it.")
        else:
            print(f"Notion final sync skipped for {date}: already finalized.")
        return False

    if key in state and (force or not is_finalized):
        archive_existing_page(token, state[key])

    main_markdown, detail_parts = daily_markdown_parts(date, markdown, policy)
    detail_pages: list[tuple[str, str]] = []
    success = True
    for detail_title, detail_markdown in detail_parts:
        detail_key = f"{key}:{slug(detail_title)}"
        detail_digest = markdown_hash(detail_markdown)
        if not force and state.get(content_hash_key(detail_key)) == detail_digest and detail_key in state:
            detail_pages.append((detail_title, page_url({"url": state.get(f"url:{detail_key}", "")}, state[detail_key])))
            continue
        try:
            if detail_key in state:
                archive_existing_page(token, state[detail_key])
            page = create_page_preferred(
                token,
                {"type": "page_id", "page_id": parent_page_id},
                {"title": page_title(f"Daily Activity Log - {date} - {detail_title}")},
                detail_markdown,
                policy,
            )
            state[detail_key] = page["id"]
            state[f"url:{detail_key}"] = page_url(page, page["id"])
            state[content_hash_key(detail_key)] = detail_digest
            clear_failure(state, detail_key)
            save_state(state)
            detail_pages.append((detail_title, state[f"url:{detail_key}"]))
        except RuntimeError as exc:
            record_failure(state, detail_key, exc)
            save_state(state)
            success = False

    final_main_markdown = append_detail_links(main_markdown, detail_pages)
    try:
        if database_id:
            parent = {"type": "database_id", "database_id": database_id}
            properties = daily_properties(date, markdown)
        else:
            parent = {"type": "page_id", "page_id": parent_page_id}
            properties = {"title": page_title(f"Daily Activity Log - {date}")}
        page = create_page_preferred(token, parent, properties, final_main_markdown, policy)
        state[key] = page["id"]
        state[f"url:{key}"] = page_url(page, page["id"])
        state[content_hash_key(key)] = source_digest
        clear_failure(state, key)
    except RuntimeError as exc:
        record_failure(state, key, exc)
        success = False

    if success:
        state[finalized_key(key)] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        clear_daily_failures(state, key)
    save_state(state)
    if success:
        print(f"Notion final sync complete for {date}.")
    else:
        print(f"Notion final sync incomplete for {date}; failed parts were saved for retry.")
    return success


def archive_existing_page(token: str, page_id: str) -> None:
    try:
        archive_page(token, page_id)
    except RuntimeError:
        pass


def sync_delayed_final(args: argparse.Namespace, config: dict[str, Any], token: str, parent_page_id: str) -> None:
    policy = sync_policy(config)
    configure_request_rate(policy)
    state = load_state()
    ensure_databases_if_needed(token, state, parent_page_id, config)
    database_id = state.get("db:daily") if config.get("notion", {}).get("use_databases", True) else None
    dates = delayed_sync_dates(args, policy, state)
    if not dates:
        print("Notion delayed final sync skipped: no eligible dates.")
        return
    failed: list[str] = []
    for day in dates:
        daily_path = ROOT / "journal" / "daily" / f"{day.isoformat()}.md"
        if not daily_path.exists():
            continue
        ok = sync_daily_final(
            token,
            state,
            parent_page_id,
            day.isoformat(),
            daily_path.read_text(encoding="utf-8"),
            database_id,
            policy,
            force=bool(args.force),
        )
        if not ok and not (daily_finalized(state, f"dbdaily:{day.isoformat()}") or daily_finalized(state, f"daily:{day.isoformat()}")):
            failed.append(day.isoformat())
    save_state(state)
    if failed:
        raise RuntimeError(f"Notion delayed final sync failed for: {', '.join(failed)}")


def ensure_databases_if_needed(token: str, state: dict[str, str], parent_page_id: str, config: dict[str, Any]) -> None:
    if config.get("notion", {}).get("use_databases", True):
        ensure_databases(token, state, parent_page_id)


def delayed_sync_dates(args: argparse.Namespace, policy: dict[str, Any], state: dict[str, str]) -> list[dt.date]:
    if args.finalize_due:
        through = parse_date_arg(args.through_date) if args.through_date else dt.datetime.now().date()
        dates = eligible_daily_dates(through, policy)
        return [day for day in dates if daily_needs_final_sync(day, state)]
    if args.date is None:
        return []
    day = parse_date_arg(args.date)
    if args.force:
        return [day]
    if day > due_date_for(dt.datetime.now().date(), policy):
        print(f"Notion delayed final sync skipped for {day.isoformat()}: not old enough for D+{policy['finalize_after_days']} finalization.")
        return []
    return [day] if daily_needs_final_sync(day, state) else []


def daily_needs_final_sync(day: dt.date, state: dict[str, str]) -> bool:
    date = day.isoformat()
    for key in [f"dbdaily:{date}", f"daily:{date}"]:
        if failed_key(key) in state:
            return True
        if key in state and daily_finalized(state, key):
            return False
    return True


def system_page_markdown() -> str:
    return """## 목적
이 페이지는 내가 매일 무엇을 공부했고 무엇을 만들었는지 자동으로 기록하기 위한 홈입니다.

## 현재 흐름
- 매일 실행이 Git, Codex, 파일 변경 흔적을 수집합니다.
- 애매한 내용은 Notion에 질문으로 적지 않고 Codex Review에서 직접 물어봅니다.
- Codex Review는 부족한 정보가 있을 때만 한국어로 질문하고, 왜 묻는지와 어떤 자료/상황을 보고 묻는지 함께 설명합니다.
- 질문에는 실용적인 추천 답변이 붙으며, 사용자는 `추천 답변으로 저장`, `수정해서 저장: ...`, `저장하지 않음` 중 하나로 처리할 수 있습니다.
- 답변은 로컬 질문 파일과 Daily Log의 Studied / Built / Decisions / Problems / Next Actions에 정리됩니다.
- Notion 동기화는 가능하면 DB에 기록하고, DB 생성이 실패하면 기존 페이지 방식으로 fallback합니다.

## 로컬 주요 파일
- scripts/run_daily.ps1
- scripts/collect_daily.py
- scripts/open_codex_review.ps1
- prompts/codex_activity_review.md
- scripts/weekly_review.py
- scripts/notion_sync.py
- config/activity-journal.json
"""


def iso_week(day: dt.date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def parse_date_arg(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def max_iso_week(year: int) -> int:
    return dt.date(year, 12, 28).isocalendar().week


def validate_week_arg(value: str) -> str:
    match = re.fullmatch(r"(\d{4})-W(\d{2})", value)
    if not match:
        raise ValueError
    year = int(match.group(1))
    week = int(match.group(2))
    if week < 1 or week > max_iso_week(year):
        raise ValueError
    return value


def sync(args: argparse.Namespace) -> None:
    config = load_config()
    notion = config.get("notion", {})
    if not notion.get("enabled", False):
        print("Notion sync skipped: disabled in config.")
        return
    parent_page_id = notion.get("parent_page_id")
    if not parent_page_id:
        print("Notion sync skipped: parent_page_id is missing.")
        return
    token = get_notion_token()
    if not token:
        print("Notion sync skipped: NOTION_TOKEN is not set.")
        return
    policy = sync_policy(config)
    if policy["mode"] == "delayed_final":
        sync_delayed_final(args, config, token, parent_page_id)
        return

    week = args.week or iso_week(dt.date.fromisoformat(args.date))
    daily_path = ROOT / "journal" / "daily" / f"{args.date}.md"
    weekly_path = ROOT / "journal" / "weekly" / f"{week}.md"
    state = load_state()
    body_mode = notion_body_mode(config)
    configure_request_rate(policy)

    upsert_child_page(token, state, "system", parent_page_id, "Activity Journal - 자동 공부/작업 기록 시스템", system_page_markdown(), body_mode)

    used_database_sync = False
    if notion.get("use_databases", True):
        try:
            ensure_databases(token, state, parent_page_id)
            if daily_path.exists():
                daily_markdown = daily_path.read_text(encoding="utf-8")
                upsert_database_page(
                    token,
                    state,
                    f"dbdaily:{args.date}",
                    state["db:daily"],
                    daily_properties(args.date, daily_markdown),
                    daily_markdown,
                    body_mode,
                )
            if weekly_path.exists():
                weekly_markdown = weekly_path.read_text(encoding="utf-8")
                upsert_database_page(token, state, f"dbweekly:{week}", state["db:weekly"], weekly_properties(week, weekly_markdown), weekly_markdown, body_mode)
            for project_path in project_log_paths():
                project_markdown = project_path.read_text(encoding="utf-8")
                try:
                    upsert_database_page(
                        token,
                        state,
                        project_state_key(project_path),
                        state["db:projects"],
                        project_properties(project_path, project_markdown),
                        project_markdown,
                        body_mode,
                    )
                except RuntimeError as exc:
                    print(f"Project Log sync skipped for {project_path.name}; daily/weekly sync preserved. {exc}")
            used_database_sync = True
        except RuntimeError as exc:
            print(f"Notion database sync failed; falling back to page sync. {exc}")

    if not used_database_sync:
        if daily_path.exists():
            upsert_child_page(
                token,
                state,
                f"daily:{args.date}",
                parent_page_id,
                f"Daily Activity Log - {args.date}",
                daily_path.read_text(encoding="utf-8"),
                body_mode,
            )
        if weekly_path.exists():
            upsert_child_page(token, state, f"weekly:{week}", parent_page_id, f"Weekly Review - {week}", weekly_path.read_text(encoding="utf-8"), body_mode)

    save_state(state)
    print("Notion sync complete.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Daily date to sync, YYYY-MM-DD.")
    parser.add_argument("--week", help="Weekly review week, YYYY-Www. Defaults to the date's ISO week.")
    parser.add_argument("--finalize-due", action="store_true", help="Sync all D+N delayed-final daily logs due by --through-date.")
    parser.add_argument("--through-date", help="Date used to select delayed-final due logs, YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--force", action="store_true", help="Replace an already finalized Notion page for the requested date.")
    args = parser.parse_args()
    if not args.date and not args.finalize_due:
        parser.error("--date is required unless --finalize-due is used.")
    if args.date:
        try:
            parse_date_arg(args.date)
        except ValueError:
            parser.error(f"--date must be YYYY-MM-DD, got: {args.date}")
    if args.through_date:
        try:
            parse_date_arg(args.through_date)
        except ValueError:
            parser.error(f"--through-date must be YYYY-MM-DD, got: {args.through_date}")
    if args.week:
        try:
            validate_week_arg(args.week)
        except ValueError:
            parser.error(f"--week must be a valid ISO week like YYYY-Www, got: {args.week}")
    try:
        sync(args)
    except RuntimeError as exc:
        print(f"Notion sync failed; local files preserved. {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
