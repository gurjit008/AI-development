# Surgical navigation demo reporting MVP contracts

Status: implementation contract; bounded local application, no production deployment. Python 3.13 standard library HTTP/SQLite backend, static HTML/CSS/JS, one same-origin process bound to 127.0.0.1 by default. No npm or Python dependencies required. Python http.server is a development server, not an approved production ingress. SQLite is appropriate for this single-process local MVP; deployment, TLS, jurisdiction/retention requirements, multi-instance operation and clinical validation remain future decisions. No clinical decision support is implemented.

## Ownership and modules

- Database agent owns app/database.py, app/schema.py, app/importer.py, app/migrations/ and its focused tests. Public persistence interface `connect(db_path)` returns sqlite3 connection (Row rows, foreign_keys enabled); `initialize(db_path)` applies idempotent schema and schema metadata. Schema interface `get_schema()` returns the metadata response below; importer interface `preview_workbook(data:bytes, filename:str)` returns parsed proposal and diagnostics; `commit_import(connection, proposal, owner_id, actor_id)` commits all records and audit events in one transaction. Coordinate exact signature updates through primary agent.
- Backend agent owns app/server.py, app/service.py, app/auth.py, app/__init__.py and backend tests. Calls database/schema/importer; serves only web/index.html, web/app.js and web/styles.css. CLI `python -m app.server --db <path> --port 8000`. Bootstrap users explicitly through documented setup command, synthetic values only; no raw reference auto-import.
- Frontend agent owns web/index.html, web/app.js, web/styles.css. Uses the HTTP contracts below. Schema-driven forms and tables; no copied reference respondent data.
- Primary owns orchestration, README and scripts; QA owns independent tests; security/reviewer inspect cross-module behavior. Workers are not alone in the codebase and must preserve others' edits.

## Data and field schema

All IDs opaque UUID strings; timestamps UTC ISO8601. Date fields YYYY-MM-DD. Fields object keyed by schema field id; each supplied cell is `{state,value,raw?}`. State enum `value|unknown|not_applicable|not_recorded`; value is null unless state=value; then scalar string, finite number, boolean, or array of HTTPS URL strings for link fields. Missing schema fields represent not_recorded. raw is optional original cell text retained only for explicit imports and authorized views, never server logs. Reject unknown field ids, wrong types, oversized values and nonfinite numbers. Plain text never HTML. Text max 10000 chars, short text max 250 chars, links max 10 per field/2048 chars each. Exact list options below; import may preserve unmatched historical options in raw with state unknown, avoiding invented mapping. Drafts allow incomplete inputs; submitted native V3 reports require surgery_date, hospital_name, city, surgeon_name, application_engineers, surgery_type, surgery_success, navigation_usage, system_base, software_version, system_identity. This requiredness is an MVP assumption, not inferred spreadsheet form validation. If surgery_type Other require other_surgery_type; system_identity Other require other_system_identity; unsuccessful surgery requires failure_description; partial/not-used navigation requires navigation_limitation_reason. Import bypasses native requiredness and creates draft records with diagnostics.

`GET /api/schema` returns `{versions:{V1:[field],V2:[field],V3:[field]},inventory:{categories,availability,readiness}}`. Field metadata `{id,label,type,source_column,required,options?,legacy_sentinels?,section}`. Every original header retained with its source column; labels containing staff names are retained in metadata only because they are source headers, but do not generate seeded real identities. All V3 fields supported, including duplicated questions as distinct columns and opaque Column 47; do not silently merge them.

V3 column to field id/type mapping:

