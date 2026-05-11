from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import auto_recover
import activity_db
import activity_watch
import chatgpt_live_server
import collect_daily
import capture_controls
import dashboard_app
import notion_sync
import project_health
import project_review
import question_quality
import privacy_filters
import retention_cleanup
import sync_sqlite
import tray_app
import weekly_review


def subprocess_env(*, without_notion_token: bool = False) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if without_notion_token:
        env.pop("NOTION_TOKEN", None)
    return env


def markdown_section(markdown: str, heading: str) -> str:
    marker = f"## {heading}"
    start = markdown.index(marker)
    start = markdown.index("\n", start) + 1
    end = markdown.find("\n## ", start)
    return markdown[start:] if end == -1 else markdown[start:end]


def remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if child.is_file():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    path.rmdir()


class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self) -> None:
        super().__init__("https://api.notion.com/v1/test", 500, "server", {}, None)

    def read(self) -> bytes:
        return b'{"message":"fail"}'


def assert_runtime_error(exc: Exception, expected_prefix: str) -> None:
    original_urlopen = urllib.request.urlopen

    def fake_urlopen(_req, timeout=30):  # noqa: ANN001
        raise exc

    urllib.request.urlopen = fake_urlopen
    try:
        try:
            notion_sync.request_json("GET", "/test", "token")
        except RuntimeError as err:
            assert str(err).startswith(expected_prefix), str(err)
        else:
            raise AssertionError("expected RuntimeError")
    finally:
        urllib.request.urlopen = original_urlopen


def test_weekly_answered_problem_filter() -> None:
    decisions = [
        "- What changed? -> Updated weekly review",
        "- Plain decision without answer marker",
    ]
    problems = [
        "- What changed?",
        "- Still unresolved",
    ]
    assert weekly_review.answered_problem_keys(decisions) == {"- What changed?"}
    assert weekly_review.unresolved_problems(problems, decisions) == ["- Still unresolved"]


def test_weekly_cleanup_answered_daily_removes_exact_problem_matches() -> None:
    markdown = """# Daily

## Decisions
- What changed? -> Updated weekly review

## Problems
- What changed?
- Still unresolved

## Next Actions
- Continue
"""
    cleaned = weekly_review.cleanup_answered_daily(markdown)
    problems = markdown_section(cleaned, "Problems")
    assert "- What changed?" not in problems
    assert "- Still unresolved" in problems
    assert "## Next Actions\n- Continue" in cleaned


def test_weekly_cleanup_answered_daily_adds_placeholder_when_empty() -> None:
    markdown = """# Daily

## Decisions
- What changed? -> Updated weekly review

## Problems
- What changed?

## Next Actions
- Continue
"""
    cleaned = weekly_review.cleanup_answered_daily(markdown)
    problems = markdown_section(cleaned, "Problems")
    assert "- No unresolved problems captured." in problems
    assert weekly_review.section(cleaned, "Problems") == []


def test_weekly_display_path_uses_markdown_slashes() -> None:
    path = weekly_review.ROOT / "journal" / "daily" / "2099-01-01.md"
    assert weekly_review.display_path(path) == "journal/daily/2099-01-01.md"


def test_weekly_built_items_keep_daily_date_context() -> None:
    root = weekly_review.ROOT
    daily_dir = root / "journal" / "daily"
    first = daily_dir / "2099-01-05.md"
    second = daily_dir / "2099-01-06.md"
    old_first = first.read_text(encoding="utf-8") if first.exists() else None
    old_second = second.read_text(encoding="utf-8") if second.exists() else None
    try:
        daily_dir.mkdir(parents=True, exist_ok=True)
        first.write_text("# Daily\n\n## Built\n- Activity Journal: 1 modified files\n", encoding="utf-8")
        second.write_text("# Daily\n\n## Built\n- Activity Journal: 2 modified files\n", encoding="utf-8")
        markdown = weekly_review.render_weekly(dt.date(2099, 1, 5))
        assert "- 2099-01-05: Activity Journal: 1 modified files" in markdown
        assert "- 2099-01-06: Activity Journal: 2 modified files" in markdown
        assert "- Built: 2" in markdown
    finally:
        if old_first is None:
            if first.exists():
                first.unlink()
        else:
            first.write_text(old_first, encoding="utf-8")
        if old_second is None:
            if second.exists():
                second.unlink()
        else:
            second.write_text(old_second, encoding="utf-8")


def test_notion_status_property() -> None:
    cases = {
        "Status: Confirmed\n": "Confirmed",
        "Status: Draft\n": "Draft",
        "Status: Something\n": "Draft",
        "# Daily\n": "Draft",
    }
    for markdown, expected in cases.items():
        actual = notion_sync.status_property(markdown)["select"]["name"]
        assert actual == expected, (markdown, actual, expected)


def test_notion_section_omits_placeholders() -> None:
    markdown = """# Daily

## Decisions
- No decisions captured.

## Problems
- No unresolved problems captured.
"""
    assert notion_sync.section(markdown, "Decisions") == ""
    assert notion_sync.section(markdown, "Problems") == ""


def test_notion_request_errors() -> None:
    assert_runtime_error(FakeHTTPError(), "Notion API error 500:")
    assert_runtime_error(urllib.error.URLError("dns failed"), "Notion network error:")
    assert_runtime_error(TimeoutError("timed out"), "Notion network error:")
    assert_runtime_error(OSError("connection reset"), "Notion network error:")


def test_notion_main_reports_runtime_errors() -> None:
    original_sync = notion_sync.sync

    def fake_sync(_args: Namespace) -> None:
        raise RuntimeError("network down")

    notion_sync.sync = fake_sync
    stderr = StringIO()
    try:
        try:
            with redirect_stderr(stderr):
                original_argv = __import__("sys").argv
                __import__("sys").argv = ["notion_sync.py", "--date", "2099-01-01"]
                try:
                    notion_sync.main()
                finally:
                    __import__("sys").argv = original_argv
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError("expected SystemExit")
    finally:
        notion_sync.sync = original_sync
    assert "Notion sync failed; local files preserved. network down" in stderr.getvalue()


def test_notion_database_id_saved_immediately() -> None:
    saved: list[dict[str, str]] = []
    original_create_database = notion_sync.create_database
    original_save_state = notion_sync.save_state

    calls = 0

    def fake_create_database(_token: str, _parent_page_id: str, title: str, _properties: dict) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return "db_daily_id"
        raise RuntimeError(f"failed after {title}")

    def fake_save_state(state: dict[str, str]) -> None:
        saved.append(dict(state))

    notion_sync.create_database = fake_create_database
    notion_sync.save_state = fake_save_state
    state: dict[str, str] = {}
    try:
        try:
            notion_sync.ensure_databases("token", state, "parent")
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected RuntimeError")
    finally:
        notion_sync.create_database = original_create_database
        notion_sync.save_state = original_save_state
    assert saved
    assert saved[0]["db:daily"] == "db_daily_id"


def test_notion_page_id_saved_before_remaining_blocks_append() -> None:
    saved: list[dict[str, str]] = []
    original_create_page = notion_sync.create_page
    original_append = notion_sync.append_markdown_children
    original_save_state = notion_sync.save_state

    def fake_create_page(_token: str, _parent: dict, _properties: dict, _markdown: str) -> dict[str, str]:
        return {"id": "page_id"}

    def fake_append(_token: str, _page_id: str, _markdown: str, start: int = 0) -> None:
        raise RuntimeError("append failed")

    def fake_save_state(state: dict[str, str]) -> None:
        saved.append(dict(state))

    notion_sync.create_page = fake_create_page
    notion_sync.append_markdown_children = fake_append
    notion_sync.save_state = fake_save_state
    state: dict[str, str] = {}
    try:
        try:
            notion_sync.upsert_child_page("token", state, "daily:2099-01-01", "parent", "Title", "# Title")
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected RuntimeError")
    finally:
        notion_sync.create_page = original_create_page
        notion_sync.append_markdown_children = original_append
        notion_sync.save_state = original_save_state
    assert saved
    assert saved[0]["daily:2099-01-01"] == "page_id"


def test_notion_body_summary_omits_large_detail_sections() -> None:
    markdown = """# Daily Activity Log - 2099-01-01

## Studied
- Summary item

## Sources
- Source item

## Browser Activity
- Long browser detail

## ChatGPT Live Captures
- Long capture detail
"""
    body = notion_sync.notion_body_markdown(markdown, "summary")
    assert "Summary item" in body
    assert "Source item" in body
    assert "Browser Activity" not in body
    assert "Long capture detail" not in body
    assert "Browser Activity" in notion_sync.notion_body_markdown(markdown, "full")


def test_notion_recreates_large_existing_page_on_update() -> None:
    original_list = notion_sync.list_child_blocks
    original_archive = notion_sync.archive_page
    original_create_page = notion_sync.create_page
    original_request = notion_sync.request_json
    original_save = notion_sync.save_state
    archived: list[str] = []
    created: list[str] = []

    def fake_list(_token: str, _page_id: str) -> list[dict[str, object]]:
        return [{"id": f"block-{index}"} for index in range(notion_sync.RECREATE_BLOCK_THRESHOLD + 1)]

    def fake_archive(_token: str, page_id: str) -> None:
        archived.append(page_id)

    def fake_create(_token: str, _parent: dict, _properties: dict, _markdown: str) -> dict[str, str]:
        created.append(_markdown)
        return {"id": "new_page_id"}

    def fake_request(_method: str, _path: str, _token: str, _payload: dict | None = None) -> dict:
        return {}

    def fake_save(_state: dict[str, str]) -> None:
        return None

    notion_sync.list_child_blocks = fake_list
    notion_sync.archive_page = fake_archive
    notion_sync.create_page = fake_create
    notion_sync.request_json = fake_request
    notion_sync.save_state = fake_save
    state = {"dbdaily:2099-01-01": "old_page_id", "hash:dbdaily:2099-01-01": "old_hash"}
    markdown = "# Daily\n\n## Studied\n- Updated\n\n## Browser Activity\n- Detail"
    try:
        page_id = notion_sync.upsert_database_page("token", state, "dbdaily:2099-01-01", "database_id", {"Name": {"title": []}}, markdown)
    finally:
        notion_sync.list_child_blocks = original_list
        notion_sync.archive_page = original_archive
        notion_sync.create_page = original_create_page
        notion_sync.request_json = original_request
        notion_sync.save_state = original_save
    assert page_id == "new_page_id"
    assert archived == ["old_page_id"]
    assert state["dbdaily:2099-01-01"] == "new_page_id"
    assert state["hash:dbdaily:2099-01-01"] == notion_sync.notion_content_hash(markdown)
    assert "Browser Activity" not in created[0]


def test_notion_markdown_api_create_payload() -> None:
    original_request = notion_sync.request_json
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(method: str, path: str, _token: str, payload: dict | None = None) -> dict:
        calls.append((method, path, payload))
        return {"id": "page_id", "url": "https://notion.so/page"}

    notion_sync.request_json = fake_request
    try:
        page = notion_sync.create_page_markdown("token", {"type": "page_id", "page_id": "parent"}, {"title": []}, "# Title")
    finally:
        notion_sync.request_json = original_request
    assert page["id"] == "page_id"
    assert calls == [
        (
            "POST",
            "/pages",
            {"parent": {"type": "page_id", "page_id": "parent"}, "properties": {"title": []}, "markdown": "# Title"},
        )
    ]


def test_notion_delayed_final_splits_details_and_finalizes() -> None:
    original_create = notion_sync.create_page_preferred
    original_save = notion_sync.save_state
    created: list[dict[str, object]] = []

    def fake_create(_token: str, parent: dict, properties: dict, markdown: str, _policy: dict) -> dict[str, str]:
        page_id = f"page_{len(created)}"
        created.append({"parent": parent, "properties": properties, "markdown": markdown, "id": page_id})
        return {"id": page_id, "url": f"https://notion.so/{page_id}"}

    def fake_save(_state: dict[str, str]) -> None:
        return None

    notion_sync.create_page_preferred = fake_create
    notion_sync.save_state = fake_save
    state: dict[str, str] = {}
    policy = {
        "upload_mode": "markdown_api",
        "split_large_markdown": True,
        "max_blocks_per_page": 5,
        "max_request_rate_per_second": 2,
        "final_body_mode": "full",
    }
    markdown = """# Daily Activity Log - 2099-01-01

## Studied
- Summary

## Browser Activity
- Browser detail

## ChatGPT Live Captures
- Chat detail
"""
    try:
        ok = notion_sync.sync_daily_final("token", state, "parent", "2099-01-01", markdown, "database", policy)
    finally:
        notion_sync.create_page_preferred = original_create
        notion_sync.save_state = original_save
    assert ok is True
    assert state["dbdaily:2099-01-01"] == "page_2"
    assert "finalized:dbdaily:2099-01-01" in state
    assert state["hash:dbdaily:2099-01-01"] == notion_sync.markdown_hash(markdown)
    assert "Browser detail" in str(created[0]["markdown"])
    assert "Chat detail" in str(created[1]["markdown"])
    assert "Detailed Logs" in str(created[2]["markdown"])
    assert "https://notion.so/page_0" in str(created[2]["markdown"])


