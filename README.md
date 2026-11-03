# tum-moodle-drive-sync

A Python 3.12+ CLI that copies selected TUM Moodle course files into a local folder managed by **Google Drive for desktop**. You sign in interactively through TUM SSO; subsequent runs reuse the locally protected Moodle session until it expires. No university password, Moodle API token or Google OAuth credential is configured.

**Status: MVP, locally tested with synthetic Moodle fixtures. Live TUM login, real course discovery, Windows behavior and Google Drive cloud upload have not been verified.** The cloud environment used for development blocked access to TUM Moodle. See [upstream research and licensing](docs/upstream-research.md) and the manual acceptance steps below. This is an independent student tool, not an official TUM service.

## What the MVP supports

- Interactive Playwright browser login, including whatever SSO/MFA steps TUM requires from you.
- Enrolled-course discovery through Moodle's session AJAX interface; explicit selection by ID.
- Standard Moodle File and Folder activities, direct/embedded resource links, and linked section pages.
- PDF, PPTX, DOCX, ZIP and other binary/text downloadable file types, without an extension allowlist. HTML/XHTML and JSON responses are rejected to avoid saving login/error pages.
- Course/topic directories, Windows-safe filenames, deterministic resource identifiers and collision-safe new versions. Source deletions do not delete local files.
- SQLite source URL/revision/hash/download/check timestamps and event history; per-run text changelogs and rotating logs.
- Sequential requests, configurable limits, HTTP conditional requests, scheduled CLI execution and an exclusive state-directory lock.

It does **not** download arbitrary external URLs, videos from external providers, assignments/submissions, forum attachments, quizzes, or every custom Moodle plugin. JavaScript-only/lazy resources without standard HTML links may be missed. Folder subdirectories are flattened into their topic folder with resource IDs preventing collisions. Course AJAX/HTML changes may require adapter changes. There is no REST token fallback or automated SSO workaround.

## Windows installation (PowerShell)

Install Python 3.12 or newer from python.org, Git for Windows, and Google Drive for desktop. Sign into Drive yourself. Choose a private, unshared destination; find its actual drive letter/path in Explorer. The example below uses `G:\My Drive\TUM Moodle`; your installation may use another path. Keep this project outside both the downloaded-material directory and the private state directory.

```powershell
git clone https://github.com/shukailiao/tum-moodle-drive-sync.git
cd tum-moodle-drive-sync
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m playwright install chromium
```

Using the explicit virtual-environment executable avoids PowerShell activation-policy problems. `py -3.12` requires Python 3.12 to be installed; use your installed newer Python version if appropriate. For runtime-only installation, replace `".[dev]"` with `.`. Instead of downloading Chromium, set `browser_channel = 'msedge'` or `'chrome'` in your configuration to use that installed browser.

## Configure, log in, select courses

```powershell
New-Item -ItemType Directory -Force -Path 'G:\My Drive\TUM Moodle'
.\.venv\Scripts\tum-moodle-sync.exe init --output 'G:\My Drive\TUM Moodle'
.\.venv\Scripts\tum-moodle-sync.exe login
.\.venv\Scripts\tum-moodle-sync.exe check
.\.venv\Scripts\tum-moodle-sync.exe courses
```

`init` prints the config path (normally `%LOCALAPPDATA%\tum-moodle-drive-sync\config.toml`). Open it in an editor. `login` opens a **fresh visible browser**: choose the appropriate TUM login option, complete SSO and MFA yourself, wait until Moodle is signed in, then press Enter in the terminal. No password is requested in the terminal. The program verifies a protected Moodle page before saving cookies. Closing/cancelling the browser does not complete login.

Use the IDs printed by `courses` in `course_ids`, for example:

```toml
output_dir = 'G:\My Drive\TUM Moodle'
course_ids = [12345, 67890] # EXAMPLES ONLY: replace with your own course IDs
request_interval = 1.0
revalidate_hours = 24.0
max_file_mb = 512
```

Full options are in [config.example.toml](config.example.toml). Empty `course_ids` intentionally refuses synchronization. There is no implicit “all courses” mode. Paths must be absolute. TOML single quotes allow literal backslashes; double-quoted TOML paths require escaped backslashes.

The default state directory is also under `%LOCALAPPDATA%\tum-moodle-drive-sync`. On Linux, config normally lives in `~/.config/tum-moodle-drive-sync`, and state in `~/.local/share/tum-moodle-drive-sync`. Config and state may share a directory; neither may be inside the output directory. Keep state outside all cloud-synchronized folders and Git checkouts. A custom configuration path is supported as a **global option before the subcommand**:

```powershell
.\.venv\Scripts\tum-moodle-sync.exe --config 'C:\Users\YOUR_NAME\moodle-config.toml' courses
```

## Run and inspect results

```powershell
.\.venv\Scripts\tum-moodle-sync.exe sync
.\.venv\Scripts\tum-moodle-sync.exe sync --revalidate
.\.venv\Scripts\tum-moodle-sync.exe history
```

`sync` prints statuses such as `NEW`, `UPDATED`, `UNCHANGED`, `LOCAL EDIT`, `RESTORED`, `ERROR` and `LOGIN REQUIRED`. Changelogs and `sync.log` are saved in the private state directory, alongside `state.sqlite3` and `session.bin`. Inspect failures even if some files succeeded. Changelogs/history are retained without automatic pruning; only the log rotates. Periodically review disk use and archive private reports yourself.

Example layout:

```text
TUM Moodle/
  Algorithms [12345]/
    Week 1/
      Lecture 1 [resource-id].pdf
      Lecture 1 [resource-id-content-hash-unique-suffix].pdf
```

Revision changes in Moodle URLs trigger a download. For an existing file with the same URL/revision, the body is skipped until `revalidate_hours` elapses. Revalidation uses ETag or Last-Modified if supplied; otherwise it downloads and hashes the content. `--revalidate` checks now, still using conditional requests. An unchanged content hash avoids an additional output copy. A server that changes content without revisions or valid validators may delay detection; this tool cannot guarantee immediate detection of undocumented changes.

User edits are detected by comparing the local SHA-256 with the recorded hash. They are left intact; a remote update creates another file. Missing local files are downloaded again. Files with equal names get distinct resource identifiers. Renaming/moving courses or sections does not migrate existing copies; new versions use current names. Deleting remote resources never deletes anything locally. If the database is lost, a subsequent run may create extra copies but still will not overwrite existing files.

Google Drive for desktop performs the cloud upload independently. This program confirms **local writes only**, cannot confirm cloud completion, and does not change sharing settings. Ensure Drive is running and wait for its “up to date” status. Output must already exist so an unavailable drive produces an error.

## Schedule with Windows Task Scheduler

First complete one successful interactive `login`, `courses`, and `sync`. Use the same Windows user for login and scheduled runs: DPAPI sessions are user-bound. Keep Google Drive for desktop running. The MVP is intended to run while you are logged on; it does not solve unattended SSO.

1. Open Task Scheduler → **Create Task**. Name it `TUM Moodle Drive Sync`.
2. In General, choose your own account and **Run only when user is logged on**. Administrator privileges are unnecessary.
3. Add a daily trigger that repeats every **1 hour**, indefinitely. Avoid minute-level polling.
4. Add a **Start a program** action, replacing the paths below with your absolute paths:

   | Field | Value |
   | --- | --- |
   | Program/script | `C:\Users\YOUR_NAME\projects\tum-moodle-drive-sync\.venv\Scripts\python.exe` |
   | Add arguments | `-m tum_moodle_drive_sync --config "C:\Users\YOUR_NAME\AppData\Local\tum-moodle-drive-sync\config.toml" sync` |
   | Start in | `C:\Users\YOUR_NAME\projects\tum-moodle-drive-sync` |

5. In Settings, select **Do not start a new instance** if already running. Allow sufficient run time for large courses; forced termination can leave an orphan file. The app also locks its state directory against overlapping commands.
6. Run the task manually once. Inspect **Last Run Result**, the state-directory changelog/log, and the local destination. Check Drive separately for upload completion.

| Exit code | Meaning |
| --- | --- |
| 0 | Command completed; sync has no reported errors. Does not establish complete custom-plugin coverage or cloud upload. |
| 1 | Configuration, IO/network or partial synchronization failure. |
| 2 | Missing/expired authentication (also argparse usage errors). Run interactive `login` again. |
| 3 | Another process holds the state lock. |
| 130 | Cancelled/interrupted. |

Sessions expire according to TUM policy. Scheduled runs stop instead of trying to bypass login. Run `login` again under the same account; then retry. `logout` removes the local saved session, not the server-side session. Session files must never be put in GitHub Actions secrets or artifacts. A test-only CI workflow is included; unattended TUM synchronization in Actions is deferred.

## Test and develop

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m "not browser"
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m ruff format --check src tests
.\.venv\Scripts\python.exe -m build
```

The optional real-browser fixture intercepts all browser traffic and uses synthetic cookies. It does not log into TUM. On Windows, point it to an installed Chromium-family browser executable (example):

```powershell
$env:TUM_SYNC_TEST_CHROMIUM = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
.\.venv\Scripts\python.exe -m pytest -q -m browser
```

If the executable is not configured and `chromium` is unavailable, this test is skipped. A normal `pytest` run includes it when available. Windows runs also exercise the DPAPI session round-trip; Linux runs exercise POSIX permissions. CI is configured for Windows and Linux, but adding a workflow is not evidence that its remote runs passed.

Modules: `auth` / `security` handle browser login and local sessions; `http` handles scoped requests and retries; `moodle` discovers enrolled courses; `resources` traverses supported pages; `sync` / `files` handle downloads and preservation; `database` stores metadata; `cli` handles configuration, locking and reports. SQLite schema version 1 is used; future incompatible schemas need explicit migrations.

### Manual acceptance before relying on live synchronization

1. On Windows, install/configure as above and use a private Drive folder.
2. Log in through the real TUM flow and run `check`. Confirm `courses` matches your enrolled courses, including courses beyond the first listing page.
3. Select one course; compare downloaded files/topics against visible Moodle File/Folder activities, including separate section pages and embedded files. `NO FILES` does not prove a course has no materials: verify unsupported/lazy markup manually.
4. Run sync twice. The second run should report unchanged files without new copies. Test `--revalidate` as well.
5. When a legitimate course update occurs, verify a new version appears and the old copy remains. Do not modify university resources just to test this tool. Locally edit a disposable downloaded copy and confirm it is preserved.
6. Verify missing/expired-session handling, using `logout` then `check`; log back in afterwards. Run the scheduled task manually.
7. Wait for Drive's upload indicator and confirm the files in your private Google Drive web view. That manual check, not the CLI exit code, establishes cloud synchronization.

Report failures with redacted diagnostics; never attach cookies, session keys, passwords or course materials. Read [SECURITY.md](SECURITY.md). Software is [MIT licensed](LICENSE); course material copyrights are unaffected.
