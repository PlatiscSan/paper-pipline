# Paper Pipeline

`paper-pipeline` is a Python 3.11+ command-line pipeline for searching arXiv, PubMed,
Crossref, Europe PMC, Semantic Scholar, and OpenAlex,
downloading only openly accessible PDFs, extracting schema-validated data through any
OpenAI-compatible provider, and persisting every durable state transition in SQLite.

It never bypasses authentication, CAPTCHAs, institutional access, or paywalls. Automated tests
use local mocks and do not call real scholarly or AI services.

## Architecture

- `search`: a common asynchronous provider protocol with deterministic per-source allocation,
  concurrent pagination, recursive PubMed XML text, immediate database upserts, and deterministic
  reclamation of unused source quotas.
- `download`: open-access candidate resolution is separate from HTTP transport. The downloader
  performs paper-level concurrency, per-host pacing, Range resume, isolated `.part` files,
  retry/backoff, `Retry-After`, PDF validation, and atomic replacement after handles close.
- `extract`: PDF text and AI calls are decoupled. Text mode preserves `[PDF_PAGE N]`, chunks long
  files serially per paper, extracts each chunk, then merges. File mode uploads to a provider's
  Files API, calls Responses with `input_file`, and deletes the remote temporary file by default.
- `db`: SQLAlchemy repository with Alembic migrations, SQLite WAL/foreign keys, indexed states,
  identifier-aware upserts, runs, and events. Network calls occur outside transactions.
- `cli`: Typer is a thin argument and presentation layer over services.

## Install

PowerShell with Conda:

```powershell
conda create -n paper-pipeline python=3.12 -y
conda activate paper-pipeline
python -m pip install -e ".[dev]"
Copy-Item pipeline.example.toml pipeline.toml
paper-pipeline init --config pipeline.toml --force
```

Bash:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp pipeline.example.toml pipeline.toml
paper-pipeline init --config pipeline.toml --force
```

All relative paths in TOML are resolved against the configuration file's directory, not the
current working directory. Credentials are entered in the ignored local `pipeline.toml`; secret
fields are redacted and are never stored in SQLite or written to logs.

## Configuration

Copy `pipeline.example.toml`. Set `academic_email`, replace the provider `base_url` and `model`,
and set `api_key`. No provider URL, model, API style, token parameter, or request option is fixed
in code. Keep `pipeline.toml` private; it is ignored by Git and keys are never logged or saved to
SQLite.

Text-mode provider:

```toml
[extraction.provider]
base_url = "https://your-provider.example/v1"
api_key = "replace-with-your-key"
model = "your-model-id"
api_style = "chat_completions"
pdf_mode = "text"
structured_output = "auto" # strict -> json -> prompt
output_token_param = "max_tokens"
```

Native file mode requires provider support for Files, Responses, and `input_file`:

```toml
api_style = "responses"
pdf_mode = "file"
keep_remote_file = false
```

## Commands

Both `python -m paper_pipeline --help` and `paper-pipeline --help` are supported.

```bash
paper-pipeline init
paper-pipeline search --keywords "dry reforming of methane" --sources arxiv,pubmed --year 2022-2026 --total 200
paper-pipeline import-csv --input papers.csv
paper-pipeline download --concurrency 10
paper-pipeline extract --concurrency 4
paper-pipeline extract --schema schemas/custom.json
paper-pipeline run --keywords "dry reforming of methane" --total 200
paper-pipeline resume --retry-failed
paper-pipeline status
paper-pipeline retry --stage download --include-unavailable
paper-pipeline retry --stage extract
paper-pipeline export --format csv --output exports/results.csv
paper-pipeline export --format jsonl --output exports/results.jsonl --only-extracted
paper-pipeline doctor
```

Downloads log batch start and periodic progress. With `--verbose`, every completed paper is logged;
when no request completes for 15 seconds, a heartbeat reports completed and active task counts.

Repeat `--keywords` for multiple terms. `--total` is the result target per keyword; it is divided
deterministically over selected sources, with earlier source names receiving any remainder. When a
source returns fewer records than allocated, the deficit is reassigned in configured source order
to sources that filled their allocation. Cross-source and cross-keyword results are deduplicated.

Available source names are `arxiv`, `pubmed`, `crossref`, `europe_pmc`, `semantic_scholar`, and
`openalex`. OpenAlex currently requires a free API key in `crawler.openalex_api_key`; Semantic
Scholar can use `crawler.semantic_scholar_api_key` for higher and more reliable rate limits.

The downloader also understands publisher-operated routes. PLOS DOI downloads require no key.
Springer Nature's Open Access API uses `downloader.springer_nature_api_key`; Elsevier Article
Retrieval uses `downloader.elsevier_api_key`. Both keys are entered directly in `pipeline.toml`.
`use_publisher_apis = false` disables all publisher-specific routes. Elsevier still decides access
per article and may return 403; the pipeline then tries the remaining OA candidates and never uses
institutional tokens or subscription sessions.

CSV import recognizes `title, authors, year, abstract, url, source, doi, pmid, pmcid, arxiv_id,
pdf_url, file, resolved_url, status`. DOI, PMCID, PMID, arXiv ID, normalized URL, then normalized
title define deduplication priority. Valid existing PDF paths are registered without downloading.

## State and recovery

Download states are `pending/downloading/downloaded/skipped/unavailable/failed`; extraction states
are `pending/extracting/success/failed`. `resume` reads SQLite, so it survives process restarts and
does not repeat successful downloads or paid AI calls. `retry` resets classified failures without
deleting valid PDFs or successful extraction results unless extraction `--force` is explicit.

## Development and tests

```bash
ruff check .
ruff format --check .
mypy src
pytest
```

The suite covers normalization, identifier priority, metadata merge, year allocation, nested
PubMed XML, filenames/PDF headers, JSON recovery/schema validation, chunking, configuration,
migrations/upserts/retry/export, and mocked Range HTTP downloads. No test requires real network or
AI credentials.

## Known limitations

- Scanned PDFs without embedded text fail with `PDF_TEXT_EMPTY`; cloud OCR is intentionally absent.
- Landing-page PDF discovery supports standard `citation_pdf_url`; JavaScript-only pages are not
  browser-automated. Ordinary public links labelled Open PDF, View PDF, Download PDF, or Full Text
  PDF are followed, while login, purchase, subscription, and institutional-access links are skipped.
- Open-access metadata can be incomplete or stale. `REMOTE_FORBIDDEN` and `REMOTE_NOT_FOUND` are
  kept distinct from `NO_OPEN_ACCESS_PDF` and local processing failures.
- Provider-specific deviations from OpenAI-compatible Files/Responses semantics may require
  `extra_headers` or `request_options`, or a small adapter extension.
- SQLite is intended for a single local pipeline deployment, not a distributed worker fleet.

## Security and legal scope

Only explicitly public candidates from metadata, arXiv, PMC OA, Unpaywall, Semantic Scholar, PLOS,
Springer Nature OA, Elsevier's API, and publisher citation metadata are considered. Local
configuration is ignored by Git. Authorization headers and keys are never printed or persisted in
SQLite. The downloader does not supply subscription cookies or institutional authorization.