def test_notion_delayed_final_does_not_update_finalized_without_force() -> None:
    original_create = notion_sync.create_page_preferred
    created: list[dict[str, object]] = []

    def fake_create(_token: str, parent: dict, properties: dict, markdown: str, _policy: dict) -> dict[str, str]:
        created.append({"parent": parent, "properties": properties, "markdown": markdown})
        return {"id": "new_page", "url": "https://notion.so/new_page"}

    notion_sync.create_page_preferred = fake_create
    state = {
        "dbdaily:2099-01-01": "existing_page",
        "hash:dbdaily:2099-01-01": "old_hash",
        "finalized:dbdaily:2099-01-01": "2099-01-04T00:00:00+00:00",
    }
    try:
        ok = notion_sync.sync_daily_final("token", state, "parent", "2099-01-01", "# Changed", "database", {"split_large_markdown": True})
    finally:
        notion_sync.create_page_preferred = original_create
    assert ok is False
    assert created == []
    assert state["dbdaily:2099-01-01"] == "existing_page"


def test_notion_delayed_due_dates_skip_recent_and_finalized() -> None:
    root = collect_daily.ROOT / "journal" / "raw" / "_tmp_notion_due_dates"
    remove_tree(root)
    try:
        daily_dir = root / "journal" / "daily"
        daily_dir.mkdir(parents=True)
        for day in ["2099-01-01", "2099-01-02", "2099-01-04"]:
            (daily_dir / f"{day}.md").write_text("# Daily\n", encoding="utf-8")
        policy = {"finalize_after_days": 3}
        due = notion_sync.eligible_daily_dates(dt.date(2099, 1, 5), policy, root)
        state = {"dbdaily:2099-01-01": "page", "finalized:dbdaily:2099-01-01": "done"}
        selected = [day for day in due if notion_sync.daily_needs_final_sync(day, state)]
    finally:
        remove_tree(root)
    assert [day.isoformat() for day in selected] == ["2099-01-02"]


def test_run_daily_uses_delayed_notion_finalization() -> None:
    content = (collect_daily.ROOT / "scripts" / "run_daily.ps1").read_text(encoding="utf-8")
    assert "--finalize-due --through-date $TargetDate" in content
    assert "python $NotionSyncScript --date $ResolvedDate" not in content
    assert "[switch]$CatchUpMissed" in content
    assert "Test-DailyNeedsCatchUp" in content
    assert "Invoke-DailyWorkflow -TargetDate $MissedDate" in content


def test_notion_load_state_reports_invalid_json() -> None:
    original_state_path = notion_sync.STATE_PATH
    path = Path("journal") / "raw" / "_tmp_invalid_notion_state.json"
    path.write_text("{bad json", encoding="utf-8")
    notion_sync.STATE_PATH = path
    try:
        try:
            notion_sync.load_state()
        except RuntimeError as exc:
            assert "Notion state file is not valid JSON" in str(exc)
        else:
            raise AssertionError("expected RuntimeError")
    finally:
        notion_sync.STATE_PATH = original_state_path
        if path.exists():
            path.unlink()


def test_notion_load_state_rejects_invalid_shape() -> None:
    original_state_path = notion_sync.STATE_PATH
    path = Path("journal") / "raw" / "_tmp_invalid_notion_state_shape.json"
    path.write_text('{"system": 123}', encoding="utf-8")
    notion_sync.STATE_PATH = path
    try:
        try:
            notion_sync.load_state()
        except RuntimeError as exc:
            assert "must be a JSON object of string keys and string values" in str(exc)
        else:
            raise AssertionError("expected RuntimeError")
    finally:
        notion_sync.STATE_PATH = original_state_path
        if path.exists():
            path.unlink()


def test_notion_main_reports_invalid_state_without_traceback() -> None:
    original_load_state = notion_sync.load_state
    original_load_config = notion_sync.load_config

    def fake_load_state() -> dict[str, str]:
        raise RuntimeError("Notion state file is not valid JSON: test")

    notion_sync.load_state = fake_load_state
    notion_sync.load_config = lambda: {
        "notion": {
            "enabled": True,
            "parent_page_id": "parent",
            "use_databases": True,
            "sync_policy": {"mode": "delayed_final"},
        }
    }
    stderr = StringIO()
    stdout = StringIO()
    original_token = os.environ.get("NOTION_TOKEN")
    os.environ["NOTION_TOKEN"] = "token"
    try:
        try:
            with redirect_stderr(stderr):
                original_argv = sys.argv
                sys.argv = ["notion_sync.py", "--date", "2099-01-01"]
                try:
                    notion_sync.main()
                finally:
                    sys.argv = original_argv
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError("expected SystemExit")
    finally:
        notion_sync.load_state = original_load_state
        notion_sync.load_config = original_load_config
        if original_token is None:
            os.environ.pop("NOTION_TOKEN", None)
        else:
            os.environ["NOTION_TOKEN"] = original_token
    err = stderr.getvalue()
    assert "Notion sync failed; local files preserved. Notion state file is not valid JSON: test" in err
    assert "Traceback" not in err


def test_collect_write_once_preserves_existing_content() -> None:
    path = Path("journal") / "raw" / "_smoke_write_once.md"
    if path.exists():
        path.unlink()
    try:
        assert collect_daily.write_review_file(path, "first") == "created"
        assert path.read_text(encoding="utf-8") == "first"
        assert collect_daily.write_review_file(path, "second") == "preserved"
        assert path.read_text(encoding="utf-8") == "first"
        assert collect_daily.write_review_file(path, "third", overwrite=True) == "overwritten"
        assert path.read_text(encoding="utf-8") == "third"
        path.unlink()
        assert collect_daily.write_review_file(path, "fourth", overwrite=True) == "created"
        assert path.read_text(encoding="utf-8") == "fourth"
    finally:
        if path.exists():
            path.unlink()


def test_collect_ignores_generated_journal_paths() -> None:
    ignored = {"journal"}
    assert collect_daily.should_ignore(Path("project") / "journal" / "raw" / "file.json", ignored)
    assert not collect_daily.should_ignore(Path("project") / "scripts" / "collect_daily.py", ignored)


def test_collect_find_git_repos_detects_git_dirs_when_git_is_ignored() -> None:
    root = collect_daily.ROOT / "journal" / "raw" / "_tmp_find_git_repos"
    remove_tree(root)
    try:
        repo = root / "workspace" / "project"
        (repo / ".git").mkdir(parents=True)
        config = {
            "project_roots": [str(root / "workspace")],
            "scan": {"ignored_dirs": [".git", "node_modules", "journal"]},
        }
        assert collect_daily.find_git_repos(config) == [repo]
    finally:
        remove_tree(root)


def test_collect_sanitizes_codex_sessions() -> None:
    sessions = [
        {"id": "abc", "updated_at": "2026-05-04T01:00:00Z", "thread_name": "Sensitive title"},
    ]
    sanitized = collect_daily.sanitize_codex_sessions(sessions)
    assert sanitized == [{"id": "abc", "updated_at": "2026-05-04T01:00:00Z"}]
    assert set(sanitized[0].keys()) == {"id", "updated_at"}


def test_collect_manual_notes_enter_studied_section() -> None:
    raw = {
        "git_activity": [],
        "modified_files": {},
        "codex": {"history": [], "sessions": []},
    }
    markdown = collect_daily.render_daily(
        collect_daily.parse_date("2099-01-01"),
        raw,
        "- Learned how Codex skills and Notion sync fit together.\n- Reviewed a browser article.",
    )
    studied = markdown_section(markdown, "Studied")
    assert "- Learned how Codex skills and Notion sync fit together." in studied
    assert "- Reviewed a browser article." in studied
    assert "No study signal inferred." not in studied


def test_collect_daily_uses_explicit_decisions_placeholder() -> None:
    raw = {
        "git_activity": [],
        "modified_files": {},
        "codex": {"history": [], "sessions": []},
    }
    markdown = collect_daily.render_daily(collect_daily.parse_date("2099-01-01"), raw, "")
    decisions = markdown_section(markdown, "Decisions")
    assert "- Automatic local evidence capture and Notion sync remain the active logging workflow." in decisions
    assert weekly_review.section(markdown, "Decisions") == [
        "- Automatic local evidence capture and Notion sync remain the active logging workflow."
    ]
    assert notion_sync.section(markdown, "Decisions") == "Automatic local evidence capture and Notion sync remain the active logging workflow."


def test_render_daily_displays_configured_timezone_clocks() -> None:
    timestamp = "2099-01-01T00:30:00+00:00"
    raw = {
        "git_activity": [],
        "modified_files": {},
        "codex": {"history": [], "sessions": []},
        "external_inputs": {
            "browser_history": [
                {
                    "title": "Browser page",
                    "url": "https://example.com/page",
                    "domain": "example.com",
                    "profile": "Chrome/Default",
                    "source_path": "History",
                    "first_visit_time": timestamp,
                    "last_visit_time": timestamp,
                    "visit_count": 1,
                }
            ],
            "chatgpt_live": [
                {
                    "title": "Live chat",
                    "url": "https://chatgpt.com/c/test",
                    "app": "chatgpt.com",
                    "captured_at": timestamp,
                    "source_path": "chatgpt_live.jsonl",
                    "text": "Study planning text",
                }
            ],
            "recent_files": [{"title": "notes.md", "path": "notes.md", "modified_at": timestamp}],
            "activity_watch": [{"title": "Editor", "process": "Code.exe", "last_seen": timestamp, "sample_count": 1}],
        },
    }
    markdown = collect_daily.render_daily(dt.date(2099, 1, 1), raw, "", {"timezone": "Asia/Seoul"})
    assert "- 09:30 - Browser page" in markdown
    assert "- 09:30 - Live chat" in markdown
    assert "- 09:30 - notes.md" in markdown
    assert "- 09:30 - Editor" in markdown
    assert "- 00:30 - Live chat" not in markdown


def test_render_question_file_uses_readable_korean_template() -> None:
    markdown = collect_daily.render_question_file(dt.date(2099, 1, 1))
    assert "Codex Review가 raw evidence와 daily draft를 읽고 부족한 정보만 질문합니다." in markdown
    assert "질문을 만들 때는 아래 형식을 사용합니다." in markdown
    assert "媛" not in markdown
    assert "�" not in markdown


def external_config(root: Path, *, include_raw_text: bool = True, max_text_chars: int = 12000) -> dict:
    return {
        "timezone": "UTC",
        "external_inputs": {
            "enabled": True,
            "inbox_dir": str(root / "inbox"),
            "chatgpt_export_dir": str(root / "imports" / "chatgpt"),
            "include_raw_text": include_raw_text,
            "max_items_per_source": 50,
            "max_text_chars_per_item": max_text_chars,
        },
    }


def question_quality_root(name: str) -> Path:
    root = collect_daily.ROOT / "journal" / "raw" / name
    remove_tree(root)
    for dirname in ["raw", "daily", "questions", "weekly"]:
        (root / "journal" / dirname).mkdir(parents=True, exist_ok=True)
    week = project_health.iso_week(dt.date(2099, 1, 1))
    (root / "journal" / "weekly" / f"{week}.md").write_text("# Weekly\n", encoding="utf-8")
    return root


def write_question_quality_fixture(
    root: Path,
    *,
    daily: str,
    raw: dict | None = None,
    questions: str = "# Questions\n",
) -> None:
    day = "2099-01-01"
    payload = raw or {
        "git_activity": [],
        "modified_files": {"Activity Journal": ["scripts/question_quality.py"]},
        "codex": {"history": [{"session_id": "s", "ts": 1}], "sessions": []},
    }
    (root / "journal" / "raw" / f"{day}.json").write_text(json.dumps(payload), encoding="utf-8")
    (root / "journal" / "daily" / f"{day}.md").write_text(daily, encoding="utf-8")
    (root / "journal" / "questions" / f"{day}.md").write_text(questions, encoding="utf-8")


def project_review_root(name: str) -> Path:
    root = collect_daily.ROOT / "journal" / "raw" / name
    remove_tree(root)
    for dirname in ["raw", "daily", "weekly", "projects"]:
        (root / "journal" / dirname).mkdir(parents=True, exist_ok=True)
    return root


def recovery_root(name: str) -> Path:
    root = collect_daily.ROOT / "journal" / "raw" / name
    remove_tree(root)
    for dirname in ["raw", "daily", "questions", "weekly", "projects"]:
        (root / "journal" / dirname).mkdir(parents=True, exist_ok=True)
    return root


def recovery_config(root: Path, *, notion_enabled: bool = False) -> dict:
    return {
        "timezone": "UTC",
        "notion": {"enabled": notion_enabled, "parent_page_id": "parent"},
        "external_inputs": {
            "enabled": True,
            "inbox_dir": str(root / "inbox"),
            "chatgpt_export_dir": str(root / "imports" / "chatgpt"),
            "include_raw_text": True,
            "max_items_per_source": 50,
        },
    }


