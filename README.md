# Master Data Validator
this is my new comment
SAP S/4HANA LTMC pre-upload validator. Collapses thousands of validation
errors into a handful of decisions, with inline record editing, a
repository, and module-gated access control backed by Postgres.

Supports two modules today:

- **SD — Customer Master** (single XML file per dataset)
- **MM — Material Master** (three XLSX files per dataset: main, alt UoM, long text)

PP / QM / FICO appear in the module picker but are stubbed — their
upload flow errors out until the rule set for each lands.

---

## What's new (v32)

- **MM module** is now fully integrated. Three-file upload, live
  format-check per slot, 65 validation rules, shared Decisions UI.
- **MM template downloads** on the upload page (Main / Alt UoM / Long Text)
- **65 MM rules** covering business checklist, LTMC template mandatory
  fields, SAP format rules, and KDS catalog validations. All rules
  fire equally — no source categorisation or filter buckets.
- **6 MM rules are dormant** pending catalog data from the SAP team
  (Division, Profit Center, MRP Controller, Strategy Group, Production
  Supervisor, Scheduling Profile). They activate automatically when
  those KDS sheets are added to `backend/kds/MM_KDS.xlsx`.
- **LTMC XML export for MM is NOT yet implemented** — see
  [MM_MODULE.md](MM_MODULE.md) for the full status and what's next.
  SD export still works end-to-end.

See `MM_MODULE.md` for the full MM spec: file formats, rule list,
KDS catalogs, known limitations.

---

## Prerequisites

1. **Python 3.10+** (from python.org)
2. **PostgreSQL** running locally on port 5432
   - A database named `masterdata`
   - The `postgres` user password matching the `DATABASE_URL`
   - Default assumption: password is `Lumbini`

### One-time Postgres setup

```bash
psql -U postgres -c "CREATE DATABASE masterdata;"
```

If your password is different, override via environment variable:

Windows PowerShell:
```powershell
$env:DATABASE_URL = "postgresql://postgres:YOUR_PASSWORD@localhost:5432/masterdata"
```

macOS / Linux:
```bash
export DATABASE_URL="postgresql://postgres:YOUR_PASSWORD@localhost:5432/masterdata"
```

The app creates all tables and seeds the demo users automatically on
first start.

---

## Quick Start

### Windows

```powershell
.\run.bat
```

If `run.bat` hangs (antivirus issue), skip it and run directly:

```powershell
python -m pip install fastapi uvicorn python-multipart lxml openpyxl pydantic "psycopg[binary]"
cd backend
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

### macOS / Linux

```bash
./run.sh
```

Then open **http://localhost:8000** and log in.

---

## Demo Credentials (seeded on first start)

| Username   | Password   | Role         | Module |
|------------|------------|--------------|--------|
| admin      | admin123   | Administrator | (all) |
| ituser     | it123      | IT — read all, mark LTMC uploaded | (all) |
| sduser     | sd123      | Module user | SD (Customer) |
| mmuser     | mm123      | Module user | MM (Material) |
| ppuser     | pp123      | Module user | PP |
| qmuser     | qm123      | Module user | QM |
| ficouser   | fico123    | Module user | FICO |

**For production:** change all these passwords immediately after first
login (Admin → Users → Change password).

---

## Architecture

```
   Browser
      │
      ▼
  FastAPI app (Python)
      │
      ├─► Postgres (users, sessions, file metadata, audit trail)
      │
      └─► Local disk at backend/storage/{MODULE}/
             SD/{file_id}_{filename}         — single XML per dataset
             MM/{file_id}/                   — folder bundle (3 xlsx + manifest)
