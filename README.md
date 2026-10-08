# Alaka-Ledger

A local-first portfolio aggregation foundation named after Alaka, the mythical
city of treasures. It provides transactional SQLite storage, typed broker adapter
contracts, opt-in USD/INR quotes, and a Streamlit holdings dashboard.

**Status:** CAS PDF/XLSX uploads can be parsed into a local, session-only holdings
preview. Supported layouts are described below; IBKR Flex remains a stub.
CAS previews do not update Alaka Vault or portfolio totals. The portfolio dashboard
shows cost basis, not market value. Total Net Worth remains "Not valued" until
market prices, cash balances, and liabilities are available.

## Local Setup

Use Python 3.10 or newer. Run commands from the repository root.
[requirements.txt](requirements.txt) is the sole dependency manifest and is used
for both local setup and CI. There is no editable install, development extra,
or separate build configuration.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

For an existing virtual environment, rerun the install command after requirements
change. XLSX reading uses `openpyxl`; encrypted PDFs use `pypdf` with crypto support.
On macOS/Linux, use `.venv/bin/python` in place of the Windows executable.

No database configuration is required. The database is named **Alaka Vault** and
uses the fixed location `.local/alaka_vault.sqlite3` beneath the project root,
independent of the working directory. The directory and database are created
only when **Refresh Holdings** is requested. `ALAKA_DATABASE_PATH` is no longer
read; existing databases elsewhere are not moved or imported automatically.

Optionally configure market-data access through the environment or an untracked
`.env`, using [.env.example](.env.example) as the reference:

| Variable | Meaning |
| --- | --- |
| `ALAKA_ALLOW_MARKET_DATA` | `false` by default. Set `true` to permit public Yahoo FX requests. |

Process environment variables take precedence over `.env`. Keep statements
outside the repository and keep the project outside cloud-synchronized folders
because it now contains local financial storage.

```powershell
.\.venv\Scripts\python.exe -m streamlit run ui/app.py
```

Open http://127.0.0.1:8501. Do not expose the dashboard to a network: it has no
authentication and is designed for a trusted, single-user workstation.

## CAS Uploads

Open **Import CAS**, select a `.pdf` or `.xlsx` statement, enter its PDF password
if needed, and select **Parse Statement**. The preview lists ISIN, security name,
quantity, currency, statement price/value when supplied, and source page/sheet.
Missing prices and values remain unknown, not zero. A preview is not a reconciled
portfolio or a statement of current net worth.

- PDF support covers text-based ruled tables and whitespace-aligned columns,
	including password-protected PDFs. Scanned images require OCR and are rejected.
- XLSX support covers holdings sheets with recognizable column headers. Required
	headers are `ISIN` (or `ISIN Code`) and `Quantity`, `Units`, `Closing Balance`,
	or equivalent supported aliases. Optional fields include `Security Name`,
	`Market Price`/`NAV`, `Market Value`/`Valuation`, and `Currency` (INR only).
- Numeric cells accept plain and Indian-grouped decimals. Formula-based numeric
	fields are rejected; use a values-only workbook. Password-encrypted Excel files
	and legacy `.xls` files are not supported.
- Repeated headers are skipped. Recognizable transaction sections are excluded;
	snapshots are never converted into BUY events. Rows are retained separately,
	not merged across accounts or deduplicated. Account attribution, statement-date
	reconciliation, and saving snapshots to the vault are not implemented yet.
- Unknown headers, ambiguous or malformed holdings rows, and empty extractions
	produce errors. Broker layouts vary; this is not certification of every CAS
	variant. Tests use generated examples, not real customer statements.
- Upload limit: 20 MiB; PDF limit: 200 pages; XLSX limits: 50 sheets, 50,000 rows
	and 100 columns per sheet, and 100 MiB expanded ZIP size.

Files are sent only to the local Streamlit process and parsed in memory. The app
does not persist raw uploads or passwords, call external parsers, or log statement
contents. Password widget state is cleared after each parse attempt. Uploads and
previews can remain in process/browser-session memory until replaced or the
session ends; clearing Python references is not secure memory erasure.

