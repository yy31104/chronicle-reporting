UV ?= uv
UV_CACHE_DIR ?= $(CURDIR)/.cache/uv
export UV_CACHE_DIR

.PHONY: bootstrap check test build demo-reference dev-config dev-up dev-down db-migrate test-postgres demo-postgres wheelhouse

bootstrap:
	$(UV) sync --locked

check:
	$(UV) run --locked ruff check src tests
	$(UV) run --locked ruff format --check src tests
	PYTHONPATH=src $(UV) run --locked python -c "import chronicle; print('chronicle: package import OK')"

test:
	$(UV) run --locked pytest -m "not postgres"

build:
	$(UV) build

demo-reference:
	$(UV) run --locked python -m tests.reference.demo

dev-config:
	PYTHONPATH=src $(UV) run --locked python -m chronicle.db configure

dev-up: dev-config
	PYTHONPATH=src $(UV) run --locked python -m chronicle.compose -p chronicle up -d --wait

dev-down:
	PYTHONPATH=src $(UV) run --locked python -m chronicle.compose -p chronicle stop

db-migrate:
	PYTHONPATH=src $(UV) run --locked python -m chronicle.db migrate

# Hash-checked runtime wheels for reproducibility bundles (current platform, Python 3.12).
WHEELHOUSE ?= $(CURDIR)/var/wheelhouse
wheelhouse:
	$(UV) tool run --python 3.12 --from pip==25.3 pip download --quiet --only-binary=:all: \
		--no-deps --require-hashes -r requirements.lock -d $(WHEELHOUSE)

test-postgres: wheelhouse
	CHRONICLE_WHEELHOUSE=$(WHEELHOUSE) $(UV) run --locked pytest --tb=short -m postgres tests/postgres

demo-postgres:
	PYTHONPATH=src $(UV) run --locked python -m tests.postgres.demo

demo-synthetic:
	PYTHONPATH=src $(UV) run --locked python -m chronicle.cli demo-synthetic

serve:
	PYTHONPATH=src $(UV) run --locked python -m chronicle.cli serve

private-up:
	CHRONICLE_APP_UID=$$(id -u) CHRONICLE_APP_GID=$$(id -g) PYTHONPATH=src $(UV) run --locked python -m chronicle.compose -p chronicle -f compose.yaml -f compose.private.yaml up -d --build --wait

private-down:
	CHRONICLE_APP_UID=$$(id -u) CHRONICLE_APP_GID=$$(id -g) PYTHONPATH=src $(UV) run --locked python -m chronicle.compose -p chronicle -f compose.yaml -f compose.private.yaml stop app
