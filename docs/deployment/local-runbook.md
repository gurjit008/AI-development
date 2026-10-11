# Local operation and recovery

This application is a single-process local demonstration tool. Python's HTTP server is not an approved production server. Production use needs explicit authorization and decisions about HTTPS, identity management, privacy, retention, encryption, and operational ownership. This workflow performs no deployment or artifact publication.

## Prerequisites and verification

Use Python 3.13 and Node.js 22. CI selects Python 3.13.1 and Node.js 22.15.0 to match the inspected development environment. There are no application package installs. Run from the repository root:

```powershell
python scripts/verify.py
# Windows wrapper; returns failure if the verifier fails:
./scripts/verify.ps1
```

If the local PowerShell execution policy disables scripts, use `python scripts/verify.py` directly. Do not relax the machine policy just to run the wrapper. The inspected host blocked direct PowerShell script execution; its wrapper was syntax-checked with the PowerShell parser. The hosted Windows job must confirm execution in its runner environment.

Verification compiles Python source in memory, checks JavaScript syntax with `node --check web/app.js`, runs `node --test web/app.test.cjs`, and runs `python -m unittest discover -s tests -v`. Missing implementation files, missing tests, zero discovered tests, syntax errors, and test failures fail verification. This does not replace browser interaction/accessibility checks. CI runs the same checks on Ubuntu and Windows, with read-only repository permissions and pinned actions. No credentials, live reference workbooks, private databases, or artifacts are uploaded. A passing CI job does not authorize production release.

## Start and validate locally

Keep runtime databases and backups in `.local`, outside the static assets directory. Restrict that directory to the intended OS user; SQLite does not encrypt files. Use only synthetic data until authorized to process actual historical material. Bootstrap an administrator with an interactive password prompt, then start the service:

```powershell
python -m app.server --db .local/demo.sqlite3 --create-user admin --role admin
python -m app.server --db .local/demo.sqlite3 --port 8000
```

The setup command prompts using `getpass` and exits after creating the account. Never supply passwords as command-line arguments or commit them. `--password-env ENV_NAME` is available for controlled automation; inject that variable privately rather than writing its value into scripts or logs, then clear it when finished.

Start one process and use `http://127.0.0.1:8000`. Do not expose the listener publicly or add a reverse proxy as part of local setup. The migration initializer runs before service starts; make a SQLite backup before updating an existing database. Database migrations are owned by the database implementation; inspect compatibility before switching code versions.

In another terminal, request the session endpoint:

```powershell
Invoke-WebRequest -Uri http://127.0.0.1:8000/api/session -UseBasicParsing
```

Success requires HTTP 200, a session envelope with a CSRF token, and an unauthenticated `user` value. Then log in through the browser; create a synthetic draft, edit it, restart the service, confirm persistence, and verify that another engineer cannot read the draft. Verify imports first through preview and inspect warnings before committing. An unsuccessful preview must not change database records. Do not put clinical narratives, passwords, or session tokens in diagnostic messages or issue reports.

HTTP availability alone does not establish correct authorization or durable writes. Failures to log in, repeated 500 responses, lock errors, missing drafts after restart, and unexpected access to another engineer's report are stop-use conditions. Record a timestamp, action, HTTP status, and non-sensitive error code; inspect the local process console and tests. No external alerting or reliability objective is configured for this local MVP.

## Consistent SQLite backup

Never copy only the main database file while WAL may contain uncheckpointed writes. Use SQLite's backup API. This command refuses to overwrite an existing backup and opens the source in read-only mode, so a misspelled source path does not create a new database:

```powershell
@'
from pathlib import Path
import sqlite3

source = Path('.local/demo.sqlite3').resolve(strict=True)
backup = Path('.local/backups/demo-before-update.db').resolve()
backup.parent.mkdir(parents=True, exist_ok=True)
if backup.exists():
    raise SystemExit('Choose a new backup filename; existing backups are not overwritten.')
with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
    with sqlite3.connect(backup) as dst:
        src.backup(dst)
        checks = dst.execute('PRAGMA integrity_check').fetchall()
        if checks != [('ok',)]:
            raise SystemExit('Backup integrity check failed; retain original database.')
print('SQLite backup verified.')
'@ | python -
```

Backups contain the same sensitive records as the source. Apply the same directory permissions and approved retention policy. Check free disk space before backup. No remote backup, encryption service, or automatic deletion is configured.

## Update, rollback, and recovery

1. Run verification against the intended code version. Finish independent security/code review and browser acceptance checks.
2. Stop the local server using Ctrl+C. Back up the current database using the command above, retaining the previous code version and its revision identifier.
3. Review migration compatibility with the database owner. Start the new code against a disposable restored copy before using the normal database path. Validate session availability, login, report persistence, authorization, export, and inventory/import workflows.
4. If verification or smoke checks fail, stop the process and keep the failed database for diagnosis. Do not apply reverse migrations or overwrite the original database. Use the previous code version with a compatible restored database copy.

Restore a verified backup into a **new** runtime file with the backup API. In the backup command, change `source` to the chosen backup and `backup` to a new `.local/restored-demo.db` path; it must not already exist. Start the compatible code using `--db .local/restored-demo.db`. This preserves the source and failed database without deleting WAL files or moving directories. Stop all processes using a database before changing its operational path. A restore recovers only writes present at backup time; reconcile later writes explicitly rather than silently merging records.

Production deployment, shared-resource changes, destructive replacement, and irreversible migration/cleanup actions remain unauthorized. GitHub-hosted CI execution must be observed separately; local verification cannot prove that hosted runners completed successfully.
