# Activity Journal

Local-first activity journal for tracking what you studied, built, decided, and need to clarify.

This MVP collects evidence from:

- Codex CLI history and session index
- Git repositories under configured project roots
- Recently modified files under configured project roots
- Optional inbox notes, ChatGPT exports, live ChatGPT/Gemini browser captures, browser history, and foreground app activity

It writes daily and weekly Markdown drafts, builds project-level rollups, indexes local evidence in SQLite, and can sync summary/final logs into Notion.

## What You Get

- Daily logs under `journal/daily/YYYY-MM-DD.md`
- Weekly reviews under `journal/weekly/YYYY-Www.md`
- Project rollups under `journal/projects/`
- A local health report for scheduler, Notion, SQLite, capture, and file access
- A local browser dashboard for health, daily sections, project rollups, questions, and SQLite search
- Optional Windows scheduled tasks, tray controls, and browser-extension capture
- Privacy-first defaults: generated journals, raw logs, local config, databases, and secrets stay untracked

## Repository Layout

```text
browser_extension/   Optional local ChatGPT/Gemini capture extension source
config/              Public example config only; private config is ignored
docs/                Architecture, demo output, publishing, and privacy/security notes
prompts/             Codex review prompt template
scripts/             Collectors, sync jobs, health checks, tray/settings apps
```

See `docs/demo-daily-log.md` for a synthetic daily output example.
See `CONTRIBUTING.md` before proposing changes that touch capture, sync, or local data handling.

## Public Repository Safety

This repository is safe to publish only when local data stays untracked. The committed config is `config/activity-journal.example.json`; each user gets a private `config/activity-journal.json` during setup. The `.gitignore` excludes local journals, imports, inbox files, manual notes, SQLite databases, config backups, and Chrome extension signing artifacts.

Before publishing or committing, review:

- `docs/privacy-security.md`
- `docs/publishing.md`

## Quick Start

1. Install dependencies, create local folders, and create `config/activity-journal.json` from the example if it does not exist:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_local.ps1 -WhatIf
powershell -ExecutionPolicy Bypass -File scripts/install_local.ps1
```

2. Edit `config/activity-journal.json` for your timezone, project folders, and optional integrations.
3. Run a daily collection:

```powershell
python scripts/collect_daily.py
```

4. Run a weekly review:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_weekly.ps1
```

When `scripts/run_daily.ps1` or the Python CLIs run without `--date`, they use `config/activity-journal.json`'s `timezone` value to decide today's date. The daily evidence window is that timezone's midnight-to-midnight range. Passing `--date YYYY-MM-DD` uses that explicit date.

Run the interactive daily review flow:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_daily.ps1
```

Run evidence collection only:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_daily.ps1 -NonInteractive
```

Scheduled daily runs use `-RefreshDrafts`, so an unreviewed draft can be refreshed with the latest same-day evidence before the Notion sync runs. Manual runs without that switch still preserve existing review Markdown by default.

Re-running collection for the same date refreshes `journal/raw/YYYY-MM-DD.json` but preserves existing `journal/daily` and `journal/questions` Markdown files so reviewed answers are not overwritten.

To intentionally regenerate those review Markdown files, use `python scripts/collect_daily.py --date YYYY-MM-DD --overwrite-review-files`. Use this only for recovery because it can overwrite reviewed answers.

Privacy note: `codex.include_history_text` controls whether Codex history/session evidence stores only IDs and timestamps or also richer local context. The example config defaults this to `false`; enable it only if you want richer local context in raw evidence.

Optional manual notes can fill gaps that automation cannot see. Create `manual_notes/YYYY-MM-DD.md` before collection and write short bullet lines:

```markdown
- Learned how Codex skills and Notion sync fit together.
- Reviewed a browser article about weekly review systems.
```

Those lines are added to the daily `Studied` section.

External inputs can expand what the system sees beyond Git and Codex. The collector reads:

- `inbox/YYYY-MM-DD.md`
- `inbox/YYYY-MM-DD/*.md`
- `inbox/YYYY-MM-DD/*.txt`
- recently modified `inbox/*.md` and `inbox/*.txt`
- ChatGPT export `.zip` files or `conversations.json` files under `imports/chatgpt`
- live ChatGPT/Gemini browser captures from the local `chatgpt_live_server.py` receiver
- local Chromium browser history from Chrome, Edge, Brave, and Vivaldi profiles
- recently modified study/work files under configured Desktop, Documents, and Downloads roots
- foreground app/window activity captured by `scripts/activity_watch.py`, including visible accessibility text for supported active apps when available