def fake_recovery_runner(root: Path, calls: list[list[str]]):
    def runner(command: list[str], _cwd: Path) -> dict[str, object]:
        calls.append(command)
        joined = " ".join(command).replace("\\", "/")
        if "collect_daily.py" in joined:
            (root / "journal" / "raw" / "2099-01-01.json").write_text(
                json.dumps({"git_activity": [], "modified_files": {}, "codex": {"history": [], "sessions": []}}),
                encoding="utf-8",
            )
            daily = root / "journal" / "daily" / "2099-01-01.md"
            if not daily.exists():
                daily.write_text("# Daily\n\n## Projects\n- Activity Journal\n", encoding="utf-8")
            questions = root / "journal" / "questions" / "2099-01-01.md"
            if not questions.exists():
                questions.write_text("# Questions\n", encoding="utf-8")
        elif "weekly_review.py" in joined:
            (root / "journal" / "weekly" / project_health.iso_week(dt.date(2099, 1, 1))).with_suffix(".md").write_text("# Weekly\n", encoding="utf-8")
        elif "project_review.py" in joined:
            (root / "journal" / "raw" / "project_rollup_2099-01-01.json").write_text(
                json.dumps({"checked_date": "2099-01-01", "projects": {}, "missing_goal_projects": []}),
                encoding="utf-8",
            )
        elif "question_quality.py" in joined:
            (root / "journal" / "raw" / "question_candidates_2099-01-01.json").write_text(
                json.dumps({"checked_date": "2099-01-01", "candidates": []}),
                encoding="utf-8",
            )
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}

    return runner


def write_daily(root: Path, day: str, markdown: str) -> None:
    (root / "journal" / "daily" / f"{day}.md").write_text(markdown, encoding="utf-8")


def test_collect_external_inputs_reads_inbox_date_files() -> None:
    root = collect_daily.ROOT / "journal" / "raw" / "_tmp_external_inbox"
    remove_tree(root)
    try:
        (root / "inbox" / "2099-01-01").mkdir(parents=True)
        (root / "inbox" / "2099-01-01.md").write_text("# Learned source capture\nDetails", encoding="utf-8")
        (root / "inbox" / "2099-01-01" / "note.txt").write_text("Reviewed ChatGPT export flow", encoding="utf-8")
        (root / "inbox" / "outside.txt").write_text("Outside window", encoding="utf-8")
        config = external_config(root)
        since, until = collect_daily.collection_window(dt.date(2099, 1, 1), config)
        external = collect_daily.collect_external_inputs(config, dt.date(2099, 1, 1), since, until, root)
        titles = [item["title"] for item in external["inbox"]]
        assert "Learned source capture" in titles
        assert "Reviewed ChatGPT export flow" in titles
        assert "Outside window" not in titles
        assert external["inbox"][0]["text"]
    finally:
        remove_tree(root)


def chatgpt_fixture(title: str, when: dt.datetime, text: str) -> dict:
    timestamp = when.replace(tzinfo=dt.timezone.utc).timestamp()
    return {
        "title": title,
        "create_time": timestamp,
        "update_time": timestamp,
        "mapping": {
            "message-node": {
                "message": {
                    "content": {
                        "parts": [text],
                    }
                }
            }
        },
    }


def test_collect_chatgpt_export_json_and_zip() -> None:
    root = collect_daily.ROOT / "journal" / "raw" / "_tmp_external_chatgpt"
    remove_tree(root)
    try:
        export_dir = root / "imports" / "chatgpt"
        export_dir.mkdir(parents=True)
        inside = chatgpt_fixture("Useful system design", dt.datetime(2099, 1, 1, 1, 0), "Raw ChatGPT conversation text")
        outside = chatgpt_fixture("Old chat", dt.datetime(2098, 12, 31, 23, 0), "Old text")
        (export_dir / "conversations.json").write_text(json.dumps([inside, outside]), encoding="utf-8")
        zipped = chatgpt_fixture("Zipped export", dt.datetime(2099, 1, 1, 2, 0), "Zip conversation text")
        with zipfile.ZipFile(export_dir / "chatgpt-export.zip", "w") as archive:
            archive.writestr("conversations.json", json.dumps([zipped]))
        config = external_config(root)
        since, until = collect_daily.collection_window(dt.date(2099, 1, 1), config)
        chatgpt, warnings = collect_daily.collect_chatgpt_exports(config, since, until, root)
        titles = [item["title"] for item in chatgpt]
        assert "Useful system design" in titles
        assert "Zipped export" in titles
        assert "Old chat" not in titles
        assert any(item.get("text") == "Raw ChatGPT conversation text" for item in chatgpt)
        assert warnings == []
    finally:
        remove_tree(root)


def test_collect_chatgpt_malformed_export_warns_without_failure() -> None:
    root = collect_daily.ROOT / "journal" / "raw" / "_tmp_external_bad_chatgpt"
    remove_tree(root)
    try:
        export_dir = root / "imports" / "chatgpt"
        export_dir.mkdir(parents=True)
        (export_dir / "conversations.json").write_text('{"not":"a list"}', encoding="utf-8")
        with zipfile.ZipFile(export_dir / "no-conversations.zip", "w") as archive:
            archive.writestr("chat.html", "<html></html>")
        config = external_config(root)
        since, until = collect_daily.collection_window(dt.date(2099, 1, 1), config)
        chatgpt, warnings = collect_daily.collect_chatgpt_exports(config, since, until, root)
        assert chatgpt == []
        assert warnings
    finally:
        remove_tree(root)


def test_collect_browser_history_reads_chromium_history() -> None:
    root = collect_daily.ROOT / "journal" / "raw" / "_tmp_external_browser"
    remove_tree(root)
    try:
        profile_dir = root / "Browser" / "User Data" / "Default"
        profile_dir.mkdir(parents=True)
        history_path = profile_dir / "History"
        conn = sqlite3.connect(history_path)
        try:
            conn.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT)")
            conn.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER)")
            conn.execute("INSERT INTO urls (id, url, title) VALUES (1, 'https://example.com/study', 'Study Article')")
            conn.execute("INSERT INTO urls (id, url, title) VALUES (2, 'https://old.example.com/', 'Old Article')")
            inside = dt.datetime(2099, 1, 1, 1, 0, tzinfo=dt.timezone.utc)
            outside = dt.datetime(2098, 12, 31, 1, 0, tzinfo=dt.timezone.utc)
            conn.execute("INSERT INTO visits (url, visit_time) VALUES (1, ?)", (collect_daily.datetime_to_chromium_timestamp(inside),))
            conn.execute("INSERT INTO visits (url, visit_time) VALUES (1, ?)", (collect_daily.datetime_to_chromium_timestamp(inside + dt.timedelta(minutes=5)),))
            conn.execute("INSERT INTO visits (url, visit_time) VALUES (2, ?)", (collect_daily.datetime_to_chromium_timestamp(outside),))
            conn.commit()
        finally:
            conn.close()

        config = external_config(root)
        config["external_inputs"]["browser_history"] = {
            "enabled": True,
            "history_paths": [str(history_path)],
            "max_items": 20,
        }
        since, until = collect_daily.collection_window(dt.date(2099, 1, 1), config)
        external = collect_daily.collect_external_inputs(config, dt.date(2099, 1, 1), since, until, root)
        assert external["browser_history"][0]["title"] == "Study Article"
        assert external["browser_history"][0]["visit_count"] == 2
        assert external["browser_history"][0]["domain"] == "example.com"
        assert "Old Article" not in [item["title"] for item in external["browser_history"]]

        raw = {"git_activity": [], "modified_files": {}, "codex": {"history": [], "sessions": []}, "external_inputs": external}
        markdown = collect_daily.render_daily(dt.date(2099, 1, 1), raw, "")
        assert "Browser activity: 1 page(s) visited" in markdown
        assert "https://example.com/study" in markdown
    finally:
        remove_tree(root)


def test_collect_recent_files_reads_configured_roots() -> None:
    root = collect_daily.ROOT / "journal" / "raw" / "_tmp_external_recent_files"
    remove_tree(root)
    try:
        files_root = root / "Documents"
        files_root.mkdir(parents=True)
        inside = files_root / "study-notes.pdf"
        outside = files_root / "old-notes.pdf"
        inside.write_text("inside", encoding="utf-8")
        outside.write_text("outside", encoding="utf-8")
        inside_time = dt.datetime(2099, 1, 1, 2, 0, tzinfo=dt.timezone.utc).timestamp()
        outside_time = dt.datetime(2098, 12, 31, 2, 0, tzinfo=dt.timezone.utc).timestamp()
        os.utime(inside, (inside_time, inside_time))
        os.utime(outside, (outside_time, outside_time))

        config = external_config(root)
        config["external_inputs"]["recent_files"] = {
            "enabled": True,
            "roots": [str(files_root)],
            "max_items": 20,
            "max_depth": 2,
        }
        since, until = collect_daily.collection_window(dt.date(2099, 1, 1), config)
        external = collect_daily.collect_external_inputs(config, dt.date(2099, 1, 1), since, until, root)
        titles = [item["title"] for item in external["recent_files"]]
        assert "study-notes.pdf" in titles
        assert "old-notes.pdf" not in titles

        raw = {"git_activity": [], "modified_files": {}, "codex": {"history": [], "sessions": []}, "external_inputs": external}
        markdown = collect_daily.render_daily(dt.date(2099, 1, 1), raw, "")
        assert "Recent files: 1 file(s) changed" in markdown
        assert "study-notes.pdf" in markdown
    finally:
        remove_tree(root)


def test_collect_activity_watch_reads_window_log() -> None:
    root = collect_daily.ROOT / "journal" / "raw" / "_tmp_external_activity_watch"
    remove_tree(root)
    try:
        log_path = root / "journal" / "raw" / "activity_watch.jsonl"
        log_path.parent.mkdir(parents=True)
        inside = dt.datetime(2099, 1, 1, 3, 0, tzinfo=dt.timezone.utc)
        outside = dt.datetime(2098, 12, 31, 3, 0, tzinfo=dt.timezone.utc)
        events = [
            {"ts": inside.isoformat(), "process": "chrome.exe", "title": "Study page", "text": "Visible lesson notes about algebra", "text_chars": 34},
            {"ts": inside.isoformat(), "process": "chrome.exe", "title": "Study page"},
            {"ts": outside.isoformat(), "process": "old.exe", "title": "Old page"},
        ]
        log_path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")

        config = external_config(root)
        config["external_inputs"]["activity_watch"] = {
            "enabled": True,
            "log_path": str(log_path),
            "max_items": 20,
        }
        since, until = collect_daily.collection_window(dt.date(2099, 1, 1), config)
        external = collect_daily.collect_external_inputs(config, dt.date(2099, 1, 1), since, until, root)
        assert external["activity_watch"][0]["title"] == "Study page"
        assert external["activity_watch"][0]["sample_count"] == 2
        assert external["activity_watch"][0]["text_capture_count"] == 1
        assert external["activity_watch"][0]["text_excerpt"] == "Visible lesson notes about algebra"

        raw = {"git_activity": [], "modified_files": {}, "codex": {"history": [], "sessions": []}, "external_inputs": external}
        markdown = collect_daily.render_daily(dt.date(2099, 1, 1), raw, "")
        assert "App activity: 1 active window(s)" in markdown
        assert "Study page" in markdown
        assert "Visible app text captured from 1 active window(s)" in markdown
        assert "Visible text: Visible lesson notes about algebra" in markdown
    finally:
        remove_tree(root)


def test_collect_chatgpt_live_reads_capture_log() -> None:
    root = collect_daily.ROOT / "journal" / "raw" / "_tmp_external_chatgpt_live"
    remove_tree(root)
    try:
        log_path = root / "journal" / "raw" / "chatgpt_live.jsonl"
        log_path.parent.mkdir(parents=True)
        inside = dt.datetime(2099, 1, 1, 4, 0, tzinfo=dt.timezone.utc)
        outside = dt.datetime(2098, 12, 31, 4, 0, tzinfo=dt.timezone.utc)
        events = [
            {
                "captured_at": inside.isoformat(),
                "app": "chatgpt.com",
                "title": "Live study chat",
                "url": "https://chatgpt.com/c/live",
                "content_hash": "same",
                "text": "Live ChatGPT explanation about study planning.",
            },
            {
                "captured_at": inside.isoformat(),
                "app": "chatgpt.com",
                "title": "Live study chat",
                "url": "https://chatgpt.com/c/live",
                "content_hash": "same",
                "text": "Duplicate should be deduped.",
            },
            {
                "captured_at": outside.isoformat(),
                "app": "chatgpt.com",
                "title": "Old chat",
                "url": "https://chatgpt.com/c/old",
                "content_hash": "old",
                "text": "Old text",
            },
        ]
        log_path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")

        config = external_config(root)
        config["external_inputs"]["chatgpt_live"] = {
            "enabled": True,
            "log_path": str(log_path),
            "max_items": 20,
            "max_text_chars_per_item": 200,
        }
        since, until = collect_daily.collection_window(dt.date(2099, 1, 1), config)
        external = collect_daily.collect_external_inputs(config, dt.date(2099, 1, 1), since, until, root)
        assert len(external["chatgpt_live"]) == 1
        assert external["chatgpt_live"][0]["title"] == "Live study chat"

        raw = {"git_activity": [], "modified_files": {}, "codex": {"history": [], "sessions": []}, "external_inputs": external}
        markdown = collect_daily.render_daily(dt.date(2099, 1, 1), raw, "")
        assert "ChatGPT live: Live study chat" in markdown
        assert "Live ChatGPT explanation" in markdown or "Duplicate should be deduped" in markdown
    finally:
        remove_tree(root)