| Column | Field id | Type |
|---|---|---|
| A | response_timestamp | datetime |
| B | respondent_email | short_text |
| C | surgery_date | date |
| D | hospital_name | short_text |
| E | city | short_text |
| F | transport_issues | text |
| G | surgeon_name | short_text |
| H | application_engineers | short_text |
| I | surgery_type | enum |
| J | procedure_subtype | text |
| K | other_surgery_type | short_text |
| L | xray_count | integer |
| M | surgery_success | boolean |
| N | failure_description | text |
| O | navigation_usage | enum |
| P | navigation_limitation_reason | text |
| Q | instrument_issues | text |
| R | software_issues | text |
| S | data_issues | text |
| T | other_issues | text |
| U | improvement_points | text |
| V | surgery_summary | text |
| W | hospital_requests | text |
| X | software_issues_secondary | text |
| Y | feedback_accuracy | text |
| Z | feedback_ergonomics | text |
| AA | feedback_ease_of_use | text |
| AB | feedback_other | text |
| AC | internal_requests | text |
| AD | legacy_reviewer_comments | text |
| AE | instrument_loss_damage | text |
| AF | other_information | text |
| AG | deployed_by | short_text |
| AH | system_base | enum |
| AI | software_version | short_text |
| AJ | system_identity | short_text |
| AK | other_system_identity | short_text |
| AL | extra_instruments | text |
| AM | tool_tip_accuracy_mm | decimal |
| AN | fiducial_registration_mm | decimal |
| AO | surface_registration_mm | decimal |
| AP | system_photo_links | links |
| AQ | surgery_photo_links | links |
| AR | accuracy_photo_links | links |
| AS | demo_request_links | links |
| AT | consent_form_links | links |
| AU | feedback_photo_links | links |
| AV | legacy_column_47 | text |
| AW | customer_location | boolean |

Surgery types: Cranial, CT-Spine, X-Ray Spine, Biopsy, Any (Cranial, Full body), ENT, Other. Navigation usage: Completely, Partially, Not used. System base Optical, EM. Native booleans true/false. Numeric accuracy nonnegative; xray count nonnegative integer. Legacy 100.00 in accuracy and 1000 in xray counts indicate unavailable/unknown per headers and MUST NOT enter computed averages/count totals. Preserve raw and state unknown; historical 1000 conflates unknown/not-applicable and must not be guessed. Native entry may record actual 100/1000 measurements since state is explicit. Blank imported fields state not_recorded. Text NA/N/A/none becomes unknown unless a header unambiguously means no issue/feedback, then not_applicable; preserve raw. Inventory NA has separate unavailable semantics; never treat it as zero quantity.

V1/V2 schemas retain EVERY header and source-column association; use shared V3 ids only where meaning is equivalent and `legacy_<version>_<column>` for other fields. V1 includes deployment/removal dates, general navigation accuracy, surgery details, feedback-form status, exposure count, costing/action notes; V2 includes broad issue/feedback summaries plus separate categories and failure reason. Do not lose those fields during import or editing. Form version controls visible schema; V3 is new report default.

## Persistence contract

Tables (SQLite, foreign keys, CHECK constraints; id UUID text):

- users(id,username UNIQUE,password_hash,role CHECK engineer/reviewer/admin,active INTEGER,created_at). Passwords PBKDF2-HMAC-SHA256 with random per-user salts and calibrated iteration floor; no plaintext credentials.
- sessions(id_hash PRIMARY KEY,user_id FK,csrf_token,expires_at,created_at). Session token random >=32 bytes, store digest. Invalidate on logout, user deactivate, role/password change.
- reports(id,owner_id FK users,form_version CHECK V1/V2/V3,status CHECK draft/submitted,fields_json TEXT CHECK json_valid,version INTEGER >=1,created_at,updated_at,source_json nullable). owner_id and form_version immutable after creation. Index owner_id/updated_at. Source JSON contains workbook filename/hash,sheet,row,import_batch_id, row_fingerprint; sensitive raw values remain only fields_json.
- comments(id,report_id FK,author_id FK,body,created_at); immutable appended comments.
- systems(id,identity UNIQUE,name,city,hospital,application_experts,issues,readiness CHECK ready/not_ready/unknown,source_json nullable,version,created_at,updated_at). Readiness explicit; blue latest-update is metadata, not operational readiness.
- inventory_items(id,system_id FK,category,name,serial_number nullable,quantity nullable nonnegative integer,availability CHECK available/unavailable/unknown,remarks,source_json nullable,version,created_at,updated_at). available may have null quantity for historical AV; unavailable does not imply quantity zero. Categories Hardware, Cranial instruments, Spine instruments, Other components.
- inventory_movements(id,system_id FK,item_name,event_date nullable,quantity nullable,provided_by,reference_note,cost nullable nonnegative decimal,created_at,source_json nullable). Captures extra-provided ledger; does not automatically increment stock because historical workbook lacks reconciliation rules. Cost currency unknown, explicitly labelled unspecified.
- import_batches(id,workbook_hash,filename,actor_id FK,created_at,report_count,inventory_count); imported_rows(fingerprint PRIMARY KEY,batch_id FK,entity_type,entity_id). Exact workbook hash/sheet/row and stable row-content fingerprints used for idempotence; neither fuzzy patient nor surgery matching.
- audit_events(id,actor_id FK nullable,action,entity_type,entity_id,changed_fields_json,created_at). Append in same transaction as mutation; no passwords/session tokens/raw surgery narratives. No API for modifying/deleting audits.

