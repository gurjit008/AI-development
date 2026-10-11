# Demo reporting

A local application for surgical navigation demonstration reports and equipment inventories, based on the three workbooks in `docs/reference/DemoDetails`. New reports use V3; historical V1/V2/V3 forms retain their original fields and provenance. Source workbooks are never imported at startup.

## Requirements and design

Read [requirements analysis](docs/requirements/analysis.md) for source evidence, assumptions, contradictions, and acceptance criteria. [Architecture contracts](docs/architecture/contracts.md) defines API, schema, permissions, and independent implementation scopes.

Python 3.13 and Node.js 22 are the development baseline. The application uses Python's standard library, SQLite, and browser JavaScript; no package installation is required. Node is needed for JavaScript verification, not for serving the application.

## Local use

Run commands from the repository root. Keep runtime databases under `.local/`, which is excluded from Git. Account bootstrap instructions are provided in the [local runbook](docs/deployment/local-runbook.md).

```powershell
python -m app.server --db .local/demo.sqlite3 --create-user admin --role admin
python -m app.server --db .local/demo.sqlite3 --port 8000
```

The first command prompts for a password of 12–256 characters and creates the administrator account. Use the administration screen to create engineer and reviewer accounts.

Open `http://127.0.0.1:8000`. Engineer accounts manage their own reports. Reviewers read reports and append comments. Administrators manage accounts, equipment, imports, and all reports. A new installation contains no historical surgery records.

Administrators select an XLSX workbook in the import screen, inspect diagnostics, choose an active engineer owner, and explicitly confirm. Import preserves uncertain values, source version, and row provenance. Duplicate rows are skipped. Equipment movements record supplied items separately from stock quantities. External photo and document links are not fetched by the server.

Report lists support filters and CSV export with the same visibility rules. Export includes explicit field states and neutralizes spreadsheet formula prefixes. Imported sentinel values such as accuracy `100` or X-ray count `1000` become unknown, while native measurements use explicit states and may record those actual numeric values.

## Verification

```powershell
python scripts/verify.py
```

The Windows wrapper is `./scripts/verify.ps1`, when execution policy permits it. Verification checks Python syntax, JavaScript syntax, frontend regressions, and discovered automated tests; it fails if there are no tests. Tests use disposable databases and synthetic fixtures.

## Operational limits

The local HTTP server binds loopback and is for local development. Production deployment requires a separate design for HTTPS, identity, privacy/retention, database access, backups, and monitoring. No production deployment is included. See the [local runbook](docs/deployment/local-runbook.md) for backup and recovery guidance.

Historical workbook timestamps do not establish a timezone; import retains that uncertainty. Inventory locations conflict in several reference sheets, and movement cost currency is unspecified. Review diagnostics before using imported records for operational decisions.