def test_privacy_filters_block_raw_domains_and_apps() -> None:
    config = {
        "privacy": {
            "exclusions": {
                "raw_block_domains": ["secret.example.com"],
                "raw_block_apps": ["KakaoTalk.exe"],
            }
        }
    }
    chat_event = chatgpt_live_server.normalize_payload(
        {
            "url": "https://secret.example.com/c/private",
            "title": "Private chat",
            "text": "Private text",
        }
    )
    public_event = chatgpt_live_server.normalize_payload(
        {
            "url": "https://chatgpt.com/c/public",
            "title": "Public chat",
            "text": "Public text",
        }
    )
    assert not chatgpt_live_server.should_store_event(config, chat_event)
    assert chatgpt_live_server.should_store_event(config, public_event)
    assert privacy_filters.should_block_raw_activity_event(config, {"process": "KakaoTalk.exe", "title": "Chat"})
    assert not privacy_filters.should_block_raw_activity_event(config, {"process": "Code.exe", "title": "Editor"})


def test_capture_controls_pause_sources_and_exclusions() -> None:
    config = capture_controls.ensure_config_shape({"timezone": "UTC"})
    assert config["tray"]["enabled"] is True
    assert config["tray"]["show_notifications"] is True
    assert capture_controls.should_capture_source(config, "activity_watch")
    capture_controls.set_capture_enabled(config, False)
    assert not capture_controls.should_capture_source(config, "activity_watch")
    capture_controls.resume(config)
    capture_controls.set_source_enabled(config, "chatgpt_live", False)
    assert not capture_controls.should_capture_source(config, "chatgpt_live")
    assert capture_controls.should_capture_source(config, "activity_watch")
    config["capture"]["privacy_mode_until"] = "2099-01-01T01:00:00+00:00"
    assert not capture_controls.capture_active(config, dt.datetime(2099, 1, 1, 0, 30, tzinfo=dt.timezone.utc))
    assert capture_controls.capture_active(config, dt.datetime(2099, 1, 1, 2, 0, tzinfo=dt.timezone.utc))
    capture_controls.add_exclusion(config, "raw_block_domains", "secret.example.com")
    capture_controls.add_exclusion(config, "raw_block_domains", "SECRET.example.com")
    assert capture_controls.exclusion_values(config, "raw_block_domains") == ["secret.example.com"]
    capture_controls.remove_exclusion(config, "raw_block_domains", "secret.example.com")
    assert capture_controls.exclusion_values(config, "raw_block_domains") == []


def test_capture_toggles_block_live_and_activity_raw_storage() -> None:
    config = capture_controls.ensure_config_shape(
        {
            "privacy": {"exclusions": {"raw_block_domains": [], "raw_block_apps": []}},
            "external_inputs": {
                "enabled": True,
                "chatgpt_live": {"enabled": True},
                "activity_watch": {"enabled": True},
            },
        }
    )
    chat_event = chatgpt_live_server.normalize_payload({"url": "https://chatgpt.com/c/test", "title": "Public", "text": "text"})
    app_event = {"process": "Code.exe", "title": "Editor"}
    assert chatgpt_live_server.should_store_event(config, chat_event)
    assert activity_watch.should_store_event(config, app_event)
    capture_controls.set_capture_enabled(config, False)
    assert not chatgpt_live_server.should_store_event(config, chat_event)
    assert not activity_watch.should_store_event(config, app_event)
    capture_controls.resume(config)
    capture_controls.set_source_enabled(config, "chatgpt_live", False)
    capture_controls.set_source_enabled(config, "activity_watch", False)
    assert not chatgpt_live_server.should_store_event(config, chat_event)
    assert not activity_watch.should_store_event(config, app_event)


def test_tray_actions_pause_and_resume_capture_config() -> None:
    root = collect_daily.ROOT / "journal" / "raw" / "_tmp_tray_actions"
    remove_tree(root)
    try:
        root.mkdir(parents=True)
        config_path = root / "activity-journal.json"
        config_path.write_text(json.dumps({"timezone": "UTC"}, ensure_ascii=False), encoding="utf-8")
        paused = tray_app.apply_pause(15, config_path)
        saved = capture_controls.ensure_config_shape(capture_controls.load_config(config_path))
        assert paused["active"] is False
        assert saved["capture"]["privacy_mode_until"]
        assert saved["capture"]["privacy_mode_reason"] == "tray pause 15 min"
        resumed = tray_app.apply_resume(config_path)
        saved = capture_controls.ensure_config_shape(capture_controls.load_config(config_path))
        assert resumed["active"] is True
        assert saved["capture"]["privacy_mode_until"] is None
    finally:
        remove_tree(root)


def test_tray_dependency_status_shape() -> None:
    status = tray_app.dependency_status()
    assert status["status"] in {"OK", "Missing"}
    assert status["install_command"] == "python -m pip install --user pystray pillow"
    assert set(status["dependencies"]) == {"pystray", "PIL"}
    assert status["dependencies"]["PIL"]["package"] == "Pillow"


def test_collect_external_inputs_skips_when_capture_paused() -> None:
    root = collect_daily.ROOT / "journal" / "raw" / "_tmp_capture_paused"
    remove_tree(root)
    try:
        config = external_config(root)
        capture_controls.set_capture_enabled(config, False)
        since, until = collect_daily.collection_window(dt.date(2099, 1, 1), config)
        external = collect_daily.collect_external_inputs(config, dt.date(2099, 1, 1), since, until, root)
    finally:
        remove_tree(root)
    assert external["capture_paused"]
    assert external["chatgpt_live"] == []
    assert "External input capture paused" in external["warnings"][0]


def test_collect_and_render_apply_privacy_exclusions() -> None:
    root = collect_daily.ROOT / "journal" / "raw" / "_tmp_privacy_exclusions"
    remove_tree(root)
    try:
        log_dir = root / "journal" / "raw"
        log_dir.mkdir(parents=True)
        chat_log = log_dir / "chatgpt_live.jsonl"
        watch_log = log_dir / "activity_watch.jsonl"
        timestamp = "2099-01-01T04:00:00+00:00"
        chat_events = [
            {
                "captured_at": timestamp,
                "app": "chatgpt.com",
                "title": "Public chat",
                "url": "https://chatgpt.com/c/public",
                "content_hash": "public",
                "text": "Public study text",
            },
            {
                "captured_at": timestamp,
                "app": "secret.example.com",
                "title": "Secret chat",
                "url": "https://secret.example.com/c/private",
                "content_hash": "secret",
                "text": "Secret study text",
            },
        ]
        watch_events = [
            {"ts": timestamp, "process": "Code.exe", "title": "Editor"},
            {"ts": timestamp, "process": "KakaoTalk.exe", "title": "Private messenger"},
        ]
        chat_log.write_text("\n".join(json.dumps(event) for event in chat_events), encoding="utf-8")
        watch_log.write_text("\n".join(json.dumps(event) for event in watch_events), encoding="utf-8")

        config = external_config(root)
        config["external_inputs"]["chatgpt_live"] = {"enabled": True, "log_path": str(chat_log), "max_items": 20}
        config["external_inputs"]["activity_watch"] = {"enabled": True, "log_path": str(watch_log), "max_items": 20}
        config["privacy"] = {
            "exclusions": {
                "raw_block_domains": ["secret.example.com"],
                "raw_block_apps": ["KakaoTalk.exe"],
                "summary_hide_domains": [],
                "summary_hide_apps": [],
            }
        }
        since, until = collect_daily.collection_window(dt.date(2099, 1, 1), config)
        external = collect_daily.collect_external_inputs(config, dt.date(2099, 1, 1), since, until, root)
        assert [item["title"] for item in external["chatgpt_live"]] == ["Public chat"]
        assert [item["process"] for item in external["activity_watch"]] == ["Code.exe"]

        config["privacy"]["exclusions"] = {
            "raw_block_domains": [],
            "raw_block_apps": [],
            "summary_hide_domains": ["secret.example.com"],
            "summary_hide_apps": ["KakaoTalk.exe"],
        }
        external = collect_daily.collect_external_inputs(config, dt.date(2099, 1, 1), since, until, root)
        assert len(external["chatgpt_live"]) == 2
        assert len(external["activity_watch"]) == 2
        raw = {"git_activity": [], "modified_files": {}, "codex": {"history": [], "sessions": []}, "external_inputs": external}
        markdown = collect_daily.render_daily(dt.date(2099, 1, 1), raw, "", config)
        assert "Public chat" in markdown
        assert "Editor" in markdown
        assert "Secret chat" not in markdown
        assert "Private messenger" not in markdown
    finally:
        remove_tree(root)


def retention_config(root: Path, *, max_raw_mb: float = 500) -> dict:
    return {
        "timezone": "UTC",
        "retention": {
            "enabled": True,
            "keep_recent_days": 90,
            "max_raw_mb": max_raw_mb,
            "archive_dir": str(root / "journal" / "archive" / "raw"),
            "delete_archives": False,
        },
        "external_inputs": {
            "chatgpt_live": {
                "log_path": str(root / "journal" / "raw" / "chatgpt_live.jsonl"),
            },
            "activity_watch": {
                "log_path": str(root / "journal" / "raw" / "activity_watch.jsonl"),
            },
        },
    }


def read_gzip_text_lines(path: Path) -> list[str]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [line.rstrip("\n") for line in handle if line.strip()]


def test_retention_archives_old_jsonl_and_keeps_recent() -> None:
    root = collect_daily.ROOT / "journal" / "raw" / "_tmp_retention_cleanup"
    remove_tree(root)
    try:
        raw_dir = root / "journal" / "raw"
        raw_dir.mkdir(parents=True)
        now_value = dt.datetime(2099, 5, 1, 12, 0, tzinfo=dt.timezone.utc)
        old_chat = {
            "captured_at": "2099-01-15T10:00:00+00:00",
            "app": "chatgpt.com",
            "title": "Old chat",
            "text": "Old text",
        }
        recent_chat = {
            "captured_at": "2099-04-30T10:00:00+00:00",
            "app": "chatgpt.com",
            "title": "Recent chat",
            "text": "Recent text",
        }
        unknown_chat = {"app": "chatgpt.com", "title": "No timestamp", "text": "Keep me"}
        old_watch = {"ts": "2099-01-16T10:00:00+00:00", "process": "chrome.exe", "title": "Old window"}
        recent_watch = {"ts": "2099-04-30T10:00:00+00:00", "process": "chrome.exe", "title": "Recent window"}
        old_chat_line = json.dumps(old_chat)
        recent_chat_line = json.dumps(recent_chat)
        unknown_chat_line = json.dumps(unknown_chat)
        old_watch_line = json.dumps(old_watch)
        recent_watch_line = json.dumps(recent_watch)
        chat_log = raw_dir / "chatgpt_live.jsonl"
        watch_log = raw_dir / "activity_watch.jsonl"
        chat_log.write_text("\n".join([old_chat_line, recent_chat_line, unknown_chat_line, "{bad json"]), encoding="utf-8")
        watch_log.write_text("\n".join([old_watch_line, recent_watch_line]), encoding="utf-8")

        archive_path = root / "journal" / "archive" / "raw" / "2099-01" / "chatgpt_live.jsonl.gz"
        archive_path.parent.mkdir(parents=True)
        with gzip.open(archive_path, "wt", encoding="utf-8") as handle:
            handle.write(old_chat_line + "\n")

        report = retention_cleanup.cleanup_report(retention_config(root), root=root, now_value=now_value)

        kept_chat = chat_log.read_text(encoding="utf-8")
        kept_watch = watch_log.read_text(encoding="utf-8")
        assert report["status"] == "OK"
        assert report["archived_line_count"] == 2
        assert old_chat_line not in kept_chat
        assert recent_chat_line in kept_chat
        assert unknown_chat_line in kept_chat
        assert "bad json" in kept_chat
        assert report["sources"][0]["invalid_line_count"] == 1
        assert old_watch_line not in kept_watch
        assert recent_watch_line in kept_watch
        chat_archive_lines = read_gzip_text_lines(archive_path)
        watch_archive_lines = read_gzip_text_lines(root / "journal" / "archive" / "raw" / "2099-01" / "activity_watch.jsonl.gz")
        assert chat_archive_lines.count(old_chat_line) == 1
        assert old_watch_line in watch_archive_lines
    finally:
        remove_tree(root)