Open the official ChatGPT/OpenAI export flow:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/open_chatgpt_export.ps1
```

After the export email arrives, place the downloaded `.zip` or `conversations.json` under `imports/chatgpt`. The next daily collection will read matching conversations for the target date. The helper script only opens the official pages; it does not store passwords, tokens, cookies, or browser session data.

External input privacy note: `external_inputs.include_raw_text` controls whether inbox, ChatGPT, and visible active-window text is stored in `journal/raw/YYYY-MM-DD.json`. The example config keeps external inputs and raw text disabled by default. Browser history stores visited page titles, URLs, timestamps, profile names, and visit counts, but not passwords, cookies, or browser session tokens. Live ChatGPT/Gemini capture stores page text sent by the optional local browser extension to `127.0.0.1`. Recent-file tracking stores paths, filenames, modified times, and sizes. Activity watch stores foreground window titles, process names, timestamps, sample counts, and visible accessibility text for supported apps when Windows exposes it. Daily Markdown receives summarized study signals plus ChatGPT Live Captures, Browser Activity, Recent Files, and App Activity sections.

Optional high-fidelity live ChatGPT/Gemini page-text capture:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/open_chatgpt_capture_extension.ps1
```

The foreground activity watcher now captures visible active-window text automatically when Windows exposes it. The local browser extension is still available if you want a more complete in-page ChatGPT/Gemini transcript. Chrome requires one manual approval for a local unpacked extension. The repository keeps only the unpacked source at `browser_extension/chatgpt-live-capture/`; private signing keys and packaged update artifacts belong in `%LOCALAPPDATA%\ActivityJournal\browser_extension\` or the directory named by `ACTIVITY_JOURNAL_EXTENSION_ARTIFACT_DIR`. After the extension is loaded, normal ChatGPT/Gemini use is captured automatically through the local receiver; no password, cookie, or browser session token is stored by Activity Journal.

Run local smoke tests:

```powershell
python scripts/smoke_tests.py
```

Open the local dashboard:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/open_dashboard.ps1
```

The dashboard runs on `127.0.0.1:8776` by default. It reads local Markdown, health status, project rollups, and SQLite search results without uploading data.

Check whether the automation is healthy:

```powershell
python scripts/project_health.py
```

This writes `journal/system_status.md` and `journal/raw/system_status.json`. The report checks today's local files, pending Codex Review questions, weekly review presence, Notion token/state setup, Windows scheduled tasks, and journal folder access. It does not call the Notion API and does not print token values.

If the health report says `Recent file item limit reached`, the collector is working but found more recently modified files than it is configured to keep. Raise `external_inputs.recent_files.max_items` in your private `config/activity-journal.json`, narrow `external_inputs.recent_files.roots`, or add noisy folders to the scan ignore list.

Run question quality diagnostics:

```powershell
python scripts/question_quality.py --date YYYY-MM-DD
```

This writes `journal/raw/question_candidates_YYYY-MM-DD.json`. The file is a generated diagnostic artifact and can be overwritten by the next run. It does not modify `journal/questions/YYYY-MM-DD.md` or `journal/daily/YYYY-MM-DD.md`; Codex Review reads the candidates and decides whether any question is actually needed.

Build project-level rollups:

```powershell
python scripts/project_review.py --date YYYY-MM-DD
```

This writes `journal/projects/<project-slug>.md` and `journal/raw/project_rollup_YYYY-MM-DD.json`. Project goals, status, and links live in `journal/projects/project_metadata.json`; existing metadata values are preserved, and new projects only receive empty default records until Codex Review asks for the missing goal.

Run safe auto recovery:

```powershell
python scripts/auto_recover.py --date YYYY-MM-DD
```

This checks health, runs only allowlisted local generation repairs, writes `journal/recovery_status.md` and `journal/raw/recovery_YYYY-MM-DD.json`, then opens Codex Review only when user decisions remain. Scheduled daily runs call it with `--no-open-codex --from-run-daily` so non-interactive collection does not open a review window.

Generated files:

```text
journal/daily/YYYY-MM-DD.md
journal/weekly/YYYY-Www.md
journal/projects/<project-slug>.md
journal/questions/YYYY-MM-DD.md
journal/raw/YYYY-MM-DD.json
journal/system_status.md
journal/recovery_status.md
journal/raw/system_status.json
journal/raw/question_candidates_YYYY-MM-DD.json
journal/raw/project_rollup_YYYY-MM-DD.json
journal/raw/recovery_YYYY-MM-DD.json
```

## Daily Notion Routine

Use the generated daily file as the Notion `Daily Activity Log` draft.

Recommended Notion fields:

- `Date`
- `Projects`
- `Studied`
- `Built`
- `Decisions`
- `Problems`
- `Next Actions`
- `Sources`
- `Status`: `Draft` or `Confirmed`

The `journal/questions` file is a local-only review queue. It is not meant to be copied to Notion.

The preferred flow is interactive Codex Review. The scheduled collector gathers evidence first, then opens Codex so Codex can decide whether any clarification is actually needed. If the raw evidence and daily draft are already clear, Codex should not ask extra questions.

When Codex does ask, it asks in Korean, explains what evidence triggered the question, and includes a practical recommended answer. Your answer is saved back into `journal/questions/YYYY-MM-DD.md`, merged into the daily log, then weekly review and Notion sync are run.

Questions are written in a structured format:

```text
## Q: <stable_id>
Category: <배운 점|작업명|상태|결정|다음 행동|설정 문제>
Confidence: <high|medium|low>

Question:
<사용자에게 물어볼 질문>

Context:
- <왜 묻는지>
- <어떤 파일/상황을 근거로 묻는지>

Recommendation:
<추천 답변>

Options:
- 추천 답변으로 저장
- 수정해서 저장: ...
- 저장하지 않음

Answer:
```