## Architecture

| Module | Responsibility |
| --- | --- |
| [core/config.py](core/config.py) | Fixed Alaka Vault location and market-data environment validation. |
| [core/database.py](core/database.py) | Transaction-scoped connections, schema constraints, and local snapshots. |
| [ingestion/base_parser.py](ingestion/base_parser.py) | Generic parser contract, typed snapshots/events, and parsing exceptions. |
| [ingestion/parsers.py](ingestion/parsers.py) | CAS PDF/XLSX snapshot extraction and the IBKR Flex XML stub. |
| [core/forex_engine.py](core/forex_engine.py) | Opt-in public FX retrieval and bounded process-local caching. |
| [core/portfolio_engine.py](core/portfolio_engine.py) | Decimal-based cost aggregation and display formatting. |
| [ui/app.py](ui/app.py) | Lazy holdings loading, native currency totals, allocation chart, filters, and pagination. |

### Storage Contract

- Each operation owns a connection in its calling thread. SQLite serializes
	writers with `BEGIN IMMEDIATE`; operations commit atomically or roll back.
- Foreign keys, positive/finite decimal checks, ISO date checks, and broker-scoped
	trade IDs enforce integrity. Ledger events cannot be updated or deleted.
- Decimal values are stored as text. Bind values as strings using placeholders;
	do not use SQL floating-point aggregation for money. Calculations use a
	50-digit decimal context and half-even rounding only for display.
- A ledger event references a `(ticker, broker, asset_class)` holding. An import
	service must create/update that snapshot and append the event in one transaction.
	Retain zero-quantity snapshots to preserve references. This orchestration is
	not part of the CAS preview; the IBKR transaction parser remains a stub.
- This initial schema supports long-only BUY/SELL events. Reconciliation,
	corporate actions, tax lots, cash flows, migrations, and ledger correction
	workflows require explicit future design. Back up local storage before schema changes.

### FX and Valuation

Only the fixed public ticker `USDINR=X` is requested; no portfolio symbols or
account data are sent. Requests are disabled by default. Yahoo can still observe
your IP address when access is enabled, and quotes may be delayed.

The locked in-memory cache has a 15-minute request cooldown, including failed
requests. A previously obtained quote may be used for up to seven days with a
visible stale status. Expired or missing quotes yield no rate, never a fabricated
1:1 conversion. Restarting the process clears the cache. Only INR and USD
translation is supported; incomplete conversions suppress the aggregate total
and chart while preserving native currency subtotals.

## Privacy Boundaries

[.streamlit/config.toml](.streamlit/config.toml) disables Streamlit usage telemetry,
binds the server to loopback, and retains CORS/XSRF protections. Holdings are
retained only in the current browser session, not in a global Streamlit data cache.
SQLite files are **not encrypted**. Use OS account permissions and disk encryption;
local-first operation does not protect a compromised workstation or a shared browser.

The supplied [.gitignore](.gitignore) is preserved verbatim. It does not cover
Flex XML statements, arbitrary text exports, every environment-file variant, or
SQLite journal/WAL sidecars. Ignore rules also do not remove already tracked files.
The `.local/` rule excludes Alaka Vault and its sidecars. Never force-add that
directory. Keep real statement inputs outside the repository. Tests use synthetic
records and temporary storage only. "Vault" is a name, not an encryption guarantee.

The repository test rejects tracked statement formats, database sidecars, local
state, and common credential filenames without printing their paths. It is a
filename-based safeguard, not a secret scanner or a guarantee against leaks.
Review every staged change before publishing. The template directory exception
does not permit real statements or unignore matching files beneath it.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests cover transaction rollback, constraints, concurrent writers, parser failure
contracts, generated XLSX and encrypted PDF parsing, upload/password state cleanup,
offline FX behavior, exact cost calculations, lazy UI loading, filtering, and
pagination. They make no live market requests.
GitHub Actions runs the same suite with dependencies installed exclusively from
[requirements.txt](requirements.txt). Its version ranges are the requested ranges,
not a reproducible lockfile. Live provider behavior and every version combination
within those ranges are not certified by these tests.