def test_collect_external_raw_text_policy_and_daily_privacy() -> None:
    root = collect_daily.ROOT / "journal" / "raw" / "_tmp_external_privacy"
    remove_tree(root)
    try:
        (root / "inbox").mkdir(parents=True)
        long_text = "Inbox Title\nSUPER_SECRET_FULL_BODY " * 20
        (root / "inbox" / "2099-01-01.md").write_text(long_text, encoding="utf-8")
        config = external_config(root, include_raw_text=False)
        since, until = collect_daily.collection_window(dt.date(2099, 1, 1), config)
        external = collect_daily.collect_external_inputs(config, dt.date(2099, 1, 1), since, until, root)
        assert "text" not in external["inbox"][0]

        config = external_config(root, include_raw_text=True, max_text_chars=20)
        external = collect_daily.collect_external_inputs(config, dt.date(2099, 1, 1), since, until, root)
        assert len(external["inbox"][0]["text"]) <= 20
        raw = {"git_activity": [], "modified_files": {}, "codex": {"history": [], "sessions": []}, "external_inputs": external}
        markdown = collect_daily.render_daily(dt.date(2099, 1, 1), raw, "")
        assert "SUPER_SECRET_FULL_BODY" not in markdown
        assert "Inbox:" in markdown
    finally:
        remove_tree(root)


def test_collect_uses_configured_timezone_for_window() -> None:
    config = {"timezone": "Asia/Seoul"}
    since, until = collect_daily.collection_window(dt.date(2099, 1, 1), config)
    assert since.isoformat() == "2099-01-01T00:00:00+09:00"
    assert until.isoformat() == "2099-01-02T00:00:00+09:00"
    assert collect_daily.is_iso_between("2099-01-01T00:00:00+09:00", since, until)
    assert collect_daily.is_iso_between("2099-01-01T00:30:00+09:00", since, until)
    assert not collect_daily.is_iso_between("2099-01-02T00:00:00+09:00", since, until)
    assert not collect_daily.is_iso_between("2098-12-31T23:30:00+09:00", since, until)


def test_collect_timezone_fallback_is_local_naive() -> None:
    config = {"timezone": "Invalid/Timezone"}
    since, until = collect_daily.collection_window(dt.date(2099, 1, 1), config)
    assert since.tzinfo is None
    assert until.tzinfo is None
    assert since == dt.datetime(2099, 1, 1, 0, 0)
    assert until == dt.datetime(2099, 1, 2, 0, 0)
    assert collect_daily.is_iso_between("2099-01-01T00:00:00", since, until)
    assert not collect_daily.is_iso_between("2099-01-02T00:00:00", since, until)


def test_collect_codex_respects_half_open_window() -> None:
    since = dt.datetime(2099, 1, 1, 0, 0, tzinfo=dt.timezone.utc)
    until = dt.datetime(2099, 1, 2, 0, 0, tzinfo=dt.timezone.utc)
    codex_home = collect_daily.ROOT / "journal" / "raw" / "_tmp_smoke_codex_home"
    history_path = codex_home / "history.jsonl"
    session_path = codex_home / "session_index.jsonl"
    codex_home.mkdir(parents=True, exist_ok=True)
    try:
        history_rows = [
            {"session_id": "at-start", "ts": since.timestamp()},
            {"session_id": "inside", "ts": since.timestamp() + 60},
            {"session_id": "at-end", "ts": until.timestamp()},
        ]
        session_rows = [
            {"id": "session-start", "updated_at": "2099-01-01T00:00:00+00:00", "thread_name": "start"},
            {"id": "session-inside", "updated_at": "2099-01-01T00:01:00+00:00", "thread_name": "inside"},
            {"id": "session-end", "updated_at": "2099-01-02T00:00:00+00:00", "thread_name": "end"},
        ]
        (codex_home / "history.jsonl").write_text(
            "\n".join(json.dumps(row) for row in history_rows),
            encoding="utf-8",
        )
        (codex_home / "session_index.jsonl").write_text(
            "\n".join(json.dumps(row) for row in session_rows),
            encoding="utf-8",
        )
        raw = collect_daily.collect_codex(
            {"codex": {"home": str(codex_home), "include_history_text": False, "max_history_items": 10}},
            since,
            until,
        )
    finally:
        if history_path.exists():
            history_path.unlink()
        if session_path.exists():
            session_path.unlink()
        if codex_home.exists():
            codex_home.rmdir()
    assert [row["session_id"] for row in raw["history"]] == ["at-start", "inside"]
    assert [row["id"] for row in raw["sessions"]] == ["session-start", "session-inside"]


def test_weekly_parse_date_uses_explicit_date_first() -> None:
    config = {"timezone": "Invalid/Timezone"}
    assert weekly_review.parse_date("2099-01-01", config) == dt.date(2099, 1, 1)


def test_collect_invalid_date_reports_cli_error() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/collect_daily.py", "--date", "invalid-date"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=subprocess_env(),
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "--date must be YYYY-MM-DD, got: invalid-date" in combined
    assert "Traceback" not in combined


def test_collect_print_default_date_exits_without_writing() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/collect_daily.py", "--print-default-date"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=subprocess_env(),
    )
    assert result.returncode == 0
    assert result.stderr == ""
    assert dt.date.fromisoformat(result.stdout.strip())


def test_weekly_invalid_date_reports_cli_error() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/weekly_review.py", "--date", "invalid-date"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=subprocess_env(),
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "--date must be YYYY-MM-DD, got: invalid-date" in combined
    assert "Traceback" not in combined


def test_notion_invalid_date_reports_cli_error_before_token_check() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/notion_sync.py", "--date", "invalid-date"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=subprocess_env(),
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "--date must be YYYY-MM-DD, got: invalid-date" in combined
    assert "NOTION_TOKEN is not set" not in combined
    assert "Traceback" not in combined


def test_notion_invalid_week_reports_cli_error_before_token_check() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/notion_sync.py", "--date", "2099-01-01", "--week", "2099-W99"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=subprocess_env(),
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "--week must be a valid ISO week like YYYY-Www, got: 2099-W99" in combined
    assert "NOTION_TOKEN is not set" not in combined
    assert "Traceback" not in combined


def test_notion_valid_date_without_token_still_skips() -> None:
    original_get_token = notion_sync.get_notion_token
    notion_sync.get_notion_token = lambda: None
    stdout = StringIO()
    original_argv = sys.argv
    try:
        with redirect_stdout(stdout):
            sys.argv = ["notion_sync.py", "--date", "2099-01-01"]
            notion_sync.main()
    finally:
        sys.argv = original_argv
        notion_sync.get_notion_token = original_get_token
    assert "Notion sync skipped: NOTION_TOKEN is not set." in stdout.getvalue()


def test_health_question_parser_ignores_template_fences() -> None:
    markdown = """# Questions

```text
## Q: <stable_id>
Answer:
```

## Q: next_action
Question:
What should happen next?

Answer:
추천 답변으로 저장

## Q: unresolved
Question:
What is unclear?

Answer:
"""
    questions = project_health.parse_question_blocks(markdown)
    assert [question["id"] for question in questions] == ["next_action", "unresolved"]
    assert [question["answered"] for question in questions] == [True, False]


def test_question_quality_generates_core_candidates() -> None:
    root = question_quality_root("_tmp_question_quality_core")
    try:
        daily = """# Daily Activity Log - 2099-01-01

Status: Draft

## Projects
- Activity Journal

## Studied
- No study signal inferred.

## Built
- Activity Journal: 1 modified files

## Decisions
- No decisions captured.

## Problems
- Codex Review has not finalized this draft yet.

## Next Actions
- Open Codex Review and answer only the missing clarification questions.

## Sources
- Codex history items: 1
"""
        write_question_quality_fixture(root, daily=daily)
        report = question_quality.build_report(
            dt.date(2099, 1, 1),
            {"timezone": "UTC", "notion": {"enabled": False}},
            root=root,
        )
    finally:
        remove_tree(root)
    ids = [candidate["id"] for candidate in report["candidates"]]
    assert "daily_learning" in ids
    assert "next_action" in ids
    assert "unresolved_problem" in ids
    assert report["candidate_count"] == len(set(ids))


def test_question_quality_excludes_answered_ids_and_fenced_examples() -> None:
    root = question_quality_root("_tmp_question_quality_answered")
    try:
        daily = """# Daily Activity Log - 2099-01-01

Status: Draft

## Projects
- Activity Journal

## Studied
- No study signal inferred.

## Built
- Activity Journal: 1 modified files

## Decisions
- No decisions captured.

## Problems
- Codex Review has not finalized this draft yet.

## Next Actions
- Open Codex Review and answer only the missing clarification questions.
"""
        questions = """# Questions

```text
## Q: daily_learning
Answer:
```

## Q: next_action
Question:
What should happen next?

Answer:
추천 답변으로 저장
"""
        write_question_quality_fixture(root, daily=daily, questions=questions)
        report = question_quality.build_report(
            dt.date(2099, 1, 1),
            {"timezone": "UTC", "notion": {"enabled": False}},
            root=root,
        )
    finally:
        remove_tree(root)
    ids = [candidate["id"] for candidate in report["candidates"]]
    assert "daily_learning" in ids
    assert "next_action" not in ids


def test_health_question_quality_missing_file_is_ok() -> None:
    root = question_quality_root("_tmp_question_quality_missing_health")
    try:
        status = project_health.check_question_quality(dt.date(2099, 1, 1), root)
    finally:
        remove_tree(root)
    assert status["status"] == "OK"
    assert status["generated"] is False


