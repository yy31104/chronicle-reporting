# Chronicle Reporting

Compare monthly economic reports across source revisions while keeping old answers reproducible.

Solo portfolio project. Built with AI assistance.

Chronicle retains source revisions, immutable snapshots and the evidence behind each calculation. The web interface compares employment changes and three-month production means. It shows both subtraction of saved decimal outputs and an exact rational difference rounded once.

The included BLS example revises January 2024 employment growth from 353 to 229 thousand persons. The Federal Reserve example changes the production mean from 102.666666666667 to 102.633333333333. Their saved-output difference is -0.033333333334; the exact difference is -1/30, displayed as -0.033333333333.

[View the comparison screen](docs/images/exact-difference.png).

## Run locally

Requires Python 3.12, uv and Docker. From a fresh checkout:

```sh
make bootstrap
make dev-up db-migrate
make demo-synthetic
PYTHONPATH=src uv run --locked python -m chronicle.cli access-config --dataset chronicle-synthetic-v1
make private-up
```

Open http://127.0.0.1:8787 and use the viewer token in `var/credentials.json`. Tokens remain in page memory. The application and database bind to loopback. Configuration creates random passwords once; it preserves existing data. `make private-down` stops the application; `make dev-down` stops the stack without deleting its volume.

For the rational rounding example, run `PYTHONPATH=src uv run --locked python scripts/seed_exact_difference_demo.py`, then add `chronicle-synthetic-exact-v1` to the local access policy. See [operations](docs/OPERATIONS.md) for configuration, import and recovery commands.

## Data and behavior

- Query a named snapshot, apply an exclusive source-date cutoff, or select the latest correction within a pinned snapshot.
- Retain original lexical decimals, units, batches and receipt times. Missing, withdrawn and unknown-time values never become zero.
- Publish cumulative snapshots with pointer compare-and-swap and verified content-addressed files. Parser repairs create a new interpretation generation.
- Replay retained reports, compare revisions, calculate descriptive revision statistics and export permitted reproducibility bundles.
- Enforce viewer/operator dataset scopes, current source-use restrictions, bounded work, database cancellation and verified backup closure.

[data/archive/releases.json](data/archive/releases.json) contains twelve attributed BLS and Board of Governors table cells from four historical publications. It is a small revision example, not an automatically updated economic feed. BLS test HTML is explicitly modeled; Federal Reserve test tables are extracted publisher data. RTDSM workbooks and compared reference tables are not distributed. Its optional comparison script accepts workbooks supplied locally.

[Time and numeric rules](docs/WORKING-CONTRACT.md), [source contract](docs/SOURCE-CONTRACT.md), [publication contract](docs/OBJECT-PUBLICATION.md) and [design decisions](docs/adr/0006-bitemporal-time-model.md) describe the boundaries. PostgreSQL role separation and HTTP scopes target a private local installation; public multi-tenant hosting is outside the deployment configuration.

## Verify

```sh
make check
make test
make test-postgres
make build
PYTHONPATH=src uv run --locked python scripts/refresh_resources.py --check
PYTHONPATH=src uv run --locked python scripts/revision_statistics_report.py --check
```

Database tests require a disposable PostgreSQL 18 instance configured with `CHRONICLE_DB_*`. They create and remove isolated databases. `make test-postgres` downloads hash-checked runtime wheels for offline bundle reconstruction. Set `CHRONICLE_DB_CONTAINER` to the disposable container for dump/restore tests. See [test setup](tests/README.md).

Chromium smoke checks use Playwright and explicitly local credentials. After creating the synthetic datasets, run `CREDENTIALS_PATH=var/credentials.json SYNTHETIC_ONLY=1 node tests/browser_smoke.cjs`. Run `tests/browser_exact_difference.cjs` for the rational rounding example. No public capacity or machine-loss recovery claim follows from local tests.

MIT licensed. Publisher data attribution and exclusions are described in [data notices](data/README.md).