SQLite connection per request; busy timeout, WAL; SQL parameters for values and whitelisted sort keys. Mutations and corresponding audit inserts atomic. Optimistic version checks for reports/systems/items. No destructive delete endpoints in MVP. Databases under private runtime data directory never static-served.

## HTTP contracts

JSON objects UTF-8. Errors `{error:{code,message,fields?}}`. 400 malformed/validation; 401 no valid session; 403 role/CSRF denial; 404 inaccessible/missing object; 409 stale version; 413 request limit; 429 login throttle. Success envelopes detailed below. Authenticated responses Cache-Control:no-store. API maximum JSON body 1 MiB except import preview 24 MiB. Cookies HttpOnly, SameSite=Strict, Path=/; Secure when HTTPS enabled. Restrict Host/Origin to configured loopback origin; all state changes require matching Origin plus X-CSRF-Token, including login via pre-auth session from GET session endpoint. No wildcard CORS.

| Method/path | Request | Response / access |
|---|---|---|
| GET /api/session | none | `{user:null or {id,username,role},csrf_token}` establishes pre-auth CSRF session |
| POST /api/login | `{username,password}` + CSRF | `{user,csrf_token}` rotated authenticated session |
| POST /api/logout | CSRF | `{ok:true}` |
| GET /api/schema | none | schema; authenticated |
| GET /api/reports | filters below | `{items:[report],total,page,page_size}` engineer own; reviewer/admin all |
| POST /api/reports | `{form_version:'V3',fields,status}` | 201 `{report}` engineer/admin; owner=current actor |
| GET /api/reports/{id} | none | `{report,comments:[comment]}` visibility rules above |
| PUT /api/reports/{id} | `{fields,status,version}` | `{report}` engineer own/admin; reviewer forbidden |
| POST /api/reports/{id}/comments | `{body}` max 4000 | 201 `{comment}` reviewer/admin only |
| GET /api/reports/export.csv | same filters | authorized CSV including field states; formula-safe |
| GET /api/systems | none | `{items:[system]}` authenticated |
| POST /api/systems | system mutable fields | 201 `{system}` admin |
| PUT /api/systems/{id} | mutable fields + version | `{system}` admin |
| GET /api/systems/{id}/inventory | none | `{items:[item],movements:[movement]}` authenticated |
| POST /api/systems/{id}/inventory | item fields | 201 `{item}` admin |
| PUT /api/inventory/{id} | item fields + version | `{item}` admin |
| POST /api/systems/{id}/movements | movement fields | 201 `{movement}` admin |
| GET /api/users | none | `{items:[{id,username,role,active}]}` admin for import owner choice |
| POST /api/users | `{username,password,role}` | 201 `{user}` admin |
| PUT /api/users/{id} | `{role?,active?,password?}` | `{user}` admin; prevent last active admin removal |
| GET /api/audit | page,page_size | `{items,total,page,page_size}` admin |
| POST /api/imports/preview | `{filename,content_base64}` | `{token,summary,diagnostics,expires_at}` admin; no DB writes |
| POST /api/imports/commit | `{token,owner_id}` | `{batch_id,imported_reports,imported_inventory,skipped_duplicates}` admin; atomic |