def test_health_question_quality_unresolved_candidates_warn() -> None:
    root = question_quality_root("_tmp_question_quality_unresolved_health")
    try:
        candidate_file = root / "journal" / "raw" / "question_candidates_2099-01-01.json"
        candidate_file.write_text(
            json.dumps(
                {
                    "checked_date": "2099-01-01",
                    "candidates": [
                        {
                            "id": "next_action",
                            "category": "다음 행동",
                            "severity": "high",
                            "confidence": "high",
                            "reason": "test",
                            "evidence": [],
                            "recommended_answer": "test",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        status = project_health.check_question_quality(dt.date(2099, 1, 1), root)
    finally:
        remove_tree(root)
    assert status["status"] == "Warning"
    assert status["unresolved_candidate_count"] == 1


def test_project_review_rolls_up_daily_logs_and_metadata_goal() -> None:
    root = project_review_root("_tmp_project_review_rollup")
    try:
        write_daily(
            root,
            "2099-01-01",
            """# Daily Activity Log - 2099-01-01

Status: Confirmed

## Projects
- Activity Journal

## Studied
- Learned project rollups.

## Built
- Activity Journal: 2 modified files

## Decisions
- Activity Journal goal tracking belongs in project metadata.

## Problems
- No unresolved problems captured.

## Next Actions
- Activity Journal: sync project log to Notion.
""",
        )
        (root / "journal" / "weekly" / project_health.iso_week(dt.date(2099, 1, 1))).with_suffix(".md").write_text("# Weekly\n", encoding="utf-8")
        metadata = {
            "version": 1,
            "projects": {
                "Activity Journal": {
                    "slug": "activity-journal",
                    "goal": "Build a reliable automatic activity journal.",
                    "status": "Active",
                    "links": ["https://example.test/project"],
                }
            },
        }
        (root / "journal" / "projects" / "project_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        report = project_review.build_rollup(dt.date(2099, 1, 1), root=root)
        project_review.write_rollup(report, root=root)
        project_path = root / "journal" / "projects" / "activity-journal.md"
        markdown = project_path.read_text(encoding="utf-8")
    finally:
        remove_tree(root)
    assert report["project_count"] == 1
    assert report["missing_goal_count"] == 0
    assert "Goal: Build a reliable automatic activity journal." in markdown
    assert "- 2099-01-01: Activity Journal: 2 modified files" in markdown
    assert "- https://example.test/project" in markdown


def test_project_review_preserves_existing_metadata_values() -> None:
    root = project_review_root("_tmp_project_review_metadata")
    try:
        write_daily(
            root,
            "2099-01-01",
            """# Daily

## Projects
- Activity Journal
- New Project

## Built
- Activity Journal: 1 modified files
""",
        )
        metadata_path = root / "journal" / "projects" / "project_metadata.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "projects": {
                        "Activity Journal": {
                            "slug": "activity-journal",
                            "goal": "Preserve this goal.",
                            "status": "Paused",
                            "links": ["local"],
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        project_review.build_rollup(dt.date(2099, 1, 1), root=root)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    finally:
        remove_tree(root)
    assert metadata["projects"]["Activity Journal"]["goal"] == "Preserve this goal."
    assert metadata["projects"]["Activity Journal"]["status"] == "Paused"
    assert metadata["projects"]["New Project"]["goal"] == ""
    assert metadata["projects"]["New Project"]["status"] == "Active"


def test_question_quality_adds_project_goal_candidate() -> None:
    root = question_quality_root("_tmp_question_quality_project_goal")
    try:
        daily = """# Daily Activity Log - 2099-01-01

Status: Draft

## Projects
- Activity Journal

## Studied
- Project rollup flow.

## Built
- Activity Journal: 1 modified files

## Decisions
- Project logs should exist.

## Problems
- No unresolved problems captured.

## Next Actions
- Continue project review.
"""
        write_question_quality_fixture(root, daily=daily)
        report = question_quality.build_report(
            dt.date(2099, 1, 1),
            {"timezone": "UTC", "notion": {"enabled": False}},
            root=root,
        )
    finally:
        remove_tree(root)
    ids = [candidate["id"] for candidate in report["candidates"]]
    assert "project_goal_activity-journal" in ids


def test_health_project_review_missing_and_missing_goal_states() -> None:
    root = project_review_root("_tmp_project_review_health")
    try:
        missing = project_health.check_project_review(dt.date(2099, 1, 1), root)
        (root / "journal" / "raw" / "project_rollup_2099-01-01.json").write_text(
            json.dumps(
                {
                    "checked_date": "2099-01-01",
                    "projects": {"Activity Journal": {"name": "Activity Journal"}},
                    "missing_goal_projects": ["Activity Journal"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        warning = project_health.check_project_review(dt.date(2099, 1, 1), root)
    finally:
        remove_tree(root)
    assert missing["status"] == "Warning"
    assert missing["generated"] is False
    assert warning["status"] == "Warning"
    assert warning["missing_goal_count"] == 1


def test_notion_project_properties_and_state_key() -> None:
    path = Path("journal") / "projects" / "activity-journal.md"
    markdown = """# Project Log - Activity Journal

Status: Paused
Goal: Build a reliable journal.

## Recent Progress
- Added project review.

## Open Problems
- Goal missing elsewhere.

## Repos / Links
- https://example.test/project
"""
    props = notion_sync.project_properties(path, markdown)
    assert props["Name"]["title"][0]["text"]["content"] == "Activity Journal"
    assert props["Status"]["select"]["name"] == "Paused"
    assert props["Goal"]["rich_text"][0]["text"]["content"] == "Build a reliable journal."
    assert notion_sync.project_state_key(path) == "dbproject:activity-journal"


def test_auto_recover_safe_generation_preserves_review_files() -> None:
    root = recovery_root("_tmp_auto_recover_safe_generation")
    calls: list[list[str]] = []
    try:
        daily_path = root / "journal" / "daily" / "2099-01-01.md"
        daily_path.write_text("KEEP DAILY", encoding="utf-8")
        report = auto_recover.recover(
            dt.date(2099, 1, 1),
            recovery_config(root),
            root=root,
            open_codex=False,
            runner=fake_recovery_runner(root, calls),
        )
        joined = " ".join(" ".join(call) for call in calls).replace("\\", "/")
        preserved_daily = daily_path.read_text(encoding="utf-8")
    finally:
        remove_tree(root)
    assert "collect_daily.py" in joined
    assert "weekly_review.py" in joined
    assert "project_review.py" in joined
    assert "question_quality.py" in joined
    assert "setup_task.ps1" not in joined
    assert "set_notion_token.ps1" not in joined
    assert report["action_count"] >= 4
    assert preserved_daily == "KEEP DAILY"


def test_auto_recover_creates_external_dirs_and_logs_recovery() -> None:
    root = recovery_root("_tmp_auto_recover_external_dirs")
    calls: list[list[str]] = []
    try:
        report = auto_recover.recover(
            dt.date(2099, 1, 1),
            recovery_config(root),
            root=root,
            open_codex=False,
            runner=fake_recovery_runner(root, calls),
        )
        assert (root / "inbox").is_dir()
        assert (root / "imports" / "chatgpt").is_dir()
        assert (root / "journal" / "raw" / "recovery_2099-01-01.json").exists()
        health = project_health.check_recovery(dt.date(2099, 1, 1), root)
    finally:
        remove_tree(root)
    assert report["failed_action_count"] == 0
    assert health["status"] == "OK"
    assert health["generated"] is True


def test_auto_recover_defers_unsafe_user_actions() -> None:
    root = recovery_root("_tmp_auto_recover_defers")
    calls: list[list[str]] = []
    original_token_status = project_health.token_status
    project_health.token_status = lambda: "missing"
    try:
        report = auto_recover.recover(
            dt.date(2099, 1, 1),
            recovery_config(root, notion_enabled=True),
            root=root,
            open_codex=False,
            runner=fake_recovery_runner(root, calls),
        )
        joined = " ".join(" ".join(call) for call in calls)
    finally:
        project_health.token_status = original_token_status
        remove_tree(root)
    skipped_ids = {item["id"] for item in report["skipped_user_actions"]}
    assert "notion_setup" in skipped_ids
    assert "set_notion_token.ps1" not in joined
    assert "notion_sync.py" not in joined


def test_auto_recover_opens_codex_only_when_allowed() -> None:
    root = recovery_root("_tmp_auto_recover_open_codex")
    calls: list[list[str]] = []
    try:
        (root / "journal" / "raw" / "2099-01-01.json").write_text(
            json.dumps({"git_activity": [], "modified_files": {}, "codex": {"history": [], "sessions": []}}),
            encoding="utf-8",
        )
        write_daily(root, "2099-01-01", "# Daily\n\n## Projects\n- Activity Journal\n")
        (root / "journal" / "questions" / "2099-01-01.md").write_text("# Questions\n", encoding="utf-8")
        (root / "journal" / "weekly" / project_health.iso_week(dt.date(2099, 1, 1))).with_suffix(".md").write_text("# Weekly\n", encoding="utf-8")
        (root / "journal" / "raw" / "project_rollup_2099-01-01.json").write_text(
            json.dumps({"checked_date": "2099-01-01", "projects": {}, "missing_goal_projects": []}),
            encoding="utf-8",
        )
        (root / "journal" / "raw" / "question_candidates_2099-01-01.json").write_text(
            json.dumps(
                {
                    "checked_date": "2099-01-01",
                    "candidates": [
                        {
                            "id": "unresolved_problem",
                            "severity": "high",
                            "confidence": "high",
                            "category": "상태",
                            "reason": "test",
                            "evidence": [],
                            "recommended_answer": "test",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        report = auto_recover.recover(
            dt.date(2099, 1, 1),
            recovery_config(root),
            root=root,
            open_codex=True,
            from_run_daily=False,
            runner=fake_recovery_runner(root, calls),
        )
        joined = " ".join(" ".join(call) for call in calls).replace("\\", "/")
    finally:
        remove_tree(root)
    assert report["codex_review"]["opened"] is True
    assert "open_codex_review.ps1" in joined


def test_auto_recover_does_not_open_codex_from_run_daily() -> None:
    root = recovery_root("_tmp_auto_recover_run_daily_no_codex")
    calls: list[list[str]] = []
    try:
        (root / "journal" / "raw" / "2099-01-01.json").write_text(
            json.dumps({"git_activity": [], "modified_files": {}, "codex": {"history": [], "sessions": []}}),
            encoding="utf-8",
        )
        write_daily(root, "2099-01-01", "# Daily\n\n## Projects\n- Activity Journal\n")
        (root / "journal" / "questions" / "2099-01-01.md").write_text("# Questions\n", encoding="utf-8")
        (root / "journal" / "weekly" / project_health.iso_week(dt.date(2099, 1, 1))).with_suffix(".md").write_text("# Weekly\n", encoding="utf-8")
        (root / "journal" / "raw" / "project_rollup_2099-01-01.json").write_text(
            json.dumps({"checked_date": "2099-01-01", "projects": {}, "missing_goal_projects": []}),
            encoding="utf-8",
        )
        (root / "journal" / "raw" / "question_candidates_2099-01-01.json").write_text(
            json.dumps(
                {
                    "checked_date": "2099-01-01",
                    "candidates": [{"id": "project_goal_activity-journal", "severity": "medium", "confidence": "high"}],
                }
            ),
            encoding="utf-8",
        )
        report = auto_recover.recover(
            dt.date(2099, 1, 1),
            recovery_config(root),
            root=root,
            open_codex=True,
            from_run_daily=True,
            runner=fake_recovery_runner(root, calls),
        )
        joined = " ".join(" ".join(call) for call in calls).replace("\\", "/")
    finally:
        remove_tree(root)
    assert report["codex_review"]["opened"] is False
    assert "open_codex_review.ps1" not in joined


def test_health_missing_daily_files_reports_action_needed() -> None:
    root = project_health.ROOT / "journal" / "raw" / "_tmp_health_missing_root"
    if root.exists():
        raise AssertionError(f"temporary test root already exists: {root}")
    root.mkdir(parents=True)
    try:
        report = project_health.build_report(
            dt.date(2099, 1, 1),
            {"timezone": "Asia/Seoul", "notion": {"enabled": False}},
            root=root,
            include_scheduler=False,
        )
    finally:
        root.rmdir()
    assert report["overall"] == "Action Needed"
    assert report["sections"]["daily_collection"]["status"] == "Action Needed"
    assert "run_daily.ps1" in " ".join(report["recommended_actions"])


def test_health_notion_missing_token_is_warning() -> None:
    original_token_status = project_health.token_status
    project_health.token_status = lambda: "missing"
    try:
        status = project_health.check_notion(
            project_health.ROOT,
            {"notion": {"enabled": True, "parent_page_id": "parent"}},
        )
    finally:
        project_health.token_status = original_token_status
    assert status["status"] == "Warning"
    assert status["token"] == "missing"
    assert "set_notion_token.ps1" in " ".join(status["recommended_actions"])


def test_health_notion_warns_when_page_hash_is_stale() -> None:
    root = project_health.ROOT / "journal" / "raw" / "_tmp_health_notion_hash"
    remove_tree(root)
    original_token_status = project_health.token_status
    project_health.token_status = lambda: "set"
    try:
        (root / "journal" / "raw").mkdir(parents=True)
        (root / "journal" / "daily").mkdir(parents=True)
        (root / "journal" / "raw" / "notion_pages.json").write_text(
            json.dumps({"dbdaily:2099-01-01": "page_id", "hash:dbdaily:2099-01-01": "old_hash"}),
            encoding="utf-8",
        )
        (root / "journal" / "daily" / "2099-01-01.md").write_text("# Daily\n\n## Studied\n- Updated\n", encoding="utf-8")
        status = project_health.check_notion(
            root,
            {"timezone": "UTC", "notion": {"enabled": True, "parent_page_id": "parent", "body_mode": "summary"}},
            dt.date(2099, 1, 1),
        )
    finally:
        project_health.token_status = original_token_status
        remove_tree(root)
    assert status["status"] == "Warning"
    assert status["daily_synced"] is True
    assert status["daily_hash_matches"] is False
    assert "notion_sync.py --date 2099-01-01" in " ".join(status["recommended_actions"])


def test_health_notion_delayed_pending_and_stale_finalized() -> None:
    root = project_health.ROOT / "journal" / "raw" / "_tmp_health_notion_delayed"
    remove_tree(root)
    original_token_status = project_health.token_status
    original_now = project_health.now
    project_health.token_status = lambda: "set"
    project_health.now = lambda _config: dt.datetime(2099, 1, 5, 12, 0, tzinfo=dt.timezone.utc)
    config = {
        "timezone": "UTC",
        "notion": {
            "enabled": True,
            "parent_page_id": "parent",
            "sync_policy": {"mode": "delayed_final", "finalize_after_days": 3, "final_body_mode": "full"},
        },
    }
    try:
        (root / "journal" / "raw").mkdir(parents=True)
        (root / "journal" / "daily").mkdir(parents=True)
        (root / "journal" / "daily" / "2099-01-04.md").write_text("# Recent\n", encoding="utf-8")
        recent = project_health.check_notion(root, config, dt.date(2099, 1, 4))
        assert recent["status"] == "OK"
        assert recent["pending_finalization"] is True

        (root / "journal" / "daily" / "2099-01-01.md").write_text("# Old changed\n", encoding="utf-8")
        (root / "journal" / "raw" / "notion_pages.json").write_text(
            json.dumps(
                {
                    "dbdaily:2099-01-01": "page",
                    "hash:dbdaily:2099-01-01": "old_hash",
                    "finalized:dbdaily:2099-01-01": "done",
                }
            ),
            encoding="utf-8",
        )
        stale = project_health.check_notion(root, config, dt.date(2099, 1, 1))
    finally:
        project_health.token_status = original_token_status
        project_health.now = original_now
        remove_tree(root)
    assert stale["status"] == "Warning"
    assert stale["daily_finalized"] is True
    assert stale["daily_hash_matches"] is False
    assert "--force" in " ".join(stale["recommended_actions"])


def test_health_access_denied_scan_reports_warning_without_touching_path() -> None:
    original_scandir = project_health.os.scandir

    def fake_scandir(path):  # noqa: ANN001
        raise PermissionError("denied for test")

    project_health.os.scandir = fake_scandir
    try:
        issues = project_health.scan_access_denied(project_health.ROOT / "journal", project_health.ROOT)
    finally:
        project_health.os.scandir = original_scandir
    assert issues
    assert issues[0]["path"] == "journal"
    assert "denied for test" in issues[0]["error"]


def test_health_access_denied_scan_skips_raw_temp_dirs() -> None:
    assert project_health.should_skip_access_scan(project_health.ROOT / "journal" / "raw" / "tmpabc", project_health.ROOT)
    assert project_health.should_skip_access_scan(project_health.ROOT / "journal" / "raw" / "_tmpabc", project_health.ROOT)
    assert not project_health.should_skip_access_scan(project_health.ROOT / "journal" / "raw" / "2026-05-08.json", project_health.ROOT)


def test_health_scheduler_evaluation_uses_expected_scripts() -> None:
    tasks = [
        {
            "TaskName": "Activity Journal Daily",
            "State": "Ready",
            "Execute": "powershell",
            "Arguments": r"-ExecutionPolicy Bypass -File C:\repo\scripts\run_daily.ps1 -NonInteractive -RefreshDrafts -CatchUpMissed",
            "WorkingDirectory": r"C:\repo",
            "StartWhenAvailable": True,
            "WakeToRun": True,
            "DisallowStartIfOnBatteries": False,
            "StopIfGoingOnBatteries": False,
        },
        {
            "TaskName": "Activity Journal Weekly",
            "State": "Ready",
            "Execute": "powershell",
            "Arguments": r"-ExecutionPolicy Bypass -File C:\repo\scripts\run_weekly.ps1",
            "WorkingDirectory": r"C:\repo",
            "StartWhenAvailable": True,
            "WakeToRun": True,
            "DisallowStartIfOnBatteries": False,
            "StopIfGoingOnBatteries": False,
        },
        {
            "TaskName": "Activity Journal Codex Review",
            "State": "Ready",
            "Execute": "powershell",
            "Arguments": r"-ExecutionPolicy Bypass -File C:\repo\scripts\open_codex_review.ps1",
            "WorkingDirectory": r"C:\repo",
            "StartWhenAvailable": True,
            "WakeToRun": True,
            "DisallowStartIfOnBatteries": False,
            "StopIfGoingOnBatteries": False,
        },
        {
            "TaskName": "Activity Journal Watcher",
            "State": "Ready",
            "Execute": "pythonw.exe",
            "Arguments": r"C:\repo\scripts\activity_watch.py --interval 30 --heartbeat 300 --include-accessibility-text --max-text-chars 20000",
            "WorkingDirectory": r"C:\repo",
            "StartWhenAvailable": True,
            "WakeToRun": False,
            "DisallowStartIfOnBatteries": False,
            "StopIfGoingOnBatteries": False,
        },
        {
            "TaskName": "Activity Journal ChatGPT Receiver",
            "State": "Ready",
            "Execute": "pythonw.exe",
            "Arguments": r"C:\repo\scripts\chatgpt_live_server.py --host 127.0.0.1 --port 8765",
            "WorkingDirectory": r"C:\repo",
            "StartWhenAvailable": True,
            "WakeToRun": False,
            "DisallowStartIfOnBatteries": False,
            "StopIfGoingOnBatteries": False,
        },
    ]
    assert project_health.evaluate_scheduler_tasks(tasks, project_health.ROOT)["status"] == "OK"
    tasks[0]["Arguments"] = r"-ExecutionPolicy Bypass -File C:\repo\scripts\run_daily.ps1"
    assert project_health.evaluate_scheduler_tasks(tasks, project_health.ROOT)["status"] == "Action Needed"
    tasks[0]["Arguments"] = r"-ExecutionPolicy Bypass -File C:\repo\scripts\run_daily.ps1 -NonInteractive -RefreshDrafts -CatchUpMissed"
    tasks[0]["WakeToRun"] = False
    assert project_health.evaluate_scheduler_tasks(tasks, project_health.ROOT)["status"] == "Action Needed"


def test_health_startup_launcher_checks_expected_arguments() -> None:
    root = collect_daily.ROOT / "journal" / "raw" / "_tmp_startup_launcher"
    remove_tree(root)
    try:
        path = root / "Activity Journal Watcher.vbs"
        path.parent.mkdir(parents=True)
        path.write_text('WshShell.Run "pythonw scripts\\activity_watch.py --include-accessibility-text"', encoding="ascii")
        assert project_health.evaluate_startup_launcher(path, ["scripts\\activity_watch.py", "--include-accessibility-text"])["status"] == "OK"
        assert project_health.evaluate_startup_launcher(path, ["scripts\\activity_watch.py", "--missing"])["status"] == "Action Needed"
    finally:
        remove_tree(root)


def test_health_tray_reports_dependencies_and_launcher() -> None:
    status = project_health.check_tray(project_health.ROOT, {"timezone": "UTC", "tray": {"enabled": True, "show_notifications": True}})
    assert status["status"] in {"OK", "Warning"}
    assert status["enabled"] is True
    assert status["show_notifications"] is True
    assert set(status["dependencies"]) == {"pystray", "PIL"}
    assert "startup_launcher" in status


def test_install_scripts_include_tray_assets() -> None:
    install = (collect_daily.ROOT / "scripts" / "install_local.ps1").read_text(encoding="utf-8")
    setup = (collect_daily.ROOT / "scripts" / "setup_task.ps1").read_text(encoding="utf-8")
    uninstall = (collect_daily.ROOT / "scripts" / "uninstall_local.ps1").read_text(encoding="utf-8")
    assert "Activity Journal Tray.vbs" in install
    assert "Activity Journal Dashboard.lnk" in install
    assert "dashboard_app.py" in install
    assert "pystray pillow" in install
    assert "activity-journal.example.json" in install
    assert "Move-ExtensionArtifacts" in install
    assert "ActivityJournal\\browser_extension" in install
    assert "activity-journal.example.json" in setup
    assert "Activity Journal Tray.vbs" in uninstall
    assert "Activity Journal Dashboard.lnk" in uninstall
    assert "Activity Journal Tray.lnk" in uninstall


def test_dashboard_renders_core_sections() -> None:
    markdown = "# Daily\n\n## Studied\n- Read docs\n\n## Built\n- Added dashboard\n"
    sections = dashboard_app.parse_markdown_sections(markdown)
    assert sections["Studied"] == "- Read docs"
    assert sections["Built"] == "- Added dashboard"
    payload = {
        "date": "2099-01-01",
        "week": "2099-W01",
        "health": {
            "overall": "OK",
            "recommended_actions": [],
            "sections": {
                "daily_collection": {"status": "OK"},
                "sqlite_database": {"status": "OK"},
            },
        },
        "daily": {"path": "journal/daily/2099-01-01.md", "exists": True, "sections": sections, "excerpt": markdown},
        "weekly": {"path": "journal/weekly/2099-W01.md", "exists": False, "excerpt": ""},
        "questions": {"path": "journal/questions/2099-01-01.md", "exists": False, "excerpt": ""},
        "projects": {"path": "journal/raw/project_rollup_2099-01-01.json", "exists": False, "projects": []},
        "recent": {"daily": [], "weekly": [], "projects": []},
        "search": {"query": "", "error": None, "results": []},
    }
    rendered = dashboard_app.render_page(payload, "").decode("utf-8")
    assert "Activity Journal Dashboard" in rendered
    assert "Daily Sections" in rendered
    assert "Added dashboard" in rendered


def test_public_repo_safety_artifacts() -> None:
    root = collect_daily.ROOT
    gitignore = (root / ".gitignore").read_text(encoding="utf-8")
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    example = json.loads((root / "config" / "activity-journal.example.json").read_text(encoding="utf-8"))
    assert "journal/" in gitignore
    assert "imports/" in gitignore
    assert "config/activity-journal.json" in gitignore
    assert "browser_extension/*.pem" in gitignore
    assert not (root / "browser_extension" / "chatgpt-live-capture.pem").exists()
    assert not (root / "browser_extension" / "chatgpt-live-capture.crx").exists()
    assert not (root / "browser_extension" / "chatgpt-live-capture-update.xml").exists()
    assert "*.sqlite3" in gitignore
    assert "pystray" in requirements
    assert "Pillow" in requirements
    assert example["external_inputs"]["enabled"] is False
    assert example["external_inputs"]["include_raw_text"] is False
    assert example["codex"]["include_history_text"] is False
    assert example["notion"]["enabled"] is False
    assert example["notion"]["parent_page_id"] == ""
    assert (root / "docs" / "privacy-security.md").exists()
    assert (root / "docs" / "publishing.md").exists()
    assert (root / "docs" / "architecture.md").exists()
    assert (root / "docs" / "demo-daily-log.md").exists()
    assert (root / "LICENSE").exists()
    assert (root / ".github" / "workflows" / "ci.yml").exists()


def test_chatgpt_live_server_uses_local_extension_artifact_dir() -> None:
    root = collect_daily.ROOT / "journal" / "raw" / "_tmp_extension_artifacts"
    remove_tree(root)
    original_dir = os.environ.get(chatgpt_live_server.EXTENSION_ARTIFACT_ENV)
    try:
        artifact_dir = root / "artifacts"
        artifact_dir.mkdir(parents=True)
        update_path = artifact_dir / chatgpt_live_server.EXTENSION_UPDATE_XML_NAME
        crx_path = artifact_dir / chatgpt_live_server.EXTENSION_CRX_NAME
        update_path.write_text("<update />", encoding="utf-8")
        crx_path.write_bytes(b"crx")
        os.environ[chatgpt_live_server.EXTENSION_ARTIFACT_ENV] = str(artifact_dir)
        assert chatgpt_live_server.extension_update_xml_path() == update_path
        assert chatgpt_live_server.extension_crx_path() == crx_path
    finally:
        if original_dir is None:
            os.environ.pop(chatgpt_live_server.EXTENSION_ARTIFACT_ENV, None)
        else:
            os.environ[chatgpt_live_server.EXTENSION_ARTIFACT_ENV] = original_dir
        remove_tree(root)


def test_health_json_only_no_write_outputs_parseable_json_without_token_value() -> None:
    secret = "secret_project_health_token"
    env = subprocess_env()
    env["NOTION_TOKEN"] = secret
    result = subprocess.run(
        [sys.executable, "scripts/project_health.py", "--json-only", "--no-write"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["report_type"] == "activity_journal_health"
    assert secret not in result.stdout


def test_health_external_inputs_warns_without_chatgpt_export() -> None:
    root = project_health.ROOT / "journal" / "raw" / "_tmp_health_external"
    remove_tree(root)
    try:
        (root / "inbox").mkdir(parents=True)
        config = external_config(root)
        status = project_health.check_external_inputs(dt.date(2099, 1, 1), root, config)
        assert status["status"] == "Warning"
        assert status["chatgpt_count"] == 0
        assert "open_chatgpt_export.ps1" in " ".join(status["recommended_actions"])
    finally:
        remove_tree(root)


def write_chrome_extension_manifest(root: Path) -> None:
    manifest_path = root / "browser_extension" / "chatgpt-live-capture" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "name": "Activity Journal ChatGPT Capture",
                "version": "0.1.5",
                "host_permissions": [
                    "http://127.0.0.1:8765/*",
                    "https://chatgpt.com/*",
                    "https://gemini.google.com/*",
                ],
                "background": {"service_worker": "background.js"},
                "content_scripts": [
                    {
                        "matches": ["https://chatgpt.com/*", "https://gemini.google.com/*"],
                        "js": ["content.js"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_health_chrome_extension_reports_ok_with_receiver_and_capture() -> None:
    root = project_health.ROOT / "journal" / "raw" / "_tmp_health_chrome_extension"
    remove_tree(root)
    original_probe = project_health.probe_chatgpt_receiver
    try:
        write_chrome_extension_manifest(root)
        log_path = root / "journal" / "raw" / "chatgpt_live.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            json.dumps(
                {
                    "captured_at": "2099-01-01T01:00:00+00:00",
                    "app": "chatgpt.com",
                    "title": "Live chat",
                    "url": "https://chatgpt.com/c/live",
                    "content_hash": "live",
                    "text": "Live text",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        config = external_config(root)
        config["external_inputs"]["chatgpt_live"] = {
            "enabled": True,
            "log_path": str(log_path),
            "server_host": "127.0.0.1",
            "server_port": 8765,
        }
        project_health.probe_chatgpt_receiver = lambda host, port: {"status": "OK", "url": f"http://{host}:{port}/health", "ok": True}
        status = project_health.check_chrome_extension(dt.date(2099, 1, 1), root, config)
        assert status["status"] == "OK"
        assert status["recent_capture_count"] == 1
        assert status["manifest"]["status"] == "OK"
        assert status["receiver"]["status"] == "OK"
    finally:
        project_health.probe_chatgpt_receiver = original_probe
        remove_tree(root)


def test_health_chrome_extension_warns_without_recent_capture() -> None:
    root = project_health.ROOT / "journal" / "raw" / "_tmp_health_chrome_extension_empty"
    remove_tree(root)
    original_probe = project_health.probe_chatgpt_receiver
    try:
        write_chrome_extension_manifest(root)
        config = external_config(root)
        config["external_inputs"]["chatgpt_live"] = {
            "enabled": True,
            "log_path": str(root / "journal" / "raw" / "chatgpt_live.jsonl"),
            "server_host": "127.0.0.1",
            "server_port": 8765,
        }
        project_health.probe_chatgpt_receiver = lambda host, port: {"status": "OK", "url": f"http://{host}:{port}/health", "ok": True}
        status = project_health.check_chrome_extension(dt.date(2099, 1, 1), root, config)
        assert status["status"] == "Warning"
        assert status["recent_capture_count"] == 0
    finally:
        project_health.probe_chatgpt_receiver = original_probe
        remove_tree(root)


def test_health_retention_warns_when_recent_raw_exceeds_limit() -> None:
    root = project_health.ROOT / "journal" / "raw" / "_tmp_health_retention"
    remove_tree(root)
    try:
        log_path = root / "journal" / "raw" / "chatgpt_live.jsonl"
        log_path.parent.mkdir(parents=True)
        log_path.write_text(
            json.dumps(
                {
                    "captured_at": "2099-04-30T10:00:00+00:00",
                    "app": "chatgpt.com",
                    "title": "Large recent log",
                    "text": "x" * 1000,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        config = retention_config(root, max_raw_mb=0.000001)
        status = project_health.check_retention(root, config)
    finally:
        remove_tree(root)
    assert status["status"] == "Warning"
    assert "exceed" in " ".join(status["recommended_actions"])


def sqlite_test_config(root: Path, *, text_retention_days: int = 90) -> dict:
    return {
        "timezone": "UTC",
        "database": {
            "enabled": True,
            "path": str(root / "journal" / "activity_journal.sqlite3"),
            "text_retention_days": text_retention_days,
            "keep_metadata_after_text_prune": True,
            "enable_fts": True,
        },
        "external_inputs": {
            "chatgpt_live": {
                "enabled": True,
                "log_path": str(root / "journal" / "raw" / "chatgpt_live.jsonl"),
            },
            "activity_watch": {
                "enabled": True,
                "log_path": str(root / "journal" / "raw" / "activity_watch.jsonl"),
            },
        },
        "retention": {
            "enabled": True,
            "keep_recent_days": text_retention_days,
            "max_raw_mb": 500,
            "archive_dir": str(root / "journal" / "archive" / "raw"),
            "delete_archives": False,
        },
        "notion": {"enabled": False},
    }


def write_sqlite_daily_fixture(root: Path, day: dt.date) -> None:
    raw_dir = root / "journal" / "raw"
    daily_dir = root / "journal" / "daily"
    raw_dir.mkdir(parents=True, exist_ok=True)
    daily_dir.mkdir(parents=True, exist_ok=True)
    timestamp = f"{day.isoformat()}T10:00:00+00:00"
    raw = {
        "date": day.isoformat(),
        "window": {"since": f"{day.isoformat()}T00:00:00+00:00", "until": f"{day.isoformat()}T23:59:59+00:00"},
        "git_activity": [],
        "modified_files": {},
        "codex": {"history": [], "sessions": []},
        "external_inputs": {
            "inbox": [],
            "chatgpt": [],
            "chatgpt_live": [
                {
                    "title": "SQLite daily capture",
                    "app": "chatgpt.com",
                    "url": "https://chatgpt.com/c/sqlite",
                    "conversation_id": "sqlite",
                    "captured_at": timestamp,
                    "content_hash": "daily-live-hash",
                    "source_path": "journal/raw/chatgpt_live.jsonl",
                    "text": "needlesqlitephrase from daily capture",
                }
            ],
            "browser_history": [
                {
                    "title": "SQLite docs",
                    "url": "https://sqlite.org/fts5.html",
                    "domain": "sqlite.org",
                    "profile": "Chrome/Default",
                    "source_path": "History",
                    "first_visit_time": timestamp,
                    "last_visit_time": timestamp,
                    "visit_count": 1,
                }
            ],
            "recent_files": [],
            "activity_watch": [
                {
                    "process": "code.exe",
                    "title": "SQLite implementation",
                    "first_seen": timestamp,
                    "last_seen": timestamp,
                    "sample_count": 1,
                    "text_capture_count": 1,
                    "text_hashes": ["watch-daily-hash"],
                    "text_excerpt": "Visible needlesqlitephrase app text",
                    "text": "Visible needlesqlitephrase app text",
                }
            ],
            "warnings": [],
        },
    }
    (raw_dir / f"{day.isoformat()}.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    (daily_dir / f"{day.isoformat()}.md").write_text("# Daily\n\nneedlesqlitephrase\n", encoding="utf-8")
    (raw_dir / "chatgpt_live.jsonl").write_text(
        json.dumps(
            {
                "captured_at": timestamp,
                "received_at": timestamp,
                "source": "browser_extension",
                "app": "chatgpt.com",
                "title": "SQLite raw capture",
                "url": "https://chatgpt.com/c/sqlite-raw",
                "conversation_id": "sqlite-raw",
                "content_hash": "raw-live-hash",
                "text": "needlesqlitephrase from raw live log",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (raw_dir / "activity_watch.jsonl").write_text(
        json.dumps(
            {
                "ts": timestamp,
                "title": "SQLite raw app",
                "process": "code.exe",
                "text": "needlesqlitephrase from raw app log",
                "text_hash": "raw-watch-hash",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def test_sqlite_sync_indexes_daily_and_jsonl_idempotently() -> None:
    root = collect_daily.ROOT / "journal" / "raw" / "_tmp_sqlite_sync"
    remove_tree(root)
    day = dt.date(2099, 2, 1)
    try:
        config = sqlite_test_config(root)
        write_sqlite_daily_fixture(root, day)
        first = sync_sqlite.sync_date(day, config, root)
        second = sync_sqlite.sync_date(day, config, root)
        db_path = activity_db.database_settings(config, root)["path"]
        conn = activity_db.connect_database(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM events WHERE local_date = ?", (day.isoformat(),)).fetchone()[0]
            source_rows = conn.execute("SELECT source, COUNT(*) FROM events GROUP BY source").fetchall()
            source_counts = {row[0]: row[1] for row in source_rows}
            results = activity_db.search_events(conn, "needlesqlitephrase", limit=10)
        finally:
            conn.close()
        health = project_health.check_sqlite_database(day, root, config)
    finally:
        remove_tree(root)
    assert first["status"] == "OK"
    assert second["status"] == "OK"
    assert count == first["event_count"]
    assert source_counts["chatgpt_live"] == 1
    assert source_counts["chatgpt_live_raw"] == 1
    assert source_counts["activity_watch_sample"] == 1
    assert results
    assert health["status"] == "OK"
    assert health["checked_event_count"] == count


def test_sqlite_retention_prunes_old_text_but_keeps_metadata() -> None:
    root = collect_daily.ROOT / "journal" / "raw" / "_tmp_sqlite_retention"
    remove_tree(root)
    try:
        config = sqlite_test_config(root)
        db_path = activity_db.database_settings(config, root)["path"]
        conn = activity_db.open_configured_database(config, root)
        try:
            activity_db.upsert_event(
                conn,
                {
                    "source": "chatgpt_live",
                    "local_date": "2099-01-01",
                    "occurred_at": "2099-01-01T10:00:00+00:00",
                    "title": "Old searchable metadata",
                    "text_excerpt": "Old excerpt remains",
                    "text": "old full text should be pruned",
                    "raw_json": {"text": "old full text should be pruned"},
                    "raw_path": "journal/raw/2099-01-01.json",
                    "content_hash": "old-hash",
                },
            )
            conn.commit()
        finally:
            conn.close()
        dry_run = retention_cleanup.cleanup_report(
            config,
            root=root,
            dry_run=True,
            now_value=dt.datetime(2099, 5, 1, tzinfo=dt.timezone.utc),
        )
        report = retention_cleanup.cleanup_report(
            config,
            root=root,
            dry_run=False,
            now_value=dt.datetime(2099, 5, 1, tzinfo=dt.timezone.utc),
        )
        conn = activity_db.connect_database(db_path)
        try:
            row = conn.execute("SELECT title, text_excerpt, text, raw_json, text_pruned FROM events").fetchone()
            results = activity_db.search_events(conn, "Old searchable metadata", limit=10)
        finally:
            conn.close()
    finally:
        remove_tree(root)
    assert dry_run["database"]["prunable_event_count"] == 1
    assert report["database"]["pruned_event_count"] == 1
    assert row["title"] == "Old searchable metadata"
    assert row["text_excerpt"] == "Old excerpt remains"
    assert row["text"] is None
    assert row["raw_json"] is None
    assert row["text_pruned"] == 1
    assert not results


def main() -> None:
    test_weekly_answered_problem_filter()
    test_weekly_cleanup_answered_daily_removes_exact_problem_matches()
    test_weekly_cleanup_answered_daily_adds_placeholder_when_empty()
    test_weekly_display_path_uses_markdown_slashes()
    test_weekly_built_items_keep_daily_date_context()
    test_notion_status_property()
    test_notion_section_omits_placeholders()
    test_notion_request_errors()
    test_notion_main_reports_runtime_errors()
    test_notion_database_id_saved_immediately()
    test_notion_page_id_saved_before_remaining_blocks_append()
    test_notion_body_summary_omits_large_detail_sections()
    test_notion_recreates_large_existing_page_on_update()
    test_notion_markdown_api_create_payload()
    test_notion_delayed_final_splits_details_and_finalizes()
    test_notion_delayed_final_does_not_update_finalized_without_force()
    test_notion_delayed_due_dates_skip_recent_and_finalized()
    test_run_daily_uses_delayed_notion_finalization()
    test_notion_load_state_reports_invalid_json()
    test_notion_load_state_rejects_invalid_shape()
    test_notion_main_reports_invalid_state_without_traceback()
    test_collect_write_once_preserves_existing_content()
    test_collect_ignores_generated_journal_paths()
    test_collect_find_git_repos_detects_git_dirs_when_git_is_ignored()
    test_collect_sanitizes_codex_sessions()
    test_collect_manual_notes_enter_studied_section()
    test_collect_daily_uses_explicit_decisions_placeholder()
    test_render_daily_displays_configured_timezone_clocks()
    test_render_question_file_uses_readable_korean_template()
    test_collect_external_inputs_reads_inbox_date_files()
    test_collect_chatgpt_export_json_and_zip()
    test_collect_chatgpt_malformed_export_warns_without_failure()
    test_collect_browser_history_reads_chromium_history()
    test_collect_recent_files_reads_configured_roots()
    test_collect_activity_watch_reads_window_log()
    test_collect_chatgpt_live_reads_capture_log()
    test_privacy_filters_block_raw_domains_and_apps()
    test_capture_controls_pause_sources_and_exclusions()
    test_capture_toggles_block_live_and_activity_raw_storage()
    test_tray_actions_pause_and_resume_capture_config()
    test_tray_dependency_status_shape()
    test_collect_external_inputs_skips_when_capture_paused()
    test_collect_and_render_apply_privacy_exclusions()
    test_retention_archives_old_jsonl_and_keeps_recent()
    test_collect_external_raw_text_policy_and_daily_privacy()
    test_collect_uses_configured_timezone_for_window()
    test_collect_timezone_fallback_is_local_naive()
    test_collect_codex_respects_half_open_window()
    test_weekly_parse_date_uses_explicit_date_first()
    test_collect_invalid_date_reports_cli_error()
    test_collect_print_default_date_exits_without_writing()
    test_weekly_invalid_date_reports_cli_error()
    test_notion_invalid_date_reports_cli_error_before_token_check()
    test_notion_invalid_week_reports_cli_error_before_token_check()
    test_notion_valid_date_without_token_still_skips()
    test_health_question_parser_ignores_template_fences()
    test_question_quality_generates_core_candidates()
    test_question_quality_excludes_answered_ids_and_fenced_examples()
    test_health_question_quality_missing_file_is_ok()
    test_health_question_quality_unresolved_candidates_warn()
    test_project_review_rolls_up_daily_logs_and_metadata_goal()
    test_project_review_preserves_existing_metadata_values()
    test_question_quality_adds_project_goal_candidate()
    test_health_project_review_missing_and_missing_goal_states()
    test_notion_project_properties_and_state_key()
    test_auto_recover_safe_generation_preserves_review_files()
    test_auto_recover_creates_external_dirs_and_logs_recovery()
    test_auto_recover_defers_unsafe_user_actions()
    test_auto_recover_opens_codex_only_when_allowed()
    test_auto_recover_does_not_open_codex_from_run_daily()
    test_health_missing_daily_files_reports_action_needed()
    test_health_notion_missing_token_is_warning()
    test_health_notion_warns_when_page_hash_is_stale()
    test_health_notion_delayed_pending_and_stale_finalized()
    test_health_access_denied_scan_reports_warning_without_touching_path()
    test_health_access_denied_scan_skips_raw_temp_dirs()
    test_health_scheduler_evaluation_uses_expected_scripts()
    test_health_startup_launcher_checks_expected_arguments()
    test_health_tray_reports_dependencies_and_launcher()
    test_install_scripts_include_tray_assets()
    test_dashboard_renders_core_sections()
    test_public_repo_safety_artifacts()
    test_chatgpt_live_server_uses_local_extension_artifact_dir()
    test_health_json_only_no_write_outputs_parseable_json_without_token_value()
    test_health_external_inputs_warns_without_chatgpt_export()
    test_health_chrome_extension_reports_ok_with_receiver_and_capture()
    test_health_chrome_extension_warns_without_recent_capture()
    test_health_retention_warns_when_recent_raw_exceeds_limit()
    test_sqlite_sync_indexes_daily_and_jsonl_idempotently()
    test_sqlite_retention_prunes_old_text_but_keeps_metadata()
    print("Smoke tests passed.")


if __name__ == "__main__":
    main()