이 형식은 이렇게 읽으면 됩니다:

- `stable_id`는 생성된 질문과 답변을 연결하는 내부 ID입니다. 사용자가 직접 관리할 필요는 없습니다.
- `Context`는 Codex가 왜 묻는지, 어떤 근거가 불명확했는지 설명합니다.
- `Recommendation`은 Codex가 가장 실용적이라고 판단한 추천 답변입니다. 맞아 보이면 `추천 답변으로 저장`이라고 답하면 됩니다.
- 추천 답변이 대체로 맞지만 표현을 바꾸고 싶으면 `수정해서 저장: ...` 뒤에 원하는 답변을 적습니다.
- 기록에 남길 필요가 없으면 `저장하지 않음`이라고 답합니다.

## Notion Sync

Notion sync can be enabled in `config/activity-journal.json`, but the token is not stored in this repo. The sync prefers Notion databases (`Daily Activity Log`, `Weekly Review`, and `Project Log`) and falls back to ordinary child pages if database creation is unavailable.

Set a user environment variable if you want scheduled sync:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/set_notion_token.ps1
```

The sync script can also read the saved Windows user environment value directly, so a full restart is usually not required for manual runs. Scheduled tasks may still require a fresh logon session on some Windows setups.

Notion page and database IDs are tracked in `journal/raw/notion_pages.json` to avoid duplicate pages/databases. Do not edit or delete this file unless you intentionally want to reconnect Notion state; if it is damaged, local Markdown logs remain separate.

For daily logs, the local Markdown `Status:` line is the source of truth for the Notion `Status` property. `Status: Confirmed` syncs as `Confirmed`; anything else syncs as `Draft`. Codex Review should only mark a daily log as `Confirmed` after all questions are answered and unresolved `Problems` are cleared.

Project logs sync to the Notion `Project Log` database when `journal/projects/*.md` exists. Project `Status` is limited to `Active`, `Paused`, or `Done`; unknown or invalid local values sync as `Active`.

Verify token setup:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/set_notion_token.ps1 -VerifyOnly
```

## Local MVP Settings App

Open the local settings UI:

```powershell
python scripts/settings_app.py
```

Open the dashboard directly:

```powershell
python scripts/dashboard_app.py
```

Validate the config without opening the UI:

```powershell
python scripts/settings_app.py --validate-config
```

Install or repair the local MVP shortcuts and scheduled tasks:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_local.ps1 -WhatIf
powershell -ExecutionPolicy Bypass -File scripts/install_local.ps1
```

Uninstall scheduled tasks and shortcuts while preserving `journal\` and `config\` data:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/uninstall_local.ps1 -WhatIf
powershell -ExecutionPolicy Bypass -File scripts/uninstall_local.ps1
```

The settings UI can pause/resume all capture, toggle individual sources, start temporary privacy mode, and edit excluded apps/domains.

Start the tray icon manually:

```powershell
python scripts/tray_app.py
```

The tray menu supports opening the dashboard, quick pause for 15/30/60 minutes, pause until tomorrow, resume now, opening Settings, refreshing status, and running a health check. `scripts/install_local.ps1` also installs the tray dependencies (`pystray`, `Pillow`), creates Start Menu shortcuts, and adds a Windows startup launcher.

## Schedule on Windows

Preview the task registration command:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup_task.ps1 -WhatIf
```

Register daily and weekly scheduled tasks:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup_task.ps1
```

The scheduled tasks run as follows:

- `Activity Journal Daily`: collects evidence and writes a local draft at 23:50.
- `Activity Journal Codex Review`: opens Codex at 23:51 so questions and answers stay inside Codex.
- `Activity Journal Weekly`: writes a weekly review, refreshes project rollups, and syncs them to Notion on Sunday at 23:55.
- `Activity Journal Watcher`: starts at Windows logon and records foreground app/window changes for later daily collection.
- `Activity Journal ChatGPT Receiver`: starts at Windows logon and receives optional browser-extension captures on `127.0.0.1:8765`.
- `Activity Journal Tray`: starts at Windows logon from the Startup folder and gives quick pause/resume controls.

`Activity Journal Codex Review` opens only when the matching `journal/raw/YYYY-MM-DD.json` and `journal/daily/YYYY-MM-DD.md` files exist. If it does not open, run `powershell -ExecutionPolicy Bypass -File scripts/run_daily.ps1 -NonInteractive` first to regenerate the daily inputs.

Tasks use `StartWhenAvailable`, so if the computer was off at the scheduled time, Windows can run the missed task after the computer is available again.

## Limits

This does not automatically read every ChatGPT web conversation. For ChatGPT, use one of these:

- Export important conversations and place them in a configured input folder.
- Paste important conclusions into the generated daily draft.
- Add a future API-based workflow if you use ChatGPT through an API or controlled app.

Auto recovery does not delete files, change permissions, register scheduled tasks, set Notion tokens, or retry Notion API writes. Those remain explicit user actions.