Report object includes id,owner_id,form_version,fields,status,version,created_at,updated_at,source. List filters `q` (hospital/city/surgeon/system text), `date_from,date_to` (inclusive surgery-date), `surgery_type,system_identity,system_base,status,owner_id`; engineer owner filter cannot widen visibility. Pagination page >=1, page_size 1..100 default20; sort newest updated_at then id. Exact filter matching for categorical fields; parameterized case-insensitive substring q. Invalid date ranges fail 400. CSV uses same filters and permissions, stable union schema columns, id/version/source metadata, field value plus `<field>__state`; neutralize all cells starting with formula/control/whitespace followed by =,+,-,@ using apostrophe prefix; quote CSV via csv module. Export limit 10000 rows with explicit error above cap, never silently truncate.

## Historical workbook import

User explicitly requires import. Parser accepts XLSX ZIP/XML bytes only, not filesystem paths or external fetch. filename informational only. Max decoded 16 MiB, expanded XML total 64 MiB, 1000 ZIP entries, 10000 rows; reject encryption, macros, unsafe paths, XML DTD/entity content, invalid archive and unsupported sheet layouts. Ignore formula execution and report uncached formula values as unknown diagnostics. Parse shared strings, inline strings, worksheet relationships, sparse cells and Excel 1900/1904 date-system metadata. Convert serial datetime without pretending workbook local times have a known timezone; preserve raw timestamps and note uncertainty. Header rows matched by workbook version and labels, report blank rows skipped. V1 six system inventory sheets parsed with category boundaries; skip serial/category/header/legend/current-status metadata rows; preserve status blocks and additional-instrument movements separately. Parse quantity AV=available/null and NA=unavailable/null. Preserve conflicting sheet name/title/current city as source metadata and diagnostic, never silently decide actual location. All eight V1 sheets and V2/V3 report sheets supported.

Preview stages bounded bytes/proposal in memory behind cryptographically random token bound to admin session and expiry 10min; show row counts, field warnings, inventory conflicts and duplicates; no sensitive raw rows in response/logs. Commit requires engineer owner_id for imported reports and active admin session; recheck permission and validations, then transaction; reject blocking diagnostics without writes. A repeated commit token returns expired/consumed 400; re-preview same workbook and commit skips duplicates. Changed workbook exact identical raw row fingerprints dedupe; changed records are new drafts rather than silently overwriting existing reports. No automatic import at startup; no raw source content seeded. Attachments remain explicit HTTPS links, never fetched/uploaded/proxied/thumbnailed or claimed accessible; each displayed link states external and uses rel=noopener noreferrer. Imported URL strings are validated and unsafe strings preserved only inert in raw.

## Verification and release boundary

Acceptance: all 49 V3 columns rendered/persisted; V1/V2 metadata and distinct software question/opaque column retained; conditional submission validation and unknown numeric state behave correctly. Synthetic fixtures test report CRUD/restart persistence, every role/object denial, CSRF/login/logout/deactivation, optimistic races, audit atomicity, all filters and identical CSV scope/formula mitigation, all-version imports, date epochs, sentinel values, AV/NA inventory, conflicting headings, dry-run no writes, commit rollback and repeated import dedupe. Confirm reference files unchanged and no source rows in seeds/web/docs. Browser verify keyboard-visible labels, grouped forms, empty/loading/error states, narrow viewport, save/submit/comments/inventory/import/export. CI runs unittest plus syntax checks. Local-target latency measured only if performance claim made; production readiness is not claimed. Open decisions: production identity provider/HTTPS, storage encryption/retention, clinical privacy approvals, ownership mapping finer than admin selection, attachment access, inventory reconciliation and currency.