```

**What's in Postgres:**
- `users` — accounts + roles
- `sessions` — active login tokens (survive server restarts)
- `files` — file repository metadata with validation status
- `audit_entries` — global audit trail, every action tracked

**What's on disk:**
- SD: single XML file at `backend/storage/SD/{file_id}_{filename}`
- MM: 3-file bundle folder at `backend/storage/MM/{file_id}/` containing
  `main.xlsx`, `alt_uom.xlsx`, `longtext.xlsx`, and `manifest.txt`

Blobs on disk, metadata in Postgres. Standard pattern.

### Legacy data migration

If you're upgrading from an earlier version with `backend/storage/*.json`
files, the app automatically imports them into Postgres on first run.
Safe to run multiple times (no duplicates).

---

## 5-Minute Demo Flow

### SD (Customer) — works end-to-end including export

1. **Login** as `sduser / sd123` — repository shows SD files only
2. **Upload** an XML/XLSX file via the Upload button
3. **Dashboard** shows record count, error count, decisions, completion %
4. **Decisions** → pick "Replace with" on the pattern decision → type
   `CIF` → Apply. Watch 126 errors collapse to 0.
5. **Records** view for any error → errors pinned at top, `J`/`K`
   keyboard navigates between error records
6. **Export** — download LTMC-ready XML
7. **Dashboard** → "Mark as Validated" (green button, enabled once
   errors = 0)
8. **Logout**, login as `ituser / it123` — sees all modules
9. **Mark LTMC uploaded** on the same file — workflow complete
10. **Login as admin** → Audit Trail shows every action with
    who/what/when/why

### MM (Material) — validation works; LTMC export still in progress

1. **Login** as `mmuser / mm123`
2. **Upload File** → Module dropdown → **MM — Material Master**
3. **Download templates** at top of modal (Main / Alt UoM / Long Text)
4. **Drop your 3 files** into the 3 slots; each turns green ✓ once
   the format matches
5. **Upload & Validate** → session opens on the Decisions page
6. **Fix errors** using the same Decisions / Group & Replace / Fix
   Individually UI as SD
7. **Export** — see `MM_MODULE.md`. The current Export button will
   not produce LTMC-valid MATMAS XML. Use the validator to clean
   your data files, then run them through your existing LTMC
   staging process.

---

## Workflow

```
  Upload ──► In Progress ──► (Module user fixes) ──► Validated
                                                        │
                                                        ▼
                                    (IT / Admin) ──► LTMC Uploaded
                                                        ▲
                                        (Revoke at any step)
```

- Module users (SD/MM/PP/QM/FICO) see only their module's files
- IT users see all files, can mark LTMC uploaded
- Admin does everything and manages users

---

## Project Structure

```
monday/
  README.md                    ← this file
  MM_MODULE.md                 ← MM-specific spec (rules, KDS, status)
  DEPLOYMENT.md                ← VM / console deployment notes
  SECURITY.md                  ← security posture + hardening checklist

  backend/
    main.py                    FastAPI app (SD endpoints + session mgmt)
    mm_routes.py               MM-specific endpoints (/api/mm/*)
    requirements.txt
    services/
      # SD / shared
      xml_engine.py            SpreadsheetML parser + writer (bit-perfect)
      validator.py             SD rule engine
      decision_engine.py       Error → Decision grouping
      applier.py               Bulk actions + undo snapshots
      db.py                    Postgres connection + schema init
      auth.py                  Users + sessions
      repository.py            File metadata + MM bundle storage
      audit_log.py             Global audit trail
      kds_loader.py            SD KDS parser
      kds_reference.py         SD KDS catalogs (cached)
      # MM
      mm_file_detector.py      Identify main/alt_uom/longtext by row-2 codes
      mm_loader.py             Parse each xlsx into LoadedRow dicts
      mm_merger.py             Join 3 files by MATNR; detect orphans/dups
      mm_kds.py                MM KDS loader (8 catalogs from MM_KDS.xlsx)
      mm_checklist.py          65 MM validation rules (declarative)
      mm_validator.py          MM rule execution engine
    kds/
      Sales_and_Dist_KDS.xlsx  SD reference data
      MM_KDS.xlsx              MM reference data (plants, material groups,
                               storage locations, valuation classes, etc.)
    mm_templates/
      main_template.xlsx       Served at /api/mm/template/main
      alt_uom_template.xlsx    Served at /api/mm/template/alt_uom
      longtext_template.xlsx   Served at /api/mm/template/longtext
    storage/                   (auto-created)
      SD/, MM/, PP/, QM/, FICO/

  frontend/
    index.html                 SPA (dense sidebar + light content)
    static/
      css/main.css
      js/app.js                Alpine.js controller

  data/                        Sample files (dev reference — not exposed in UI)

  meeting_pack/                Demo-day documents

  run.bat, run.sh              Launchers (with Postgres preflight check)
```

---

## Keyboard Shortcuts (Record Editor)

| Key | Action |
|-----|--------|
| `J` / `↓` | Next error |
| `K` / `↑` | Previous error |
| `Ctrl+S` | Save changes |
| `Esc` | Close modal / back |

---

## Troubleshooting

### `[ERROR] Cannot connect to Postgres`

The app couldn't reach Postgres. Check:
1. Postgres service is running (`pg_isready` on Unix, Services →
   "postgresql-x64-16" on Windows)
2. Database `masterdata` exists: `psql -U postgres -l | findstr masterdata`
3. Password matches your `DATABASE_URL`
4. Port 5432 isn't blocked by firewall

### MM upload rejects my file with "does not match the MAIN slot"

Each of the 3 MM slots runs a format check before accepting the file.
A file matches a slot if its row 2 (SAP field code header) contains
the signature for that slot:

- **Main:** needs `MATNR` AND `MBRSH` in row 2
- **Alt UoM:** needs `MATNR`, `MEINH`, `UMREZ`, `UMREN` in row 2
- **Long Text:** needs `MATNR` AND `BASE_TEXT` in row 2

If your file is being rejected, open it in Excel and check row 2
literally — it must be the SAP codes, not friendly labels (those go
in row 1). Download the templates from the upload page to see the
expected format.

### MM log shows "skipped rules (missing catalogs)"

This is info, not an error. 6 MM rules are dormant until those KDS
sheets are provided. See `MM_MODULE.md` for the full list and how
to enable them.

### `run.bat` hangs on venv creation

Skip venv entirely:
```powershell
python -m pip install fastapi uvicorn python-multipart lxml openpyxl pydantic "psycopg[binary]"
cd backend
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

### `pip install` fails with permission errors

```powershell
python -m pip install --user fastapi uvicorn python-multipart lxml openpyxl pydantic "psycopg[binary]"
```

### Port 8000 already in use

```powershell
python -m uvicorn main:app --host 0.0.0.0 --port 8001
```

Then open `http://localhost:8001`.

### Forgot a user password

Login as admin → Users → Change password.

If you lose the admin password, reset via SQL:
```sql
psql -U postgres -d masterdata -c "UPDATE users SET password_hash = '8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92' WHERE username = 'admin';"
```
(SHA-256 hash for `123456`. Change it via the UI after logging in.)

### Want a clean slate

```bash
psql -U postgres -d masterdata -c "DROP TABLE users, sessions, files, audit_entries CASCADE;"
rm -rf backend/storage
```

Next server start will recreate everything and reseed demo users.

---

## Production Notes

Prototype with deliberate simplifications:

- Passwords are SHA-256 hashed (use bcrypt in production)
- Sessions are simple cookies (add CSRF + SameSite=Strict)
- No TLS (put behind an ALB/nginx with a real cert)
- No rate limiting on login (add before shipping)
- File blobs on local disk (move to S3 for production)

See `meeting_pack/05_PRODUCTION_ARCHITECTURE.md` for the production
architecture plan, including AWS sizing, security hardening, and
integration with Healthium's Entra ID / Okta.
